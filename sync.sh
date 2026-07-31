#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
# Two-way source sync between this Mac and the Pi, plus deployment.
#
#   ./sync.sh push          Mac  -> Pi   (source only)
#   ./sync.sh pull          Pi   -> Mac  (source only)
#   ./sync.sh sync          both ways, newer file wins
#   ./sync.sh status        dry run, show what each direction would do
#   ./sync.sh watch         auto-push on every local save (needs fswatch)
#   ./sync.sh deploy        push + install launcher + run the selftest
#   ./sync.sh selftest      run the on-Pi selftest
#   ./sync.sh gui           start the GUI on the Pi's display
#   ./sync.sh logs          tail the GUI log
#
#   ./sync.sh install-launchers          desktop icons + Pi menu entries
#   ./sync.sh install-autostart          launch birdshot maximized at every login
#   ./sync.sh remove-autostart           stop doing that
#   ./sync.sh autowrite status           is an autowrite.yes stick present?
#   ./sync.sh autowrite enable  <mount> [key=value ...]
#   ./sync.sh autowrite disable <mount>
#   ./sync.sh ramdisk [status|on|off]    stage captures in RAM instead of eMMC
#
# "sync" is a convenience, not a conflict-resolving sync engine: it runs rsync
# --update in both directions, so the newer mtime wins per file and deletions
# are never propagated. Edit on one side at a time and it behaves predictably.
# When in doubt run "status" first.

set -euo pipefail

PI_HOST="${PI_HOST:-pi@raspberrypi.local}"
REMOTE_DIR="${REMOTE_DIR:-/home/pi/birdshot}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
EXCLUDES=(
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store'
  --exclude '.git/' --exclude '*.swp' --exclude '.venv/'
)

