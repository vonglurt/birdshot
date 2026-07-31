#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
# Pull captures off the Pi onto this Mac, so you can watch frames arrive live.
#
#   ./mac/pull-photos.sh once        one pass
#   ./mac/pull-photos.sh watch       keep pulling every few seconds
#   ./mac/pull-photos.sh live        watch + print each new file as it lands
#   ./mac/pull-photos.sh mirror      watch, and delete local files the Pi dropped
#   ./mac/pull-photos.sh latest      keep only latest.jpg fresh (cheap monitor)
#   ./mac/pull-photos.sh verify      compare both sides: counts, sizes, checksums
#
# Frames are written on the Pi as .part and renamed into place once complete, so
# a pull can never pick up a half-written JPEG. .part files are excluded anyway.
#
# macOS 26 ships openrsync, which lacks GNU extensions such as --info=. Only
# portable flags are used here; a Homebrew GNU rsync is picked up if installed.

set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
REMOTE_DATA="${REMOTE_DATA:-/home/pi/birdshot-data}"
LOCAL_DATA="${LOCAL_DATA:-$HOME/birdshot-data}"
INTERVAL="${INTERVAL:-4}"

SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
RSYNC="$(command -v /opt/homebrew/bin/rsync || command -v rsync)"

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s==>%s %s\n' "$c_warn" "$c_off" "$*"; }

mkdir -p "$LOCAL_DATA"

pull() {
  "$RSYNC" -a --partial --prune-empty-dirs \
    --exclude '*.part' --exclude '.wallpaper/' \
    --out-format='%n' \
    -e "ssh ${SSH_OPTS[*]}" \
    ${@+"$@"} \
    "$PI_HOST:$REMOTE_DATA/" "$LOCAL_DATA/"
}

# Transferred files only, one per line (rsync also prints bare directories).
transferred() { pull ${@+"$@"} | grep -vE '/$' | grep -E '\.' || true; }

case "${1:-watch}" in
  once)
    say "pulling $PI_HOST:$REMOTE_DATA -> $LOCAL_DATA"
    n="$(transferred | tee /dev/stderr | grep -c . || true)"
    say "$n file(s) transferred"
    ;;

  watch)
    say "watching (every ${INTERVAL}s, ctrl-c to stop)  -> $LOCAL_DATA"
    while true; do
      n="$(transferred | grep -c . || true)"
      [[ "$n" -gt 0 ]] && say "$(date '+%H:%M:%S')  +$n file(s)"
      sleep "$INTERVAL"
    done
    ;;

  live)
    say "live pull, printing each new frame (ctrl-c to stop)"
    while true; do
      transferred | grep -E '\.(jpg|mp4)$' || true
      sleep "$INTERVAL"
    done
    ;;

  mirror)
    say "mirroring with deletes -- local copies removed when the Pi drops them"
    while true; do
      transferred --delete >/dev/null || true
      sleep "$INTERVAL"
    done
    ;;

  latest)
    say "following latest.jpg only -> $LOCAL_DATA/latest.jpg"
    while true; do
      "$RSYNC" -a -e "ssh ${SSH_OPTS[*]}" \
        "$PI_HOST:$REMOTE_DATA/latest.jpg" "$LOCAL_DATA/latest.jpg" 2>/dev/null || true
      sleep 1
    done
    ;;

  verify)
    # Compare both sides properly: not just counts, but per-file size and a
    # content checksum. A sync that silently truncated files would pass a count
    # check and fail this one.
    say "verifying $PI_HOST:$REMOTE_DATA against $LOCAL_DATA"
    remote_list="$(mktemp)"; local_list="$(mktemp)"
    trap 'rm -f "$remote_list" "$local_list"' EXIT

    ssh "${SSH_OPTS[@]}" "$PI_HOST" \
      "cd '$REMOTE_DATA' && find . -type f ! -name '*.part' ! -path './.wallpaper/*' \
       -printf '%s %p\n' | sort -k2" > "$remote_list"
    ( cd "$LOCAL_DATA" 2>/dev/null && find . -type f ! -name '*.part' ! -path './.wallpaper/*' \
       -exec stat -f '%z %N' {} + | sort -k2 ) > "$local_list" 2>/dev/null || true

    rn=$(grep -c . < "$remote_list" || true)
    ln=$(grep -c . < "$local_list" || true)
    printf '    remote files : %s\n    local files  : %s\n' "$rn" "$ln"

    if diff -q "$remote_list" "$local_list" >/dev/null 2>&1; then
      say "every file present with an identical size"
    else
      warn "differences (< remote only, > local only):"
      diff "$remote_list" "$local_list" | head -20 | sed 's/^/    /'
    fi

    # Checksum a sample so we are testing content, not just metadata.
    say "checksumming up to 20 files on both sides"
    sample="$(ssh "${SSH_OPTS[@]}" "$PI_HOST" \
      "cd '$REMOTE_DATA' && find . -type f -name '*.jpg' | sort | head -20")"
    [[ -z "$sample" ]] && { say "no jpgs to compare"; exit 0; }
    rsum="$(ssh "${SSH_OPTS[@]}" "$PI_HOST" \
      "cd '$REMOTE_DATA' && printf '%s\n' '$sample' | xargs -r md5sum | awk '{print \$1}' | sort")"
    lsum="$(cd "$LOCAL_DATA" && printf '%s\n' "$sample" | xargs -r md5 -q 2>/dev/null | sort || true)"
    if [[ "$rsum" == "$lsum" && -n "$lsum" ]]; then
      say "checksums match ($(printf '%s\n' "$sample" | grep -c .) files)"
    else
      warn "CHECKSUM MISMATCH -- content differs between Pi and Mac"
      exit 1
    fi
    ;;

  *)
    sed -n '4,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
esac
