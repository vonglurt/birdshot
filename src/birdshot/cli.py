# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Headless birdshot control -- run captures over SSH, and verify a deployment.

    birdshot-cli selftest             exercise every module against the real camera
    birdshot-cli doctor               checklist: platform, deps, cameras, storage
    birdshot-cli info                 camera, modes, storage, calibration
    birdshot-cli capture -n 40        burst capture with auto-exposure
    birdshot-cli timelapse -i 5 -n 60 interval capture
    birdshot-cli assemble <session>   build a movie from a session
    birdshot-cli sessions             list sessions
"""

import argparse
import json
import os
import sys
import time

from birdshot.config import CAPTURE_MODES, Config
from birdshot.naming import describe_shutter, shutter_dir


def _engine(cfg, on_event=None):
    from birdshot import backends
    from birdshot.storage import Storage

    storage = Storage(cfg)
    events = []

    def handler(name, payload):
        events.append((name, payload))
        if on_event:
            on_event(name, payload)

    engine = backends.create_engine(cfg, storage, handler)
    engine.start()
    return engine, storage, events


def _progress(name, payload):
    if name == "frame":
        st = payload.get("stats")
        print("  #%-5d %-8s %-9s gain %5.2f  %6.2f MB  %s"
              % (payload.get("seq", 0), shutter_dir(payload.get("shutter_us", 0)),
                 st.verdict if st else "?", payload.get("gain", 0),
                 payload.get("bytes", 0) / 1e6,
                 os.path.basename(payload.get("path") or "-")))
    elif name == "error":
        print("  ERROR: %s" % payload.get("msg"), file=sys.stderr)


# ----------------------------------------------------------------------
def cmd_info(args, cfg):
    from birdshot import backends

    print("=== cameras ===")
    for c in backends.list_cameras():
        print("  [%s:%d] %s  %s" % (c["backend"], c["index"], c["model"], c["id"]))
    ok, why = backends.picamera2_available()
    if ok:
        from picamera2 import Picamera2
        p = Picamera2()
        for m in p.sensor_modes:
            print("  mode %-12s crop %s" % (m.get("size"), m.get("crop_limits")))
        p.close()
    else:
        print("  (picamera2 stack not available: %s)" % why)

    print("\n=== capture modes ===")
    for i, m in enumerate(CAPTURE_MODES):
        mark = "*" if i == cfg["capture_mode"] else " "
        print("  %s %dx%d  ~%.0f fps  %s" % (mark, m[0], m[1], m[3], m[2]))

    print("\n=== storage ===")
    from birdshot.storage import free_mb
    print("  capture root : %s (%.1f GB free)"
          % (cfg["data_root"], free_mb(cfg["data_root"]) / 1024))
    print("  usb offload  : %s (%.1f GB free)"
          % (cfg["usb_root"], free_mb(cfg["usb_root"]) / 1024))

    print("\n=== exposure ===")
    print("  auto         : %s" % cfg["auto_exposure"])
    print("  motion limit : %s" % describe_shutter(cfg["motion_limit_us"]))
    print("  target luma  : %s" % cfg["target_luma"])
    cal = cfg["calibration"] or {}
    if cal.get("done"):
        print("  calibrated   : %.1f EV scene range"
              % (cal.get("dynamic_range_ev") or 0))
    else:
        print("  calibrated   : no")
    return 0


def cmd_capture(args, cfg):
    cfg["auto_exposure"] = not args.manual
    if args.shutter:
        cfg["manual_shutter_us"] = args.shutter
    if args.mode is not None:
        cfg["capture_mode"] = args.mode
    engine, storage, _ = _engine(cfg, _progress)
    print("capturing %s frames at %dx%d..."
          % (args.count or "unlimited", *cfg.capture_size()))
    t0 = time.time()
    engine.send("burst", count=args.count)
    _await_state(engine, "burst", args.timeout)
    elapsed = time.time() - t0
    _finish(engine, storage)
    if args.count:
        print("%d frames in %.1fs = %.2f fps" % (args.count, elapsed, args.count / elapsed))
    return 0


def _resolve_session(name, cfg):
    """Locate a session by name, searching the cascade tiers as well.

    An empty or blank name must be rejected outright: os.path.join(root, "")
    yields the data root itself, which would silently process every frame on the
    machine instead of one session.
    """
    if not name or not str(name).strip():
        print("no session given", file=sys.stderr)
        return None
    name = str(name).strip()
    if os.path.isdir(name) and os.path.basename(name.rstrip("/")):
        return name.rstrip("/")

    roots = [cfg["data_root"]]
    for spec in (cfg.get("cascade_tiers") or []):
        p = spec.get("path") if isinstance(spec, dict) else str(spec)
        if p and not (":" in p and "@" in p):
            roots.append(p)
    roots.append(cfg.get("usb_root", ""))
    for root in roots:
        cand = os.path.join(root, name)
        if os.path.isdir(cand):
            return cand
    print("no such session: %s (looked in %s)" % (name, ", ".join(r for r in roots if r)),
          file=sys.stderr)
    return None


def _await_state(engine, state, timeout):
    """Block while the engine is in ``state``, or until it times out."""
    deadline = time.time() + (timeout or 1e9)
    try:
        while engine.state != state and time.time() < deadline:
            time.sleep(0.05)          # wait for it to start
        while engine.state == state and time.time() < deadline:
            time.sleep(0.2)           # wait for it to finish
    except KeyboardInterrupt:
        print("\ninterrupted")


def _finish(engine, storage):
    engine.send("stop")
    deadline = time.time() + 30
    while engine.state in ("burst", "timelapse", "video") and time.time() < deadline:
        time.sleep(0.2)
    engine.shutdown()
    engine.join(timeout=30)
    storage.stop()


def cmd_rapid(args, cfg):
    cfg["rapid_mode"] = args.mode
    if args.mode is not None:
        cfg["capture_mode"] = args.res if args.res is not None else cfg["capture_mode"]
    phases = {}

    def on_event(name, payload):
        if name == "rapid":
            ph = payload.get("phase")
            if ph == "start" and payload.get("mode") == "ram":
                print("RAM burst: capacity %d frames (%.0f MB budget, %.1f MB/frame)"
                      % (payload["capacity"], payload["budget_mb"],
                         payload["per_frame_mb"]))
            elif ph == "drain" and payload.get("done", 0) % 10 == 0:
                print("  draining %d/%d" % (payload["done"], payload["total"]))
            elif ph == "done":
                phases["drained"] = payload["total"]
        elif name == "error":
            print("  ERROR: %s" % payload.get("msg"), file=sys.stderr)

    engine, storage, _ = _engine(cfg, on_event)
    w, h = cfg.capture_size()
    print("rapid %s, %dx%d, %s frames"
          % (args.mode, w, h, args.count or "max"))
    t0 = time.time()
    engine.send("rapid", mode=args.mode, count=args.count)
    _await_state(engine, "rapid", args.timeout)
    taken = engine._taken
    rate = engine.capture_rate()
    _finish(engine, storage)
    total_elapsed = time.time() - t0
    if taken:
        print("captured %d frames at %.2f fps  (%.1f ms/frame)"
              % (taken, rate, 1000.0 / rate if rate else 0))
        print("wall clock incl. camera setup and drain: %.1fs" % total_elapsed)
    report = engine.profile_report()
    if report:
        print("per-frame breakdown (BIRDSHOT_PROFILE):")
        for k, v in sorted(report.items(), key=lambda kv: -kv[1]):
            print("   %-11s %6.2f ms" % (k, v))
        print("   %-11s %6.2f ms" % ("SUM", sum(report.values())))
    return 0


def cmd_cascade(args, cfg):
    """Group capture with background migration down the storage tiers."""
    from birdshot import cascade as csc

    if args.tiers:
        cfg["cascade_tiers"] = [{"path": p, "label": p, "min_free_mb": 400,
                                 "flush_after_s": 5} for p in args.tiers.split(",")]
    cfg["cascade_enabled"] = True
    cfg["group_frames"] = args.group
    if args.ring:
        cfg["cascade_ring"] = True
    if args.res is not None:
        cfg["capture_mode"] = args.res
    cfg.save()

    tiers = csc.build_tiers(cfg)
    print("cascade:")
    for i, t in enumerate(tiers):
        print("  %d. %-34s %8.1f GB free%s"
              % (i + 1, t.path, t.free_mb() / 1024, "   (archive)" if t.keep else ""))
    print("groups of %d frames\n" % args.group)

    def on_event(name, payload):
        if name == "cascade":
            ev = payload.get("event")
            if ev == "migrated":
                print("  moved %s -> %s  %.0f MB at %.0f MB/s"
                      % (payload["group"], payload["to"],
                         payload["bytes"] / 1e6, payload.get("mbps", 0)))
            elif ev == "failed":
                print("  FAILED %s: %s" % (payload["group"], payload.get("error")),
                      file=sys.stderr)
            elif ev == "evicted":
                print("  ring: dropped %s" % payload["group"])
        elif name == "group":
            print("  sealed %s" % os.path.basename(payload.get("sealed") or ""))
        elif name == "error":
            print("  ERROR: %s" % payload.get("msg"), file=sys.stderr)

    from birdshot import backends
    from birdshot.storage import Storage
    storage = Storage(cfg, on_event)
    engine = backends.create_engine(cfg, storage, on_event)
    engine.start()

    t0 = time.time()
    engine.send("rapid", mode="continuous", count=args.count)
    _await_state(engine, "rapid", args.timeout)
    taken, rate = engine._taken, engine.capture_rate()
    engine.send("stop")
    deadline = time.time() + 60
    while engine.state in ("rapid", "drain") and time.time() < deadline:
        time.sleep(0.2)
    engine.shutdown(); engine.join(timeout=60)

    print("\ncaptured %d frames at %.2f fps" % (taken, rate))
    print("draining cascade...")
    storage.drain_cascade(300)
    st = storage.cascade_status()
    if st:
        for t in st["tiers"]:
            print("  %-34s %8.1f GB free   %d pending"
                  % (t["label"], t["free_mb"] / 1024, t["pending"]))
        print("  moved %d groups (%.2f GB), %d errors"
              % (st["moved_groups"], st["moved_bytes"] / 1e9, st["errors"]))
    storage.stop(60)
    return 0


def cmd_timelapse(args, cfg):
    cfg["timelapse_interval_s"] = args.interval
    engine, storage, _ = _engine(cfg, _progress)
    print("timelapse: %s frames every %.1fs" % (args.count or "unlimited", args.interval))
    engine.send("timelapse", count=args.count)
    _await_state(engine, "timelapse", args.timeout)
    _finish(engine, storage)
    return 0


def cmd_exif(args, cfg):
    """Stamp EXIF into a session's frames from its index."""
    from birdshot import exif as exifmod

    ok, backend = exifmod.available()
    print("backend:", backend)
    if not ok:
        return 1
    path = _resolve_session(args.session, cfg)
    if path is None:
        return 1

    def prog(done, total):
        if total:
            print("\r  %d/%d" % (done, total), end="", flush=True)

    res = exifmod.tag_session(path, cfg, only_ok=args.only_ok, progress=prog)
    print()
    print(json.dumps(res, indent=2))
    if res.get("tagged"):
        import glob
        sample = sorted(glob.glob(os.path.join(path, "**", "*.jpg"), recursive=True))
        if sample:
            print("\nread back from %s:" % os.path.basename(sample[0]))
            for k, v in exifmod.read_back(sample[0]).items():
                print("   %-20s %s" % (k, v))
    return 0 if res.get("ok") else 1


