<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# birdshot native — the 2.0 line ("Migration")

**The birdshot pipeline rewritten in C++17. No Python, no runtime, no
dependencies — the math, the ephemeris, the JSON and the JPEG encoder are
all in this tree.** One CMake build targets macOS, Windows, Linux, and the
BSDs; the packaging under `packaging/` ships the same binary through
Flatpak, deb, rpm, Homebrew and the FreeBSD ports.

`2.0.0-rc1` · MIT · builds green and selftests clean on macOS today, with a
CI matrix holding Linux, Windows and FreeBSD to the same bar.

---

## Why a rewrite

Three reasons, in the order they mattered:

1. **Speed.** The 1.x capture loop was memory-bandwidth bound in numpy on a
   CM4 at 21 fps RAPID. This engine runs the identical pipeline — metering,
   AE, gates, storage, encode — at **~475 fps for RAPID and ~190 fps for
   full COLLECT** on a laptop, per `birdshot selftest`'s engine checks. The
   Pi will be slower than the laptop; it will not be slower than Python.
2. **Distribution.** A Python + PyQt5 + numpy install is a per-platform
   negotiation. A static binary is not. This tree compiles anywhere a C++17
   compiler exists and ships as one file.
3. **Licensing.** The 1.x GUI made PyQt5's GPL the one licensing caveat in
   an otherwise MIT project. There is no PyQt5 here — the native line is
   MIT all the way down, with `vendor/` empty by the same policy as 1.x.

## What is in the RC

Everything headless, at behavioural parity with 1.x — same config keys,
same folder naming, same `index.jsonl` records, same verdicts — plus the
**Horizons** layer (geometry, earth coordinates, sunset estimation,
multi-day alignment, shoot planning) that 1.x only had as a design review:

| | |
|---|---|
| **Capture engine** | COLLECT / RAPID / TIMELAPSE / BIRD FLIGHT over a backend interface; the synthetic scene ships, platform camera backends slot in behind five calls |
| **Auto-exposure** | the EV-space PID: lux feed-forward, highlight priority with the sky's separate clip budget, fast-acquire, constrained-equilibrium settling, the shutter/gain ladder |
| **Quality gates** | dark / blown / empty / ok from histogram passes; Laplacian + Tenengrad focus measures; the focus map |
| **Bird Flight** | the full gate ladder: motion, subject blob, ring-of-sky, composition margins, boundary sharpness |
| **Solar ephemeris** | NOAA algorithm, in-tree, validated: sunset to ±1 min, the 62.6° azimuth swing at 40°N, the 3.3–3.9 min lower-limb contact window |
| **Earth coordinates** | WGS84 site handling, ECEF, great-circle distance and bearings |
| **Planning** | `birdshot plan`: per-evening sunset time/azimuth/drift, descent rate, contact window, golden hour, civil dusk — and whether your lens's FOV holds the swing from a fixed mount |
| **Alignment** | `birdshot align`: frames from different days paired by **solar elevation**, not clock time, plus a pixel-shift refiner for stacking |
| **Storage** | `sess-`/`rapid-`/`tlc-` sessions, `s<N>`/`ms<N>`/`us<N>` buckets, centisecond names claimed with `O_EXCL`, `.part` renames, `index.jsonl`, resume state |
| **JPEG** | an in-tree baseline JFIF encoder (luma), decodable by everything we could throw at it |
| **Selftest** | 30 checks covering all of the above, including three end-to-end engine runs and a loopback fetch from the viewfinder; `ctest` wired |

The synthetic backend deserves a sentence: with a site configured it lights
its sky from the **real solar elevation at your coordinates** — capture at
night and the gates will correctly call your frames dark; raise the shutter
cap and you are back in the legacy `s191` regime. The whole pipeline can be
exercised, end to end, anywhere, at any hour, honestly.

## What is not in it (yet)

Stated plainly, because an RC that hides its edges is not a candidate:

