# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
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

__version__ = "1.0.0"
