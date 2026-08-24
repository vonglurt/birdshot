<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Contributing to birdshot

**Fork it.** That needs no permission from anyone. Point it at a different
sensor, strip the GUI and run it headless on a Zero, replace the exposure loop
with something better — the MIT licence allows all of it, and a fork that goes
somewhere this project will not is worth more than a pull request that waters
it down.

If you want a change to land *here*, read on. There are three rules that are
not negotiable and a handful of constraints that will bite you if nobody warns
you first.

## The hardware this is written against

Stated up front, because most surprises come from assuming otherwise:

| | |
|---|---|
| Camera | IMX477 (Pi HQ Camera), 4056×3040, manual-focus C/CS mount |
| Board | Compute Module 4, 4 GB, Debian 11 bullseye |
| Stack | libcamera 0.0.5, picamera2 0.3.12, Python 3.9, PyQt5 |

**Python 3.9 is the floor.** Bullseye ships it and the Pi is not being upgraded
for a patch. No `match`, no `X | Y` unions at runtime, no `dict[str, int]`
annotations without `from __future__ import annotations` — which every module
already has. `make lint` byte-compiles against your host Python, so it will
*not* catch a 3.10-ism if you are on 3.12. Check it yourself.

## Three rules

### 1. Every commit must be signed

**Unsigned commits will not be merged.** SSH is the least trouble:

```sh
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Then register the key at **github.com/settings/keys**. The step everyone
misses: a key added as an *Authentication Key* does **not** verify signatures.
Add the same public key a second time with **Key type: Signing Key**. You will
see the identical key listed twice; that is correct.

Check before you push:

```sh
git log --format='%G? %GS %an <%ae> %s' -3
```

`G` in the first column is a good signature. `N` is unsigned and will be
rejected. To sign commits you already made:

```sh
git rebase --exec 'git commit --amend --no-edit -S' origin/main
```

### 2. `vendor/` is the only place foreign code may live

`src/`, `bin/`, `mac/` and `docs/` are Paul Richeson's work exclusively and are
MIT throughout. A commit that puts somebody else's code in any of them is wrong
*even when the licence is compatible*, because the question that layout answers
is "whose is it?", not "may we use it?".

[vendor/README.md](vendor/README.md) has the procedure — `SOURCE.txt`,
verbatim `LICENSE`, `MANIFEST.sha256`, and a row in
[THIRD-PARTY.md](THIRD-PARTY.md). `make vendor-check` enforces the shape.

**Do not add a dependency casually.** Every import is something an operator has
to install on a Pi over a slow link, and one of the five current ones is already
a licence problem (PyQt5 is GPL — see [THIRD-PARTY.md](THIRD-PARTY.md)). New
GPL dependencies outside `src/birdshot/gui/` will be refused: the headless path
being free of copyleft is a property worth keeping.

### 3. Nothing about your machine enters the repository

This project was published out of a working tree containing a real Pi on a real
LAN. A private address, a `/home/<user>` path and a USB stick's volume label
were scrubbed from every commit once, and the point is that they do not come
back.

```sh
make hooks      # git config core.hooksPath .githooks
```

The pre-commit hook refuses staged content carrying a private IP, a home path
that is not `/home/pi`, a personal `/media/` mount, private key material, or a
capture/log file. It is not a style check — it is the thing standing between a
convenient hardcoded address and a permanent public record of your network.

Deployment targets belong in environment variables, which is how `sync.sh`
already does it:

```sh
PI_HOST=pi@raspberrypi.local REMOTE_DIR=/home/pi/birdshot ./sync.sh push
```

If you must bypass the hook — a history document that has to name the old
project, say — the escape is deliberate and greppable:

```sh
BIRDSHOT_ALLOW_UNSANITISED=1 git commit ...
```

Expect to justify it in review.

## The gate

```sh
make check
```

`lint` byte-compiles every module and parses every shell script; `sanitise`
runs the hook's rules across the whole tracked tree, which catches anything
committed before the hook existed; `vendor-check` verifies the manifests. All
three must pass before a pull request is opened.

`make audit-history` is the slower, thorough version — it reads every blob in
every commit rather than the current tree. Run it before a release, or any time
history has been rewritten.

## Testing, and its honest limits

**`make check` does not prove the code works.** It proves it parses and that
nothing private leaked. The real test needs the camera:

```sh
make selftest        # ./sync.sh selftest — 18 checks against real hardware
```

It exercises naming, the quality gates, the exposure ladder, PID convergence,
storage layout, YUV conversion, the live camera, ffmpeg and Qt. **Run it after
any change to `src/birdshot/`,** and say in the pull request whether you did.
"I could not, I have no CM4" is a perfectly acceptable answer and much better
than silence — it tells the maintainer what still needs checking.

Changes to `analysis.py`, `exposure.py`, `naming.py` and `cascade.py` are the
ones most worth testing on hardware, because they are the ones whose failure is
quiet: a wrong exposure or a mis-bucketed folder does not raise, it just
produces worse photographs.

## Style

Match the file you are editing. Beyond that:

- **Comments explain *why*, and say what was measured.** This codebase's
  comments carry numbers — "78 MB/s write", "identical to 6 on this board",
  "~50-100 ms per file". That is the house voice. If you tuned a constant, the
  comment says what you tuned it against.
- **Say what something does *not* do.** `cascade.py` opens by stating that it
  does not make capture faster, because that is the thing everyone assumes.
  Pre-empting a wrong assumption is worth more than restating a right one.
- Every source file opens with `# SPDX-License-Identifier: MIT` and
  `# Copyright (c) 2026 Paul Richeson`. New files too.
- `from __future__ import annotations` at the top of every module. Non-optional
  on 3.9.

## Reporting a bug

Include the board, the OS image, `birdshot-cli info` output, and — if capture
is involved — a line or two from the session's `index.jsonl`. **Do not attach
frames**; they are large and they are photographs of somewhere you live.

Security issues go to [SECURITY.md](SECURITY.md), not to the issue tracker.
