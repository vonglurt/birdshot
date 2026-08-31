<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# The physics: every tuned number, explained

What the instrument actually computes, why each constant has the value it
has, and which control tunes it. Everything here runs on the shared 640×480
luma plane ("luma is for judgment, colour is for people" —
[ARCHITECTURE.md](ARCHITECTURE.md)); every threshold is a `settings.json`
key with the same name and default as 1.x, and every key named below has a
control in the Bench rail (findable by name in the settings search).

The problem that drives all of it: a small dark bird against bright sky
spans more luminance range than the sensor has, moves fast enough to smear
at ordinary shutters, and is photographed through a manual lens with no
focus feedback.

---

## 1. Metering: two zones, one number

The frame is split at `sky_zone_frac` (0.40) — the top fraction of rows is
the **sky zone**, the rest the **subject zone** (`analysis.cpp`,
`meter_only`). Each zone gets a 256-bin histogram; percentiles come from
the histogram CDF (exact for 8-bit, one pass, no sorting).

The single number the exposure loop controls is a weighted blend of the
two zone medians:

```
meter = (subject_weight · subject_p50 + sky_weight · sky_p50)
        / (subject_weight + sky_weight)
```

Defaults 1.0 / 0.15: the subject zone dominates by design — the whole
reason birdshot exists is that plain average metering exposes for the sky
and delivers silhouettes. The sky still gets a vote so a frame that is
mostly sky doesn't drag the subject into blowout.

*Bench: Scene → Exposure and tone → Auto exposure targets.*

## 2. Auto-exposure: a PID in EV space

`exposure.cpp`. The controller works in **EV (log2) space** because light
is multiplicative: "one stop too dark" is the same correction at noon and
at dusk. The error is

```
err = log2(target / meter)        // positive = needs more light
```

with `target = target_luma` (118 — middle grey biased bright, because the
subject zone median sits below true scene mean when sky leaks into it).
When the calibration wizard has measured the scene (1.x only for now), the
target drops by up to 25% as the measured dynamic range widens past 4 EV.

The pipeline, in the order the controller applies it:

1. **Meter averaging** — the last `ae_average_n` (3) readings, combined by
   `ae_average_mode` (`median` | `mean` | `none`). The window is thrown
   away whenever the raw reading is more than 1.5 EV from target, so big
   scene changes never get averaged into sluggishness. Measured in 1.x:
   unfiltered metering wandered 0.399 EV frame-to-frame; median-3 held
   0.000 EV — but averaging across a step change caused 277% overshoot,
   hence the discard rule.
2. **Highlight priority** — clipping can only ever demand *less* exposure,
   and it overrides the brightness demand when it fires. Two separate
   budgets:
   - subject zone: clip above `max_clip_frac` (0.020) pulls exposure down
     by `0.5·log2(1+overage)`, up to 3 EV — the subject must never blow;
   - sky zone: its own far looser budget `sky_clip_tolerance` (0.60),
     trimming gently (`0.35·log2(1+overage)`, capped at 0.75 EV) — clipped
     sky is *expected*, it only trims, never drives.
3. **Deadband** — within `pid_deadband_ev` (0.20) of target the loop holds
   station and bleeds its integral (×0.85/frame). 0.20 is tuned to sit
   outside the IMX477's gain quantisation (~0.13 EV), which a tighter
   deadband hunts against forever.
4. **Fast acquire** — beyond 1.5 EV of error the scene changed; the loop
   jumps straight toward the answer (clamped to ±4 EV) and zeroes the
   integral rather than winding it up.
5. **PID** — `pid_kp` 0.55, `pid_ki` 0.10, `pid_kd` 0.12; integral clamped
   to ±`pid_integral_clamp_ev` (2.0); output slew-limited to ±`pid_slew_ev`
   (1.5) with anti-windup that unwinds the integral when the limiter
   saturates. The output is then multiplied by `ae_damping` (0.5): the
   sensor applies a request two frames late, so taking the full step every
   frame overshoots — half-steps converge.
6. **Allocation** — `energy = exposure_us · gain · 2^out` is split into
   shutter and gain (§3).

**Settled** means one of two things: the error entered the deadband for 3
frames, or the loop reached a **constrained equilibrium** — the correction
went to ~zero (|out| < 0.10 EV) while the error stayed open because
highlight priority and the brightness demand are pulling against each
other. That second state is not failure; it is exactly the scene this
camera is pointed at, and it is when the lux constant (§4) is allowed to
learn.

The AE clock is **virtual frame-cadence time** (`seq · 0.25 s`), not wall
clock: the gains were tuned at 1.x's 3–4 fps, and a wall-clock dt at the
native engine's 600 fps would amplify the derivative term's response to
metering noise a hundredfold.

