# birdshot: Operating Guide for Bird and Sky Photography on the Raspberry Pi HQ Camera

**A Practical Tutorial**

*Applies to: birdshot on a Raspberry Pi Compute Module 4 with an IMX477 sensor,
Debian 11, libcamera 0.0.5, picamera2 0.3.12.*

---

## Abstract

This guide describes the operation of birdshot, a capture application for the
Raspberry Pi High Quality Camera aimed at photographing birds against bright sky.
The subject matter is unusually demanding: the scene spans a luminance range that
exceeds the sensor's, the subject is small, dark, and moving, and the camera is
manually focused. This document explains each operating mode, the meaning and
effect of every adjustable parameter, and gives measured example configurations
for five common tasks. Section IX may be used as a standalone quick reference.
All performance figures were measured on the target hardware rather than
estimated.

---

## Table of Contents

- [I. Introduction](#i-introduction)
- [II. System Characterisation](#ii-system-characterisation)
- [III. Installation and First Run](#iii-installation-and-first-run)
- [IV. Operating Modes](#iv-operating-modes)
- [V. Exposure Control](#v-exposure-control)
- [VI. Tone Reproduction and Levels](#vi-tone-reproduction-and-levels)
- [VII. Focusing](#vii-focusing)
- [VIII. Storage](#viii-storage)
- [IX. Recommended Configurations](#ix-recommended-configurations)
- [X. Troubleshooting](#x-troubleshooting)

---

## I. Introduction

### A. Scope

birdshot replaces a shell script that called `libcamera-still` in a loop. The
motivation for replacing it was not convenience but correctness: the built-in
automatic exposure was producing silhouettes, and a fixed shutter could not
follow changing light.

### B. Why This Subject Is Hard

Three properties of bird-against-sky photography drive nearly every design
decision in this software.

1. **The dynamic range exceeds the sensor's.** Open sky and a shadowed treeline
   differ by 3 to 8 EV. No single exposure holds both. Something must be
   sacrificed, and the software must sacrifice the *right* thing.

2. **Whole-frame metering fails.** A meter averaging the whole frame is dominated
   by sky, which drives exposure down until the bird is a silhouette. This is
   the single most common failure and it is not a bug in the sensor.

3. **The subject moves.** Freezing a wingbeat needs roughly 1/500 s or shorter.
   That competes directly with the need for light.

### C. Document Conventions

Parameter names appear as `parameter_name` and correspond exactly to keys in
`~/.config/birdshot/settings.json`. Every parameter is also reachable from the
graphical interface. Measured values are stated as measured; estimates are
labelled as such.

---

## II. System Characterisation

### A. Hardware

| Item | Value |
|---|---|
| Sensor | IMX477, 4056 × 3040 (12.3 MP), Bayer RGGB |
| Lens | Manual-focus C/CS mount; reports no aperture or focal length |
| Compute | Compute Module 4, 4 × Cortex-A72, 3.7 GB RAM |
| CMA pool | 512 MB (contiguous DMA, carved from system RAM) |
| eMMC | 78 MB/s write |
| USB storage | NTFS over FUSE on USB 2.0, ~12 MB/s sustained |
| Network | Gigabit Ethernet |

### B. Measured Capture Rates

Sensor readout is not the limiting factor. The full-resolution buffer copy
(226 ms at 12.3 MP) and JPEG encoding are, and both are bound by memory
bandwidth rather than clock speed.

| Mode | Sensor capable | COLLECT | RAPID |
|---|---|---|---|
| 4056 × 3040 | 10.8 fps | 3.3 fps | 4.5 fps |
| 2028 × 1520 | 41.7 fps | 8.6 fps | **21.0 fps** |
| 1332 × 990 | 41.7 fps | 10.4 fps | **34.8 fps** |
| 1920 × 1080 H.264 | 50 fps | — | 50 fps |

**Note.** 2028 × 1520 is 2 × 2 binned and has the *same field of view* as full
resolution. It is not a crop. For birds in flight it is usually the correct
choice: 21 fps against 3.3 fps is the difference between catching a wing
position and not.

### C. A Constraint That Is Easy to Miss

Camera buffers are allocated from the CMA pool, which is **not** ordinary RAM —
it must be physically contiguous. One 4056 × 3040 configuration consumes 333 MB
of the 512 MB pool. This is why the RAM-buffer slider (Section VIII-D) has a
practical ceiling near 70% rather than 100%, and why exhausting it produces a
camera failure rather than a memory failure.

---

## III. Installation and First Run

### A. Deployment

From the workstation:

```bash
./sync.sh deploy              # copy, install launchers, run the selftest
./sync.sh install-autostart   # optional: launch at every login
```

`deploy` finishes by running 18 self-tests against the real camera. If any fail,
resolve that before shooting; they cover naming, the quality gates, the exposure
ladder, PID convergence, storage migration and EXIF.

### B. Starting the Application

Three desktop icons are installed:

| Icon | Purpose |
|---|---|
| **birdshot** | normal start, maximised |
| **birdshot (AUTO)** | honours an `autowrite.yes` USB stick and begins capturing |
| **birdshot (Focus)** | opens directly on the focusing tools |

### C. Calibration

On first run the application offers a calibration wizard. **Complete it.** It
takes about one minute and asks you to aim at three things:

1. **Open sky** — fill the frame with bright empty sky.
2. **The treeline** — the horizon where birds will perch or pass.
3. **A subject or grey card** (optional).

The wizard measures the exposure the loop converges on for each, and the
difference between sky and treeline is the scene's dynamic range. This sets how
much highlight headroom the metering must reserve. A 7 EV scene requires a far
more conservative target than a 3 EV one, and this cannot be inferred from a
single frame.

Results persist, along with the last shutter, gain, and frame counter, so a
restart resumes rather than starting cold.

---

## IV. Operating Modes

The mode dial in the Bench face's rail shows all five modes simultaneously. Use the
arrows, the `[` and `]` keys, or click. Selecting a mode opens its settings.

### A. Stills (COLLECT)

The full pipeline. Each frame is analysed at native resolution, scored against
the quality gates, and filed into a shutter-duration folder.

Use when: the subject is perched or slow, image assessment matters, or you intend
to cull afterwards using the recorded metrics.

**Cost:** roughly 2.5× slower than RAPID, because of the native-resolution focus
measurement and the content analysis.

### B. Rapid

Metering and auto-exposure only. No quality gates, no focus measurement. Frames
are written as flat, datestamped files.

Use when: birds are in flight, or anything where frame rate is the priority.

**What is given up:** no `verdict`, no sharpness figure, no `contrast_tiles`.
Exposure metadata is still recorded, and frames can be scored later.

### C. Timelapse

Captures at a fixed interval with auto-exposure running continuously between
frames, so the loop is already converged when each exposure is taken.

While running, the preview displays a **countdown ring** showing seconds until
the next frame, and the status line reports the last file written and the tier
it will end up on. The countdown remains visible even with overlays switched
off, because during an unattended timelapse the only questions are whether it is
still running and when the next frame lands.

Interval guidance:

| Subject | Interval | Result at 60 fps |
|---|---|---|
| Cloud movement | 2–5 s | 1 h → 12–30 s of video |
| Sunrise / sunset | 5–10 s | 1 h → 6–12 s |
| Shadow across a day | 30–60 s | 8 h → 8–16 s |
| Nest activity | 10–20 s | 4 h → 12–24 s |

### D. Video

H.264 to MP4 using the hardware encoder. The encoder tops out at 1080p; asking
for more is silently renegotiated down, so higher resolutions are not offered.

Recording reconfigures the sensor, so stills pause. Any capture in progress is
stopped and closed cleanly first. The live preview and exposure meter continue
throughout, using a lightweight metering path so they do not compete with the
encoder.

---

## V. Exposure Control

### A. Why the Built-In AGC Is Disabled

`AeEnable` is off permanently, for two measured reasons.

1. libcamera's AGC needs approximately five frames to re-converge. At 3.3 fps
   that is over a second of wasted capture on every scene change.
2. It meters the whole frame, which for this subject produces silhouettes.

### B. The Metering Model

The frame is divided into two zones:

```
+-----------------------------+  <-- top sky_zone_frac of the frame
|          SKY ZONE           |      weighted at sky_weight (0.15)
+-----------------------------+
|        SUBJECT ZONE         |      weighted at 1.0
|   (treeline, perch, bird)   |
+-----------------------------+
```

The metering value is the weighted median of the two zones. Because the subject
zone dominates, exposure follows the treeline rather than the sky.

| Parameter | Default | Effect of increasing | Effect of decreasing |
|---|---|---|---|
| `sky_zone_frac` | 0.40 | more of the frame treated as sky; exposure follows a smaller region | more of the frame counts as subject; a high horizon can drag exposure down |
| `sky_weight` | 0.15 | sky influences exposure more; frames darken | sky ignored further; subject brightens, sky blows out sooner |

**Setting `sky_zone_frac`:** it should match where your horizon actually sits.
Enable the metering-zone overlay (scroll up on the image) and set it so the
dashed line falls on your horizon.

### C. Target Luminance

`target_luma` (0–255) is the brightness the subject zone is driven toward.

| Value | Result |
|---|---|
| 40–60 | dark, protects highlights strongly, shadows need lifting afterwards |
| 90–120 | conventional exposure; the default of 118 is a mid-grey target |
| 140+ | bright, blows highlights readily |

This is the single most influential setting. The calibration wizard sets it from
your subject reading if you complete the optional third step.

### D. Highlight Priority

The clipping term can only ever *reduce* exposure. It is measured on the subject
zone, with the sky held to a separate and far looser budget.

| Parameter | Default | Meaning |
|---|---|---|
| `max_clip_frac` | 0.02 | tolerated clipping **in the subject zone** |
| `sky_clip_tolerance` | 0.60 | how much the sky may clip before it says anything |

This separation matters. Metering clipping across the whole frame means a bright
sky always exceeds a 2% budget, so the term fires on every frame and drives
exposure down until the treeline is black. Separate budgets let the sky clip —
which is the accepted trade — while still protecting the subject.

Behaviour, measured:

| Scene | Response |
|---|---|
| Bird under a blown sky, subject correct | holds (−0.09 EV) |
| Sky completely gone | gentle trim (−0.23 EV) |
| Subject itself blowing out | strong correction (−2.06 EV) |
| Scene too dark | brightens (+1.23 EV) |

### E. The Shutter/Gain Ladder

Given a required quantity of light, the software must choose a shutter and gain
pair. Two strategies are available, and the choice matters.

**Exposure priority** (`prefer_exposure_time: true`, the default) reaches for
duration first and treats gain as a last resort. Gain buys brightness at a
permanent cost in noise; a longer exposure costs only motion blur, and only once
it becomes long.

**Motion priority** (`prefer_exposure_time: false`) holds the shutter at
`motion_limit_us` and raises gain instead, keeping wingbeats frozen at the cost
of noise.

The difference at one light level:

```
motion priority:    5000 us @ gain 4.00
exposure priority: 20000 us @ gain 1.00
```

| Parameter | Default | Applies to | Effect |
|---|---|---|---|
| `motion_limit_us` | 2000 (1/500 s) | motion priority | longest shutter before gain is used |
| `gain_preferred_max` | 4.0 | both | gain ceiling before the shutter is extended further |
| `shutter_hard_max_us` | 33000 (1/30 s) | both | absolute longest shutter in daylight modes |

**Choosing between them.** Use exposure priority for perched birds, treeline
detail and timelapse. Switch to motion priority for flight. This is the one
setting most worth changing between sessions.

### F. The PID Controller and Its Damping

Error is computed in EV (log₂) space because exposure is multiplicative. A
controller linear in microseconds would be badly mistuned at one end of its
range.

| Parameter | Default | Effect of increasing | Effect of decreasing |
|---|---|---|---|
| `pid_kp` | 0.55 | faster response, more overshoot | sluggish, may never reach target |
| `pid_ki` | 0.10 | eliminates steady offset faster; can wind up | persistent small error |
| `pid_kd` | 0.12 | damps overshoot; amplifies noise | more overshoot |
| `pid_deadband_ev` | 0.20 | stops hunting sooner; coarser accuracy | finer accuracy; may hunt |
| `pid_slew_ev` | 1.5 | larger jumps allowed per frame | gentler, slower convergence |
| `ae_damping` | 0.5 | full corrections; overshoot with latency | very gentle, slow |
| `ae_average_n` | 3 | steadier; more lag | more responsive; jitters |
| `ae_average_mode` | median | `median` rejects outliers; `mean` smooths; `none` is raw | — |

**Why the filter is scheduled.** Filtering and responsiveness pull against each
other. Measured with the real 2-frame control latency and 6% metering noise:

| Configuration | Steady-scene wander | Step response |
|---|---|---|
| Unfiltered | 0.399 EV, 167 changes / 200 frames | 2 frames, no overshoot |
| Median-3 | 0.000 EV, motionless | 277% overshoot |

Neither is acceptable alone. The software therefore uses the **raw** reading when
the error is large (the light genuinely changed) and the **median** once it is
small (steady scene, reject noise). This gives 0.000 EV wander on a steady scene
while still tracking a 4× light change.

**If you see the exposure hunting**, raise `pid_deadband_ev` before touching the
gains. If it responds sluggishly to real changes, lower `ae_average_n` to 1.

### G. Lux Feed-Forward

The sensor reports an estimate of scene illuminance. Because required exposure is
inversely proportional to scene luminance, a single learned constant converts one
to the other. When the camera swings from treeline to open sky, this jumps to
approximately the right exposure in one frame rather than integrating toward it.

The constant is learned automatically from well-exposed frames and persists
between runs. No configuration is required. It appears in settings as
`state.k_lux`.

---

## VI. Tone Reproduction and Levels

### A. The Gamma Curve Is Already Applied

The IMX477's tuning file contains a 33-point gamma curve that the ISP applies in
hardware to every frame:

```
x: 0, 1024, 2048, 3072, 4096, ...       (linear input, 16-bit)
y: 0, 5040, 9338, 12356, 15312, ...     (gamma-corrected output)
```

**Do not apply it again in software.** A linear 0.10 maps to 0.332 once and 0.717
twice — 116% too bright. It would also cost 449 ms per frame at full resolution.

All tone control in birdshot works by *replacing the curve the ISP uses*, so the
hardware applies it at no CPU cost and no double-gamma occurs. The one
consequence is that changing the curve reopens the camera, taking about a second.

### B. Setting Black and White Points

The histogram beneath the image is the levels control.

| Action | Effect |
|---|---|
| Click the **left half** | sets the black point at that position |
| Click the **right half** | sets the white point |
| Drag | moves whichever point is active |
| Left/Right arrows | nudge by 0.01 (hold Shift for 0.05) |
| Up/Down arrows | switch which point is active |
| Double-click | resets to full range |

**What this does.** Between the two points, the range is stretched to fill the
output. A scene occupying 0.15–0.70 then uses all of 0–1 instead of half of it.
This is what expands the mid-tones and makes average brightness legible — the
practical reason to set levels at all.

**Procedure.**

1. Point the camera at a representative scene and let exposure settle.
2. Observe where the histogram data actually begins and ends. There is usually
   empty space at both ends.
3. Click just to the left of where the data starts. That is the black point.
4. Click just to the right of where the bulk of the data ends — *not* past the
   sky spike at the far right, which you are deliberately allowing to clip.
5. Wait. Application is deferred by 900 ms so that dragging does not reopen the
   camera on every pixel of movement.

### C. Knee Rounding

Outside the points, tones are **not** clipped. A knee function bends
asymptotically toward 0 and 1:

```
t = -0.3  ->  0.0036          t = +1.0  ->  0.9559
t = +0.5  ->  0.5000          t = +1.4  ->  0.9984
```

The curve is linear through the middle and joins the knees with matching slope,
so there is no visible kink. Nothing ever reaches a hard stop, which is why
highlights round off instead of going flat.

`tone_knee_soft` (0–0.45, default 0.12) controls how much of the range is given
to the knees. Zero clips hard. Larger values compress more gently over a wider
region.

### D. Tone Presets

| Preset | Use |
|---|---|
| `stock` | the ISP's own curve; the correct default |
| `levels` | black/white points from the histogram, with knees |
| `rolloff` | lifts a dark average *and* rounds off whites — the shape this subject usually wants |
| `lift` | shadows only |
| `contrast` | scales contrast about 18% grey |
| `gamma` | plain power curve |
| `linear` | no gamma; flat and dark, closest to raw |

`rolloff` deserves particular attention for bird work. It performs two jobs at
once: it lifts the low and mid tones so an on-average-dark frame is not stranded
far from white, and it bends the top into a shoulder. Measured with lift 0.18:
input 0.02 rises to 0.233, while the steps approaching white shrink from a
constant 0.016 to 0.0053.

### E. Live Controls

`Contrast`, `Brightness`, `Saturation` and `Sharpness` are applied by the ISP as
live controls and need no restart. Use these for small adjustments; use the tone
curve for the shape of the response.

---

## VII. Focusing

The lens is manual and reports nothing. Three aids are provided, all on the
Focus aids section of the Scene tab.

### A. The Focus Map

Shades each region of the frame by how much detail the lens is resolving there,
and rings the sharpest. This answers "which part of this is in focus", which
peaking alone cannot: peaking illuminates a sharp branch and noisy sky
identically.

### B. The 1:1 Focus Monitor

A frameless, always-on-top window showing **actual sensor pixels** at 100–400%,
with a numeric score and a peak-hold marker.

**Procedure.** Turn the focus ring until the number stops climbing. The yellow
marker records the best reading achieved, so you can tell whether you have gone
past the optimum. Full-frame copies are taken only while this window is open, so
it costs nothing when closed.

### C. Calibrating the Blur Gate

What counts as sharp depends on the lens, the aperture and the subject.
Accordingly the blur gate is referenced to a frame *you* declare sharp: focus
carefully, then press **Use current view as the sharp reference**. The threshold
is set to half that reading.

Until you do this it sits at a floor that rejects nothing. That is deliberate — a
mis-set blur gate that silently discards frames is far worse than one that passes
everything.

### D. Outdoor Mode

For working in sunlight, where the display washes out. It contrast-stretches the
preview against its own 2nd and 98th percentiles, then draws the frame's own
gradients as **alternating yellow and black hazard bands**. Solid yellow
disappears against bright sky and black disappears against shadow; alternating
them guarantees one of the two contrasts, whatever the edge lies on.

An "edges only" variant discards the picture entirely and shows structure on a
dark field — the most legible option in direct sun.

---

## VIII. Storage

### A. The Ordering That Matters

**The USB stick is the slowest storage in the system, not the fastest.** NTFS
over FUSE on a USB 2.0 port sustains about 12 MB/s; the eMMC does 78. Capture
therefore writes to the eMMC and migrates outward.

### B. Filenames

Every frame, in every mode, is named `YYYYMMDDHHMMSScc` — 16 digits at
centisecond resolution:

```
2026-07-31 13:35:32.47   ->   2026073113353247.jpg
```

The last four digits are centiseconds within the minute (0000–5999), which is
identical to writing seconds then centiseconds separately. Names sort
chronologically as plain text, and at 100 slots per second against a 35 fps
ceiling, collisions essentially never occur.

Legacy shutter-duration folders are preserved: `s<N>` is tenths of a second,
`ms<N>` tenths of a millisecond, `us<N>` microseconds. The two ranges overlap and
the original rule is reproduced exactly — 1600000 µs is `s16`, but 160000 µs is
`ms1600`.

### C. The Cascade

Optional. Frames are written into groups which background workers migrate down a
chain of tiers, verifying each before freeing the source:

```
RAM (1.9 GB, 459 MB/s) -> eMMC (16 GB, 60 MB/s) -> USB (55 GB, 12 MB/s)
```

Each tier clears itself, so **capture runs for as long as the bottom tier has
room, not the top one**. The RAM tier is optional; without it the chain is
eMMC → USB and behaves identically.

**What it does not do.** It does not make capture faster — the eMMC already
exceeds the peak capture demand of ~12 MB/s by five times. Nor can it raise the
sustained rate above the slowest tier. Upper tiers absorb bursts; over a long run
the cascade shifts data only as fast as the bottom accepts it.

**Safety.** A group is deleted from a tier only after being copied to the next
*and verified* — every file present, every size identical. The bottom tier never
deletes unless ring mode is explicitly enabled.

### D. The RAM Slider

Sets how much of system RAM the top tier may use, applied by remounting the
tmpfs.

**The practical ceiling is about 70%, not 100%.** CMA is carved from the same
MemTotal, and a tmpfs large enough to crowd it makes the camera fail to allocate
buffers. The slider displays the remaining headroom and warns past the safe line.

| Slider | Buffer | Headroom for CMA + OS |
|---|---|---|
| 50% | 1872 MB | +760 MB |
| 70% | 2621 MB | +11 MB (ceiling) |
| 80% | 2996 MB | **−363 MB — will fail** |

### E. Unattended Operation

Place a file named `autowrite.yes` in the root of a USB stick. On the next launch
birdshot captures to that stick automatically and copies on a timer. The file may be
empty, or carry `key=value` lines:

```
mode=continuous        res=1        count=0       start=yes
interval=30            delete_after_copy=no       quality=92
```

Unknown keys are reported rather than ignored — a typo in an unattended
configuration is otherwise invisible until you find the card empty.

---

## IX. Recommended Configurations

These are starting points, measured on the target hardware. Adjust
`target_luma` first; it has the largest effect.

### A. Birds in Flight

Frame rate dominates. Accept noise; reject blur.

```
capture_mode          1        # 2028x1520 binned, same FOV, 21 fps
Mode                  Rapid
prefer_exposure_time  false    # motion priority: hold the shutter short
motion_limit_us       1000     # 1/1000 s
gain_preferred_max    8.0      # allow noise to keep it frozen
shutter_hard_max_us   2000
target_luma           70
max_clip_frac         0.02
sky_clip_tolerance    0.70     # sky will blow; that is accepted
pid_deadband_ev       0.15     # respond quickly
ae_average_n          1        # no filtering; the scene changes fast
```

### B. Perched Birds and Treeline Detail

Resolution and tonality dominate. The subject is static.

```
capture_mode          0        # 4056x3040 native
Mode                  Stills
prefer_exposure_time  true     # duration before gain
motion_limit_us       4000
gain_preferred_max    2.0      # keep it clean
shutter_hard_max_us   20000
target_luma           95
tone_curve            rolloff
tone_lift             0.15
tone_knee             0.65
sky_zone_frac         0.35
```

### C. Night Sky and Long Exposure

Reproduces the original `s191` work.

```
capture_mode          0
Mode                  Stills
auto_exposure         false    # long exposures should not be automatic
manual_shutter_us     19100000 # 19.1 s -> folder s191
manual_gain           1.0
reject_action         flag
tone_curve            stock
```

Disable auto-exposure. Nineteen-second exposures are a deliberate artistic
choice, not something a control loop should be selecting.

### D. All-Day Unattended Monitoring

Longevity and not filling up dominate.

```
Mode                  Rapid
capture_mode          1
cascade_enabled       true
cascade_ram_pct       50
group_frames          200
offload_continuous    true
offload_interval_s    30
cascade_ring          false    # true only if recent footage matters more than complete footage
min_free_mb           2048
target_luma           85
ae_average_n          3        # steady; light changes slowly
pid_deadband_ev       0.25     # avoid chasing cloud shadows
```

Place `autowrite.yes` on the stick and use the **birdshot (AUTO)** icon.

### E. Timelapse of Changing Light

The light changes by design, so the loop must track it without flickering.

```
Mode                  Timelapse
capture_mode          0
timelapse_interval_s  5.0
prefer_exposure_time  true
target_luma           100
ae_average_n          5        # heavy smoothing; frame-to-frame flicker is the enemy
ae_damping            0.35
pid_deadband_ev       0.15     # small deadband so it tracks the sun
tone_curve            rolloff
reject_action         flag
```

Assemble from the Library face's encode panel at 60 fps, restricted to frames that passed the
quality gates — this removes the occasional dark or blown frame that would
otherwise flicker through the finished sequence.

---

## X. Troubleshooting

### A. Exposure Is Stuck Dark

Most often the highlight term is being driven by the sky. Check that
`sky_clip_tolerance` is 0.5 or higher and that `max_clip_frac` refers to the
subject zone (0.02 is correct). Verify `sky_zone_frac` places the boundary on
your actual horizon.

If `target_luma` is below about 60, the image is *intended* to be dark; raise it.

### B. Exposure Hunts or Dances

Raise `pid_deadband_ev` to 0.25. If that is insufficient, lower `ae_damping` to
0.35. Adjust the PID gains last.

### C. Exposure Stops Responding

This was a defect and is fixed; if it recurs, the log will contain
`sensor clamped the request; resuming from actual`. That message is normal
recovery, not an error.

### D. Camera Fails to Start

Check `dmesg` for `cma_alloc: alloc failed`. If present, the CMA pool is
exhausted — lower `cascade_ram_pct`, or close another application holding the
camera. Only one process may own the camera at a time.

### E. Frames Are Not Reaching the USB Stick

Use **Flush everything down now** in Machine > Cascade. Note that at 21 fps the
capture rate (~10 MB/s) is close to the stick's ceiling (~12 MB/s), so continuous
copying only just keeps pace and cannot catch up once behind. The status bar
reports how many frames the stick is behind.

### F. Everything Is Marked "empty"

The content gate found no region carrying contrast. Pointing at clear sky
produces this correctly. If the scene plainly has detail, the lens is likely out
of focus — check the focus monitor, and confirm the blur gate has been calibrated
rather than left at an arbitrary threshold.

---

## Appendix A: Keyboard and Mouse Reference

| Input | Action |
|---|---|
| `F11` or double-click the image | fullscreen |
| `Esc` | leave fullscreen; dismiss the out-of-space notice |
| `[` `]` | step the capture mode |
| Scroll wheel over the image | all overlays on (up) / off (down) |
| Click histogram left / right half | set black / white point |
| Arrow keys on the histogram | nudge the active point |
| Double-click the histogram | reset levels |

## Appendix B: Command Line

```bash
birdshot-cli info                       # camera, modes, storage, calibration
birdshot-cli capture -n 200             # COLLECT
birdshot-cli rapid -n 500 --res 1       # RAPID
birdshot-cli timelapse -i 5 -n 720
birdshot-cli cascade -n 500 -g 200      # group capture with migration
birdshot-cli flush                      # force all groups to the archive
birdshot-cli exif <session>             # stamp EXIF from the index
birdshot-cli assemble <session> --exif --fps 60
birdshot-cli selftest                   # 18 checks against the real camera
```

Prefixing `rapid` with `BIRDSHOT_PROFILE=1` prints a per-frame timing breakdown.

---

*This software is distributed under the MIT License.
Copyright (c) 2026 Paul Richeson*
