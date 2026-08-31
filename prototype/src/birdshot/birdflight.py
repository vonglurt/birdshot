# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Capture Bird Flight: decide, frame by frame, whether NOW is the shot.

The mode watches the luma stream for a *bird-shaped opportunity*: a discrete
dark subject surrounded by bright sky, sharp along its boundary, well inside
the frame, in a frame that is mostly sky. When every gate agrees, the engine
fires a burst; this module only judges.

All gates read plain config keys (``bf_*``), all adjustable from the GUI's
Bird Flight section:

    capture   bf_burst, bf_cooldown_s
    subject   bf_subject_luma_max, bf_min_area_frac, bf_max_area_frac,
              bf_ring_sky_frac
    sky       bf_sky_luma_min, bf_sky_min_frac
    quality   bf_min_sharpness
    framing   bf_margin_frac
    motion    bf_require_motion, bf_motion_min

Pure numpy, no camera types: the same detector runs against the synthetic
scene, a webcam, or the IMX477 -- and in the selftest against arrays built
in-line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# The subject search runs on a 4x-downsampled mask: at 160x120 the connected-
# component pass is microseconds, and a bird smaller than 4 px was never going
# to pass the sharpness gate anyway.
DOWN = 4


@dataclass
class Sighting:
    """One frame's verdict. ``take`` means every gate passed."""
    present: bool = False           # a plausible subject exists at all
    take: bool = False
    reasons: List[str] = field(default_factory=list)   # why NOT taken
    # measurements, for the GUI readout and the EXIF UserComment
    motion_frac: float = 0.0
    sky_frac: float = 0.0
    area_frac: float = 0.0
    ring_sky_frac: float = 0.0
    sharpness: float = 0.0
    centroid: Optional[Tuple[float, float]] = None     # (x, y) full-res px
    bbox: Optional[Tuple[int, int, int, int]] = None   # x0, y0, x1, y1 full-res

    def to_dict(self) -> Dict[str, Any]:
        return {
            "present": self.present, "take": self.take, "reasons": self.reasons,
            "motion_frac": round(self.motion_frac, 5),
            "sky_frac": round(self.sky_frac, 3),
            "area_frac": round(self.area_frac, 5),
            "ring_sky_frac": round(self.ring_sky_frac, 3),
            "sharpness": round(self.sharpness, 1),
            "centroid": self.centroid, "bbox": self.bbox,
        }


def _largest_blob(mask: np.ndarray) -> Optional[Tuple[np.ndarray, int]]:
    """Largest 4-connected component that does NOT touch the frame border.

    Border-touching dark regions are never the subject: the ground strip, a
    roofline, a tree at the edge, or a bird half out of frame -- all things
    the mode must wait out, not shoot. Plain BFS: the mask is 160x120, scipy
    is not a dependency, and the constant factor does not matter at that size.
    """
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: Optional[np.ndarray] = None
    best_n = 0
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys, xs):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        blob = []
        touches_border = False
        while stack:
            y, x = stack.pop()
            blob.append((y, x))
            if y in (0, h - 1) or x in (0, w - 1):
                touches_border = True
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if not touches_border and len(blob) > best_n:
            best_n = len(blob)
            b = np.zeros_like(mask, dtype=bool)
            for y, x in blob:
                b[y, x] = True
            best = b
    if best is None:
        return None
    return best, best_n


