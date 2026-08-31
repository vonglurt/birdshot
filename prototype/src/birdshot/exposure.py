# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Auto-exposure: EV-space PID with highlight priority and a lux feed-forward.

Why not just use libcamera's AGC/AEC? Two reasons, both of which bite hard on
this workload:

1. It re-converges from scratch and needs roughly five frames to settle, which
   at 4 fps is over a second of wasted burst every time the scene changes.
2. It meters for the whole frame. Pointing a whole-frame meter at bright sky
   guarantees the bird -- the only thing we care about -- comes out a silhouette.

So AeEnable is switched off permanently and exposure is driven from here.

Three things act together:

* **Feed-forward** from the sensor's own lux estimate. Scene luminance and
  required exposure are inversely proportional, so a single learned constant K
  turns a lux reading into an exposure guess. When the camera swings from
  treeline to open sky the feed-forward jumps immediately, in one frame, rather
  than integrating its way there.
* **PID feedback** on the metering error, expressed in EV (log2) because
  exposure is multiplicative -- a linear controller on raw microseconds would be
  wildly mistuned at one end of the range or the other.
* **Highlight priority.** The clipping term can only ever push exposure *down*,
  and it overrides the brightness term when it fires. Sky detail lost to
  clipping is unrecoverable; a slightly dark bird is not.

The output EV is then allocated to a shutter/gain pair by a ladder that keeps
the shutter as short as the light allows, so wingbeats stay frozen.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .analysis import FrameStats

_EPS = 1e-6

# Beyond this error we stop trusting the PID and jump straight to the answer.
# This is what removes the "AE takes five frames" problem.
FAST_ACQUIRE_EV = 1.5
FAST_ACQUIRE_CLAMP_EV = 4.0
SETTLED_FRAMES = 3
# Below this correction the loop is holding station, even if the error itself
# cannot be closed because another constraint is pushing back.
STABLE_OUTPUT_EV = 0.10


@dataclass
class ExposureDecision:
    exposure_us: int
    gain: float
    ev_error: float = 0.0
    ev_output: float = 0.0
    meter: float = 0.0
    target: float = 0.0
    settled: bool = False
    mode: str = "pid"  # pid | acquire | highlight | manual | feedforward
    p: float = 0.0
    i: float = 0.0
    d: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exposure_us": self.exposure_us,
            "gain": round(self.gain, 3),
            "ev_error": round(self.ev_error, 3),
            "ev_output": round(self.ev_output, 3),
            "meter": round(self.meter, 2),
            "target": round(self.target, 2),
            "settled": self.settled,
            "mode": self.mode,
        }


def allocate(
    energy: float,
    motion_limit_us: float,
    gain_preferred_max: float,
    shutter_hard_max_us: float,
    exposure_min_us: float,
    exposure_max_us: float,
    gain_min: float,
    gain_max: float,
    prefer_exposure: bool = False,
) -> Tuple[int, float]:
    """Split a required exposure "energy" (us x gain) into shutter and gain.

    The ladder, in increasing order of how much light the scene demands:

    1. base gain, shutter grows to the motion limit      -- cleanest
    2. shutter pinned at the motion limit, gain grows    -- still frozen
    3. gain pinned at its preferred cap, shutter grows   -- blur risk begins
    4. shutter at its hard cap, gain grows to the max    -- last resort

    Shutter is therefore always the shortest one that reaches the target at an
    acceptable noise level, which is what "smallest duration" asks for.
    """
    motion_limit_us = max(1.0, float(motion_limit_us))
    shutter_hard_max_us = max(motion_limit_us, float(shutter_hard_max_us))
    gain_min = max(1e-3, float(gain_min))
    gain_preferred_max = max(gain_min, float(gain_preferred_max))
    gain_max = max(gain_preferred_max, float(gain_max))
    energy = max(energy, exposure_min_us * gain_min)
    e1 = motion_limit_us * gain_min
    e2 = motion_limit_us * gain_preferred_max
    e3 = shutter_hard_max_us * gain_preferred_max

    if prefer_exposure:
        # Exposure-priority: spend duration first and treat gain as the last
        # resort. Gain buys brightness at the cost of noise it can never give
        # back, whereas a longer exposure is free until motion smears -- so the
        # shutter runs all the way to its hard cap before gain moves at all.
        if energy <= shutter_hard_max_us * gain_min:
            t, g = energy / gain_min, gain_min
        elif energy <= shutter_hard_max_us * gain_preferred_max:
            t, g = shutter_hard_max_us, energy / shutter_hard_max_us
        else:
            t, g = shutter_hard_max_us, energy / shutter_hard_max_us
    elif energy <= e1:
        t, g = energy / gain_min, gain_min
    elif energy <= e2:
        t, g = motion_limit_us, energy / motion_limit_us
    elif energy <= e3:
        t, g = energy / gain_preferred_max, gain_preferred_max
    else:
        t, g = shutter_hard_max_us, energy / shutter_hard_max_us

    t = int(round(max(exposure_min_us, min(exposure_max_us, t))))
    g = max(gain_min, min(gain_max, g))
    return t, g


