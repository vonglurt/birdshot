#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# Install birdshot on this machine, from this checkout.
#
#   ./install.sh              system deps via the native package manager,
#                             then `pip install --user .`, then doctor
#   ./install.sh --dev        editable install (pip install -e) for hacking
#   ./install.sh --no-system-deps    skip the package-manager step
#   ./install.sh --json-report       doctor output as JSON (for copal's
#                                    provisioning; exit code is doctor's)
#
# Policy (docs/PACKAGING.md, Phase 3 and 6.3): numpy, Qt and the camera stack
# come from the *distro*, never from pip wheels, on the appliances -- Alpine's
# musl and the Pi's libcamera both want the native builds. pip only installs
# birdshot itself, which is pure Python.

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dev=0 sysdeps=1 json=0

for arg in "$@"; do
    case "$arg" in
        --dev) dev=1 ;;
        --no-system-deps) sysdeps=0 ;;
        --json-report) json=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { [ "$json" = 1 ] || echo "== $*"; }

# ---------------------------------------------------------------- system deps
if [ "$sysdeps" = 1 ]; then
    if command -v apk >/dev/null 2>&1; then
        # Alpine / copal -- the flagship target. PyQt5's package name is
        # pinned by copal's aports; fall back to CLI-only if it is absent.
        say "apk: python, numpy, ffmpeg (+ Qt if the repo carries it)"
        sudo apk add --no-progress python3 py3-pip py3-numpy ffmpeg
        sudo apk add --no-progress py3-qt5 2>/dev/null \
            || say "no py3-qt5 in this apk repo -- GUI skipped, CLI unaffected"
    elif command -v apt-get >/dev/null 2>&1; then
        # Debian / Raspberry Pi OS / Ubuntu.
        say "apt: python, numpy, Qt, ffmpeg, rsync"
        sudo apt-get update -qq
        sudo apt-get install -y python3 python3-pip python3-numpy \
            python3-pyqt5 ffmpeg rsync
        # Only exists on Raspberry Pi OS; elsewhere there is no camera anyway.
        sudo apt-get install -y python3-picamera2 python3-simplejpeg 2>/dev/null \
            || say "picamera2 not in this apt repo -- fine off the Pi"
    elif command -v dnf >/dev/null 2>&1; then
        say "dnf: python, numpy, Qt (ffmpeg may need rpmfusion)"
        sudo dnf install -y python3 python3-pip python3-numpy python3-qt5
        sudo dnf install -y ffmpeg 2>/dev/null \
            || say "ffmpeg not available -- enable rpmfusion for timelapse assembly"
    elif command -v brew >/dev/null 2>&1; then
        say "brew: ffmpeg (numpy arrives via pip on macOS)"
        brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
    else
        say "no known package manager -- continuing with pip only"
    fi
fi

# ------------------------------------------------------------------- birdshot
pipflags=(--user)
[ "$dev" = 1 ] && pipflags+=(-e)
say "pip install ${pipflags[*]} $here"
# Newer distros mark the system Python externally managed (PEP 668); a --user
# install of a pure-Python package is exactly what that flag exists to allow.
python3 -m pip install "${pipflags[@]}" "$here" 2>/dev/null \
    || python3 -m pip install --break-system-packages "${pipflags[@]}" "$here"

# ------------------------------------------------------------------ the proof
# Run doctor from the checkout: works even when ~/.local/bin is not on PATH.
if [ "$json" = 1 ]; then
    exec python3 "$here/bin/birdshot-cli" doctor --json --write-config
else
    say "doctor"
    python3 "$here/bin/birdshot-cli" doctor
    echo
    echo "installed. If 'birdshot-cli' is not found, add ~/.local/bin to PATH."
fi
