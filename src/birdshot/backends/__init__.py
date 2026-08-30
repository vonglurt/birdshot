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
    synthetic   a generated sky-and-bird scene (numpy only). Runs the *real*
                analysis gates and the real EV-space AE loop against a scene
                that actually responds to exposure -- so GUI, metering and
                exposure work can all be developed on any machine.

``create_engine`` picks by ``cfg["backend"]``: ``auto`` prefers the real
camera and falls back to synthetic; naming a backend forces it.
"""

from typing import Any, Callable, Dict, List, Optional


def picamera2_available() -> "tuple[bool, Optional[str]]":
    """(available, reason-if-not) for the real camera stack."""
    from birdshot import camera
    return camera.HAVE_PICAMERA2, camera.PICAMERA2_ERROR


def list_cameras() -> List[Dict[str, Any]]:
    """Every camera an engine could open on this machine.

    Real cameras first, the synthetic scene always last -- so index 0 is
    always the best available and the list is never empty. This is the list
    the GUI's camera selector shows.
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
    cams.append({
        "backend": "synthetic", "index": 0,
        "model": "Synthetic sky (generated test scene)", "id": "synthetic:0",
    })
    return cams


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

    if choice in ("auto", "synthetic"):
        from birdshot.backends.synthetic import SyntheticEngine
        return SyntheticEngine(cfg, storage, on_event)

    raise ValueError("unknown backend %r (want auto, picamera2 or synthetic)"
                     % choice)