def boundary_sharpness(y8: np.ndarray, bbox: Tuple[int, int, int, int]) -> float:
    """How crisp the subject's edges are, 0..~100.

    Mean of the top-decile gradient magnitudes inside the (padded) subject
    box. Sky is flat, so nearly all gradient energy in the box IS the
    subject boundary -- a sharp wing scores high, a motion-blurred or
    out-of-focus one scores low. Whole-frame sharpness would not work here:
    an empty sky is "sharp" by having no detail to blur.
    """
    x0, y0, x1, y1 = bbox
    pad = 6
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(y8.shape[1], x1 + pad), min(y8.shape[0], y1 + pad)
    patch = y8[y0:y1, x0:x1].astype(np.float32)
    if patch.shape[0] < 4 or patch.shape[1] < 4:
        return 0.0
    gx = np.abs(np.diff(patch, axis=1))
    gy = np.abs(np.diff(patch, axis=0))
    grads = np.concatenate([gx.ravel(), gy.ravel()])
    if grads.size == 0:
        return 0.0
    top = np.partition(grads, -max(1, grads.size // 10))[-max(1, grads.size // 10):]
    return float(top.mean()) * (100.0 / 255.0)


class BirdFlightDetector:
    """Feed it frames; it tells you when to fire."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._prev: Optional[np.ndarray] = None

    def _k(self, key: str, default):
        v = self.cfg.get(key, default)
        return default if v is None else v

    def update(self, y8: np.ndarray) -> Sighting:
        s = Sighting()
        cfg = self._k

        small = y8[::DOWN, ::DOWN]
        sky_min = float(cfg("bf_sky_luma_min", 110))
        sky = small >= sky_min
        s.sky_frac = float(sky.mean())

        # Motion gate first: it is nearly free and rejects the static scene.
        prev, self._prev = self._prev, small.astype(np.int16)
        if prev is not None:
            s.motion_frac = float((np.abs(self._prev - prev) > 12).mean())
        if cfg("bf_require_motion", True):
            if prev is None or s.motion_frac < float(cfg("bf_motion_min", 0.0005)):
                s.reasons.append("no motion")

        # A subject: dark, discrete, the largest such blob.
        subject = small <= float(cfg("bf_subject_luma_max", 80))
        found = _largest_blob(subject)
        if found is None:
            s.reasons.append("no subject")
            return s
        blob, n = found
        frame_px = small.shape[0] * small.shape[1]
        s.area_frac = n / float(frame_px)

        ys, xs = np.nonzero(blob)
        bx0, bx1 = int(xs.min()), int(xs.max()) + 1
        by0, by1 = int(ys.min()), int(ys.max()) + 1
        s.centroid = (float(xs.mean()) * DOWN, float(ys.mean()) * DOWN)
        s.bbox = (bx0 * DOWN, by0 * DOWN, bx1 * DOWN, by1 * DOWN)

        if s.area_frac < float(cfg("bf_min_area_frac", 0.0004)):
            s.reasons.append("subject too small")
            return s
        s.present = True
        if s.area_frac > float(cfg("bf_max_area_frac", 0.05)):
            s.reasons.append("subject too large (not a bird, or too close)")

        # Against sky: the ring around the box must be bright. This is what
        # separates a bird from a branch, a roofline, or the ground strip.
        rp = 3
        rx0, ry0 = max(0, bx0 - rp), max(0, by0 - rp)
        rx1 = min(small.shape[1], bx1 + rp)
        ry1 = min(small.shape[0], by1 + rp)
        ring = np.zeros_like(small, dtype=bool)
        ring[ry0:ry1, rx0:rx1] = True
        ring[by0:by1, bx0:bx1] = False
        if ring.any():
            s.ring_sky_frac = float(sky[ring].mean())
        if s.ring_sky_frac < float(cfg("bf_ring_sky_frac", 0.85)):
            s.reasons.append("not against sky")

        # Composition: mostly sky, subject inside the margins.
        if s.sky_frac < float(cfg("bf_sky_min_frac", 0.5)):
            s.reasons.append("frame not sky enough")
        margin = float(cfg("bf_margin_frac", 0.08))
        mh, mw = y8.shape[0] * margin, y8.shape[1] * margin
        cx, cy = s.centroid
        if not (mw <= cx <= y8.shape[1] - mw and mh <= cy <= y8.shape[0] - mh):
            s.reasons.append("subject too near the edge")

        # Focus, judged where it matters: on the subject boundary.
        s.sharpness = boundary_sharpness(y8, s.bbox)
        if s.sharpness < float(cfg("bf_min_sharpness", 12.0)):
            s.reasons.append("boundary not sharp enough")

        s.take = not s.reasons
        return s
