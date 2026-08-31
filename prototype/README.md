<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# birdshot prototype — the 1.x Python line (deprecated)

**This directory is the original birdshot: Python + numpy + PyQt5, the 1.x
line that shot on the CM4 first.** It works, it is what the deployed
instrument runs today, and it is being deprecated in favour of the native
C++17 rewrite in [`../native/`](../native/) — see the 2.0 line's README for
why (speed, distribution, licensing).

What deprecation means here, concretely:

- **No new features land in this tree.** Bug fixes that keep the deployed
  instrument shooting are still welcome; everything else goes to `native/`.
- **The compiled line owns the front door.** `make run`, `make doctor`,
  `make selftest`, `make dist` at the repo root all mean the native binary.
  The Python equivalents live on as `make prototype`, `make
  prototype-doctor`, `make prototype-selftest`, `make prototype-dist`.
- **The tree retires when the native line shoots real frames.** The release
  rule is unchanged: no 2.0.0 final until a platform camera backend captures
  from real hardware. Until that day, this prototype is the reference
  implementation the rewrite is held to parity with.

Everything in here is self-contained and path-relative, so it works from
this directory exactly as it did from the repo root:

```
./install.sh                 install on this machine (pip install --user .)
./sync.sh push               deploy source to the Pi
./sync.sh selftest           the 18-check on-camera selftest
./mac/pull-photos.sh watch   pull captures onto the Mac as they land
```

`pyproject.toml` here remains the source of truth for the Python package;
the channel adapters under `packaging/` consume the sdist + wheel it builds.

The full documentation for this line is the repo's top-level
[README](../README.md) and [docs/](../docs/) — written for 1.x and accurate
for it.