*Bench: Scene → Exposure and tone → Auto exposure targets / PID smoothing.*

## 3. The shutter/gain ladder

`allocate()` in `exposure.cpp` splits the required exposure *energy*
(µs × gain) between duration and amplification.

With `prefer_exposure_time` **on** (the default): shutter lengthens all
the way to `shutter_hard_max_us` (33 000 — daylight bird work never needs
longer) before gain moves at all. Gain buys brightness at the cost of
noise it can never give back; a longer exposure is free until motion
smears.

With it **off**, the legacy four-rung ladder, for wingbeats:

1. shutter grows to `motion_limit_us` (2 000 µs = 1/500 s, the freeze-a-
   wingbeat number) at base gain;
2. shutter pins there while gain rises to `gain_preferred_max` (4.0);
3. gain pins while shutter grows to the hard cap;
4. both pinned — gain runs to the sensor maximum as the last resort.

*Bench: Scene → Exposure and tone → Shutter/gain ladder.*

## 4. The lux feed-forward

For a fixed lens, `energy · lux ≈ K` is a constant. The controller learns
K only on settled frames (`K ← 0.9K + 0.1·energy·lux`), checkpoints it to
`state.k_lux`, and uses it to **seed** the next cold start: one lux
reading and the first frame opens within a stop of correct instead of
climbing from wherever the loop last was.

## 5. Quality gates

Every frame gets a verdict from histogram passes on the luma plane
(`analysis.cpp`), recorded in `index.jsonl`:

| Verdict | Test | Key (default) |
|---|---|---|
| `dark` | p95 < threshold — even the brightest pixels are dim | `dark_p95_max` (40) |
| `blown` | subject-zone clip fraction above threshold — clipped **sky** alone is expected and never condemns a frame | `blown_clip_frac` (0.35) |
| `empty` | no tile has contrast, or the whole frame is soft with little detail anywhere | `content_std_min` (8.0), `blur_threshold` |
| `ok` | none of the above | — |

Precedence: blown > dark > empty > ok. "Soft" is judged only after a focus
pass has actually run — until then the verdict stays provisional rather
than falsely condemning. Content is measured on an 8×6 tile grid;
`contrast_tiles` counts tiles whose stddev clears `content_std_min`.

What happens to a rejected frame is `reject_action`: `flag` (keep, record
the verdict), `quarantine` (move under `_rejected/`), or `delete` (never
written; the index still records it).

*Bench: Scene → Quality gates.*

## 6. Focus measures

The lens is manual with no feedback, so focus is measured, not commanded:

- **Laplacian variance** — variance of a 4-neighbour Laplacian; the
  classic "how much edge energy" measure.
- **Normalised sharpness** — `100·√lap_var / max(region_std, 1)`:
  normalised by the focus region's *own* contrast so a flat scene isn't
  called blurry and a contrasty one isn't called sharp for free. ~100 =
  edges as strong as the local contrast allows; single digits = smeared.
  This is what the blur gate reads.
- **Tenengrad** — mean squared Sobel gradient, subsampled ×2 (full density
  cost 44 ms/frame on the CM4 for a number no gate reads).
- **Focus map** — 9×12 tiles of Laplacian energy, √-compressed against the
  peak so mid-focus regions stay visible next to one very sharp edge; the
  sharpest tile gets the ring.

Focus is refined on a native-resolution centre crop when the backend
provides one — the 640×480 plane is for metering; real sharpness needs
real pixels.

The blur gate is **referenced, not fixed**: "Use current view as the sharp
reference" stores the live `sharpness_norm` as `sharpness_reference` and
sets `blur_threshold` to half of it — what counts as sharp depends on the
lens, aperture and subject, so the gate is calibrated to a frame you
called focused.

*Bench: Scene → Focus aids and overlays (map, peaking, zebras, readout,
the 1:1 monitor, the reference button).*

## 7. Bird Flight: the gate ladder

