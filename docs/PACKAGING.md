<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Packaging, build and distribution — the implementation plan

This is the step-by-step plan for taking birdshot from "a checkout that is
rsynced to one Pi" to a program with **many distribution channels**, built by
**one normal build system from beginning to end**, on Mac, PC, Linux, BSD —
Debian, Ubuntu, RPM, Flatpak and the rest — while keeping the requirements as
easy as they are today.

Boxes marked ☑ are implemented in this tree; ☐ is scheduled work. The plan is
written to be executed top to bottom: every later phase consumes only what the
earlier phases produced.

First targets, in order:

1. **Debian on Raspberry Pi OS** — where the instrument already runs.
2. **Copal** (an Alpine Linux flavour) — where birdshot is the preferred
   camera software for the OS desktop. Copal's auto-install is the flagship
   channel, and the one we tune for best performance (Phase 6).
3. Everything else, as thin adapters over the same build.

---

## Phase 0 — ground rules

These decide every choice below; when a channel fights them, the channel loses.

- **0.1 One source of truth.** Version, dependencies, entry points and
  metadata live in exactly one place each. The version is
  `src/birdshot/__init__.py:__version__`; `pyproject.toml` reads it; every
  package format reads `pyproject.toml`. No channel carries its own copy of a
  dependency list.
- **0.2 The sdist is the universal artifact.** birdshot is pure Python. One
  `python -m build` produces a source distribution and a wheel, and *every*
  channel — apk, deb, rpm, Flatpak, Homebrew, ports, pip — is a thin adapter
  that consumes that sdist (or the git tag it was built from). Nothing is
  compiled per-platform by us; native speed comes from numpy and the camera
  stack, which the distros already build well.
- **0.3 Keep the easy requires.** Hard requirement: Python ≥ 3.9 and numpy.
  Everything else — picamera2, simplejpeg, piexif, PyQt5 — is an *extra*, and
  the program already degrades politely when one is missing (exiftool fallback
  for EXIF, headless CLI without Qt). A channel that can't provide an extra
  ships without it rather than blocking.
- **0.4 The checkout keeps working.** `bin/birdshot-cli` from a git clone,
  with no install step at all, must behave identically to an installed
  package forever. `sync.sh` deploys checkouts; copal deploys packages; both
  run the same modules.
- **0.5 `make check` gates everything.** Lint + sanitise + vendor-check pass
  before any tag, and the release pipeline runs it again in CI.

## Phase 1 — the canonical build (pyproject.toml) ☑

The "normal build system from beginning to end" is the standard Python one:
`pyproject.toml` + setuptools + `python -m build`. Every downstream format has
first-class tooling for exactly this shape.

- **1.1 ☑** Add `pyproject.toml`: project metadata, `requires-python >= 3.9`
  (Debian 11 bullseye on the Pi is the oldest floor we honour), `numpy` as the
  only hard dependency, and dynamic version read from
  `birdshot.__version__`.
- **1.2 ☑** Express the soft dependencies as extras:
  `birdshot[pi]` (picamera2 + simplejpeg), `[exif]` (piexif), `[gui]` (PyQt5),
  `[full]` (all of them). `pip install birdshot` stays feather-light.
- **1.3 ☑** `make dist` target: `python -m build` → `dist/*.tar.gz` +
  `dist/*.whl`. This pair is what CI attaches to every GitHub Release and what
  every package below consumes.
- **1.4 ☑** Bump the working version to `1.1.0.dev0`. `main` is the 1.1 line
  (“The Chickens Are Restless”); 1.0.0 final, when the Pi is powered on and
  selftested, is cut from the `v1.0.0-rc1` tag on a `release/1.0` branch
  (Phase 7).

## Phase 2 — entry points move into the package ☑

The launchers were standalone scripts in `bin/` with a `sys.path` hack. Wheels
and distro packages want importable modules with named mains.

- **2.1 ☑** `bin/birdshot-cli` → `src/birdshot/cli.py`,
  `bin/birdshot-gui` → `src/birdshot/gui/app.py`,
  `bin/birdshot-wallpaper` → `src/birdshot/wallpaper.py`. Verbatim moves; the
  GUI launcher lands inside `gui/` so the PyQt5 import boundary audited by
  `make deps` is unchanged.
- **2.2 ☑** `bin/*` become six-line wrappers that add `src/` to the path and
  call the package main — rule 0.4, the checkout keeps working, `sync.sh`
  needs no change.
- **2.3 ☑** `[project.scripts]` maps `birdshot-cli`, `birdshot-gui`,
  `birdshot-wallpaper` to those mains, so every install method produces the
  same three commands on `$PATH`.

## Phase 3 — self-configuration: doctor and the installer ☑

