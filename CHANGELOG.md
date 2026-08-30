<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
<!-- birdshot-lint: allow-legacy-name — this file records the rename -->
# Changelog

Notable changes to birdshot. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). The version string lives in
`src/birdshot/__init__.py` as `__version__`.

## [Unreleased]

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
