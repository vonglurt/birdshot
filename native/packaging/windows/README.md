<!-- SPDX-License-Identifier: MIT — Copyright (c) 2026 Paul Richeson -->
# Building on Windows

No dependencies, so either toolchain works out of the box.

**MSVC** (Visual Studio 2019+ or Build Tools):

```bat
cmake -S native -B build
cmake --build build --config Release
build\Release\birdshot.exe selftest
```

**MinGW-w64** (or MSYS2 `UCRT64`):

```sh
cmake -S native -B build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build
build/birdshot.exe selftest
```

The binary is static; copy `birdshot.exe` anywhere. Settings live in
`%USERPROFILE%\.config\birdshot\settings.json`, captures under
`%USERPROFILE%\birdshot-data`, both overridable (`--config`, `data_root`).

The CI matrix (`.github/workflows/native.yml`) builds and selftests every
push on `windows-latest` with MSVC, so a broken Windows build cannot reach a
tag unnoticed.