# macOS 26 ships openrsync (protocol 29), which lacks GNU extensions such as
# --info=. Prefer a Homebrew GNU rsync when present, but stay on the portable
# flag set either way so both work.
RSYNC="$(command -v /opt/homebrew/bin/rsync || command -v rsync)"

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s==>%s %s\n' "$c_warn" "$c_off" "$*"; }
die()  { printf '%s==>%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

require_host() {
  ssh "${SSH_OPTS[@]}" "$PI_HOST" true 2>/dev/null \
    || die "cannot reach $PI_HOST over ssh"
}

# Note: macOS ships bash 3.2, where "${arr[@]}" on an empty array trips set -u.
# The ${arr[@]+"${arr[@]}"} form is the portable way to expand to nothing.
do_push() {
  "$RSYNC" -az --out-format='%n' "${EXCLUDES[@]}" ${@+"$@"} \
    -e "ssh ${SSH_OPTS[*]}" \
    "$LOCAL_DIR/" "$PI_HOST:$REMOTE_DIR/"
}

do_pull() {
  "$RSYNC" -az --out-format='%n' "${EXCLUDES[@]}" ${@+"$@"} \
    -e "ssh ${SSH_OPTS[*]}" \
    "$PI_HOST:$REMOTE_DIR/" "$LOCAL_DIR/"
}

# Run a transfer and report how many files actually moved.
report() {
  local label="$1"; shift
  local out n
  out="$("$@" | grep -vE '^(\./)?$|/$' || true)"
  n="$(printf '%s' "$out" | grep -c . || true)"
  if [[ "$n" -gt 0 ]]; then
    printf '%s\n' "$out" | sed 's/^/    /'
    say "$label: $n file(s)"
  else
    say "$label: already up to date"
  fi
}

cmd_push() {
  require_host
  say "push  $LOCAL_DIR -> $PI_HOST:$REMOTE_DIR"
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "mkdir -p '$REMOTE_DIR'"
  report "pushed" do_push
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "chmod +x '$REMOTE_DIR'/bin/* 2>/dev/null || true"
}

cmd_pull() {
  require_host
  say "pull  $PI_HOST:$REMOTE_DIR -> $LOCAL_DIR"
  report "pulled" do_pull
}

cmd_sync() {
  require_host
  say "two-way sync (newer wins, no deletions)"
  report "Mac -> Pi" do_push --update
  report "Pi -> Mac" do_pull --update
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "chmod +x '$REMOTE_DIR'/bin/* 2>/dev/null || true"
}

cmd_status() {
  require_host
  say "would push (Mac -> Pi):"
  do_push --dry-run --itemize-changes | sed 's/^/    /'
  say "would pull (Pi -> Mac):"
  do_pull --dry-run --itemize-changes | sed 's/^/    /'
}

cmd_watch() {
  command -v fswatch >/dev/null || die "fswatch not found -- brew install fswatch"
  require_host
  cmd_push
  say "watching $LOCAL_DIR for changes (ctrl-c to stop)"
  fswatch -o -e '__pycache__' -e '\.pyc$' -e '\.git' "$LOCAL_DIR" | while read -r _; do
    printf '\n'; say "change detected"
    do_push || warn "push failed, will retry on next change"
    ssh "${SSH_OPTS[@]}" "$PI_HOST" "chmod +x '$REMOTE_DIR'/bin/* 2>/dev/null || true"
  done
}

cmd_deploy() {
  cmd_push
  say "installing launcher and data directories"
  ssh "${SSH_OPTS[@]}" "$PI_HOST" bash -s <<REMOTE
set -e
mkdir -p ~/birdshot-data ~/.config/birdshot ~/.local/share/applications
cat > ~/.local/share/applications/birdshot.desktop <<'DESK'
[Desktop Entry]
Type=Application
Name=birdshot
Comment=IMX477 bird and sky capture
Exec=$REMOTE_DIR/bin/birdshot-gui
Icon=camera-photo
Terminal=false
Categories=Graphics;Photography;
DESK
chmod +x $REMOTE_DIR/bin/* 2>/dev/null || true
# A convenience symlink so 'birdshot-cli' works from any shell on the Pi.
mkdir -p ~/.local/bin
ln -sf $REMOTE_DIR/bin/birdshot-cli ~/.local/bin/birdshot-cli
ln -sf $REMOTE_DIR/bin/birdshot-gui ~/.local/bin/birdshot-gui
echo "installed to $REMOTE_DIR"
REMOTE
  cmd_install_launchers
  say "running selftest"
  cmd_selftest
}

cmd_install_launchers() {
  require_host
  say "installing desktop icons and menu entries"
  ssh "${SSH_OPTS[@]}" "$PI_HOST" bash -s <<REMOTE
set -e
DESKTOP="\$(xdg-user-dir DESKTOP 2>/dev/null || echo \$HOME/Desktop)"
mkdir -p "\$DESKTOP" ~/.local/share/applications

make_entry() {
  # \$1 file  \$2 name  \$3 args  \$4 comment  \$5 icon
  cat > "\$1" <<DESK
[Desktop Entry]
Type=Application
Version=1.0
Name=\$2
GenericName=Camera capture
Comment=\$4
Exec=$REMOTE_DIR/bin/birdshot-gui \$3
Path=$REMOTE_DIR
Icon=\$5
Terminal=false
Categories=Graphics;Photography;AudioVideo;
Keywords=camera;bird;imx477;capture;
StartupNotify=true
DESK
  chmod +x "\$1"
}

# Desktop icons
make_entry "\$DESKTOP/birdshot.desktop"       "birdshot"           ""                 "IMX477 bird capture" camera-photo
make_entry "\$DESKTOP/birdshot-auto.desktop"  "birdshot (AUTO)"    "--auto"           "Capture straight to an autowrite.yes USB stick, no clicks" media-record
make_entry "\$DESKTOP/birdshot-focus.desktop" "birdshot (Focus)"   "--tab Focus"      "Open on the focus tools for setting the lens" zoom-in

# Application menu entries (same files, menu location)
make_entry ~/.local/share/applications/birdshot.desktop      "birdshot"        ""       "IMX477 bird capture" camera-photo
make_entry ~/.local/share/applications/birdshot-auto.desktop "birdshot (AUTO)" "--auto" "Capture straight to an autowrite.yes USB stick, no clicks" media-record

# PCManFM on this image wants desktop files marked trusted, or it shows a
# "this file is not executable" prompt on every launch.
if command -v gio >/dev/null 2>&1; then
  for f in "\$DESKTOP"/birdshot*.desktop; do gio set "\$f" metadata::trusted true 2>/dev/null || true; done
fi
update-desktop-database ~/.local/share/applications 2>/dev/null || true

echo "desktop icons in \$DESKTOP:"
ls -1 "\$DESKTOP"/birdshot*.desktop
echo "menu entries:"
ls -1 ~/.local/share/applications/birdshot*.desktop
REMOTE
  say "three desktop icons: birdshot, birdshot (AUTO), birdshot (Focus)"
  say "and birdshot / birdshot (AUTO) under Graphics in the menu"
}

cmd_install_autostart() {
  require_host
  say "installing desktop autostart (runs at login, which is automatic)"
  ssh "${SSH_OPTS[@]}" "$PI_HOST" bash -s <<REMOTE
set -e
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/birdshot.desktop <<'DESK'
[Desktop Entry]
Type=Application
Name=birdshot
Comment=IMX477 bird capture - autostarts, honours autowrite.yes USB sticks
Exec=$REMOTE_DIR/bin/birdshot-gui --auto
Icon=camera-photo
Terminal=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
DESK
echo "installed ~/.config/autostart/birdshot.desktop"
REMOTE
  say "birdshot will now launch maximized at login"
  say "put an empty file called autowrite.yes on a USB stick for unattended capture"
}

cmd_remove_autostart() {
  require_host
  ssh "${SSH_OPTS[@]}" "$PI_HOST" \
    "rm -f ~/.config/autostart/birdshot.desktop && echo removed"
  say "autostart disabled"
}

cmd_autowrite() {
  require_host
  local action="${1:-status}"
  case "$action" in
    status)
      ssh "${SSH_OPTS[@]}" "$PI_HOST" \
        "cd '$REMOTE_DIR' && PYTHONPATH=src python3 -c \"
from birdshot import autostart
f = autostart.detect()
print(autostart.MARKER + ' found on ' + f['mount'] if f else 'no ' + autostart.MARKER + ' on any mounted volume')
if f:
    print('  options:', f['options'] or '(defaults)')
    for w in f.get('warnings') or []: print('  WARNING:', w)
\""
      ;;
    enable)
      local mount="${2:-}"
      [[ -n "$mount" ]] || die "usage: $0 autowrite enable /media/pi/STICK [key=value ...]"
      shift 2
      local body=""
      for kv in "$@"; do body+="$kv"$'\n'; done
      ssh "${SSH_OPTS[@]}" "$PI_HOST" \
        "printf '%s' '$body' > '$mount/autowrite.yes' && \
         echo wrote '$mount/autowrite.yes' && cat '$mount/autowrite.yes'"
      ;;
    disable)
      local mount="${2:-}"
      [[ -n "$mount" ]] || die "usage: $0 autowrite disable /media/pi/STICK"
      ssh "${SSH_OPTS[@]}" "$PI_HOST" "rm -f '$mount/autowrite.yes' && echo removed"
      ;;
    *) die "unknown autowrite action: $action" ;;
  esac
}

cmd_ramdisk() {
  require_host
  local action="${1:-status}"
  case "$action" in
    status)
      ssh "${SSH_OPTS[@]}" "$PI_HOST" "cd '$REMOTE_DIR' && PYTHONPATH=src python3 -c \"
from birdshot.config import Config
c = Config()
r = c['data_root']
print('capture root      :', r)
print('on a ramdisk      :', 'yes' if r.startswith('/dev/shm') else 'no')
print('continuous offload:', c['offload_continuous'], 'every', c['offload_interval_s'], 's')
print('free after copy   :', c['offload_delete_source'])
\"; echo; df -h /dev/shm | tail -1"
      ;;
    on)
      # /dev/shm is already a 1.9 GB tmpfs on this image, so there is nothing to
      # create -- capture just points at it. Deleting after a verified copy is
      # mandatory here, otherwise RAM fills and capture stops.
      ssh "${SSH_OPTS[@]}" "$PI_HOST" "cd '$REMOTE_DIR' && PYTHONPATH=src python3 -c \"
from birdshot.config import Config
c = Config()
c['data_root'] = '/dev/shm/birdshot'
c['offload_continuous'] = True
c['offload_interval_s'] = 10
c['offload_delete_source'] = True
c['min_free_mb'] = 300
c.save()
print('capture root now', c['data_root'])
\" && mkdir -p /dev/shm/birdshot"
      warn "RAM staging is on. Capture is NOT faster -- the eMMC already runs at"
      warn "60 MB/s against a peak capture rate of about 12 MB/s. What this buys"
      warn "you is no eMMC wear and no disk jitter."
      warn "Frames are deleted after a verified copy, so the offload target must"
      warn "keep up: over gigabit to the Mac it will, to the 12 MB/s USB stick it"
      warn "will not, and capture will stop when the 1.9 GB fills."
      warn "Nothing survives a power cut. Use ./sync.sh ramdisk off to revert."
      ;;
    off)
      ssh "${SSH_OPTS[@]}" "$PI_HOST" "cd '$REMOTE_DIR' && PYTHONPATH=src python3 -c \"
from birdshot.config import Config
import os
c = Config()
c['data_root'] = os.path.expanduser('~/birdshot-data')
c['offload_delete_source'] = False
c['min_free_mb'] = 2048
c.save()
print('capture root back to', c['data_root'])
\""
      say "back to the eMMC"
      ;;
    *) die "usage: $0 ramdisk [status|on|off]" ;;
  esac
}

cmd_selftest() {
  require_host
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "cd '$REMOTE_DIR' && python3 bin/birdshot-cli selftest"
}

cmd_info() {
  require_host
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "cd '$REMOTE_DIR' && python3 bin/birdshot-cli info"
}

cmd_gui() {
  require_host
  say "starting GUI on the Pi's display (:0)"
  ssh "${SSH_OPTS[@]}" "$PI_HOST" \
    "cd '$REMOTE_DIR' && DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority \
     nohup python3 bin/birdshot-gui > /tmp/birdshot-gui.log 2>&1 & echo started"
  sleep 2
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "tail -20 /tmp/birdshot-gui.log" || true
}

cmd_stop_gui() {
  require_host
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "pkill -f birdshot-gui || true; echo stopped"
}

cmd_logs() {
  require_host
  ssh "${SSH_OPTS[@]}" "$PI_HOST" "tail -f /tmp/birdshot-gui.log"
}

case "${1:-help}" in
  push)     cmd_push ;;
  pull)     cmd_pull ;;
  sync)     cmd_sync ;;
  status)   cmd_status ;;
  watch)    cmd_watch ;;
  deploy)   cmd_deploy ;;
  selftest) cmd_selftest ;;
  info)     cmd_info ;;
  gui)      cmd_gui ;;
  stop-gui) cmd_stop_gui ;;
  logs)     cmd_logs ;;
  install-launchers) cmd_install_launchers ;;
  install-autostart) cmd_install_autostart ;;
  remove-autostart)  cmd_remove_autostart ;;
  autowrite) shift; cmd_autowrite "$@" ;;
  ramdisk)   shift; cmd_ramdisk "$@" ;;
  *)        sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
