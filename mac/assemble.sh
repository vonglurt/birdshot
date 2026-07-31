#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
# Assemble a pulled session into a movie, on the Mac.
#
#   ./mac/assemble.sh ~/birdshot-data/tlc-1730380000
#   ./mac/assemble.sh ~/birdshot-data/sess-123 --fps 60 --width 3840 --all
#
# Same frame selection as the Pi: index.jsonl drives the order, and frames the
# quality gates rejected are skipped unless --all is given. The Mac encodes this
# an order of magnitude faster than four A72 cores, which matters once a session
# runs to thousands of 12 MP frames.

set -euo pipefail

FPS=60
WIDTH=""
ONLY_OK=1
OUTPUT=""
SESSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fps)    FPS="$2"; shift 2 ;;
    --width)  WIDTH="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --all)    ONLY_OK=0; shift ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)        SESSION="$1"; shift ;;
  esac
done

[[ -n "$SESSION" ]] || { echo "usage: $0 <session-dir> [--fps N] [--width N] [--all]" >&2; exit 1; }
[[ -d "$SESSION" ]] || { echo "no such directory: $SESSION" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not found -- brew install ffmpeg" >&2; exit 1; }

SESSION="${SESSION%/}"
NAME="$(basename "$SESSION")"
OUTPUT="${OUTPUT:-$SESSION/../${NAME}_${FPS}fps.mp4}"

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/birdshot-assemble.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT

echo "==> selecting frames from $NAME"
python3 - "$SESSION" "$SCRATCH" "$ONLY_OK" <<'PY'
import json, os, sys

session, scratch, only_ok = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
frames = []
index = os.path.join(session, "index.jsonl")

if os.path.exists(index):
    with open(index) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            rel = e.get("file")
            if not rel:
                continue
            if only_ok and e.get("verdict", "ok") != "ok":
                continue
            p = os.path.join(session, rel)
            if os.path.exists(p):
                frames.append(p)

if not frames:
    # No index (e.g. a legacy runCam.sh folder) -- timestamp-first filenames
    # sort chronologically anyway.
    for root, dirs, files in os.walk(session):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for n in sorted(files):
            if n.lower().endswith((".jpg", ".jpeg")):
                frames.append(os.path.join(root, n))
    frames.sort()

for i, src in enumerate(frames):
    os.symlink(os.path.abspath(src), os.path.join(scratch, "%08d.jpg" % i))

print("    %d frames" % len(frames))
if not frames:
    sys.exit(2)
PY

VF="scale=trunc(iw/2)*2:trunc(ih/2)*2"
[[ -n "$WIDTH" ]] && VF="scale=${WIDTH}:-2"

echo "==> encoding at ${FPS} fps -> $OUTPUT"
ffmpeg -hide_banner -loglevel warning -stats -y \
  -framerate "$FPS" -i "$SCRATCH/%08d.jpg" \
  -vf "$VF" -c:v libx264 -preset slow -crf 18 \
  -pix_fmt yuv420p -movflags +faststart \
  "$OUTPUT"

echo "==> done: $OUTPUT"
ls -lh "$OUTPUT"
