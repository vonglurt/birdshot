<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Screenshots

**Empty on purpose.** These captures need the Pi powered on with the camera
attached, and it is not. The landing page and the guide have slots for them and
render correctly without them; `1.0.0-rc1` is a release *candidate* largely
because these are outstanding.

Nothing here is mocked up, composited or rendered. If a file is in this
directory it came off the running system.

## The shot list

| File | What it must show |
|---|---|
| `gui-main.png` | The capture tab, live preview, on-image readout showing shutter, gain and frame count mid-session |
| `gui-focus.png` | The focus monitor with the blur gate calibrated — the measure moving as the lens is turned |
| `gui-cascade.png` | The cascade tab mid-migration: a group sealed, one in flight, and the predicted run length |
| `gui-histogram.png` | Histogram levels with black and white points set by hand |
| `rig.jpg` | The CM4 and IMX477 as actually mounted, lens and all |
| `frame-sample.jpg` | One full-resolution capture, EXIF intact, downscaled for the web |

## Taking them

On the Pi, with the GUI running on its own display:

```sh
# whole screen, after a delay long enough to switch tabs
scrot -d 5 ~/gui-main.png
# or, if scrot is not installed
import -window root ~/gui-main.png
```

Then pull them across and commit **one at a time, after looking at each one**:

```sh
scp pi@raspberrypi.local:~/gui-main.png assets/screenshots/
```

## Before you commit one

A screenshot of a live camera is a photograph of wherever the camera points,
and the GUI puts a file path and a capture root on screen next to it.

- **Look at the preview pane.** It shows your actual view — a window, a garden,
  a neighbour's roofline. Decide deliberately that you want that public.
- **Look at the paths on screen.** The GUI shows the capture root. It should read
  `/home/pi/birdshot-data` or `/media/pi/…`; if it shows a different account
  name, change the account or crop it out.
- **`frame-sample.jpg` keeps its EXIF**, which is the point — but EXIF carries
  the capture timestamp. birdshot writes no GPS tags, and you should not add any.
- The pre-commit hook blocks `*.jpg` and `*.png` arriving through a bulk
  `git add`. That is deliberate. Add each file by name, having looked at it:

```sh
BIRDSHOT_ALLOW_UNSANITISED=1 git add assets/screenshots/gui-main.png
```

The bypass is required here and only here, and it exists so that committing a
photograph is always a decision rather than a side effect.
