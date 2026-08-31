# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""birdshot -- IMX477 bird and sky capture for the Raspberry Pi CM4.

Modules:
    config      persisted settings, calibration set-points, resume state
    naming      s<N>/ms<N>/us<N> shutter-duration folder naming
    analysis    metering, quality gates and focus measures (numpy only)
    exposure    EV-space PID auto-exposure with lux feed-forward
    storage     session layout, frame index, USB offload
    camera      the capture engine
    timelapse   ffmpeg movie assembly
    gui         PyQt5 front end
"""

# main is the 1.1 line ("The Chickens Are Restless"); 1.0.0 final is cut from
# the v1.0.0-rc1 tag once the on-hardware selftest passes. PEP 440 spelling.
__version__ = "1.1.0.dev0"
