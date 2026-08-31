<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Parity: the native line against the prototype

The 1.x Python line is the specification the rewrite is held to
([ARCHITECTURE.md](ARCHITECTURE.md), rule 3). This page is the honest
ledger: what is at parity, what 2.0 adds, what still lives only in 1.x,
and where the port deliberately differs. Physics and tuned numbers are in
[PHYSICS.md](PHYSICS.md).

## At parity (same keys, same numbers, same behaviour)

| Subsystem | Notes |
|---|---|
| Auto-exposure | the full EV-space PID: gains, deadband, fast-acquire, highlight budgets, ladder, damping, averaging, lux feed-forward, constrained-equilibrium settling — every constant carried, including the virtual AE clock |
| Metering + quality gates | same zones, thresholds, verdict words, `index.jsonl` records |
| Bird Flight | same gate order, same `bf_*` keys, same reason strings, same burst/cooldown machine |
| Focus measures | Laplacian variance, normalised sharpness, tenengrad, focus map, referenced blur gate |
| Storage contract | `sess-/rapid-/tlc-` sessions, shutter buckets, `O_EXCL` claims, `.part` renames, `index.jsonl`, resume state, reject actions |
| Capture modes | COLLECT / RAPID / TIMELAPSE / BIRD FLIGHT through one engine |
| Config | same `settings.json` path, keys, defaults and deep-merge — a native install picks up a 1.x machine's tuning |
| EXIF | in-tree APP1 writer replaces piexif: same tags, centisecond SubSec, ISO from gain, metrics in UserComment; validated against exiftool |
| Assembly | `birdshot assemble` = 1.x's ffmpeg invocation at arm's length, same frame selection, same `encode_*` keys |
| Profiles, doctor | same machine-key exclusions, same checklist shape |
| GUI | four faces, the Bench 3-tab rail, accordion gating-with-reason, settings search, provenance + reset, mode dial, outdoor mode, focus monitor, Library darkroom, encode panel, blocking storage overlay |

## New in 2.0 (no 1.x ancestor)

- **The Horizons layer**: NOAA solar ephemeris, WGS84 geodesy,
  `birdshot sun` / `plan` (per-evening sunset, azimuth drift, contact
  window, FOV check) and `align` (frames paired across days by solar
  elevation). 1.x had this only as a design review.
- **The sun-lit synthetic scene**: with a site set, the reference backend
  lights its sky from the real solar elevation at your coordinates.
- **In-tree JPEG codec** (encoder *and* decoder) and the replay-from-JPEG
  camera; zero dependencies end to end.
- **The loopback HTTP viewfinder** (`birdshot gui`) for headless use.
- **Speed**: ~475 fps RAPID / ~190 fps COLLECT on a laptop against 21 fps
  on the CM4.

## Still 1.x-only (deferred, gated in the GUI with the reason)

| Feature | Where it stands |
|---|---|
| Storage cascade (tmpfs → eMMC → USB tiers, groups, ring mode) | migrates with the Pi backend it serves; section greyed |
| H.264 video recording | needs the Pi's hardware encoder; mode + section greyed |
| USB offload / rsync sync, autowrite.yes unattended config | Pi-deploy machinery; `--start` covers unattended launch |
| picamera2 / V4L2 / Media Foundation / libcamera backends | AVFoundation (macOS) is in; the IMX477 on the CM4 is what 2.0.0 waits for |
| Tone curve + live ISP controls | the curve is applied by the Pi ISP; meaningless until that backend lands (the histogram's black/white levels — a display stretch — are in) |
| Calibration wizard | keys and target-bias math are in the core; the wizard UI rides with the first exposure-owning backend |
| Sensor/webcam resolution selection, hflip/vflip | per-backend capabilities, arrive with their backends |
| Single-frame capture capability | the Camera-face shutter takes a burst of one, exactly as 1.x did off the Pi |
| Wallpaper follower, `latest.jpg` | Pi-desk conveniences, unported |

## Deliberate differences

- **Bug fixes, not bug parity**: 1.x's unregistered-widget reset holes and
  doubly-bound combos were fixed in the port, not reproduced. The one
  control 1.x should have deleted — `meter_ema`, a slider nothing reads —
  was dropped; the live smoothing keys it was superseded by
  (`ae_average_n`, `ae_average_mode`, `ae_damping`,
  `pid_integral_clamp_ev`, `prefer_exposure_time`, `sky_clip_tolerance`,
  `subject_weight`, `bf_motion_min`) got the Bench controls they never had
  in either line.
- **Every overlay toggle lives in one place** (Scene → Focus aids and
  overlays) instead of being split between Shoot and Scene; the
  toggle-all wheel gesture syncs all of them.
- **Typing site coordinates arms `site_set`** instead of leaving the sun
  readout disagreeing with visibly-correct numbers.
- `outdoor_style` is persisted (1.x forgot the style, kept the mode).
- PyQt5/GPL → Qt 6 C++/LGPL-dynamic: the licensing caveat is gone; the
  application is MIT throughout.

---

## Appendix: the config key ledger

1.x carries 105 top-level `settings.json` keys; the native line carries
the ~70 that its ported subsystems read, at identical defaults (the full
native reference, with meanings and controls, is
[PHYSICS.md](PHYSICS.md#appendix-the-settings-key-reference)). Because
both lines deep-merge over defaults and neither deletes unknown keys, a
1.x config file loads into the native line untouched: the Pi-bound keys
simply wait for the subsystems that read them.

**Carried at parity** — all backend/site/storage keys, the complete
exposure set (`auto_exposure`, `manual_*`, the ladder, the six PID terms,
`ae_*`, `prefer_exposure_time`), all metering targets and both clip
budgets, the quality gates and `sharpness_reference`, all thirteen
`bf_*` keys, the mode limits, all `exif_*` and `encode_*` keys, the
interface keys (`ui_face`, `shoot_mode`, `outdoor_*`,
`tone_black`/`tone_white` — recorded, display-only until the tone port),
and the machine state (`calibration`, `state` incl. `k_lux`).

**Native-only** — `lens_focal_mm` and `sensor_width_mm` (the Horizons
planner's FOV check), `capture_width`/`capture_height` (the synthetic
scene's plane), `outdoor_style` (1.x never persisted it),
`replay_path` promoted into defaults.

**Still 1.x-only**, grouped by the subsystem that reads them — each
arrives with its port, none is read by native code today:

| Subsystem | Keys |
|---|---|
| Sensor geometry (picamera2) | `capture_mode`, `video_mode`, `webcam_mode`, `hflip`, `vflip`, `encode_threads` |
| Tone curve / live ISP | `tone_curve`, `tone_gamma`, `tone_contrast`, `tone_lift`, `tone_knee`, `tone_knee_soft`, `tone_shoulder`, `levels_live`, `isp_contrast`, `isp_brightness`, `isp_saturation`, `isp_sharpness` |
| USB offload | `usb_root`, `offload_to_usb`, `offload_continuous`, `offload_interval_s`, `offload_delete_source` |
| Storage cascade | `cascade_enabled`, `cascade_use_ram`, `cascade_tiers`, `cascade_ram_pct`, `group_frames`, `group_mb`, `cascade_ring` |
| Rapid strategy | `rapid_mode` (`ram` vs `continuous` — the native engine is fast enough that only one path exists so far) |

**Dropped on purpose** — `meter_ema`, dead in both lines (1.x's own
defaults table marks it "kept for compatibility; the window below
supersedes it"); an old config carrying it is ignored, not an error.
`timelapse_fps` stays in both defaults tables, equally dead, so 1.x
configs round-trip byte-identically.
