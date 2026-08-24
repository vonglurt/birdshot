<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Third-party software

birdshot is MIT (see [LICENSE](LICENSE)). It carries **no third-party code in
its own tree** — every file under `src/`, `bin/`, `mac/` and `docs/` is original
work by Paul Richeson, and `vendor/` is empty by design (see
[vendor/README.md](vendor/README.md)).

What follows is therefore not a list of things birdshot contains. It is a list
of things birdshot *reaches for at runtime*, recorded because the distinction
between the two is the whole of the licence question, and because one of the
entries below is not permissive.

---

## The finding that matters: PyQt5 is GPL

**PyQt5 is offered by Riverbank Computing under GPL v3 or a paid commercial
licence. There is no permissive option.** birdshot's GUI imports it. An MIT
project that hands a user a GUI built on GPL bindings is the one real licence
hazard in this repository, and it is worth stating precisely rather than
waving at.

**What is and is not affected.**

birdshot's own source files remain MIT. You may take any file in this
repository under the MIT terms; that grant is Paul Richeson's to make and
nothing about PyQt5 withdraws it. What the GPL governs is *conveying a
combined work* — shipping birdshot and PyQt5 together as one artefact.

| You are… | Result |
|---|---|
| Cloning this repo and running it, having installed PyQt5 yourself | Fine. Nothing is conveyed to you but MIT source; you obtained PyQt5 from Riverbank under their terms. |
| Redistributing this repo's source | Fine. No PyQt5 code travels with it. |
| Shipping an SD-card image, `.deb`, container or PyInstaller bundle containing **both** birdshot and PyQt5 | **The combined artefact must be GPL-3.0.** birdshot's files stay MIT inside it; the aggregate cannot be offered under MIT alone. |
| Selling such a bundle without a Riverbank commercial licence | Not permitted by Riverbank's terms. |

This is the ordinary "permissive application, copyleft toolkit" position and it
is not unique to birdshot; it is recorded here because a maintainer who does not
know it will eventually publish an image and get it wrong.

**The mitigation is already in the architecture.** PyQt5 is confined to
`src/birdshot/gui/` — six files. Verified, not assumed:

```sh
grep -rl PyQt5 --include='*.py' src bin
```

returns `src/birdshot/gui/*` plus two false hits: `src/birdshot/__init__.py`
mentions PyQt5 in a docstring, and `bin/birdshot-cli` imports it inside
`t_qt()`, a function body that only `birdshot-cli selftest` ever runs.
Importing the CLI does not import Qt.

So the entire headless path — capture, metering, auto-exposure, the storage
cascade, EXIF, timelapse — **touches no GPL code at all**. A deployment that
runs `birdshot-cli` and never opens the GUI is MIT plus permissive
dependencies, full stop, and can be bundled and redistributed freely. If you
need that guarantee, run headless and do not install PyQt5; every module
outside `gui/` imports cleanly without it.

---

## Runtime dependencies

Python packages, installed by the operator with `pip3`/`apt`. None is vendored.

| Package | Licence | How birdshot uses it | Copyleft? |
|---|---|---|---|
| **PyQt5** | GPL-3.0 **or** Riverbank Commercial | the on-Pi GUI — `src/birdshot/gui/` only | **yes — see above** |
| **picamera2** | BSD-2-Clause | the capture engine; the sole camera interface | no |
| **numpy** | BSD-3-Clause | metering, histograms, focus measures, tone curve | no |
| **simplejpeg** | MIT (wraps libjpeg-turbo: IJG / BSD-3-Clause / zlib) | JPEG encode on the capture path | no |
| **piexif** | MIT | injects the EXIF APP1 segment without re-encoding | no |

`libcamera` (LGPL-2.1-or-later) sits under picamera2 as a system library. It is
dynamically linked by picamera2, not by birdshot, and birdshot never links
against it.

## External programs

Invoked as separate processes via `subprocess`. Process invocation is an
arm's-length boundary: no licence propagates across it, which is why the GPL
entries here carry no obligation for birdshot even though the PyQt5 entry above
does.

| Program | Licence | Called from | Purpose |
|---|---|---|---|
| **ffmpeg** | LGPL-2.1-or-later, or GPL-2.0-or-later depending on build flags | `src/birdshot/timelapse.py` | assembling frames into a movie |
| **rsync** | GPL-3.0-or-later | `src/birdshot/storage.py`, `src/birdshot/cascade.py`, `sync.sh`, `mac/pull-photos.sh` | offload and tier migration |
| **OpenSSH** (`ssh`) | BSD-style | `src/birdshot/cascade.py`, `sync.sh` | remote tiers and deployment |
| **xdg-open** | MIT (xdg-utils) | `src/birdshot/gui/main_window.py` | opening the capture folder |
| desktop helpers (`pcmanfm`, `feh`, `gio`) | various | `bin/birdshot-wallpaper` | setting the desktop wallpaper |

None of these is required for capture except as noted; a missing `ffmpeg` costs
you movie assembly and nothing else.

## Verifying this file

It is a hand-maintained document and drifts if nobody checks it. `make deps`
lists every third-party import and every external binary the tree actually
reaches for, so a new dependency that never made it into the table above shows
up as a line here with no row to match it.
