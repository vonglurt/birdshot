<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# The native line: design philosophy and architecture

How the 2.0 line is built, why it is built that way, and how the GUI is
wired. Written when the tree stood at ~13,000 lines: ~5,900 in the core
library, ~4,400 in the Qt front end, a 920-line selftest holding 34 checks
over all of it. The algorithms themselves — the AE controller, the gates,
the Bird Flight ladder, the solar math — are explained constant by
constant in [PHYSICS.md](PHYSICS.md); the ledger against the 1.x
prototype is [PARITY.md](PARITY.md).

---

## Part I — Design philosophy

Nine rules, each earned rather than declared. Everything in the tree can be
traced to one of them.

### 1. Nothing in the core that isn't ours

The core library and the `birdshot` CLI have **zero third-party
dependencies**: the JSON parser, the math kit, the JPEG encoder *and*
decoder, the EXIF writer, the NOAA solar ephemeris, the WGS84 geodesy — all
in-tree, all MIT. The only things linked are **system boundaries**: the C++
standard library, POSIX/winsock sockets, and per-platform camera frameworks
(AVFoundation today; V4L2, Media Foundation and libcamera the same way
later). A system framework is like libc — it ships with the machine, it
carries no vendored code and no licensing consequence.

The payoff is the distribution story: this tree compiles anywhere a C++17
compiler exists and ships as one static file. The cost is real — writing a
JPEG codec is not free — and it is paid deliberately, once, instead of
paying the per-platform dependency negotiation forever.

The one exception is drawn precisely: the Qt front end links Qt 6
**dynamically under the LGPL**, as an *optional* CMake target. No Qt at
configure time, no change to anything else. The 1.x licensing caveat was
PyQt5's GPL — the Python bindings, not Qt itself — and it does not apply to
Qt's C++ API. The application stays MIT.

### 2. Luma is for judgment, colour is for people

Every meter, every quality gate, the AE controller and the Bird Flight
detector run on the 640×480 luma plane (`Gray8`). That is where the tuned
numbers live — the PID gains, the gate thresholds, the focus measures —
and they are never re-derived casually. Colour (`Rgb8`) exists so the frame
a person keeps looks like what the camera saw: it is captured, saved and
displayed, and it is **never analysed**. When the webcam delivers 1080p
colour, the engine saves that, while the gates keep judging the letterboxed
luma. One plane for the machine, one for the human.

### 3. The prototype is the specification

The 1.x Python line is not legacy to be discarded; it is the **reference
implementation** the rewrite is held to parity with. Same `settings.json`
keys with the same deep-merge, same session folders, same `index.jsonl`
records, same verdict words, same Bird Flight reason strings, same shutter
bucket names going back to `runCam.sh`. Where the prototype encoded a
tuned behaviour, the port carries the tuning, not just the shape — the
canonical example being the AE clock: the engine feeds the PID **virtual
frame-cadence time** (`seq * 0.25`), because the gains were tuned at 1.x's
3–4 fps and wall-clock dt at 600 fps would make the derivative term amplify
metering noise a hundredfold.

### 4. Gate, never hide

A control that could only ever answer with an error is shown **disabled,
with the reason**, not removed. The rule scales from a single button to
whole subsystems: on a webcam, the Exposure section greys with "owns its
own exposure — these have no effect here"; the Cascade section greys with
"not in the native line yet". The same mechanism serves capability
differences between cameras and unported features — the user always sees
the full shape of the instrument and exactly why a piece is dark.

### 5. One pipeline, many faces

There is exactly **one capture loop** and exactly **one preview widget**.
Idle, the loop is `PreviewPump` (capture → analyse → AE → sink); recording,
it is the `Engine` with its frame tap playing the sink's role. Every face —
Camera, Field, Bench, Library, the fullscreen window, the focus monitor,
the browser viewfinder — paints from that one stream. The preview widget is
*reparented* between faces rather than duplicated, so no face pays for a
second per-frame pipeline. Anything that would need a second copy of the
truth is restructured until it doesn't.

### 6. Honest edges, loud failures

An RC that hides its edges is not a candidate: the deferred list is stated
in the README, gated in the GUI, and recorded in the changelog. Failures
are proportional to their consequence — a bad frame gets a verdict, a
failed camera gets a banner naming the fix, and a full disk gets a
**blocking overlay** that cannot be dismissed until space actually exists,
because capture stopping silently is the one failure a field instrument
must never have.

### 7. The disk contract is sacred

`sess-<epoch>/s<N>/YYYYMMDDHHMMSScc.jpg`, names claimed with `O_EXCL`,
writes via `.part` + rename, one JSON line per frame in `index.jsonl`. This
layout predates both rewrites and every consumer — sync, the Library,
alignment, assembly — reads it. New code adapts to the contract; the
contract does not adapt to new code.

