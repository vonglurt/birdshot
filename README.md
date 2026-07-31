# birdshot

Bird and sky capture for a Raspberry Pi Compute Module 4 with an **IMX477 (Pi HQ
Camera)**, replacing the old `runCam.sh` loop.

The GUI runs on the Pi's own display; captures land on the eMMC and are pulled to
the Mac over the network.

---

## What the hardware actually is

Worth stating plainly, because several of these differ from what you might expect
and they drive every design decision below.

| | |
|---|---|
| Camera | **IMX477**, 4056x3040 (12.3 MP), manual-focus C/CS mount |
| Board | Compute Module 4, 4 GB, Debian 11 bullseye, 4x Cortex-A72 |
| Stack | libcamera 0.0.5, libcamera-apps 1.2.1, picamera2 0.3.12, Python 3.9 |
| eMMC | **78 MB/s** write, ~16 GB free |
| USB stick | NTFS over FUSE on a **USB 2.0** port: **~12 MB/s** sustained, 59 GB |
| Network | Gigabit to the Mac, 0.7 ms |

**The USB stick is the slowest storage in the system, not the fastest.** Capture
goes to the eMMC and is offloaded to USB in the background; writing captures
straight to the stick would stall every burst.

### Measured capture rates

Sensor readout is not the limit -- the full-resolution buffer copy and JPEG
encode are, and they are memory-bandwidth bound.

| Mode | Sensor can do | COLLECT | **RAPID** | Use for |
|---|---|---|---|---|
| 4056x3040 native | 10.8 fps | 3.3 | **4.5** | perched birds, treeline, timelapse |
| 2028x1520 binned | 41.7 fps | 8.6 | **21.0** | birds in flight (same field of view) |
| 1332x990 cropped | 41.7 fps | 10.4 | **34.8** | fastest action |
| 1920x1080 H.264 | 50 fps | -- | **50** | video (hardware encoder) |

**COLLECT** runs the full pipeline: quality gates, native-resolution focus
measurement, `s<N>` shutter folders. **RAPID** strips that back to metering and
auto-exposure only and writes flat datestamped files, which is where the 2-3x
comes from. Both use the same auto-exposure.

Encode threads are set to 3: measured identical to 6 on this board, and it leaves
a core for the capture loop and the GUI.

---

## Folder naming

The old `runCam.sh` convention is preserved exactly. Note this differs from what
the directory names suggest at a glance:

```
s191   = --shutter 19100000 us = 19.1 SECONDS   (not 191 ms)
s01    =           100000 us   = 0.1 s
ms1600 =           160000 us   = 160 ms
ms500  =            50000 us   = 50 ms
```

So `s<N>` is tenths of a second and `ms<N>` is tenths of a millisecond. The two
ranges overlap, and the old scripts resolved it by using `s` only for whole
tenths of a second -- 1600000 us is `s16`, but 160000 us is `ms1600`. That rule
is reproduced, including the two-digit zero padding (`s01`, `s04`).

Bird work runs far shorter than anything in the old set, so the scheme extends
downwards with `us<N>` below 1 ms, where `ms` would start rounding away real
precision. Your existing `s191` night captures still sort correctly alongside.

Auto-exposure produces arbitrary values, so directory names are **buckets**
(100 us steps in the `ms` range, 10 us in `us`). Every frame's exact exposure is
recorded in the session's `index.jsonl`.

---

## Layout on the Pi

```
/home/pi/birdshot/            the application (synced from the Mac)
/home/pi/birdshot-data/
  sess-<epoch>/              one directory per COLLECT run
    ms18/  ms20/  s191/      split by shutter duration, as before
      2026073113353247.jpg
    index.jsonl              one JSON line per frame: settings + all metrics
    session.json             summary written on close
    _rejected/               only if reject action is "quarantine"
  tlc-<epoch>/               timelapse runs
  video/                     H.264 recordings
  timelapse/                 assembled movies
  latest.jpg                 small live preview, refreshed ~1 Hz
```

### Frame filenames

Every frame, in both COLLECT and RAPID, is named `YYYYMMDDHHMMSScc` -- 16 digits
at centisecond resolution:

```
2026-07-31 13:35:32.47   ->   2026073113353247.jpg
```

