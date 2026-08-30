# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""The replay backend: recorded footage through the real pipeline.

Point it at a folder of stills (or a video file, when OpenCV is present) and
it plays them through the same analysis gates, session lifecycle and -- the
reason it exists -- the Bird Flight detector. Tuning ``bf_*`` thresholds
against footage of real birds beats tuning them against a flapping ellipse;
this is the "file/replay backend" the roadmap promised for exactly that.

Rides on :class:`SyntheticEngine`'s loop like the OpenCV backend does: only
the frame source changes. Exposure is whatever the footage was shot at, so
``_auto_expose`` is a no-op and the HUD shows the configured values, not
real ones. Playback advances one frame per engine tick (~20/s) and loops.
"""

from __future__ import annotations

import os
from typing import List, Optional

import numpy as np

from birdshot.backends.synthetic import H, SyntheticEngine, W

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")


def _decode_jpeg(data: bytes) -> Optional[np.ndarray]:
    """RGB array from encoded image bytes: simplejpeg, cv2, then Pillow."""
    try:
        import simplejpeg
        return simplejpeg.decode_jpeg(data, colorspace="RGB")
    except Exception:  # noqa: BLE001 -- wrong codec or module missing: next
        pass
    try:
        import cv2
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        return arr[:, :, ::-1] if arr is not None else None
    except ImportError:
        pass
    try:
        import io

        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    except ImportError:
        return None


class ReplayEngine(SyntheticEngine):
    """Recorded frames behind the synthetic engine's loop."""

    CAPABILITIES = frozenset({"burst", "timelapse", "birdflight"})

    def __init__(self, cfg, storage, on_event):
        super().__init__(cfg, storage, on_event)
        self.name = "camera-replay"
        self._path = os.path.expanduser(str(cfg.get("replay_path") or ""))
        self._files: List[str] = []
        self._video = None
        self._frame_i = -1
        self._color: Optional[np.ndarray] = None
        self._source_failed = False
        self._opened = False

    # ------------------------------------------------------------------
    def _open_source(self) -> bool:
        if self._opened:
            return not self._source_failed
        self._opened = True
        if not self._path or not os.path.exists(self._path):
            self._source_failed = True
            self._emit("error", {"msg": "replay: no such path %r -- set "
                                        "replay_path (the GUI's camera picker "
                                        "asks for it)" % self._path})
            return False
        if os.path.isdir(self._path):
            self._files = sorted(
                os.path.join(root, f)
                for root, dirs, files in os.walk(self._path)
                if "_rejected" not in root
                for f in files if f.lower().endswith(IMAGE_EXTS))
            if not self._files:
                self._source_failed = True
                self._emit("error", {"msg": "replay: %s holds no images"
                                            % self._path})
            return not self._source_failed
        if self._path.lower().endswith(VIDEO_EXTS):
            try:
                import cv2
                self._video = cv2.VideoCapture(self._path)
                if not self._video.isOpened():
                    raise OSError("could not open")
            except (ImportError, OSError) as exc:
                self._source_failed = True
                self._emit("error", {"msg": "replay: video needs OpenCV "
                                            "(pip install opencv-python): %r"
                                            % exc})
            return not self._source_failed
        self._files = [self._path]      # a single image still exercises gates
        return True

    # ------------------------------------------------------------------
    # frame-source hooks
    # ------------------------------------------------------------------
    def _acquire(self, t: float) -> Optional[np.ndarray]:
        if not self._open_source():
            return None
        rgb = None
        if self._video is not None:
            import cv2
            ok, frame = self._video.read()
            if not ok:  # loop
                self._video.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._video.read()
            if ok and frame is not None:
                rgb = frame[:, :, ::-1]
        else:
            self._frame_i = (self._frame_i + 1) % len(self._files)
            try:
                with open(self._files[self._frame_i], "rb") as fh:
                    rgb = _decode_jpeg(fh.read())
            except OSError:
                rgb = None
        if rgb is None:
            return None
        # Analysis geometry matches every backend: 640x480, letterboxed.
        scale = W / float(rgb.shape[1])
        new_h = max(1, int(rgb.shape[0] * scale))
        ys = np.linspace(0, rgb.shape[0] - 1, new_h).astype(np.intp)
        xs = np.linspace(0, rgb.shape[1] - 1, W).astype(np.intp)
        rgb = rgb[ys][:, xs]
        if rgb.shape[0] < H:
            rgb = np.vstack([rgb, np.zeros((H - rgb.shape[0], W, 3),
                                           rgb.dtype)])
        rgb = np.ascontiguousarray(rgb[:H])
        self._color = rgb
        y = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1]
             + 0.114 * rgb[:, :, 2])
        return np.clip(y, 0, 255).astype(np.uint8)

    def _auto_expose(self, stats, now: float):
        return None                     # the footage was already exposed

    def _seed_exposure(self) -> None:
        pass

    def _preview_rgb(self, y8: np.ndarray) -> np.ndarray:
        if self._color is not None:
            return np.ascontiguousarray(self._color[::2, ::2])
        return super()._preview_rgb(y8)

    def _capture_rgb(self, y8: np.ndarray) -> np.ndarray:
        if self._color is not None:
            return self._color
        return super()._capture_rgb(y8)

    def _lux(self) -> Optional[float]:
        return None

    def _destination_label(self) -> str:
        return ("replay %s -> %s"
                % (os.path.basename(self._path.rstrip("/")) or "?",
                   os.path.basename(self.cfg["data_root"] or "local")))

    def _close_source(self) -> None:
        if self._video is not None:
            try:
                self._video.release()
            except Exception:  # noqa: BLE001
                pass
            self._video = None
