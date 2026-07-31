# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Session layout, the frame index, and background offload to USB.

Layout under ``data_root`` (the eMMC, because it writes at 78 MB/s where the
NTFS USB stick manages about 12):

    birdshot-data/
      sess-1730380000/          one directory per capture run
        ms20/                   one directory per shutter duration, as before
          20260731-142233.417_000042.jpg
        s191/
        index.jsonl             one JSON line per frame: settings + metrics
        session.json            summary written on close
        _rejected/              only when reject_action == "quarantine"
      tlc-1730380000/           timelapse runs
      video/
      timelapse/                assembled movies
      latest.jpg                small preview, cheap for the Mac to poll

The shutter subdirectory naming is the existing ``s<N>``/``ms<N>`` convention
from runCam.sh, so old and new captures sort together.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any, Dict, Optional

from .naming import shutter_dir, timestamp_name


# Below this much free space on the capture tier, writing is genuinely unsafe
# regardless of what the cascade is doing.
HARD_FLOOR_MB = 120.0


def free_mb(path: str) -> float:
    """Free space in MB on the filesystem holding ``path``."""
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        st = os.statvfs(path or "/")
    except OSError:
        return 0.0
    return st.f_bavail * st.f_frsize / (1024.0 * 1024.0)


def _count_images(path: str) -> int:
    """Cheap recursive count of .jpg files, used to gauge offload lag."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".jpg"):
                    total += 1
                elif entry.is_dir() and not entry.name.startswith("_"):
                    total += _count_images(entry.path)
    except OSError:
        return 0
    return total


def _claim_name(directory: str, base: str, ext: str = ".jpg") -> Optional[str]:
    """Atomically claim ``<directory>/<base><ext>``, suffixing on collision.

    O_EXCL rather than check-then-write: several encoder threads finish at once,
    and a existence check would have them all pick the same free name and
    overwrite each other, which silently dropped frames. At centisecond
    resolution the suffix should essentially never be needed.
    """
    os.makedirs(directory, exist_ok=True)
    for n in range(100000):
        candidate = os.path.join(
            directory, base + ext if n == 0 else "%s_%03d%s" % (base, n, ext))
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        except OSError:
            return None
        os.close(fd)
        return candidate
    return None


class Session:
    """One capture run."""

    def __init__(self, root: str, kind: str = "sess"):
        self.kind = kind
        # Same 16-digit centisecond stamp as the frames, so a session folder
        # sorts and reads the same way its contents do. Nothing parses this back
        # out -- sessions are discovered by listing directories -- so older
        # epoch-named folders keep working alongside.
        self.id = "%s-%s" % (kind, timestamp_name())
        self.path = os.path.join(root, self.id)
        self.started = time.time()
        self.frames = 0
        self.counts: Dict[str, int] = {"ok": 0, "dark": 0, "blown": 0, "empty": 0}
        self.bytes = 0
        os.makedirs(self.path, exist_ok=True)
        self._index = open(os.path.join(self.path, "index.jsonl"), "a", buffering=1)
        self._lock = threading.Lock()

        # Group mode (cascade). Frames land in g000001/, g000002/, ... and each
        # is sealed once full so the migrator can move it while capture carries
        # on writing the next one.
        self.group_seq = 0
        self.group_path: Optional[str] = None
        self.group_frames = 0
        self.group_bytes = 0
        self.group_writers = 0

    def record(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self._index.write(json.dumps(entry) + "\n")
            self.frames += 1
            self.bytes += int(entry.get("bytes", 0))
            v = entry.get("verdict", "ok")
            self.counts[v] = self.counts.get(v, 0) + 1

    # -- group mode ------------------------------------------------------
    def start_group(self) -> str:
        from .cascade import group_dir

        self.group_seq += 1
        self.group_path = group_dir(self.path, self.group_seq)
        self.group_frames = 0
        self.group_bytes = 0
        os.makedirs(self.group_path, exist_ok=True)
        return self.group_path

    def acquire_group(self) -> str:
        """Claim the open group for one frame, and count as an active writer.

        Sealing must not race a write: three encoder threads write concurrently
        while the engine thread decides when a group is full. Without this
        counter a frame could land in a directory whose manifest had already
        been written, leaving it unlisted and therefore unverified.
        """
        with self._lock:
            if not self.group_path:
                self.start_group()
            self.group_writers += 1
            return self.group_path

    def release_group(self, nbytes: int) -> None:
        with self._lock:
            self.group_writers = max(0, self.group_writers - 1)
            self.group_frames += 1
            self.group_bytes += nbytes

    def seal_current_group(self) -> Optional[str]:
        """Seal the open group and return its path, or None if there wasn't one."""
        from .cascade import seal_group

        with self._lock:
            path = self.group_path
            if not path:
                return None
            self.group_path = None  # new writers start a fresh group

        # Let the writers already inside this group finish before listing it.
        deadline = time.time() + 10.0
        while time.time() < deadline:
            with self._lock:
                if self.group_writers == 0:
                    break
            time.sleep(0.002)

        seal_group(path, {"session": self.id, "kind": self.kind})
        return path

    def roll_group_if_needed(self, max_frames: int, max_bytes: int) -> Optional[str]:
        """Seal and return the finished group when the current one is full."""
        with self._lock:
            if not self.group_path:
                return None
            full = ((max_frames and self.group_frames >= max_frames)
                    or (max_bytes and self.group_bytes >= max_bytes))
        return self.seal_current_group() if full else None

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "path": self.path,
            "started": self.started,
            "ended": time.time(),
            "frames": self.frames,
            "bytes": self.bytes,
            "counts": dict(self.counts),
        }

    def close(self) -> Dict[str, Any]:
        summary = self.summary()
        try:
            with open(os.path.join(self.path, "session.json"), "w") as fh:
                json.dump(summary, fh, indent=2)
        except OSError:
            pass
        with self._lock:
            try:
                self._index.close()
            except OSError:
                pass
        return summary