The last four digits are centiseconds within the minute (0000-5999), which is
the same string as writing seconds then centiseconds separately, since
`seconds * 100 + centiseconds` is exactly that count. So `yyyymmddhhmm`+`cs` and
`yyyymmddhhmmss`+`cc` agree, and the name sorts chronologically as plain text.

One-second naming needed collision suffixes constantly -- rapid capture runs at up
to 35 fps. At 100 slots per second frames land about three apart, so the `_001`
suffix is now a safety net rather than the normal case. Names are still claimed
atomically with `O_EXCL`, because three encoder threads finish concurrently.

Frames are written as `.part` and renamed once complete, so a sync running on the
Mac can never pick up a half-written JPEG.

---

## Getting started

From the Mac, in this directory:

```bash
./sync.sh deploy          # push, install launcher, run the selftest
./sync.sh gui             # start the GUI on the Pi's display
```

Then on the Pi's screen, work through the calibration wizard it offers on first
run (about a minute -- see below).

### Everyday commands

```bash
./sync.sh watch                  # auto-push on every save while developing
./sync.sh status                 # dry run: what each direction would change
./sync.sh sync                   # two-way, newer file wins
./sync.sh selftest               # verify the deployment against the real camera
./sync.sh info                   # camera, modes, storage, calibration state
./sync.sh logs                   # tail the GUI log

./mac/pull-photos.sh watch       # keep pulling new frames to ~/birdshot-data
./mac/pull-photos.sh live        # ... and print each file as it lands
./mac/assemble.sh ~/birdshot-data/tlc-1730380000 --fps 60
```

`sync` is a convenience, not a conflict-resolving sync engine: it runs rsync
`--update` both ways, so the newer mtime wins per file and deletions never
propagate. Edit on one side at a time. Run `status` first if unsure.

> macOS 26 ships `openrsync`, not GNU rsync, so the scripts stick to portable
> flags. They will pick up a Homebrew GNU rsync automatically if you install one.

### Headless, over SSH

```bash
birdshot-cli info
birdshot-cli capture -n 200          # COLLECT: full pipeline with quality gates
birdshot-cli rapid -n 500 --res 1    # RAPID: flat YYYYmmddHHMMSS names, ~21 fps
birdshot-cli timelapse -i 5 -n 720   # one frame every 5 s
birdshot-cli sessions
birdshot-cli assemble rapid-1730380000 --fps 60
birdshot-cli selftest
```

`BIRDSHOT_PROFILE=1` in front of `rapid` prints a per-frame breakdown of where the
capture loop spends its time -- useful if a change makes things slower.

---

## Auto-exposure

libcamera's own AGC is switched off permanently. Two reasons, both of which bite
here: it needs roughly five frames to re-converge (over a second of wasted burst
at these rates), and it meters the whole frame, which against bright sky
guarantees the bird comes out a silhouette.

Three mechanisms act together:

**Feed-forward from lux.** Scene luminance and required exposure are inversely
proportional, so one learned constant turns the sensor's own lux reading into an
exposure estimate. Swing the camera from treeline to open sky and the
feed-forward jumps in a single frame rather than integrating its way there. The
constant is learned online from well-exposed frames and persists between runs.

**PID feedback in EV space.** Error is `log2(target / measured)`. Exposure is
multiplicative, so a linear controller on raw microseconds would be badly
mistuned at one end of the range or the other. Includes a deadband (stops
hunting), a slew limit, integral clamping with anti-windup, and EMA smoothing on
the measurement so a bird crossing frame does not swing exposure. Above 1.5 EV of
error it abandons the PID and jumps straight to the answer -- this is what removes
the "AE takes five photos" problem.

**Highlight priority.** The clipping term can only ever push exposure *down*, and
it overrides the brightness term when it fires. It is measured on the **subject
zone**, with the sky held to its own far looser budget (60% by default). Metering
whole-frame clipping meant a bright sky always blew past a 2% tolerance, so the
term fired on every frame and drove exposure down until the treeline went black
-- the loop sat there dark and never came back.

### If auto-exposure seems to stop

It used to, and the cause was not the PID. The loop waited for the sensor to
report back exactly what it had been asked for before correcting again, and the
IMX477 never does: exposure quantises to line times (asking 2000 us yields 1986)
and analogue gain snaps to its own steps (asking 2.10 yields 2.00). Worse, a gain
change takes 5-7 frames to reach metadata on this board, not the 2 the docs
imply. The wait had no escape, so ordinary quantisation could stall it forever
-- and the one thing that could have corrected it was the loop that had stopped.