- **Some GUI edges.** The desktop front end exists — `birdshot-gui`
  under `native/qt/`, a Qt 6 Widgets rewrite of the prototype's four
  faces (Camera / Field / Bench / Library, Ctrl+1..4) over the same
  settings.json: live preview with the full overlay stack, the Bench
  settings rail with search/provenance/profiles, the Bird Flight gate
  ladder, the Library darkroom. It builds only when CMake finds Qt 6
  (LGPL, dynamically linked — the app stays MIT; the 1.x caveat was
  PyQt5's GPL, which does not apply here) and needs a platform camera
  backend before it can shoot anything real. `birdshot gui` — the
  loopback HTTP viewfinder, zero-dependency — remains for headless and
  remote use. Pieces whose core is not ported (video, cascade, EXIF,
  encode) are greyed in the GUI with the reason, exactly how the
  prototype gated what a camera could not do.
- **Most platform camera backends.** The first one is in: **AVFoundation
  (macOS)** captures real frames from real hardware through the full
  pipeline -- webcams and external cameras, listed by the GUI's camera
  selector, letterboxed to the shared 640x480 analysis plane. A webcam
  owns its own exposure, so it declares no `exposure` capability and the
  GUI greys those dials with the reason. V4L2 (Linux/BSD), Media
  Foundation (Windows) and libcamera (Pi) are the remaining ports; the
  IMX477 on the CM4 -- the instrument itself -- is the one 2.0.0 waits
  for. Real-bird tuning still happens on 1.x replay.
- **The cascade, EXIF injection and ffmpeg assembly.** Headless 1.x
  features that migrate after the camera backends, in that order.

## Build

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
build/birdshot selftest
```

With Qt 6 present (`brew install qt`, `apt install qt6-base-dev`, ...) the
same build also produces `build/birdshot-gui`, the desktop front end; pass
`-DCMAKE_PREFIX_PATH=$(brew --prefix qt)` on macOS if CMake does not find
it on its own (the top-level `make build` does this for you). Without Qt,
nothing changes: the core and the CLI stay dependency-free.

That is the whole recipe on every platform (see
`packaging/windows/README.md` for the MSVC spelling). CMake ≥ 3.16 and any
C++17 compiler.

## Use

```sh
birdshot site set 40.7128,-74.0060 --name "the yard" --elev 10

birdshot sun                        # position now + today's events
birdshot plan --days 14             # two weeks of evenings, one line each
birdshot capture -n 200             # COLLECT: gates, buckets, index.jsonl
birdshot rapid -n 500               # flat centisecond names, fastest path
birdshot timelapse -i 5             # one frame every 5 s until Ctrl-C
birdshot birdflight -v              # watch the sky, fire on a bird
birdshot align ~/birdshot-data/sess-*   # pair frames across days by sun elevation
birdshot sessions
birdshot doctor
```

Settings live in `~/.config/birdshot/settings.json` — the same file, keys
and deep-merge behaviour as 1.x, so a native install dropped onto a machine
that ran the Python line picks up its tuning. `--config` points anywhere.

## Module map

| | |
|---|---|
| `include/birdshot/`, `src/` | the core library, one concern per file |
| `json`, `mathkit` | in-tree JSON and the histogram/stats kit |
| `geo`, `solar` | WGS84 + the NOAA ephemeris (the Horizons layer) |
| `naming`, `storage`, `config` | the 1.x-compatible disk contract |
| `analysis`, `exposure` | metering, gates, the EV PID and the ladder |
| `birdflight` | the auto-take gate ladder |
| `backend`, `synthetic`, `engine` | capture: interface, reference backend, the loop |
| `plan`, `align` | shoot planning and multi-day alignment |
| `jpeg`, `image` | the baseline JFIF encoder and the luma plane |
| `gui` | PreviewPump (the shared idle capture loop) + the loopback HTTP viewfinder |
| `../qt/` | the Qt Widgets front end: four faces, the Bench rail, the gate ladder |
| `selftest` | the 30-check gate; `cli/main.cpp` is the binary |

## Versioning

The 1.x Python line continues, deprecated, under `prototype/` and is what
runs on the deployed Pi today. This tree is the 2.0 line; `2.0.0` final is cut from this RC once
the selftest has passed on each shipping platform's CI **and** a platform
camera backend has captured real frames on real hardware — the same
no-rc-without-the-instrument rule 1.x releases follow.
