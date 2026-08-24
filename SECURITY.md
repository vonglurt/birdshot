<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Security Policy

## Reporting a vulnerability

Email **paulr@sdf.org** with `[birdshot]` in the subject. Please do not open a
public issue for anything exploitable. Expect an acknowledgement within about a
week; this is a personal project, not a staffed one.

## Supported versions

`1.0.0-rc1` is the current release candidate and the only supported version.
Fixes land on `main`.

## What is actually exposed

birdshot is a camera application on a private network. It listens on nothing,
opens no port and serves no request. Its exposure is entirely in what it
*reaches out to* and what it *writes down*.

| Surface | Notes |
|---|---|
| `sync.sh`, `mac/pull-photos.sh` | Drive `ssh` and `rsync` to the Pi using **your** keys and agent. They run whatever `PI_HOST` says. Setting `PI_HOST` from an untrusted source is remote command execution on your Mac, by design — it is a deployment script, not a sandbox. |
| `src/birdshot/cascade.py` remote tiers | A cascade tier may be `user@host:/path`. Paths are quoted before interpolation into the remote command, but the tier list is trusted input: it comes from your own config. Do not accept one from anyone else. |
| `~/.config/birdshot/settings.json` | Plain JSON, mode 0644 by default. Holds paths and capture parameters. **No credentials** — SSH auth is your agent's business and birdshot never handles a key or a password. |
| Captured frames + `index.jsonl` | The real disclosure risk, and it is not a code defect. See below. |
| `bin/birdshot-wallpaper` | Writes the live view to the Pi's desktop wallpaper. Anyone who can see the screen sees the camera. Obvious, and easy to forget when the Pi is on a shared display. |
| EXIF tagging (`src/birdshot/exif.py`) | Writes capture parameters into the JPEG. It writes **no GPS tags** and never has. If you add them, understand you are stamping your address into every frame you publish. |

## The thing most likely to hurt you

**It is not a bug in this software. It is publishing a photograph.**

A birdshot session is thousands of high-resolution frames of a fixed view from
somewhere you live or work, each stamped to the centisecond, with a
machine-readable `index.jsonl` recording exactly when the camera was pointed
there. That is a movement and occupancy record of a real place. A timelapse of
your garden is also a record of when your garden is empty.

The repository defends against this structurally rather than advisorily:
`.gitignore` excludes `*.jpg`, `*.mp4`, `index.jsonl`, `session.json` and
`birdshot-data/`, and the pre-commit hook **refuses** to stage any of them
regardless of `.gitignore`. Both exist because "remember not to commit your
captures" is not a control.

Before publishing frames anywhere, consider what is in the corners of the
frame — a licence plate, a house number, a neighbour's window — and that EXIF
carries the timestamp even when the filename does not.

## Deployment notes

- The Pi is assumed to be on a trusted LAN. **Do not port-forward it.** birdshot
  provides no authentication because it needs none where it is designed to run.
- Prefer key-based SSH with an agent. `sync.sh` sets `BatchMode=yes`, so it will
  fail rather than fall back to prompting for a password.
- `autowrite.yes` on a USB stick makes birdshot start capturing to that stick at
  boot with no interaction. That is the intended behaviour and it means **any
  stick with that file on it, in that Pi, starts recording.** Treat the marker
  file as a physical-access control decision.
- Storage cascade deletion is opt-in per tier and every deletion is preceded by
  a verified copy — every file present, every size identical. The last tier
  never deletes unless ring mode is explicitly enabled. If you enable ring mode,
  you have asked for a system that overwrites its own oldest data, and it will.

## Scope

**In scope:** anything letting a party who is not you execute code, read files
outside the capture root, exfiltrate frames, or cause the cascade to delete data
that was never verified downstream.

**Out of scope:** the Pi being unauthenticated on your own LAN (by design);
`sync.sh` executing what `PI_HOST` tells it to (that is its job); physical
access to the Pi or the USB stick; and the operator publishing their own
photographs.