Now the wait is bounded, tolerances are relative rather than absolute, and if a
request really was clamped the controller re-syncs from whatever the sensor
actually did (clearing the integral, which would otherwise be wound up against
an unreachable target). Measured over 40 s: 0 stalls, 227 corrections, against
41 stalls and 82 corrections before.

"Settled" also counts a *constrained* equilibrium -- when highlight priority and
the brightness term pull against each other, which is routine for a dark subject
under a blown sky, the error never enters the deadband but the correction goes to
zero. Requiring a small error rather than a small correction meant settled never
fired in exactly the scene this camera is pointed at, which also stopped the lux
constant ever being learned.

Metering is zone-weighted, not average: the top 40% of frame is treated as sky
and weighted at 0.15, the rest as subject at 1.0. All tunable in the Exposure tab.

### Shutter/gain ladder

Auto-exposure picks the **shortest shutter** the light allows, so wingbeats stay
frozen. As the scene demands more light:

1. base gain, shutter lengthens to the motion limit (default 1/500 s) -- cleanest
2. shutter pinned at the motion limit, gain rises to its preferred cap (4x)
3. gain pinned, shutter lengthens toward the hard cap -- blur risk begins
4. shutter at the hard cap, gain rises to the sensor maximum -- last resort

Both caps are adjustable. Gain is spent before the shutter is allowed past the
motion limit, which is the trade you want for birds.

### Calibration wizard

Runs on first start, and any time from the Exposure tab. It asks you to point at
**open sky**, then the **treeline**, then optionally a subject or grey card, and
lets the loop settle on each.

The sky-to-treeline gap is the dynamic range the sensor has to straddle, and it
sets how much highlight headroom metering must leave -- a 7 EV gap needs a far
more conservative target than a 3 EV one, and guessing that from one frame is
exactly what silhouettes birds. Results persist in
`~/.config/birdshot/settings.json`, along with the last shutter, gain and frame
counter, so a restart resumes where you left off.

---

## Quality gates

Every frame is scored from the free 640x480 luma plane, plus a native-resolution
512x512 centre crop for focus (judging focus on a downscaled preview would hide
exactly the softness we are looking for). Verdicts land in `index.jsonl`:

- **dark** -- p95 below threshold
- **blown** -- clipping has spread into the *subject* zone. Clipped sky alone is
  expected for this subject matter and is deliberately not penalised.
- **empty** -- no tile anywhere carries real contrast, or the frame is soft.
  Covers blur, fog, lens cap, and pointing at blank sky.
- **ok** -- everything else

Rejected frames can be flagged (default -- keep everything, record the verdict),
quarantined into `_rejected/`, or dropped. Timelapse assembly uses only frames
that passed, which quietly removes the flicker those frames would cause.

---

## The interface

Four tabs, each a stack of collapsible sections. A collapsed section still shows
its current setting on one line, so closing things does not hide state:

| Tab | Holds |
|---|---|
| **Shoot** | mode dropdown, one START button, and the settings for each mode |
| **Image** | exposure and tone, focus aids, quality gates |
| **Storage** | cascade tiers, RAM buffer, flush, paths, unattended start |
| **Process** | encode photos into a movie, EXIF |

One **mode dropdown** (Stills / Rapid / Timelapse / Video) drives a single START
button, and selecting a mode opens its section. The four separate start buttons
are gone.

**Fullscreen** -- the button, `F11`, or a **double-click on the image**. Every
overlay stays live; `Esc`, `F11` or another double-click returns.

### Outdoor mode

For working in sunlight, where the panel washes out and a bird against bright sky
is a low-contrast shape. It contrast-stretches the preview against its own 2nd
and 98th percentiles, then burns the frame's gradients in as a bright outline:

- **boost** -- the stretched picture with edges drawn over it
- **edges only** -- structure on a dark field, nothing competing with it

Edges are drawn as **alternating yellow and black hazard bands**, one pixel thick.
Flat yellow disappears against a bright sky and black disappears against shadow;
alternating them means one of the two always contrasts, whatever the edge happens
to lie on. Band width and sensitivity are adjustable.

