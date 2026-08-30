<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
<!-- birdshot-lint: allow-legacy-name — this file records the rename -->
# Changelog

Notable changes to birdshot. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). The version string lives in
`src/birdshot/__init__.py` as `__version__`.

## [Unreleased]

- **Settings profiles** — save a whole setup (camera, exposure, gates,
  mode — everything but the machine paths) under a name and activate it in
  one gesture: the profile row in Bench's header (save / new... / del,
  picking one applies it, rebuilding the engine if it names a different
  camera), `birdshot-cli profiles list|save|use|show|delete`, and
  `birdshot-cli --profile <name> <command>` for a one-shot headless run.
  Files live next to settings.json under `profiles/`; machine paths
  (data_root, usb_root, cascade tiers) never ride a profile, so a profile
  can move between installs without pointing capture at a missing disk.
  Covered by selftest.
- **Webcam captures at native resolution** — the opencv backend used to
  save the 640x480 analysis frame; it now keeps the camera's delivered
  frame for saves and letterboxes a copy for analysis. What to ask the
  device for is the new "Webcam capture" setting (Stills section, default
  "Best the camera offers" — devices negotiate down from 1920x1080), with
  the negotiated size shown live ("Delivering 1280x720") and logged.
- **One window, four faces** — the GUI grew a face switcher (title bar, or
  Ctrl+1..4) over the same engine and settings.json, matching the audiences
  the packaging plan ships to: **Camera** (a plain camera app — preview,
  shutter, mode strip, camera picker; what the copal desktop boots),
  **Field** (the instrument outdoors: huge START/STOP, storage headroom,
  outdoor controls, and the Bird Flight **gate ladder** — every auto-take
  gate's live value against its threshold, so holding fire is readable),
  **Bench** (the classic dense window), **Library** (the darkroom:
  sessions, verdict badges, bird takes with the trigger that fired them,
  frame detail with use-as-sharpness-reference/open/delete, and the encode
  panel's new home). `ui_face: auto` resolves per install — Pi→field,
  checkout→bench, Alpine/copal→camera, Mac install→library; `--face`
  overrides. Fusion now runs the dark palette the custom controls always
  used.
- **Bench re-scoped: Shoot / Scene / Machine** — a setting lives with what
  it tunes: per-mode sections in Shoot, the image science in Scene,
  this-install concerns in Machine, which gained **Install health** (the
  doctor checklist in-GUI, plus a status-bar chip) and **Identity** (the
  `exif_*` keys' first GUI). The old tab names still work as `--tab`
  aliases; Process's encode panel lives in the Library face.
- **Capability gating** — every engine declares what it can do
  (`CAPABILITIES` in `birdshot/backends`); the GUI greys out what the
  selected camera cannot run, with the reason, instead of offering
  controls that could only return error events. Modes grey on the dial,
  whole sections gate (Rapid/Video/Cascade off-Pi, Bird Flight on the Pi
  until its engine wiring lands), exposure groups gate on webcams that own
  their own exposure, the tone curve on anything without the Pi ISP.
- **Settings search and provenance** — a find-a-setting box jumps to any
  of the ~90 keys (tab selected, section opened, row flashed); labels of
  values changed from the defaults turn amber with the default in the
  tooltip, the rail footer counts the drift, and `reset...` restores it.
- **Bird Flight: triggers recorded, subject box live** — every frame a
  take fires now carries its trigger sighting in `index.jsonl` (what the
  EXIF chain and the Library's "trigger that fired this take" read), and
  the detector's subject box is drawn on the preview at display rate,
  green through a take.
- **Replay backend** — `--backend replay` / the camera picker's "Replay
  footage..." entry play a folder of stills (or a video file, with OpenCV)
  through the real analysis, session and Bird Flight pipeline. Picking it
  in the GUI asks for the folder (`replay_path`). This is how `bf_*` gates
  get tuned against real birds before the Pi engine wiring lands.
- **Selftest honest off the instrument** — a new SKIP outcome for checks
  whose subject this machine cannot run (piexif absent; the ISP tuning
  file off-Pi, where `build_tuning()` correctly declines and the test now
  asserts exactly that). Off-Pi selftest is green: 18 PASS, 1 SKIP.
- **Capture Bird Flight landed** (off-Pi) — a new mode that watches the sky
  and fires a burst on its own when a dark subject is surrounded by sky,
  sharp along its boundary, inside the frame margins, and moving. The
  detector (`birdflight.py`, pure numpy) reports *why* it holds fire; the
  GUI's Bird Flight section exposes capture settings (burst, cooldown, take
  limit) and every auto-take gate; `birdshot-cli birdflight` runs it
  headless; frames land in `bird-<timestamp>` sessions. Gates are covered
  by selftest. Not yet wired into the Pi `CameraEngine`.
- **Camera selection landed** — a camera dropdown (+ rescan) at the top of
  the Shoot tab: Pi cameras, webcams by name (new **opencv backend**:
  AVFoundation on macOS, V4L2 on Linux — the device owns exposure, our
  gates still judge the frames), and the synthetic demo scene, switchable
  live; the choice persists as `backend`/`camera_index`. CLI grew
  `--backend`/`--camera`; the real engine honours `camera_index` too.
  macOS camera consent is requested on the main thread (`backends.warm_up`)
  so the engine thread never trips over the permission dialog. `auto` still
  never opens a webcam uninvited — a webcam is only used when picked.
- **The backend split began** — new `birdshot.backends` package: the engine
  protocol has a factory (`create_engine`) and camera enumeration
  (`list_cameras`); `camera.py` guards its picamera2/libcamera imports and
  stays the real backend; a new **synthetic backend** (a generated sky with a
  flapping bird, exposure-responsive, run through the real analysis gates and
  real AE controller) drives the GUI and CLI off the Pi. The GUI now runs on
  macOS: `make run` opens it against the synthetic sky. New config key
  `backend` (`auto`/`picamera2`/`synthetic`), new `birdshot-gui --backend`
  and `--screenshot` flags; `doctor` and `info` report through the backend
  layer.
- **The build went standard** (`docs/PACKAGING.md` is the full plan):
  `pyproject.toml` is the one source of truth; `python -m build` (or
  `make dist`) produces the sdist + wheel every distribution channel
  consumes. Version bumped to `1.1.0.dev0` — `main` is now the 1.1 line.
- The launchers moved into the package (`birdshot.cli`, `birdshot.gui.app`,
  `birdshot.wallpaper`); `bin/*` are thin wrappers, so a bare checkout and
  `sync.sh` behave exactly as before.
- New `birdshot-cli doctor`: a pass/warn/fail install checklist (platform,
  deps, cameras, storage, config) with `--json` for installers and
  `--write-config`. New `install.sh`: detects apk/apt/dnf/brew, installs
  distro dependencies, pip-installs birdshot, ends by running doctor.
- New `packaging/`: Alpine APKBUILD (the copal flagship channel), Debian
  dh+pybuild, RPM spec, Flatpak manifest, Homebrew formula, FreeBSD port
  skeleton, and a shared desktop entry.
- New `.github/workflows/release.yml`: every `v*` tag re-runs `make check`,
  builds the artifacts and attaches them to the GitHub Release.

- Adopted a release system: semver + a codename per minor version, signed
  tags, a GitHub Release (pre-release for `-rcN`) per tag. `1.0.x` is
  retroactively codenamed **Cow Tools** and designated the alpha prototype /
  proof-of-concept deployment (Debian 11, CM4 on the I/O breakout board, HQ
  Camera). `1.1.x` will be **The Chickens Are Restless**.
- Added `docs/ROADMAP.md` — the backlog for `v1.1.0-rc1`: camera selection
  dropdown, cross-platform capture backends (run on the Mac, deploy to the
  Pi), copal-driven install into `~/code/`, `birdshot-cli doctor`, the
  responsiveness/CPU work, and the **Capture Bird Flight** auto-take mode.
- Added `docs/BIRDS-VS-COWS.md`. It is release-critical. Do not ask why.

## [1.0.0-rc1] — 2026-08-23 — first public candidate

The project became public. It had been developed privately since 2026-07-31
across five commits, all of which were rewritten for this release (see
*Rewritten history* below).

**This is a release candidate, not a release.** The software is deployed and
running as described — the capture engine, auto-exposure, quality gates,
storage cascade, EXIF tagging and timelapse assembly all work on the target
hardware. What is missing is *formal* physical testing: a documented pass of
`birdshot-cli selftest` against the camera at this exact tree, and the
screenshots the landing page and guide have slots for. Both need the Pi powered
on. Until then this is `rc1`.

### Renamed

- The project is **birdshot**, formerly *picam*. The old name described the
  hardware, collided with dozens of repositories and a PyPI package, and could
  not be branded. Applied to every tracked file **and to every commit in
  history**, so no commit in the published record carries the old name.
- `picam/` → `src/birdshot/` — a `src/` layout, matching the other projects in
  this family, and the reason `bin/*` now extends `sys.path` with `src` rather
  than the repository root.
- `bin/picam-{cli,gui,wallpaper}` → `bin/birdshot-{cli,gui,wallpaper}`.
- `GUIDE.md` → `docs/GUIDE.md`.
- `~/picam-data` → `~/birdshot-data`; `~/.config/picam` → `~/.config/birdshot`;
  `picam.desktop` → `birdshot.desktop`; `PICAM_PROFILE` → `BIRDSHOT_PROFILE`.
- `CameraEngine._picam` → `._cam`. The attribute holds a `Picamera2` instance,
  so `_birdshot` would have been actively misleading; `_cam` removes the old
  stem and reads truer than either.
- `picamera2` — the upstream dependency — is untouched everywhere. The rename
  was applied on a word boundary specifically so it would survive.

### Sanitised

Every one of these was in the tree *and in history*, and each was rewritten
across all five commits rather than fixed going forward:

- A private LAN address (`192.168.x.x`) → the `raspberrypi.local` default,
  still overridable by `PI_HOST`.
- `/home/<user>/…` → `/home/pi/…`, the account the software actually deploys to.
- A specific USB stick's volume label → `/media/pi/ARCHIVE`.
- `Copyright (c) 2026 <email>` → `Copyright (c) 2026 Paul Richeson`, in the
  `LICENSE` and in all 27 SPDX file headers.

Verified: no blob in any commit contains a private address, a personal home or
media path, key material, or the old project name. `make audit-history` re-runs
that check on demand.

### Rewritten history

All five commits were rebuilt with `git-filter-repo` and re-signed. Author and
committer are now `Paul Richeson <paulr@sdf.org>` throughout; author dates are
unchanged; every commit verifies with a good SSH signature. Commit hashes are
therefore all new — the pre-rewrite history was never published, so nothing
depended on the old ones.

The history contains **only** `.py`, `.sh` and `.md` files plus `LICENSE` and
`.gitignore`. No logs, no captured frames, no binaries, no build output have
ever been committed.

### Added

- **`vendor/`** — the documented home for external licensed items, empty of
  code by design. `vendor/README.md` gives the vendoring procedure
  (`SOURCE.txt`, verbatim `LICENSE`, `MANIFEST.sha256`) and the rule that
  foreign code lives there or nowhere.
- **`THIRD-PARTY.md`** — every runtime dependency and external binary, with
  licence and how it is reached. Records the finding that **PyQt5 is GPL-3.0 or
  commercial**, what that governs, and the mitigation already present in the
  architecture: the GPL surface is confined to `src/birdshot/gui/`, so the
  headless path is copyleft-free.
- **`CONTRIBUTING.md`** — signed commits, the `vendor/` rule, the sanitisation
  hook, the `make check` gate, and the honest limits of testing without a CM4.
- **`SECURITY.md`** — the real exposure is publishing photographs of where you
  live, not a defect in the code; the repository defends against that
  structurally.
- **`.githooks/pre-commit`** — refuses staged content carrying a private IP, a
  non-`pi` home path, a personal `/media/` mount, key material, the old project
  name, or a capture/log file. Install with `make hooks`. Bypass is explicit and
  greppable: `BIRDSHOT_ALLOW_UNSANITISED=1`.
- **`Makefile`** — `check`, `lint`, `sanitise`, `audit-history`, `deps`,
  `vendor-check`, `hooks`, `selftest`, `info`.
- **`index.html`, `CNAME`, `.nojekyll`, `.github/workflows/pages.yml`** — the
  birdshot.org landing page and its deployment.
- **`assets/screenshots/`** — slots and a shot list for captures that require
  the powered-on system. Deliberately empty; see its README.
- **`docs/lab-reports/lab-report-birdshot-publication.md`** — what the
  publication audit found, what was done about it, and what remains.
- **`CHANGELOG.md`** — this file.

### Fixed

- `sync.sh` ran four inline `python3 -c` snippets that imported `birdshot` from
  the deployment root. The `src/` layout broke every one of them. Fixed as
  `PYTHONPATH=src python3 -c`, and applied across all five commits so no commit
  in the published history carries the break.

### Known gaps

Tracked as next steps in the lab report:

- No screenshots. Requires the powered-on Pi.
- No recorded selftest pass at this tree. Requires the camera.
- No CI. `make check` is host-runnable and should run on push; the hardware
  selftest cannot run in Actions and never will.
- `birdshot.org` DNS is not yet cut over; the registrar TXT verification record
  is pending.
- No packaging (`pyproject.toml`). Deployment is `sync.sh` + `pip3` on the Pi.
