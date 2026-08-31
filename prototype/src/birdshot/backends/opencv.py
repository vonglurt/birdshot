# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""The OpenCV backend: real webcams, anywhere OpenCV can open one.

On macOS this reaches the built-in and USB cameras through AVFoundation; on
Linux it is V4L2. It rides on :class:`SyntheticEngine`'s loop -- same
commands, same events, same storage lifecycle -- and only replaces the frame
source. Two honest limitations, both inherent to webcams:

* **The device owns exposure.** UVC/AVFoundation auto-exposure cannot be
  driven by our EV-space controller, so ``_auto_expose`` is a no-op and the
  HUD shows the configured (not actual) shutter. The quality gates still run
  on what the device delivers.
* **The first open may ask for permission.** On macOS the OS pops the camera
  consent dialog for the hosting process the first time; until it is granted
  every read fails, which surfaces here as a clean error event, not a hang.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from birdshot.backends.synthetic import SyntheticEngine, W, H
from birdshot.config import WEBCAM_MODES


class OpenCVEngine(SyntheticEngine):
    """A webcam behind the synthetic engine's loop."""

    # The device owns exposure (see the module docstring), so "exposure" and
    # "lux" drop off the synthetic backend's list; "webcam_modes" is the
    # capture-request selector only this backend honours.
    CAPABILITIES = frozenset({"burst", "timelapse", "birdflight",
                              "webcam_modes"})

    def __init__(self, cfg, storage, on_event):
        super().__init__(cfg, storage, on_event)
        self.name = "camera-opencv"
        self._index = int(cfg.get("camera_index", 0) or 0)
        self._cap = None
        self._cap_failed = False
        self._grab_error_at = 0.0
        self._color: Optional[np.ndarray] = None       # analysis-size RGB
        self._color_full: Optional[np.ndarray] = None  # native RGB, for saves

    # ------------------------------------------------------------------
    def _ensure_cap(self):
        if self._cap is not None or self._cap_failed:
            return self._cap
        try:
            import cv2
        except ImportError:
            self._cap_failed = True
            self._emit("error", {"msg": "OpenCV backend selected but cv2 is not "
                                        "importable -- pip install opencv-python"})
            return None
        cap = cv2.VideoCapture(self._index)
        if not cap.isOpened():
            cap.release()
            self._cap_failed = True
            self._emit("error", {"msg": "camera %d did not open -- unplugged, in "
                                        "use, or camera permission not granted "
                                        "to this process" % self._index})
            return None
        # The device negotiates down from the request (see WEBCAM_MODES), so
        # the first entry is effectively "your best".
        idx = max(0, min(int(self.cfg.get("webcam_mode", 0)),
                         len(WEBCAM_MODES) - 1))
        req_w, req_h, _label = WEBCAM_MODES[idx]
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, req_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, req_h)
        got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        self._emit("camera", {"msg": "webcam %d: asked %dx%d, delivering %dx%d"
                                     % (self._index, req_w, req_h, got_w, got_h)})
        self._cap = cap
        return cap

    def _handle(self, cmd, kw):
        if cmd == "reconfigure":
            # Reopen with the (possibly changed) webcam_mode request; the
            # next _acquire redoes the negotiation and reports it.
            self._close_source()
            self._cap_failed = False
            return
        super()._handle(cmd, kw)

    # ------------------------------------------------------------------
    # frame-source hooks
    # ------------------------------------------------------------------
    def _acquire(self, t: float) -> Optional[np.ndarray]:
        import time
        cap = self._ensure_cap()
        if cap is None:
            return None
        import cv2
        ok, frame = cap.read()
        if not ok or frame is None:
            now = time.monotonic()
            if now - self._grab_error_at > 5.0:   # say it, but do not spam
                self._grab_error_at = now
                self._emit("error", {"msg": "camera %d stopped delivering frames"
                                            % self._index})
            return None
        # Saves get what the camera delivered. The analysis (and preview)
        # copy is letterboxed to 640x480 like every other backend -- saving
        # THAT was why webcam captures used to come out tiny.
        self._color_full = np.ascontiguousarray(frame[:, :, ::-1])
        frame = cv2.resize(frame, (W, int(W * frame.shape[0] / frame.shape[1])))
        if frame.shape[0] < H:
            pad = np.zeros((H - frame.shape[0], W, 3), frame.dtype)
            frame = np.vstack([frame, pad])
        frame = frame[:H]
        self._color = frame[:, :, ::-1]
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def _auto_expose(self, stats, now: float):
        return None                                # the device owns exposure

    def _seed_exposure(self) -> None:
        pass

    def _preview_rgb(self, y8: np.ndarray) -> np.ndarray:
        if self._color is not None:
            return np.ascontiguousarray(self._color[::2, ::2])
        return super()._preview_rgb(y8)

    def _capture_rgb(self, y8: np.ndarray) -> np.ndarray:
        if self._color_full is not None:
            return self._color_full                # native resolution
        if self._color is not None:
            return np.ascontiguousarray(self._color)
        return super()._capture_rgb(y8)

    def _lux(self) -> Optional[float]:
        return None                                # nothing meters lux for us

    def _destination_label(self) -> str:
        import os
        return ("camera %d -> %s"
                % (self._index, os.path.basename(self.cfg["data_root"] or "local")))

    def _close_source(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None
