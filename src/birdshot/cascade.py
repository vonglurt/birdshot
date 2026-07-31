# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Tiered storage cascade: capture fast, migrate downwards, never fill up.

Frames are written into **groups** -- numbered directories holding a bounded
number of frames -- and background workers migrate whole sealed groups down a
chain of storage tiers:

    tmpfs (1.9 GB, 459 MB/s)  ->  eMMC (16 GB, 60 MB/s)  ->  USB (55 GB, 12 MB/s)

Each tier is a buffer smoothing over the slower one beneath it, and each clears
itself once its contents are safely one level down. Capture therefore runs for
as long as the *last* tier has room, rather than for as long as the first one
does.

Two things this does not do, stated plainly because they are easy to assume:

* **It does not make capture faster.** The eMMC already writes at 60 MB/s
  against a peak capture demand of ~12 MB/s. Putting tmpfs in front buys
  eMMC wear and jitter, not throughput.
* **It does not raise the sustained rate above the slowest tier.** The upper
  tiers absorb bursts, but over a long run the cascade can only shift data as
  fast as the bottom tier accepts it. If capture out-runs that, the buffers fill
  and you get a bounded run length -- which the GUI predicts up front.

Safety rules, because this deletes data:

* A group is only ever removed from a tier after it has been copied to the next
  tier *and verified* -- every file present, every size identical.
* The last tier never deletes anything unless ring mode is explicitly enabled.
* Sealing is atomic: a group is a migration candidate only once ``group.json``
  exists, so a group still being written is never touched.
* Everything is resumable. Groups are self-describing, so a restart picks up
  whatever was left mid-cascade.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

MANIFEST = "group.json"
GROUP_RE = re.compile(r"^g(\d{6})$")
_REMOTE_RE = re.compile(r"^[^/\s]+@[^/:\s]+:")