### 8. External programs at arm's length

ffmpeg assembles movies as a **separate process**, exactly as 1.x invoked
it: no linkage, no licence propagation, no build-time dependency. The GUI's
encode panel shells out to `birdshot assemble` — its own CLI — so exactly
one encode path exists to trust, test, and debug.

### 9. Selftested against the outside world

34 checks run everywhere `ctest` runs, no hardware needed — and where a
format crosses the tree's boundary it is validated against an independent
implementation: the JPEG encoder against ffmpeg's decoder (and `sips`, and
PIL), the EXIF writer against exiftool, the ephemeris against published
NOAA numbers, the engine against three end-to-end runs on the synthetic
backend. The synthetic scene itself follows this rule: with a site set it
lights its sky from the **real solar elevation at those coordinates**, so
the whole pipeline — including the golden-hour colour ramp — exercises
honestly at any hour, anywhere.

---

## Part II — Layered architecture

Strict downward dependencies; no layer reaches up.

```
─────────────────────────────────────────────────────────────────────────────
 FRONT ENDS          cli/main.cpp        src/gui.cpp          qt/  (optional)
                     the birdshot CLI    Viewfinder over      birdshot-gui,
                     20+ commands        loopback HTTP        four faces
─────────────────────────────────────────────────────────────────────────────
 CAPTURE             engine.cpp                    gui.cpp:PreviewPump
                     the recording loop:           the idle loop: capture,
                     sessions, gates, saves,       analyse, AE, sink —
                     frame/sighting taps           no storage
                     ┌─────────────────────────────────────────────┐
                     │ backend.cpp — the Backend interface, 5 calls│
                     │ synthetic.cpp  avfoundation.mm  replay.cpp  │
                     └─────────────────────────────────────────────┘
─────────────────────────────────────────────────────────────────────────────
 DOMAIN              analysis.cpp   exposure.cpp   birdflight.cpp
                     metering+gates the EV PID     the gate ladder
                     storage.cpp    config.cpp     plan.cpp   align.cpp
                     disk contract  settings.json  horizons layer
─────────────────────────────────────────────────────────────────────────────
 KITS                image.cpp   jpeg.cpp    exif.cpp   json.cpp
                     Gray8/Rgb8  codec both  APP1/TIFF  parser+writer
                     mathkit.cpp naming.cpp  geo.cpp    solar.cpp
─────────────────────────────────────────────────────────────────────────────
 SYSTEM              C++17 std · POSIX/winsock · AVFoundation (macOS)
                     Qt 6 Widgets (optional, LGPL, dynamic)
─────────────────────────────────────────────────────────────────────────────
 CROSS-CUTTING       selftest.cpp — 34 checks spanning every layer above
```

The load-bearing seam is the **Backend interface**: five virtual calls
(`name`, `capabilities`, `limits`, `capture`, plus the factory). Everything
above it — metering, AE, gates, Bird Flight, storage, both GUIs — is
backend-blind. A `Frame` carries three planes: `y` (the 640×480 analysis
plane, always), `full` (native luma, optional), `color` (native RGB,
optional). Analysis reads `y`, focus refines on `full`'s centre crop, and
saves prefer `color`. Adding a camera means implementing five calls and
listing honest capability strings; the GUI's gating does the rest.

---

## Part III — GUI architecture analysis

`native/qt/` is ~5,300 lines across seven units. The design reduces to
four mechanisms.

### 1. One seam to the core: `CaptureController`

The only object that touches capture. It owns the `Backend`, the
`PreviewPump`, and (while recording) the `Engine` on a worker thread, and
it normalises both sources into **one signal**:

```
idle:       PreviewPump ──sink──▶ deliver() ──frameReady(FramePacket)──▶ GUI
recording:  Engine ──frame tap──▶ deliver() ──frameReady(FramePacket)──▶ GUI
                   └─sighting tap─────────────sightingReady(...)──────▶ GUI
```

Faces cannot tell whether a frame came from the idle loop or a recording;
they connect once and paint. `FramePacket` is an immutable value — the
planes ride in `shared_ptr<const Gray8/Rgb8>` — so the queued-connection
hop between threads copies pointers, not pixels.

Two rate rules protect the event loop: the engine's frame tap emits at most
~15 packets/s (the engine itself still runs at full speed — 600+ fps
synthetic runs stay 600+ fps), and sightings throttle to 10/s **except
fired takes, which always land**. Switching cameras is
`rebuildBackend()` — tear down the pump, re-run the factory, restart — the
same gesture the prototype used, and also the honest implementation of
"Reset AE loop".

### 2. One preview, reparented

