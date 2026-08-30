# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Install checklist -- how is this deployment actually doing?

    birdshot-cli doctor                  human-readable pass/warn/fail table
    birdshot-cli doctor --json           machine-readable, for installers (copal)
    birdshot-cli doctor --write-config   also persist the config it validated

Every check degrades rather than crashes: doctor must run usefully on a bare
Mac checkout with nothing installed, because "what is missing" is exactly what
it exists to report. Exit code is 0 unless something FAILs.
"""

import json
import os
import platform
import shutil
import sys

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "--"

# (module, required, what breaks without it)
MODULES = [
    ("numpy", True, "analysis, exposure, focus -- nothing runs without it"),
    ("picamera2", False, "the Pi camera backend; capture needs it"),
    ("simplejpeg", False, "fast JPEG encode on capture"),
    ("piexif", False, "in-process EXIF (exiftool is the fallback)"),
    ("PyQt5", False, "the GUI; the CLI works without it"),
]

# (binary, required, role)
BINARIES = [
    ("ffmpeg", True, "timelapse assembly"),
    ("rsync", False, "checkout deploys and photo pulls over SSH"),
    ("exiftool", False, "EXIF fallback backend"),
]


def _libc():
    try:
        name, ver = platform.libc_ver()
        if name:
            return "%s %s" % (name, ver)
    except Exception:  # noqa: BLE001
        pass
    # platform.libc_ver() reports nothing on musl; /etc/os-release knows.
    return None


def _os_release():
    info = {}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                info[k] = v.strip('"')
    except OSError:
        pass
    return info


def _check_platform(rows):
    osr = _os_release()
    distro = osr.get("PRETTY_NAME") or platform.platform(terse=True)
    detail = "%s, %s" % (distro, platform.machine())
    libc = _libc()
    if libc:
        detail += ", " + libc
    if osr.get("ID") == "alpine" or "copal" in (osr.get("ID_LIKE", "") + osr.get("ID", "")):
        detail += " (musl -- the copal flagship target; use apk deps, not pip wheels)"
    rows.append((INFO, "platform", detail))

    v = sys.version_info
    status = OK if v >= (3, 9) else FAIL
    rows.append((status, "python", "%d.%d.%d at %s%s"
                 % (v.major, v.minor, v.micro, sys.executable,
                    "" if status == OK else " -- need >= 3.9")))


def _check_modules(rows):
    for name, required, role in MODULES:
        try:
            mod = __import__(name)
            ver = getattr(mod, "__version__", None) or \
                getattr(mod, "QT_VERSION_STR", "") or "present"
            rows.append((OK, name, str(ver)))
        except Exception as exc:  # noqa: BLE001 -- broken installs raise oddly
            status = FAIL if required else WARN
            rows.append((status, name, "not importable (%s) -- %s"
                         % (exc.__class__.__name__, role)))


def _check_binaries(rows):
    for name, required, role in BINARIES:
        path = shutil.which(name)
        if path:
            rows.append((OK, name, path))
        else:
            rows.append((FAIL if required else WARN, name,
                         "not on PATH -- %s" % role))


def _check_cameras(rows):
    try:
        from birdshot import backends
        cams = backends.list_cameras()
    except Exception as exc:  # noqa: BLE001 -- numpy missing, most likely
        rows.append((WARN, "cameras", "enumeration failed: %r" % exc))
        return
    real = [c for c in cams if c["backend"] != "synthetic"]
    for i, c in enumerate(real):
        rows.append((OK, "camera[%d]" % i, "%s %s (%s)"
                     % (c["model"], c["id"], c["backend"])))
    if not real:
        detail = "no hardware camera; the synthetic backend will drive the GUI"
        if sys.platform.startswith("linux"):
            try:
                nodes = sorted(d for d in os.listdir("/dev") if d.startswith("video"))
            except OSError:
                nodes = []
            if nodes:
                detail += " (/dev/%s present but unclaimed)" % ", /dev/".join(nodes)
        rows.append((WARN, "cameras", detail))


def _check_storage(rows, cfg):
    for key in ("data_root", "usb_root"):
        path = cfg.get(key) if hasattr(cfg, "get") else cfg[key]
        if not path:
            rows.append((WARN, key, "not configured"))
            continue
        if not os.path.isdir(path):
            # data_root is created on first capture; absence is not an error.
            rows.append((WARN, key, "%s does not exist yet" % path))
            continue
        try:
            free_gb = shutil.disk_usage(path).free / 1e9
        except OSError as exc:
            rows.append((FAIL, key, "%s: %s" % (path, exc)))
            continue
        writable = os.access(path, os.W_OK)
        rows.append((OK if writable else FAIL, key, "%s, %.1f GB free%s"
                     % (path, free_gb, "" if writable else ", NOT writable")))


def _check_config(rows, cfg):
    path = getattr(cfg, "path", None)
    exists = bool(path) and os.path.exists(path)
    rows.append((OK if exists else WARN, "config",
                 ("%s" % path) if exists else
                 "no settings file yet (defaults in effect%s)"
                 % (", would be %s" % path if path else "")))
    try:
        from birdshot.config import CAPTURE_MODES
        mode = cfg["capture_mode"]
        if not 0 <= int(mode) < len(CAPTURE_MODES):
            rows.append((FAIL, "capture_mode", "%r out of range" % (mode,)))
    except Exception as exc:  # noqa: BLE001
        rows.append((WARN, "capture_mode", "unreadable: %r" % (exc,)))


def collect(cfg):
    """Every check, as (status, name, detail) rows -- the GUI's health panel
    and :func:`run` both read from here so they can never disagree."""
    rows = []
    _check_platform(rows)
    _check_modules(rows)
    _check_binaries(rows)
    _check_cameras(rows)
    _check_storage(rows, cfg)
    _check_config(rows, cfg)
    return rows


def run(cfg, as_json=False, write_config=False):
    rows = collect(cfg)

    if write_config:
        try:
            cfg.save()
            rows.append((OK, "write-config", getattr(cfg, "path", "saved")))
        except Exception as exc:  # noqa: BLE001
            rows.append((FAIL, "write-config", repr(exc)))

    failed = [n for s, n, _ in rows if s == FAIL]
    warned = [n for s, n, _ in rows if s == WARN]

    if as_json:
        from birdshot import __version__
        print(json.dumps({
            "version": __version__,
            "checks": [{"status": s, "name": n, "detail": d} for s, n, d in rows],
            "failed": failed, "warned": warned, "ok": not failed,
        }, indent=2))
    else:
        print("=== birdshot doctor ===")
        for status, name, detail in rows:
            print("  %-4s  %-14s %s" % (status, name, detail))
        print()
        if failed:
            print("FAIL: %s" % ", ".join(failed))
        elif warned:
            print("usable with gaps: %s" % ", ".join(warned))
        else:
            print("everything present")
    return 1 if failed else 0