SSH_OPTS = ["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]


def is_remote(path: str) -> bool:
    """True for rsync-style ``user@host:/path`` targets."""
    return bool(_REMOTE_RE.match(path))


def split_remote(path: str) -> Tuple[str, str]:
    host, _, rest = path.partition(":")
    return host, rest


# ----------------------------------------------------------------------
@dataclass
class Tier:
    """One rung of the cascade."""

    path: str
    label: str = ""
    # Start migrating out of this tier once free space drops below this.
    min_free_mb: float = 512.0
    # Migrate a sealed group anyway once it has sat here this long, so data
    # keeps flowing downwards even when nothing is under space pressure.
    flush_after_s: float = 30.0
    # Sustained write speed in MB/s, measured on this board. Used to predict how
    # long capture can run before the cascade backs up; guessing it from the path
    # gets the USB stick badly wrong (12 MB/s, not 60).
    speed_mb_s: float = 60.0
    # The archive at the bottom never deletes (unless ring mode is on).
    keep: bool = False

    def __post_init__(self):
        if not self.label:
            self.label = self.path

    @property
    def remote(self) -> bool:
        return is_remote(self.path)

    def free_mb(self) -> float:
        if self.remote:
            host, rest = split_remote(self.path)
            try:
                out = subprocess.run(
                    ["ssh", *SSH_OPTS, host, "df -Pm %s 2>/dev/null | tail -1" % _q(rest)],
                    capture_output=True, timeout=20,
                ).stdout.decode().split()
                return float(out[3]) if len(out) >= 4 else 0.0
            except Exception:  # noqa: BLE001
                return 0.0
        p = self.path
        while p and not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        try:
            st = os.statvfs(p or "/")
        except OSError:
            return 0.0
        return st.f_bavail * st.f_frsize / (1024.0 * 1024.0)

    def total_mb(self) -> float:
        if self.remote:
            return 0.0
        p = self.path
        while p and not os.path.exists(p):
            p = os.path.dirname(p) or "/"
        try:
            st = os.statvfs(p)
        except OSError:
            return 0.0
        return st.f_blocks * st.f_frsize / (1024.0 * 1024.0)

    def ensure(self) -> None:
        if self.remote:
            host, rest = split_remote(self.path)
            subprocess.run(["ssh", *SSH_OPTS, host, "mkdir -p %s" % _q(rest)],
                           capture_output=True, timeout=30)
        else:
            os.makedirs(self.path, exist_ok=True)


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ----------------------------------------------------------------------
def group_dir(root: str, seq: int) -> str:
    return os.path.join(root, "g%06d" % seq)


def seal_group(path: str, extra: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Write the manifest that makes a group eligible for migration.

    The manifest lists every file with its size, which is what later lets a
    migration be verified exactly rather than by counting.
    """
    if not os.path.isdir(path):
        return None
    files: List[List[Any]] = []
    total = 0
    try:
        # Recursive: COLLECT keeps its shutter subdirectories inside the group
        # (g000001/ms20/...), so a flat listing would miss every frame.
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for name in sorted(names):
                if name == MANIFEST or name.endswith(".part"):
                    continue
                full = os.path.join(root, name)
                if os.path.isfile(full):
                    size = os.path.getsize(full)
                    files.append([os.path.relpath(full, path), size])
                    total += size
    except OSError:
        return None

    manifest = {
        "seq": int(os.path.basename(path)[1:] or 0),
        "frames": len(files),
        "bytes": total,
        "sealed": time.time(),
        "files": files,
    }
    if extra:
        manifest.update(extra)
    tmp = os.path.join(path, MANIFEST + ".part")
    try:
        with open(tmp, "w") as fh:
            json.dump(manifest, fh)
        os.replace(tmp, os.path.join(path, MANIFEST))
    except OSError:
        return None
    return manifest


def read_manifest(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(path, MANIFEST)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def sealed_groups(root: str) -> List[str]:
    """Sealed group directories under ``root``, oldest first."""
    out: List[str] = []
    if not os.path.isdir(root):
        return out
    for session in sorted(os.listdir(root)):
        sdir = os.path.join(root, session)
        if not os.path.isdir(sdir):
            continue
        for name in sorted(os.listdir(sdir)):
            if not GROUP_RE.match(name):
                continue
            gdir = os.path.join(sdir, name)
            if os.path.isfile(os.path.join(gdir, MANIFEST)):
                out.append(gdir)
    return out


# ----------------------------------------------------------------------
def copy_group(src: str, dst_tier: Tier, rel: str) -> Tuple[bool, str]:
    """rsync one group to the next tier. Does not delete anything."""
    if dst_tier.remote:
        host, rest = split_remote(dst_tier.path)
        dest = "%s:%s/" % (host, os.path.join(rest, os.path.dirname(rel)))
        subprocess.run(
            ["ssh", *SSH_OPTS, host,
             "mkdir -p %s" % _q(os.path.join(rest, os.path.dirname(rel)))],
            capture_output=True, timeout=30)
        cmd = ["rsync", "-a", "--partial", "--exclude", "*.part",
               "-e", "ssh " + " ".join(SSH_OPTS), src.rstrip("/"), dest]
    else:
        dest_dir = os.path.join(dst_tier.path, os.path.dirname(rel))
        os.makedirs(dest_dir, exist_ok=True)
        cmd = ["nice", "-n", "10", "rsync", "-a", "--partial",
               "--exclude", "*.part", src.rstrip("/"), dest_dir + "/"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=3600)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if res.returncode != 0:
        return False, res.stderr.decode()[-200:]
    return True, "ok"


def copy_session_meta(src_session: str, dst_tier: Tier, rel_session: str) -> None:
    """Carry index.jsonl / session.json down alongside the groups.

    Without this the per-frame metrics index stays on the top tier -- which for
    the default cascade is tmpfs, so a reboot would lose it while every frame it
    describes sat safely on the USB stick. It is a few hundred KB and rsync is
    incremental, so copying it on every group migration costs nothing.
    """
    names = [n for n in ("index.jsonl", "session.json")
             if os.path.isfile(os.path.join(src_session, n))]
    if not names:
        return
    if dst_tier.remote:
        host, rest = split_remote(dst_tier.path)
        dest = "%s:%s/" % (host, os.path.join(rest, rel_session))
        subprocess.run(["ssh", *SSH_OPTS, host,
                        "mkdir -p %s" % _q(os.path.join(rest, rel_session))],
                       capture_output=True, timeout=30)
        cmd = ["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS)]
    else:
        dest = os.path.join(dst_tier.path, rel_session) + "/"
        os.makedirs(dest, exist_ok=True)
        cmd = ["nice", "-n", "10", "rsync", "-a"]
    cmd += [os.path.join(src_session, n) for n in names]
    cmd.append(dest)
    try:
        subprocess.run(cmd, capture_output=True, timeout=300)
    except Exception:  # noqa: BLE001
        pass


def verify_group(manifest: Dict[str, Any], dst_tier: Tier, rel: str) -> Tuple[bool, str]:
    """Confirm every file in the manifest arrived at the right size.

    This is the gate that permits deletion, so it compares sizes rather than
    just counting: a truncated copy would pass a count check.
    """
    want = {name: size for name, size in manifest.get("files", [])}
    if not want:
        return True, "empty group"

    if dst_tier.remote:
        host, rest = split_remote(dst_tier.path)
        target = os.path.join(rest, rel)
        try:
            out = subprocess.run(
                ["ssh", *SSH_OPTS, host,
                 "cd %s 2>/dev/null && find . -type f -printf '%%s %%P\\n'"
                 % _q(target)],
                capture_output=True, timeout=120).stdout.decode()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        have = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                have[parts[1].strip()] = int(parts[0])
    else:
        target = os.path.join(dst_tier.path, rel)
        if not os.path.isdir(target):
            return False, "destination missing"
        have = {}
        try:
            for root, _dirs, names in os.walk(target):
                for n in names:
                    full = os.path.join(root, n)
                    have[os.path.relpath(full, target)] = os.path.getsize(full)
        except OSError as exc:
            return False, str(exc)

    missing = [n for n in want if n not in have]
    if missing:
        return False, "%d file(s) missing, e.g. %s" % (len(missing), missing[0])
    bad = [n for n, sz in want.items() if have.get(n) != sz]
    if bad:
        return False, "%d file(s) wrong size, e.g. %s" % (len(bad), bad[0])
    return True, "verified %d files" % len(want)


def remove_group(path: str) -> bool:
    try:
        shutil.rmtree(path)
    except OSError:
        return False
    # Tidy the session directory once its last group has gone.
    parent = os.path.dirname(path)
    try:
        if not any(GROUP_RE.match(n) for n in os.listdir(parent)):
            for leftover in os.listdir(parent):
                if leftover in ("index.jsonl", "session.json"):
                    continue
            if not os.listdir(parent):
                os.rmdir(parent)
    except OSError:
        pass
    return True


# ----------------------------------------------------------------------
def adopt_file(tier_root: str, path: str, kind: str = "media") -> Optional[str]:
    """Wrap a finished file as a one-item group so the cascade carries it down.

    Videos and assembled movies are written whole rather than frame by frame, so
    they cannot be grouped as they go. Once complete they are moved into a group
    directory and sealed, after which they migrate exactly like frames -- which
    is what "all outputs cascade" needs.
    """
    if not os.path.isfile(path):
        return None
    session = os.path.join(tier_root, "%s-%s" % (kind, os.path.splitext(
        os.path.basename(path))[0]))
    gdir = group_dir(session, 1)
    try:
        os.makedirs(gdir, exist_ok=True)
        dest = os.path.join(gdir, os.path.basename(path))
        try:
            os.replace(path, dest)          # same filesystem: instant
        except OSError:
            shutil.copy2(path, dest)        # crossing filesystems
            os.unlink(path)
    except OSError:
        return None
    seal_group(gdir, {"session": os.path.basename(session), "kind": kind})
    return gdir


class Cascade(threading.Thread):
    """Background migrator. One thread, one group at a time, deliberately.

    Parallel copies would only thrash the slow bottom tier and steal CPU from
    JPEG encoding, which is the thing that actually limits capture rate.
    """

    def __init__(self, tiers: List[Tier], cfg, on_event: Optional[Callable] = None):
        super().__init__(daemon=True, name="cascade")
        self.tiers = tiers
        self.cfg = cfg
        self.on_event = on_event
        self._running = True
        self._lock = threading.Lock()
        self._busy: Optional[str] = None
        self._last: str = "idle"
        self._moved_groups = 0
        self._moved_bytes = 0
        self._errors = 0
        self._wake = threading.Event()
        # Never retire the session currently being captured into.
        self.active_session: Optional[str] = None
        # While set, every sealed group migrates regardless of space pressure or
        # age -- this is what "flush" means: get it all to the bottom tier.
        self.force = threading.Event()

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        self._wake.set()

    def nudge(self) -> None:
        """Called when a group is sealed, so migration starts without waiting."""
        self._wake.set()

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(name, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            busy, last = self._busy, self._last
            moved, mbytes, errs = self._moved_groups, self._moved_bytes, self._errors
        tiers = []
        for i, t in enumerate(self.tiers):
            pending = len(sealed_groups(t.path)) if not t.remote else 0
            tiers.append({
                "index": i, "label": t.label, "path": t.path,
                "free_mb": t.free_mb(), "total_mb": t.total_mb(),
                "pending": pending, "keep": t.keep, "remote": t.remote,
                "speed_mb_s": t.speed_mb_s,
            })
        return {"tiers": tiers, "busy": busy, "last": last,
                "moved_groups": moved, "moved_bytes": mbytes, "errors": errs}

    # ------------------------------------------------------------------
    def run(self) -> None:
        while self._running:
            try:
                did = self._pass()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last = "cascade error: %s" % exc
                    self._errors += 1
                did = False
            if not did:
                self._wake.wait(timeout=2.0)
                self._wake.clear()

    def _pass(self) -> bool:
        """One migration step. Returns True if it moved something."""
        for i in range(len(self.tiers) - 1):
            src, dst = self.tiers[i], self.tiers[i + 1]
            groups = sealed_groups(src.path)
            if not groups:
                continue

            free = src.free_mb()
            pressure = free < src.min_free_mb
            now = time.time()

            for gdir in groups:
                manifest = read_manifest(gdir)
                if manifest is None:
                    continue
                aged = (now - float(manifest.get("sealed", now))) >= src.flush_after_s
                if not (pressure or aged or self.force.is_set()):
                    continue
                if self._migrate(gdir, src, dst, manifest):
                    return True
            # Nothing eligible on this rung; try the next one down.
        # Bottom tier full and ring mode on: drop the oldest to keep going.
        return self._evict_bottom()

    def _migrate(self, gdir: str, src: Tier, dst: Tier, manifest: Dict[str, Any]) -> bool:
        rel = os.path.relpath(gdir, src.path)
        with self._lock:
            self._busy = "%s -> %s" % (rel, dst.label)

        dst.ensure()
        t0 = time.time()
        rel_session = os.path.dirname(rel)
        copy_session_meta(os.path.dirname(gdir), dst, rel_session)
        ok, msg = copy_group(gdir, dst, rel)
        if ok:
            ok, msg = verify_group(manifest, dst, rel)

        nbytes = int(manifest.get("bytes", 0))
        if ok:
            # Only now is deleting the source safe.
            if not src.keep:
                remove_group(gdir)
                self._retire_session_if_empty(os.path.dirname(gdir))
            dt = max(1e-3, time.time() - t0)
            with self._lock:
                self._busy = None
                self._moved_groups += 1
                self._moved_bytes += nbytes
                self._last = ("%s -> %s: %d frames, %.0f MB at %.0f MB/s"
                              % (rel, dst.label, manifest.get("frames", 0),
                                 nbytes / 1e6, nbytes / 1e6 / dt))
            self._emit("cascade", {"event": "migrated", "group": rel,
                                   "to": dst.label, "bytes": nbytes,
                                   "mbps": nbytes / 1e6 / dt})
            return True

        with self._lock:
            self._busy = None
            self._errors += 1
            self._last = "FAILED %s -> %s: %s" % (rel, dst.label, msg)
        self._emit("cascade", {"event": "failed", "group": rel,
                               "to": dst.label, "error": msg})
        # Back off briefly so a persistent failure does not spin.
        time.sleep(2.0)
        return False

    def adopt_orphans(self) -> int:
        """Sweep loose frames in upper tiers into a group so they can migrate.

        COLLECT used to write outside any group, and anything left by an older
        build is stranded on the top tier for good otherwise. Files are *moved*
        within the same filesystem, so this is a rename, not a copy.
        """
        adopted = 0
        for tier in self.tiers[:-1]:
            if tier.remote or not os.path.isdir(tier.path):
                continue
            for session in sorted(os.listdir(tier.path)):
                sdir = os.path.join(tier.path, session)
                if not os.path.isdir(sdir):
                    continue
                if os.path.abspath(sdir) == os.path.abspath(self.active_session or ""):
                    continue
                try:
                    names = os.listdir(sdir)
                except OSError:
                    continue
                loose = [n for n in names
                         if not GROUP_RE.match(n)
                         and n not in ("index.jsonl", "session.json")
                         and not n.endswith(".part")]
                if not loose:
                    continue
                seqs = [int(m.group(1)) for m in
                        (GROUP_RE.match(n) for n in names) if m]
                gdir = group_dir(sdir, max(seqs) + 1 if seqs else 1)
                try:
                    os.makedirs(gdir, exist_ok=True)
                    for n in loose:
                        os.replace(os.path.join(sdir, n), os.path.join(gdir, n))
                    seal_group(gdir, {"session": session, "adopted": True})
                    adopted += 1
                except OSError:
                    continue
        return adopted

    def flush(self, timeout: float = 900.0,
              on_progress: Optional[Callable] = None) -> Dict[str, Any]:
        """Push everything in the upper tiers down to the bottom one.

        Returns once the upper tiers hold no sealed groups, or the timeout
        expires. Safe to call while capture is running -- the group currently
        being written is sealed by the caller, not here.
        """
        adopted = self.adopt_orphans()
        self.force.set()
        self.nudge()
        deadline = time.time() + timeout
        start_moved = self._moved_groups
        try:
            while time.time() < deadline:
                pending = sum(len(sealed_groups(t.path))
                              for t in self.tiers[:-1] if not t.remote)
                busy = self.status().get("busy")
                if on_progress:
                    on_progress(pending, self._moved_groups - start_moved)
                if pending == 0 and not busy:
                    break
                self.nudge()
                time.sleep(0.4)
        finally:
            self.force.clear()
        pending = sum(len(sealed_groups(t.path))
                      for t in self.tiers[:-1] if not t.remote)
        return {"ok": pending == 0, "adopted": adopted, "pending": pending,
                "moved": self._moved_groups - start_moved,
                "bottom": self.tiers[-1].label}

    def _retire_session_if_empty(self, session_dir: str) -> None:
        """Remove a drained session directory from an upper tier.

        Its metadata has already been copied down by copy_session_meta, and the
        session still being captured into is never touched.
        """
        if os.path.abspath(session_dir) == os.path.abspath(self.active_session or ""):
            return
        try:
            names = os.listdir(session_dir)
        except OSError:
            return
        if any(GROUP_RE.match(n) for n in names):
            return
        try:
            shutil.rmtree(session_dir)
        except OSError:
            pass

    def _evict_bottom(self) -> bool:
        """Ring mode: when the archive is full, drop the oldest groups.

        Off by default -- this is the only place birdshot deletes data that has not
        been copied somewhere else, so it must be asked for explicitly.
        """
        if not self.cfg.get("cascade_ring"):
            return False
        bottom = self.tiers[-1]
        if bottom.remote or bottom.free_mb() >= bottom.min_free_mb:
            return False
        groups = sealed_groups(bottom.path)
        if not groups:
            return False
        oldest = groups[0]
        manifest = read_manifest(oldest) or {}
        remove_group(oldest)
        with self._lock:
            self._last = ("ring: dropped oldest group %s (%.0f MB) to make room"
                          % (os.path.basename(oldest), manifest.get("bytes", 0) / 1e6))
        self._emit("cascade", {"event": "evicted", "group": os.path.basename(oldest),
                               "bytes": manifest.get("bytes", 0)})
        return True


# ----------------------------------------------------------------------
def friendly_label(path: str) -> str:
    """A human name for a tier, derived from where it actually lives.

    Configured labels get overwritten by --tiers and by anything that rewrites
    the tier list, and a raw path is a poor answer to "where did that frame go".
    The filesystem knows better than the config does.
    """
    if is_remote(path):
        host, rest = split_remote(path)
        return "%s (network)" % host.split("@")[-1]
    mount = _mount_point(path)
    fst = _fstype(mount)
    if fst == "tmpfs":
        return "RAM"
    if mount.startswith("/media/") or mount.startswith("/mnt/") or \
            mount.startswith("/run/media/"):
        return "%s (USB)" % os.path.basename(mount.rstrip("/"))
    if mount == "/":
        return "eMMC"
    return os.path.basename(mount.rstrip("/")) or mount


def is_ram_path(path: str) -> bool:
    """True if this tier lives on a tmpfs, i.e. is volatile."""
    if is_remote(path):
        return False
    return _fstype(_mount_point(path)) == "tmpfs"


def build_tiers(cfg) -> List[Tier]:
    """Construct the tier chain from config, skipping anything unusable.

    The chain is always top-to-bottom fastest-to-largest, and always ends at the
    archive. Dropping the RAM tier leaves eMMC -> USB, which behaves identically
    -- only the buffer above is gone.
    """
    tiers: List[Tier] = []
    specs = cfg.get("cascade_tiers") or []
    use_ram = bool(cfg.get("cascade_use_ram", True))
    for i, spec in enumerate(specs):
        path = spec.get("path") if isinstance(spec, dict) else str(spec)
        if not path:
            continue
        # Skipping the RAM rung must never remove the last tier -- something has
        # to be the archive.
        if not use_ram and is_ram_path(path) and i < len(specs) - 1:
            continue
        configured = (spec.get("label") if isinstance(spec, dict) else "") or ""
        # A label equal to the path carries no information -- derive one.
        t = Tier(
            path=path,
            label=configured if configured and configured != path
            else friendly_label(path),
            min_free_mb=float(spec.get("min_free_mb", 512)) if isinstance(spec, dict) else 512.0,
            flush_after_s=float(spec.get("flush_after_s", 30)) if isinstance(spec, dict) else 30.0,
            speed_mb_s=float(spec.get("speed_mb_s", 60)) if isinstance(spec, dict) else 60.0,
        )
        tiers.append(t)
    for t in tiers:
        t.keep = False
    if tiers:
        tiers[-1].keep = True   # the archive never deletes
    return tiers


def predict(tiers: List[Tier], mb_per_s: float, bottom_mb_per_s: float) -> Dict[str, Any]:
    """How long capture can run before the cascade backs up.

    If the bottom tier accepts data at least as fast as capture produces it, the
    run is unbounded. Otherwise the buffers above absorb the difference, and the
    run length is (total buffer) / (surplus rate).
    """
    if not tiers:
        return {"unbounded": False, "seconds": 0.0, "buffer_mb": 0.0}
    buffer_mb = sum(max(0.0, t.free_mb() - t.min_free_mb) for t in tiers[:-1])
    bottom_free = tiers[-1].free_mb() if not tiers[-1].remote else float("inf")
    surplus = mb_per_s - bottom_mb_per_s
    if surplus <= 0:
        seconds = (bottom_free / mb_per_s) if mb_per_s > 0 and bottom_free != float("inf") else float("inf")
        return {"unbounded": bottom_free == float("inf"), "seconds": seconds,
                "buffer_mb": buffer_mb, "limited_by": "bottom tier capacity"}
    return {"unbounded": False, "seconds": buffer_mb / surplus,
            "buffer_mb": buffer_mb, "limited_by": "bottom tier write speed",
            "surplus_mb_s": surplus}


# ----------------------------------------------------------------------
# RAM tier sizing.
#
# The top tier is a tmpfs, and tmpfs pages come out of the same RAM the camera
# needs for its DMA buffers. CMA is carved from MemTotal, and tmpfs pages are
# movable so the kernel will happily place them *inside* the CMA region -- then
# a camera allocation has to migrate them out, and if RAM is full it cannot.
# That is exactly the "cma_alloc: alloc failed" crash. So the budget always
# leaves CMA plus working headroom alone, and the GUI shows where the real
# ceiling is rather than letting the slider run into it.

OS_RESERVE_MB = 600.0  # X, the GUI, page cache, our own encode buffers


def meminfo_mb() -> Dict[str, float]:
    out: Dict[str, float] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    out[k] = float(parts[0]) / 1024.0
    except OSError:
        pass
    return out


def ram_budget(pct: float) -> Dict[str, Any]:
    """What a given percentage of RAM means, and whether it is safe."""
    mi = meminfo_mb()
    total = mi.get("MemTotal", 0.0)
    cma = mi.get("CmaTotal", 0.0)
    budget = total * max(0.0, min(100.0, pct)) / 100.0
    # Everything the tmpfs must NOT take.
    reserved = cma + OS_RESERVE_MB
    max_safe_mb = max(0.0, total - reserved)
    max_safe_pct = (max_safe_mb / total * 100.0) if total else 0.0
    return {
        "pct": pct,
        "total_mb": total,
        "cma_mb": cma,
        "budget_mb": budget,
        "max_safe_mb": max_safe_mb,
        "max_safe_pct": max_safe_pct,
        "safe": budget <= max_safe_mb,
        "headroom_mb": max_safe_mb - budget,
    }


def _mount_point(path: str) -> str:
    p = os.path.abspath(path)
    while p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    return p


def _fstype(mount: str) -> str:
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1].replace("\\040", " ") == mount:
                    return parts[2]
    except OSError:
        pass
    return ""


def ram_tier_size_mb(path: str) -> Tuple[float, float]:
    """(total, used) MB of the tmpfs hosting ``path``."""
    mount = _mount_point(path)
    try:
        st = os.statvfs(mount)
    except OSError:
        return 0.0, 0.0
    total = st.f_blocks * st.f_frsize / 1e6
    used = (st.f_blocks - st.f_bfree) * st.f_frsize / 1e6
    return total, used


def resize_ram_tier(path: str, pct: float) -> Tuple[bool, str]:
    """Remount the tmpfs behind ``path`` at ``pct`` percent of RAM.

    Needs root, which this image grants passwordlessly. Shrinking below what is
    already stored will fail, which is the correct outcome -- it would mean
    discarding frames that have not migrated yet.
    """
    mount = _mount_point(path)
    fst = _fstype(mount)
    if fst != "tmpfs":
        return False, "%s is %s, not tmpfs -- nothing to resize" % (mount, fst or "?")

    info = ram_budget(pct)
    _, used = ram_tier_size_mb(path)
    if info["budget_mb"] < used:
        return False, ("%.0f MB is already stored on %s; cannot shrink to %.0f MB"
                       % (used, mount, info["budget_mb"]))
    cmd = ["sudo", "-n", "mount", "-o", "remount,size=%d%%" % int(round(pct)), mount]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if res.returncode != 0:
        return False, res.stderr.decode().strip()[-160:] or "remount failed"
    total, _ = ram_tier_size_mb(path)
    return True, "%s resized to %.0f MB (%.0f%% of RAM)" % (mount, total, pct)


def apply_ram_pct(cfg) -> Tuple[bool, str]:
    """Resize the top tier and rescale its watermark to match."""
    tiers = build_tiers(cfg)
    if not tiers:
        return False, "no cascade tiers configured"
    pct = float(cfg.get("cascade_ram_pct", 50))
    ok, msg = resize_ram_tier(tiers[0].path, pct)
    if not ok:
        return False, msg

    # Keep a slice of the tier free so writers never hit a full filesystem
    # between migrations -- 15% of it, floored at the hard write floor.
    total, _ = ram_tier_size_mb(tiers[0].path)
    specs = list(cfg.get("cascade_tiers") or [])
    if specs and isinstance(specs[0], dict):
        specs[0] = dict(specs[0])
        specs[0]["min_free_mb"] = max(150.0, round(total * 0.15))
        cfg["cascade_tiers"] = specs
        cfg.save()
        msg += ", migrate below %.0f MB free" % specs[0]["min_free_mb"]
    return True, msg