`birdflight.cpp` decides, frame by frame, whether NOW is the shot. It
looks for a **discrete dark subject surrounded by bright sky, sharp along
its boundary, well inside a frame that is mostly sky** — every clause is a
gate, judged in this order (the same order as the Field face's live ladder
and the Bench's gate list):

| # | Gate | Test | Key (default) |
|---|---|---|---|
| 1 | motion | fraction of pixels changed by >12 between frames ≥ threshold | `bf_require_motion` (on), `bf_motion_min` (0.0005) |
| 2 | sky defined | a pixel is "sky" if luma ≥ threshold | `bf_sky_luma_min` (110) |
| 3 | frame is sky | sky fraction of the frame ≥ threshold | `bf_sky_min_frac` (0.5) |
| 4 | subject found | largest 4-connected blob of luma ≤ threshold that does **not** touch the frame border | `bf_subject_luma_max` (80) |
| 5 | subject size | area fraction within [min, max] — too small can't be sharp, too big isn't a bird (or is too close) | `bf_min_area_frac` (0.0004), `bf_max_area_frac` (0.05) |
| 6 | sky ring | fraction of sky in a 3-px ring around the subject's box ≥ threshold — separates a bird from a branch, a roofline, the ground strip | `bf_ring_sky_frac` (0.85) |
| 7 | composition | centroid inside the frame by at least the margin on every edge | `bf_margin_frac` (0.08) |
| 8 | boundary sharp | boundary sharpness ≥ threshold | `bf_min_sharpness` (12.0) |

Details that carry the tuning:

- The search runs on a **4× downsample** (160×120): the connected-component
  pass is microseconds there, and a bird smaller than 4 px was never going
  to pass the sharpness gate anyway.
- Border-touching dark blobs are excluded outright — the ground strip, a
  roofline, a tree at the edge, a bird half out of frame: all things the
  mode must wait out, not shoot.
- **Boundary sharpness** is measured where it matters: pad the subject box
  by 6 px, take the mean of the top decile of gradient magnitudes inside
  it, scale to 0..~100. Sky is flat, so nearly all gradient energy in the
  box IS the subject's edge — a sharp wing scores high, a blurred one low.
  A whole-frame measure cannot do this: empty sky is "sharp" by having
  nothing to blur.

When every gate agrees, the engine fires a burst of `bf_burst` (5), rests
`bf_cooldown_s` (3.0), and stops after `bf_takes` takes (0 = keep
watching). The sighting that fired — measurements, box, reasons — rides
into `index.jsonl` and the EXIF UserComment.

*Bench: Shoot → Bird Flight; the Field face shows the same ladder live.*

## 8. The solar layer (Horizons)

`solar.cpp` — the NOAA solar-position algorithm, in-tree, validated
against published NOAA numbers (rise/set to within a minute). New in 2.0;
1.x had it only as a design review. The constants worth knowing:

| Constant | Value | Why |
|---|---|---|
| sunset (upper limb disappears) | centre at **−0.833°** | refraction at the horizon (0.567°) + solar semidiameter (0.267°) |
| lower limb first touches | centre at **−0.203°** | refraction − semidiameter; the start of the 3.3–3.9 min contact window |
| civil / nautical / astronomical dusk | −6° / −12° / −18° | standard definitions |
| golden hour begins | +6° | convention |
| descent rate | `dh/dt = −15°/h · cos(lat) · sin(azimuth)` | exact identity; 11.5°/h at a 40°N equinox sunset |
| sunset azimuth swing | **62.6°** over the year at 40°N | why a fixed mount needs its FOV checked (`birdshot plan`, using `lens_focal_mm` / `sensor_width_mm`) |

Multi-day alignment (`birdshot align`) pairs frames across days by **solar
elevation, not clock time** — the sun is the clock that actually lit the
scene — then refines with a pixel-shift search for stacking.

The synthetic backend closes the loop: with a site set it lights its sky
from the real solar elevation at those coordinates, so the whole pipeline
— gates, AE, the golden-hour colour ramp — exercises honestly at any hour,
anywhere.

*Bench: Machine → Site - horizons (coordinates, lens geometry, live sun
readout); `birdshot sun` / `plan` / `align` on the CLI.*

## 9. The display aids

The preview's overlays are measurements too — each one has a threshold
worth knowing (`qt/preview.cpp`):

- **Clipping zebras** stripe pixels at luma ≥ 250 — true sensor clipping,
  the same bin the meter's `clip_hi` counts — with a `(row+col)/6` parity
  pattern so the underlying pixels stay half-visible.
- **Focus peaking** highlights where `|gx| + |gy| > 28`, the same edge
  measure the prototype used; the box it draws is the native-resolution
  centre crop that focus is actually judged on.
- **The focus map** shades tiles by √-compressed Laplacian energy (§6) and
  skips tiles below 0.12 of the peak — below that floor the shading is
  noise, not signal.
- **Outdoor mode** exists because a naked preview is unreadable on a
  screen washed out by sunlight. `boost` applies a 2nd/98th-percentile
  contrast stretch to the luma; both styles then mark edges above
  `max(4.0, p96(|∇|) / outdoor_strength)` with hazard bands
  `outdoor_stripe_px` wide — the threshold is *relative to the scene's own
  gradient distribution*, so "sensitivity" means the same thing on a flat
  sky and a busy treeline.
- **The histogram's level points** (click left = black, right = white,
  minimum gap 0.02) are recorded as `tone_black` / `tone_white` — the same
  keys 1.x fed into its tone curve. In the native line they are display
  state until the tone/ISP port lands with the Pi backend.
