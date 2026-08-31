# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Assemble captured stills into a movie with ffmpeg.

Frames are selected from a session's ``index.jsonl`` so the quality gates carry
through -- by default only frames that passed (verdict ``ok``) are included,
which quietly drops the dark/blown/empty ones that would otherwise flicker
through the finished timelapse.

Encoding 12.3 MP stills on the CM4 is slow (the Pi has no hardware H.264 encoder
usable from ffmpeg here, so it is libx264 on four Cortex-A72 cores). The same
function runs far faster on the Mac -- see ``mac/assemble.sh`` -- so the GUI
offers both and defaults to whichever the user picked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

_IMG_RE = re.compile(r".*\.jpe?g$", re.I)


def frames_from_index(session_dir: str, only_ok: bool = True) -> List[str]:
    """Ordered list of frame paths for a session, from its index."""
    index = os.path.join(session_dir, "index.jsonl")
    frames: List[str] = []
    if os.path.exists(index):
        with open(index, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                rel = entry.get("file")
                if not rel:
                    continue
                if only_ok and entry.get("verdict", "ok") != "ok":
                    continue
                path = os.path.join(session_dir, rel)
                if os.path.exists(path):
                    frames.append(path)
    if frames:
        return frames
    # No index (an old runCam.sh folder, say) -- fall back to sorted filenames,
    # which the timestamp-first naming makes chronological anyway.
    for root, _dirs, files in os.walk(session_dir):
        if os.path.basename(root).startswith("_"):
            continue
        for name in sorted(files):
            if _IMG_RE.match(name):
                frames.append(os.path.join(root, name))
    return sorted(frames)


def frames_from_folder(path: str, recursive: bool = True) -> List[str]:
    """Every image in a folder, in filename order.

    Used by the encode tab for arbitrary sources -- rapid-capture runs, the old
    runCam.sh ``s191``/``sauto`` folders, or anything else. The timestamp-first
    filenames used everywhere here sort chronologically.
    """
    out: List[str] = []
    if not os.path.isdir(path):
        return out
    if recursive:
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("_"))
            for name in sorted(files):
                if _IMG_RE.match(name):
                    out.append(os.path.join(root, name))
    else:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isfile(full) and _IMG_RE.match(name):
                out.append(full)
    return out


def select_frames(path: str, only_ok: bool = True, recursive: bool = True) -> List[str]:
    """Index-driven selection when the folder has one, plain listing otherwise."""
    if os.path.exists(os.path.join(path, "index.jsonl")):
        frames = frames_from_index(path, only_ok=only_ok)
        if frames:
            return frames
    return frames_from_folder(path, recursive=recursive)


def has_index(path: str) -> bool:
    return os.path.exists(os.path.join(path, "index.jsonl"))