def cmd_flush(args, cfg):
    """Force every pending group down to the bottom tier."""
    from birdshot.storage import Storage

    if not cfg["cascade_enabled"]:
        print("cascade is not enabled", file=sys.stderr)
        return 1
    st = Storage(cfg)
    if st.cascade is None:
        print("cascade failed to start", file=sys.stderr)
        return 1
    for t in st.cascade.tiers:
        print("  %-36s %8.1f GB free   %d pending"
              % (t.label, t.free_mb() / 1024, len(__import__("birdshot.cascade",
                 fromlist=["x"]).sealed_groups(t.path)) if not t.remote else 0))
    print("\nflushing to %s ..." % st.cascade.tiers[-1].label)

    def prog(pending, moved):
        print("\r  %d pending, %d moved" % (pending, moved), end="", flush=True)

    res = st.flush_cascade(args.timeout, on_progress=prog)
    print()
    print(json.dumps(res, indent=2))
    for t in st.cascade.tiers:
        print("  %-36s %8.1f GB free" % (t.label, t.free_mb() / 1024))
    st.cascade.stop()
    return 0 if res.get("ok") else 1


def cmd_sessions(args, cfg):
    from birdshot import timelapse as tl
    for s in tl.list_sessions(cfg["data_root"]):
        counts = s.get("counts") or {}
        print("%-24s %6d frames  %8.1f MB  %s"
              % (s["id"], s.get("frames", 0), s.get("bytes", 0) / 1e6,
                 " ".join("%s=%d" % kv for kv in sorted(counts.items())) or ""))
    return 0


