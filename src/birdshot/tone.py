# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Output tone curve for the IMX477.

**The HQ camera's gamma curve is already applied.** libcamera's tuning file for
this sensor -- ``rpi.contrast`` in ``/usr/share/libcamera/ipa/rpi/vc4/imx477.json``
-- carries the 33-point curve that appears in the Raspberry Pi documentation, and
the ISP applies it in hardware to every frame before we ever see it:

    x: 0, 1024, 2048, 3072, 4096, ...   (16-bit linear input)
    y: 0, 5040, 9338, 12356, 15312, ... (16-bit gamma-corrected output)

Two measurements decide how tone control is done here:

* **Re-applying that curve in software would double it.** A linear 0.10 maps to
  0.332 once, but 0.717 twice -- 116% too bright. The image would wash out.
* **A software LUT is not fast.** Measured on this CM4: 449 ms per frame at
  4056x3040 and 105 ms at 2028x1520, which would drop full-resolution capture
  from 4.5 fps to about 2.

So tone control works by *replacing the curve the ISP uses* rather than
post-processing pixels. The hardware then applies it for free -- no CPU cost at
any resolution, and no double-gamma.

The catch is that the ISP reads its tuning once, when the camera opens, so
changing the curve restarts the camera (about a second). Contrast and brightness
are separately available as live libcamera controls that need no restart.
"""

from __future__ import annotations

import copy
import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

TUNING_CANDIDATES = [
    "/usr/share/libcamera/ipa/rpi/vc4/imx477.json",
    "/usr/share/libcamera/ipa/raspberrypi/imx477.json",
]

FULL = 65535

# The stock HQ-camera curve, kept here so the module works even if the tuning
# file moves. This is the vector plotted as "Gamma Ref-Vector HQ-Cam".
STOCK_X = [0, 1024, 2048, 3072, 4096, 5120, 6144, 7168, 8192, 9216, 10240,
           11264, 12288, 13312, 14336, 15360, 16384, 18432, 20480, 22528,
           24576, 26624, 28672, 30720, 32768, 36864, 40960, 45056, 49152,
           53248, 57344, 61440, 65535]
STOCK_Y = [0, 5040, 9338, 12356, 15312, 18051, 20790, 23193, 25744, 27942,
           30035, 32005, 33975, 35815, 37600, 39168, 40642, 43379, 45749,
           47753, 49621, 51253, 52698, 53796, 54876, 57012, 58656, 59954,
           61183, 62355, 63419, 64476, 65535]

PRESETS = [
    ("stock", "Stock HQ-cam curve (what the ISP already does)"),
    ("linear", "Linear - no gamma at all, flat and dark"),
    ("gamma", "Plain power curve, set by the gamma value"),
    ("contrast", "Stock curve with contrast scaled about mid-grey"),
    ("lift", "Stock curve with shadows lifted or crushed"),
    ("rolloff", "Soft shoulder: rounds off blown whites and lifts a dark average"),
    ("levels", "Black/white points set on the histogram, with rounded knees"),
]


def knee_map(t: float, k: float) -> float:
    """Map t to (0,1): linear in the middle, exponential knees at both ends.

    Straight clipping throws away everything past the set points -- blown white
    is blown, and no amount of grading brings it back. This instead bends
    asymptotically toward 0 and 1, so tones near the ends compress rather than
    disappear, and nothing ever actually reaches a hard stop.

    The knees join the linear segment with matching slope, so there is no visible
    kink where they meet.
    """
    if k <= 0.0:
        return min(1.0, max(0.0, t))
    k = min(k, 0.49)
    if t < k:
        return k * math.exp((t - k) / k)
    if t > 1.0 - k:
        return 1.0 - k * math.exp(-(t - (1.0 - k)) / k)
    return t


def tuning_path() -> Optional[str]:
    for p in TUNING_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def stock_curve() -> Tuple[List[float], List[float]]:
    """The ISP's current curve, normalised to 0..1. Read from the tuning file
    when available so we reflect reality rather than a hard-coded copy."""
    path = tuning_path()
    if path:
        try:
            with open(path) as fh:
                data = json.load(fh)
            algos = data["algorithms"] if isinstance(data.get("algorithms"), list) else [data]
            for algo in algos:
                for name, body in algo.items():
                    if "contrast" in name and "gamma_curve" in body:
                        g = body["gamma_curve"]
                        xs = [v / FULL for v in g[0::2]]
                        ys = [v / FULL for v in g[1::2]]
                        return xs, ys
        except (OSError, ValueError, KeyError):
            pass
    return [v / FULL for v in STOCK_X], [v / FULL for v in STOCK_Y]


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            span = xs[i] - xs[i - 1] or 1e-9
            f = (x - xs[i - 1]) / span
            return ys[i - 1] + f * (ys[i] - ys[i - 1])
    return ys[-1]


def build_curve(kind: str = "stock", gamma: float = 2.2, contrast: float = 1.0,
                lift: float = 0.0, knee: float = 0.65,
                shoulder: float = 2.0, black: float = 0.0,
                white: float = 1.0,
                knee_soft: float = 0.12) -> Tuple[List[float], List[float]]:
    """Return (xs, ys) normalised 0..1 for the requested tone curve."""
    xs, ys = stock_curve()
    kw_knee, kw_shoulder = knee, shoulder
    kw_black, kw_white, kw_knee_soft = black, white, knee_soft

    if kind == "linear":
        return list(xs), list(xs)

    if kind == "gamma":
        g = max(0.05, float(gamma))
        return list(xs), [pow(x, 1.0 / g) for x in xs]

    if kind == "contrast":
        # Pivot about the curve's own value at 18% grey, which keeps a
        # photographic mid-tone anchored while the ends open or close.
        mid = _interp(0.18, xs, ys)
        k = max(0.05, float(contrast))
        out = [min(1.0, max(0.0, mid + (y - mid) * k)) for y in ys]
        out[0], out[-1] = 0.0, 1.0
        return list(xs), out

    if kind == "levels":
        # Black and white points, as set on the histogram. Everything between
        # them is stretched to the full range -- which is what "expand the
        # centre" means, and why the mid-tones become easier to judge: a scene
        # occupying 0.15-0.70 now uses all of 0..1 instead of half of it.
        b = min(0.95, max(0.0, float(kw_black)))
        w = max(b + 0.02, min(1.0, float(kw_white)))
        k = max(0.0, min(0.49, float(kw_knee_soft)))
        out = []
        for x, y in zip(xs, ys):
            t = (y - b) / (w - b)
            out.append(min(1.0, max(0.0, knee_map(t, k))))
        out[0] = 0.0
        out[-1] = 1.0
        return list(xs), out

    if kind == "rolloff":
        # Two jobs at once, which is the shape you get when the subject sits
        # dark under a sky that is already at the top of the range:
        #
        #   * lift the low and mid tones, so an on-average-dark frame is not
        #     stranded far from white
        #   * bend the top into a shoulder, so values approaching white bunch up
        #     smoothly instead of slamming into a wall and going flat
        #
        # The result is the bathtub/S shape: open shadows, straight middle,
        # rounded highlights, and nothing hard-clipped at the very top.
        knee = min(0.95, max(0.20, float(kw_knee)))
        shoulder = max(0.05, float(kw_shoulder))
        amount = float(lift)
        denom = 1.0 - math.exp(-shoulder)
        out = []
        for x, y in zip(xs, ys):
            wl = max(0.0, 1.0 - y / 0.6)          # fades out by the mid-tones
            v = y + amount * wl * (1.0 - y)
            if v > knee:
                t = (v - knee) / max(1e-6, 1.0 - knee)
                v = knee + (1.0 - knee) * (1.0 - math.exp(-shoulder * t)) / denom
            out.append(min(1.0, max(0.0, v)))
        out[0] = 0.0
        out[-1] = 1.0
        return list(xs), out

    if kind == "lift":
        # Positive lift raises shadows (useful for birds against bright sky),
        # negative crushes them. Weighted to fade out by the mid-tones.
        amount = float(lift)
        out = []
        for x, y in zip(xs, ys):
            w = max(0.0, 1.0 - x / 0.5)
            out.append(min(1.0, max(0.0, y + amount * w * (1.0 - y))))
        # Anchor black at zero. Without this a positive lift raises the whole
        # curve off the floor, so pure black is written into the ISP gamma as
        # dark grey and every frame comes out with milky, unrecoverable blacks.
        out[0] = 0.0
        out[-1] = 1.0
        return list(xs), out

    return list(xs), list(ys)


def curve_from_cfg(cfg) -> Tuple[List[float], List[float]]:
    return build_curve(
        cfg.get("tone_curve", "stock"),
        gamma=float(cfg.get("tone_gamma", 2.2)),
        contrast=float(cfg.get("tone_contrast", 1.0)),
        lift=float(cfg.get("tone_lift", 0.0)),
        knee=float(cfg.get("tone_knee", 0.65)),
        shoulder=float(cfg.get("tone_shoulder", 2.0)),
        black=float(cfg.get("tone_black", 0.0)),
        white=float(cfg.get("tone_white", 1.0)),
        knee_soft=float(cfg.get("tone_knee_soft", 0.12)),
    )


def is_stock(cfg) -> bool:
    return (cfg.get("tone_curve", "stock") or "stock") == "stock"


def build_tuning(cfg) -> Optional[Dict[str, Any]]:
    """A tuning dict for ``Picamera2(tuning=...)`` with our curve substituted.

    Returns None when the stock curve is selected, so the camera opens exactly
    as libcamera intends and we add no behaviour of our own.
    """
    if is_stock(cfg):
        return None
    path = tuning_path()
    if not path:
        return None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None

    xs, ys = curve_from_cfg(cfg)
    flat: List[int] = []
    for x, y in zip(xs, ys):
        flat.append(int(round(min(1.0, max(0.0, x)) * FULL)))
        flat.append(int(round(min(1.0, max(0.0, y)) * FULL)))

    data = copy.deepcopy(data)
    algos = data["algorithms"] if isinstance(data.get("algorithms"), list) else [data]
    patched = False
    for algo in algos:
        for name, body in algo.items():
            if "contrast" in name and isinstance(body, dict) and "gamma_curve" in body:
                body["gamma_curve"] = flat
                patched = True
    return data if patched else None


def describe(cfg) -> str:
    """Human-readable summary, sampled at photographic mid-tones."""
    xs, ys = curve_from_cfg(cfg)
    sx, sy = stock_curve()
    kind = cfg.get("tone_curve", "stock")
    lines = ["tone curve: %s" % kind]
    if kind == "gamma":
        lines[0] += " (1/%.2f)" % float(cfg.get("tone_gamma", 2.2))
    elif kind == "contrast":
        lines[0] += " (x%.2f)" % float(cfg.get("tone_contrast", 1.0))
    elif kind == "lift":
        lines[0] += " (%+.2f)" % float(cfg.get("tone_lift", 0.0))
    elif kind == "levels":
        lines[0] += " (black %.2f, white %.2f, knee %.2f)" % (
            float(cfg.get("tone_black", 0.0)), float(cfg.get("tone_white", 1.0)),
            float(cfg.get("tone_knee_soft", 0.12)))
    elif kind == "rolloff":
        lines[0] += " (knee %.2f, shoulder %.1f, lift %+.2f)" % (
            float(cfg.get("tone_knee", 0.65)), float(cfg.get("tone_shoulder", 2.0)),
            float(cfg.get("tone_lift", 0.0)))
    lines.append("   in     this curve   stock")
    for v in (0.05, 0.1, 0.2, 0.4, 0.6, 0.8):
        lines.append("   %.2f   %.3f        %.3f" % (v, _interp(v, xs, ys), _interp(v, sx, sy)))
    if is_stock(cfg):
        lines.append("   (unchanged -- the ISP's own curve, applied in hardware)")
    else:
        lines.append("   applied by the ISP, so it costs no CPU at any resolution")
    return "\n".join(lines)


def lut8(cfg) -> "Any":
    """256-entry uint8 LUT for the selected curve.

    Provided for offline use on already-captured JPEGs. It is deliberately NOT
    in the capture path: measured 449 ms per frame at 4056x3040 on this CM4, and
    the frames coming off the ISP have already been through a curve, so applying
    one here would double it.
    """
    import numpy as np

    xs, ys = curve_from_cfg(cfg)
    x = np.linspace(0.0, 1.0, 256)
    y = np.interp(x, xs, ys)
    return np.clip(y * 255.0, 0, 255).astype(np.uint8)
