# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""The capture engine: one Picamera2 instance, driven by a state machine.

Runs on its own thread and talks to the GUI purely through an event callback,
so nothing here imports Qt and the whole engine can be driven headless.

Design notes that matter:

* **One configuration, two streams.** ``main`` is the full-resolution capture,
  ``lores`` is a 640x480 YUV plane that libcamera produces for free. Metering,
  quality gates, the histogram and the preview all run off ``lores``, so none of
  them cost a JPEG decode or a downscale.
* **Threaded JPEG encode.** Encoding a 12.3 MP frame is the bottleneck on this
  CM4 -- a single thread manages 2 fps, four threads manage 4. The sensor itself
  will do 10, so this is worth the complexity.
* **Exposure latency is respected.** A control set now lands two frames later.
  The AE loop therefore reads the *actual* exposure back from each frame's
  metadata and holds off on a new correction until its last one has landed,
  which is what stops the loop oscillating.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np
import simplejpeg

from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

from . import analysis
from .analysis import FrameStats, analyse, focus_map, meter_only, refine_with_hires
from .config import EXPOSURE_MIN_US, GAIN_MIN
from .exposure import ExposureController
from .naming import timestamp_name
from .storage import Storage
from . import tone

LORES_SIZE = (640, 480)
FOCUS_CROP = 512  # native-resolution centre crop used for the focus measure
# How many frames to wait for a control to land before assuming the sensor
# clamped it and carrying on from what it actually did.
#
# Measured on this CM4: a gain change takes 5-7 frames to appear in metadata,
# not the 2 the docs imply -- the ISP pipeline is deeper than that. At 4 the
# timeout fired before the sensor had answered, so the loop "recovered" from a
# request that was about to land, re-issued a different one, and hunted forever.
AE_LAND_FRAMES = 10

# Camera buffers come from the kernel's CMA pool, which is 512 MB on this board
# and is NOT ordinary RAM -- it must be physically contiguous, so it runs out
# far sooner than free memory suggests. One 4056x3040 still configuration costs
# (main 37 MB + raw 18.5 MB) per buffer, so six buffers is 333 MB, most of the
# pool. Buffer counts are therefore scaled by frame size.
CMA_TOTAL_MB = 512
BIG_FRAME_PIXELS = 8_000_000


def buffer_count_for(size) -> int:
    return 4 if (size[0] * size[1]) >= BIG_FRAME_PIXELS else 6


