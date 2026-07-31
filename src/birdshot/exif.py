# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Write EXIF metadata into captured frames, as a preprocessing step.

Deliberately **not** done at capture time. Rapid capture runs at up to 35 fps and
the whole design is about keeping the capture loop out of the sensor's way;
adding a metadata rewrite per frame would take that back. Everything needed is
already recorded in ``index.jsonl``, so tagging can happen later, in bulk, when
frames are being prepared for a movie.

``exiftool`` is not installed on this Pi, and would not be the right tool anyway:
it costs a process spawn per file (~50-100 ms), which over a few thousand frames
is minutes. ``piexif`` is available and *injects the APP1 segment directly into
the JPEG byte stream* -- no re-encode, so no quality loss and no visible cost.

The centisecond filenames map exactly onto EXIF's ``SubSecTimeOriginal``, so the
sub-second precision survives into the metadata rather than being lost.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import piexif

    HAVE_PIEXIF = True
except ImportError:  # pragma: no cover - depends on the target
    piexif = None
    HAVE_PIEXIF = False


def available() -> Tuple[bool, str]:
    if HAVE_PIEXIF:
        return True, "piexif %s" % getattr(piexif, "VERSION", "?")
    return False, "piexif not installed (pip3 install piexif)"


def _rational(value: float, denom: int = 1000) -> Tuple[int, int]:
    return int(round(value * denom)), denom


def build_exif(entry: Dict[str, Any], cfg, size: Optional[Tuple[int, int]] = None) -> bytes:
    """EXIF bytes for one frame, from its index.jsonl entry."""
    when = float(entry.get("t") or time.time())
    lt = time.localtime(when)
    stamp = time.strftime("%Y:%m:%d %H:%M:%S", lt)
    subsec = "%02d" % int((when - int(when)) * 100)  # centiseconds, as captured

    shutter_us = int(entry.get("shutter_us") or 0)
    gain = float(entry.get("gain") or 1.0)
    metrics = entry.get("metrics") or {}

    zeroth: Dict[int, Any] = {
        piexif.ImageIFD.Make: cfg.get("exif_make", "Raspberry Pi"),
        piexif.ImageIFD.Model: cfg.get("exif_model", "IMX477 HQ Camera"),
        piexif.ImageIFD.Software: cfg.get("exif_software", "birdshot"),
        piexif.ImageIFD.DateTime: stamp,
        # We flip in the ISP, so the stored pixels are already upright.
        piexif.ImageIFD.Orientation: 1,
    }
    artist = cfg.get("exif_artist")
    if artist:
        zeroth[piexif.ImageIFD.Artist] = artist
    copyright_ = cfg.get("exif_copyright")
    if copyright_:
        zeroth[piexif.ImageIFD.Copyright] = copyright_

    exif: Dict[int, Any] = {
        piexif.ExifIFD.DateTimeOriginal: stamp,
        piexif.ExifIFD.DateTimeDigitized: stamp,
        piexif.ExifIFD.SubSecTimeOriginal: subsec,
        piexif.ExifIFD.SubSecTimeDigitized: subsec,
        # Analogue gain is the only sensitivity we control, so ISO 100 = gain 1.
        piexif.ExifIFD.ISOSpeedRatings: max(1, int(round(gain * 100))),
        piexif.ExifIFD.ExposureMode: 1,      # manual -- our own AE drives it
        piexif.ExifIFD.WhiteBalance: 0,      # auto (AWB stays on)
        piexif.ExifIFD.LightSource: 0,
        piexif.ExifIFD.SceneCaptureType: 0,
    }
    if shutter_us > 0:
        exif[piexif.ExifIFD.ExposureTime] = (shutter_us, 1_000_000)
        # APEX shutter speed, for tools that prefer it.
        exif[piexif.ExifIFD.ShutterSpeedValue] = _rational(
            math.log2(1_000_000.0 / shutter_us), 100)

    # Manual C-mount glass reports nothing, so these come from settings.
    fnum = cfg.get("exif_fnumber")
    if fnum:
        exif[piexif.ExifIFD.FNumber] = _rational(float(fnum), 10)
        exif[piexif.ExifIFD.ApertureValue] = _rational(
            2 * math.log2(float(fnum)), 100)
    focal = cfg.get("exif_focal_mm")
    if focal:
        exif[piexif.ExifIFD.FocalLength] = _rational(float(focal), 10)
    lens = cfg.get("exif_lens")
    if lens:
        exif[piexif.ExifIFD.LensModel] = lens

    if size:
        exif[piexif.ExifIFD.PixelXDimension] = int(size[0])
        exif[piexif.ExifIFD.PixelYDimension] = int(size[1])

    # birdshot's own measurements, so a frame carries its scoring with it.
    note = {
        "verdict": entry.get("verdict"),
        "shutter_us": shutter_us,
        "gain": round(gain, 3),
        "shutter_dir": entry.get("shutter_dir"),
        "seq": entry.get("seq"),
    }
    for key in ("meter", "p50", "p95", "clip_hi", "sharpness_norm",
                "contrast_tiles", "dynamic_range"):
        if key in metrics:
            note[key] = metrics[key]
    exif[piexif.ExifIFD.UserComment] = b"ASCII\0\0\0" + json.dumps(
        note, separators=(",", ":")).encode("ascii", "replace")

    verdict = entry.get("verdict")
    if verdict:
        zeroth[piexif.ImageIFD.ImageDescription] = "birdshot %s" % verdict

    return piexif.dump({"0th": zeroth, "Exif": exif, "1st": {}, "thumbnail": None})


