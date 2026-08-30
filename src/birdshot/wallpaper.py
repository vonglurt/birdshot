# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Keep the Pi's desktop wallpaper showing the live camera view.

The old runCam.sh trick was ``pcmanfm --set-wallpaper /home/pi/sauto/latest.png``
after every still. It mostly did not refresh, because pcmanfm caches the
wallpaper by path -- setting the same path twice is a no-op even when the file
underneath has changed.

The fix is to alternate between two filenames, so every update is a genuinely
new path as far as pcmanfm is concerned.

    birdshot-wallpaper                 follow latest.jpg, update every 2s
    birdshot-wallpaper --interval 5
    birdshot-wallpaper --once

Note this is the low-fidelity option. It shows the 320x240 preview scaled to
your screen, so it is fine for "is the camera still pointed at the tree" but not
for judging focus. For focus use the GUI's focus monitor, which shows real 1:1
sensor pixels with peaking and a peak-hold score.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

from birdshot.config import Config


def set_wallpaper(path: str) -> bool:
    """Try the desktop helpers this Pi image is likely to have, in order."""
    attempts = [
        ["pcmanfm", "--set-wallpaper", path],
        ["pcmanfm", "-w", path],
        ["feh", "--bg-max", path],
        ["xwallpaper", "--zoom", path],
    ]
    for cmd in attempts:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            if subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--source", help="image to follow (default <data_root>/latest.jpg)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = Config(args.config) if args.config else Config()
    source = args.source or os.path.join(cfg["data_root"], "latest.jpg")
    scratch = os.path.join(cfg["data_root"], ".wallpaper")
    os.makedirs(scratch, exist_ok=True)

    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", os.path.expanduser("~/.Xauthority"))

    if not os.path.exists(source):
        print("waiting for %s -- start a capture or the GUI first" % source)

    slot = 0
    last_mtime = 0.0
    while True:
        try:
            mtime = os.path.getmtime(source)
        except OSError:
            mtime = 0.0

        if mtime > last_mtime:
            last_mtime = mtime
            slot ^= 1
            # Alternating destination defeats pcmanfm's path-keyed cache.
            dest = os.path.join(scratch, "live_%d.jpg" % slot)
            try:
                shutil.copyfile(source, dest)
            except OSError as exc:
                print("copy failed: %s" % exc, file=sys.stderr)
            else:
                if not set_wallpaper(dest):
                    print("no working wallpaper setter found "
                          "(tried pcmanfm, feh, xwallpaper)", file=sys.stderr)
                    return 1

        if args.once:
            return 0
        time.sleep(max(0.2, args.interval))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
