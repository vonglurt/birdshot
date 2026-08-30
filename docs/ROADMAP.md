<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Roadmap

How birdshot gets from the working prototype to the next version: what the
current tree *is*, how releases are named and shipped from here on, and the
backlog for the next release candidate.

---

## Where we are: the alpha prototype

The original birdshot is an **alpha prototype**. It works — it was deployed and
running on the target hardware — but it was last physically tested months ago
and the system is currently powered off. Until the Pi is plugged back in, the
honest designation for what exists is:

> **v1.0.0-rc1 · codename “Cow Tools” — alpha release candidate,
> proof-of-concept deployment.**
>
> Deployed on: Debian 11 (bullseye) · Raspberry Pi Compute Module 4 (4 GB) on
> a CM4 I/O breakout board · Raspberry Pi HQ Camera (IMX477, C/CS mount).

The tag `v1.0.0-rc1` already exists on this exact tree, signed and pushed. The
codename fits an alpha: a set of hand-made tools of no immediately obvious
purpose that nevertheless clearly took a great deal of effort. See
[BIRDS-VS-COWS.md](BIRDS-VS-COWS.md) for the required background reading.

## The release system

- **Versioning** — semver, string lives in `src/birdshot/__init__.py`.
  Candidates are `-rcN`; a candidate is promoted to a release only after a
  documented `birdshot-cli selftest` pass on real hardware at that tag.
- **Codenames** — each minor version gets one, drawn from the Larson corpus.
  `1.0.x` is **Cow Tools**. `1.1.x` is **The Chickens Are Restless**, which is
  also an accurate description of the backlog below.
- **Tags** — annotated, SSH-signed, message summarises what is deployed and
  what is unverified (the `v1.0.0-rc1` tag message is the template).
- **GitHub Releases** — every tag gets a Release on
  `github.com/vonglurt/birdshot` so the source is downloadable without git;
  `-rcN` tags are marked *pre-release*. The auto-generated tarball/zip is the
  artifact — the program deploys from source, so nothing is compiled for the
  release itself.
- **Gate** — `make check` (lint + sanitise + vendor-check) must pass before any
  tag, same as before any push.

### Promoting 1.0.0 (needs the Pi powered on)

The only work between rc1 and 1.0.0 final is physical verification: plug the
system in, run `make selftest`, take the screenshots the README and guide have
slots for, then tag `v1.0.0`. This can happen any time the Pi is on, and does
not block the 1.1 work below.

---

## Backlog for the next release candidate — v1.1.0-rc1 “The Chickens Are Restless”

### 1. Camera selection

Today the engine assumes the one IMX477. Next version:

- Enumerate available system cameras at startup (`Picamera2.global_camera_info()`
  on the Pi; AVFoundation device list on macOS).
- A **dropdown in the GUI** to pick the camera, a `--camera` flag for the CLI,
  and the choice persisted per-profile in `~/.config/birdshot`.

### 2. Cross-platform: run on the Mac, keep the Pi deploy

Two deployment stories, both first-class:

- **Remote (as today):** `sync.sh` over SSH to the Pi stays the deployment
  path for the capture appliance.
- **Local (new):** the same checkout runs directly on this Mac. That means
  splitting `CameraEngine` behind a small backend interface:
  `picamera2` backend (Pi), an OpenCV/AVFoundation backend (macOS, and
  incidentally generic V4L2 Linux), and a file/replay backend so development
  and tests never need hardware at all. Exposure control will be shallower off
  the Pi — that is acceptable; the Mac target is development and preview, the
  Pi is the instrument.

> The build, packaging and distribution side of this backlog now has its own
> step-by-step implementation plan in [PACKAGING.md](PACKAGING.md), including
> the copal flagship channel, licensing, versioning policy and the long-term
> maintenance structure. Items 3 and 4 below are tracked there in detail.

### 3. Install script and the copal system

birdshot becomes installable by the **copal** provisioning system (the Linux VM
checkout at `~/code/copal-alpine-linux` — planning notes have called it Arch;
the checkout says Alpine; confirm which before writing the manifest):

- copal's installer gains a step that `git clone`s (or falls back to the
  GitHub Release tarball over HTTPS) into `~/code/` on the installed system —
  birdshot among the cloned projects.
- birdshot itself ships an idempotent `install.sh` that copal (or a human) can
  run from the checkout: detect platform, install dependencies, install the
  desktop entry where that applies.

### 4. `birdshot-cli doctor` — self-configuration and install checklist

A tool that tells a developer how the install is actually doing, instead of
finding out at capture time:

- Detects: platform, Python version, importable dependencies, external
  binaries (ffmpeg, rsync, exiftool), cameras found, storage tiers present and
  writable, config validity.
- Prints a pass/warn/fail checklist; `doctor --write-config` seeds a sane
  config for what it found. `install.sh` ends by running it.

### 5. Performance: stop eating the machine

The known symptom: **100 % CPU on the Pi, the mouse freezes** and recovers.
The system must stay responsive while capturing.

- First, measure: profile the GUI preview and analysis loops before changing
  anything.
- Likely wins, in order: drop preview decode resolution and rate (analysis
  does not need the display rate), vectorise the per-frame scoring that is
  still per-pixel Python, cap worker threads to cores − 1, and `nice` the
  offload/timelapse workers so the compositor keeps breathing.
- **On changing languages:** not for 1.1. `picamera2` is the reason the
  exposure control works, and it is Python. If profiling shows a hot kernel
  that numpy/OpenCV cannot absorb, the escape hatch is a small native module
  (Rust via PyO3, or C) for that kernel only — not a rewrite.

### 6. New mode: **Capture Bird Flight**

A monitoring mode that watches the sky and takes the photo itself when the
photo is worth taking:

- **Detect** — motion gate first (cheap frame differencing), then subject
  check: a discrete dark region against a sky-classified background (blue or
  white, by hue/luma), i.e. a bird where a bird would be.
- **Judge** — focus score on the *subject boundary* (Laplacian/Tenengrad on
  the edge band, not the whole frame — sky is always “sharp”), plus a
  composition gate: minimum subject size, subject inside the frame margins,
  minimum sky fraction.
- **Take** — when detect and judge both pass: full-resolution burst, then a
  cooldown so one crow does not fill the eMMC. Frames are saved under the
  existing storage cascade and EXIF-tagged with the trigger scores.
- **Settings** — a capture-settings panel in the GUI (and config keys) for
  both the capture side (burst length, cooldown, resolution) and the auto-take
  side (motion sensitivity, sky hue range, minimum sharpness, minimum subject
  size). Saved per-profile like everything else.

### Sequencing

1. Backend split + camera enumeration (unblocks Mac work, needs no Pi).
2. Doctor + install.sh + copal hook (makes every later deploy checkable).
3. Performance pass (on-Pi; first session after power-up, right after the
   1.0.0 selftest).
4. Capture Bird Flight, built on the backend split so the detector can be
   developed on the Mac against replayed footage and deployed to the Pi.

Ship `v1.1.0-rc1` when 1–3 hold on both platforms; Bird Flight rides in 1.1 if
it is ready, or becomes the headline of `1.2.0` (codename to be argued about).
