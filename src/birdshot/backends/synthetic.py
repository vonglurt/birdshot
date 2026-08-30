# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""The synthetic backend: a generated sky with a bird in it.

Implements the engine protocol of :class:`birdshot.camera.CameraEngine` with
no camera, no libcamera and no Pi -- numpy only. The scene *responds to
exposure* (rendered luminance scales with shutter x gain), and the engine runs
the real analysis gates and the real EV-space AE controller against it, so the
GUI, metering, quality-gate and exposure code paths all exercise for real on a
development machine. What it cannot exercise: the ISP, CMA buffer pressure,
sensor mode switching, rapid/RAM capture and video -- those need the
instrument.

The scene: a graded sky, drifting cloud bands, a dark ground strip, and one
bird -- a flapping ellipse -- crossing the frame every few seconds. Not art,
but it meters like a sky, gates like a scene, and gives the future Capture
Bird Flight detector (docs/ROADMAP.md) something to hunt.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from birdshot.analysis import analyse, focus_map
from birdshot.exposure import ExposureController
from birdshot.storage import Storage

IDLE, PREVIEW, BURST, TIMELAPSE = "idle", "preview", "burst", "timelapse"
BIRDFLIGHT = "birdflight"

W, H = 640, 480
GROUND_ROWS = 64          # dark strip at the bottom, so "empty sky" isn't all there is
# Scene luminance is base * (shutter_us * gain) / NOMINAL_US_GAIN. With the
# sky's base around 0.6 this settles the AE loop near a plausible daylight
# 5-8 ms, comfortably inside the sensor limits the controller assumes.
NOMINAL_US_GAIN = 20_000.0
SIM_LUX = 4000.0          # a bright overcast day, fed to the AE seed


