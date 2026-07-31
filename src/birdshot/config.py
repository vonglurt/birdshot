# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Persisted settings, calibration set-points and resume state.

Everything the user tunes in the GUI lands in a single JSON file so that a
restart picks up exactly where the last run left off, including the calibration
captured by the sky/treeline wizard.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict

CONFIG_DIR = os.path.expanduser("~/.config/birdshot")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

# Sensor limits for the IMX477 as reported by libcamera on this unit.
SENSOR_NAME = "imx477"
SENSOR_FULL_SIZE = (4056, 3040)
EXPOSURE_MIN_US = 114
EXPOSURE_MAX_US = 60_000_000
GAIN_MIN = 1.0
GAIN_MAX = 22.26

# Capture modes: (width, height, label, fps measured end-to-end on this CM4).
# The sensor itself will do 10.8 / 41.7 / 41.7 fps for these modes; the figures
# below are what survives the full-resolution copy, JPEG encode and disk write,
# which is memory-bandwidth bound rather than sensor bound.
CAPTURE_MODES = [
    (4056, 3040, "Full 4056x3040 (12.3MP native)", 3.3),
    (2028, 1520, "Binned 2028x1520 (3.1MP, same FOV)", 8.6),
    (1332, 990, "Fast 1332x990 (cropped)", 10.4),
]

