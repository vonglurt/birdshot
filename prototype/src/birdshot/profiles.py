# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Named settings profiles: save a whole setup, activate it in one gesture.

A profile is a snapshot of the tunables -- exposure, gates, modes, the
selected camera, the Bird Flight thresholds, all of it -- minus what belongs
to the *machine* rather than the way you shoot:

    version, state, calibration     bookkeeping and measured light, not taste
    ui_face                         deploy policy (see gui/faces.py)
    data_root, usb_root,            where captures live on THIS box; a
    cascade_tiers, min_free_mb      profile must never move your data

So "dawn treeline", "webcam desk test" and "field bird watch" can carry
different cameras, shutters and gates, while activating one on a different
install never points capture at a path that does not exist there.

Profiles live as JSON files next to the settings file
(``~/.config/birdshot/profiles/<name>.json``), so a scratch config gets its
own scratch profiles and the deployed Pi keeps its own set. The GUI's
activator row (Bench, under the camera picker) and ``birdshot-cli profiles``
both drive these functions; ``birdshot-cli --profile <name> <command>``
activates one for a single headless run without persisting anything.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List

from birdshot.config import DEFAULTS

# What never rides a profile (rationale in the module docstring).
MACHINE_KEYS = frozenset({
    "version", "state", "calibration", "ui_face",
    "data_root", "usb_root", "cascade_tiers", "min_free_mb",
})

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$")


def _dir(cfg) -> str:
    base = os.path.dirname(getattr(cfg, "path", "") or "") or "."
    return os.path.join(base, "profiles")


def _path(cfg, name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError("profile names are 1-40 letters, digits, dots, "
                         "dashes, underscores or spaces (got %r)" % (name,))
    return os.path.join(_dir(cfg), "%s.json" % name)


def snapshot(cfg) -> Dict[str, Any]:
    """The profile-worthy slice of the current settings."""
    data = cfg.as_dict()
    return {k: v for k, v in data.items()
            if k not in MACHINE_KEYS and k in DEFAULTS}


def list_profiles(cfg) -> List[Dict[str, Any]]:
    """Saved profiles, alphabetically: {name, path, saved}."""
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(_dir(cfg)))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        path = os.path.join(_dir(cfg), fn)
        entry = {"name": fn[:-5], "path": path, "saved": None}
        try:
            with open(path) as fh:
                entry["saved"] = (json.load(fh) or {}).get("saved")
        except (OSError, ValueError):
            pass   # still listed; apply() will say what is wrong with it
        out.append(entry)
    return out


def save(cfg, name: str) -> str:
    """Persist the current settings under ``name`` (atomic write)."""
    path = _path(cfg, name)
    os.makedirs(_dir(cfg), exist_ok=True)
    payload = json.dumps({"name": name, "saved": time.time(),
                          "settings": snapshot(cfg)},
                         indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=_dir(cfg), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(cfg, name: str) -> Dict[str, Any]:
    with open(_path(cfg, name)) as fh:
        data = json.load(fh)
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("%s carries no settings" % name)
    return settings


def apply(cfg, name: str, save_config: bool = True) -> List[str]:
    """Activate a profile: write its values into the live config.

    Machine keys are dropped even if an old or hand-edited file carries
    them, and keys a profile predates keep their current values. Returns
    the keys whose value actually changed, so callers can say what moved.
    """
    settings = load(cfg, name)
    changed: List[str] = []
    for key, value in settings.items():
        if key in MACHINE_KEYS or key not in DEFAULTS:
            continue
        if cfg.get(key) != value:
            cfg[key] = copy.deepcopy(value)
            changed.append(key)
    if save_config and changed:
        cfg.save()
    return sorted(changed)


def delete(cfg, name: str) -> None:
    os.unlink(_path(cfg, name))