- Three overlays are deliberately **exempt from the toggle-all gesture's
  spirit**: the timelapse countdown ring, the Bird Flight subject box and
  the non-ok verdict frame always paint, because each one is the answer to
  "is it working?" and hiding it would cost a shot.

*Bench: Scene → Focus aids and overlays; outdoor mode on the Bench view
row, mirrored by the Field face's big buttons.*

---

## Appendix: the settings key reference

Every key in `settings.json`, its default, and where its control lives.
Defaults are identical to 1.x wherever the key exists in both lines
(`Config::defaults()` in `src/config.cpp` is the authority). *Shoot /
Scene / Machine* are the Bench rail's tabs; every bound control is also
findable by label in the Bench's settings search.

### Backend and interface

| Key | Default | Meaning | Control |
|---|---|---|---|
| `backend` | `synthetic` | which camera drives capture | camera selector (Bench header, Camera face) |
| `camera_index` | 0 | device index within the backend | camera selector |
| `replay_path` | `""` | folder of stills the replay backend loops | "Replay footage..." folder dialog |
| `ui_face` | `auto` | boot face; `auto` = dev tree → Bench, Mac → Library, else Camera | Machine → Paths and unattended start |
| `shoot_mode` | 0 | mode dial position | the mode dial (all three faces) |
| `outdoor_mode` | off | sunlight-readable preview | Bench view row; Field face mirror |
| `outdoor_style` | `boost` | `boost` (stretch + edges) or `edges` | Bench view row combo; Field buttons |
| `outdoor_stripe_px` | 3 | hazard band width | Bench view row |
| `outdoor_strength` | 1.0 | edge-marking sensitivity | Bench view row |
| `tone_black` / `tone_white` | 0.0 / 1.0 | histogram level points | drag on the Bench histogram |

### Site (Horizons)

| Key | Default | Meaning | Control |
|---|---|---|---|
| `site_set` | off | the ephemeris, plan and align trust the coordinates | Machine → Site; auto-armed by typing lat/lon |
| `site_lat` / `site_lon` | 0.0 / 0.0 | degrees, +N / +E | Machine → Site |
| `site_elev_m` | 0.0 | metres above sea level | Machine → Site |
| `site_name` | `""` | label in `session.json` and plans | Machine → Site |
| `lens_focal_mm` | 6.0 | planner FOV check | Machine → Site → Lens geometry |
| `sensor_width_mm` | 6.287 | IMX477 width; FOV = 2·atan(w/2f) | Machine → Site → Lens geometry |

### Storage and stills

| Key | Default | Meaning | Control |
|---|---|---|---|
| `data_root` | `~/birdshot-data` | where sessions land | Machine → Paths |
| `min_free_mb` | 2048 | capture stops (blocking overlay) below this | Machine → Paths |
| `capture_width` / `capture_height` | 640 / 480 | the analysis plane; saves prefer the backend's native planes | — (fixed; per-backend modes arrive with their backends) |
| `jpeg_quality` | 92 | the save encoder's quality | Shoot → Stills |
| `save_pgm` | off | loss-free luma sibling for alignment/stacking | Shoot → Stills |
| `burst_count` | 0 | COLLECT frame limit (0 = unlimited) | Shoot → Stills |
| `rapid_count` | 0 | RAPID frame limit (0 = max) | Shoot → Rapid |
| `timelapse_interval_s` | 5.0 | seconds between frames | Shoot → Timelapse |
| `timelapse_count` | 0 | frame limit (0 = unlimited) | Shoot → Timelapse |
| `timelapse_fps` | 60 | dead in both lines (assembly reads `encode_fps`); kept so 1.x configs round-trip | — |
| `reject_action` | `flag` | `flag` / `quarantine` / `delete` a gated frame | Scene → Quality gates |

### Exposure (§2–§4)