def assemble(
    frames: List[str],
    output: str,
    fps: int = 60,
    width: Optional[int] = None,
    crf: int = 18,
    preset: str = "veryfast",
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Build ``output`` from ``frames`` at ``fps``.

    Uses a scratch directory of sequentially numbered symlinks so ffmpeg can use
    its fast numbered-sequence reader regardless of the real filenames, and so
    no image data is copied.
    """
    if not frames:
        return {"ok": False, "error": "no frames to assemble", "frames": 0}

    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="birdshot-tl-", dir=os.path.dirname(os.path.abspath(output)))
    try:
        for i, src in enumerate(frames):
            if cancel is not None and cancel.is_set():
                return {"ok": False, "error": "cancelled", "frames": 0}
            link = os.path.join(scratch, "%08d.jpg" % i)
            try:
                os.symlink(os.path.abspath(src), link)
            except OSError:
                shutil.copy2(src, link)
            if progress and i % 50 == 0:
                progress(i, len(frames))

        vf = "scale=%d:-2" % width if width else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(scratch, "%08d.jpg"),
            "-vf", vf,
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            output,
        ]
        t0 = time.time()
        # -progress writes key=value lines to stdout, which is how we drive a
        # real progress bar instead of an indeterminate spinner.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for raw in proc.stdout:
                if cancel is not None and cancel.is_set():
                    proc.terminate()
                    return {"ok": False, "error": "cancelled", "frames": len(frames)}
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("frame=") and progress:
                    try:
                        progress(int(line.split("=", 1)[1]), len(frames))
                    except ValueError:
                        pass
        finally:
            proc.stdout.close()
            stderr = proc.stderr.read().decode("utf-8", "replace")
            proc.stderr.close()
            proc.wait()

        if proc.returncode != 0:
            return {"ok": False, "error": stderr[-500:] or "ffmpeg failed",
                    "frames": len(frames)}
        if progress:
            progress(len(frames), len(frames))
        return {
            "ok": True,
            "output": output,
            "frames": len(frames),
            "fps": fps,
            "seconds": round(len(frames) / float(fps), 2),
            "elapsed": round(time.time() - t0, 1),
            "bytes": os.path.getsize(output) if os.path.exists(output) else 0,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


class AssembleJob(threading.Thread):
    """Run :func:`assemble` off the GUI thread."""

    def __init__(self, session_dir: str, output: str, fps: int = 60,
                 only_ok: bool = True, width: Optional[int] = None,
                 crf: int = 18, preset: str = "veryfast", recursive: bool = True,
                 write_exif: bool = False, cfg=None,
                 on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_progress: Optional[Callable[[int, int], None]] = None,
                 on_stage: Optional[Callable[[str], None]] = None):
        super().__init__(daemon=True, name="assemble")
        self.session_dir = session_dir
        self.output = output
        self.fps = fps
        self.only_ok = only_ok
        self.width = width
        self.crf = crf
        self.preset = preset
        self.recursive = recursive
        self.write_exif = write_exif
        self.cfg = cfg
        self.on_done = on_done
        self.on_progress = on_progress
        self.on_stage = on_stage
        self.cancel = threading.Event()
        self.result: Optional[Dict[str, Any]] = None

    def run(self) -> None:
        # Preprocessing: stamp EXIF into the source frames before encoding, so
        # the stills carry their exposure and scoring wherever they end up.
        exif_result = None
        if self.write_exif and self.cfg is not None:
            from . import exif as exifmod

            if self.on_stage:
                self.on_stage("writing EXIF")
            exif_result = exifmod.tag_session(
                self.session_dir, self.cfg, only_ok=self.only_ok,
                progress=self.on_progress, cancel=self.cancel)

        if self.on_stage:
            self.on_stage("encoding")
        frames = select_frames(self.session_dir, only_ok=self.only_ok,
                               recursive=self.recursive)
        self.result = assemble(
            frames, self.output, fps=self.fps, width=self.width,
            crf=self.crf, preset=self.preset,
            progress=self.on_progress, cancel=self.cancel,
        )
        if exif_result is not None and isinstance(self.result, dict):
            self.result["exif"] = exif_result
        if self.on_done:
            self.on_done(self.result)


def list_sessions(data_root: str) -> List[Dict[str, Any]]:
    """Sessions found under the data root, newest first."""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(data_root):
        return out
    for name in os.listdir(data_root):
        path = os.path.join(data_root, name)
        if not os.path.isdir(path) or name in ("video", "timelapse"):
            continue
        info: Dict[str, Any] = {"id": name, "path": path, "frames": 0, "counts": {}}
        summary = os.path.join(path, "session.json")
        if os.path.exists(summary):
            try:
                with open(summary) as fh:
                    info.update(json.load(fh))
            except (OSError, ValueError):
                pass
        if not info.get("frames"):
            index = os.path.join(path, "index.jsonl")
            if os.path.exists(index):
                try:
                    with open(index) as fh:
                        info["frames"] = sum(1 for _ in fh)
                except OSError:
                    pass
        info["mtime"] = os.path.getmtime(path)
        out.append(info)
    return sorted(out, key=lambda d: d.get("mtime", 0), reverse=True)