def tag_file(path: str, entry: Dict[str, Any], cfg) -> bool:
    """Inject EXIF into one JPEG. Atomic: writes a sibling, then renames."""
    if not HAVE_PIEXIF or not os.path.isfile(path):
        return False
    try:
        blob = build_exif(entry, cfg)
        tmp = path + ".exif.part"
        piexif.insert(blob, path, tmp)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 - a bad frame must not stop the batch
        try:
            os.unlink(path + ".exif.part")
        except OSError:
            pass
        return False


def tag_session(
    session_dir: str,
    cfg,
    only_ok: bool = False,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel=None,
) -> Dict[str, Any]:
    """Tag every frame a session's index describes.

    Frames with no index entry are skipped rather than guessed at -- a timestamp
    recovered from the filename would be right, but the exposure and metrics
    would not, and wrong EXIF is worse than none.
    """
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why, "tagged": 0}

    index = os.path.join(session_dir, "index.jsonl")
    if not os.path.exists(index):
        return {"ok": False, "error": "no index.jsonl in %s" % session_dir, "tagged": 0}

    entries: List[Dict[str, Any]] = []
    with open(index) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not e.get("file"):
                continue
            if only_ok and e.get("verdict", "ok") != "ok":
                continue
            entries.append(e)

    tagged = failed = missing = 0
    t0 = time.time()
    for i, e in enumerate(entries):
        if cancel is not None and cancel.is_set():
            break
        path = os.path.join(session_dir, e["file"])
        if not os.path.exists(path):
            missing += 1
            continue
        if tag_file(path, e, cfg):
            tagged += 1
        else:
            failed += 1
        if progress and (i % 25 == 0):
            progress(i, len(entries))
    if progress:
        progress(len(entries), len(entries))

    return {"ok": True, "tagged": tagged, "failed": failed, "missing": missing,
            "total": len(entries), "elapsed": round(time.time() - t0, 1)}


def read_back(path: str) -> Dict[str, Any]:
    """Summarise a tagged file, for verification."""
    if not HAVE_PIEXIF:
        return {}
    try:
        d = piexif.load(path)
    except Exception:  # noqa: BLE001
        return {}
    ex = d.get("Exif", {})
    zer = d.get("0th", {})

    def txt(v):
        return v.decode("ascii", "replace") if isinstance(v, bytes) else v

    et = ex.get(piexif.ExifIFD.ExposureTime)
    out = {
        "Make": txt(zer.get(piexif.ImageIFD.Make)),
        "Model": txt(zer.get(piexif.ImageIFD.Model)),
        "DateTimeOriginal": txt(ex.get(piexif.ExifIFD.DateTimeOriginal)),
        "SubSecTimeOriginal": txt(ex.get(piexif.ExifIFD.SubSecTimeOriginal)),
        "ExposureTime": ("%d/%d" % et) if et else None,
        "ISO": ex.get(piexif.ExifIFD.ISOSpeedRatings),
    }
    uc = ex.get(piexif.ExifIFD.UserComment)
    if uc:
        try:
            out["UserComment"] = json.loads(uc[8:].decode("ascii", "replace"))
        except ValueError:
            pass
    return out
