# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Frame analysis: metering, exposure quality gates and focus aids.

Pure numpy -- OpenCV is not installed on the Pi and is not worth the build.
Everything here runs on the 640x480 luma plane that libcamera hands us for free
alongside the full-resolution capture, so metering costs no extra decode.

The gates answer the three questions asked of every frame:

* is it **dark**   -- underexposed to the point of being unusable
* is it **blown**  -- so much of it is clipped at 255 that detail is gone
* is it **empty**  -- blurred, fogged, lens-capped or otherwise featureless,
  i.e. there is no region anywhere in frame carrying real contrast

Sky-versus-treeline work is inherently high dynamic range, so "blown" is
deliberately not a simple clipped-pixel count: a correctly exposed bird against
a bright sky *will* clip some sky. The gate only fires when clipping is
widespread enough to have eaten the subject zone too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional, Tuple

import numpy as np

# Grid used for "is there anything in this frame" tests.
TILE_COLS = 8
TILE_ROWS = 6

_EPS = 1e-6


@dataclass
class FrameStats:
    """Everything we measure about one frame."""

    # Luminance distribution over the whole frame.
    mean: float = 0.0
    std: float = 0.0
    p1: float = 0.0
    p5: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    clip_hi: float = 0.0  # fraction >= 250
    clip_lo: float = 0.0  # fraction <= 5

    # Zone metering.
    sky_p50: float = 0.0
    sky_clip_hi: float = 0.0
    subject_p50: float = 0.0
    subject_p95: float = 0.0
    meter: float = 0.0  # the single number the PID controls

    # Content and focus.
    sharpness: float = 0.0  # raw laplacian variance
    sharpness_norm: float = 0.0  # contrast-normalised, this is what we gate on
    tenengrad: float = 0.0
    focus_measured: bool = False  # False until a focus pass has actually run
    best_tile_std: float = 0.0
    contrast_tiles: int = 0  # tiles carrying real detail
    dynamic_range: float = 0.0  # p99 - p1

    # Verdicts.
    is_dark: bool = False
    is_blown: bool = False
    is_empty: bool = False
    has_subject: bool = False
    verdict: str = "ok"  # ok | dark | blown | empty

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d


def _percentiles(y: np.ndarray) -> Tuple[float, float, float, float, float]:
    """p1, p5, p50, p95, p99 from a 256-bin histogram -- much faster than
    np.percentile on a 300k-pixel array, and exact enough for 8-bit data."""
    hist = np.bincount(y.ravel(), minlength=256).astype(np.float64)
    cdf = np.cumsum(hist)
    total = cdf[-1]
    if total <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    cdf /= total
    return tuple(float(np.searchsorted(cdf, q)) for q in (0.01, 0.05, 0.50, 0.95, 0.99))


def normalised_sharpness(src: np.ndarray, lap_var: float) -> float:
    """Focus measure that does not depend on how contrasty the scene is.

    Normalise by the *focus region's own* contrast, not the whole frame's. The
    two differ enormously here: metering deliberately spans bright sky to shadowed
    treeline, so the frame's dynamic range is near the full 0-255 while the centre
    crop being measured may be a flat patch of wall. Dividing one by the other
    made every frame look soft.

    Roughly: values near 100 mean edges as strong as the local contrast allows
    (sharp), while single digits mean detail has been smeared away.
    """
    spread = float(src.std())
    return 100.0 * math.sqrt(max(lap_var, 0.0)) / max(spread, 1.0)