| Key | Default | Meaning | Control |
|---|---|---|---|
| `auto_exposure` | on | the PID runs; off = manual pair below | Scene → Exposure |
| `manual_shutter_us` / `manual_gain` | 2000 / 1.0 | the manual operating point | Scene → Exposure → Manual |
| `target_luma` | 118.0 | the meter's setpoint | Scene → Exposure → targets |
| `max_clip_frac` | 0.020 | subject-zone clip budget (highlight priority) | Scene → Exposure → targets |
| `sky_clip_tolerance` | 0.60 | the sky's own, far looser budget — trims only | Scene → Exposure → targets |
| `sky_zone_frac` | 0.40 | top fraction of rows that is "sky" | Scene → Exposure → targets |
| `subject_weight` / `sky_weight` | 1.0 / 0.15 | the meter blend (§1) | Scene → Exposure → targets |
| `motion_limit_us` | 2000 | 1/500 s — freeze a wingbeat | Scene → Exposure → ladder |
| `gain_preferred_max` | 4.0 | gain cap before shutter passes the motion limit | Scene → Exposure → ladder |
| `shutter_hard_max_us` | 33000 | the shutter's ceiling for daylight work | Scene → Exposure → ladder |
| `prefer_exposure_time` | on | shutter to the hard cap before gain moves | Scene → Exposure → ladder |
| `pid_kp` / `pid_ki` / `pid_kd` | 0.55 / 0.10 / 0.12 | the loop gains (tuned at 3–4 fps; the AE clock is virtual) | Scene → Exposure → PID |
| `pid_deadband_ev` | 0.20 | hold-station band, outside gain quantisation | Scene → Exposure → PID |
| `pid_slew_ev` | 1.5 | max EV step per frame (with anti-windup) | Scene → Exposure → PID |
| `pid_integral_clamp_ev` | 2.0 | integral bound | Scene → Exposure → PID |
| `ae_damping` | 0.5 | fraction of each correction applied (2-frame latency) | Scene → Exposure → PID |
| `ae_average_n` / `ae_average_mode` | 3 / `median` | meter window, discarded on a >1.5 EV step | Scene → Exposure → PID |

### Quality gates and focus (§5–§6)

| Key | Default | Meaning | Control |
|---|---|---|---|
| `dark_p95_max` | 40.0 | dark if p95 below | Scene → Quality gates |
| `blown_clip_frac` | 0.35 | blown if *subject-zone* clip above | Scene → Quality gates |
| `blur_threshold` | 2.0 | soft if normalised sharpness below (set to half the reference) | Scene → Quality gates |
| `sharpness_reference` | unset | the frame the user called focused | "Use current view as the sharp reference"; Library detail pane |
| `content_std_min` | 8.0 | a tile carries detail above this stddev | Scene → Quality gates |

### Bird Flight (§7) — in judging order

| Key | Default | Gate |
|---|---|---|
| `bf_require_motion` / `bf_motion_min` | on / 0.0005 | 1: something moved |
| `bf_sky_luma_min` | 110 | 2: what counts as sky |
| `bf_sky_min_frac` | 0.5 | 3: the frame is mostly sky |
| `bf_subject_luma_max` | 80 | 4: what counts as subject |
| `bf_min_area_frac` / `bf_max_area_frac` | 0.0004 / 0.05 | 5: bird-sized |
| `bf_ring_sky_frac` | 0.85 | 6: surrounded by sky |
| `bf_margin_frac` | 0.08 | 7: inside the frame |
| `bf_min_sharpness` | 12.0 | 8: boundary sharp |
| `bf_burst` / `bf_cooldown_s` / `bf_takes` | 5 / 3.0 / 0 | the take machine, not gates |

All under *Shoot → Bird Flight*; values judged live in the Field ladder.

### Identity and assembly

| Key | Default | Meaning | Control |
|---|---|---|---|
| `exif_enabled` | on | stamp EXIF when encoding or assembling | Machine → Identity; encode panel |
| `exif_make` / `exif_model` | `Raspberry Pi` / `IMX477 HQ Camera` | the instrument's identity | Machine → Identity |
| `exif_software` | `birdshot` | Software tag | — (fixed) |
| `exif_lens`, `exif_focal_mm`, `exif_fnumber`, `exif_artist`, `exif_copyright` | empty / 0 | optional identity (0 / blank = not recorded) | Machine → Identity |
| `encode_fps` | 60 | assembled movie frame rate | Library → encode panel |
| `encode_width` | 1920 | scale (0 = native) | Library → encode panel |
| `encode_crf` | 18 | x264 quality, lower = better | Library → encode panel |
| `encode_preset` | `veryfast` | x264 preset | Library → encode panel |
| `encode_only_ok` | on | gate-filtered frame selection | Library → encode panel |

### Machine state (never edited by hand, never rides a profile)

| Key | Holds |
|---|---|
| `version` | config schema version (2) |
| `calibration` | the wizard's zone measurements and derived dynamic range (1.x wizard for now; the target bias in §2 reads it) |
| `state` | resume state: last session, last shutter/gain, frame sequence, the learned `k_lux` |