# Video modes: (width, height, fps, label).
# The CM4's hardware H.264 encoder tops out at 1080p -- asking for 2028x1080
# gets silently renegotiated down to 1920x1080, so those sizes are not offered.
# Widths are kept to multiples of 32 to avoid further renegotiation.
VIDEO_MODES = [
    (1920, 1080, 50, "1920x1080 @ 50fps (encoder maximum)"),
    (1920, 1080, 30, "1920x1080 @ 30fps (smaller files)"),
    (1280, 720, 90, "1280x720 @ 90fps (fast action)"),
    (1280, 720, 50, "1280x720 @ 50fps"),
]

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    # ---- storage -------------------------------------------------------
    # Capture always lands on the eMMC (78 MB/s). The USB stick is NTFS over
    # a USB 2.0 port (~12 MB/s) and is only ever an offload target.
    "data_root": os.path.expanduser("~/birdshot-data"),
    "usb_root": "/media/pi/ARCHIVE/birdshot",
    "offload_to_usb": True,
    "offload_delete_source": False,
    # Copy the in-progress session to USB on a timer, not just when it closes.
    # Set automatically by an autowrite.yes stick, since unattended runs have
    # nobody around to trigger a copy.
    "offload_continuous": False,
    "offload_interval_s": 30,
    "min_free_mb": 2048,  # stop capture below this much free space on the data root
    # ---- geometry ------------------------------------------------------
    "hflip": True,  # matches the legacy runCam.sh --hflip
    "vflip": True,  # matches the legacy runCam.sh --vflip
    "capture_mode": 0,  # index into CAPTURE_MODES
    "video_mode": 0,  # index into VIDEO_MODES
    "jpeg_quality": 92,
    # Measured: 3 threads matches 6 on this CM4 (memory-bandwidth bound), and
    # leaves a core free for the capture loop and the GUI.
    "encode_threads": 3,
    # ---- exposure ------------------------------------------------------
    "auto_exposure": True,
    "manual_shutter_us": 2000,
    "manual_gain": 1.0,
    # Shutter-priority-short: never exceed this while the scene allows it, so
    # wingbeats stay frozen. Exceeded only when gain is already maxed.
    "motion_limit_us": 2000,  # 1/500 s
    "gain_preferred_max": 4.0,  # raise gain to here before lengthening shutter
    "shutter_hard_max_us": 33_000,  # daylight bird work never needs longer
    # PID over EV (log2) error.
    "pid_kp": 0.55,
    "pid_ki": 0.10,
    "pid_kd": 0.12,
    "pid_deadband_ev": 0.12,
    "pid_slew_ev": 1.5,  # max EV change per frame once settled
    "pid_integral_clamp_ev": 2.0,
    "meter_ema": 0.4,  # smoothing on the measured metric (0 = none)
    # ---- metering targets (overwritten by the calibration wizard) -------
    "target_luma": 118.0,  # desired subject-zone median, 0-255
    "max_clip_frac": 0.020,  # tolerated fraction of pixels at/above 250
    "sky_zone_frac": 0.40,  # top fraction of frame treated as sky
    "sky_weight": 0.15,  # how much the sky zone counts toward metering
    "subject_weight": 1.0,
    # ---- tone curve ----------------------------------------------------
    # The ISP already applies the HQ-cam gamma curve in hardware. These replace
    # that curve rather than adding a second one, so they cost no CPU. Changing
    # the curve reopens the camera (~1 s); contrast/brightness below are live
    # libcamera controls and need no restart.
    "tone_curve": "stock",   # stock | linear | gamma | contrast | lift
    "tone_gamma": 2.2,
    "tone_contrast": 1.0,
    "tone_lift": 0.0,
    "isp_contrast": 1.0,     # live control, 1.0 = tuning default
    "isp_brightness": 0.0,   # live control, -1..1
    "isp_saturation": 1.0,
    "isp_sharpness": 1.0,
    # ---- quality gates -------------------------------------------------
    "dark_p95_max": 40.0,  # p95 below this => frame is dark
    "blown_clip_frac": 0.35,  # this fraction clipped => frame is blown
    # Normalised sharpness below this counts as soft. The usable value depends
    # on the lens, the aperture and the subject, so it is meant to be set from
    # a known-good frame via the Focus tab rather than guessed. The default is
    # deliberately low so the gate never rejects anything until calibrated.
    "blur_threshold": 2.0,
    "sharpness_reference": None,  # sharpness of a frame the user called focused
    "content_std_min": 8.0,  # best-tile stddev below this => no real content
    "reject_action": "flag",  # flag | delete | quarantine
    # ---- timelapse -----------------------------------------------------
    "timelapse_interval_s": 5.0,
    "timelapse_fps": 60,
    "timelapse_count": 0,  # 0 = until stopped
    # ---- burst ---------------------------------------------------------
    "burst_count": 0,  # 0 = free-run until stopped
    # ---- rapid (flat YYYYmmddHHMMSS stills, fastest possible) ----------
    # Measured end-to-end (fps), continuous vs ram:
    #   4056x3040   4.53 / 4.17     2028x1520  21.02 / 16.34    1332x990  34.81 / 36.75
    # "continuous" wins nearly everywhere because the full-resolution buffer
    # copy happens on the worker threads in parallel, whereas a RAM burst does
    # it serially on the capture loop. "ram" is kept for the case where disk
    # I/O jitter matters more than raw rate.
    "rapid_mode": "continuous",
    "rapid_count": 0,  # 0 = fill the RAM budget / run until stopped
    # ---- storage cascade (groups migrating tmpfs -> eMMC -> USB) --------
    # Frames are written into numbered group directories; a background worker
    # copies each sealed group to the next tier, verifies it, then frees the
    # source. Each tier clears itself, so capture runs for as long as the
    # BOTTOM tier has room rather than the top one.
    "shoot_mode": 0,        # index into the Shoot tab's mode dropdown
    "outdoor_mode": False,  # high-contrast preview for bright sunlight
    "cascade_enabled": False,
    # The RAM tier is optional. Turned off, the chain degrades cleanly to
    # eMMC -> USB; the path is otherwise identical, so nothing else changes.
    "cascade_use_ram": True,
    "cascade_tiers": [
        {"path": "/dev/shm/birdshot", "label": "tmpfs (RAM)",
         "min_free_mb": 400, "flush_after_s": 5, "speed_mb_s": 459},
        {"path": os.path.expanduser("~/birdshot-data/cascade"), "label": "eMMC",
         "min_free_mb": 2048, "flush_after_s": 30, "speed_mb_s": 60},
        {"path": "/media/pi/ARCHIVE/birdshot", "label": "USB stick",
         "min_free_mb": 1024, "flush_after_s": 60, "speed_mb_s": 12},
    ],
    # How much of system RAM the top (tmpfs) tier may use. Applied by
    # remounting the tmpfs. The practical ceiling is lower than 100% because
    # CMA -- the camera's DMA pool -- is carved from the same MemTotal, and a
    # tmpfs large enough to crowd it reproduces the "cma_alloc failed" crash.
    "cascade_ram_pct": 50,
    "group_frames": 200,  # seal a group after this many frames
    "group_mb": 250,  # ... or this many megabytes, whichever first
    # Ring mode drops the OLDEST groups from the bottom tier when it fills.
    # This is the one place data is deleted without existing anywhere else, so
    # it is off unless explicitly asked for.
    "cascade_ring": False,
    # ---- EXIF ----------------------------------------------------------
    # Written as a preprocessing step before movie assembly, never at capture
    # time -- rapid capture runs at up to 35 fps and everything needed is
    # already in index.jsonl. The manual C-mount lens reports nothing, so focal
    # length and aperture come from here if you want them recorded.
    "exif_enabled": True,
    "exif_make": "Raspberry Pi",
    "exif_model": "IMX477 HQ Camera",
    "exif_software": "birdshot",
    "exif_lens": "",
    "exif_focal_mm": 0.0,   # 0 = do not record
    "exif_fnumber": 0.0,    # 0 = do not record
    "exif_artist": "",
    "exif_copyright": "",
    # ---- encode (stills -> movie) --------------------------------------
    "encode_fps": 60,
    "encode_width": 1920,  # 0 = native
    "encode_crf": 18,
    "encode_preset": "veryfast",
    "encode_only_ok": True,
    # ---- calibration (populated by the wizard) -------------------------
    "calibration": {
        "done": False,
        "timestamp": None,
        "sky": None,  # {"ev": float, "luma": float, "lux": float}
        "treeline": None,
        "subject": None,
        "dynamic_range_ev": None,
    },
    # ---- resume state --------------------------------------------------
    "state": {
        "last_session": None,
        "last_shutter_us": 2000,
        "last_gain": 1.0,
        "frame_seq": 0,
    },
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thread-safe settings object with atomic write-through to disk."""

    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    # -- dict-ish access -------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, "r") as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            return
        with self._lock:
            self._data = _deep_merge(DEFAULTS, stored)

    def save(self) -> None:
        """Atomic save: write to a temp file in the same dir, then rename."""
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=True)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- convenience -----------------------------------------------------
    def set_state(self, **kwargs: Any) -> None:
        with self._lock:
            self._data["state"].update(kwargs)

    def set_calibration(self, zone: str, ev: float, luma: float, lux: float) -> None:
        with self._lock:
            cal = self._data["calibration"]
            cal[zone] = {"ev": ev, "luma": luma, "lux": lux}
            cal["timestamp"] = time.time()
            sky, tree = cal.get("sky"), cal.get("treeline")
            if sky and tree:
                cal["dynamic_range_ev"] = abs(sky["ev"] - tree["ev"])
                cal["done"] = True
        self.save()

    def capture_size(self) -> tuple:
        idx = max(0, min(self["capture_mode"], len(CAPTURE_MODES) - 1))
        return CAPTURE_MODES[idx][0], CAPTURE_MODES[idx][1]

    def video_size(self) -> tuple:
        idx = max(0, min(self["video_mode"], len(VIDEO_MODES) - 1))
        return VIDEO_MODES[idx][0], VIDEO_MODES[idx][1]

    def video_fps(self) -> int:
        idx = max(0, min(self["video_mode"], len(VIDEO_MODES) - 1))
        return VIDEO_MODES[idx][2]