`PreviewWidget` is instantiated once and moved between the Bench splitter,
the Camera face and the Field face as the user switches (`setParent` +
`insertWidget`). It renders the display image itself — colour base when the
packet carries one, luma otherwise — and burns zebras, peaking and the
outdoor rendering **into the pixels**, then paints vector overlays (zones,
grid, focus map, HUD, countdown ring, bird box, verdict frame) on top.
Every overlay toggle lives in one place — Scene > "Focus aids and
overlays" — and the wheel-over-the-preview gesture syncs every checkbox,
so no toggle can lie about its state; three paints are exempt (countdown
ring, bird box, verdict frame) because each answers "is it working?" and
hiding it would cost a shot. The Camera face stashes the overlay flags on
entry and restores them on exit: a camera app shows the picture.
Fullscreen is a deliberate *second* widget in
a separate window (settings copied over), so closing it can never leave the
main window half-configured.

### 3. The settings registry: bind once, derive everything

Every bound control goes through five tiny factories (`spinInt`,
`spinDouble`, `check`, `comboStr`, `line`) that write the config key on
change, save, and register `{key, widget, refresh}` in one list. Five
features are then *derived* from that single list rather than built:

| Feature | Derivation |
|---|---|
| find-a-setting search | walk each bind's form label + owning accordion → completer entries |
| provenance (amber labels) | `cfg[key] != Config::defaults()[key]`, restyle on flip only, counted per key |
| the reset dialog | the same diff, deduplicated, listed and applied |
| profiles | `Config::snapshot()` minus the machine keys, applied key-by-key |
| sibling sync | after any write, every *other* bind on the same key refreshes — so a key bound in two places (`exif_enabled` on the Machine tab and the encode panel) can never disagree with itself |

Adding a setting is one factory call; search, provenance, reset, profiles
and sibling sync pick it up with no further code — **provided the key
exists in `Config::defaults()`**. That proviso is the registry's one
earned invariant: provenance and reset silently skip unknown keys, and the
factories read the initial value with no fallback, so a bound key missing
from defaults produces a control that shows a clamped garbage value on
first run and can never be reset. The port learned this the expensive way
(the encode panel booted at 1 fps / CRF 0 because the `encode_*` defaults
had not been carried), so the rule is now stated here: **every key a
factory binds must have a default.**

The prototype's known bugs in this area — unregistered widgets invisible
to reset, doubly-bound combos — were fixed in the port rather than
reproduced. Its subtler trap was fidelity itself: the port faithfully
carried `meter_ema`, a control 1.x's own defaults table marks as
superseded, while the keys that superseded it (`ae_damping`,
`ae_average_n`/`_mode`, `pid_integral_clamp_ev`, `prefer_exposure_time`)
had controls in neither line. The rule that shook out: **parity is owed to
behaviour, not to dead weight** — every key the core actually reads gets a
control; a control nothing reads gets deleted. The full key-to-control map
lives in [PHYSICS.md](PHYSICS.md); the ledger of what was kept, added and
dropped is [PARITY.md](PARITY.md).

### 4. The face ↔ shell contract

Faces are dumb about the core and talk only to `MainWindow`:

```
faces call the shell:            the shell calls faces:
  cfg(), capture(), caps()         updateGo(running, state, label)
  modes(), tuner()                 syncMode(idx)
  setFace(), goClicked()           setOutdoor(on, style)
  log(), openPath()                refreshStatus(session, free)
  chkOutdoor(), cmbOutdoor()       onSighting(s, takeN, fired)
```

Bench's widgets are the masters (the Field face's big OUTDOOR button pushes
the Bench checkbox, whose handler does the real work), so state has one
owner and the faces merely mirror it. Mode changes fan out from the one
master `ModeTuner`; capability changes fan out from `applyCapabilities()`,
which computes per-mode reasons once and applies them to all three tuners
and every gated section.

### Threading model, in one table

| Thread | Runs | Talks to GUI via |
|---|---|---|
| GUI (main) | Qt event loop, all painting, config writes | — |
| PreviewPump | capture → analyse → AE, at `gui_preview_fps` | queued `frameReady` |
| Engine worker | the real recording loop, full speed | queued taps + `recordingFinished` |
| Doctor | probes disk/backend/config | `QMetaObject::invokeMethod` |
| Encode | `birdshot assemble` as a `QProcess` | process signals |

No mutexes in GUI code: everything crosses on Qt's queued connections
carrying immutable values. The only shared mutable state is inside the
backends, guarded at the frame-buffer boundary.

### What the GUI deliberately does not do

It does not analyse frames (the pump/engine already did), does not encode
movies (it shells to the CLI), does not talk to backends directly (the
controller does), and does not persist UI state beyond what the prototype
persisted — the boot face is deploy policy, not a session preference.
