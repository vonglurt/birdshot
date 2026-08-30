<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# packaging/

One directory per distribution channel, each a thin adapter over the same
canonical build (`pyproject.toml` → `python -m build`). The plan, the rules
and the channel status table live in [docs/PACKAGING.md](../docs/PACKAGING.md);
this file is just the map.

| dir | format | consumes | notes |
|---|---|---|---|
| `alpine/` | APKBUILD | tag tarball | **flagship** — copal's auto-installed desktop camera app |
| `debian/` | dh + pybuild | sdist | Debian, Raspberry Pi OS, Ubuntu/PPA |
| `rpm/` | spec | sdist | Fedora/openSUSE via COPR |
| `flatpak/` | manifest | sdist | any desktop Linux; Flathub later |
| `homebrew/` | formula | tag tarball | the Mac darkroom |
| `freebsd/` | port skeleton | tag tarball | untested until a BSD box exists |
| `share/` | — | — | desktop entry and other channel-shared assets |

Rules that apply to every channel:

- The dependency list is `pyproject.toml`'s; a channel translates names, it
  never adds or pins its own.
- `source=`/`url=` always points at a **tag**, never a branch.
- A channel file that cannot keep up gets marked "community" in the README's
  support matrix rather than silently rotting (docs/PACKAGING.md, 9.5).
