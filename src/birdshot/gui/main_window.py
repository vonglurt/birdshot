# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Main GUI window.

Runs on the Pi's own display. The camera engine lives on a worker thread and
pushes events across a Qt signal, which is what makes cross-thread delivery
safe -- nothing here touches the camera directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAction, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox,
    QSplitter, QStackedWidget, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from .. import timelapse as tl
from ..camera import available_ram_mb
from ..config import CAPTURE_MODES, VIDEO_MODES
from ..naming import PRESET_SHUTTERS_US, describe_shutter, shutter_dir
from . import faces
from .calibrate import CalibrationDialog
from .focus_monitor import FocusMonitor
from .preview import HistogramWidget, PreviewWidget, ToneCurveWidget
from .widgets import Accordion, BlockingOverlay, FullscreenPreview, ModeTuner


# Measured RAM-burst rates, keyed by capture size. These are what the sensor
# plus the buffer copy deliver with the encoder taken off the critical path.
RAPID_RAM_FPS = {
    (4056, 3040): 4.17,
    (2028, 1520): 16.34,
    (1332, 990): 36.75,
}
RAPID_CONT_FPS = {
    (4056, 3040): 4.53,
    (2028, 1520): 21.02,
    (1332, 990): 34.81,
}


class EngineBridge(QObject):
    """Marshals engine events from the camera thread onto the GUI thread."""

    event = pyqtSignal(str, object)