def cmd_assemble(args, cfg):
    from birdshot import timelapse as tl

    path = _resolve_session(args.session, cfg)
    if path is None:
        return 1
    if getattr(args, "exif", False):
        from birdshot import exif as exifmod
        print("writing EXIF...")
        print(" ", exifmod.tag_session(path, cfg, only_ok=not args.all))
    frames = tl.frames_from_index(path, only_ok=not args.all)
    print("%d frames selected" % len(frames))
    out = args.output or os.path.join(cfg["data_root"], "timelapse",
                                      "%s_%dfps.mp4" % (os.path.basename(path), args.fps))
    result = tl.assemble(frames, out, fps=args.fps, width=args.width)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_doctor(args, cfg):
    from birdshot import doctor
    return doctor.run(cfg, as_json=args.json, write_config=args.write_config)


# ----------------------------------------------------------------------
def cmd_selftest(args, cfg):
    """Exercise every part of the stack against the real camera."""
    import numpy as np

    failures = []

    def check(label, fn):
        try:
            detail = fn()
            print("  PASS  %-34s %s" % (label, detail or ""))
        except Exception as exc:  # noqa: BLE001
            print("  FAIL  %-34s %r" % (label, exc))
            failures.append(label)

    print("=== birdshot selftest ===")

    def t_naming():
        from birdshot.naming import parse_shutter_dir, shutter_dir as sd
        # Every directory name the old runCam.sh could produce, reproduced.
        legacy = {
            100_000: "s01", 400_000: "s04", 1_600_000: "s16", 2_400_000: "s24",
            3_200_000: "s32", 4_800_000: "s48", 6_400_000: "s64",
            9_200_000: "s92", 19_100_000: "s191",
            50_000: "ms500", 80_000: "ms800", 160_000: "ms1600",
        }
        for us, name in legacy.items():
            assert sd(us) == name, "%d -> %s, wanted %s" % (us, sd(us), name)
            assert parse_shutter_dir(name) == us, name
        # New fast end, where ms would round away real precision.
        assert sd(2_000) == "ms20", sd(2_000)
        assert sd(500) == "us500", sd(500)
        assert sd(114) == "us110", sd(114)   # bucketed onto the 10 us grid
        assert parse_shutter_dir("us500") == 500
        assert parse_shutter_dir("sauto") is None
        # Bucketing must never collapse two decades onto one name.
        names = {sd(us) for us in (200, 500, 900, 1500, 5000, 50_000, 500_000)}
        assert len(names) == 7, names
        return "all %d legacy names round-trip" % len(legacy)
    check("naming matches runCam.sh", t_naming)

    def t_analysis():
        from birdshot.analysis import analyse
        c = cfg.as_dict()
        flat = np.full((480, 640), 128, np.uint8)
        st = analyse(flat, c)
        assert st.is_empty, "flat frame should read as empty"
        dark = np.full((480, 640), 3, np.uint8)
        assert analyse(dark, c).verdict == "dark"
        rng = np.random.RandomState(0)
        noisy = rng.randint(0, 255, (480, 640)).astype(np.uint8)
        st2 = analyse(noisy, c)
        assert st2.contrast_tiles > 0, "noise should register as detail"
        return "flat=empty, black=dark, textured=content"
    check("analysis gates", t_analysis)

    def t_allocate():
        from birdshot.exposure import allocate
        kw = dict(motion_limit_us=2000, gain_preferred_max=4.0,
                  shutter_hard_max_us=33000, exposure_min_us=114,
                  exposure_max_us=1_000_000, gain_min=1.0, gain_max=22.26)
        t1, g1 = allocate(500, **kw)
        assert t1 == 500 and g1 == 1.0, (t1, g1)
        t2, g2 = allocate(6000, **kw)
        assert t2 == 2000 and abs(g2 - 3.0) < 0.01, (t2, g2)
        t3, g3 = allocate(40000, **kw)
        assert abs(g3 - 4.0) < 0.01 and t3 == 10000, (t3, g3)
        prev = 0
        for e in (200, 1000, 5000, 20000, 200000, 2_000_000):
            t, g = allocate(e, **kw)
            assert t * g >= prev, "ladder must be monotonic"
            prev = t * g
        return "ladder monotonic, holds shutter at motion limit"
    check("exposure ladder", t_allocate)

    def t_pid():
        from birdshot.analysis import FrameStats
        from birdshot.exposure import ExposureController
        c = Config(os.path.join("/tmp", "birdshot-selftest.json"))
        c["auto_exposure"] = True
        ctrl = ExposureController(c)
        us, gain = 2000, 1.0
        # Simulate a scene needing 4x more light and check it converges fast.
        for i in range(12):
            luma = min(255.0, 30.0 * (us * gain) / 2000.0)
            st = FrameStats(meter=luma, p50=luma, p95=luma, clip_hi=0.0)
            d = ctrl.update(st, us, gain, lux=1000.0, now=i * 0.25)
            us, gain = d.exposure_us, d.gain
            if d.settled:
                return "converged in %d frames to %s" % (i + 1, describe_shutter(us))
        raise AssertionError("did not settle in 12 frames (got %s)" % describe_shutter(us))
    check("PID converges", t_pid)

    def t_ae_damping():
        """The loop must hold still on noise yet still jump on a real change."""
        import random, math, statistics
        from birdshot.analysis import FrameStats
        from birdshot.exposure import ExposureController
        if os.path.exists("/tmp/birdshot-selftest-ae.json"):
            os.unlink("/tmp/birdshot-selftest-ae.json")
        c = Config("/tmp/birdshot-selftest-ae.json")
        c["target_luma"] = 47.0
        c["ae_average_n"] = 3
        c["ae_average_mode"] = "median"
        c["ae_damping"] = 0.5
        c["pid_deadband_ev"] = 0.20

        def run(disturb_at=None, noise=0.06, steps=160):
            ctrl = ExposureController(c)
            rnd = random.Random(11)
            us, gain, lum = 2350, 1.0, 0.020
            pipe = [(us, gain)] * 2           # the real control latency
            energies = []
            for i in range(steps):
                if disturb_at is not None and i == disturb_at:
                    lum = 0.005
                su, sg = pipe.pop(0)
                m = max(1.0, lum * su * sg * (1.0 + rnd.gauss(0, noise)))
                d = ctrl.update(FrameStats(meter=m, p50=m, p95=m * 1.6),
                                su, sg, lux=None, now=i * 0.1)
                us, gain = d.exposure_us, d.gain
                pipe.append((us, gain))
                energies.append(us * gain)
            return energies

        # Steady scene: exposure must barely move despite 6% metering noise.
        steady = run()
        lg = [math.log2(e) for e in steady[40:]]
        wander = statistics.pstdev(lg)
        assert wander < 0.05, "loop still hunting: %.3f EV sd" % wander

        # Real change: it must still respond, not sit there filtered.
        moved = run(disturb_at=60)
        before = statistics.median(moved[50:60])
        after = statistics.median(moved[-20:])
        assert after > before * 2.0, \
            "loop failed to respond to a 4x light drop (%.0f -> %.0f)" % (before, after)
        if os.path.exists("/tmp/birdshot-selftest-ae.json"):
            os.unlink("/tmp/birdshot-selftest-ae.json")
        return "steady %.3f EV sd, still tracks a 4x light change" % wander
    check("AE damping", t_ae_damping)

    def t_storage():
        import shutil
        from birdshot.analysis import FrameStats
        from birdshot.storage import Storage
        tmp = "/tmp/birdshot-selftest-data"
        shutil.rmtree(tmp, ignore_errors=True)
        c = Config("/tmp/birdshot-selftest.json")
        c["data_root"] = tmp
        c["offload_to_usb"] = False
        s = Storage(c)
        s.start_session("test")
        st = FrameStats(verdict="ok")
        p = s.write_frame(b"\xff\xd8\xff\xd9", 2000, 1.0, 1, st)
        assert p and "ms20" in p, p
        # COLLECT frames use the same 16-digit centisecond stamp as rapid.
        stem = os.path.basename(p)[:-4]
        assert len(stem) == 16 and stem.isdigit(), stem
        summary = s.close_session()
        assert summary["frames"] == 1
        idx = os.path.join(summary["path"], "index.jsonl")
        entry = json.loads(open(idx).read().strip())
        assert entry["shutter_dir"] == "ms20"
        s.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        return "ms20/%s.jpg, index.jsonl, session summary" % stem
    check("storage layout", t_storage)

    def t_rapid_naming():
        import shutil
        from birdshot.storage import Storage
        tmp = "/tmp/birdshot-selftest-rapid"
        shutil.rmtree(tmp, ignore_errors=True)
        c = Config("/tmp/birdshot-selftest.json")
        c["data_root"] = tmp
        c["offload_to_usb"] = False
        s = Storage(c)
        s.start_session("rapid")
        when = 1785520458.5   # a fixed instant, so the name is deterministic
        names = [os.path.basename(s.write_rapid(b"\xff\xd8\xff\xd9", i, when))
                 for i in range(1, 4)]
        from birdshot.naming import timestamp_name, parse_timestamp_name
        expect0 = timestamp_name(when) + ".jpg"
        assert names[0] == expect0, (names, expect0)
        # 16 digits: YYYYMMDDHHMMSScc, centiseconds within the minute.
        assert len(names[0]) == 16 + 4, names[0]
        assert names[0][:16].isdigit(), names[0]
        # Identical instant -> suffixed, never overwritten.
        assert names[1].endswith("_001.jpg"), names
        assert names[2].endswith("_002.jpg"), names
        assert len(set(names)) == 3, names
        # Round-trips back to the same instant, to centisecond precision.
        back = parse_timestamp_name(names[0])
        assert back is not None and abs(back - when) < 0.011, (back, when)
        # Distinct centiseconds must give distinct names with no suffix at all.
        base = 1785520458.0
        stamps = [timestamp_name(base + i * 0.03) for i in range(30)]
        assert len(set(stamps)) == 30, "centisecond resolution not distinguishing"
        # And they must sort chronologically as plain strings.
        assert stamps == sorted(stamps), "names do not sort chronologically"
        # Flat layout: no shutter subdirectories.
        assert not [d for d in os.listdir(s.session.path) if os.path.isdir(
            os.path.join(s.session.path, d))], "rapid must stay flat"
        s.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        return "%s, 16 digits, 30 frames at 30 ms all distinct" % expect0
    check("rapid flat naming", t_rapid_naming)

    def t_all_names():
        """Every generated name uses the centisecond stamp -- audited, not assumed."""
        import re as _re, shutil, tempfile
        from birdshot.naming import timestamp_name
        from birdshot.storage import Session, Storage
        from birdshot.analysis import FrameStats

        stamp16 = _re.compile(r"^\d{16}$")
        tmp = tempfile.mkdtemp()
        c = Config("/tmp/birdshot-selftest.json")
        c["data_root"] = tmp
        c["offload_to_usb"] = False
        c["cascade_enabled"] = False

        # 1. session directories
        sess = Session(tmp, "rapid")
        kind, _, stamp = sess.id.partition("-")
        assert kind == "rapid" and stamp16.match(stamp), sess.id

        # 2. COLLECT frames  3. RAPID frames
        st = Storage(c)
        st.start_session("sess")
        p1 = st.write_frame(b"\xff\xd8\xff\xd9", 2000, 1.0, 1, FrameStats(verdict="ok"))
        assert stamp16.match(os.path.basename(p1)[:-4]), p1
        st.close_session()
        st.start_session("rapid")
        p2 = st.write_rapid(b"\xff\xd8\xff\xd9", 1)
        assert stamp16.match(os.path.basename(p2)[:-4]), p2
        st.close_session(); st.stop(0)

        # 4. video filenames come from the same helper
        import birdshot.camera as cam
        src = open(cam.__file__).read()
        assert 'kw.get("name") or timestamp_name()' in src, \
            "video filename no longer derives from timestamp_name"
        assert "%Y%m%d-%H%M%S" not in src, "camera.py still has old-style naming"
        import birdshot.storage as sto
        sto_src = open(sto.__file__).read()
        assert "int(time.time())" not in sto_src, "storage.py still uses raw epoch names"

        shutil.rmtree(tmp, ignore_errors=True)
        return "sessions, COLLECT, RAPID and video all 16-digit"
    check("all names use centiseconds", t_all_names)

    def t_exif():
        import shutil, tempfile, glob
        from birdshot import exif as exifmod
        from birdshot.storage import Storage
        from birdshot.analysis import FrameStats
        from birdshot.naming import timestamp_name
        ok, backend = exifmod.available()
        if not ok:
            raise AssertionError(backend)

        tmp = tempfile.mkdtemp()
        c = Config("/tmp/birdshot-selftest.json")
        c["data_root"] = tmp; c["offload_to_usb"] = False; c["cascade_enabled"] = False
        c["exif_focal_mm"] = 16.0; c["exif_fnumber"] = 2.8; c["exif_lens"] = "C-mount 16mm"
        # A minimal but real JPEG, so piexif has something valid to splice into.
        import numpy as np, simplejpeg
        jpg = simplejpeg.encode_jpeg(
            np.random.randint(0, 255, (32, 32, 3), dtype=np.uint8), quality=80,
            colorspace="RGB")
        s2 = Storage(c); s2.start_session("sess")
        st = FrameStats(verdict="ok", p50=46.0, meter=47.0, sharpness_norm=14.9)
        p = s2.write_frame(jpg, 1956, 1.939, 9, st)
        s2.close_session(); s2.stop(0)
        sess_dir = os.path.dirname(os.path.dirname(p))

        res = exifmod.tag_session(sess_dir, c)
        assert res["ok"] and res["tagged"] == 1, res
        back = exifmod.read_back(p)
        assert back["Make"] == "Raspberry Pi", back
        assert back["ExposureTime"] == "1956/1000000", back
        assert back["ISO"] == 194, back            # gain 1.939 -> ISO 194
        assert back["UserComment"]["verdict"] == "ok", back

        # The EXIF timestamp must agree with the filename, to the centisecond.
        stem = os.path.basename(p)[:16]
        stamp = back["DateTimeOriginal"].replace(":", "").replace(" ", "")
        assert stamp == stem[:14], (stamp, stem)
        assert back["SubSecTimeOriginal"] == stem[14:16], (back, stem)

        # Tagging must be lossless and idempotent.
        n1 = os.path.getsize(p)
        assert exifmod.tag_session(sess_dir, c)["tagged"] == 1
        assert abs(os.path.getsize(p) - n1) < 64, "re-tagging grew the file"
        shutil.rmtree(tmp, ignore_errors=True)
        return "%s, EXIF stamp matches filename to the centisecond" % backend
    check("exif tagging", t_exif)

    def t_focus_map():
        from birdshot.analysis import focus_map
        rng = np.random.RandomState(1)
        y = np.full((480, 640), 128, np.uint8)
        # Put real detail in one tile only; the map must point at it.
        y[40:90, 480:580] = rng.randint(0, 255, (50, 100))
        fmap, best, peak = focus_map(y, rows=9, cols=12)
        assert fmap.shape == (9, 12), fmap.shape
        assert peak > 0, "no energy detected"
        assert abs(fmap.max() - 1.0) < 1e-5, fmap.max()
        # Tile row ~1, col ~9 given a 9x12 grid over 480x640.
        assert best[0] in (0, 1) and best[1] in (8, 9, 10), best
        return "located detail at row %d col %d" % best
    check("focus map", t_focus_map)

    def t_encode_select():
        import shutil
        from birdshot import timelapse as tl2
        tmp = "/tmp/birdshot-selftest-enc"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "sub"), exist_ok=True)
        for n in ("20260731120001.jpg", "20260731120002.jpg"):
            open(os.path.join(tmp, n), "wb").write(b"x")
        open(os.path.join(tmp, "sub", "20260731120003.jpg"), "wb").write(b"x")
        assert not tl2.has_index(tmp)
        assert len(tl2.frames_from_folder(tmp, recursive=True)) == 3
        assert len(tl2.frames_from_folder(tmp, recursive=False)) == 2
        flat = tl2.select_frames(tmp, only_ok=True, recursive=False)
        assert [os.path.basename(f) for f in flat] == sorted(
            ["20260731120001.jpg", "20260731120002.jpg"]), flat
        shutil.rmtree(tmp, ignore_errors=True)
        return "plain folders and subfolders both selectable"
    check("encode source selection", t_encode_select)

    def t_autowrite():
        import shutil
        from birdshot import autostart
        tmp = "/tmp/birdshot-selftest-auto"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        marker = os.path.join(tmp, autostart.MARKER)
        with open(marker, "w") as fh:
            fh.write("# a comment\nmode=ram\nres=2\ninterval=15\n"
                     "delete_after_copy=yes\nbogus=1\nnot a pair\n")
        opts, warns = autostart.parse_marker(marker)
        assert opts == {"mode": "ram", "res": 2, "interval": 15,
                        "delete_after_copy": True}, opts
        assert len(warns) == 2, warns   # unknown key + malformed line
        c = Config("/tmp/birdshot-selftest.json")
        summary = autostart.apply(c, {"mount": tmp, "options": opts, "warnings": warns})
        assert c["usb_root"] == os.path.join(tmp, "birdshot"), c["usb_root"]
        assert c["offload_continuous"] and c["offload_interval_s"] == 15
        assert c["offload_delete_source"] is True
        assert c["capture_mode"] == 2 and c["rapid_mode"] == "ram"
        assert summary["start"] is True   # defaults to starting
        # An empty marker must still be valid and mean "just use defaults".
        with open(marker, "w") as fh:
            fh.write("")
        opts2, warns2 = autostart.parse_marker(marker)
        assert opts2 == {} and warns2 == [], (opts2, warns2)
        shutil.rmtree(tmp, ignore_errors=True)
        return "parsed, 2 warnings surfaced, config applied"
    check("autowrite.yes handling", t_autowrite)

    def t_cascade():
        import shutil
        from birdshot import cascade as csc
        base = "/tmp/birdshot-selftest-casc"
        shutil.rmtree(base, ignore_errors=True)
        t0 = csc.Tier(os.path.join(base, "t0"), "top", min_free_mb=0, flush_after_s=0)
        t1 = csc.Tier(os.path.join(base, "t1"), "mid", min_free_mb=0, flush_after_s=0)
        t2 = csc.Tier(os.path.join(base, "t2"), "bottom", min_free_mb=0, flush_after_s=0)
        t2.keep = True
        for t in (t0, t1, t2):
            t.ensure()

        gdir = os.path.join(t0.path, "sess-1", "g000001")
        os.makedirs(gdir)
        payload = {}
        for i in range(5):
            name = "f%02d.jpg" % i
            data = bytes([i]) * (100 + i)
            open(os.path.join(gdir, name), "wb").write(data)
            payload[name] = len(data)
        # A half-written frame must not be sealed into the manifest.
        open(os.path.join(gdir, "f99.jpg.part"), "wb").write(b"incomplete")

        man = csc.seal_group(gdir, {"session": "sess-1"})
        assert man["frames"] == 5, man
        assert all(n != "f99.jpg.part" for n, _ in man["files"])
        assert len(csc.sealed_groups(t0.path)) == 1

        c = Config("/tmp/birdshot-selftest.json")
        c["cascade_ring"] = False
        casc = csc.Cascade([t0, t1, t2], c)
        casc.start()
        deadline = time.time() + 25
        while time.time() < deadline:
            if os.path.isdir(os.path.join(t2.path, "sess-1", "g000001")):
                break
            time.sleep(0.2)
        casc.stop(); casc.join(timeout=10)

        dst = os.path.join(t2.path, "sess-1", "g000001")
        assert os.path.isdir(dst), "group never reached the bottom tier"
        for name, size in payload.items():
            f = os.path.join(dst, name)
            assert os.path.exists(f) and os.path.getsize(f) == size, name
        # Source tiers must have cleared themselves; the archive must not.
        assert not os.path.isdir(gdir), "top tier did not clear"
        assert not os.path.isdir(os.path.join(t1.path, "sess-1", "g000001")), "mid did not clear"

        # Verification must reject a truncated copy rather than allow deletion.
        open(os.path.join(dst, "f00.jpg"), "wb").write(b"x")
        ok, why = csc.verify_group(man, t2, "sess-1/g000001")
        assert not ok and "size" in why, (ok, why)
        shutil.rmtree(base, ignore_errors=True)
        return "5 files migrated 3 tiers, sources cleared, truncation caught"
    check("storage cascade", t_cascade)

    def t_tone():
        from birdshot import tone
        # Its own config file: presets share tone_lift and friends, so a value
        # left behind by one case must not silently change the next.
        if os.path.exists("/tmp/birdshot-selftest-tone.json"):
            os.unlink("/tmp/birdshot-selftest-tone.json")
        c = Config("/tmp/birdshot-selftest-tone.json")
        sx, sy = tone.stock_curve()
        # The curve the ISP actually uses must match the published HQ-cam vector.
        assert len(sx) == 33, len(sx)
        for got, want in ((sy[1], 5040 / 65535.0), (sy[4], 15312 / 65535.0),
                          (sy[12], 33975 / 65535.0), (sy[-1], 1.0)):
            assert abs(got - want) < 1e-6, (got, want)

        # Stock must not generate a tuning override at all.
        c["tone_curve"] = "stock"
        assert tone.build_tuning(c) is None
        assert tone.is_stock(c)

        # Every other preset must produce a patched, monotonic 33-point curve
        # anchored at pure black and pure white -- including with a lift applied,
        # which used to float the whole curve off zero and grey out the blacks.
        for kind in ("linear", "gamma", "contrast", "lift"):
            c["tone_curve"] = kind
            c["tone_lift"] = 0.18 if kind == "lift" else 0.0
            xs, ys = tone.curve_from_cfg(c)
            assert len(xs) == 33 and ys[0] == 0.0 and abs(ys[-1] - 1.0) < 1e-9, kind
            assert all(ys[i] <= ys[i + 1] + 1e-9 for i in range(len(ys) - 1)), \
                "%s is not monotonic" % kind
            t = tone.build_tuning(c)
            assert t is not None, kind
            algos = t["algorithms"] if isinstance(t.get("algorithms"), list) else [t]
            flat = None
            for a in algos:
                for n, b in a.items():
                    if "contrast" in n and isinstance(b, dict):
                        flat = b["gamma_curve"]
            assert flat and len(flat) == 66, kind
            assert flat[0] == 0 and flat[-1] == 65535, kind

        # Linear really is the identity, and gamma really lifts mid-tones.
        c["tone_curve"] = "linear"
        lx, ly = tone.curve_from_cfg(c)
        assert max(abs(a - b) for a, b in zip(lx, ly)) < 1e-9
        c["tone_curve"] = "gamma"; c["tone_gamma"] = 2.2
        _, gy = tone.curve_from_cfg(c)
        assert gy[4] > lx[4], "gamma should lift mid-tones above linear"

        # Rolloff must lift a dark average AND round off the top.
        c["tone_curve"] = "rolloff"; c["tone_lift"] = 0.18
        c["tone_knee"] = 0.65; c["tone_shoulder"] = 2.0
        rx, ry = tone.curve_from_cfg(c)
        assert all(ry[i] <= ry[i + 1] + 1e-9 for i in range(len(ry) - 1)), "not monotonic"
        assert ry[0] == 0.0 and abs(ry[-1] - 1.0) < 1e-9
        assert tone._interp(0.05, rx, ry) > tone._interp(0.05, sx, sy) + 0.02, \
            "rolloff should lift the dark end"
        # Steps must shrink toward white -- that is the shoulder rounding off.
        gaps = [ry[i + 1] - ry[i] for i in range(len(ry) - 5, len(ry) - 1)]
        assert all(gaps[i] >= gaps[i + 1] - 1e-9 for i in range(len(gaps) - 1)), gaps
        stock_gaps = [sy[i + 1] - sy[i] for i in range(len(sy) - 5, len(sy) - 1)]
        assert gaps[-1] < stock_gaps[-1], "whites are not being rounded off"
        assert tone.build_tuning(c) is not None

        # The LUT exists for offline use and must be monotonic 0..255.
        c["tone_curve"] = "stock"
        lut = tone.lut8(c)
        assert lut.shape == (256,) and lut[0] == 0 and lut[255] == 255
        assert all(lut[i] <= lut[i + 1] for i in range(255))
        c["tone_curve"] = "stock"; c.save()
        os.unlink("/tmp/birdshot-selftest-tone.json")
        return "ISP curve verified, 5 presets anchored at black and white"
    check("tone curve", t_tone)

    def t_yuv():
        from birdshot.camera import yuv420_to_rgb
        buf = np.zeros((720, 640), np.uint8)
        buf[:480] = 200
        buf[480:] = 128
        rgb = yuv420_to_rgb(buf, 640, 480, half=True)
        assert rgb.shape == (240, 320, 3), rgb.shape
        assert abs(int(rgb.mean()) - 200) < 6, rgb.mean()
        return "grey stays grey, %s" % (rgb.shape,)
    check("YUV420 -> RGB", t_yuv)

    def t_camera():
        got = {}

        def on_event(name, payload):
            got.setdefault(name, []).append(payload)

        engine, storage, _ = _engine(cfg, on_event)
        engine.send("preview")
        deadline = time.time() + 20
        while time.time() < deadline and not got.get("preview"):
            time.sleep(0.2)
        assert got.get("preview"), "no preview frames in 20s"
        pv = got["preview"][-1]
        st = pv["stats"]
        engine.send("stop")
        engine.shutdown()
        engine.join(timeout=10)
        storage.stop()
        return ("%d frames, %s, lux %s, p50 %d"
                % (len(got["preview"]), st.verdict,
                   ("%.0f" % pv["lux"]) if pv.get("lux") else "-", int(st.p50)))
    check("live camera preview", t_camera)

    def t_ffmpeg():
        import subprocess
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True)
        assert r.returncode == 0
        return r.stdout.decode().split("\n")[0][:44]
    check("ffmpeg present", t_ffmpeg)

    def t_qt():
        from PyQt5.QtCore import QT_VERSION_STR
        return "Qt " + QT_VERSION_STR
    check("PyQt5 importable", t_qt)

    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(fn=cmd_info)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    sub.add_parser("sessions").set_defaults(fn=cmd_sessions)

    dr = sub.add_parser("doctor", help="how is this install doing? deps, cameras, storage")
    dr.add_argument("--json", action="store_true", help="machine-readable, for installers")
    dr.add_argument("--write-config", action="store_true",
                    help="persist the validated config to disk")
    dr.set_defaults(fn=cmd_doctor)

    c = sub.add_parser("capture")
    c.add_argument("-n", "--count", type=int, default=20)
    c.add_argument("-m", "--mode", type=int, help="0=full 1=binned 2=fast")
    c.add_argument("--manual", action="store_true")
    c.add_argument("--shutter", type=int, help="manual shutter in us")
    c.add_argument("--timeout", type=float, default=300)
    c.set_defaults(fn=cmd_capture)

    r = sub.add_parser("rapid", help="fastest single photos, flat YYYYmmddHHMMSS names")
    r.add_argument("-n", "--count", type=int, default=0)
    r.add_argument("--mode", choices=["ram", "continuous"], default="ram")
    r.add_argument("--res", type=int, help="0=full 1=binned 2=fast")
    r.add_argument("--timeout", type=float, default=120)
    r.set_defaults(fn=cmd_rapid)

    fl = sub.add_parser("flush", help="force all groups down to the bottom tier")
    fl.add_argument("--timeout", type=float, default=900)
    fl.set_defaults(fn=cmd_flush)

    ex = sub.add_parser("exif", help="write EXIF into a session's frames")
    ex.add_argument("session")
    ex.add_argument("--only-ok", action="store_true",
                    help="only frames that passed the quality gates")
    ex.set_defaults(fn=cmd_exif)

    cs = sub.add_parser("cascade", help="group capture with tiered background migration")
    cs.add_argument("-n", "--count", type=int, default=0)
    cs.add_argument("-g", "--group", type=int, default=200, help="frames per group")
    cs.add_argument("--tiers", help="comma-separated tier paths, top first")
    cs.add_argument("--res", type=int, help="0=full 1=binned 2=fast")
    cs.add_argument("--ring", action="store_true", help="drop oldest when full")
    cs.add_argument("--timeout", type=float, default=300)
    cs.set_defaults(fn=cmd_cascade)

    t = sub.add_parser("timelapse")
    t.add_argument("-i", "--interval", type=float, default=5.0)
    t.add_argument("-n", "--count", type=int, default=0)
    t.add_argument("--timeout", type=float, default=0)
    t.set_defaults(fn=cmd_timelapse)

    a = sub.add_parser("assemble")
    a.add_argument("session")
    a.add_argument("-o", "--output")
    a.add_argument("--fps", type=int, default=60)
    a.add_argument("--width", type=int)
    a.add_argument("--all", action="store_true", help="include rejected frames")
    a.add_argument("--exif", action="store_true", help="stamp EXIF first")
    a.set_defaults(fn=cmd_assemble)

    args = ap.parse_args()
    cfg = Config(args.config) if args.config else Config()
    return args.fn(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