Developers (and copal's auto-install) need to *see* how an install is doing
rather than find out at capture time.

- **3.1 ☑** `birdshot-cli doctor`: a pass/warn/fail checklist — platform and
  libc (it knows Alpine/musl when it sees it), Python version, each required
  and optional module, external binaries (ffmpeg, rsync, exiftool), cameras
  found, storage roots present/writable with free space, config validity.
  `--json` for machines (copal's installer consumes this), `--write-config`
  to persist the validated config. Exit code 0 unless something FAILs.
- **3.2 ☑** `install.sh` at the repo root: detects apk / apt / dnf / brew,
  installs the system dependencies with the native package manager (distro
  numpy and Qt, never pip-compiled ones on the appliance), pip-installs
  birdshot itself (`--dev` for editable), and finishes by running doctor so
  the install ends with its own report.
- **3.3 ☐** copal integration: copal's provisioning calls
  `install.sh --json-report` and archives the doctor output with the image
  build (see Phase 6, and `docs/ROADMAP.md` for the `~/code/` clone step).

## Phase 4 — the channels

Each channel is a directory under `packaging/`, kept deliberately thin
(rule 0.2). Scaffolds marked ☑ are complete files that build the current tree;
they graduate to *maintained channel* when a real device installs from them in
CI or by hand.

- **4.1 Alpine / Copal — the flagship ☑ scaffold.**
  `packaging/alpine/APKBUILD`: builds the wheel with gpep517 from the GitHub
  tag tarball, installs the three commands, ships the desktop entry
  (birdshot is the Copal desktop's preferred camera app), and splits
  ahead-of-time-compiled bytecode into the standard `-pyc` subpackage so cold
  start on the appliance never pays the compile (Phase 6 owns the rest of the
  performance story). PyQt5's apk name is pinned by copal's own aports tree.
- **4.2 Debian / Raspberry Pi OS / Ubuntu ☑ scaffold.**
  `packaging/debian/`: `dh` + pybuild from the same sdist —
  `debian/control`, `rules`, `copyright`, `changelog`. Targets bookworm's
  `pybuild-plugin-pyproject`; on the bullseye Pi the checkout + `sync.sh`
  path remains the supported route (0.4), so we never need to backport
  tooling. Ubuntu consumes the identical source package; a PPA is the
  low-ceremony distribution point until/unless Debian proper wants it.
- **4.3 RPM (Fedora / openSUSE) ☑ scaffold.**
  `packaging/rpm/birdshot.spec` using the `%pyproject_wheel` /
  `%pyproject_install` macros. COPR is the distribution point, same role as
  the PPA.
- **4.4 Flatpak ☑ scaffold.** `packaging/flatpak/org.birdshot.birdshot.yml`:
  KDE runtime for Qt, pip modules for numpy/PyQt5, `birdshot-gui` as the app
  command. This is the "any desktop Linux, no root" channel; Flathub
  submission is ☐ and waits until the GUI runs without the camera present
  (the cross-platform backend split in `docs/ROADMAP.md`).
- **4.5 macOS ☑ scaffold.** `packaging/homebrew/birdshot.rb`, a formula over
  the release tarball in a private tap (`brew tap vonglurt/birdshot`). Mac is
  a development and darkroom machine — assemble, EXIF, sessions — until the
  AVFoundation backend lands; the formula ships now because those subcommands
  already work anywhere.
- **4.6 BSD ☑ skeleton.** `packaging/freebsd/`: a ports skeleton
  (`USES=python`, distfile from the GitHub tag). Untested until a FreeBSD
  environment exists; the port exists so the shape is settled and a BSD user
  has somewhere to start.
- **4.7 PC / Windows ☐.** `pipx install birdshot` gives the CLI (sessions,
  assemble, EXIF) today; there is no camera backend and no packaged channel
  until one exists. Honest support level: "tools run, camera does not."

## Phase 5 — CI and the release pipeline ☑

- **5.1 ☑** `.github/workflows/release.yml`: on every `v*` tag — run
  `make check`, `python -m build`, create the GitHub Release if the tag
  arrived without one (pre-release for `-rc`/`dev` tags), attach the sdist
  and wheel. The Release page is the canonical download point every channel's
  `source=` URL points at.
- **5.2 ☐** Extend the workflow with a channel-smoke job: build the apk in an
  Alpine container and the deb in a Debian container from the just-built
  sdist, so a tag that breaks a channel fails loudly on tag day.
- **5.3 ☐** PyPI publication (trusted publishing from the same workflow).
  This unlocks `pip`/`pipx` everywhere without our infrastructure; the name
  `birdshot` was chosen partly because it is free there.

## Phase 6 — Copal: auto-install and best performance ☐

Copal is the flavour of Alpine where birdshot is the desktop's camera
software, so the apk must land *configured and fast*, not merely installed.

- **6.1** Auto-install hook: copal's image build installs the apk, then runs
  `birdshot-cli doctor --json --write-config` as the target user; a non-zero
  doctor fails the image build. The install is proven before a user boots it.
- **6.2** Bytecode ahead-of-time: the `-pyc` subpackage (4.1) is in the
  desktop image by default — first launch does no compiling on musl's slower
  cold path.
- **6.3** musl-aware dependencies: numpy and Qt come from copal's apk repo
  (native builds), never from pip wheels (manylinux wheels target glibc;
  musllinux wheels exist but the distro build is the one copal has already
  optimised). `install.sh` already follows this rule.
- **6.4** Runtime budget on the appliance: the responsiveness work from
  `docs/ROADMAP.md` (preview decode budget, worker caps, niced offload) is
  *validated on copal first* — the 100 % CPU / frozen-mouse symptom is
  measured there, and the capture defaults the apk ships are the ones that
  keep the copal desktop responsive.
- **6.5** Desktop integration: the `.desktop` entry (shipped ☑) plus copal's
  default-application registration so "camera" on the Copal desktop opens
  birdshot.

## Phase 7 — versioning policy (compatible with what is common)

- **7.1** Semantic versioning, PEP 440-compatible spellings
  (`1.1.0rc1` == tag `v1.1.0-rc1`; distro spellings `1.1.0_rc1` apk,
  `1.1.0~rc1` deb/rpm sort correctly and map mechanically).
- **7.2** `main` is the next minor, always suffixed `.dev0`. A release is:
  branch `release/X.Y` → tag `vX.Y.0-rc1` → on-hardware selftest → tag
  `vX.Y.0`. Fixes cherry-pick to the release branch and tag `X.Y.Z`.
- **7.3** Support window: the current minor gets fixes; the previous minor
  gets data-loss and security fixes only; older, nothing. One deployed
  instrument and a small team — promising more would be fiction.
- **7.4** Compatibility contract: config keys, session/index layout and the
  shutter-directory naming are covered by semver — breaking any of them is a
  major version, because captures outlive software.

## Phase 8 — licensing (compatible with what is common)

- **8.1** Core stays **MIT** — every file already carries an SPDX header, and
  `make check` keeps it that way. MIT is why every channel above can ship
  birdshot without legal review.
- **8.2** The one boundary that matters: **PyQt5 is GPLv3**. Our source is
  MIT, but any *distribution that bundles PyQt5 with birdshot* (Flatpak most
  of all) conveys the combined work under GPLv3's terms — which MIT satisfies,
  since MIT is GPL-compatible. Policy, already enforced by `make deps`: PyQt5
  imports live only under `src/birdshot/gui/`, so the CLI/engine remain
  cleanly MIT-only and a GUI-less package has no GPL surface at all. If a
  channel ever needs a GPL-free GUI, the escape hatch is PySide6 (LGPL), not
  relicensing.
- **8.3** Vendored code, if any ever appears, goes through `vendor/` with
  SOURCE/LICENSE/MANIFEST as `make vendor-check` already demands;
  `THIRD-PARTY.md` stays the ledger of everything we depend on.

## Phase 9 — organizational structure for long-term maintenance

Structure for a project that must outlive its author's spare time:

- **9.1 Roles, not people.** *Upstream maintainer* (reviews, tags, owns
  `main`), *channel maintainers* (own one `packaging/` directory each — the
  natural first contribution for a distro person; copal's maintainer owns
  `packaging/alpine/`), *hardware testers* (run `selftest` on real cameras
  and sign off release candidates). One person may hold all roles today; the
  *boundaries* exist now so handing one off never requires restructuring.
- **9.2 Repository layout.** One upstream repo; `packaging/` lives in-tree so
  a tag pins the exact packaging that shipped it. Distro-side forks (aports,
  debian salsa) pull from tags, never from `main`.
- **9.3 Decision record.** Design decisions with long shadows are already
  written down in the README's design sections and `docs/lab-reports/`; that
  habit is the succession plan. A maintainer who disappears leaves behind the
  *why*, not just the what.
- **9.4 Bus-factor mechanics.** Signed tags with a published key policy; CI
  releases so no laptop is load-bearing; `CONTRIBUTING.md` states the gate
  (`make check`, selftest for camera-touching changes); the GitHub org
  (rather than a personal account) is ☐ and becomes worth it at the second
  regular contributor.
- **9.5 Issue hygiene as capacity planning.** Labels per channel
  (`channel:alpine`, `channel:debian`, …) so a failing channel is visibly one
  maintainer's, and an unstaffed channel is demoted to "community" in the
  README's support matrix instead of silently rotting.

## Phase 10 — distribution channels and rollout strategy

| Channel | Consumes | Who it serves | Status |
|---|---|---|---|
| git checkout + `sync.sh` | the repo | the deployed Pi, developers | **shipping** |
| GitHub Releases (sdist/wheel/tarball) | CI build | everyone below | **shipping** |
| Copal apk (auto-install) | tag tarball | the Copal desktop — flagship | scaffold ☑, Phase 6 ☐ |
| Debian/RasPiOS/Ubuntu deb | sdist | the instrument's own OS, PPA users | scaffold ☑ |
| pip / pipx (PyPI) | sdist+wheel | any Python user, Windows CLI | ☐ (5.3) |
| RPM via COPR | sdist | Fedora/openSUSE | scaffold ☑ |
| Flatpak | sdist | any desktop Linux, sandboxed | scaffold ☑, Flathub ☐ |
| Homebrew tap | tag tarball | the Mac darkroom | scaffold ☑ |
| FreeBSD port | tag tarball | BSD | skeleton ☑ |

Rollout order matches risk: channels serving the two named targets (Pi OS,
Copal) harden first; broad channels (PyPI, Flathub) open only after the
cross-platform backend split makes a camera-less install genuinely useful,
so no channel's first impression is a program that can't see a camera.