class MainWindow(QMainWindow):
    def __init__(self, cfg, storage, engine_factory, auto=None, face=None):
        super().__init__()
        self.cfg = cfg
        self.storage = storage
        self.auto = auto
        self.setWindowTitle("birdshot")
        self.resize(1400, 860)
        self._camera_combos = []

        self.bridge = EngineBridge()
        self.bridge.event.connect(self._on_event)
        # Kept so the camera selector can rebuild the engine on a new device.
        self._engine_factory = engine_factory
        self.engine = engine_factory(self._emit_event)

        self._counts = {"ok": 0, "dark": 0, "blown": 0, "empty": 0}
        self._session_frames = 0
        self._session_bytes = 0
        self._calib: Optional[CalibrationDialog] = None
        self._focus: Optional[FocusMonitor] = None
        self._fullscreen: Optional[FullscreenPreview] = None
        self._space_blocked = False
        self._assemble_job: Optional[tl.AssembleJob] = None
        self._binding = False
        self._last_sharpness = 0.0
        self._rapid_t0 = time.time()

        self._build_ui()
        self.set_face(face if face in faces.FACES else faces.resolve_face(cfg))
        self.engine.start()
        self.engine.send("preview")

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_status)
        self._tick.start(1000)
        # First health check once startup has settled; the chip in the status
        # bar carries the answer and clicking it opens the full checklist.
        QTimer.singleShot(3000, self._run_doctor)

        if self.cfg.get("outdoor_mode"):
            self.chk_outdoor.setChecked(True)

        if self.auto:
            # Unattended: skip the calibration prompt (nothing would answer it)
            # and start shooting.
            QTimer.singleShot(2500, self._begin_auto)
        elif not (self.cfg["calibration"] or {}).get("done"):
            QTimer.singleShot(1500, self._offer_calibration)

    # Old tab names people (and desktop entries) may still ask for, mapped to
    # where that content lives now.
    TAB_ALIASES = {"image": "scene", "storage": "machine", "cascade": "machine",
                   "focus": "scene", "exposure": "scene", "quality": "scene",
                   "rapid": "shoot", "capture": "shoot"}

    def select_tab(self, name: str) -> bool:
        """Switch to a tab (or face) by name, honouring pre-face aliases."""
        n = name.strip().lower()
        if n in ("process", "encode"):
            self.set_face("library")
            return True
        if n in faces.FACES:
            self.set_face(n)
            return True
        n = self.TAB_ALIASES.get(n, n)
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i).lower() == n:
                self.set_face("bench")
                self._tabs.setCurrentIndex(i)
                return True
        return False

    # ------------------------------------------------------------------
    # faces (see gui/faces.py): one window, four faces over one engine
    # ------------------------------------------------------------------
    def set_face(self, name: str) -> None:
        if name not in faces.FACES:
            name = "bench"
        leaving = getattr(self, "_face", None)
        self._face = name
        self._stack.setCurrentIndex(faces.FACES.index(name))
        self.facebar.set_active(name)
        # The Camera face is a plain camera app: no HUD, no metering zones,
        # no zebras. Stash the operator's overlay state and hand it back.
        overlay_keys = ("show_hud", "show_zones", "show_zebra", "show_peaking",
                        "show_focus_map", "show_sharpness")
        if name == "camera" and leaving != "camera":
            self._overlay_stash = {k: getattr(self.preview, k)
                                   for k in overlay_keys}
            for k in overlay_keys:
                setattr(self.preview, k, False)
        elif leaving == "camera" and name != "camera" \
                and getattr(self, "_overlay_stash", None):
            for k, v in self._overlay_stash.items():
                setattr(self.preview, k, v)
            self._overlay_stash = None
        # The one live preview moves into whichever face is showing; Library
        # has no preview, so the widget just stays parked on the hidden page.
        if name == "bench":
            self.preview.setParent(None)
            self._bench_preview_layout.insertWidget(1, self.preview, 1)
            self.preview.setVisible(True)
        elif name == "camera":
            self.preview.setParent(None)
            self.face_camera.preview_slot.insertWidget(0, self.preview, 1)
            self.preview.setVisible(True)
        elif name == "field":
            self.preview.setParent(None)
            self.face_field.preview_slot.insertWidget(0, self.preview, 1)
            self.preview.setVisible(True)
        elif name == "library":
            self.face_library.refresh()
            self._refresh_sources()
        self._refresh_go_button()

    def caps(self) -> frozenset:
        """What the current engine can actually do (backends docstring)."""
        from birdshot import backends
        return backends.engine_capabilities(self.engine)

    def go_clicked(self) -> None:
        self._go_clicked()

    def log(self, msg: str) -> None:
        self._log(msg)

    def open_path(self, path: str) -> None:
        """Open a file or folder in the desktop's own viewer."""
        if not path:
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, path])
        except OSError as exc:
            self._log("open failed: %s" % exc)

    # ------------------------------------------------------------------
    def _begin_auto(self) -> None:
        """Start an unattended run configured by an autowrite.yes stick."""
        from .. import autostart

        self.banner.setVisible(True)
        self.banner.setText(
            "AUTOWRITE  -  %s   ->   %s   |   copying every %ds%s"
            % (self.auto["mount"], self.auto["dest"], self.auto["interval"],
               "   |   eMMC freed after copy" if self.auto["delete_after_copy"] else "")
        )
        for w in self.auto.get("warnings", []):
            self._log("autowrite.yes: %s" % w)
        self._log(autostart.describe(self.auto).replace("\n", " | "))

        if self.auto.get("start"):
            self.tuner.set_index(1)   # Rapid: opens its section, labels STOP
            self._toggle_rapid()
            self._log("unattended capture started")

    # ==================================================================
    # engine plumbing
    # ==================================================================
    def _emit_event(self, name: str, payload: Dict[str, Any]) -> None:
        self.bridge.event.emit(name, payload)

    def _on_event(self, name: str, payload: Dict[str, Any]) -> None:
        if name == "preview":
            self._on_preview(payload)
        elif name == "frame":
            self._on_frame(payload)
        elif name == "state":
            self._on_state(payload)
        elif name == "session":
            self._on_session(payload)
        elif name == "video":
            self._on_video(payload)
        elif name == "single":
            self._log("single shot: %s" % payload.get("path", "(not saved)"))
        elif name == "assembled":
            self._on_assembled(payload)
        elif name == "encode_progress":
            self._on_encode_progress(payload)
        elif name == "encode_stage":
            self.lbl_encode_status.setText("%s..." % payload.get("stage"))
            self._log("encode: %s" % payload.get("stage"))
        elif name == "bird":
            self._on_bird(payload)
        elif name == "doctor":
            self._on_doctor(payload)
        elif name == "rapid":
            self._on_rapid(payload)
        elif name == "cascade":
            self._on_cascade(payload)
        elif name == "group":
            self._log("group sealed: %s" % os.path.basename(payload.get("sealed") or ""))
        elif name == "error":
            self._log("ERROR: %s" % payload.get("msg"))
            if payload.get("fatal"):
                QMessageBox.critical(self, "Camera error", str(payload.get("msg")))

    def _on_preview(self, payload: Dict[str, Any]) -> None:
        stats = payload.get("stats")
        self.preview.sky_zone_frac = float(self.cfg["sky_zone_frac"])
        self.preview.set_frame(payload.get("rgb"), payload.get("y"), stats)
        self.histogram.target = float(self.cfg["target_luma"])
        self.histogram.set_frame(payload.get("y"), stats)

        shutter = payload.get("shutter_us") or 0
        gain = payload.get("gain") or 0.0
        lux = payload.get("lux")
        decision = payload.get("decision")

        self.lbl_shutter.setText(describe_shutter(shutter))
        self.lbl_folder.setText(shutter_dir(shutter))
        self.lbl_gain.setText("%.2fx" % gain)
        self.lbl_lux.setText("%.0f" % lux if lux else "-")
        self.lbl_fps.setText("%.1f" % (payload.get("fps") or 0.0))

        clip_txt = "clip %.2f%%" % (stats.clip_hi * 100.0) if stats else "clip -"
        ae_txt = ("%s err %+.2f out %+.2f EV"
                  % (decision.mode, decision.ev_error, decision.ev_output)
                  if decision is not None else "")
        self.lbl_line.setText(
            "%s   g%.2f   %s   %s lux   %.1f fps   %s   sharp %s   tiles %s"
            % (describe_shutter(shutter), gain, shutter_dir(shutter),
               ("%.0f" % lux) if lux else "-", payload.get("fps") or 0.0,
               clip_txt,
               ("%.1f" % stats.sharpness_norm) if stats and stats.focus_measured
               else "-",
               stats.contrast_tiles if stats else "-"))
        state = payload.get("state", "")
        last = payload.get("last_file")
        dest = payload.get("destination") or ""
        # Basename plus the tier it will end up on -- never the current path.
        # With the cascade running a frame starts on tmpfs and finishes on the
        # stick, so the directory it happens to be in right now is not an
        # answer to "where did that go".
        if last:
            self.lbl_session.setText("%s  ->  %s" % (last, dest))
        self.preview.set_hud({
            "countdown": payload.get("next_in"),
            "interval": payload.get("interval"),
            "last_file": last,
            "shutter": describe_shutter(shutter),
            "gain": "g%.2f" % gain,
            "folder": shutter_dir(shutter),
            "lux": ("%s lux" % int(lux)) if lux else "",
            "fps": "%.1f fps" % (payload.get("fps") or 0.0),
            "clip": clip_txt,
            "verdict": stats.verdict if stats else "",
            "ae": ae_txt,
        })
        if payload.get("next_in") is not None:
            self.status.showMessage(
                "timelapse: next frame in %.1f s   |   last %s -> %s"
                % (payload["next_in"], last or "-", dest), 1500)

        if stats is not None:
            self.lbl_verdict.setText(stats.verdict.upper())
            self.lbl_verdict.setStyleSheet(
                "font-weight:600; color:%s;" % {
                    "ok": "#5fd07a", "dark": "#5aa0ff",
                    "blown": "#ff6a44", "empty": "#e0a828",
                }.get(stats.verdict, "#ccc")
            )
            # Rapid mode skips the focus and content passes, so show a dash
            # rather than a zero that reads as "completely out of focus".
            self.lbl_sharp.setText("%.1f" % stats.sharpness_norm
                                   if stats.focus_measured else "- (not measured)")
            self.lbl_tiles.setText("%d" % stats.contrast_tiles
                                   if self.engine.state not in ("rapid", "drain") else "-")
            self.lbl_clip.setText("%.2f%%" % (stats.clip_hi * 100.0))


        if self._fullscreen is not None:
            self._fullscreen.view.set_frame(payload.get("rgb"), payload.get("y"), stats)
        if payload.get("focus_map") is not None:
            self.preview.set_focus_map(payload["focus_map"], payload.get("focus_best"),
                                       payload.get("focus_peak") or 0.0)
        if stats is not None and stats.focus_measured:
            self._last_sharpness = stats.sharpness_norm
        if stats is not None and hasattr(self, "lbl_focus_live"):
            best = payload.get("focus_best")
            self.lbl_focus_live.setText(
                "sharpness %6.1f   peak %8.0f   sharpest tile %s\n"
                "shutter %-12s gain %4.2f   contrast tiles %d"
                % (stats.sharpness_norm, payload.get("focus_peak") or 0.0,
                   ("row %d col %d" % best) if best else "-",
                   describe_shutter(shutter), gain, stats.contrast_tiles)
            )

        if self.engine.state in ("rapid", "drain") and hasattr(self, "lbl_rapid_live"):
            elapsed = max(1e-3, time.time() - getattr(self, "_rapid_t0", time.time()))
            taken = payload.get("taken") or 0
            self.lbl_rapid_live.setText(
                "%d frames in %.1f s = %.2f fps   |   %s"
                % (taken, elapsed, taken / elapsed, self.engine.state)
            )
            if self.cfg["rapid_mode"] == "ram" and self.prog_rapid.isVisible() \
                    and self.engine.state == "rapid":
                self.prog_rapid.setValue(taken)

        if self._calib is not None and self._calib.isVisible():
            self._calib.handle_preview(payload)
        if self._focus is not None and self._focus.isVisible():
            self._focus.handle_preview(payload)

    def _on_frame(self, payload: Dict[str, Any]) -> None:
        if hasattr(self, "face_camera"):
            self.face_camera.on_frame(payload)
        stats = payload.get("stats")
        if stats is not None:
            self._counts[stats.verdict] = self._counts.get(stats.verdict, 0) + 1
        self._session_frames += 1
        self._session_bytes += int(payload.get("bytes") or 0)
        self.lbl_counts.setText(
            "ok %d | dark %d | blown %d | empty %d"
            % (self._counts.get("ok", 0), self._counts.get("dark", 0),
               self._counts.get("blown", 0), self._counts.get("empty", 0))
        )
        self.lbl_session.setText("%d frames, %.0f MB"
                                 % (self._session_frames, self._session_bytes / 1e6))

    def _on_state(self, payload: Dict[str, Any]) -> None:
        state = payload.get("state", "?")
        self.lbl_state.setText(state.upper())
        self.btn_rapid.setChecked(state in ("rapid", "drain"))
        if hasattr(self, "btn_cascade_go"):
            self.btn_cascade_go.setChecked(state in ("rapid", "drain"))
            self.btn_cascade_go.setText(
                "STOP  -  finish and flush down" if state in ("rapid", "drain")
                else "CAPTURE IN GROUPS  -  fastest write, self-clearing")
        self.btn_rapid.setText({
            "rapid": "STOP  -  finish and write out",
            "drain": "draining to disk...",
        }.get(state, "RAPID  -  fastest single photos"))
        self.btn_collect.setChecked(state == "burst")
        self.btn_collect.setText("STOP COLLECTING" if state == "burst"
                                 else "COLLECT  -  full res, fastest rate")
        self.btn_timelapse.setChecked(state == "timelapse")
        if hasattr(self, "btn_bird"):
            self.btn_bird.setChecked(state == "birdflight")
            if state != "birdflight" and hasattr(self, "lbl_bird"):
                self.lbl_bird.setText("idle")
        self.btn_timelapse.setText("Stop timelapse" if state == "timelapse"
                                   else "Start timelapse")
        self.btn_record.setChecked(state == "video")
        self.btn_record.setText("Stop recording" if state == "video" else "Record video")
        self._refresh_go_button()

    def _on_session(self, payload: Dict[str, Any]) -> None:
        self._log("session %s closed: %d frames, %.0f MB, %s"
                  % (payload.get("id"), payload.get("frames", 0),
                     payload.get("bytes", 0) / 1e6, payload.get("counts")))
        self._refresh_sources()

    def _on_video(self, payload: Dict[str, Any]) -> None:
        if not payload.get("recording"):
            self.lbl_video_stat.setText("stopped: %s" % (payload.get("path") or "-"))
            self._log("recording stopped: %s" % payload.get("path"))
            return
        elapsed = payload.get("elapsed")
        if elapsed is None:
            self._log("recording -> %s" % payload.get("path"))
            return
        mb = (payload.get("bytes") or 0) / 1e6
        self.lbl_video_stat.setText(
            "REC  %02d:%02d   %.0f MB   %.1f Mbps   %.1f GB free\n%s"
            % (int(elapsed) // 60, int(elapsed) % 60, mb,
               payload.get("mbps") or 0.0, (payload.get("free_mb") or 0) / 1024.0,
               os.path.basename(payload.get("path") or ""))
        )

    # ==================================================================
    # UI construction
    # ==================================================================
    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Horizontal)

        # ---- left: preview -------------------------------------------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(6, 6, 6, 6)
        self.banner = QLabel()
        self.banner.setVisible(False)
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            "background:#7a4a10;color:#ffe8c0;padding:7px;border-radius:4px;"
            "font-weight:600;font-family:monospace;"
        )
        lv.addWidget(self.banner)
        self.preview = PreviewWidget()
        self.preview.double_clicked.connect(self._toggle_fullscreen)
        self.preview.overlays_toggled.connect(self._set_all_overlays)
        self.histogram = HistogramWidget()
        self.histogram.set_levels(self.cfg.get("tone_black", 0.0),
                                  self.cfg.get("tone_white", 1.0))
        self.histogram.levels_changed.connect(self._levels_changed)
        # Applying reopens the camera, so a drag must not do it per pixel.
        self._levels_timer = QTimer(self)
        self._levels_timer.setSingleShot(True)
        self._levels_timer.setInterval(900)
        self._levels_timer.timeout.connect(self._apply_levels)
        lv.setSpacing(3)
        lv.addWidget(self.preview, 1)
        lv.addWidget(self.histogram)
        # The preview is a single widget shared by every face; this layout is
        # where it comes home to when Bench is showing (see set_face).
        self._bench_preview_layout = lv

        lrow = QHBoxLayout()
        lrow.setContentsMargins(6, 0, 6, 0)
        hint = QLabel("levels: click left = black, right = white, arrows nudge, "
                      "double-click resets")
        hint.setStyleSheet("color:#7a7a84;font-size:11px;")
        lrow.addWidget(hint, 1)
        chk_live = QCheckBox("apply to capture")
        chk_live.setChecked(bool(self.cfg.get("levels_live", True)))
        chk_live.setToolTip("Writes the levels into the ISP curve. Reopens the\n"
                            "camera, so it waits until you stop adjusting.")
        chk_live.toggled.connect(
            lambda v: (self.cfg.__setitem__("levels_live", v), self._save()))
        lrow.addWidget(chk_live)
        lrow.addWidget(QLabel("knee"))
        kn = QDoubleSpinBox()
        kn.setRange(0.0, 0.45)
        kn.setSingleStep(0.02)
        kn.setDecimals(2)
        kn.setValue(float(self.cfg.get("tone_knee_soft", 0.12)))
        kn.setToolTip("How gently tones round off outside the points.\n"
                      "0 clips hard; higher compresses more smoothly.")
        kn.valueChanged.connect(
            lambda v: (self.cfg.__setitem__("tone_knee_soft", v), self._save(),
                       self._levels_timer.start()))
        lrow.addWidget(kn)
        lv.addLayout(lrow)
        lv.addWidget(self._build_readout())
        lv.addLayout(self._build_view_row())
        splitter.addWidget(left)

        # ---- right: controls -----------------------------------------
        # Three tabs, scoped by what a setting tunes: Shoot (each mode's own
        # keys), Scene (the image science), Machine (this install). Each is a
        # stack of collapsible sections; the mode header and START stay above
        # the tabs, always visible. What was the Process tab lives with the
        # Library face now.
        self._acc = {}
        self._sections = {}

        def section(title, widget, expanded=False, tab=0):
            a = Accordion(title, expanded)
            a.addWidget(widget)
            self._acc[title] = a
            self._sections[title] = (tab, a)
            return a

        self._rapid_page = self._tab_rapid()

        shoot = self._stack([
            section("Stills - full pipeline, quality gates", self._tab_capture()),
            section("Rapid - fastest, flat filenames", self._rapid_page),
            section("Timelapse", self._tab_timelapse()),
            section("Video", self._tab_video()),
            section("Bird Flight - auto-take, sharp against sky",
                    self._tab_birdflight()),
        ])
        scene = self._stack([
            section("Exposure and tone", self._tab_exposure(), expanded=True,
                    tab=1),
            section("Focus aids", self._tab_focus(), tab=1),
            section("Quality gates", self._tab_quality(), tab=1),
        ])
        machine = self._stack([
            section("Cascade - tiers, RAM buffer, flush", self._tab_cascade(),
                    tab=2),
            section("Paths, offload and unattended start", self._tab_storage(),
                    tab=2),
            section("Install health - doctor", self._tab_health(), tab=2),
            section("Identity - EXIF", self._tab_identity(), tab=2),
        ])

        tabs = QTabWidget()
        tabs.addTab(shoot, "Shoot")
        tabs.addTab(scene, "Scene")
        tabs.addTab(machine, "Machine")
        self._hide_redundant_buttons()
        tabs.currentChanged.connect(self._tab_changed)
        self._tabs = tabs
        # The Focus aids drive a per-frame Laplacian pass, so the focus map
        # follows the section the way it used to follow the old Focus tab.
        self._acc["Focus aids"].toggled_open.connect(self._focus_section_toggled)

        rail = QWidget()
        rail_v = QVBoxLayout(rail)
        rail_v.setContentsMargins(0, 0, 0, 0)
        rail_v.setSpacing(4)
        rail_v.addWidget(self._mode_header())
        rail_v.addWidget(tabs, 1)
        # The header's first _mode_changed ran before the sections existed and
        # had nothing to expand; re-run it now that they do.
        self._mode_changed(self.tuner.index())

        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setWidget(rail)
        # Cap the sidebar rather than letting it take a share of the width. The
        # canvas is 4:3 (the sensor is 4056x3040), so once it is height-limited
        # every extra pixel of width is wasted -- but until then width is what
        # makes the image bigger. Fixing the panel gives the canvas the rest.
        right.setMinimumWidth(400)
        right.setMaximumWidth(480)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(1, True)

        # ---- the face shell ------------------------------------------
        # Bench is this splitter; the other three faces are built in
        # faces.py over the same engine, preview and config.
        self.face_camera = faces.CameraFace(self)
        self.face_field = faces.FieldFace(self)
        self.face_library = faces.LibraryFace(self)
        self._camera_combos.append(self.face_camera.cmb_camera)
        self.face_camera.cmb_camera.activated.connect(self._switch_camera)
        self._populate_cameras()
        # The encode UI (the old Process tab) lives with the Library face,
        # next to the sessions it consumes. The window still owns the job.
        self.face_library.adopt_encode_page(self._tab_encode())
        self._acc["Encode photos into a movie"] = self.face_library.enc_acc

        from birdshot import __version__
        self.facebar = faces.FaceBar("v%s" % __version__)
        self.facebar.face_picked.connect(self.set_face)

        self._stack = QStackedWidget()
        for page in (self.face_camera, self.face_field, splitter,
                     self.face_library):
            self._stack.addWidget(page)

        central = QWidget()
        shell = QVBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self.facebar)
        shell.addWidget(self._stack, 1)
        self.setCentralWidget(central)
        self._face = "bench"

        self.overlay = BlockingOverlay(self)
        self.overlay.setGeometry(self.rect())

        act_full = QAction(self)
        act_full.setShortcut("F11")
        act_full.triggered.connect(self._toggle_fullscreen)
        self.addAction(act_full)
        for key, delta in (("[", -1), ("]", 1)):
            a = QAction(self)
            a.setShortcut(key)
            a.triggered.connect(lambda _c=False, d=delta: self._step_mode(d))
            self.addAction(a)
        for i, name in enumerate(faces.FACES):
            a = QAction(self)
            a.setShortcut("Ctrl+%d" % (i + 1))
            a.triggered.connect(lambda _c=False, n=name: self.set_face(n))
            self.addAction(a)

        act_esc = QAction(self)
        act_esc.setShortcut("Esc")
        act_esc.triggered.connect(self._dismiss_overlay)
        self.addAction(act_esc)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.btn_doctor = QPushButton("doctor: ...")
        self.btn_doctor.setFlat(True)
        self.btn_doctor.setCursor(Qt.PointingHandCursor)
        self.btn_doctor.setStyleSheet(
            "QPushButton{border:none;background:transparent;color:#888;"
            "font-family:monospace;font-size:11px;}")
        self.btn_doctor.clicked.connect(self._open_health)
        self.lbl_state = QLabel("IDLE")
        self.lbl_free = QLabel("-")
        self.lbl_offload = QLabel("-")
        for w in (self.btn_doctor, self.lbl_state, self.lbl_free,
                  self.lbl_offload):
            self.status.addPermanentWidget(w)

    def _stack(self, widgets) -> QWidget:
        """Vertical stack of sections in a scroll area."""
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)
        for w in widgets:
            lay.addWidget(w)
        lay.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        area.setFrameShape(QScrollArea.NoFrame)
        return area

    # Capture modes offered by the single START button.
    MODES = [
        ("Stills", "burst", "full pipeline: quality gates, s<N> folders"),
        ("Rapid", "rapid", "fastest: flat YYYYMMDDHHMMSScc names, no gates"),
        ("Timelapse", "timelapse", "one frame every N seconds"),
        ("Video", "video", "H.264 to MP4, hardware encoder"),
        ("Bird Flight", "birdflight",
         "watch the sky; burst when a bird is sharp against it"),
    ]

    def _mode_header(self) -> QWidget:
        """Mode dropdown plus the one button that starts whatever is selected."""
        box = QGroupBox()
        v = QVBoxLayout(box)

        rowc = QHBoxLayout()
        rowc.addWidget(QLabel("camera"))
        self.cmb_camera = QComboBox()
        self.cmb_camera.setToolTip(
            "Which device drives capture. 'Synthetic sky' is the built-in\n"
            "demo scene -- no hardware needed. Picking a webcam opens it\n"
            "(macOS may ask for camera permission the first time).")
        rowc.addWidget(self.cmb_camera, 1)
        btn_rescan = QPushButton("rescan")
        btn_rescan.setToolTip("Look for cameras again, after plugging one in.")
        btn_rescan.clicked.connect(self._populate_cameras)
        rowc.addWidget(btn_rescan)
        v.addLayout(rowc)
        self._camera_combos.append(self.cmb_camera)
        self._populate_cameras()
        # activated fires only on a user pick, never on programmatic updates.
        self.cmb_camera.activated.connect(self._switch_camera)

        self.tuner = ModeTuner(self.MODES, int(self.cfg.get("shoot_mode", 0)))
        self.tuner.changed.connect(self._mode_changed)
        v.addWidget(self.tuner)

        self.lbl_mode_hint = QLabel()
        self.lbl_mode_hint.setStyleSheet("color:#888;")
        v.addWidget(self.lbl_mode_hint)

        self.btn_go = QPushButton()
        self.btn_go.setCheckable(True)
        self.btn_go.setMinimumHeight(70)
        self.btn_go.clicked.connect(self._go_clicked)
        v.addWidget(self.btn_go)

        self._mode_changed(self.tuner.index())
        return box

    def _build_view_row(self) -> QHBoxLayout:
        """Fullscreen and outdoor-mode controls, under the image they act on.

        They used to sit in the mode header, but they tune the *view*, not the
        capture -- and the Field face carries its own big versions of them.
        """
        row = QHBoxLayout()
        row.setContentsMargins(6, 0, 6, 0)
        btn_full = QPushButton("Fullscreen  (F11)")
        btn_full.clicked.connect(self._toggle_fullscreen)
        row.addWidget(btn_full)
        self.chk_outdoor = QCheckBox("Outdoor mode")
        self.chk_outdoor.setToolTip(
            "Contrast-stretches the preview and burns in its edges, so the\n"
            "subject stays findable on a screen washed out by sunlight.")
        self.chk_outdoor.toggled.connect(self._outdoor_toggled)
        row.addWidget(self.chk_outdoor)
        self.cmb_outdoor = QComboBox()
        self.cmb_outdoor.addItems(["boost", "edges only"])
        self.cmb_outdoor.currentIndexChanged.connect(self._outdoor_style_changed)
        row.addWidget(self.cmb_outdoor)
        row.addWidget(QLabel("stripe"))
        sp = QSpinBox()
        sp.setRange(1, 12)
        sp.setValue(int(self.cfg.get("outdoor_stripe_px", 3)))
        sp.setSuffix(" px")
        sp.valueChanged.connect(
            lambda x: (self.cfg.__setitem__("outdoor_stripe_px", x), self._save(),
                       setattr(self.preview, "stripe_px", x), self.preview.update()))
        row.addWidget(sp)
        row.addWidget(QLabel("sensitivity"))
        sd = QDoubleSpinBox()
        sd.setRange(0.2, 6.0)
        sd.setSingleStep(0.2)
        sd.setValue(float(self.cfg.get("outdoor_strength", 1.0)))
        sd.valueChanged.connect(
            lambda x: (self.cfg.__setitem__("outdoor_strength", x), self._save(),
                       setattr(self.preview, "outdoor_strength", x),
                       self.preview.update()))
        row.addWidget(sd)
        row.addStretch(1)
        self.preview.stripe_px = int(self.cfg.get("outdoor_stripe_px", 3))
        self.preview.outdoor_strength = float(self.cfg.get("outdoor_strength", 1.0))
        return row

    def _hide_redundant_buttons(self) -> None:
        """One START button now drives every mode, so the per-mode ones go.

        The objects stay alive because _on_state still uses them to track state;
        they are simply no longer shown.
        """
        for name in ("btn_collect", "btn_rapid", "btn_timelapse", "btn_record",
                     "btn_cascade_go", "btn_bird"):
            b = getattr(self, name, None)
            if b is not None:
                b.setVisible(False)

    def _levels_changed(self, black: float, white: float) -> None:
        self.cfg["tone_black"] = round(float(black), 4)
        self.cfg["tone_white"] = round(float(white), 4)
        self._save()
        span = max(1e-6, white - black)
        self.status.showMessage(
            "levels  black %.2f  white %.2f   mid-tones expanded %.1fx"
            % (black, white, 1.0 / span), 4000)
        if self.cfg.get("levels_live", True):
            self._levels_timer.start()   # restart on every move; fires once

    def _apply_levels(self) -> None:
        """Push the levels into the ISP curve, once the user stops moving them."""
        if self.cfg.get("tone_black", 0.0) <= 0.001 and \
           self.cfg.get("tone_white", 1.0) >= 0.999:
            # Full range: nothing to grade, so leave the stock curve alone
            # rather than writing an identity tuning and reopening the camera.
            if self.cfg.get("tone_curve") == "levels":
                self.cfg["tone_curve"] = "stock"
                self._save()
                self.engine.send("reconfigure")
            return
        self.cfg["tone_curve"] = "levels"
        self._save()
        self._tone_changed(apply=False)
        self.engine.send("reconfigure")
        self._log("levels applied: black %.2f white %.2f (camera reopened)"
                  % (self.cfg["tone_black"], self.cfg["tone_white"]))

    def _mode_changed(self, idx: int) -> None:
        idx = max(0, min(idx, len(self.MODES) - 1))
        self.cfg["shoot_mode"] = idx
        self._save()
        label, _key, hint = self.MODES[idx]
        self.lbl_mode_hint.setText(hint)
        for face in (getattr(self, "face_field", None),
                     getattr(self, "face_camera", None)):
            if face is not None:
                face.sync_mode(idx)
        # Open the matching section so its settings are right there.
        for title, acc in self._acc.items():
            if title.lower().startswith(label.lower()):
                acc.set_expanded(True)
            elif any(title.lower().startswith(m[0].lower()) for m in self.MODES):
                acc.set_expanded(False)
        self._refresh_go_button()

    def _step_mode(self, delta: int) -> None:
        if self.engine.state in ("burst", "rapid", "drain", "timelapse", "video", "birdflight"):
            return          # never switch mode mid-capture
        self.tuner.step(delta)

    def _refresh_go_button(self) -> None:
        state = self.engine.state
        running = state in ("burst", "rapid", "drain", "timelapse", "video", "birdflight")
        label = self.MODES[max(0, min(int(self.cfg.get("shoot_mode", 0)),
                                      len(self.MODES) - 1))][0]
        self.btn_go.setChecked(running)
        self.btn_go.setText(("STOP  -  %s" % state.upper()) if running
                            else "START  -  %s" % label.upper())
        self.btn_go.setStyleSheet(
            "QPushButton{font-size:18px;font-weight:700;border-radius:8px;"
            "background:%s;color:white;}" % ("#a03020" if running else "#1f7a3f"))
        for face in (getattr(self, "face_field", None),
                     getattr(self, "face_camera", None)):
            if face is not None:
                face.update_go(running, state, label)

    def _go_clicked(self) -> None:
        if self.engine.state in ("burst", "rapid", "drain", "timelapse", "video", "birdflight"):
            self.engine.send("stop")
            return
        key = self.MODES[max(0, min(int(self.cfg.get("shoot_mode", 0)),
                                    len(self.MODES) - 1))][1]
        {"burst": self._toggle_collect, "rapid": self._toggle_rapid,
         "timelapse": self._toggle_timelapse, "video": self._toggle_record,
         "birdflight": self._toggle_birdflight}[key]()

    def _outdoor_style_changed(self, i: int) -> None:
        self.preview.outdoor_style = "edges" if i else "boost"
        if hasattr(self, "face_field"):
            self.face_field.set_outdoor(self.chk_outdoor.isChecked(), i)
        self.preview.update()

    def _outdoor_toggled(self, on: bool) -> None:
        self.preview.outdoor = on
        self.cfg["outdoor_mode"] = bool(on)
        self._save()
        if self._fullscreen is not None:
            self._fullscreen.view.outdoor = on
        if hasattr(self, "face_field"):
            self.face_field.set_outdoor(on, self.cmb_outdoor.currentIndex())
        self.preview.update()

    def _set_all_overlays(self, on: bool) -> None:
        """Scroll wheel over the image: everything on, or everything off."""
        for w in (self.preview, getattr(self._fullscreen, "view", None)):
            if w is None:
                continue
            w.show_zebra = on
            w.show_zones = on
            w.show_grid = on
            w.show_peaking = on
            w.show_focus_map = on
            w.show_sharpness = on
            w.show_hud = on
            w.update()
        # Keep the Focus tab's checkboxes honest about what is actually drawn.
        for chk, val in ((getattr(self, "chk_fmap", None), on),
                         (getattr(self, "chk_sharp_num", None), on),
                         (getattr(self, "chk_peak2", None), on),
                         (getattr(self, "chk_zebra2", None), on)):
            if chk is not None:
                chk.blockSignals(True)
                chk.setChecked(val)
                chk.blockSignals(False)
        self.engine.send("focus_map", on=on)
        self.status.showMessage("all overlays %s" % ("on" if on else "off"), 2000)

    def _toggle_fullscreen(self) -> None:
        if self._fullscreen is not None:
            self._fullscreen.close()
            return
        view = PreviewWidget()
        for attr in ("show_zebra", "show_peaking", "show_zones", "show_grid",
                     "show_focus_map", "show_sharpness", "show_hud", "outdoor",
                     "outdoor_style", "outdoor_strength", "stripe_px",
                     "sky_zone_frac"):
            setattr(view, attr, getattr(self.preview, attr))
        view.double_clicked.connect(self._toggle_fullscreen)
        view.overlays_toggled.connect(self._set_all_overlays)
        self._fullscreen = FullscreenPreview(view, self)
        self._fullscreen.closed.connect(self._fullscreen_closed)
        self._fullscreen.showFullScreen()

    def _fullscreen_closed(self) -> None:
        self._fullscreen = None

    # ---- camera selection --------------------------------------------
    def _populate_cameras(self) -> None:
        from birdshot import backends
        self._cameras = backends.list_cameras()
        current = backends.resolve_choice(self.cfg)
        selected = len(self._cameras) - 1        # synthetic is always last
        labels = []
        for i, cam in enumerate(self._cameras):
            labels.append(cam["model"] if cam["backend"] == "synthetic"
                          else "%s  (%s)" % (cam["model"], cam["backend"]))
            if (cam["backend"], cam["index"]) == current:
                selected = i
        # Every face's picker shows the same list and the same selection.
        for combo in self._camera_combos:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(labels)
            combo.setCurrentIndex(selected)
            combo.blockSignals(False)
        if hasattr(self, "face_field"):
            self.face_field.set_camera_label(labels[selected])

    def _switch_camera(self, i: int) -> None:
        """Tear the engine down and rebuild it on the picked device."""
        from birdshot import backends
        if not (0 <= i < len(self._cameras)):
            return
        cam = self._cameras[i]
        if ((cam["backend"], cam["index"]) == backends.resolve_choice(self.cfg)
                and self.engine.is_alive()):
            return
        previous = (self.cfg.get("backend"), int(self.cfg.get("camera_index", 0)))
        self.cfg["backend"] = cam["backend"]
        self.cfg["camera_index"] = cam["index"]
        self._save()

        old = self.engine
        try:
            old.send("stop")
            old.shutdown()
            old.join(timeout=10)
        except Exception:  # noqa: BLE001 -- a wedged engine must not block the swap
            pass
        try:
            backends.warm_up(self.cfg)   # we ARE the main thread here
            self.engine = self._engine_factory(self._emit_event)
        except Exception as exc:  # noqa: BLE001
            self.cfg["backend"], self.cfg["camera_index"] = previous
            self._save()
            self.banner.setText("could not open %s: %s" % (cam["model"], exc))
            self.banner.setVisible(True)
            self.engine = self._engine_factory(self._emit_event)
            self._populate_cameras()
        self.engine.start()
        self.engine.send("preview")

    def _build_readout(self) -> QWidget:
        """One compact line. The detail lives in the HUD drawn on the image.

        The old five-row grid cost about 110px of height that the 4:3 canvas
        wanted more than the numbers did.
        """
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(10)
        mono = QFont("DejaVu Sans Mono", 10)

        self.lbl_line = QLabel("-")
        self.lbl_line.setFont(mono)
        row.addWidget(self.lbl_line, 1)

        self.lbl_verdict = QLabel("-")
        self.lbl_verdict.setFont(QFont("DejaVu Sans", 10, QFont.Bold))
        row.addWidget(self.lbl_verdict)

        self.lbl_session = QLabel("-")
        self.lbl_session.setFont(mono)
        self.lbl_session.setStyleSheet("color:#8d949c;")
        row.addWidget(self.lbl_session)

        # Kept so the rest of the code can keep writing to them; they simply
        # feed the single line and the on-image HUD now.
        for name in ("lbl_shutter", "lbl_gain", "lbl_folder", "lbl_lux",
                     "lbl_fps", "lbl_clip", "lbl_sharp", "lbl_tiles",
                     "lbl_ae", "lbl_counts"):
            setattr(self, name, QLabel())
        box.setMaximumHeight(26)
        return box

    # ---- binding helpers ---------------------------------------------
    def _save(self) -> None:
        if not self._binding:
            self.cfg.save()

    def _spin(self, key: str, lo, hi, step=1, decimals=None, suffix="", on_change=None):
        if decimals is None:
            w = QSpinBox()
            w.setRange(int(lo), int(hi))
            w.setSingleStep(int(step))
            w.setValue(int(self.cfg[key]))
            sig = w.valueChanged
        else:
            w = QDoubleSpinBox()
            w.setRange(float(lo), float(hi))
            w.setSingleStep(float(step))
            w.setDecimals(decimals)
            w.setValue(float(self.cfg[key]))
            sig = w.valueChanged
        if suffix:
            w.setSuffix(suffix)

        def handler(value):
            self.cfg[key] = value
            self._save()
            if on_change:
                on_change(value)

        sig.connect(handler)
        return w

    def _check(self, key: str, text: str, on_change=None) -> QCheckBox:
        w = QCheckBox(text)
        w.setChecked(bool(self.cfg[key]))

        def handler(state):
            self.cfg[key] = bool(state)
            self._save()
            if on_change:
                on_change(bool(state))

        w.toggled.connect(handler)
        return w

    def _combo(self, key: str, items, on_change=None) -> QComboBox:
        w = QComboBox()
        for label in items:
            w.addItem(label)
        idx = self.cfg[key]
        if isinstance(idx, str):
            idx = items.index(idx) if idx in items else 0
        w.setCurrentIndex(int(idx))

        def handler(i):
            self.cfg[key] = i
            self._save()
            if on_change:
                on_change(i)

        w.currentIndexChanged.connect(handler)
        return w

    # ---- tabs --------------------------------------------------------
    def _tab_capture(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        self.btn_collect = QPushButton("COLLECT  -  full res, fastest rate")
        self.btn_collect.setCheckable(True)
        self.btn_collect.setMinimumHeight(64)
        self.btn_collect.setStyleSheet(
            "QPushButton{font-size:16px;font-weight:700;background:#1f7a3f;color:white;"
            "border-radius:6px;}"
            "QPushButton:checked{background:#a03020;}"
        )
        self.btn_collect.clicked.connect(self._toggle_collect)
        v.addWidget(self.btn_collect)

        hint = QLabel(
            "Runs the sensor as fast as JPEG encoding allows, with auto-exposure\n"
            "holding the shortest shutter that keeps the subject zone exposed.\n"
            "Frames land in a new session folder, split by shutter duration."
        )
        hint.setStyleSheet("color:#888;")
        v.addWidget(hint)

        form = QFormLayout()
        self.cmb_mode = self._combo(
            "capture_mode", [m[2] for m in CAPTURE_MODES],
            on_change=lambda i: self.engine.send("reconfigure"),
        )
        form.addRow("Resolution", self.cmb_mode)
        self.lbl_expect = QLabel()
        form.addRow("Expected rate", self.lbl_expect)
        self.cmb_mode.currentIndexChanged.connect(self._update_expected)
        self._update_expected(self.cfg["capture_mode"])

        form.addRow("Burst limit (0 = unlimited)", self._spin("burst_count", 0, 100000, 10))
        form.addRow("JPEG quality", self._spin("jpeg_quality", 50, 100, 1))
        form.addRow("Encode threads", self._spin("encode_threads", 1, 8, 1))
        v.addLayout(form)

        row = QHBoxLayout()
        btn_single = QPushButton("Single shot")
        btn_single.clicked.connect(lambda: self.engine.send("single", save=True))
        row.addWidget(btn_single)
        btn_preview = QPushButton("Preview only")
        btn_preview.clicked.connect(lambda: self.engine.send("preview"))
        row.addWidget(btn_preview)
        v.addLayout(row)

        geo = QGroupBox("Orientation and framing")
        gv = QVBoxLayout(geo)
        gv.addWidget(self._check("hflip", "Horizontal flip",
                                 on_change=lambda _: self.engine.send("reconfigure")))
        gv.addWidget(self._check("vflip", "Vertical flip",
                                 on_change=lambda _: self.engine.send("reconfigure")))
        cb_zone = QCheckBox("Metering zones")
        cb_zone.setChecked(True)
        cb_zone.toggled.connect(lambda s: setattr(self.preview, "show_zones", s))
        gv.addWidget(cb_zone)
        cb_g = QCheckBox("Thirds grid")
        cb_g.toggled.connect(lambda s: setattr(self.preview, "show_grid", s))
        gv.addWidget(cb_g)
        gv.addWidget(QLabel("Focus aids live in Scene > Focus aids."))
        v.addWidget(geo)

        v.addStretch(1)
        return page

    # ------------------------------------------------------------------
    def _tab_rapid(self) -> QWidget:
        """Fastest-possible stills, written as flat YYYYmmddHHMMSS.jpg."""
        page = QWidget()
        v = QVBoxLayout(page)

        self.btn_rapid = QPushButton("RAPID  -  fastest single photos")
        self.btn_rapid.setCheckable(True)
        self.btn_rapid.setMinimumHeight(64)
        self.btn_rapid.setStyleSheet(
            "QPushButton{font-size:16px;font-weight:700;background:#1d5f9e;color:white;"
            "border-radius:6px;}"
            "QPushButton:checked{background:#a03020;}"
        )
        self.btn_rapid.clicked.connect(self._toggle_rapid)
        v.addWidget(self.btn_rapid)

        hint = QLabel(
            "Flat files named YYYYmmddHHMMSS.jpg in one folder per run.\n"
            "No shutter subfolders, no quality gates - metering and auto-exposure\n"
            "only, so the loop stays out of the sensor's way."
        )
        hint.setStyleSheet("color:#888;")
        v.addWidget(hint)

        mode = QGroupBox("Strategy")
        mv = QVBoxLayout(mode)
        self.cmb_rapid_mode = self._combo_str("rapid_mode", ["ram", "continuous"])
        self.cmb_rapid_mode.currentIndexChanged.connect(lambda _: self._update_rapid_estimate())
        mv.addWidget(self.cmb_rapid_mode)
        self.lbl_rapid_mode = QLabel()
        self.lbl_rapid_mode.setWordWrap(True)
        self.lbl_rapid_mode.setStyleSheet("color:#888;")
        mv.addWidget(self.lbl_rapid_mode)
        v.addWidget(mode)

        form = QFormLayout()
        self.cmb_rapid_res = self._combo(
            "capture_mode", [m[2] for m in CAPTURE_MODES],
            on_change=lambda i: (self.engine.send("reconfigure"),
                                 self._update_rapid_estimate(),
                                 self.cmb_mode.setCurrentIndex(i)),
        )
        form.addRow("Resolution", self.cmb_rapid_res)
        form.addRow("Frame limit (0 = max)",
                    self._spin("rapid_count", 0, 100000, 10,
                               on_change=lambda _: self._update_rapid_estimate()))
        v.addLayout(form)

        self.lbl_rapid_est = QLabel()
        self.lbl_rapid_est.setWordWrap(True)
        self.lbl_rapid_est.setStyleSheet(
            "background:#20242a;padding:8px;border-radius:4px;font-family:monospace;"
        )
        v.addWidget(self.lbl_rapid_est)

        self.prog_rapid = QProgressBar()
        self.prog_rapid.setVisible(False)
        v.addWidget(self.prog_rapid)

        self.lbl_rapid_live = QLabel("-")
        self.lbl_rapid_live.setStyleSheet("font-family:monospace;font-size:12px;")
        v.addWidget(self.lbl_rapid_live)

        v.addStretch(1)
        self._update_rapid_estimate()
        return page

    def _update_rapid_estimate(self) -> None:
        """Show what the selected strategy will actually deliver."""
        idx = int(self.cfg["capture_mode"])
        w, h, label, inline_fps = CAPTURE_MODES[max(0, min(idx, len(CAPTURE_MODES) - 1))]
        per_mb = w * h * 3 / 1e6
        ram_mb = available_ram_mb() * 0.55
        capacity = int(ram_mb / max(per_mb, 0.1))
        limit = int(self.cfg["rapid_count"]) or capacity

        cont_fps = RAPID_CONT_FPS.get((w, h), inline_fps)
        ram_fps = RAPID_RAM_FPS.get((w, h), inline_fps)

        if self.cfg["rapid_mode"] == "ram":
            self.lbl_rapid_mode.setText(
                "Frames go straight to memory; nothing is encoded until the burst\n"
                "ends. Measured slower than continuous at this resolution\n"
                "(%.1f vs %.1f fps) because the buffer copy runs on the capture\n"
                "loop instead of the worker threads. Useful only if you need\n"
                "zero disk I/O during the burst." % (ram_fps, cont_fps)
            )
            frames = min(limit, capacity)
            self.lbl_rapid_est.setText(
                "%dx%d   %.1f MB per frame\n"
                "RAM budget %.0f MB  ->  %d frames capacity\n"
                "~%.1f fps  ->  about %.1f s of shooting, then a drain to disk"
                % (w, h, per_mb, ram_mb, capacity, ram_fps,
                   frames / max(ram_fps, 0.1))
            )
        else:
            self.lbl_rapid_mode.setText(
                "Encodes as it goes, on the worker threads. Fastest option at\n"
                "this resolution and it runs indefinitely with no drain phase."
            )
            self.lbl_rapid_est.setText(
                "%dx%d   %.1f MB per frame\n"
                "~%.1f fps sustained, unlimited duration\n"
                "roughly %.0f MB and %d frames per minute"
                % (w, h, per_mb, cont_fps, cont_fps * 60 * per_mb * 0.045,
                   int(cont_fps * 60))
            )

    def _toggle_rapid(self) -> None:
        if self.engine.state in ("rapid", "drain"):
            self.engine.send("stop")
        else:
            self._counts = {"ok": 0, "dark": 0, "blown": 0, "empty": 0}
            self._session_frames = 0
            self._session_bytes = 0
            self._rapid_t0 = time.time()
            self.engine.send("rapid", mode=self.cfg["rapid_mode"],
                             count=int(self.cfg["rapid_count"]))

    def _on_rapid(self, payload: Dict[str, Any]) -> None:
        phase = payload.get("phase")
        if phase == "start":
            if payload.get("mode") == "ram":
                self.prog_rapid.setVisible(True)
                self.prog_rapid.setRange(0, int(payload.get("target") or 0) or 100)
                self.prog_rapid.setValue(0)
                self.prog_rapid.setFormat("buffering %v / %m frames")
                self._log("rapid RAM burst: capacity %d frames (%.0f MB budget)"
                          % (payload.get("capacity", 0), payload.get("budget_mb", 0)))
            else:
                self.prog_rapid.setVisible(False)
                self._log("rapid continuous capture started")
        elif phase == "drain":
            total, done = payload.get("total", 0), payload.get("done", 0)
            self.prog_rapid.setVisible(True)
            self.prog_rapid.setRange(0, total)
            self.prog_rapid.setValue(done)
            self.prog_rapid.setFormat("encoding %v / %m frames")
        elif phase == "done":
            self.prog_rapid.setValue(payload.get("total", 0))
            self.prog_rapid.setFormat("done: %m frames written")
            self._log("rapid burst drained: %d frames written" % payload.get("total", 0))
            self._refresh_sources()

    def _update_expected(self, idx: int) -> None:
        try:
            mode = CAPTURE_MODES[int(idx)]
        except (IndexError, ValueError):
            return
        self.lbl_expect.setText("~%.1f fps measured end-to-end on this Pi" % mode[3])

    # ------------------------------------------------------------------
    def _tab_focus(self) -> QWidget:
        """Everything for setting focus on the manual C-mount lens."""
        page = QWidget()
        v = QVBoxLayout(page)

        intro = QLabel(
            "The lens is manual with no feedback, and the subject is usually a\n"
            "small shape against bright sky. These are the aids that make that\n"
            "workable."
        )
        intro.setStyleSheet("color:#888;")
        v.addWidget(intro)

        overlays = QGroupBox("Overlays on the main preview")
        ov = QVBoxLayout(overlays)

        self.chk_fmap = QCheckBox("Focus map  -  shade each area by how sharp it is")
        self.chk_fmap.toggled.connect(self._toggle_focus_map)
        ov.addWidget(self.chk_fmap)
        fmap_hint = QLabel(
            "    Ranks the frame by resolved detail and rings the sharpest area.\n"
            "    Peaking alone cannot tell a sharp branch from noisy sky; this can."
        )
        fmap_hint.setStyleSheet("color:#777;font-size:11px;")
        ov.addWidget(fmap_hint)

        self.chk_sharp_num = QCheckBox("Sharpness readout  -  large number with peak-hold")
        self.chk_sharp_num.toggled.connect(
            lambda s: (setattr(self.preview, "show_sharpness", s), self.preview.update())
        )
        ov.addWidget(self.chk_sharp_num)

        self.chk_peak2 = QCheckBox("Focus peaking  -  highlight high-contrast edges")
        self.chk_peak2.toggled.connect(
            lambda s: (setattr(self.preview, "show_peaking", s), self.preview.update())
        )
        ov.addWidget(self.chk_peak2)

        self.chk_zebra2 = QCheckBox("Clipping zebras")
        self.chk_zebra2.setChecked(True)
        self.chk_zebra2.toggled.connect(
            lambda s: (setattr(self.preview, "show_zebra", s), self.preview.update())
        )
        ov.addWidget(self.chk_zebra2)

        row = QHBoxLayout()
        btn_reset_peak = QPushButton("Reset peak-hold")
        btn_reset_peak.clicked.connect(self.preview.reset_focus_peak)
        row.addWidget(btn_reset_peak)
        row.addStretch(1)
        ov.addLayout(row)
        v.addWidget(overlays)

        cal = QGroupBox("Blur gate calibration")
        cv = QVBoxLayout(cal)
        cv.addWidget(QLabel(
            "What counts as 'sharp' depends on the lens, aperture and subject,\n"
            "so the blur gate is referenced to a frame you call focused rather\n"
            "than to a fixed number. Focus carefully, then press this."
        ))
        btn_setref = QPushButton("Use current view as the sharp reference")
        btn_setref.clicked.connect(self._set_sharpness_reference)
        cv.addWidget(btn_setref)
        self.lbl_sharp_ref = QLabel()
        self.lbl_sharp_ref.setStyleSheet("font-family:monospace;color:#9a9;")
        cv.addWidget(self.lbl_sharp_ref)
        v.addWidget(cal)
        self._refresh_sharp_ref()

        mon = QGroupBox("1:1 focus monitor")
        mv = QVBoxLayout(mon)
        btn_focus = QPushButton("Open focus monitor")
        btn_focus.setMinimumHeight(46)
        btn_focus.setStyleSheet("font-weight:600;")
        btn_focus.clicked.connect(self._open_focus_monitor)
        mv.addWidget(btn_focus)
        mv.addWidget(QLabel(
            "A frameless, always-on-top window showing real sensor pixels at\n"
            "100-400%, with peaking and a peak-hold score. Turn the ring until\n"
            "the number stops climbing.\n\n"
            "Full-frame copies are only taken while that window is open, so it\n"
            "costs nothing when closed."
        ))
        v.addWidget(mon)

        live = QGroupBox("Live focus reading")
        lv = QVBoxLayout(live)
        self.lbl_focus_live = QLabel("-")
        self.lbl_focus_live.setStyleSheet("font-family:monospace;font-size:12px;")
        lv.addWidget(self.lbl_focus_live)
        v.addWidget(live)

        v.addStretch(1)
        return page

    def _set_sharpness_reference(self) -> None:
        """Anchor the blur gate to what the user says is a focused frame."""
        value = self._last_sharpness
        if not value:
            QMessageBox.information(
                self, "No reading yet",
                "Wait for a live frame with a sharpness reading first.")
            return
        self.cfg["sharpness_reference"] = value
        # Half the reference: comfortably below a good frame, well above the
        # 2-3x margin that separates focused from visibly soft.
        self.cfg["blur_threshold"] = round(value * 0.5, 2)
        self.cfg.save()
        self._refresh_sharp_ref()
        self._log("sharp reference %.1f, blur gate now %.1f"
                  % (value, self.cfg["blur_threshold"]))

    def _refresh_sharp_ref(self) -> None:
        ref = self.cfg.get("sharpness_reference")
        if ref:
            self.lbl_sharp_ref.setText(
                "reference %.1f   ->   frames below %.1f are flagged soft"
                % (ref, self.cfg["blur_threshold"]))
        else:
            self.lbl_sharp_ref.setText(
                "not set - blur gate is at %.1f and will not reject anything"
                % self.cfg["blur_threshold"])

    def _toggle_focus_map(self, on: bool) -> None:
        self.preview.show_focus_map = on
        # The map costs a Laplacian pass per frame, so only compute it while
        # something is actually displaying it.
        self.engine.send("focus_map", on=on)
        self.preview.update()

    def _tab_changed(self, index: int) -> None:
        if not hasattr(self, "_tabs"):
            return
        if self._tabs.tabText(index) == "Machine":
            self._refresh_cascade()

    def _focus_section_toggled(self, on: bool) -> None:
        """The focus map costs a Laplacian pass per frame, so it follows the
        Focus section the way it used to follow the old Focus tab."""
        if on and not self.chk_fmap.isChecked():
            self.chk_fmap.setChecked(True)
            self.chk_sharp_num.setChecked(True)
        elif not on and self.chk_fmap.isChecked():
            self.chk_fmap.setChecked(False)

    # ------------------------------------------------------------------
    def _tab_encode(self) -> QWidget:
        """Turn any folder of stills into an encoded movie."""
        page = QWidget()
        v = QVBoxLayout(page)

        v.addWidget(QLabel("Encode a folder of photos into a video."))

        src = QGroupBox("Source")
        sf = QFormLayout(src)
        self.cmb_source = QComboBox()
        self.cmb_source.currentIndexChanged.connect(lambda _: self._scan_source())
        sf.addRow("Folder", self.cmb_source)

        row = QHBoxLayout()
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_source)
        row.addWidget(btn_browse)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_sources)
        row.addWidget(btn_refresh)
        sf.addRow(row)

        self.chk_recursive = QCheckBox("Include subfolders")
        self.chk_recursive.setChecked(True)
        self.chk_recursive.toggled.connect(lambda _: self._scan_source())
        sf.addRow(self.chk_recursive)

        self.chk_exif = QCheckBox("Write EXIF into the source frames first")
        self.chk_exif.setChecked(bool(self.cfg["exif_enabled"]))
        self.chk_exif.toggled.connect(
            lambda s: (self.cfg.__setitem__("exif_enabled", s), self.cfg.save()))
        self.chk_exif.setToolTip(
            "Stamps date/time to the centisecond, exposure, ISO from gain, and\n"
            "birdshot's own metrics into each JPEG. Modifies the source files in\n"
            "place, losslessly - the JPEG is not re-encoded.")
        sf.addRow(self.chk_exif)

        self.chk_encode_ok = QCheckBox("Only frames that passed the quality gates")
        self.chk_encode_ok.setChecked(bool(self.cfg["encode_only_ok"]))
        self.chk_encode_ok.toggled.connect(
            lambda s: (self.cfg.__setitem__("encode_only_ok", s), self.cfg.save(),
                       self._scan_source())
        )
        sf.addRow(self.chk_encode_ok)

        self.lbl_source_info = QLabel("-")
        self.lbl_source_info.setWordWrap(True)
        self.lbl_source_info.setStyleSheet(
            "background:#20242a;padding:8px;border-radius:4px;font-family:monospace;"
        )
        sf.addRow(self.lbl_source_info)
        v.addWidget(src)

        out = QGroupBox("Output")
        of = QFormLayout(out)
        of.addRow("Frame rate", self._spin("encode_fps", 1, 240, 1, suffix=" fps",
                                           on_change=lambda _: self._scan_source()))
        self.spin_ewidth = QSpinBox()
        self.spin_ewidth.setRange(0, 4096)
        self.spin_ewidth.setValue(int(self.cfg["encode_width"]))
        self.spin_ewidth.setSpecialValueText("native")
        self.spin_ewidth.setSuffix(" px wide")
        self.spin_ewidth.valueChanged.connect(
            lambda x: (self.cfg.__setitem__("encode_width", x), self.cfg.save())
        )
        of.addRow("Scale", self.spin_ewidth)
        of.addRow("Quality (CRF, lower = better)",
                  self._spin("encode_crf", 0, 51, 1))
        self.cmb_preset = self._combo_str(
            "encode_preset",
            ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
        )
        of.addRow("Encoder preset", self.cmb_preset)
        self.ed_output = QLineEdit()
        self.ed_output.setPlaceholderText("(auto: <folder-name>_<fps>fps.mp4)")
        of.addRow("Output file", self.ed_output)
        v.addWidget(out)

        row2 = QHBoxLayout()
        self.btn_encode = QPushButton("Encode on the Pi")
        self.btn_encode.setMinimumHeight(44)
        self.btn_encode.setStyleSheet("font-weight:600;")
        self.btn_encode.clicked.connect(self._start_encode)
        row2.addWidget(self.btn_encode)
        self.btn_encode_cancel = QPushButton("Cancel")
        self.btn_encode_cancel.setEnabled(False)
        self.btn_encode_cancel.clicked.connect(self._cancel_encode)
        row2.addWidget(self.btn_encode_cancel)
        v.addLayout(row2)

        self.prog_encode = QProgressBar()
        self.prog_encode.setVisible(False)
        v.addWidget(self.prog_encode)

        self.lbl_encode_status = QLabel("-")
        self.lbl_encode_status.setWordWrap(True)
        self.lbl_encode_status.setStyleSheet("font-family:monospace;font-size:11px;")
        v.addWidget(self.lbl_encode_status)

        note = QLabel(
            "libx264 on four A72 cores is slow for 12 MP stills. For anything\n"
            "long, pull the folder to the Mac and run mac/assemble.sh - identical\n"
            "frame selection, an order of magnitude faster."
        )
        note.setStyleSheet("color:#888;")
        v.addWidget(note)

        v.addStretch(1)
        self._refresh_sources()
        return page

    def _refresh_sources(self) -> None:
        """List every folder that might hold photos, newest first."""
        if not hasattr(self, "cmb_source"):
            return
        current = self.cmb_source.currentData()
        self.cmb_source.blockSignals(True)
        self.cmb_source.clear()
        for s in tl.list_sessions(self.cfg["data_root"]):
            self.cmb_source.addItem("%s  (%s frames)"
                                    % (s["id"], s.get("frames") or "?"), s["path"])
        # The old runCam.sh folders are perfectly valid input too.
        for legacy in (os.path.expanduser("~/sauto"), "/media/pi/ARCHIVE/s191",
                       "/media/pi/ARCHIVE/sauto"):
            if os.path.isdir(legacy):
                self.cmb_source.addItem("%s  (legacy)" % legacy, legacy)
        self.cmb_source.blockSignals(False)
        if current:
            idx = self.cmb_source.findData(current)
            if idx >= 0:
                self.cmb_source.setCurrentIndex(idx)
        self._scan_source()

    def _browse_source(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder of photos",
                                                self.cfg["data_root"])
        if path:
            self.cmb_source.addItem(path, path)
            self.cmb_source.setCurrentIndex(self.cmb_source.count() - 1)

    def _scan_source(self) -> None:
        """Count what would actually be encoded, and say so before committing."""
        if not hasattr(self, "cmb_source"):
            return
        path = self.cmb_source.currentData()
        if not path or not os.path.isdir(path):
            self.lbl_source_info.setText("no folder selected")
            return
        indexed = tl.has_index(path)
        self.chk_encode_ok.setEnabled(indexed)
        frames = tl.select_frames(path, only_ok=indexed and self.chk_encode_ok.isChecked(),
                                  recursive=self.chk_recursive.isChecked())
        fps = max(1, int(self.cfg["encode_fps"]))
        total = 0
        for f in frames[:200]:
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        avg = total / max(min(len(frames), 200), 1)
        self.lbl_source_info.setText(
            "%d images%s\n%.1f s of video at %d fps\nsource is about %.0f MB"
            % (len(frames),
               "  (index.jsonl present)" if indexed else "  (no index - plain folder)",
               len(frames) / float(fps), fps, avg * len(frames) / 1e6)
        )

    def _start_encode(self) -> None:
        path = self.cmb_source.currentData()
        if not path or not os.path.isdir(path):
            QMessageBox.information(self, "Encode", "Choose a source folder first.")
            return
        if self._assemble_job is not None and self._assemble_job.is_alive():
            QMessageBox.information(self, "Encode", "An encode is already running.")
            return

        fps = int(self.cfg["encode_fps"])
        out = self.ed_output.text().strip()
        if not out:
            out_dir = self.storage.media_root("timelapse")
            out = os.path.join(out_dir, "%s_%dfps.mp4"
                               % (os.path.basename(path.rstrip("/")), fps))
        elif not os.path.isabs(out):
            out = os.path.join(self.cfg["data_root"], "timelapse", out)
        if os.path.exists(out):
            reply = QMessageBox.question(
                self, "Overwrite?",
                "%s already exists.\n\nOverwrite it?" % out,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        indexed = tl.has_index(path)
        self.prog_encode.setVisible(True)
        self.prog_encode.setRange(0, 100)
        self.prog_encode.setValue(0)
        self.prog_encode.setFormat("%p%  (%v frames)")
        self.btn_encode.setEnabled(False)
        self.btn_encode_cancel.setEnabled(True)
        self.lbl_encode_status.setText("encoding -> %s" % out)
        self._log("encoding %s -> %s" % (os.path.basename(path), out))

        self._assemble_job = tl.AssembleJob(
            path, out, fps=fps,
            only_ok=indexed and self.chk_encode_ok.isChecked(),
            width=self.spin_ewidth.value() or None,
            crf=int(self.cfg["encode_crf"]),
            preset=self.cfg["encode_preset"],
            recursive=self.chk_recursive.isChecked(),
            write_exif=self.chk_exif.isChecked(), cfg=self.cfg,
            on_stage=lambda st: self.bridge.event.emit("encode_stage", {"stage": st}),
            on_done=lambda r: self.bridge.event.emit("assembled", r),
            on_progress=lambda d, t: self.bridge.event.emit(
                "encode_progress", {"done": d, "total": t}),
        )
        self._assemble_job.start()

    def _cancel_encode(self) -> None:
        if self._assemble_job is not None and self._assemble_job.is_alive():
            self._assemble_job.cancel.set()
            self._log("encode cancelled")

    def _on_encode_progress(self, payload: Dict[str, Any]) -> None:
        total = max(1, int(payload.get("total") or 1))
        done = int(payload.get("done") or 0)
        self.prog_encode.setRange(0, total)
        self.prog_encode.setValue(min(done, total))

    def _tab_exposure(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        auto = self._check("auto_exposure", "Auto exposure (PID + lux feed-forward)")
        v.addWidget(auto)

        man = QGroupBox("Manual")
        mf = QFormLayout(man)
        self.cmb_preset = QComboBox()
        for us in PRESET_SHUTTERS_US:
            self.cmb_preset.addItem("%s   [%s]" % (describe_shutter(us), shutter_dir(us)), us)
        self.cmb_preset.currentIndexChanged.connect(self._preset_picked)
        mf.addRow("Shutter preset", self.cmb_preset)
        self.spin_shutter = self._spin("manual_shutter_us", 114, 60_000_000, 100,
                                       suffix=" us", on_change=self._push_manual)
        mf.addRow("Shutter", self.spin_shutter)
        self.spin_gain = self._spin("manual_gain", 1.0, 22.0, 0.5, decimals=2,
                                    suffix="x", on_change=self._push_manual)
        mf.addRow("Analogue gain", self.spin_gain)
        v.addWidget(man)

        auto_box = QGroupBox("Auto exposure targets")
        af = QFormLayout(auto_box)
        af.addRow("Target luma (0-255)", self._spin("target_luma", 20, 240, 2, decimals=1))
        af.addRow("Highlight tolerance", self._spin("max_clip_frac", 0.0, 0.5, 0.005,
                                                    decimals=3))
        af.addRow("Sky zone (top fraction)", self._spin("sky_zone_frac", 0.0, 0.9, 0.05,
                                                        decimals=2))
        af.addRow("Sky metering weight", self._spin("sky_weight", 0.0, 2.0, 0.05,
                                                    decimals=2))
        v.addWidget(auto_box)

        ladder = QGroupBox("Shutter/gain ladder (shortest shutter first)")
        lf = QFormLayout(ladder)
        lf.addRow("Motion limit", self._spin("motion_limit_us", 114, 100_000, 250,
                                             suffix=" us"))
        lf.addRow("Preferred max gain", self._spin("gain_preferred_max", 1.0, 22.0, 0.5,
                                                   decimals=1, suffix="x"))
        lf.addRow("Hard max shutter", self._spin("shutter_hard_max_us", 1000, 20_000_000,
                                                 1000, suffix=" us"))
        note = QLabel("Gain rises to the preferred cap before the shutter is allowed\n"
                      "past the motion limit, so wingbeats stay frozen.")
        note.setStyleSheet("color:#888;")
        lf.addRow(note)
        v.addWidget(ladder)

        pid = QGroupBox("PID smoothing")
        pf = QFormLayout(pid)
        pf.addRow("Kp", self._spin("pid_kp", 0.0, 3.0, 0.05, decimals=2))
        pf.addRow("Ki", self._spin("pid_ki", 0.0, 2.0, 0.02, decimals=2))
        pf.addRow("Kd", self._spin("pid_kd", 0.0, 2.0, 0.02, decimals=2))
        pf.addRow("Deadband (EV)", self._spin("pid_deadband_ev", 0.0, 1.0, 0.02,
                                              decimals=2))
        pf.addRow("Max step (EV)", self._spin("pid_slew_ev", 0.1, 5.0, 0.1, decimals=1))
        pf.addRow("Meter smoothing", self._spin("meter_ema", 0.05, 1.0, 0.05, decimals=2))
        v.addWidget(pid)

        tone_box = QGroupBox("Tone curve (applied by the ISP, no CPU cost)")
        tf = QVBoxLayout(tone_box)
        tf.addWidget(QLabel(
            "The HQ camera's gamma curve is ALREADY applied in hardware. These\n"
            "replace that curve rather than adding a second one -- re-applying it\n"
            "in software would double it (shadows +116%) and cost 449 ms a frame."
        ))
        self.tone_plot = ToneCurveWidget()
        tf.addWidget(self.tone_plot)

        tform = QFormLayout()
        from .. import tone as tonemod
        self.cmb_tone = self._combo_str("tone_curve", [k for k, _ in tonemod.PRESETS])
        self.cmb_tone.currentIndexChanged.connect(lambda _: self._tone_changed())
        tform.addRow("Curve", self.cmb_tone)
        self.lbl_tone_help = QLabel()
        self.lbl_tone_help.setStyleSheet("color:#888;")
        tform.addRow(self.lbl_tone_help)
        tform.addRow("Gamma", self._spin("tone_gamma", 0.5, 5.0, 0.1, decimals=2,
                                         on_change=lambda _: self._tone_changed()))
        tform.addRow("Contrast", self._spin("tone_contrast", 0.2, 3.0, 0.05,
                                            decimals=2,
                                            on_change=lambda _: self._tone_changed()))
        tform.addRow("Shadow lift", self._spin("tone_lift", -0.5, 0.5, 0.05,
                                               decimals=2,
                                               on_change=lambda _: self._tone_changed()))
        tform.addRow("Highlight knee", self._spin("tone_knee", 0.2, 0.95, 0.05,
                                                  decimals=2,
                                                  on_change=lambda _: self._tone_changed()))
        tform.addRow("Shoulder", self._spin("tone_shoulder", 0.2, 6.0, 0.2,
                                            decimals=1,
                                            on_change=lambda _: self._tone_changed()))
        tf.addLayout(tform)

        live = QGroupBox("Live ISP controls (no restart)")
        lf = QFormLayout(live)
        lf.addRow("Contrast", self._spin("isp_contrast", 0.0, 4.0, 0.05, decimals=2,
                                         on_change=lambda _: self.engine.send("set_exposure")))
        lf.addRow("Brightness", self._spin("isp_brightness", -1.0, 1.0, 0.05,
                                           decimals=2,
                                           on_change=lambda _: self.engine.send("set_exposure")))
        lf.addRow("Saturation", self._spin("isp_saturation", 0.0, 4.0, 0.05,
                                           decimals=2,
                                           on_change=lambda _: self.engine.send("set_exposure")))
        lf.addRow("Sharpness", self._spin("isp_sharpness", 0.0, 4.0, 0.05, decimals=2,
                                          on_change=lambda _: self.engine.send("set_exposure")))
        tf.addWidget(live)

        btn_apply = QPushButton("Apply curve (reopens the camera, ~1 s)")
        btn_apply.clicked.connect(lambda: self.engine.send("reconfigure"))
        tf.addWidget(btn_apply)
        v.addWidget(tone_box)
        self._tone_changed(apply=False)

        row = QHBoxLayout()
        btn_reset = QPushButton("Reset AE loop")
        btn_reset.clicked.connect(lambda: self.engine.send("reset_ae"))
        row.addWidget(btn_reset)
        btn_cal = QPushButton("Run calibration wizard")
        btn_cal.clicked.connect(self._open_calibration)
        row.addWidget(btn_cal)
        v.addLayout(row)

        self.lbl_cal = QLabel()
        self.lbl_cal.setStyleSheet("color:#888;")
        v.addWidget(self.lbl_cal)
        self._refresh_calibration_label()

        v.addStretch(1)
        return page

    def _tone_changed(self, apply: bool = True) -> None:
        from .. import tone as tonemod

        kind = self.cfg.get("tone_curve", "stock")
        self.lbl_tone_help.setText(
            dict(tonemod.PRESETS).get(kind, ""))
        self.tone_plot.set_curves(tonemod.curve_from_cfg(self.cfg),
                                  tonemod.stock_curve())
        if apply:
            self._log(tonemod.describe(self.cfg).splitlines()[0])

    def _preset_picked(self, idx: int) -> None:
        us = self.cmb_preset.itemData(idx)
        if us:
            self.spin_shutter.setValue(int(us))

    def _push_manual(self, _value=None) -> None:
        if not self.cfg["auto_exposure"]:
            self.engine.send("set_exposure",
                             exposure_us=int(self.cfg["manual_shutter_us"]),
                             gain=float(self.cfg["manual_gain"]))

    def _tab_quality(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "Every frame is scored from the free 640x480 luma plane, plus a\n"
            "native-resolution centre crop for the focus measure."
        ))
        form = QFormLayout()
        form.addRow("Dark if p95 below", self._spin("dark_p95_max", 1, 200, 2,
                                                    decimals=1))
        form.addRow("Blown if subject clip above", self._spin("blown_clip_frac", 0.01, 1.0,
                                                              0.01, decimals=2))
        form.addRow("Soft if sharpness below", self._spin("blur_threshold", 0.0, 200.0,
                                                          1.0, decimals=1))
        form.addRow("Detail tile threshold", self._spin("content_std_min", 0.0, 80.0,
                                                        0.5, decimals=1))
        self.cmb_reject = self._combo_str("reject_action",
                                          ["flag", "quarantine", "delete"])
        form.addRow("Rejected frames", self.cmb_reject)
        v.addLayout(form)
        v.addWidget(QLabel(
            "flag       keep everything, record the verdict in index.jsonl\n"
            "quarantine keep, but move under _rejected/ so syncs stay clean\n"
            "delete     never written to disk (the index still records it)"
        ))

        counts = QGroupBox("This session")
        cv = QVBoxLayout(counts)
        self.lbl_counts2 = QLabel("-")
        cv.addWidget(self.lbl_counts2)
        v.addWidget(counts)

        v.addWidget(QLabel("Log"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(220)
        v.addWidget(self.log)
        v.addStretch(1)
        return page

    def _combo_str(self, key: str, options) -> QComboBox:
        w = QComboBox()
        w.addItems(options)
        cur = self.cfg[key]
        if cur in options:
            w.setCurrentIndex(options.index(cur))

        def handler(i):
            self.cfg[key] = options[i]
            self._save()

        w.currentIndexChanged.connect(handler)
        return w

    def _tab_video(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        form = QFormLayout()
        form.addRow("Video mode", self._combo("video_mode", [m[3] for m in VIDEO_MODES]))
        self.spin_bitrate = QSpinBox()
        self.spin_bitrate.setRange(1, 100)
        self.spin_bitrate.setValue(12)
        self.spin_bitrate.setSuffix(" Mbps")
        form.addRow("Bitrate", self.spin_bitrate)
        v.addLayout(form)

        self.btn_record = QPushButton("Record video")
        self.btn_record.setCheckable(True)
        self.btn_record.setMinimumHeight(44)
        self.btn_record.setStyleSheet(
            "QPushButton{font-weight:600;} QPushButton:checked{background:#a03020;color:white;}"
        )
        self.btn_record.clicked.connect(self._toggle_record)
        v.addWidget(self.btn_record)

        self.lbl_video_stat = QLabel("-")
        self.lbl_video_stat.setStyleSheet(
            "background:#20242a;padding:8px;border-radius:4px;"
            "font-family:monospace;font-size:12px;"
        )
        v.addWidget(self.lbl_video_stat)

        v.addWidget(QLabel(
            "H.264 via the Pi's hardware encoder, muxed straight to MP4.\n"
            "The live preview and the exposure meter keep running while\n"
            "recording, using the lightweight metering path so they do not\n"
            "compete with the encoder.\n\n"
            "Recording reconfigures the sensor, so stills pause while it runs.\n"
            "Any capture in progress is stopped and closed cleanly first."
        ))
        v.addStretch(1)
        return page

    def _tab_timelapse(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        form = QFormLayout()
        form.addRow("Interval", self._spin("timelapse_interval_s", 0.2, 3600.0, 0.5,
                                           decimals=1, suffix=" s"))
        form.addRow("Frames (0 = unlimited)", self._spin("timelapse_count", 0, 100000, 10))
        v.addLayout(form)

        self.btn_timelapse = QPushButton("Start timelapse")
        self.btn_timelapse.setCheckable(True)
        self.btn_timelapse.setMinimumHeight(44)
        self.btn_timelapse.clicked.connect(self._toggle_timelapse)
        v.addWidget(self.btn_timelapse)

        v.addWidget(QLabel(
            "Captures at a fixed interval with auto-exposure running between\n"
            "frames, into a tlc-<timestamp> folder.\n\n"
            "To turn a finished run into a movie, use the Library face."
        ))
        v.addStretch(1)
        return page

    def _tab_birdflight(self) -> QWidget:
        """Capture Bird Flight: the auto-take gates and what a take does."""
        page = QWidget()
        v = QVBoxLayout(page)

        v.addWidget(QLabel(
            "Watches the preview for a dark subject surrounded by bright sky\n"
            "(blue or white), sharp along its boundary and well inside the\n"
            "frame. When every gate passes, it fires a burst on its own."
        ))

        self.lbl_bird = QLabel("idle")
        self.lbl_bird.setStyleSheet(
            "background:#14202a;color:#9fd0ff;padding:6px;border-radius:4px;"
            "font-family:monospace;")
        self.lbl_bird.setWordWrap(True)
        v.addWidget(self.lbl_bird)

        cap = QFormLayout()
        cap.addRow(QLabel("<b>Capture</b>"))
        cap.addRow("Burst per take", self._spin("bf_burst", 1, 40))
        cap.addRow("Cooldown after a take",
                   self._spin("bf_cooldown_s", 0.0, 60.0, 0.5, decimals=1,
                              suffix=" s"))
        cap.addRow("Stop after takes (0 = keep watching)",
                   self._spin("bf_takes", 0, 1000))
        v.addLayout(cap)

        auto = QFormLayout()
        auto.addRow(QLabel("<b>Auto-take gates</b>"))
        auto.addRow("Min boundary sharpness",
                    self._spin("bf_min_sharpness", 0.0, 100.0, 1.0, decimals=1))
        auto.addRow("Subject size min",
                    self._spin("bf_min_area_frac", 0.0, 0.2, 0.0002, decimals=4,
                               suffix=" of frame"))
        auto.addRow("Subject size max",
                    self._spin("bf_max_area_frac", 0.001, 0.5, 0.005, decimals=3,
                               suffix=" of frame"))
        auto.addRow("Subject darker than",
                    self._spin("bf_subject_luma_max", 10, 200))
        auto.addRow("Sky brighter than", self._spin("bf_sky_luma_min", 40, 250))
        auto.addRow("Min sky in frame",
                    self._spin("bf_sky_min_frac", 0.0, 1.0, 0.05, decimals=2))
        auto.addRow("Sky around subject",
                    self._spin("bf_ring_sky_frac", 0.0, 1.0, 0.05, decimals=2))
        auto.addRow("Edge margin",
                    self._spin("bf_margin_frac", 0.0, 0.4, 0.01, decimals=2))
        v.addLayout(auto)
        v.addWidget(self._check("bf_require_motion",
                                "Require motion between frames"))

        self.btn_bird = QPushButton("Start bird flight watch")
        self.btn_bird.setCheckable(True)
        self.btn_bird.setMinimumHeight(44)
        self.btn_bird.clicked.connect(self._toggle_birdflight)
        v.addWidget(self.btn_bird)

        v.addWidget(QLabel(
            "Frames land in a bird-<timestamp> session with the full quality\n"
            "pipeline; the sighting that fired the burst is logged. Changed\n"
            "gates apply from the next watch (restart the mode)."
        ))
        v.addStretch(1)
        return page

    def _tab_cascade(self) -> QWidget:
        """Group capture with background migration down the storage tiers."""
        page = QWidget()
        v = QVBoxLayout(page)

        v.addWidget(QLabel(
            "Frames are written in groups; background workers copy each finished\n"
            "group down to the next tier, verify it, then free the source. Every\n"
            "tier clears itself, so capture runs for as long as the BOTTOM tier\n"
            "has room rather than the top one."
        ))

        self.chk_cascade = self._check(
            "cascade_enabled", "Enable the storage cascade",
            on_change=lambda _: self._cascade_changed())
        v.addWidget(self.chk_cascade)
        note = QLabel("Takes effect on restart -- the capture root moves to the top tier.")
        note.setStyleSheet("color:#888;")
        v.addWidget(note)

        tiers = QGroupBox("Tiers  (capture writes to the top, data flows down)")
        tv = QVBoxLayout(tiers)
        self.tier_rows = []
        for i in range(3):
            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 2, 0, 2)
            lbl = QLabel("-")
            lbl.setStyleSheet("font-family:monospace;font-size:11px;")
            bar = QProgressBar()
            bar.setMaximumHeight(14)
            bar.setTextVisible(True)
            rl.addWidget(lbl)
            rl.addWidget(bar)
            tv.addWidget(row)
            self.tier_rows.append((lbl, bar))
        v.addWidget(tiers)

        ram = QGroupBox("RAM buffer -- how aggressive the top tier is")
        rv = QVBoxLayout(ram)
        self.sld_ram = QSlider(Qt.Horizontal)
        self.sld_ram.setRange(20, 80)
        self.sld_ram.setSingleStep(5)
        self.sld_ram.setPageStep(10)
        self.sld_ram.setTickInterval(10)
        self.sld_ram.setTickPosition(QSlider.TicksBelow)
        self.sld_ram.setValue(int(self.cfg["cascade_ram_pct"]))
        self.sld_ram.valueChanged.connect(self._ram_slider_moved)
        rv.addWidget(self.sld_ram)
        self.lbl_ram = QLabel()
        self.lbl_ram.setWordWrap(True)
        self.lbl_ram.setStyleSheet(
            "background:#20242a;padding:8px;border-radius:4px;font-family:monospace;")
        rv.addWidget(self.lbl_ram)
        btn_ram = QPushButton("Apply RAM size (remounts the tmpfs)")
        btn_ram.clicked.connect(self._apply_ram)
        rv.addWidget(btn_ram)
        v.addWidget(ram)

        form = QFormLayout()
        form.addRow("Seal a group after", self._spin("group_frames", 10, 5000, 10,
                                                     suffix=" frames"))
        form.addRow("... or after", self._spin("group_mb", 10, 4000, 10, suffix=" MB"))
        v.addLayout(form)

        self.chk_ring = self._check(
            "cascade_ring",
            "Ring mode: drop the OLDEST groups when the bottom tier fills")
        v.addWidget(self.chk_ring)
        ring_note = QLabel(
            "    Without this, capture stops when the last tier is full - nothing\n"
            "    is ever lost. With it, capture continues forever and the oldest\n"
            "    footage is deleted to make room. This is the only place birdshot\n"
            "    deletes data that does not exist anywhere else."
        )
        ring_note.setStyleSheet("color:#c88;font-size:11px;")
        v.addWidget(ring_note)

        self.lbl_cascade_predict = QLabel("-")
        self.lbl_cascade_predict.setWordWrap(True)
        self.lbl_cascade_predict.setStyleSheet(
            "background:#20242a;padding:8px;border-radius:4px;font-family:monospace;")
        v.addWidget(self.lbl_cascade_predict)

        self.btn_cascade_go = QPushButton("CAPTURE IN GROUPS  -  fastest write, self-clearing")
        self.btn_cascade_go.setCheckable(True)
        self.btn_cascade_go.setMinimumHeight(56)
        self.btn_cascade_go.setStyleSheet(
            "QPushButton{font-size:14px;font-weight:700;background:#1d6f5e;color:white;"
            "border-radius:6px;}"
            "QPushButton:checked{background:#a03020;}"
        )
        self.btn_cascade_go.clicked.connect(self._toggle_rapid)
        v.addWidget(self.btn_cascade_go)

        row2 = QHBoxLayout()
        btn_flush = QPushButton("Flush everything down now")
        btn_flush.clicked.connect(self._flush_cascade)
        row2.addWidget(btn_flush)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_cascade)
        row2.addWidget(btn_refresh)
        v.addLayout(row2)

        self.lbl_cascade_stat = QLabel("-")
        self.lbl_cascade_stat.setWordWrap(True)
        self.lbl_cascade_stat.setStyleSheet("font-family:monospace;font-size:11px;")
        v.addWidget(self.lbl_cascade_stat)

        v.addStretch(1)
        self._ram_slider_moved(int(self.cfg["cascade_ram_pct"]))
        self._refresh_cascade()
        return page

    def _ram_slider_moved(self, pct: int) -> None:
        from .. import cascade as csc

        self.cfg["cascade_ram_pct"] = int(pct)
        self._save()
        b = csc.ram_budget(pct)
        idx = int(self.cfg["capture_mode"])
        w, h, _l, _f = CAPTURE_MODES[max(0, min(idx, len(CAPTURE_MODES) - 1))]
        fps = RAPID_CONT_FPS.get((w, h), 5.0)
        mb_s = fps * (w * h * 3 / 1e6) * 0.045
        seconds = b["budget_mb"] / mb_s if mb_s > 0 else 0

        txt = ("%d%% of %.0f MB  =  %.0f MB buffer\n"
               "about %.0f s of capture at %.1f MB/s before it must migrate"
               % (pct, b["total_mb"], b["budget_mb"], seconds, mb_s))
        if b["safe"]:
            txt += "\nheadroom left for CMA + OS: %.0f MB" % b["headroom_mb"]
            self.lbl_ram.setStyleSheet(
                "background:#20242a;padding:8px;border-radius:4px;font-family:monospace;")
        else:
            txt += ("\nOVER THE SAFE CEILING of %.0f%% (%.0f MB) by %.0f MB.\n"
                    "CMA is carved from the same RAM; crowding it makes the camera\n"
                    "fail to allocate buffers -- the cma_alloc crash."
                    % (b["max_safe_pct"], b["max_safe_mb"], -b["headroom_mb"]))
            self.lbl_ram.setStyleSheet(
                "background:#3a2018;color:#ffc0a0;padding:8px;border-radius:4px;"
                "font-family:monospace;")
        self.lbl_ram.setText(txt)

    def _apply_ram(self) -> None:
        from .. import cascade as csc

        pct = int(self.cfg["cascade_ram_pct"])
        b = csc.ram_budget(pct)
        if not b["safe"]:
            reply = QMessageBox.warning(
                self, "Above the safe ceiling",
                "%d%% leaves only %.0f MB for CMA and the OS, which needs %.0f MB.\n\n"
                "The camera allocates up to 333 MB of contiguous DMA memory at full\n"
                "resolution. Crowding it is what caused the earlier crash.\n\n"
                "Apply anyway?" % (pct, b["total_mb"] - b["budget_mb"],
                                   b["cma_mb"] + csc.OS_RESERVE_MB),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        ok, msg = csc.apply_ram_pct(self.cfg)
        self._log(("RAM tier: " if ok else "RAM tier FAILED: ") + msg)
        if not ok:
            QMessageBox.warning(self, "Could not resize", msg)
        self._refresh_cascade()

    def _cascade_changed(self) -> None:
        QMessageBox.information(
            self, "Restart needed",
            "The cascade changes where capture writes, so it takes effect when\n"
            "birdshot restarts.\n\nClose and reopen it (or the AUTO desktop icon).")

    def _flush_cascade(self) -> None:
        """Force every group down to the bottom tier, clearing RAM and eMMC."""
        if self.storage.cascade is None:
            QMessageBox.information(self, "Cascade", "The cascade is not running.")
            return
        if getattr(self, "_flush_thread", None) and self._flush_thread.is_alive():
            QMessageBox.information(self, "Cascade", "A flush is already running.")
            return
        bottom = self.storage.cascade.tiers[-1].label
        self._log("flushing everything down to %s..." % bottom)

        def work():
            res = self.storage.flush_cascade(
                900.0,
                on_progress=lambda pending, moved: self.bridge.event.emit(
                    "cascade", {"event": "flushing", "pending": pending,
                                "moved": moved}))
            self.bridge.event.emit("cascade", dict(res, event="flushed"))

        self._flush_thread = threading.Thread(target=work, daemon=True,
                                              name="flush")
        self._flush_thread.start()

    def _refresh_cascade(self) -> None:
        from .. import cascade as csc

        st = self.storage.cascade_status()
        if not st:
            for lbl, bar in self.tier_rows:
                lbl.setText("-")
                bar.setValue(0)
            self.lbl_cascade_stat.setText("cascade not running")
            self.lbl_cascade_predict.setText(
                "Enable the cascade and restart to use group capture.")
            return

        for i, (lbl, bar) in enumerate(self.tier_rows):
            if i >= len(st["tiers"]):
                lbl.setText("")
                bar.setValue(0)
                continue
            t = st["tiers"][i]
            total = t["total_mb"] or 1.0
            used = max(0.0, total - t["free_mb"])
            arrow = "  |  v" if i < len(st["tiers"]) - 1 else "  |  archive"
            lbl.setText("%-14s %7.1f GB free of %5.1f GB   %d group(s) waiting%s"
                        % (t["label"], t["free_mb"] / 1024, total / 1024,
                           t["pending"], arrow))
            bar.setMaximum(100)
            bar.setValue(int(100 * used / total) if total else 0)
            bar.setFormat("%p% used")

        self.lbl_cascade_stat.setText(
            "%s\nmoved %d groups (%.1f GB), %d error(s)"
            % (st.get("busy") or st.get("last") or "idle",
               st["moved_groups"], st["moved_bytes"] / 1e9, st["errors"])
        )

        # Predict how long capture can run before the cascade backs up.
        idx = int(self.cfg["capture_mode"])
        w, h, _lab, _f = CAPTURE_MODES[max(0, min(idx, len(CAPTURE_MODES) - 1))]
        fps = RAPID_CONT_FPS.get((w, h), 5.0)
        mb_s = fps * (w * h * 3 / 1e6) * 0.045  # ~JPEG compression at q92
        bottom = st["tiers"][-1]
        bottom_speed = float(bottom.get("speed_mb_s") or 12.0)
        p = csc.predict(self.storage.cascade.tiers, mb_s, bottom_speed)
        if p.get("unbounded"):
            msg = "Capture rate %.1f MB/s. Bottom tier is remote - unbounded." % mb_s
        elif p["seconds"] == float("inf"):
            msg = "Capture rate %.1f MB/s, below the bottom tier's speed - unbounded." % mb_s
        else:
            mins = p["seconds"] / 60.0
            msg = ("Capture %.1f MB/s vs bottom tier %.0f MB/s\n"
                   "buffer above it %.1f GB  ->  about %.0f min before it backs up\n"
                   "limited by: %s"
                   % (mb_s, bottom_speed, p["buffer_mb"] / 1024, mins,
                      p.get("limited_by", "?")))
            if self.cfg["cascade_ring"]:
                msg += "\nring mode is ON - capture continues, oldest footage is dropped"
        self.lbl_cascade_predict.setText(msg)

    def _tab_storage(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        info = QLabel(
            "Capture writes to the eMMC (measured 78 MB/s). The USB stick is\n"
            "NTFS over a USB 2.0 port and sustains about 12 MB/s, so it is an\n"
            "offload target only - capturing to it directly would stall bursts."
        )
        info.setStyleSheet("color:#888;")
        v.addWidget(info)

        form = QFormLayout()
        self.ed_root = QLineEdit(self.cfg["data_root"])
        self.ed_root.editingFinished.connect(
            lambda: (self.cfg.__setitem__("data_root", self.ed_root.text()), self.cfg.save())
        )
        form.addRow("Capture root", self.ed_root)
        self.ed_usb = QLineEdit(self.cfg["usb_root"])
        self.ed_usb.editingFinished.connect(
            lambda: (self.cfg.__setitem__("usb_root", self.ed_usb.text()), self.cfg.save())
        )
        form.addRow("USB offload root", self.ed_usb)
        form.addRow("Stop below", self._spin("min_free_mb", 100, 100000, 100,
                                             suffix=" MB free"))
        v.addLayout(form)

        v.addWidget(self._check("offload_to_usb", "Offload finished sessions to USB"))
        v.addWidget(self._check("offload_continuous",
                                "Copy continuously during a run, not just at the end"))
        form2 = QFormLayout()
        form2.addRow("Copy every", self._spin("offload_interval_s", 5, 3600, 5,
                                              suffix=" s"))
        v.addLayout(form2)
        v.addWidget(self._check("offload_delete_source",
                                "Delete from eMMC after a verified copy"))

        auto = QGroupBox("Unattended start (autowrite.yes)")
        av = QVBoxLayout(auto)
        av.addWidget(QLabel(
            "Put a file named autowrite.yes in the root of a USB stick. On the\n"
            "next boot birdshot captures to that stick automatically, with no\n"
            "clicks. Remove the stick and it starts normally again.\n\n"
            "The file can be empty, or carry key=value lines:\n"
            "    mode=continuous   res=1   count=0   start=yes\n"
            "    interval=30       delete_after_copy=no   quality=92"
        ))
        self.lbl_auto_state = QLabel()
        self.lbl_auto_state.setWordWrap(True)
        self.lbl_auto_state.setStyleSheet("font-family:monospace;color:#9a9;")
        av.addWidget(self.lbl_auto_state)
        btn_scan = QPushButton("Scan for autowrite.yes now")
        btn_scan.clicked.connect(self._scan_autowrite)
        av.addWidget(btn_scan)
        v.addWidget(auto)
        self._scan_autowrite()

        row = QHBoxLayout()
        btn_now = QPushButton("Offload now")
        btn_now.clicked.connect(lambda: self.storage.offload_now())
        row.addWidget(btn_now)
        btn_open = QPushButton("Open capture folder")
        btn_open.clicked.connect(self._open_folder)
        row.addWidget(btn_open)
        v.addLayout(row)

        self.lbl_offload_detail = QLabel("-")
        self.lbl_offload_detail.setWordWrap(True)
        v.addWidget(self.lbl_offload_detail)
        v.addStretch(1)
        return page

    def _tab_health(self) -> QWidget:
        """The doctor checklist, in the window that needs it configured.

        On nine distribution channels the first question is always "what does
        this install actually have" -- the same rows birdshot-cli doctor
        prints, so the GUI and the CLI can never tell different stories.
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "Platform, modules, binaries, cameras, storage and config --\n"
            "the same checklist as birdshot-cli doctor."
        ))
        self.txt_doctor = QTextEdit()
        self.txt_doctor.setReadOnly(True)
        self.txt_doctor.setMaximumHeight(260)
        self.txt_doctor.setStyleSheet("font-family:monospace;font-size:11px;")
        self.txt_doctor.setPlainText("checking...")
        v.addWidget(self.txt_doctor)
        row = QHBoxLayout()
        btn = QPushButton("Run doctor again")
        btn.clicked.connect(self._run_doctor)
        row.addWidget(btn)
        self.lbl_doctor_stamp = QLabel("running at startup...")
        self.lbl_doctor_stamp.setStyleSheet("color:#888;")
        row.addWidget(self.lbl_doctor_stamp)
        row.addStretch(1)
        v.addLayout(row)
        v.addStretch(1)
        return page

    def _tab_identity(self) -> QWidget:
        """Who took the picture, with what glass -- the EXIF the manual
        C-mount lens cannot report. Consumed at encode/assemble time from
        index.jsonl; nothing here touches capture speed."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(self._check("exif_enabled",
                                "Write EXIF when encoding or assembling"))
        form = QFormLayout()
        form.addRow("Camera make", self._line("exif_make"))
        form.addRow("Camera model", self._line("exif_model"))
        form.addRow("Lens", self._line("exif_lens"))
        form.addRow("Focal length (0 = not recorded)",
                    self._spin("exif_focal_mm", 0.0, 2000.0, 1.0, decimals=1,
                               suffix=" mm"))
        form.addRow("F-number (0 = not recorded)",
                    self._spin("exif_fnumber", 0.0, 64.0, 0.1, decimals=1))
        form.addRow("Artist", self._line("exif_artist"))
        form.addRow("Copyright", self._line("exif_copyright"))
        v.addLayout(form)
        v.addStretch(1)
        return page

    def _line(self, key: str) -> QLineEdit:
        w = QLineEdit(str(self.cfg[key] or ""))

        def done():
            self.cfg[key] = w.text()
            self._save()

        w.editingFinished.connect(done)
        return w

    def _run_doctor(self) -> None:
        """Collect the checklist off the GUI thread (it probes cameras)."""
        if getattr(self, "_doctor_thread", None) and self._doctor_thread.is_alive():
            return

        def work():
            from birdshot import doctor
            try:
                rows = doctor.collect(self.cfg)
            except Exception as exc:  # noqa: BLE001 -- report, never crash
                rows = [(doctor.FAIL, "doctor", repr(exc))]
            self.bridge.event.emit("doctor", {"rows": rows})

        self._doctor_thread = threading.Thread(target=work, daemon=True,
                                               name="doctor")
        self._doctor_thread.start()

    def _on_doctor(self, payload: Dict[str, Any]) -> None:
        rows = payload.get("rows") or []
        self.txt_doctor.setPlainText(
            "\n".join("%-4s  %-14s %s" % tuple(r) for r in rows))
        self.lbl_doctor_stamp.setText("checked %s" % time.strftime("%H:%M:%S"))
        failed = [n for s, n, _ in rows if s == "FAIL"]
        warned = [n for s, n, _ in rows if s == "warn"]
        if failed:
            text, color = "doctor: FAIL - %s" % ", ".join(failed), "#ff6a44"
        elif warned:
            text, color = "doctor: %d warning(s)" % len(warned), "#e0a828"
        else:
            text, color = "doctor: ok", "#5fd07a"
        self.btn_doctor.setText(text)
        self.btn_doctor.setStyleSheet(
            "QPushButton{border:none;background:transparent;color:%s;"
            "font-family:monospace;font-size:11px;}"
            "QPushButton:hover{text-decoration:underline;}" % color)
        if "Install health - doctor" in self._acc:
            self._acc["Install health - doctor"].set_summary(text)

    def _open_health(self) -> None:
        self.set_face("bench")
        self.select_tab("Machine")
        acc = self._acc.get("Install health - doctor")
        if acc is not None:
            acc.set_expanded(True)

    # ==================================================================
    # actions
    # ==================================================================
    def _toggle_collect(self) -> None:
        if self.engine.state == "burst":
            self.engine.send("stop")
        else:
            self._counts = {"ok": 0, "dark": 0, "blown": 0, "empty": 0}
            self._session_frames = 0
            self._session_bytes = 0
            self.engine.send("burst", count=int(self.cfg["burst_count"]))

    def _toggle_timelapse(self) -> None:
        if self.engine.state == "timelapse":
            self.engine.send("stop")
        else:
            self.engine.send("timelapse", count=int(self.cfg["timelapse_count"]))

    def _toggle_birdflight(self) -> None:
        if self.engine.state == "birdflight":
            self.engine.send("stop")
        else:
            self.lbl_bird.setText("watching the sky...")
            self.engine.send("birdflight", takes=int(self.cfg["bf_takes"]))

    def _on_bird(self, payload: Dict[str, Any]) -> None:
        s = payload.get("sighting") or {}
        if s.get("bbox"):
            self.preview.set_bird(
                s["bbox"], "sharp %.1f · %.2f%%"
                % (s.get("sharpness", 0.0), 100.0 * s.get("area_frac", 0.0)),
                take=payload.get("phase") == "take")
        if hasattr(self, "face_field"):
            self.face_field.on_bird(payload)
        if payload.get("phase") == "take":
            msg = ("TAKE #%d  sharp %.1f, %.2f%% of frame"
                   % (payload.get("take_n", 0), s.get("sharpness", 0.0),
                      100.0 * s.get("area_frac", 0.0)))
            self.lbl_bird.setText(msg)
            self._log("bird flight: %s" % msg)
            self.status.showMessage("bird! burst fired", 3000)
        else:
            why = ", ".join(s.get("reasons") or []) or "judging..."
            self.lbl_bird.setText("subject seen -- holding: %s" % why)

    def _toggle_record(self) -> None:
        if self.engine.state == "video":
            self.engine.send("stop")
        else:
            self.engine.send("video", bitrate=self.spin_bitrate.value() * 1_000_000)

    def _scan_autowrite(self) -> None:
        from .. import autostart

        found = autostart.detect()
        if not found:
            self.lbl_auto_state.setText(
                "No %s found on any mounted volume." % autostart.MARKER)
            return
        opts = found["options"] or {}
        text = "Found on %s\n  options: %s" % (
            found["mount"], ", ".join("%s=%s" % kv for kv in sorted(opts.items()))
            or "(defaults)")
        for w in found.get("warnings", []):
            text += "\n  WARNING: %s" % w
        self.lbl_auto_state.setText(text)

    def _open_folder(self) -> None:
        self.open_path(self.cfg["data_root"])

    def _offer_calibration(self) -> None:
        reply = QMessageBox.question(
            self, "Calibrate now?",
            "No calibration is stored for this camera.\n\n"
            "The wizard measures open sky and the treeline so metering knows how\n"
            "much dynamic range it has to straddle. It takes about a minute.\n\n"
            "Run it now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._open_calibration()

    def _open_focus_monitor(self) -> None:
        if self._focus is not None and self._focus.isVisible():
            self._focus.raise_()
            self._focus.activateWindow()
            return
        self._focus = FocusMonitor(self.engine, self)
        # Park it against the right edge so it does not cover the main preview.
        screen = self.screen().availableGeometry() if hasattr(self, "screen") else None
        if screen is not None:
            self._focus.move(screen.right() - self._focus.width() - 20, screen.top() + 40)
        self._focus.show()
        self._log("focus monitor open (full-frame copies enabled while it is up)")

    def _open_calibration(self) -> None:
        self._calib = CalibrationDialog(self, self.cfg, self.engine)
        self._calib.finished.connect(self._refresh_calibration_label)
        self._calib.show()

    def _refresh_calibration_label(self) -> None:
        cal = self.cfg["calibration"] or {}
        if not cal.get("done"):
            self.lbl_cal.setText("Not calibrated.")
            return
        dr = cal.get("dynamic_range_ev")
        when = cal.get("timestamp")
        self.lbl_cal.setText(
            "Calibrated %s - scene range %.1f EV"
            % (time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "?",
               dr or 0.0)
        )

    def _on_cascade(self, payload: Dict[str, Any]) -> None:
        ev = payload.get("event")
        if ev == "migrated":
            self._log("moved %s -> %s (%.0f MB at %.0f MB/s)"
                      % (payload["group"], payload["to"],
                         payload["bytes"] / 1e6, payload.get("mbps", 0)))
        elif ev == "failed":
            self._log("CASCADE FAILED %s -> %s: %s"
                      % (payload["group"], payload["to"], payload.get("error")))
        elif ev == "flushing":
            self.lbl_cascade_stat.setText(
                "flushing: %d group(s) left in the upper tiers, %d moved"
                % (payload.get("pending", 0), payload.get("moved", 0)))
        elif ev == "flushed":
            msg = ("flush %s: %d group(s) moved to %s, %d adopted, %d still pending"
                   % ("complete" if payload.get("ok") else "INCOMPLETE",
                      payload.get("moved", 0), payload.get("bottom"),
                      payload.get("adopted", 0), payload.get("pending", 0)))
            self._log(msg)
            self.lbl_cascade_stat.setText(msg)
            self._refresh_cascade()
        elif ev == "evicted":
            self._log("ring mode dropped oldest group %s (%.0f MB)"
                      % (payload["group"], payload.get("bytes", 0) / 1e6))

    def _on_assembled(self, result: Dict[str, Any]) -> None:
        self.prog_encode.setVisible(False)
        self.btn_encode.setEnabled(True)
        self.btn_encode_cancel.setEnabled(False)
        if result.get("ok"):
            # Assembled movies cascade too.
            if self.storage.cascade is not None and result.get("output"):
                self.storage.cascade_media(result["output"], "movie")
            msg = ("%d frames -> %s\n%.1f s of video, %.0f MB, encoded in %ss"
                   % (result["frames"], result["output"], result["seconds"],
                      result.get("bytes", 0) / 1e6, result.get("elapsed")))
            ex = result.get("exif")
            if ex:
                msg += "\nEXIF: %d tagged, %d failed, %d missing (%ss)" % (
                    ex.get("tagged", 0), ex.get("failed", 0),
                    ex.get("missing", 0), ex.get("elapsed"))
            self.lbl_encode_status.setText(msg)
            self._log("encoded " + msg.replace("\n", "  "))
        else:
            self.lbl_encode_status.setText("failed: %s" % result.get("error"))
            self._log("encode failed: %s" % result.get("error"))
            if result.get("error") != "cancelled":
                QMessageBox.warning(self, "Encode failed", str(result.get("error")))

    # ==================================================================
    def _log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        if hasattr(self, "log"):
            self.log.append("%s  %s" % (stamp, msg))
        self.status.showMessage(msg, 6000)

    def _refresh_summaries(self) -> None:
        idx = max(0, min(int(self.cfg["capture_mode"]), len(CAPTURE_MODES) - 1))
        w, h, _lab, _fps = CAPTURE_MODES[idx]
        acc = self._acc

        def put(key, text):
            for title, a in acc.items():
                if title.startswith(key):
                    a.set_summary(text)

        put("Stills", "%dx%d, quality gates on, %s" % (
            w, h, "auto exposure" if self.cfg["auto_exposure"] else "manual"))
        put("Rapid", "%dx%d, %s, %s frame limit" % (
            w, h, self.cfg["rapid_mode"],
            self.cfg["rapid_count"] or "no"))
        put("Timelapse", "every %.1f s, %s frames" % (
            self.cfg["timelapse_interval_s"], self.cfg["timelapse_count"] or "unlimited"))
        put("Video", VIDEO_MODES[max(0, min(int(self.cfg["video_mode"]),
                                            len(VIDEO_MODES) - 1))][3])
        put("Exposure", "target %.0f, %s, tone: %s" % (
            self.cfg["target_luma"],
            "auto" if self.cfg["auto_exposure"] else "manual",
            self.cfg["tone_curve"]))
        put("Focus", "map %s, peaking %s, outdoor %s" % (
            "on" if self.preview.show_focus_map else "off",
            "on" if self.preview.show_peaking else "off",
            "on" if self.preview.outdoor else "off"))
        put("Quality", "dark<%.0f, blown>%.0f%%, blur<%.1f, rejects: %s" % (
            self.cfg["dark_p95_max"], self.cfg["blown_clip_frac"] * 100,
            self.cfg["blur_threshold"], self.cfg["reject_action"]))
        st = self.storage.cascade_status()
        if st:
            put("Cascade", "%s  |  %d%% RAM" % (
                " -> ".join(t["label"] for t in st["tiers"]),
                self.cfg["cascade_ram_pct"]))
        else:
            put("Cascade", "off - capture writes straight to %s"
                % self.cfg["data_root"])
        put("Paths", "%s, offload %s" % (
            self.cfg["data_root"],
            "on" if self.cfg["offload_to_usb"] else "off"))
        put("Encode", "%d fps, %s, EXIF %s" % (
            self.cfg["encode_fps"],
            "%d px wide" % self.cfg["encode_width"] if self.cfg["encode_width"]
            else "native",
            "on" if self.cfg["exif_enabled"] else "off"))
        put("Identity", "EXIF %s%s" % (
            "on" if self.cfg["exif_enabled"] else "off",
            (" - %s" % self.cfg["exif_artist"]) if self.cfg["exif_artist"]
            else ", no artist set"))

    def _refresh_status(self) -> None:
        self._check_space()
        self._refresh_go_button()
        self._refresh_summaries()
        free = self.storage.free_mb()
        self.lbl_free.setText("%.1f GB free" % (free / 1024.0))
        st = self.storage.offload_status()
        behind = st.get("behind")
        if st.get("current"):
            text = "offload: copying %s" % os.path.basename(st["current"])
        elif st.get("pending"):
            text = "offload: %d queued" % st["pending"]
        elif st.get("last"):
            text = "offload: %s" % st["last"]
        else:
            text = "offload idle" if st.get("enabled") else "offload off"
        if behind:
            # The stick manages ~12 MB/s; a fast capture can out-write it.
            text += "   |   %d frames behind" % behind
            self.lbl_offload.setStyleSheet(
                "color:#e0a828;font-weight:600;" if behind > 200 else "")
        else:
            self.lbl_offload.setStyleSheet("")
        self.lbl_offload.setText(text)
        self.lbl_offload_detail.setText(str(st.get("last") or "-"))
        if hasattr(self, "tier_rows") and self.storage.cascade is not None:
            self._refresh_cascade()
        if hasattr(self, "face_field"):
            sess = self.storage.session
            cs = self.storage.cascade_status()
            self.face_field.refresh_status(
                "session %s · %d frames" % (os.path.basename(sess.path),
                                            self._session_frames)
                if sess else "no session",
                "free %.1f GB · %s" % (free / 1024.0, text),
                cs["tiers"] if cs else None)
        if hasattr(self, "lbl_counts2"):
            self.lbl_counts2.setText(
                "ok %d | dark %d | blown %d | empty %d  (%d frames, %.0f MB)"
                % (self._counts.get("ok", 0), self._counts.get("dark", 0),
                   self._counts.get("blown", 0), self._counts.get("empty", 0),
                   self._session_frames, self._session_bytes / 1e6)
            )

    def resizeEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "overlay", None) is not None:
            self.overlay.setGeometry(self.rect())
        super().resizeEvent(event)

    def _dismiss_overlay(self) -> None:
        # Only dismissable once there is actually room again -- otherwise it
        # would just hide the fact that capture cannot run.
        if self.storage.space_state() != "full":
            self.overlay.hide()
            self._space_blocked = False

    def _check_space(self) -> None:
        """Raise an unmissable overlay when every tier is full."""
        try:
            state = self.storage.space_state()
        except Exception:  # noqa: BLE001
            return
        if state != "full":
            if self._space_blocked:
                self.overlay.hide()
                self._space_blocked = False
            return
        if self._space_blocked:
            return
        self._space_blocked = True

        lines = ["Capture has stopped. Every storage tier is full.", ""]
        st = self.storage.cascade_status()
        if st:
            for t in st["tiers"]:
                lines.append("  %-28s %7.1f GB free" % (t["label"], t["free_mb"] / 1024))
            lines += ["", "What will free space:",
                      "  - Flush to the archive:  Machine > Cascade -> Flush everything down",
                      "  - Swap or empty the USB stick",
                      "  - Turn on ring mode to drop the oldest footage automatically",
                      "  - Delete finished sessions from the archive"]
        else:
            lines.append("  %s: %.1f GB free" % (self.cfg["data_root"],
                                                 self.storage.free_mb() / 1024))
            lines += ["", "Free space on the capture drive, or enable the cascade",
                      "so frames migrate to the USB stick automatically."]
        self.overlay.show_message("OUT OF SPACE", "\n".join(lines))
        self._log("OUT OF SPACE - capture stopped")

    def closeEvent(self, event) -> None:  # noqa: N802
        # A RAM burst may still have hundreds of frames to encode, so give the
        # engine real time to finish rather than the usual couple of seconds.
        self.status.showMessage("finishing capture and copying to USB...")
        try:
            self.engine.send("stop")
            self.engine.shutdown()
            self.engine.join(timeout=120)
        except Exception:
            pass
        try:
            self.storage.stop(drain_timeout=180.0)
            self.cfg.save()
        except Exception:
            pass
        super().closeEvent(event)
