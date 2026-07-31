# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Shutter-duration folder naming, compatible with the existing runCam.sh convention.

The legacy scripts used one directory per shutter speed:

    ~/s01    --shutter   100000 us  = 0.1 s
    ~/s04    --shutter   400000 us  = 0.4 s
    ~/s16    --shutter  1600000 us  = 1.6 s
    ~/s191   --shutter 19100000 us  = 19.1 s
    ~/ms500  --shutter    50000 us  = 50 ms
    ~/ms800  --shutter    80000 us  = 80 ms
    ~/ms1600 --shutter   160000 us  = 160 ms

So the numeric suffix is in units of 100000 us for the ``s`` prefix (tenths of a
second) and 100 us for the ``ms`` prefix (tenths of a millisecond).  Bird work
runs far shorter than anything in the legacy set, so we extend the scheme
downwards with a ``us`` prefix that is a plain microsecond count.

    >>> shutter_dir(19100000)
    's191'
    >>> shutter_dir(100000)
    's01'
    >>> shutter_dir(50000)
    'ms500'
    >>> shutter_dir(2000)
    'ms20'
    >>> shutter_dir(500)
    'us500'

The two ranges overlap, and the old scripts resolved that by using ``s`` only
when the duration was a whole number of tenths of a second: 1600000 us is
``s16``, but 160000 us is ``ms1600`` rather than ``s1.6``. That rule is
reproduced exactly here, and ``s`` names keep the two-digit zero padding the old
scripts used (``s01``, ``s04``), so old and new folders sort together.

Auto-exposure produces arbitrary values rather than the handful of fixed ones
the old scripts used, so a directory name is a *bucket*: durations are rounded
onto the naming grid (100 us steps in the ``ms`` range, 10 us in the ``us``
range) to keep the number of folders sane. The exact microsecond value of every
frame is recorded in the session's index.jsonl regardless.
"""

from __future__ import annotations

import re
import time

# Frame filenames: YYYYMMDDHHMMSScc -- 16 digits, centisecond resolution.
#
# The last four digits are centiseconds within the minute (0000-5999), which is
# the same string as writing seconds then centiseconds separately, because
# seconds * 100 + centiseconds is exactly that count:
#
#     2026-07-31 13:35:32.47  ->  202607311335 | 3247
#                             ->  20260731133532 | 47      (identical)
#
# One-second naming needed collision suffixes constantly -- rapid capture runs at
# up to 35 fps. At 100 slots per second, frames land about three apart, so the
# suffix is now a safety net rather than the normal case.
TIMESTAMP_DIGITS = 16


def timestamp_name(when: float | None = None) -> str:
    """``YYYYMMDDHHMMSScc`` for a POSIX timestamp, in local time."""
    when = time.time() if when is None else when
    lt = time.localtime(when)
    frac = when - int(when)
    centis = lt.tm_sec * 100 + int(frac * 100)
    return time.strftime("%Y%m%d%H%M", lt) + "%04d" % centis


def parse_timestamp_name(name: str) -> float | None:
    """Inverse of :func:`timestamp_name`, ignoring any suffix or extension."""
    digits = re.match(r"^(\d{%d})" % TIMESTAMP_DIGITS, name)
    if not digits:
        return None
    s = digits.group(1)
    try:
        centis = int(s[12:16])
        tm = time.strptime(s[:12], "%Y%m%d%H%M")
        base = time.mktime(tm[:5] + (0,) + tm[6:])
    except ValueError:
        return None
    return base + centis / 100.0

_DECI_US = 100_000  # one tenth of a second, the "s" unit
_MS_UNIT_US = 100  # the "ms" unit, one tenth of a millisecond
_US_BUCKET = 10  # rounding grid below 1 ms

_DIR_RE = re.compile(r"^(s|ms|us)(\d+)$")


def shutter_dir(exposure_us: int) -> str:
    """Return the bucket directory name for a shutter duration in microseconds."""
    exposure_us = int(round(exposure_us))
    if exposure_us >= _DECI_US and exposure_us % _DECI_US == 0:
        return "s%02d" % (exposure_us // _DECI_US)
    if exposure_us >= 1_000:
        return "ms%d" % round(exposure_us / _MS_UNIT_US)
    return "us%d" % (int(round(exposure_us / _US_BUCKET)) * _US_BUCKET)


def parse_shutter_dir(name: str) -> int | None:
    """Inverse of :func:`shutter_dir`. Returns microseconds, or None if unparseable.

    ``sauto`` and any other non-conforming name yields None.
    """
    m = _DIR_RE.match(name.strip())
    if not m:
        return None
    prefix, digits = m.group(1), int(m.group(2))
    if prefix == "s":
        return digits * 100_000
    if prefix == "ms":
        return digits * 100
    return digits


def describe_shutter(exposure_us: int) -> str:
    """Human-readable shutter, e.g. '1/500 s (2.0 ms)'."""
    exposure_us = int(round(exposure_us))
    seconds = exposure_us / 1e6
    if seconds >= 1.0:
        return "%.1f s" % seconds
    denom = round(1.0 / seconds) if seconds > 0 else 0
    if exposure_us >= 1000:
        return "1/%d s (%.1f ms)" % (denom, exposure_us / 1000.0)
    return "1/%d s (%d us)" % (denom, exposure_us)


# The shutter ladder the GUI offers as one-click presets. Values in microseconds.
# The fast end is what bird-in-flight work actually needs; the slow end preserves
# the legacy night-sky durations that produced the existing s191 captures.
PRESET_SHUTTERS_US = [
    125,  # ~1/8000 - clipping-bright sky
    250,
    500,  # 1/2000 - fast wingbeat freeze
    1_000,  # 1/1000
    2_000,  # 1/500  - default motion limit for birds
    4_000,  # 1/250
    8_000,  # 1/125
    16_000,  # 1/60
    33_000,  # 1/30
    100_000,  # 0.1 s   (s01)
    400_000,  # 0.4 s   (s04)
    1_600_000,  # 1.6 s   (s16)
    6_400_000,  # 6.4 s   (s64)
    19_100_000,  # 19.1 s  (s191, the legacy night setting)
]
