# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Camera backends -- the split that lets birdshot run off the Pi.

An *engine* is what the GUI and CLI drive: a Thread with ``send(cmd, **kw)``,
a ``.state`` property, ``shutdown()``/``join()``, reporting through an
``on_event(name, payload)`` callback. The protocol is defined by
:class:`birdshot.camera.CameraEngine` (the original and reference
implementation) and every backend must honour it -- same commands, same event
names, same payload shapes -- so nothing above this package knows which one
it got.

Backends today:

    picamera2   the instrument: IMX477 through libcamera on the Pi
    opencv      any webcam OpenCV can open -- AVFoundation on macOS, V4L2 on
                Linux. The device owns exposure; our gates still judge frames.
    synthetic   a generated sky-and-bird scene (numpy only). Runs the *real*
                analysis gates and the real EV-space AE loop against a scene
                that actually responds to exposure -- so GUI, metering and
                exposure work can all be developed on any machine.

``create_engine`` picks by ``cfg["backend"]``: ``auto`` prefers the Pi camera
and falls back to synthetic. ``auto`` deliberately never opens a webcam --
lighting up a camera the user did not pick is not this program's call; the
GUI's selector (or ``--backend opencv``) is how a webcam gets chosen.
"""

import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional


def picamera2_available() -> "tuple[bool, Optional[str]]":
    """(available, reason-if-not) for the real camera stack."""
    from birdshot import camera
    return camera.HAVE_PICAMERA2, camera.PICAMERA2_ERROR


def _opencv_cameras() -> List[Dict[str, Any]]:
    """Webcams OpenCV could open, enumerated *without* opening any of them --
    opening probes would light camera LEDs and trigger permission dialogs."""
    try:
        import cv2  # noqa: F401 -- no cv2, no opencv backend to offer
    except ImportError:
        return []
    cams: List[Dict[str, Any]] = []
    if sys.platform == "darwin":
        # system_profiler knows the AVFoundation device list by name, in the
        # same order OpenCV indexes it, and asking costs no permission.
        try:
            import json as _json
            out = subprocess.run(
                ["system_profiler", "SPCameraDataType", "-json"],
                capture_output=True, timeout=10)
            for i, item in enumerate(
                    _json.loads(out.stdout or b"{}").get("SPCameraDataType", [])):
                cams.append({"backend": "opencv", "index": i,
                             "model": item.get("_name", "Camera %d" % i),
                             "id": "avfoundation:%d" % i})
        except Exception:  # noqa: BLE001 -- profiler missing or odd output
            pass
    elif sys.platform.startswith("linux"):
        import os
        try:
            nodes = sorted(d for d in os.listdir("/dev") if d.startswith("video")
                           and d[5:].isdigit())
        except OSError:
            nodes = []
        for d in nodes:
            n = int(d[5:])
            name = "V4L2 device %d" % n
            try:
                with open("/sys/class/video4linux/%s/name" % d) as fh:
                    name = fh.read().strip() or name
            except OSError:
                pass
            cams.append({"backend": "opencv", "index": n,
                         "model": name, "id": "v4l2:/dev/%s" % d})
    return cams


def list_cameras() -> List[Dict[str, Any]]:
    """Every camera an engine could open on this machine.

    Real cameras first (Pi camera, then webcams), the synthetic scene always
    last -- so index 0 is always the best available and the list is never
    empty. This is the list the GUI's camera selector shows.
    """
    cams: List[Dict[str, Any]] = []
    try:
        from picamera2 import Picamera2
        for i, info in enumerate(Picamera2.global_camera_info()):
            cams.append({
                "backend": "picamera2", "index": i,
                "model": info.get("Model", "?"), "id": info.get("Id", ""),
            })
    except Exception:  # noqa: BLE001 -- no stack, or a broken one: same answer
        pass
    cams.extend(_opencv_cameras())
    cams.append({
        "backend": "synthetic", "index": 0,
        "model": "Synthetic sky (demo scene)", "id": "synthetic:0",
    })
    return cams


def resolve_choice(cfg) -> "tuple[str, int]":
    """What ``create_engine`` would actually pick right now: (backend, index)
    with ``auto`` resolved. The GUI selector uses this to mark the current
    device."""
    choice = (cfg.get("backend") or "auto").strip().lower()
    idx = int(cfg.get("camera_index", 0) or 0)
    if choice == "auto":
        ok, _ = picamera2_available()
        choice = "picamera2" if ok else "synthetic"
    if choice == "synthetic":
        idx = 0
    return choice, idx


def warm_up(cfg) -> None:
    """Call on the MAIN thread before starting an opencv engine on macOS.

    The first camera access makes the OS show its consent dialog, and that
    request can only be serviced from the main thread's run loop -- an engine
    thread just fails with "not authorized". A brief open-and-release here
    lets the OS ask the user now; once granted (it persists per app), the
    engine's own open succeeds from its thread.
    """
    choice, idx = resolve_choice(cfg)
    if choice != "opencv" or sys.platform != "darwin":
        return
    try:
        import cv2
        cap = cv2.VideoCapture(idx)
        cap.release()
    except Exception:  # noqa: BLE001 -- the engine will report properly
        pass


def create_engine(cfg, storage, on_event: Callable[[str, Dict[str, Any]], None],
                  backend: Optional[str] = None):
    """Build the right engine for this machine.

    ``backend`` overrides ``cfg["backend"]``; both default to ``auto``.
    Forcing ``picamera2`` off the Pi raises rather than silently substituting
    -- a capture appliance must never *think* it is capturing.
    """
    choice = (backend or cfg.get("backend") or "auto").strip().lower()

    if choice in ("auto", "picamera2"):
        from birdshot.camera import CameraEngine, HAVE_PICAMERA2, PICAMERA2_ERROR
        if HAVE_PICAMERA2:
            return CameraEngine(cfg, storage, on_event)
        if choice == "picamera2":
            raise RuntimeError("backend 'picamera2' requested but unavailable: %s"
                               % PICAMERA2_ERROR)

    if choice == "opencv":
        from birdshot.backends.opencv import OpenCVEngine
        return OpenCVEngine(cfg, storage, on_event)

    if choice in ("auto", "synthetic"):
        from birdshot.backends.synthetic import SyntheticEngine
        return SyntheticEngine(cfg, storage, on_event)

    raise ValueError("unknown backend %r (want auto, picamera2, opencv or "
                     "synthetic)" % choice)