Edge sensitivity is relative to the scene's own gradients, so a hazy view still
shows its edges rather than going blank. Measured on a real frame, the strength
control spans 0.8% to 33% of the frame marked as edges.

### Out of space

When every tier is full, capture stops and a full-window notice says so, lists
what each tier has left, and gives the ways to free space. It cannot be dismissed
until there is actually room -- a status-bar line is the wrong place for "frames
are being lost right now".

## Rapid tab -- fastest single photos

Flat files named `YYYYmmddHHMMSS.jpg`, one folder per run, no shutter
subdirectories and no quality gates. Because the camera shoots several frames a
second and that format only has one-second resolution, the first frame in any
second gets the bare name and later ones get `_001`, `_002` and so on -- so the
exact requested filename is used wherever it is actually unambiguous.

Two strategies:

- **continuous** (default) encodes as it goes on the worker threads. The
  full-resolution buffer copy parallelises across them, which is why it is the
  faster option nearly everywhere. Runs indefinitely.
- **ram** buffers frames in memory and encodes nothing until the burst ends.
  Measured *slower* (16.3 vs 21.0 fps binned) because the copy then runs
  serially on the capture loop. Kept only for when zero disk I/O during the
  burst matters more than rate. Capacity is shown live from free RAM.

## Encode tab -- photos to video

Takes any folder of images: a rapid run, a timelapse, a `sess-*` folder, or one
of the old `runCam.sh` `s191`/`sauto` directories -- those are offered in the
dropdown automatically, and Browse takes anything else.

It reports how many images were found and how long the result will be before you
commit. Where an `index.jsonl` exists you can restrict to frames that passed the
quality gates; plain folders just use filename order, which the timestamp-first
naming makes chronological. Progress comes from ffmpeg itself, and encodes can be
cancelled.

## Focus

The lens is manual with no feedback, so the **Focus tab** collects the aids that
make that workable. Opening the tab switches the focus map and readout on
automatically, and turns them off when you leave (they cost a Laplacian pass per
frame).

- **Focus map** shades every area of the frame by how much detail the lens is
  actually resolving there, and rings the sharpest. This is the one that answers
  "which part of this is in focus" -- peaking alone lights up a sharp branch and
  noisy sky identically.
- **Sharpness readout** overlays a large number with a peak-hold bar on the
  preview, so you can watch it while turning the ring rather than looking away.
- **Focus peaking** and **clipping zebras** as before.

### Calibrating the blur gate

What counts as "sharp" depends on the lens, the aperture and the subject, so the
blur gate is referenced to a frame *you* call focused: focus carefully, then press
**Use current view as the sharp reference** in the Focus tab. The gate is set to
half that reading. Until you do, it sits at a floor low enough to reject nothing,
which is the safe default -- a mis-set blur gate that silently discards frames is
much worse than one that passes everything.

> The sharpness measure normalises by the *focus region's own* contrast, not the
> whole frame's. That distinction matters here: metering deliberately spans bright
> sky to shadowed treeline, so frame-wide dynamic range is near the full 0-255
> while the patch being measured may be flat.

**Open focus monitor** gives you a frameless always-on-top window showing:

- a **1:1 crop of native sensor pixels** (100/200/400% zoom) -- not the preview
  downscale
- **focus peaking** overlay
- a numeric focus score with a **peak-hold** marker: turn the ring until the
  number stops climbing; the yellow marker remembers your best so you can tell
  when you have gone past it

Full-frame copies are only taken while that window is open.

The main preview additionally offers clipping **zebras** (magenta) and the
metering zone boundary.

### Wallpaper monitor

If you want the desktop background to track the camera the way the old script
did:

```bash
birdshot-wallpaper            # follows latest.jpg, updates every 2 s
```

The old `pcmanfm --set-wallpaper latest.png` mostly did not refresh because
pcmanfm caches the wallpaper by path -- setting the same path twice is a no-op
even when the file changed. This alternates between two filenames to defeat that.
It shows the 320x240 preview though, so it is fine for "is the camera still
pointed at the tree" and useless for focus. Use the focus monitor for focus.

---

## Launchers and unattended operation

```bash
./sync.sh install-launchers      # desktop icons + Pi menu entries
./sync.sh install-autostart      # launch maximized at every login
./sync.sh remove-autostart
./sync.sh autowrite status       # is a marked stick present?
```