class Storage:
    """Owns the data root, the current session and the offload worker."""

    def __init__(self, cfg, on_event=None):
        self.cfg = cfg
        self.session: Optional[Session] = None
        self.cascade = None
        self._on_event = on_event

        if cfg["cascade_enabled"]:
            # In cascade mode the capture root is the top tier, and everything
            # below it is handled by the migrator rather than the offload queue.
            from .cascade import Cascade, build_tiers

            self._tiers = build_tiers(cfg)
            if self._tiers:
                self.root = self._tiers[0].path
                for t in self._tiers:
                    try:
                        t.ensure()
                    except Exception:  # noqa: BLE001
                        pass
                self.cascade = Cascade(self._tiers, cfg, on_event)
                self.cascade.start()
            else:
                self.root = cfg["data_root"]
        else:
            self.root = cfg["data_root"]

        os.makedirs(self.root, exist_ok=True)
        for sub in ("video", "timelapse"):
            os.makedirs(os.path.join(cfg["data_root"], sub), exist_ok=True)
        self._offload = OffloadWorker(cfg)

    def notify_group_sealed(self, path: str) -> None:
        """Capture calls this the moment a group is complete."""
        if self.cascade is not None:
            self.cascade.nudge()

    def cascade_media(self, path: str, kind: str = "media") -> Optional[str]:
        """Hand a finished video or movie to the cascade."""
        if self.cascade is None or not path or not os.path.isfile(path):
            return None
        gdir = __import__("birdshot.cascade", fromlist=["x"]).adopt_file(
            self.cascade.tiers[0].path, path, kind)
        if gdir:
            self.cascade.nudge()
        return gdir

    def media_root(self, sub: str) -> str:
        """Where a video or movie should be written.

        Under the cascade that is the top tier, so the file lands on the fastest
        storage and migrates down afterwards like everything else.
        """
        root = self.cascade.tiers[0].path if self.cascade is not None \
            else self.cfg["data_root"]
        path = os.path.join(root, sub)
        os.makedirs(path, exist_ok=True)
        return path

    def cascade_status(self) -> Optional[Dict[str, Any]]:
        return self.cascade.status() if self.cascade is not None else None

    # ------------------------------------------------------------------
    def start_session(self, kind: str = "sess") -> Session:
        if self.session is not None:
            self.close_session()
        self.session = Session(self.root, kind)
        if self.cascade is not None:
            self.cascade.active_session = self.session.path
        self.cfg.set_state(last_session=self.session.id)
        self.cfg.save()
        return self.session

    def close_session(self) -> Optional[Dict[str, Any]]:
        if self.session is None:
            return None
        summary = self.session.close()
        self._offload.enqueue(self.session.path)
        if self.cascade is not None:
            # Released for retirement now that nothing more will be written.
            self.cascade.active_session = None
            self.cascade.nudge()
        self.session = None
        return summary

    # ------------------------------------------------------------------
    def has_space(self) -> bool:
        return self.space_state() != "full"

    def space_state(self) -> str:
        """``ok`` | ``wait`` | ``full``.

        Without a cascade this is the old single-threshold check. With one, the
        top tier is small on purpose (tmpfs is 1.9 GB), so running low is normal
        and simply means capture should pause for the migrator to catch up --
        ``wait`` -- rather than give up. Only when there is genuinely nowhere
        left for the data to go does it become ``full``.
        """
        if self.cascade is None:
            return "ok" if free_mb(self.root) > float(self.cfg["min_free_mb"]) else "full"

        tiers = self.cascade.tiers
        top, bottom = tiers[0], tiers[-1]
        free = top.free_mb()
        if free > top.min_free_mb:
            return "ok"
        if free > HARD_FLOOR_MB:
            # Low but still writable: keep going and let the migrator drain.
            return "ok"
        # Out of room at the top. Whether that is fatal depends on whether the
        # bottom tier can still accept what the migrator is trying to push down.
        downstream_ok = (bottom.remote
                         or bottom.free_mb() > bottom.min_free_mb
                         or bool(self.cfg.get("cascade_ring")))
        return "wait" if downstream_ok else "full"

    def nudge_cascade(self) -> None:
        if self.cascade is not None:
            self.cascade.nudge()

    def free_mb(self) -> float:
        return free_mb(self.root)

    # ------------------------------------------------------------------
    def write_frame(
        self,
        jpeg: bytes,
        exposure_us: int,
        gain: float,
        seq: int,
        stats,
        decision=None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Persist one frame plus its index entry. Returns the path written."""
        if self.session is None:
            self.start_session()
        sess = self.session
        assert sess is not None

        verdict = getattr(stats, "verdict", "ok")
        action = self.cfg["reject_action"]
        if verdict != "ok" and action == "delete":
            sess.record(self._entry(None, exposure_us, gain, seq, stats, decision,
                                    0, extra, time.time()))
            return None

        sdir = shutter_dir(exposure_us)
        # With the cascade on, the shutter directory lives *inside* the current
        # group -- otherwise COLLECT frames sit outside any group and the
        # migrator never sees them, so they pile up on the top tier forever.
        grouped = bool(self.cfg["cascade_enabled"])
        base = sess.acquire_group() if grouped else sess.path
        if verdict != "ok" and action == "quarantine":
            subdir = os.path.join(base, "_rejected", sdir)
        else:
            subdir = os.path.join(base, sdir)
        os.makedirs(subdir, exist_ok=True)

        ts = time.time()
        path = _claim_name(subdir, timestamp_name(ts))
        if path is None:
            if grouped:
                sess.release_group(0)
            return None
        tmp = path + ".part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(jpeg)
            # Rename into place only once complete, so a Mac watching the folder
            # never picks up a half-written JPEG.
            os.replace(tmp, path)
        except OSError:
            for p in (tmp, path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            if grouped:
                sess.release_group(0)
            return None

        if grouped:
            sess.release_group(len(jpeg))
        sess.record(
            self._entry(path, exposure_us, gain, seq, stats, decision, len(jpeg),
                        extra, ts)
        )
        return path

    def _entry(self, path, exposure_us, gain, seq, stats, decision, nbytes, extra,
               when=None):
        # The very same instant the filename was built from. Calling time.time()
        # again here drifted the index (and therefore the EXIF timestamp) a
        # centisecond or two away from the name on the file.
        entry: Dict[str, Any] = {
            "t": round(time.time() if when is None else when, 3),
            "seq": seq,
            "file": os.path.relpath(path, self.session.path) if path else None,
            "shutter_us": int(exposure_us),
            "shutter_dir": shutter_dir(exposure_us),
            "gain": round(float(gain), 3),
            "bytes": nbytes,
            "verdict": getattr(stats, "verdict", "ok"),
        }
        if stats is not None:
            entry["metrics"] = stats.to_dict()
        if decision is not None:
            entry["ae"] = decision.to_dict()
        if extra:
            entry.update(extra)
        return entry

    # ------------------------------------------------------------------
    def write_rapid(
        self,
        jpeg: bytes,
        seq: int,
        when: Optional[float] = None,
        stats=None,
        exposure_us: int = 0,
        gain: float = 1.0,
    ) -> Optional[str]:
        """Write one rapid-mode frame as a flat ``YYYYmmddHHMMSS.jpg``.

        No shutter subdirectories -- rapid runs are a single flat folder, which
        is what feeds the encode tab directly.

        The requested format only has one-second resolution while the camera
        shoots several frames a second, so the first frame in any given second
        gets the bare name and later ones get ``_001``, ``_002`` and so on. That
        keeps the exact requested filename wherever it is actually unambiguous.
        """
        if self.session is None:
            self.start_session("rapid")
        sess = self.session
        assert sess is not None

        when = when if when is not None else time.time()
        base = timestamp_name(when)

        # In group mode frames go into the open group directory instead of the
        # session root, so the migrator can move completed groups while capture
        # keeps writing.
        grouped = bool(self.cfg["cascade_enabled"])
        target_dir = sess.acquire_group() if grouped else sess.path

        path = _claim_name(target_dir, base)
        if path is None:
            if grouped:
                sess.release_group(0)
            return None

        # The claimed name is unique, so its .part sibling is unique too.
        tmp = path + ".part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(jpeg)
            os.replace(tmp, path)
        except OSError:
            for p in (tmp, path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            if grouped:
                sess.release_group(0)
            return None

        if grouped:
            sess.release_group(len(jpeg))

        entry: Dict[str, Any] = {
            "t": round(when, 3),
            "seq": seq,
            "file": os.path.relpath(path, sess.path),
            "shutter_us": int(exposure_us),
            "shutter_dir": shutter_dir(exposure_us),
            "gain": round(float(gain), 3),
            "bytes": len(jpeg),
            "verdict": getattr(stats, "verdict", "ok") if stats else "ok",
        }
        if stats is not None:
            entry["metrics"] = stats.to_dict()
        sess.record(entry)
        return path

    def write_latest(self, jpeg: bytes) -> None:
        """Small always-current preview at a stable path, for remote polling."""
        path = os.path.join(self.root, "latest.jpg")
        tmp = path + ".part"
        try:
            with open(tmp, "wb") as fh:
                fh.write(jpeg)
            os.replace(tmp, path)
        except OSError:
            pass

    def offload_now(self, path: Optional[str] = None) -> None:
        self._offload.enqueue(path or self.root)

    def offload_status(self) -> Dict[str, Any]:
        st = self._offload.status()
        # How far the stick is behind the eMMC, so an unattended run that is
        # out-writing its USB throughput is visible rather than silent.
        sess = self.session
        if sess is not None and self.cfg["offload_to_usb"]:
            dest = os.path.join(self.cfg["usb_root"], os.path.basename(sess.path))
            st["behind"] = max(0, sess.frames - _count_images(dest))
        return st

    def stop(self, drain_timeout: float = 120.0) -> None:
        """Close the session and let outstanding copies finish.

        Closing the session queues one last rsync. Abandoning it would mean an
        unattended run ends with whatever happened to have been copied at the
        last timer tick, which is exactly the data you would want on the stick.
        """
        self.close_session()
        if self.cascade is not None and drain_timeout > 0:
            # Push everything down the cascade before quitting, otherwise frames
            # are left sitting on a tmpfs that will not survive a reboot.
            self.drain_cascade(drain_timeout)
            self.cascade.stop()
        if self.cfg["offload_to_usb"] and drain_timeout > 0:
            self._offload.drain(drain_timeout)
        self._offload.stop()

    def flush_cascade(self, timeout: float = 900.0, on_progress=None):
        """Seal whatever is open, then force everything down to the last tier."""
        if self.cascade is None:
            return {"ok": False, "error": "cascade not running"}
        if self.session is not None:
            tail = self.session.seal_current_group()
            if tail:
                self.notify_group_sealed(tail)
        return self.cascade.flush(timeout, on_progress)

    def drain_cascade(self, timeout: float = 300.0) -> bool:
        """Wait for every sealed group to reach the bottom tier."""
        if self.cascade is None:
            return True
        from .cascade import sealed_groups

        deadline = time.time() + timeout
        self.cascade.nudge()
        while time.time() < deadline:
            pending = sum(len(sealed_groups(t.path))
                          for t in self.cascade.tiers[:-1] if not t.remote)
            st = self.cascade.status()
            if pending == 0 and not st.get("busy"):
                return True
            self.cascade.nudge()
            time.sleep(0.5)
        return False


class OffloadWorker(threading.Thread):
    """Trickles finished sessions to the USB stick with rsync, one at a time.

    Deliberately single-threaded and low priority: the stick is NTFS-over-FUSE on
    a USB 2.0 port, so pushing it hard would steal CPU from JPEG encoding and
    slow the capture loop that actually matters.
    """

    def __init__(self, cfg):
        super().__init__(daemon=True, name="offload")
        self.cfg = cfg
        self.queue: "Queue[Optional[str]]" = Queue()
        self._running = True
        self._current: Optional[str] = None
        self._last: Optional[str] = None
        self._queued: set = set()
        self._resync: set = set()
        self._lock = threading.Lock()
        self.start()

    def enqueue(self, path: str) -> None:
        if not self.cfg["offload_to_usb"]:
            return
        with self._lock:
            if path == self._current:
                # A copy of this path is already in flight, and it started
                # before the frames we are being asked about. Coalesce into a
                # single re-run once it finishes -- simply dropping the request
                # would silently lose everything written since that rsync began,
                # which at shutdown means losing the tail of the whole session.
                self._resync.add(path)
                return
            if path in self._queued:
                return
            self._queued.add(path)
        self.queue.put(path)

    def stop(self) -> None:
        self._running = False
        self.queue.put(None)

    def drain(self, timeout: float = 120.0) -> bool:
        """Block until nothing is queued or in flight. True if it finished."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                idle = (self._current is None and not self._queued
                        and not self._resync)
            if idle and self.queue.empty():
                return True
            time.sleep(0.25)
        return False

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "current": self._current,
                "last": self._last,
                "pending": self.queue.qsize(),
                "enabled": bool(self.cfg["offload_to_usb"]),
            }

    def run(self) -> None:
        while self._running:
            try:
                path = self.queue.get(timeout=1.0)
            except Empty:
                continue
            if path is None:
                break
            self._sync(path)

    def _sync(self, path: str) -> None:
        dest_root = self.cfg["usb_root"]
        mount = os.path.dirname(dest_root.rstrip("/"))
        if not os.path.isdir(mount):
            with self._lock:
                self._last = "USB not mounted (%s)" % mount
            return
        try:
            os.makedirs(dest_root, exist_ok=True)
        except OSError as exc:
            with self._lock:
                self._last = "mkdir failed: %s" % exc
            return

        with self._lock:
            self._current = path
            self._queued.discard(path)
        cmd = ["nice", "-n", "10", "rsync", "-a", "--partial",
               # Never copy a frame that is still being written.
               "--exclude", "*.part"]
        if self.cfg["offload_delete_source"]:
            cmd.append("--remove-source-files")
        cmd += [path.rstrip("/"), dest_root.rstrip("/") + "/"]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=3600)
            msg = "ok" if res.returncode == 0 else res.stderr.decode()[-200:]
        except Exception as exc:  # noqa: BLE001 - report anything, never die
            msg = str(exc)
        with self._lock:
            self._current = None
            self._last = "%s -> %s: %s" % (os.path.basename(path), dest_root, msg)
            again = path in self._resync
            self._resync.discard(path)
            if again:
                self._queued.add(path)
        if again:
            # Files arrived while that rsync was running; go round once more.
            self.queue.put(path)