class ExposureController:
    """Feed-forward + PID auto-exposure. One instance per capture session."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    # ------------------------------------------------------------------
    def set_limits(self, limits) -> None:
        """Real per-mode limits from the sensor, so the ladder stays reachable."""
        self._limits = dict(limits or {})

    def resync(self, exposure_us: int, gain: float) -> None:
        """Adopt what the sensor actually did as the new operating point.

        Called when a request was clamped. The integral term is cleared because
        it accumulated against a target that could never be reached; leaving it
        wound up would immediately push straight back into the same clamp.
        """
        self._integral = 0.0
        self._prev_error = 0.0
        self._settled_count = 0
        self._stable_count = 0
        self._window = []

    def reset(self) -> None:
        self._integral = 0.0
        self._prev_error = 0.0
        self._stable_count = 0
        self._prev_time: Optional[float] = None
        self._window: list = []
        self._settled_count = 0
        # Learned feed-forward constant: energy * lux ~= K for a fixed lens.
        st = self.cfg.get("state", {}) or {}
        self._k_lux: Optional[float] = st.get("k_lux")
        self._k_samples = 0
        if not hasattr(self, "_limits"):
            self._limits = {}

    # ------------------------------------------------------------------
    def target_luma(self) -> float:
        """Metering target, biased by calibration when the wizard has run.

        With sky and treeline both measured we know the scene's dynamic range.
        A wide range means we must sit lower in the histogram to keep highlight
        headroom; a narrow one lets us expose brighter for a cleaner subject.
        """
        base = float(self.cfg["target_luma"])
        cal = self.cfg.get("calibration") or {}
        dr = cal.get("dynamic_range_ev")
        if not cal.get("done") or not dr:
            return base
        # Pull the target down by up to 25% as the range widens past 4 EV.
        excess = max(0.0, min(4.0, float(dr) - 4.0))
        return base * (1.0 - 0.0625 * excess)

    # ------------------------------------------------------------------
    def _feedforward(self, lux: Optional[float]) -> Optional[float]:
        if not lux or lux <= _EPS or not self._k_lux:
            return None
        return self._k_lux / lux

    def _learn(self, lux: Optional[float], energy: float, settled: bool) -> None:
        """Update the lux->energy constant, but only from well-exposed frames."""
        if not settled or not lux or lux <= _EPS:
            return
        k = energy * lux
        if self._k_lux is None:
            self._k_lux = k
        else:
            self._k_lux = 0.9 * self._k_lux + 0.1 * k
        self._k_samples += 1
        if self._k_samples % 20 == 0:
            self.cfg.set_state(k_lux=self._k_lux)

    def persist(self) -> None:
        """Checkpoint the learned feed-forward constant.

        Called when a session ends so a short run does not throw away what it
        learned -- the periodic checkpoint above only fires every 20 samples.
        """
        if self._k_lux:
            self.cfg.set_state(k_lux=self._k_lux)

    # ------------------------------------------------------------------
    def update(
        self,
        stats: FrameStats,
        exposure_us: int,
        gain: float,
        lux: Optional[float] = None,
        now: Optional[float] = None,
    ) -> ExposureDecision:
        """Given how the last frame came out, decide the next frame's exposure."""
        cfg = self.cfg
        now = now if now is not None else time.monotonic()
        dt = 0.25 if self._prev_time is None else max(1e-3, min(2.0, now - self._prev_time))
        self._prev_time = now

        target = self.target_luma()

        # Average the last N readings rather than exponentially smoothing. A
        # window is easier to reason about and, taken with the half-step damping
        # below, is what stops the loop crawling upward and dancing around its
        # target: a single bright frame can no longer move it far, and no single
        # correction overshoots.
        # Filtering and responsiveness pull against each other, so the filter is
        # scheduled on how big the error is.
        #
        # Measured with a 2-frame control latency and 6% metering noise:
        #   unfiltered      wander 0.399 EV, 167 changes per 200 frames -- the dance
        #   median-3        wander 0.000 EV, dead still
        # but on a step change in light:
        #   unfiltered      settles in 2 frames, no overshoot
        #   median-3        277% overshoot, because the median lags the step
        #
        # So: use the raw reading when the scene has genuinely changed, and the
        # median once it is steady. Big moves stay instant, small ones stop
        # chasing noise.
        n = max(1, int(cfg.get("ae_average_n", 3)))
        mode = str(cfg.get("ae_average_mode", "median"))
        raw = max(float(stats.meter), 1.0)
        self._window.append(raw)
        while len(self._window) > n:
            self._window.pop(0)

        if abs(math.log2(max(target, 1.0) / raw)) > FAST_ACQUIRE_EV:
            meter = raw                      # the light really moved
            self._window = [raw]             # do not average across the step
        elif mode == "mean":
            meter = sum(self._window) / len(self._window)
        elif mode == "median":
            meter = sorted(self._window)[len(self._window) // 2]
        else:
            meter = raw
        meter = max(meter, 1.0)

        # Brightness error in EV. Positive => the frame needs more light.
        err = math.log2(max(target, 1.0) / meter)
        mode = "pid"

        # Highlight priority: clipping can only ever demand *less* exposure, and
        # when it does it overrides the brightness demand entirely.
        #
        # Crucially it is measured on the SUBJECT zone, with the sky counted at
        # a fraction. Metering whole-frame clipping against a bright sky meant
        # the sky always exceeded the tolerance, so the term fired on every
        # frame and drove exposure down until the treeline went black -- the
        # loop sat there dark and never came back. Clipped sky is expected for
        # this subject matter; clipped *subject* is what must pull exposure down.
        # The two zones get separate tolerances rather than one weighted sum. A
        # sky clipping 72% still swamps a 2% budget even at a quarter weight, so
        # weighting alone left the loop stuck dark. The sky is allowed to clip
        # heavily before it says anything, and when it does it only nudges.
        max_clip = max(float(cfg["max_clip_frac"]), 1e-4)
        sky_tol = max(float(cfg.get("sky_clip_tolerance", 0.60)), 1e-4)

        clip_err = 0.0
        if stats.subject_clip_hi > max_clip:
            overage = (stats.subject_clip_hi - max_clip) / max_clip
            clip_err = -min(3.0, 0.5 * math.log2(1.0 + overage))
        if stats.sky_clip_hi > sky_tol:
            overage = (stats.sky_clip_hi - sky_tol) / sky_tol
            # Capped and gentle: losing sky detail is the accepted trade here,
            # so this only trims, it never drives.
            clip_err = min(clip_err, -min(0.75, 0.35 * math.log2(1.0 + overage)))
        if clip_err < 0.0 and clip_err < err:
            err, mode = clip_err, "highlight"

        # Deadband: stop hunting once we are close enough.
        deadband = float(cfg["pid_deadband_ev"])
        if abs(err) < deadband and mode != "highlight":
            self._settled_count += 1
            self._integral *= 0.85  # bleed off so it cannot creep
            self._prev_error = err
            energy = exposure_us * gain
            self._learn(lux, energy, True)
            return ExposureDecision(
                exposure_us=exposure_us,
                gain=gain,
                ev_error=err,
                ev_output=0.0,
                meter=meter,
                target=target,
                settled=self._settled_count >= SETTLED_FRAMES,
                mode="settled",
            )

        self._settled_count = 0

        # Fast acquire: a large error means the scene changed, not that the
        # loop is mistuned. Jump rather than integrate.
        if abs(err) > FAST_ACQUIRE_EV:
            out = max(-FAST_ACQUIRE_CLAMP_EV, min(FAST_ACQUIRE_CLAMP_EV, err))
            self._integral = 0.0
            self._prev_error = err
            energy = exposure_us * gain * (2.0 ** out)
            new_us, new_gain = self._allocate(energy)
            return ExposureDecision(
                exposure_us=new_us,
                gain=new_gain,
                ev_error=err,
                ev_output=out,
                meter=meter,
                target=target,
                mode="acquire",
            )

        # ---- PID -------------------------------------------------------
        kp, ki, kd = float(cfg["pid_kp"]), float(cfg["pid_ki"]), float(cfg["pid_kd"])
        clamp = float(cfg["pid_integral_clamp_ev"])
        slew = float(cfg["pid_slew_ev"])

        p_term = kp * err
        self._integral = max(-clamp, min(clamp, self._integral + err * dt))
        i_term = ki * self._integral
        d_term = kd * (err - self._prev_error) / dt
        self._prev_error = err

        out = p_term + i_term + d_term
        # Move only part of the way toward the correction. With a two-frame
        # control latency, applying the full step means the next frame still
        # shows the old exposure, so the loop corrects again and overshoots --
        # that is the dance. Half a step per frame converges just as fast in
        # practice and stops the hunting.
        out *= max(0.05, min(1.0, float(cfg.get("ae_damping", 0.5))))

        # Anti-windup: if the slew limiter is saturating, unwind the integral
        # rather than letting it accumulate against a limit it cannot beat.
        if abs(out) > slew:
            self._integral -= (out - math.copysign(slew, out)) / max(ki, _EPS) * 0.5
            self._integral = max(-clamp, min(clamp, self._integral))
            out = math.copysign(slew, out)

        energy = exposure_us * gain * (2.0 ** out)
        new_us, new_gain = self._allocate(energy)

        # A constrained equilibrium counts as settled. When highlight priority
        # and the brightness term pull against each other -- routine when
        # exposing a dark subject under a blown sky -- the error never enters
        # the deadband, but the loop has still converged: its output has gone to
        # nothing. Requiring a small error instead of a small correction meant
        # "settled" never fired in exactly the scene this camera is pointed at,
        # which in turn stopped the lux constant ever being learned.
        if abs(out) < STABLE_OUTPUT_EV:
            self._stable_count += 1
        else:
            self._stable_count = 0
        converged = self._stable_count >= SETTLED_FRAMES
        self._learn(lux, exposure_us * gain, converged)

        return ExposureDecision(
            settled=converged,
            exposure_us=new_us,
            gain=new_gain,
            ev_error=err,
            ev_output=out,
            meter=meter,
            target=target,
            mode=mode,
            p=p_term,
            i=i_term,
            d=d_term,
        )

    # ------------------------------------------------------------------
    def _allocate(self, energy: float) -> Tuple[int, float]:
        from .config import EXPOSURE_MAX_US, EXPOSURE_MIN_US, GAIN_MAX, GAIN_MIN

        cfg = self.cfg
        # Prefer the limits the sensor reported for the active mode over the
        # datasheet ones -- a binned 41 fps mode caps exposure far below the
        # full-resolution maximum, and asking beyond it just gets clamped.
        e_lo, e_hi = self._limits.get("exposure", (EXPOSURE_MIN_US, EXPOSURE_MAX_US))
        g_lo, g_hi = self._limits.get("gain", (GAIN_MIN, GAIN_MAX))
        # Belt and braces: a bad or unknown range must never collapse the ladder.
        if not (e_hi > e_lo > 0):
            e_lo, e_hi = EXPOSURE_MIN_US, EXPOSURE_MAX_US
        if not (g_hi > g_lo > 0):
            g_lo, g_hi = GAIN_MIN, GAIN_MAX
        hard_max = max(1.0, min(float(cfg["shutter_hard_max_us"]), e_hi))
        return allocate(
            energy=energy,
            motion_limit_us=max(1.0, min(float(cfg["motion_limit_us"]), hard_max)),
            gain_preferred_max=min(float(cfg["gain_preferred_max"]), g_hi),
            shutter_hard_max_us=hard_max,
            exposure_min_us=max(EXPOSURE_MIN_US, e_lo),
            exposure_max_us=min(EXPOSURE_MAX_US, e_hi),
            gain_min=max(GAIN_MIN, g_lo),
            gain_max=min(GAIN_MAX, g_hi),
            prefer_exposure=bool(cfg.get("prefer_exposure_time", True)),
        )

    # ------------------------------------------------------------------
    def seed(self, lux: Optional[float]) -> Optional[Tuple[int, float]]:
        """Cold-start guess from a lux reading, if we have learned the constant.

        Used on session start and after a mode change so the very first frame of
        a burst is usable instead of being thrown away while AE converges.
        """
        energy = self._feedforward(lux)
        if energy is None:
            return None
        return self._allocate(energy)