`install-launchers` puts three icons on the Pi's desktop and two entries under
Graphics in the menu:

| Icon | Launches |
|---|---|
| **birdshot** | normally, maximized |
| **birdshot (AUTO)** | `--auto` -- honours an `autowrite.yes` stick and starts shooting |
| **birdshot (Focus)** | straight to the Focus tab, for setting the lens |

They are marked executable and trusted, so PCManFM will not ask "this file is
not executable" on every launch.

The Pi already logs in automatically, so the autostart entry brings birdshot up
maximized with no interaction.

### autowrite.yes

Put a file called `autowrite.yes` in the root of a USB stick. On the next launch
birdshot finds it, points capture at that stick, starts shooting immediately and
copies frames across on a timer. Pull the stick out and it starts normally again.
An amber banner across the top of the window says what it decided, so an
unattended configuration is never invisible.

The file may be empty (defaults apply) or carry `key=value` lines:

```
mode=continuous        continuous | ram
res=1                  0 = 4056x3040, 1 = 2028x1520, 2 = 1332x990
count=0                frame limit, 0 = until stopped
start=yes              begin capturing on launch
interval=30            seconds between incremental copies
delete_after_copy=no   free the eMMC once a copy is verified
quality=92             JPEG quality
```

Unknown keys and malformed lines are reported in the log rather than silently
ignored -- a typo in an unattended config is otherwise invisible until you check
the card and find it empty.

```bash
./sync.sh autowrite enable /media/pi/ARCHIVE res=1 interval=15
./sync.sh autowrite disable /media/pi/ARCHIVE
```

### Shutdown

Unattended runs get killed rather than closed -- a power-down, a reboot, a
`pkill`. SIGTERM/SIGINT/SIGHUP are caught and routed through the normal shutdown,
which stops capture, drains any pending encodes, closes the session and waits for
the final copy to USB to finish. Without that the last stretch of a session would
be lost.

> **Watch the throughput.** The stick sustains about 12 MB/s. Rapid capture at
> 2028x1520 produces roughly 10 MB/s, so continuous copying only just keeps up
> and cannot catch up once behind. The status bar shows how many frames the stick
> is behind and turns amber past 200. For long unattended runs either drop to a
> smaller resolution, raise the interval, or accept that the copy completes at
> shutdown rather than during the run.

## Cascade tab -- group capture that clears itself

Frames are written into **groups** (numbered directories holding a bounded number
of frames). When a group is full it is *sealed*, and background workers copy it
down a chain of tiers, verify it, then free the source:

```
tmpfs 1.9 GB, 459 MB/s  ->  eMMC 16 GB, 60 MB/s  ->  USB 55 GB, 12 MB/s
```

Every tier clears itself once its contents are safely one level down, so **capture
runs for as long as the bottom tier has room, not the top one**. Videos and
assembled movies cascade too -- they are wrapped as single-item groups once
complete, so every output takes the same path down.

The RAM tier is optional: switched off, the chain becomes eMMC -> USB and behaves
identically, just without the top buffer. A slider sets how much of RAM the tmpfs
tier may use (20-80%), applied by remounting it.

> The practical ceiling is about 70%, not 100%. CMA -- the camera's contiguous DMA
> pool -- is carved from the same MemTotal, and a tmpfs large enough to crowd it
> makes the camera fail to allocate buffers. The slider shows the safe headroom
> live and warns past it rather than silently letting you reproduce that crash. Capture never
waits on migration -- the only coupling is sealing a group.

Measured: 400 frames at 2028x1520, captured at full rate while migrating
underneath, all 400 arriving in the archive with every file size verified and both
upper tiers left empty.

### What it does and does not buy you

- It does **not** make capture faster. The eMMC already writes at 60 MB/s against
  a peak capture demand of ~12 MB/s.
- It does **not** raise the sustained rate above the slowest tier. The upper tiers
  absorb bursts; over a long run the cascade shifts data only as fast as the
  bottom tier accepts it. The tab predicts the run length from your settings and
  says what the limit is.
- It **does** let a run outlive any single tier, and it keeps every tier from
  filling up without you watching it.

For an unbounded run put the Mac at the bottom -- gigabit is 110 MB/s, far above
what capture produces:

```bash
birdshot-cli cascade --tiers /dev/shm/birdshot,/home/pi/birdshot-data/cascade,user@mac:/Volumes/Birds
```

### Safety, because this deletes data

- A group is removed from a tier only after it has been copied to the next tier
  **and verified** -- every file present, every size identical. A truncated copy
  fails verification and nothing is deleted (there is a selftest for exactly this).
- The bottom tier never deletes anything unless **ring mode** is switched on. Ring
  mode drops the oldest groups to keep shooting forever; it is the only place
  birdshot deletes data that exists nowhere else, so it is off by default and
  labelled as such in the tab.
- Sealing is atomic and writer-counted: encoder threads writing into a group hold
  a reference, and sealing waits for them. Without that a frame could land in a
  group whose manifest was already written, leaving it unlisted and unverified.
- `index.jsonl` and `session.json` migrate with the groups. Otherwise the metrics
  index would be stranded on tmpfs and lost at reboot while the frames it
  describes sat safely on the stick.
- When the top tier fills, capture applies **backpressure** -- it pauses for the
  migrator rather than aborting the run. It only stops when there is genuinely
  nowhere left for data to go.
- Everything is resumable: groups are self-describing, so a restart picks up
  whatever was mid-cascade.

```bash
birdshot-cli cascade -n 500 -g 200               # 200 frames per group
birdshot-cli cascade --tiers /dev/shm/birdshot,/home/pi/birdshot-data/cascade --ring
```

## EXIF

Frames are tagged as a **preprocessing step** before movie assembly, never at
capture time -- rapid capture runs at up to 35 fps and everything needed is
already in `index.jsonl`, so there is no reason to spend the capture loop's
budget on it.

`exiftool` is not installed on this Pi and would be the wrong tool anyway: it
costs a process spawn per file (~50-100 ms), which over a few thousand frames is
minutes. **piexif** is present and injects the APP1 segment straight into the
JPEG byte stream -- no re-encode, so no quality loss. Measured 30 frames in
0.7 s, writing to the NTFS USB stick.

Tick *"Write EXIF into the source frames first"* in the Encode tab, or:

```bash
birdshot-cli exif rapid-2026073113580718            # tag in place
birdshot-cli assemble rapid-2026073113580718 --exif # tag, then encode
```

What gets written:

| Field | From |
|---|---|
| `DateTimeOriginal` + `SubSecTimeOriginal` | the capture instant, to the centisecond |
| `ExposureTime`, `ShutterSpeedValue` | measured shutter |
| `ISOSpeedRatings` | analogue gain x 100 |
| `Make` / `Model` / `Software` | Raspberry Pi / IMX477 HQ Camera / birdshot |
| `FNumber`, `FocalLength`, `LensModel` | settings -- the manual C-mount reports nothing |
| `UserComment` | birdshot's own scoring: verdict, meter, p50/p95, clipping, sharpness, contrast tiles |

The sub-second field is why the centisecond filenames matter: `2026073113580741.jpg`
becomes `13:58:07` + subsec `41`, so the precision survives into the metadata
instead of being rounded away. A selftest asserts the EXIF stamp and the filename
agree to the centisecond.

Tagging is idempotent and atomic (written to a sibling, then renamed), so it is
safe to re-run and safe to interrupt.

> With the cascade on, frames have usually migrated to the archive by the time
> you assemble. Sessions are looked up by bare name across every tier, so
> `birdshot-cli exif rapid-2026073113580718` finds it wherever it currently lives.

## Why the eMMC, and not a ramdisk

Worth stating because it is counter-intuitive: **a ramdisk does not make capture
faster here.** Measured on this board:

| Target | Write speed | Peak capture demand |
|---|---|---|
| tmpfs (`/dev/shm`) | 459 MB/s | |
| eMMC | 60-78 MB/s | **~12 MB/s** |
| USB stick (NTFS, USB 2.0) | ~12 MB/s | |

The eMMC is already five times faster than the fastest thing we can generate, so
it is nowhere near the bottleneck. The real limits are the CMA pool (see below)
and, for offload, the USB stick.

RAM staging is still available for two legitimate reasons -- avoiding eMMC write
wear, and eliminating disk jitter entirely:

