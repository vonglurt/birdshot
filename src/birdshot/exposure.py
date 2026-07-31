# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
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
    energy = max(energy, exposure_min_us * gain_min)
    e1 = motion_limit_us * gain_min
    e2 = motion_limit_us * gain_preferred_max
    e3 = shutter_hard_max_us * gain_preferred_max

    if energy <= e1:
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
    def reset(self) -> None:
        self._integral = 0.0
        self._prev_error = 0.0
        self._prev_time: Optional[float] = None
        self._meter_ema: Optional[float] = None
        self._settled_count = 0
        # Learned feed-forward constant: energy * lux ~= K for a fixed lens.
        st = self.cfg.get("state", {}) or {}
        self._k_lux: Optional[float] = st.get("k_lux")
        self._k_samples = 0

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

        # Smooth the measurement so a bird crossing frame does not swing exposure.
        alpha = float(cfg["meter_ema"])
        m = float(stats.meter)
        if self._meter_ema is None or alpha <= 0:
            self._meter_ema = m
        else:
            self._meter_ema = alpha * m + (1.0 - alpha) * self._meter_ema
        meter = max(self._meter_ema, 1.0)

        # Brightness error in EV. Positive => the frame needs more light.
        err = math.log2(max(target, 1.0) / meter)
        mode = "pid"

        # Highlight priority: clipping can only ever demand *less* exposure, and
        # when it does it overrides the brightness demand entirely.
        max_clip = max(float(cfg["max_clip_frac"]), 1e-4)
        if stats.clip_hi > max_clip:
            overage = (stats.clip_hi - max_clip) / max_clip
            clip_err = -min(3.0, 0.5 * math.log2(1.0 + overage))
            if clip_err < err:
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
        # Anti-windup: if the slew limiter is saturating, unwind the integral
        # rather than letting it accumulate against a limit it cannot beat.
        if abs(out) > slew:
            self._integral -= (out - math.copysign(slew, out)) / max(ki, _EPS) * 0.5
            self._integral = max(-clamp, min(clamp, self._integral))
            out = math.copysign(slew, out)

        energy = exposure_us * gain * (2.0 ** out)
        new_us, new_gain = self._allocate(energy)
        self._learn(lux, exposure_us * gain, False)

        return ExposureDecision(
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
        return allocate(
            energy=energy,
            motion_limit_us=float(cfg["motion_limit_us"]),
            gain_preferred_max=float(cfg["gain_preferred_max"]),
            shutter_hard_max_us=float(cfg["shutter_hard_max_us"]),
            exposure_min_us=EXPOSURE_MIN_US,
            exposure_max_us=min(EXPOSURE_MAX_US, float(cfg["shutter_hard_max_us"]) * 64),
            gain_min=GAIN_MIN,
            gain_max=GAIN_MAX,
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