class SyntheticEngine(threading.Thread):
    """Same contract as CameraEngine: commands in via send(), events out via
    the callback, states idle/preview/burst/timelapse. Video and rapid modes
    report a clean error instead of pretending.

    Also the base class for other frame-source engines (the OpenCV webcam
    backend subclasses it): a source overrides the hooks ``_acquire``,
    ``_auto_expose``, ``_preview_rgb``, ``_capture_rgb``, ``_lux``,
    ``_destination_label`` and ``_close_source`` while the command loop,
    session lifecycle, analysis, preview publishing and frame writing stay
    shared."""

    def __init__(self, cfg, storage: Storage,
                 on_event: Callable[[str, Dict[str, Any]], None]):
        super().__init__(daemon=True, name="camera-synthetic")
        self.cfg = cfg
        self.storage = storage
        self.on_event = on_event
        self.controller = ExposureController(cfg)

        self._cmds: "queue.Queue[Tuple[str, dict]]" = queue.Queue()
        self._state = IDLE
        self._running = True

        st = cfg.get("state", {}) or {}
        self._exposure_us = int(st.get("last_shutter_us", cfg["manual_shutter_us"]))
        self._gain = float(st.get("last_gain", cfg["manual_gain"]))
        self._seq = 0
        self._taken = 0
        self._target_frames = 0
        self._next_shot = 0.0
        self._last_preview = 0.0
        self._last_latest = 0.0
        self._last_written: Optional[str] = None
        self._fps_window: list = []
        self._focus_assist = False
        self._focus_map_on = False
        self._encoder_missing_said = False
        self._profile: Optional[Dict[str, float]] = (
            {} if os.environ.get("BIRDSHOT_PROFILE") else None)

        # Capture Bird Flight bookkeeping.
        self._detector = None
        self._bf_burst_left = 0
        self._bf_next_ok = 0.0
        self._bf_takes = 0
        self._bf_last_report = 0.0

        self._t0 = time.monotonic()
        self._cfg_cache: Dict[str, Any] = {}
        self._cfg_cache_at = 0.0
        self._base = self._build_base_scene()

    # ------------------------------------------------------------------
    # public API -- the engine protocol
    # ------------------------------------------------------------------
    def send(self, cmd: str, **kwargs: Any) -> None:
        self._cmds.put((cmd, kwargs))

    def shutdown(self) -> None:
        self._running = False
        self._cmds.put(("stop", {}))

    @property
    def state(self) -> str:
        return self._state

    def capture_rate(self) -> float:
        if not self._fps_window:
            return 0.0
        avg = sum(self._fps_window) / len(self._fps_window)
        return 1.0 / avg if avg > 0 else 0.0

    def profile_report(self) -> Optional[Dict[str, float]]:
        if not self._profile:
            return None
        return dict(self._profile)

    # ------------------------------------------------------------------
    # scene
    # ------------------------------------------------------------------
    @staticmethod
    def _build_base_scene() -> np.ndarray:
        """Static parts of the scene, float32 luminance 0..1."""
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        # Sky: brighter at the horizon than the zenith, like a real haze.
        base = 0.52 + 0.18 * (yy / H)
        # Ground strip, textured so the contrast gate has something real.
        rng = np.random.RandomState(7)
        ground = 0.16 + 0.05 * rng.rand(GROUND_ROWS, W).astype(np.float32)
        base[H - GROUND_ROWS:] = ground
        return base

    def _render(self, t: float) -> np.ndarray:
        """The scene at time t, float32 0..1, before exposure is applied."""
        y = self._base.copy()
        # Cloud bands drifting right, gentle enough to keep the sky "sky".
        xs = np.arange(W, dtype=np.float32)
        band = 0.05 * np.sin(xs / 90.0 + t * 0.35) + 0.03 * np.sin(xs / 31.0 - t * 0.2)
        y[:H - GROUND_ROWS] += band[None, :] * (0.4 + 0.6 * (np.arange(H - GROUND_ROWS,
                                                dtype=np.float32) / H)[:, None])
        # The bird: crosses every ~9 s, flaps at ~7 Hz, occasionally absent so
        # the "empty" verdict gets exercised too.
        cx = (t * 90.0) % (W + 240.0) - 120.0
        if 0 <= cx <= W:
            cy = H * 0.32 + 34.0 * np.sin(t * 0.9)
            ry = 5.0 + 8.0 * abs(np.sin(t * 7.0))
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            mask = ((xx - cx) / 20.0) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
            y[mask] = 0.05
        return np.clip(y, 0.0, 1.0)

    def _expose(self, scene: np.ndarray) -> np.ndarray:
        """Apply the current exposure to the scene -> uint8 luma frame."""
        gain_energy = (self._exposure_us * self._gain) / NOMINAL_US_GAIN
        y = scene * (255.0 * gain_energy)
        noise = np.random.normal(0.0, 1.2 + 0.6 * self._gain, scene.shape)
        return np.clip(y + noise, 0, 255).astype(np.uint8)

    @staticmethod
    def _tint(y8: np.ndarray) -> np.ndarray:
        """Blue-ish RGB from the luma frame, half resolution like the real
        preview path (chroma-res conversion, see camera.yuv420_to_rgb)."""
        yh = y8[::2, ::2].astype(np.float32)
        rgb = np.empty(yh.shape + (3,), np.uint8)
        rgb[..., 0] = np.clip(yh * 0.72, 0, 255)
        rgb[..., 1] = np.clip(yh * 0.86, 0, 255)
        rgb[..., 2] = np.clip(yh * 1.00, 0, 255)
        return rgb

    def _cfg_dict(self) -> Dict[str, Any]:
        now = time.monotonic()
        if now - self._cfg_cache_at > 1.0:
            self._cfg_cache = self.cfg.as_dict()
            self._cfg_cache_at = now
        return self._cfg_cache

    # ------------------------------------------------------------------
    # JPEG encode: simplejpeg if present (the Pi path), else OpenCV, else
    # Pillow. A machine with none of the three still previews fine; it just
    # cannot *save* frames, and says so once.
    # ------------------------------------------------------------------
    def _encode_jpeg(self, rgb: np.ndarray) -> Optional[bytes]:
        quality = int(self.cfg["jpeg_quality"])
        try:
            import simplejpeg
            return simplejpeg.encode_jpeg(np.ascontiguousarray(rgb),
                                          quality=quality, colorspace="RGB")
        except ImportError:
            pass
        try:
            import cv2
            ok, buf = cv2.imencode(".jpg", rgb[:, :, ::-1],
                                   [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                return bytes(buf)
        except ImportError:
            pass
        try:
            import io
            from PIL import Image
            out = io.BytesIO()
            Image.fromarray(rgb).save(out, "JPEG", quality=quality)
            return out.getvalue()
        except ImportError:
            return None

    # ------------------------------------------------------------------
    # engine loop
    # ------------------------------------------------------------------
    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        try:
            self.on_event(name, payload)
        except Exception:  # noqa: BLE001 -- a GUI slot must not kill the engine
            pass

    def run(self) -> None:
        try:
            while self._running:
                self._drain_commands()
                if self._state in (PREVIEW, BURST, TIMELAPSE, BIRDFLIGHT):
                    self._tick()
                else:
                    time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "%s died: %r" % (self.name, exc),
                                 "fatal": True})
        finally:
            self._close_source()

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd, kw = self._cmds.get_nowait()
            except queue.Empty:
                return
            self._handle(cmd, kw)
            self._emit("state", {"state": self._state})

    def _handle(self, cmd: str, kw: Dict[str, Any]) -> None:
        if cmd == "preview":
            self._state = PREVIEW
        elif cmd == "burst":
            self.storage.start_session("sess")
            self._target_frames = int(kw.get("count", self.cfg["burst_count"]) or 0)
            self._taken = 0
            self.controller.reset()
            self._seed_exposure()
            self._fps_window = []
            self._state = BURST
        elif cmd == "timelapse":
            self.storage.start_session("tlc")
            self._target_frames = int(kw.get("count", self.cfg["timelapse_count"]) or 0)
            self._taken = 0
            self._next_shot = time.monotonic()
            self.controller.reset()
            self._seed_exposure()
            self._state = TIMELAPSE
        elif cmd == "birdflight":
            from birdshot.birdflight import BirdFlightDetector
            self.storage.start_session("bird")
            self._detector = BirdFlightDetector(self._cfg_dict())
            self._bf_burst_left = 0
            self._bf_next_ok = 0.0
            self._bf_takes = 0
            self._target_frames = int(kw.get("takes", self.cfg["bf_takes"]) or 0)
            self._taken = 0
            self.controller.reset()
            self._seed_exposure()
            self._state = BIRDFLIGHT
        elif cmd == "stop":
            self._stop_activity()
        elif cmd == "set_exposure":
            self._exposure_us = int(kw.get("exposure_us", self._exposure_us))
            self._gain = float(kw.get("gain", self._gain))
        elif cmd == "reset_ae":
            self.controller.reset()
        elif cmd == "focus_assist":
            self._focus_assist = bool(kw.get("on", False))
        elif cmd == "focus_map":
            self._focus_map_on = bool(kw.get("on", False))
        elif cmd == "reconfigure":
            pass  # nothing to reconfigure; the scene is always 640x480
        elif cmd in ("video", "rapid", "single"):
            self._emit("error", {"msg": "%s capture needs the real camera -- "
                                        "the synthetic backend does not do it" % cmd})

    def _seed_exposure(self) -> None:
        if not self.cfg["auto_exposure"]:
            self._exposure_us = int(self.cfg["manual_shutter_us"])
            self._gain = float(self.cfg["manual_gain"])
            return
        seeded = self.controller.seed(self._lux())
        if seeded:
            self._exposure_us, self._gain = seeded

    def _stop_activity(self) -> None:
        if self._state in (BURST, TIMELAPSE, BIRDFLIGHT):
            summary = self.storage.close_session()
            self.controller.persist()
            self.cfg.set_state(frame_seq=self._seq,
                               last_shutter_us=self._exposure_us,
                               last_gain=self._gain)
            self.cfg.save()
            if summary:
                self._emit("session", summary)
        self._state = PREVIEW

    # ------------------------------------------------------------------
    # frame-source hooks: what a subclass overrides to become a new backend
    # ------------------------------------------------------------------
    def _acquire(self, t: float) -> Optional[np.ndarray]:
        """Grab the next luma frame with exposure already applied, or None."""
        return self._expose(self._render(t))

    def _auto_expose(self, stats, now: float):
        """Run the AE loop and apply its decision. Returns the decision."""
        if not self.cfg["auto_exposure"]:
            return None
        decision = self.controller.update(stats, self._exposure_us, self._gain,
                                          lux=self._lux(), now=now)
        if decision is not None:
            self._exposure_us = decision.exposure_us
            self._gain = decision.gain
        return decision

    def _preview_rgb(self, y8: np.ndarray) -> np.ndarray:
        """Half-resolution RGB for the preview pane."""
        return self._tint(y8)

    def _capture_rgb(self, y8: np.ndarray) -> np.ndarray:
        """Full-resolution RGB for saved frames."""
        return np.repeat(y8[:, :, None], 3, axis=2)

    def _lux(self) -> Optional[float]:
        return SIM_LUX

    def _destination_label(self) -> str:
        return ("synthetic scene -> %s"
                % os.path.basename(self.cfg["data_root"] or "local"))

    def _close_source(self) -> None:
        """Release whatever the frame source holds. The scene holds nothing."""

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        t0 = time.monotonic()
        t = t0 - self._t0
        cfg_dict = self._cfg_dict()

        y8 = self._acquire(t)
        if y8 is None:
            time.sleep(0.1)
            return
        stats = analyse(y8, cfg_dict)
        decision = self._auto_expose(stats, t0)

        wrote = False
        if self._state == BURST:
            wrote = self._write_frame(y8, stats, decision)
        elif self._state == TIMELAPSE and t0 >= self._next_shot:
            self._next_shot = t0 + float(self.cfg["timelapse_interval_s"])
            wrote = self._write_frame(y8, stats, decision)
        elif self._state == BIRDFLIGHT:
            wrote = self._tick_birdflight(y8, stats, decision, t0)
        if wrote:
            self._fps_window = (self._fps_window + [time.monotonic() - t0])[-40:]
            if (self._state in (BURST, TIMELAPSE) and self._target_frames
                    and self._taken >= self._target_frames):
                self._stop_activity()
                self._emit("state", {"state": self._state})

        self._publish_preview(y8, stats, decision)
        if self._profile is not None:
            self._profile["tick"] = (time.monotonic() - t0) * 1000.0
        # ~20 fps generation: cheap, and twice the preview publish rate.
        time.sleep(max(0.0, 0.05 - (time.monotonic() - t0)))

    def _tick_birdflight(self, y8, stats, decision, now: float) -> bool:
        """One Bird Flight step: judge the frame, manage bursts and cooldown."""
        sighting = self._detector.update(y8)

        if (sighting.take and self._bf_burst_left == 0
                and now >= self._bf_next_ok):
            self._bf_burst_left = max(1, int(self.cfg["bf_burst"]))
            self._bf_takes += 1
            self._emit("bird", {"phase": "take", "take_n": self._bf_takes,
                                "sighting": sighting.to_dict()})
        elif sighting.present and now - self._bf_last_report >= 1.0:
            # A sighting that did not fire, reported at most once a second so
            # the GUI can say *why* the mode is holding its fire.
            self._bf_last_report = now
            self._emit("bird", {"phase": "sighting",
                                "sighting": sighting.to_dict()})

        wrote = False
        if self._bf_burst_left > 0:
            wrote = self._write_frame(y8, stats, decision)
            self._bf_burst_left -= 1
            if self._bf_burst_left == 0 or not wrote:
                self._bf_next_ok = now + float(self.cfg["bf_cooldown_s"])
                if (self._target_frames
                        and self._bf_takes >= self._target_frames):
                    self._stop_activity()
                    self._emit("state", {"state": self._state})
        return wrote

    def _write_frame(self, y8: np.ndarray, stats, decision) -> bool:
        jpeg = self._encode_jpeg(self._capture_rgb(y8))
        if jpeg is None:
            if not self._encoder_missing_said:
                self._encoder_missing_said = True
                self._emit("error", {"msg": "no JPEG encoder available -- install "
                                            "simplejpeg or Pillow to save frames"})
            self._stop_activity()
            return False
        self._seq += 1
        path = self.storage.write_frame(jpeg, self._exposure_us, self._gain,
                                        self._seq, stats, decision)
        if path:
            self._last_written = os.path.basename(path)
        self._taken += 1
        self._emit("frame", {
            "path": path, "seq": self._seq, "stats": stats, "decision": decision,
            "shutter_us": self._exposure_us, "gain": self._gain, "bytes": len(jpeg),
        })
        return True

    def _publish_preview(self, y8: np.ndarray, stats, decision) -> None:
        now = time.monotonic()
        if now - self._last_preview < 0.1:  # same ~10 fps cap as the real engine
            return
        self._last_preview = now
        rgb = self._preview_rgb(y8)

        if now - self._last_latest >= 1.0:
            self._last_latest = now
            jpeg = self._encode_jpeg(rgb)
            if jpeg:
                try:
                    self.storage.write_latest(jpeg)
                except Exception:  # noqa: BLE001
                    pass

        fmap = fbest = None
        fpeak = 0.0
        if self._focus_map_on:
            fmap, fbest, fpeak = focus_map(y8)
        focus_view = None
        if self._focus_assist:
            cy, cx = H // 2, W // 2
            focus_view = y8[cy - 128:cy + 128, cx - 128:cx + 128].copy()

        self._emit("preview", {
            "rgb": rgb,
            "y": y8,
            "focus_view": focus_view,
            "focus_map": fmap,
            "focus_best": fbest,
            "focus_peak": fpeak,
            "stats": stats,
            "decision": decision,
            "shutter_us": self._exposure_us,
            "gain": self._gain,
            "lux": self._lux(),
            "state": self._state,
            "taken": self._taken,
            "target": self._target_frames,
            "fps": self.capture_rate(),
            "free_mb": self.storage.free_mb(),
            "next_in": (max(0.0, self._next_shot - now)
                        if self._state == TIMELAPSE else None),
            "interval": (float(self.cfg["timelapse_interval_s"])
                         if self._state == TIMELAPSE else None),
            "last_file": self._last_written,
            "destination": self._destination_label(),
        })