```bash
./sync.sh ramdisk on      # capture to /dev/shm/birdshot, copy out every 10 s
./sync.sh ramdisk status
./sync.sh ramdisk off
```

It sets delete-after-copy, because otherwise 1.9 GB of RAM fills and capture
stops. That makes the offload target the constraint: over gigabit to the Mac it
keeps up, to the 12 MB/s USB stick it does not. And nothing on a ramdisk survives
a power cut.

## A note on CMA

Camera buffers come from the kernel's contiguous-memory pool, which is 512 MB
here and is **not** ordinary RAM -- it must be physically contiguous, so it runs
out long before free memory suggests. One 4056x3040 still configuration costs
333 MB of it.

This matters because reconfiguring the camera (a resolution change, or switching
to video) allocates the new buffers before the old ones are freed. birdshot
therefore closes and recreates the camera on every mode change, which costs about
a second and is only paid on an actual change. Buffer counts are also scaled by
frame size (4 at full resolution, 6 otherwise).

If you see `cma_alloc: alloc failed` in `dmesg`, that is what happened. It can be
raised with `cma=` in `/boot/cmdline.txt`, but the fix above means you should not
need to.

## Storage and offload

Capture writes to the eMMC. Finished sessions are handed to a background rsync
that trickles them to the USB stick at low priority, so it never steals CPU from
the encode threads. Nothing is deleted from the eMMC unless you enable
"Delete after a verified copy" in the Storage tab. Capture stops automatically
below the free-space floor (2 GB by default).

Your existing files on the stick are untouched -- offload writes under
`/media/pi/ARCHIVE/birdshot/`.

If you later want the stick faster, reformatting it from NTFS to ext4 would
roughly double it, but it is still on a USB 2.0 port and would remain slower than
the eMMC. Pulling to the Mac over gigabit is the faster path for bulk work.

---

## Timelapse

Capture at an interval (Timelapse tab, or `birdshot-cli timelapse`), then assemble at
60 fps. Assembly on the Pi is libx264 on four A72 cores and is slow for 12 MP
frames; `mac/assemble.sh` does the same job on the Mac with identical frame
selection, an order of magnitude faster.

---

## Module map

| File | |
|---|---|
| `birdshot/naming.py` | `s<N>`/`ms<N>`/`us<N>` folder naming, legacy-compatible |
| `birdshot/config.py` | persisted settings, calibration, resume state |
| `birdshot/analysis.py` | metering, quality gates, focus measures (numpy only, no OpenCV) |
| `birdshot/exposure.py` | EV-space PID, lux feed-forward, shutter/gain ladder |
| `birdshot/storage.py` | sessions, `index.jsonl`, background USB offload |
| `birdshot/camera.py` | the capture engine (own thread, event callbacks) |
| `birdshot/timelapse.py` | ffmpeg assembly and source selection |
| `birdshot/autostart.py` | `autowrite.yes` detection and parsing |
| `birdshot/cascade.py` | tiered storage: groups, migration, verification |
| `birdshot/exif.py` | EXIF tagging as a preprocessing step |
| `birdshot/gui/widgets.py` | accordion, fullscreen preview, blocking overlay |
| `birdshot/tone.py` | ISP tone curve (the HQ-cam gamma) |
| `birdshot/gui/` | PyQt5 front end, calibration wizard, focus monitor |
| `bin/birdshot-gui` | GUI launcher (`--auto`, `--tab`, maximized by default) |
| `bin/birdshot-cli` | headless control **and the selftest** |
| `bin/birdshot-wallpaper` | desktop wallpaper monitor |
| `sync.sh` | two-way source sync, deploy, remote control |
| `mac/` | photo pull and Mac-side movie assembly |

`birdshot-cli selftest` exercises naming, the gates, the exposure ladder, PID
convergence, storage layout, YUV conversion, the live camera, ffmpeg and Qt.
Run it after any change.

---

## License

MIT. See [LICENSE](LICENSE). Every source file carries an
`SPDX-License-Identifier: MIT` header.

The copyright line reads `Copyright (c) 2026 Paul` -- change it in `LICENSE` and
in the file headers if you want a different name:

```bash
grep -rl "Copyright (c) 2026 Paul" . | xargs sed -i '' 's/Copyright (c) 2026 Paul/Copyright (c) 2026 Your Name/'
```