def cma_free_mb() -> float:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("CmaFree:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def config_cma_mb(size, buffers: int) -> float:
    """Roughly what a configuration will demand from the CMA pool."""
    px = size[0] * size[1]
    return buffers * (px * 3 + px * 1.5) / 1e6

IDLE, PREVIEW, BURST, TIMELAPSE, VIDEO = "idle", "preview", "burst", "timelapse", "video"
RAPID, DRAIN = "rapid", "drain"


class CameraConfigError(RuntimeError):
    """Raised when libcamera cannot allocate buffers for a configuration."""

# Fraction of currently-available RAM a RAM burst is allowed to fill.
RAM_BUDGET_FRAC = 0.55


def available_ram_mb() -> float:
    """MemAvailable, which accounts for reclaimable page cache."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 512.0


def yuv420_to_rgb(buf: np.ndarray, width: int, height: int, half: bool = True) -> np.ndarray:
    """Convert a planar YUV420 buffer to RGB with integer maths.

    ``half`` converts at half resolution (chroma resolution) and skips the
    chroma upsample entirely -- roughly 4x faster, and indistinguishable in a
    preview pane that is being scaled anyway.
    """
    # Planar layout for a WxH frame is H rows of Y, then H/4 rows of U and H/4
    # rows of V, each row being ``width`` bytes wide.
    y = buf[:height, :width]
    quarter = height // 4
    u = buf[height : height + quarter, :width].reshape(height // 2, width // 2)
    v = buf[height + quarter : height + 2 * quarter, :width].reshape(height // 2, width // 2)

    if half:
        yy = y[::2, ::2].astype(np.int32)
    else:
        yy = y.astype(np.int32)
        u = np.repeat(np.repeat(u, 2, 0), 2, 1)
        v = np.repeat(np.repeat(v, 2, 0), 2, 1)

    uu = u.astype(np.int32) - 128
    vv = v.astype(np.int32) - 128
    h, w = yy.shape
    uu, vv = uu[:h, :w], vv[:h, :w]

    r = np.clip(yy + ((91881 * vv) >> 16), 0, 255)
    g = np.clip(yy - ((22554 * uu + 46802 * vv) >> 16), 0, 255)
    b = np.clip(yy + ((116130 * uu) >> 16), 0, 255)
    return np.dstack((r, g, b)).astype(np.uint8)


class CameraEngine(threading.Thread):
    """Owns the camera. Commands go in through :meth:`send`, results come out
    through the ``on_event`` callback as ``(name, payload)``."""

    def __init__(self, cfg, storage: Storage, on_event: Callable[[str, Dict[str, Any]], None]):
        super().__init__(daemon=True, name="camera")
        self.cfg = cfg
        self.storage = storage
        self.on_event = on_event
        self.controller = ExposureController(cfg)

        # Bounds how many capture requests the encoder pool may hold at once.
        # Re-sized to buffer_count - 2 whenever the camera is configured, so the
        # camera always has buffers left to fill.
        self._inflight = threading.Semaphore(2)
        self._cmds: "queue.Queue[Tuple[str, dict]]" = queue.Queue()
        self._state = IDLE
        self._running = True
        self._cam: Optional[Picamera2] = None
        self._configured: Optional[Tuple] = None
        self._encoder_pool: Optional[Any] = None

        # Exposure bookkeeping.
        st = cfg.get("state", {}) or {}
        self._exposure_us = int(st.get("last_shutter_us", cfg["manual_shutter_us"]))
        self._gain = float(st.get("last_gain", cfg["manual_gain"]))
        self._requested: Optional[Tuple[int, float]] = None
        self._ae_waiting = 0
        # Real limits reported by the sensor for the active mode, so we stop
        # asking for exposures and gains it cannot deliver.
        self._limits: Dict[str, Tuple[float, float]] = {}
        self._seq = 0

        # Burst / timelapse bookkeeping.
        self._target_frames = 0
        self._taken = 0
        self._next_shot = 0.0
        self._video_out: Optional[str] = None
        self._video_started = 0.0
        self._last_video_stat = 0.0
        self._last_written: Optional[str] = None
        self._encoder: Optional[H264Encoder] = None
        self._last_preview = 0.0
        self._last_latest = 0.0
        self._fps_window: list = []
        # When on, every preview frame also carries a 1:1 native-resolution
        # centre crop so the focus monitor can show real sensor pixels. Costs a
        # full-frame copy per frame, so it is off unless something is watching.
        self._focus_assist = False
        self._focus_map_on = False

        # Rapid mode.
        self._profile: Optional[Dict[str, float]] = ({} if os.environ.get("BIRDSHOT_PROFILE")
                                                     else None)
        self._last_tick_end = 0.0
        self._last_offload = 0.0
        self._rapid_t_first = 0.0
        self._rapid_t_last = 0.0
        self._rapid_ram = False
        self._ram_frames: list = []
        self._ram_budget_mb = 0.0
        self._rapid_started = 0.0

    # ==================================================================
    # public API
    # ==================================================================
    def send(self, cmd: str, **kwargs: Any) -> None:
        self._cmds.put((cmd, kwargs))

    def shutdown(self) -> None:
        self._running = False
        self._cmds.put(("stop", {}))

    @property
    def state(self) -> str:
        return self._state

    # ==================================================================
    # camera lifecycle
    # ==================================================================
    def _transform(self) -> Transform:
        return Transform(hflip=1 if self.cfg["hflip"] else 0,
                         vflip=1 if self.cfg["vflip"] else 0)

    def _config_key(self, video: bool) -> Tuple:
        size = self.cfg.video_size() if video else self.cfg.capture_size()
        # The tone curve lives in the tuning file, which the ISP reads only when
        # the camera opens -- so a curve change has to force a reconfigure.
        return (size, bool(self.cfg["hflip"]), bool(self.cfg["vflip"]), video,
                self.cfg.get("tone_curve"), self.cfg.get("tone_gamma"),
                self.cfg.get("tone_contrast"), self.cfg.get("tone_lift"))

    def _ensure_camera(self, video: bool = False) -> None:
        key = self._config_key(video)
        if self._cam is not None and self._configured == key:
            return

        # Fully release the previous configuration before allocating the next.
        # stop() alone leaves the buffers held, so reconfiguring (stills ->
        # video, or a resolution change) would try to allocate a second set on
        # top of the first. With a full-resolution still config costing 333 MB
        # of a 512 MB CMA pool that fails outright, and libcamera's failure path
        # takes the process down. Closing and recreating costs about a second
        # and is only paid on an actual mode change.
        if self._cam is not None:
            try:
                self._cam.stop()
            except Exception:
                pass
            try:
                self._cam.close()
            except Exception:
                pass
            self._cam = None
            self._configured = None
            # Give the kernel a moment to return the pages to the CMA pool.
            for _ in range(20):
                if cma_free_mb() > 0.8 * CMA_TOTAL_MB:
                    break
                time.sleep(0.05)

        size = self.cfg.video_size() if video else self.cfg.capture_size()
        buffers = buffer_count_for(size)
        need = config_cma_mb(size, buffers)
        free = cma_free_mb()
        if need > free:
            self._emit("error", {
                "msg": "%dx%d needs about %.0f MB of contiguous DMA memory but "
                       "only %.0f MB is free. Close other camera users, or "
                       "raise cma= in /boot/cmdline.txt."
                       % (size[0], size[1], need, free)})

        tuning = tone.build_tuning(self.cfg)
        if tuning is not None:
            self._emit("state", {"tone": self.cfg.get("tone_curve")})
        self._cam = Picamera2(tuning=tuning) if tuning else Picamera2()
        lores = LORES_SIZE
        # lores must not exceed main, and libcamera wants even dimensions.
        lw = min(lores[0], size[0]) & ~1
        lh = min(lores[1], size[1]) & ~1

        if video:
            # Pin the frame duration so the recording runs at the advertised
            # rate instead of drifting with exposure time.
            fps = self.cfg.video_fps()
            frame_us = int(1_000_000 / max(fps, 1))
            cfg = self._cam.create_video_configuration(
                main={"size": size, "format": "YUV420"},
                lores={"size": (lw, lh), "format": "YUV420"},
                transform=self._transform(),
                buffer_count=buffers,
                controls={"FrameDurationLimits": (frame_us, frame_us)},
            )
        else:
            cfg = self._cam.create_still_configuration(
                main={"size": size, "format": "RGB888"},
                lores={"size": (lw, lh), "format": "YUV420"},
                transform=self._transform(),
                buffer_count=buffers,
            )
        try:
            self._cam.configure(cfg)
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {
                "msg": "could not configure %dx%d: %s (CMA free %.0f MB, needs "
                       "~%.0f MB)" % (size[0], size[1], exc, cma_free_mb(), need)})
            try:
                self._cam.close()
            except Exception:
                pass
            self._cam = None
            self._state = IDLE
            raise CameraConfigError(str(exc))

        self._configured = key
        self._lores_size = (lw, lh)
        # Never let the encoder pool hold so many requests that the camera has
        # none left to fill.
        self._inflight = threading.Semaphore(max(1, buffers - 2))

        try:
            cc = self._cam.camera_controls or {}
            et = cc.get("ExposureTime")
            ag = cc.get("AnalogueGain")
            self._limits = {}
            # libcamera reports an unknown maximum as 0 rather than omitting it.
            # Taking that literally clamps every allocation to zero and divides
            # by it, so a limit only counts when it is a real, ordered range.
            def usable(pair):
                try:
                    lo, hi = float(pair[0]), float(pair[1])
                except (TypeError, ValueError, IndexError):
                    return None
                return (lo, hi) if hi > lo > 0 else None

            for key, pair in (("exposure", et), ("gain", ag)):
                got = usable(pair) if pair else None
                if got:
                    self._limits[key] = got
            self.controller.set_limits(self._limits)
        except Exception:  # noqa: BLE001
            self._limits = {}

        self._apply_exposure(force=True)
        self._cam.start()
        # The sensor needs a couple of frames after a reconfigure before its
        # metadata is trustworthy.
        time.sleep(0.4)
        self._emit("state", {"state": self._state, "size": size, "reconfigured": True})

    def _apply_exposure(self, force: bool = False) -> None:
        if self._cam is None:
            return
        controls: Dict[str, Any] = {
            "AeEnable": False,
            "AwbEnable": True,
            "ExposureTime": int(self._exposure_us),
            "AnalogueGain": float(self._gain),
        }
        # Live ISP controls -- applied by the hardware, no CPU cost, no restart.
        for key, name in (("isp_contrast", "Contrast"),
                          ("isp_brightness", "Brightness"),
                          ("isp_saturation", "Saturation"),
                          ("isp_sharpness", "Sharpness")):
            val = self.cfg.get(key)
            if val is not None and name in getattr(self._cam, "camera_controls", {}):
                controls[name] = float(val)
        try:
            self._cam.set_controls(controls)
            self._requested = (int(self._exposure_us), float(self._gain))
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "set_controls: %s" % exc})

    def _pool(self):
        from concurrent.futures import ThreadPoolExecutor

        want = max(1, int(self.cfg["encode_threads"]))
        if self._encoder_pool is None or getattr(self._encoder_pool, "_max_workers", 0) != want:
            if self._encoder_pool is not None:
                self._encoder_pool.shutdown(wait=False)
            self._encoder_pool = ThreadPoolExecutor(want, thread_name_prefix="jpeg")
        return self._encoder_pool

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        try:
            self.on_event(name, payload)
        except Exception:
            pass

    # ==================================================================
    # main loop
    # ==================================================================
    def run(self) -> None:
        try:
            while self._running:
                self._drain_commands()
                if self._state == RAPID:
                    self._tick_rapid()
                elif self._state in (PREVIEW, BURST, TIMELAPSE):
                    self._tick_capture()
                elif self._state == VIDEO:
                    self._tick_video()
                else:
                    time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "engine died: %r" % exc, "fatal": True})
        finally:
            self._teardown()

    def _drain_commands(self) -> None:
        block = self._state == IDLE
        try:
            while True:
                cmd, kw = self._cmds.get(timeout=0.2 if block else 0)
                try:
                    self._handle(cmd, kw)
                except CameraConfigError:
                    # Already reported to the user. Stay alive and idle rather
                    # than taking the whole engine down with us.
                    self._state = IDLE
                    self._emit("state", {"state": self._state})
                except Exception as exc:  # noqa: BLE001
                    # One bad command must not kill the engine, and it must
                    # never fail silently -- a button that does nothing with no
                    # message is the worst possible outcome.
                    self._emit("error", {
                        "msg": "command %r failed: %s\n%s"
                               % (cmd, exc, traceback.format_exc())})
                block = False
        except queue.Empty:
            pass

    def _handle(self, cmd: str, kw: Dict[str, Any]) -> None:
        if cmd == "preview":
            self._ensure_camera(video=False)
            self._state = PREVIEW
        elif cmd == "burst":
            self._start_burst(kw)
        elif cmd == "rapid":
            self._start_rapid(kw)
        elif cmd == "focus_map":
            self._focus_map_on = bool(kw.get("on", False))
        elif cmd == "timelapse":
            self._start_timelapse(kw)
        elif cmd == "video":
            self._start_video(kw)
        elif cmd == "stop":
            self._stop_activity()
        elif cmd == "reconfigure":
            if self._state != IDLE:
                self._ensure_camera(video=self._state == VIDEO)
        elif cmd == "set_exposure":
            self._exposure_us = int(kw.get("exposure_us", self._exposure_us))
            self._gain = float(kw.get("gain", self._gain))
            self._apply_exposure()
        elif cmd == "reset_ae":
            self.controller.reset()
        elif cmd == "focus_assist":
            self._focus_assist = bool(kw.get("on", False))
        elif cmd == "single":
            self._capture_single(kw)
        self._emit("state", {"state": self._state})

    # ------------------------------------------------------------------
    def _start_burst(self, kw: Dict[str, Any]) -> None:
        self._ensure_camera(video=False)
        self.storage.start_session("sess")
        self._target_frames = int(kw.get("count", self.cfg["burst_count"]) or 0)
        self._taken = 0
        self._seq = int(self.cfg.get("state", {}).get("frame_seq", 0) or 0)
        self.controller.reset()
        self._seed_exposure()
        self._fps_window = []
        self._state = BURST

    def _start_rapid(self, kw: Dict[str, Any]) -> None:
        """Fastest-possible stills, written as flat YYYYmmddHHMMSS.jpg.

        Two strategies:

        ``continuous`` -- encode inline on the pool. Runs indefinitely, limited
        by JPEG throughput (3.3 fps at full resolution, 8.6 binned).

        ``ram`` -- copy frames straight to memory and encode nothing until the
        burst ends. This removes the encoder from the critical path entirely,
        leaving only the sensor and the buffer copy: 4.5 fps at full resolution,
        22.9 binned. Duration is bounded by RAM rather than time, and the frames
        are drained to disk afterwards.
        """
        self._ensure_camera(video=False)
        self.storage.start_session("rapid")
        self._rapid_ram = kw.get("mode", "continuous") == "ram"
        self._taken = 0
        self._seq = 0
        self._ram_frames = []
        self._fps_window = []
        self._rapid_started = time.monotonic()
        self.controller.reset()
        self._seed_exposure()

        if self._rapid_ram:
            w, h = self.cfg.capture_size()
            per_frame_mb = w * h * 3 / 1e6
            self._ram_budget_mb = available_ram_mb() * RAM_BUDGET_FRAC
            cap = int(self._ram_budget_mb / max(per_frame_mb, 0.1))
            requested = int(kw.get("count", 0) or 0)
            self._target_frames = min(requested, cap) if requested else cap
            self._emit("rapid", {
                "phase": "start", "mode": "ram", "capacity": cap,
                "target": self._target_frames, "per_frame_mb": per_frame_mb,
                "budget_mb": self._ram_budget_mb,
            })
        else:
            self._target_frames = int(kw.get("count", 0) or 0)
            self._emit("rapid", {"phase": "start", "mode": "continuous",
                                 "target": self._target_frames})
        self._state = RAPID

    def _destination_label(self) -> str:
        """Where frames end up, not where they are being written right now."""
        from .cascade import friendly_label

        casc = getattr(self.storage, "cascade", None)
        if casc is not None and casc.tiers:
            return casc.tiers[-1].label
        if self.cfg["offload_to_usb"]:
            return friendly_label(self.cfg["usb_root"])
        return friendly_label(self.cfg["data_root"])

    def _camera_ready(self) -> bool:
        """Guard against issuing a request to a stopped camera.

        picamera2's capture_request() blocks indefinitely rather than raising if
        the camera is not started, so one missed restart hangs the engine thread
        permanently. Cheap to check, and it turns a hang into a log line.
        """
        if self._cam is None:
            return False
        if not getattr(self._cam, "started", True):
            self._emit("error", {"msg": "camera was not running; restarting it"})
            try:
                self._configured = None
                self._ensure_camera(video=(self._state == VIDEO))
            except Exception as exc:  # noqa: BLE001
                self._emit("error", {"msg": "restart failed: %s" % exc})
                self._state = IDLE
                return False
        return True

    def _maybe_roll_group(self) -> None:
        """Seal the open group once full and hand it to the cascade.

        Sealing is what makes a group eligible for migration, so this is the
        only coupling between capture and the background movers -- capture never
        waits on them.
        """
        if not self.cfg["cascade_enabled"] or self.storage.session is None:
            return
        sealed = self.storage.session.roll_group_if_needed(
            int(self.cfg["group_frames"]),
            int(self.cfg["group_mb"]) * 1_000_000,
        )
        if sealed:
            self.storage.notify_group_sealed(sealed)
            self._emit("group", {"sealed": sealed,
                                 "seq": self.storage.session.group_seq})

    def _maybe_offload(self) -> None:
        """Trickle the in-progress session to USB on a timer.

        Unattended runs (an autowrite.yes stick) have nobody around to press
        offload, and a run can outlast the eMMC's free space. rsync is
        incremental, so repeating it costs little.
        """
        if not self.cfg["offload_continuous"] or self.storage.session is None:
            return
        now = time.monotonic()
        if now - self._last_offload < float(self.cfg["offload_interval_s"]):
            return
        self._last_offload = now
        self.storage.offload_now(self.storage.session.path)

    def _prof(self, key: str, t0: float) -> float:
        """Accumulate stage timings when BIRDSHOT_PROFILE is set in the environment."""
        now = time.monotonic()
        if self._profile is not None:
            self._profile[key] = self._profile.get(key, 0.0) + (now - t0)
            self._profile["_n_" + key] = self._profile.get("_n_" + key, 0) + 1
        return now

    def capture_rate(self) -> float:
        """Frames per second measured across the capture itself."""
        span = self._rapid_t_last - self._rapid_t_first
        if span <= 0 or self._taken < 2:
            return 0.0
        return (self._taken - 1) / span

    def profile_report(self) -> Optional[Dict[str, float]]:
        if self._profile is None:
            return None
        n = max(1, self._profile.get("_n_req", 1))
        return {k: v / n * 1000.0 for k, v in sorted(self._profile.items())
                if not k.startswith("_n_")}

    def _tick_rapid(self) -> None:
        if not self._camera_ready():
            time.sleep(0.1)
            return
        if self._profile is not None:
            now = time.monotonic()
            if self._last_tick_end:
                self._profile["gap"] = (self._profile.get("gap", 0.0)
                                        + now - self._last_tick_end)
        gap_mark = time.monotonic()
        space = self.storage.space_state()
        if space == "wait":
            # Top tier is full but the cascade can still drain it. Pause rather
            # than abandon the run -- this is the backpressure that lets capture
            # outlast any single tier.
            self.storage.nudge_cascade()
            time.sleep(0.15)
            return
        if space == "full":
            self._emit("error", {"msg": "All storage tiers are full, stopping capture"})
            self._stop_activity()
            return

        t0 = mark = self._prof("has_space", gap_mark)
        try:
            request = self._cam.capture_request()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "capture_request: %s" % exc})
            time.sleep(0.2)
            return
        mark = self._prof("req", mark)

        handed_off = False
        try:
            meta = request.get_metadata()
            lores = request.make_array("lores")
            lw, lh = self._lores_size
            y = lores[:lh, :lw]
            actual_us = int(meta.get("ExposureTime", self._exposure_us))
            actual_gain = float(meta.get("AnalogueGain", self._gain))
            mark = self._prof("lores", mark)

            cfg_snapshot = self.cfg.as_dict()
            # Histogram-only metering (~4 ms vs ~16). Rapid mode is about rate;
            # auto-exposure is the one thing still worth spending time on.
            stats = meter_only(y, cfg_snapshot)
            decision = self._run_ae(stats, actual_us, actual_gain, meta.get("Lux"))
            mark = self._prof("meter_ae", mark)

            self._seq += 1
            if self._rapid_ram:
                self._ram_frames.append(
                    (request.make_array("main"), time.time(), self._seq,
                     actual_us, actual_gain)
                )
                mark = self._prof("main_copy", mark)
            else:
                self._submit_rapid(request, self._seq, actual_us, actual_gain, stats)
                handed_off = True
                mark = self._prof("submit", mark)
            self._taken += 1
            # Timed from the first frame, so camera reconfiguration and the
            # settle sleep do not get counted against the capture rate.
            if self._taken == 1:
                self._rapid_t_first = t0
            self._rapid_t_last = time.monotonic()
        finally:
            if not handed_off:
                request.release()
        mark = self._prof("release", mark)

        dt = time.monotonic() - t0
        self._fps_window.append(dt)
        if len(self._fps_window) > 30:
            self._fps_window.pop(0)

        # Preview at 2 Hz only -- rapid mode should not spend its budget on the GUI.
        if time.monotonic() - self._last_preview >= 0.5:
            self._publish_preview(y, lores, stats, decision, meta,
                                  actual_us, actual_gain)
        self._prof("preview", mark)
        self._maybe_roll_group()
        self._maybe_offload()
        self._last_tick_end = time.monotonic()

        if self._target_frames and self._taken >= self._target_frames:
            self._stop_activity()

    def _submit_rapid(self, request, seq, exposure_us, gain, stats) -> None:
        pool = self._pool()
        quality = int(self.cfg["jpeg_quality"])
        when = time.time()
        self._inflight.acquire()

        def _work():
            try:
                main = request.make_array("main")
                jpeg = simplejpeg.encode_jpeg(main, quality=quality, colorspace="BGR")
            except Exception as exc:  # noqa: BLE001
                self._emit("error", {"msg": "encode: %s" % exc})
                return
            finally:
                request.release()
                self._inflight.release()
            path = self.storage.write_rapid(jpeg, seq, when, stats, exposure_us, gain)
            if path:
                self._last_written = os.path.basename(path)
            self._emit("frame", {
                "path": path, "seq": seq, "stats": stats, "decision": None,
                "shutter_us": exposure_us, "gain": gain, "bytes": len(jpeg),
            })

        try:
            pool.submit(_work)
        except BaseException:
            request.release()
            self._inflight.release()
            raise

    def _drain_ram(self) -> None:
        """Encode and write a RAM burst after the fact."""
        total = len(self._ram_frames)
        if not total:
            return
        self._state = DRAIN
        self._emit("rapid", {"phase": "drain", "total": total, "done": 0})
        pool = self._pool()
        quality = int(self.cfg["jpeg_quality"])
        done = [0]
        lock = threading.Lock()

        def _work(item):
            arr, when, seq, eus, g = item
            try:
                jpeg = simplejpeg.encode_jpeg(arr, quality=quality, colorspace="BGR")
                path = self.storage.write_rapid(jpeg, seq, when, None, eus, g)
                nbytes = len(jpeg)
            except Exception as exc:  # noqa: BLE001
                self._emit("error", {"msg": "drain: %s" % exc})
                path, nbytes = None, 0
            with lock:
                done[0] += 1
                n = done[0]
            self._emit("rapid", {"phase": "drain", "total": total, "done": n,
                                 "path": path, "bytes": nbytes})

        futures = [pool.submit(_work, item) for item in self._ram_frames]
        for f in futures:
            try:
                f.result()
            except Exception:
                pass
        self._ram_frames = []
        self._emit("rapid", {"phase": "done", "total": total, "done": total})

    def _start_timelapse(self, kw: Dict[str, Any]) -> None:
        self._ensure_camera(video=False)
        self.storage.start_session("tlc")
        self._target_frames = int(kw.get("count", self.cfg["timelapse_count"]) or 0)
        self._taken = 0
        self._next_shot = time.monotonic()
        self.controller.reset()
        self._seed_exposure()
        self._state = TIMELAPSE

    def _seed_exposure(self) -> None:
        """Use the learned lux constant to start near the right exposure."""
        if not self.cfg["auto_exposure"] or self._cam is None:
            self._exposure_us = int(self.cfg["manual_shutter_us"])
            self._gain = float(self.cfg["manual_gain"])
            self._apply_exposure()
            return
        try:
            lux = self._cam.capture_metadata().get("Lux")
        except Exception:
            lux = None
        seeded = self.controller.seed(lux)
        if seeded:
            self._exposure_us, self._gain = seeded
            self._apply_exposure()

    def _drain_encoders(self) -> None:
        """Wait for every queued JPEG to finish writing.

        Must happen before the session closes: an encode still in flight would
        otherwise find no open session and start a brand new one, scattering the
        tail of a burst into a second directory.
        """
        if self._encoder_pool is not None:
            self._encoder_pool.shutdown(wait=True)
            self._encoder_pool = None

    def _stop_activity(self) -> None:
        if self._state == VIDEO:
            self._stop_video()
        if self._state == RAPID and self._rapid_ram:
            # Frames are still sitting in memory; get them onto disk before the
            # session closes or they are lost.
            self._drain_ram()
        if self._state in (BURST, TIMELAPSE, RAPID, DRAIN):
            self._drain_encoders()
            # Seal the partial group before closing, or the final frames would
            # sit in an unsealed directory and never migrate.
            if self.cfg["cascade_enabled"] and self.storage.session is not None:
                tail = self.storage.session.seal_current_group()
                if tail:
                    self.storage.notify_group_sealed(tail)
            summary = self.storage.close_session()
            self.controller.persist()
            self.cfg.set_state(frame_seq=self._seq,
                               last_shutter_us=self._exposure_us,
                               last_gain=self._gain)
            self.cfg.save()
            if summary:
                self._emit("session", summary)
        self._state = PREVIEW if self._cam is not None else IDLE

    # ==================================================================
    # capture tick
    # ==================================================================
    def _tick_capture(self) -> None:
        if not self._camera_ready():
            time.sleep(0.1)
            return

        if self._state == TIMELAPSE:
            now = time.monotonic()
            if now < self._next_shot:
                # Keep the preview and the AE loop alive between exposures.
                self._meter_only()
                time.sleep(min(0.1, self._next_shot - now))
                return
            self._next_shot = now + float(self.cfg["timelapse_interval_s"])

        saving = self._state in (BURST, TIMELAPSE)
        if saving:
            space = self.storage.space_state()
            if space == "wait":
                self.storage.nudge_cascade()
                time.sleep(0.15)
                return
            if space == "full":
                self._emit("error", {"msg": "All storage tiers are full, stopping capture"})
                self._stop_activity()
                return

        t0 = time.monotonic()
        try:
            request = self._cam.capture_request()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "capture_request: %s" % exc})
            time.sleep(0.2)
            return

        handed_off = False
        try:
            # Only the cheap work happens here. Copying the full-resolution
            # buffer costs ~226 ms on this CM4 and measuring focus on it another
            # ~140 ms; both are handed to the encoder pool below, leaving the
            # capture loop free to drive the sensor at its own rate.
            meta = request.get_metadata()
            lores = request.make_array("lores")

            lw, lh = self._lores_size
            y = lores[:lh, :lw]
            actual_us = int(meta.get("ExposureTime", self._exposure_us))
            actual_gain = float(meta.get("AnalogueGain", self._gain))
            lux = meta.get("Lux")

            cfg_snapshot = self.cfg.as_dict()
            # Skip the focus measures when the frame is being saved -- the
            # worker recomputes them at native resolution moments later, so
            # doing them here on the preview plane is pure waste.
            stats = analyse(y, cfg_snapshot, focus=not saving)
            decision = self._run_ae(stats, actual_us, actual_gain, lux)

            focus_view = None
            if saving:
                self._seq += 1
                self._submit_frame(request, stats, decision, self._seq,
                                   actual_us, actual_gain, cfg_snapshot)
                handed_off = True
                self._taken += 1
            elif self._focus_assist:
                # Not saving, so nothing else is competing for the pool -- take
                # the copy here to feed the focus monitor a 1:1 view.
                focus_view, crop = self._focus_patch(request)
                if crop is not None:
                    refine_with_hires(stats, crop, cfg_snapshot)
        finally:
            if not handed_off:
                request.release()

        self._publish_preview(y, lores, stats, decision, meta, actual_us, actual_gain,
                              focus_view=focus_view)
        self._maybe_roll_group()
        self._maybe_offload()

        dt = time.monotonic() - t0
        self._fps_window.append(dt)
        if len(self._fps_window) > 20:
            self._fps_window.pop(0)

        if self._target_frames and self._taken >= self._target_frames:
            self._stop_activity()

    def _focus_patch(self, request):
        """Native-resolution centre crop: (viewable RGB, green channel)."""
        main = request.make_array("main")
        if main.shape[0] <= FOCUS_CROP or main.shape[1] <= FOCUS_CROP:
            return None, None
        cy, cx = main.shape[0] // 2, main.shape[1] // 2
        h = FOCUS_CROP // 2
        patch = main[cy - h : cy + h, cx - h : cx + h]
        # main is BGR-ordered despite libcamera calling the format RGB888.
        return np.ascontiguousarray(patch[:, :, ::-1]), patch[:, :, 1]

    def _submit_frame(self, request, stats, decision, seq, exposure_us, gain, cfg_snapshot):
        """Hand a capture request to the encoder pool.

        The worker owns the request from here: it does the full-resolution copy,
        refines the focus measure, encodes, writes and only then releases the
        buffer back to libcamera. A semaphore bounds how many requests may be in
        flight so the camera is never starved of buffers.
        """
        pool = self._pool()
        quality = int(self.cfg["jpeg_quality"])
        self._inflight.acquire()

        def _work():
            try:
                main = request.make_array("main")
                if main.shape[0] > FOCUS_CROP and main.shape[1] > FOCUS_CROP:
                    cy, cx = main.shape[0] // 2, main.shape[1] // 2
                    h = FOCUS_CROP // 2
                    refine_with_hires(stats, main[cy - h:cy + h, cx - h:cx + h, 1],
                                      cfg_snapshot)
                jpeg = simplejpeg.encode_jpeg(main, quality=quality, colorspace="BGR")
            except Exception as exc:  # noqa: BLE001
                self._emit("error", {"msg": "encode: %s" % exc})
                return
            finally:
                request.release()
                self._inflight.release()
            path = self.storage.write_frame(jpeg, exposure_us, gain, seq, stats, decision)
            if path:
                self._last_written = os.path.basename(path)
            self._emit("frame", {
                "path": path, "seq": seq, "stats": stats, "decision": decision,
                "shutter_us": exposure_us, "gain": gain, "bytes": len(jpeg),
            })

        try:
            pool.submit(_work)
        except BaseException:
            request.release()
            self._inflight.release()
            raise

    def _meter_only(self, light: bool = False) -> None:
        """Keep the preview and the AE loop alive without saving anything.

        Used between timelapse exposures and throughout video recording. With
        ``light`` the histogram-only metering path is used, which matters while
        recording: the full analysis costs ~76 ms a frame and would compete with
        the H.264 encoder for the same cores.
        """
        if not self._camera_ready():
            time.sleep(0.1)
            return
        try:
            request = self._cam.capture_request()
        except Exception:
            return
        try:
            meta = request.get_metadata()
            lores = request.make_array("lores")
        except Exception:
            return
        finally:
            request.release()

        lw, lh = self._lores_size
        y = lores[:lh, :lw]
        cfg_snapshot = self.cfg.as_dict()
        stats = meter_only(y, cfg_snapshot) if light else analyse(y, cfg_snapshot)
        actual_us = int(meta.get("ExposureTime", self._exposure_us))
        actual_gain = float(meta.get("AnalogueGain", self._gain))
        decision = self._run_ae(stats, actual_us, actual_gain, meta.get("Lux"))
        self._publish_preview(y, lores, stats, decision, meta, actual_us, actual_gain)

    # ------------------------------------------------------------------
    def _run_ae(self, stats: FrameStats, actual_us: int, actual_gain: float, lux):
        """Advance the exposure controller, respecting control latency."""
        if not self.cfg["auto_exposure"]:
            want_us = int(self.cfg["manual_shutter_us"])
            want_gain = float(self.cfg["manual_gain"])
            if (want_us, want_gain) != (self._exposure_us, self._gain):
                self._exposure_us, self._gain = want_us, want_gain
                self._apply_exposure()
            return None

        # A control set takes ~2 frames to land. Until the sensor reports back
        # the values we asked for, correcting again would double-count the error
        # and set the loop oscillating.
        #
        # But the sensor does not always give back what it was asked: exposure
        # is capped by the frame duration, gain quantises, and both clamp at the
        # mode's limits. Waiting indefinitely for an exact match is what made
        # auto-exposure freeze -- it would sit there dark forever, because the
        # one thing that could have corrected it was the loop that had stopped.
        # So the wait is bounded, and after that we accept reality and carry on
        # from whatever the sensor actually did.
        if self._requested is not None:
            req_us, req_gain = self._requested
            # Tolerances must allow for quantisation, not just latency. The
            # IMX477 snaps analogue gain to its own steps -- asking for 2.10
            # yields 2.00 -- and a flat 0.05 window never matched, so the loop
            # stalled during perfectly ordinary operation. Relative windows.
            landed = (abs(actual_us - req_us) <= max(2.0, req_us * 0.02)
                      and abs(actual_gain - req_gain) <= max(0.08, req_gain * 0.08))
            if landed:
                self._ae_waiting = 0
                self._requested = None
            else:
                self._ae_waiting += 1
                if self._ae_waiting <= AE_LAND_FRAMES:
                    return None
                self._emit("ae", {
                    "event": "recovered",
                    "asked_us": req_us, "asked_gain": round(req_gain, 3),
                    "got_us": actual_us, "got_gain": round(actual_gain, 3),
                    "msg": "sensor clamped the request; resuming from actual",
                })
                self._requested = None
                self._ae_waiting = 0
                # Re-seed the controller from reality so the integral term is
                # not still winding against a value that was never reachable.
                self.controller.resync(actual_us, actual_gain)

        decision = self.controller.update(stats, actual_us, actual_gain, lux=lux)
        if decision.exposure_us != self._exposure_us or abs(decision.gain - self._gain) > 1e-3:
            self._exposure_us = decision.exposure_us
            self._gain = decision.gain
            self._apply_exposure()
        return decision

    # ------------------------------------------------------------------
    def _publish_preview(self, y, lores, stats, decision, meta, actual_us, actual_gain,
                         focus_view=None) -> None:
        now = time.monotonic()
        if now - self._last_preview < 0.1:  # cap the GUI at ~10 fps
            return
        self._last_preview = now
        lw, lh = self._lores_size
        rgb = yuv420_to_rgb(lores, lw, lh, half=True)

        # A small always-current JPEG at a fixed path. Cheap enough to keep
        # fresh at 1 Hz, and it is what the desktop wallpaper monitor and any
        # machine on the network poll instead of hunting for the newest file.
        if now - self._last_latest >= 1.0:
            self._last_latest = now
            try:
                self.storage.write_latest(
                    simplejpeg.encode_jpeg(np.ascontiguousarray(rgb[:, :, ::-1]),
                                           quality=80, colorspace="BGR")
                )
            except Exception:
                pass

        fps = 0.0
        if self._fps_window:
            avg = sum(self._fps_window) / len(self._fps_window)
            fps = 1.0 / avg if avg > 0 else 0.0
        fmap = fbest = None
        fpeak = 0.0
        if self._focus_map_on:
            fmap, fbest, fpeak = focus_map(y)

        self._emit("preview", {
            "rgb": rgb,
            "y": y,
            "focus_view": focus_view,
            "focus_map": fmap,
            "focus_best": fbest,
            "focus_peak": fpeak,
            "stats": stats,
            "decision": decision,
            "shutter_us": actual_us,
            "gain": actual_gain,
            "lux": meta.get("Lux"),
            "state": self._state,
            "taken": self._taken,
            "target": self._target_frames,
            "fps": fps,
            "free_mb": self.storage.free_mb(),
            # Seconds until the next timelapse exposure. Costs one subtraction;
            # everything else here is already being computed.
            "next_in": (max(0.0, self._next_shot - time.monotonic())
                        if self._state == TIMELAPSE else None),
            "interval": (float(self.cfg["timelapse_interval_s"])
                         if self._state == TIMELAPSE else None),
            # Basename only. The full path is transient -- with the cascade on,
            # a frame starts on tmpfs and ends up on the USB stick, so the
            # directory it is in right now is not where it will live.
            "last_file": self._last_written,
            "destination": self._destination_label(),
        })

    # ==================================================================
    # single shot (used by the calibration wizard)
    # ==================================================================
    def _capture_single(self, kw: Dict[str, Any]) -> None:
        self._ensure_camera(video=False)
        if kw.get("exposure_us"):
            self._exposure_us = int(kw["exposure_us"])
            self._gain = float(kw.get("gain", self._gain))
            self._apply_exposure()
            time.sleep(0.4)
        try:
            request = self._cam.capture_request()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "single: %s" % exc})
            return
        try:
            meta = request.get_metadata()
            lores = request.make_array("lores")
            main = request.make_array("main")
        finally:
            request.release()
        lw, lh = self._lores_size
        stats = analyse(lores[:lh, :lw], self.cfg.as_dict())
        payload = {
            "stats": stats,
            "meta": meta,
            "shutter_us": int(meta.get("ExposureTime", self._exposure_us)),
            "gain": float(meta.get("AnalogueGain", self._gain)),
            "lux": meta.get("Lux"),
            "tag": kw.get("tag"),
        }
        if kw.get("save"):
            try:
                jpeg = simplejpeg.encode_jpeg(main, quality=int(self.cfg["jpeg_quality"]),
                                              colorspace="BGR")
                self._seq += 1
                payload["path"] = self.storage.write_frame(
                    jpeg, payload["shutter_us"], payload["gain"], self._seq, stats
                )
            except Exception as exc:  # noqa: BLE001
                self._emit("error", {"msg": "single save: %s" % exc})
        self._emit("single", payload)

    # ==================================================================
    # video
    # ==================================================================
    def _start_video(self, kw: Dict[str, Any]) -> None:
        # Starting a recording while stills are running would strand the open
        # session (and, in RAM mode, everything still buffered in memory).
        if self._state in (BURST, TIMELAPSE, RAPID):
            self._stop_activity()
        self._ensure_camera(video=True)
        root = self.storage.media_root("video")
        name = kw.get("name") or timestamp_name()
        self._video_out = os.path.join(root, "%s.mp4" % name)
        bitrate = int(kw.get("bitrate", 12_000_000))
        try:
            self._encoder = H264Encoder(bitrate=bitrate)
            self._cam.start_recording(self._encoder, FfmpegOutput(self._video_out))
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "start_recording: %s\n%s"
                                        % (exc, traceback.format_exc())})
            self._encoder = None
            self._video_out = None
            self._state = PREVIEW
            return
        self._video_started = time.monotonic()
        self._last_video_stat = 0.0
        self._state = VIDEO
        self._emit("video", {"recording": True, "path": self._video_out,
                             "elapsed": 0.0, "bytes": 0})

    def _tick_video(self) -> None:
        # Light metering only -- the hardware encoder is busy and the full
        # analysis would take cores away from it.
        self._meter_only(light=True)
        now = time.monotonic()
        if now - self._last_video_stat >= 1.0:
            self._last_video_stat = now
            size = 0
            try:
                size = os.path.getsize(self._video_out) if self._video_out else 0
            except OSError:
                pass
            elapsed = now - self._video_started
            self._emit("video", {
                "recording": True, "path": self._video_out,
                "elapsed": elapsed, "bytes": size,
                "mbps": (size * 8 / 1e6 / elapsed) if elapsed > 0 else 0.0,
                "free_mb": self.storage.free_mb(),
            })
        time.sleep(0.05)

    def _stop_video(self) -> None:
        try:
            if self._encoder is not None:
                self._cam.stop_recording()
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "stop_recording: %s" % exc})
        path = self._video_out
        self._encoder = None
        self._video_out = None
        self._configured = None
        self._emit("video", {"recording": False, "path": path})
        # Finished recordings cascade like everything else.
        if path and self.storage.cascade is not None:
            self.storage.cascade_media(path, "video")

        # stop_recording() also stops the camera. Restarting it here is not
        # optional: the state machine returns to PREVIEW, and capture_request()
        # on a stopped camera blocks forever rather than raising -- which hung
        # the engine thread, froze the preview and left every later button press
        # sitting unprocessed in the queue.
        try:
            self._ensure_camera(video=False)
        except CameraConfigError:
            self._state = IDLE
            return
        except Exception as exc:  # noqa: BLE001
            self._emit("error", {"msg": "could not return to stills: %s" % exc})
            self._state = IDLE
            return

        if path and self.storage.cascade is None:
            self.storage.offload_now(path)

    # ==================================================================
    def _teardown(self) -> None:
        try:
            if self._state == VIDEO:
                self._stop_video()
        except Exception:
            pass
        self._drain_encoders()
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass
            self._cam = None
        self.controller.persist()
        self.cfg.set_state(last_shutter_us=self._exposure_us, last_gain=self._gain,
                           frame_seq=self._seq)
        try:
            self.cfg.save()
        except Exception:
            pass
