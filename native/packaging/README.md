<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Packaging the native line

One CMake tree, one static binary per platform, zero runtime dependencies —
which makes every channel below thin: build, install `birdshot`, done. The
1.x Python channels live in `/packaging`; these are their 2.0 counterparts
and ship from the same tagged GitHub Release.

| Channel | File | Target |
|---|---|---|
| Flatpak | `flatpak/org.birdshot.Birdshot.yml` | any Linux desktop |
| Debian/Ubuntu | `debian/` | `.deb` via `dpkg-buildpackage` |
| RPM | `rpm/birdshot-native.spec` | Fedora/openSUSE |
| Homebrew | `homebrew/birdshot-native.rb` | macOS (Intel + Apple Silicon) |
| FreeBSD | `freebsd/Makefile` | the ports tree |
| Windows | `windows/README.md` | MSVC or MinGW build notes |

Everywhere, the build is:

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
build/birdshot selftest        # the gate: no package ships a failing suite
cmake --install build
```

Licensing note that the 1.x channels had to carry and these do not: there is
no PyQt5, so there is **no GPL surface anywhere** in the native line. The
whole tree is MIT with no third-party code in it.