def laplacian_variance(y: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian. The classic focus measure."""
    if y.shape[0] < 3 or y.shape[1] < 3:
        return 0.0
    f = y.astype(np.int32)
    lap = (
        4 * f[1:-1, 1:-1]
        - f[:-2, 1:-1]
        - f[2:, 1:-1]
        - f[1:-1, :-2]
        - f[1:-1, 2:]
    )
    return float(lap.var())


def tenengrad(y: np.ndarray, step: int = 2) -> float:
    """Mean Sobel gradient magnitude squared. Second opinion on focus -- less
    sensitive to noise than the Laplacian, which matters at high gain.

    Subsampled by ``step`` and kept in float32: at full density and float64 this
    cost 44 ms per frame on the CM4, which is more than the whole rest of the
    metering path put together, for a number no gate actually reads.
    """
    if y.shape[0] < 3 * step or y.shape[1] < 3 * step:
        return 0.0
    f = y[::step, ::step].astype(np.int32)
    gx = (
        f[:-2, 2:] + 2 * f[1:-1, 2:] + f[2:, 2:]
        - f[:-2, :-2] - 2 * f[1:-1, :-2] - f[2:, :-2]
    )
    gy = (
        f[2:, :-2] + 2 * f[2:, 1:-1] + f[2:, 2:]
        - f[:-2, :-2] - 2 * f[:-2, 1:-1] - f[:-2, 2:]
    )
    gx = gx.astype(np.float32)
    gy = gy.astype(np.float32)
    return float(np.mean(gx * gx + gy * gy))


def tile_stats(y: np.ndarray, rows: int = TILE_ROWS, cols: int = TILE_COLS):
    """Per-tile mean and stddev, trimming any ragged edge."""
    h, w = y.shape
    th, tw = h // rows, w // cols
    if th < 2 or tw < 2:
        return np.zeros((rows, cols)), np.zeros((rows, cols))
    cropped = y[: th * rows, : tw * cols].astype(np.float32)
    blocks = cropped.reshape(rows, th, cols, tw).transpose(0, 2, 1, 3)
    blocks = blocks.reshape(rows, cols, th * tw)
    return blocks.mean(axis=2), blocks.std(axis=2)


_BIN_IDX = np.arange(256, dtype=np.float64)


def _stats_from_hist(hist: np.ndarray):
    """Mean, std, percentiles and clip fractions from a 256-bin histogram.

    One bincount pass replaces the half-dozen separate passes over the pixel
    array that mean/std/percentile/count_nonzero would each make. Exact for
    8-bit data, and about 4x faster.
    """
    total = float(hist.sum())
    if total <= 0:
        return 0.0, 0.0, (0.0,) * 5, 0.0, 0.0
    mean = float((hist * _BIN_IDX).sum() / total)
    var = float((hist * (_BIN_IDX - mean) ** 2).sum() / total)
    cdf = np.cumsum(hist) / total
    pcts = tuple(float(np.searchsorted(cdf, q)) for q in (0.01, 0.05, 0.50, 0.95, 0.99))
    clip_hi = float(hist[250:].sum() / total)
    clip_lo = float(hist[:6].sum() / total)
    return mean, math.sqrt(var), pcts, clip_hi, clip_lo


def meter_only(y: np.ndarray, cfg: Dict[str, Any]) -> FrameStats:
    """Metering fast path for rapid capture: two histogram passes, nothing else.

    Rapid mode exists to keep up with the sensor, and full :func:`analyse` costs
    ~16 ms a frame where this costs ~4. Quality gates are skipped entirely --
    rapid frames are scored later, if at all.
    """
    st = FrameStats()
    if y is None or y.size == 0:
        return st

    sky_frac = float(cfg.get("sky_zone_frac", 0.40))
    split = max(1, min(y.shape[0] - 1, int(y.shape[0] * sky_frac)))

    h_sky = np.bincount(y[:split].ravel(), minlength=256).astype(np.float64)
    h_sub = np.bincount(y[split:].ravel(), minlength=256).astype(np.float64)
    h_all = h_sky + h_sub

    st.mean, st.std, pcts, st.clip_hi, st.clip_lo = _stats_from_hist(h_all)
    st.p1, st.p5, st.p50, st.p95, st.p99 = pcts
    st.dynamic_range = st.p99 - st.p1

    _, _, sky_p, st.sky_clip_hi, _ = _stats_from_hist(h_sky)
    st.sky_p50 = sky_p[2]
    _, _, sub_p, sub_clip, _ = _stats_from_hist(h_sub)
    st.subject_p50, st.subject_p95 = sub_p[2], sub_p[3]

    sw = float(cfg.get("sky_weight", 0.15))
    uw = float(cfg.get("subject_weight", 1.0))
    st.meter = (uw * st.subject_p50 + sw * st.sky_p50) / max(uw + sw, _EPS)

    # Exposure gates still apply; content and focus ones do not.
    st.is_dark = st.p95 < float(cfg.get("dark_p95_max", 40.0))
    st.is_blown = sub_clip > float(cfg.get("blown_clip_frac", 0.35))
    st.best_tile_std = st.std
    st.contrast_tiles = 1
    st.verdict = "blown" if st.is_blown else ("dark" if st.is_dark else "ok")
    return st


def focus_map(y: np.ndarray, rows: int = 9, cols: int = 12):
    """Per-tile focus energy, normalised to 0..1, plus the sharpest tile.

    Answers "which part of the frame is actually in focus", which binary peaking
    cannot: with a manual lens pointed at a treeline, peaking lights up both the
    sharp branch and the noisy sky, whereas this ranks them.

    Returns ``(map, (best_row, best_col), best_raw)``.
    """
    if y.shape[0] < rows * 3 or y.shape[1] < cols * 3:
        return np.zeros((rows, cols), np.float32), (0, 0), 0.0

    f = y.astype(np.int32)
    lap = (
        4 * f[1:-1, 1:-1]
        - f[:-2, 1:-1]
        - f[2:, 1:-1]
        - f[1:-1, :-2]
        - f[1:-1, 2:]
    ).astype(np.float32)
    lap *= lap  # gradient energy

    h, w = lap.shape
    th, tw = h // rows, w // cols
    blocks = lap[: th * rows, : tw * cols].reshape(rows, th, cols, tw)
    energy = blocks.mean(axis=(1, 3))

    best = np.unravel_index(int(np.argmax(energy)), energy.shape)
    peak = float(energy[best])
    if peak > 0:
        # Square root compresses the range so mid-focus regions stay visible
        # next to one very sharp edge.
        norm = np.sqrt(energy / peak)
    else:
        norm = np.zeros_like(energy)
    return norm.astype(np.float32), (int(best[0]), int(best[1])), peak


def histogram(y: np.ndarray, bins: int = 128) -> np.ndarray:
    """Normalised luminance histogram for the GUI."""
    hist = np.bincount((y.ravel() >> (8 - int(math.log2(bins)))), minlength=bins)
    total = hist.sum()
    return (hist / total) if total else hist.astype(np.float64)


def focus_peaking_mask(y: np.ndarray, threshold: float = 28.0) -> np.ndarray:
    """Boolean mask of high-gradient pixels, for the manual-focus overlay.

    The camera is a manual-focus C-mount, so this is the only real focusing aid
    available -- peaking is far more reliable than eyeballing a 640px preview.
    """
    f = y.astype(np.int16)
    gx = np.zeros_like(f)
    gy = np.zeros_like(f)
    gx[:, 1:-1] = f[:, 2:] - f[:, :-2]
    gy[1:-1, :] = f[2:, :] - f[:-2, :]
    mag = np.abs(gx) + np.abs(gy)
    return mag > threshold


def analyse(
    y: np.ndarray,
    cfg: Dict[str, Any],
    hires_crop: Optional[np.ndarray] = None,
    focus: bool = True,
    tiles: bool = True,
) -> FrameStats:
    """Measure one frame.

    ``y``           -- 8-bit luma plane, typically the 640x480 lores stream.
    ``hires_crop``  -- optional native-resolution centre crop. Focus is judged
                       on this when supplied, because a downscaled preview hides
                       exactly the softness we are trying to detect.
    ``focus``       -- set False to skip the focus measures entirely. The
                       capture loop does this when a frame is being saved, since
                       :func:`refine_with_hires` is about to recompute them at
                       native resolution on the encoder pool anyway.
    ``tiles``       -- set False to skip the content-detection grid too. With
                       both off this is metering only (~10 ms rather than ~21),
                       which is what rapid capture uses to keep the loop out of
                       the way of the sensor.
    """
    st = FrameStats()
    if y is None or y.size == 0:
        st.verdict = "empty"
        st.is_empty = True
        return st

    y = np.ascontiguousarray(y)
    st.mean = float(y.mean())
    st.std = float(y.std())
    st.p1, st.p5, st.p50, st.p95, st.p99 = _percentiles(y)
    st.dynamic_range = st.p99 - st.p1

    total = float(y.size)
    st.clip_hi = float(np.count_nonzero(y >= 250)) / total
    st.clip_lo = float(np.count_nonzero(y <= 5)) / total

    # ---- zone metering -------------------------------------------------
    # We are always shooting sky over treeline, so a plain average meter is
    # guaranteed to underexpose the bird. Split the frame and weight the
    # subject zone far more heavily than the sky.
    sky_frac = float(cfg.get("sky_zone_frac", 0.40))
    split = max(1, min(y.shape[0] - 1, int(y.shape[0] * sky_frac)))
    sky = y[:split]
    subject = y[split:]

    _, _, st.sky_p50, _, _ = _percentiles(sky)
    st.sky_clip_hi = float(np.count_nonzero(sky >= 250)) / max(sky.size, 1)
    _, _, st.subject_p50, st.subject_p95, _ = _percentiles(subject)

    sw = float(cfg.get("sky_weight", 0.15))
    uw = float(cfg.get("subject_weight", 1.0))
    st.meter = (uw * st.subject_p50 + sw * st.sky_p50) / max(uw + sw, _EPS)

    # ---- content and focus ---------------------------------------------
    if focus:
        focus_src = hires_crop if hires_crop is not None and hires_crop.size else y
        st.sharpness = laplacian_variance(focus_src)
        st.tenengrad = tenengrad(focus_src)
        st.sharpness_norm = normalised_sharpness(focus_src, st.sharpness)
        st.focus_measured = True

    content_min = float(cfg.get("content_std_min", 8.0))
    if tiles:
        tmean, tstd = tile_stats(y)
        st.best_tile_std = float(tstd.max()) if tstd.size else 0.0
        st.contrast_tiles = int(np.count_nonzero(tstd > content_min))
    else:
        # Metering-only: no content claim either way, so leave the gates alone.
        st.best_tile_std = float(st.std)
        st.contrast_tiles = 1

    # ---- verdicts ------------------------------------------------------
    st.is_dark = st.p95 < float(cfg.get("dark_p95_max", 40.0))
    # Blown only when clipping has spread into the subject zone -- clipped sky
    # alone is expected and acceptable for this subject matter.
    subject_clip = float(np.count_nonzero(subject >= 250)) / max(subject.size, 1)
    st.is_blown = subject_clip > float(cfg.get("blown_clip_frac", 0.35))
    finalize(st, cfg)
    return st


def finalize(st: FrameStats, cfg: Dict[str, Any]) -> FrameStats:
    """Derive the content verdict from the focus and tile measures.

    Split out because the focus measure is refined later, off the capture loop,
    once the full-resolution frame has been copied.
    """
    content_min = float(cfg.get("content_std_min", 8.0))
    # Empty means either nothing has contrast anywhere, or the whole frame is
    # soft. Both make the frame worthless for bird identification.
    # Until a focus pass has run, sharpness is 0 and would read as "soft"; the
    # verdict stays provisional rather than falsely condemning the frame.
    soft = (st.focus_measured
            and st.sharpness_norm < float(cfg.get("blur_threshold", 12.0)))
    featureless = st.contrast_tiles == 0
    st.is_empty = featureless or (soft and st.best_tile_std < content_min * 2.0)
    st.has_subject = st.contrast_tiles > 0 and not soft

    if st.is_blown:
        st.verdict = "blown"
    elif st.is_dark:
        st.verdict = "dark"
    elif st.is_empty:
        st.verdict = "empty"
    else:
        st.verdict = "ok"
    return st


def refine_with_hires(st: FrameStats, crop: np.ndarray, cfg: Dict[str, Any]) -> FrameStats:
    """Re-measure focus on a native-resolution crop and redo the verdict.

    Runs on the encoder pool rather than the capture loop: on this CM4 the
    Laplacian and Sobel passes over a 768x768 crop cost ~140 ms, which would
    otherwise halve the achievable frame rate. Auto-exposure never reads these
    fields, so deferring them costs the control loop nothing.
    """
    if crop is None or crop.size == 0:
        return st
    st.sharpness = laplacian_variance(crop)
    st.tenengrad = tenengrad(crop)
    st.sharpness_norm = normalised_sharpness(crop, st.sharpness)
    st.focus_measured = True
    return finalize(st, cfg)
