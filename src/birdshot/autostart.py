# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Unattended start: detect an ``autowrite.yes`` USB stick and configure from it.

Drop a file called ``autowrite.yes`` in the root of any USB stick and birdshot will,
on launch, capture to that stick automatically with no interaction at all. Pull
the stick out and birdshot goes back to behaving normally.

The file may be empty, in which case sensible defaults apply. It may also carry
``key=value`` lines to override them:

    mode=continuous       continuous | ram
    res=1                 0 = 4056x3040, 1 = 2028x1520, 2 = 1332x990
    count=0               frame limit, 0 = until stopped
    start=yes             begin capturing immediately on launch
    interval=30           seconds between incremental copies to the stick
    delete_after_copy=no  free the eMMC once a copy is verified
    quality=92            JPEG quality

Lines starting with ``#`` are ignored. Unknown keys are reported and skipped
rather than silently dropped, because a typo in an unattended config is
otherwise invisible until you check the card and find it empty.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

MARKER = "autowrite.yes"

# Where removable volumes show up on this Debian/LXDE image.
SEARCH_GLOBS = ["/media", "/mnt", "/run/media"]

_BOOL_TRUE = {"yes", "true", "1", "on", "y"}
_BOOL_FALSE = {"no", "false", "0", "off", "n"}

_KNOWN = {
    "mode": str,
    "res": int,
    "count": int,
    "start": bool,
    "interval": int,
    "delete_after_copy": bool,
    "quality": int,
    "folder": str,
}


def _candidate_mounts() -> List[str]:
    """Mounted filesystems that could be removable media, deepest first."""
    mounts: List[str] = []
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                target = parts[1].replace("\\040", " ")
                for base in SEARCH_GLOBS:
                    if target == base or target.startswith(base + "/"):
                        mounts.append(target)
                        break
    except OSError:
        pass
    # Deepest paths first so /media/pi/STICK beats /media.
    return sorted(set(mounts), key=lambda p: (-p.count("/"), p))


def parse_marker(path: str) -> Tuple[Dict[str, Any], List[str]]:
    """Parse an autowrite.yes file. Returns (options, warnings)."""
    opts: Dict[str, Any] = {}
    warnings: List[str] = []
    try:
        with open(path, "r") as fh:
            raw = fh.read()
    except OSError as exc:
        return opts, ["could not read %s: %s" % (path, exc)]

    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warnings.append("line %d: expected key=value, got %r" % (lineno, line))
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        kind = _KNOWN.get(key)
        if kind is None:
            warnings.append("line %d: unknown key %r" % (lineno, key))
            continue
        try:
            if kind is bool:
                low = value.lower()
                if low in _BOOL_TRUE:
                    opts[key] = True
                elif low in _BOOL_FALSE:
                    opts[key] = False
                else:
                    warnings.append("line %d: %r is not yes/no" % (lineno, value))
            elif kind is int:
                opts[key] = int(value)
            else:
                opts[key] = value
        except ValueError:
            warnings.append("line %d: %r is not a valid %s" % (lineno, value, kind.__name__))
    return opts, warnings


def detect() -> Optional[Dict[str, Any]]:
    """Find the first mounted volume carrying the marker file.

    Returns ``{"mount", "marker", "options", "warnings"}`` or None.
    """
    for mount in _candidate_mounts():
        marker = os.path.join(mount, MARKER)
        if not os.path.isfile(marker):
            continue
        if not os.access(mount, os.W_OK):
            continue
        opts, warnings = parse_marker(marker)
        return {"mount": mount, "marker": marker, "options": opts,
                "warnings": warnings}
    return None


def apply(cfg, found: Dict[str, Any]) -> Dict[str, Any]:
    """Point the config at the detected stick. Returns a summary for display."""
    mount = found["mount"]
    opts = found["options"]

    folder = opts.get("folder", "birdshot")
    cfg["usb_root"] = os.path.join(mount, folder)
    cfg["offload_to_usb"] = True
    # Unattended means nobody is watching to press "offload", so copies happen
    # on a timer during the run rather than only when the session closes.
    cfg["offload_continuous"] = True
    cfg["offload_interval_s"] = int(opts.get("interval", 30))
    cfg["offload_delete_source"] = bool(opts.get("delete_after_copy", False))

    if "res" in opts:
        cfg["capture_mode"] = max(0, min(2, int(opts["res"])))
    if "mode" in opts and opts["mode"] in ("continuous", "ram"):
        cfg["rapid_mode"] = opts["mode"]
    if "count" in opts:
        cfg["rapid_count"] = max(0, int(opts["count"]))
    if "quality" in opts:
        cfg["jpeg_quality"] = max(50, min(100, int(opts["quality"])))
    cfg.save()

    return {
        "mount": mount,
        "dest": cfg["usb_root"],
        "start": bool(opts.get("start", True)),
        "mode": cfg["rapid_mode"],
        "res": cfg["capture_mode"],
        "count": cfg["rapid_count"],
        "interval": cfg["offload_interval_s"],
        "delete_after_copy": cfg["offload_delete_source"],
        "warnings": found.get("warnings") or [],
    }


def describe(summary: Dict[str, Any]) -> str:
    from .config import CAPTURE_MODES

    res = CAPTURE_MODES[max(0, min(summary["res"], len(CAPTURE_MODES) - 1))]
    lines = [
        "autowrite.yes found on %s" % summary["mount"],
        "  copying to      %s" % summary["dest"],
        "  capture         %dx%d, %s mode" % (res[0], res[1], summary["mode"]),
        "  frame limit     %s" % (summary["count"] or "none"),
        "  copy every      %ds" % summary["interval"],
        "  free eMMC after %s" % ("yes" if summary["delete_after_copy"] else "no"),
        "  auto-start      %s" % ("yes" if summary["start"] else "no"),
    ]
    for w in summary.get("warnings", []):
        lines.append("  WARNING: %s" % w)
    return "\n".join(lines)
