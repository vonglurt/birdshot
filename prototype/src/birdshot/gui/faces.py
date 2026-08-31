# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""One window, four faces.

birdshot deploys to audiences that want very different first screens: the
copal desktop ships it as the OS's default camera app, the Pi runs it as a
field instrument, the Mac uses it as a darkroom, and the bench is where every
one of its ~90 settings lives. Rather than forking GUIs, the one window grows
four *faces* over the same engine and the same settings.json:

    camera    a plain camera app: preview, shutter, modes, camera picker
    field     the instrument outdoors: huge targets, the Bird Flight gate
              ladder live on screen, storage headroom always visible
    bench     the dense tuning window (main_window.py's classic layout)
    library   sessions, verdicts, bird takes with their triggers, encode

``ui_face`` in the config picks the boot face; ``auto`` resolves per install
(see :func:`resolve_face`). Switching at runtime is one click in the title
bar and deliberately does NOT persist -- the boot face is deploy policy, not
a session preference.

The live PreviewWidget is a single instance owned by the main window and
*reparented* into whichever face is showing (the same trick the fullscreen
view has always used), so no face pays for a second per-frame pipeline.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

from .widgets import Accordion, ModeTuner

FACES = ("camera", "field", "bench", "library")

# The analysis geometry every backend normalises to (see backends/opencv.py);
# Bird Flight bboxes and centroids are in these coordinates.
ANALYSIS_W, ANALYSIS_H = 640, 480

PASS_COLOR = "#7fe3a2"
FAIL_COLOR = "#ff6a44"
NA_COLOR = "#6a7480"


def resolve_face(cfg) -> str:
    """The face the GUI should boot into.

    An explicit ``ui_face`` wins. ``auto`` reads what this install is *for*,
    most specific first -- and the Pi check comes before the checkout check,
    because the deployed instrument IS a git checkout (sync.sh deploys
    checkouts):

        picamera2 present        -> field    (the instrument)
        a git checkout           -> bench    (a developer's tree)
        Alpine / musl            -> camera   (the copal desktop's camera app)
        macOS install            -> library  (the darkroom)
        anything else            -> camera
    """
    want = str(cfg.get("ui_face") or "auto").strip().lower()
    if want in FACES:
        return want

    from birdshot import backends
    if backends.resolve_choice(cfg)[0] == "picamera2":
        return "field"

    import birdshot
    pkg = os.path.dirname(os.path.abspath(birdshot.__file__))
    repo = os.path.dirname(os.path.dirname(pkg))
    if os.path.isdir(os.path.join(repo, ".git")):
        return "bench"

    try:
        from birdshot import doctor
        if "musl" in (doctor._libc() or "") or \
                "alpine" in (doctor._os_release() or "").lower():
            return "camera"
    except Exception:  # noqa: BLE001 -- face resolution must never crash boot
        pass
    if sys.platform == "darwin":
        return "library"
    return "camera"


class FaceBar(QWidget):
    """The title-bar strip: wordmark left, the four-face switcher right."""

    face_picked = pyqtSignal(str)

    def __init__(self, version: str = "", parent=None):
        super().__init__(parent)
        self.setFixedHeight(44)
        self.setStyleSheet("background:#20252b;border-bottom:1px solid #333a44;")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 10, 0)
        row.setSpacing(12)

        word = QLabel("birdshot")
        word.setStyleSheet("font-size:15px;font-weight:800;color:#cfe3ef;"
                           "border:none;")
        row.addWidget(word)
        if version:
            v = QLabel(version)
            v.setStyleSheet("font-size:11px;color:#7a8791;border:none;")
            row.addWidget(v)
        row.addStretch(1)

        self._buttons: Dict[str, QToolButton] = {}
        seg = QWidget()
        seg.setStyleSheet("background:transparent;border:none;")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(0, 6, 0, 6)
        sl.setSpacing(0)
        for name in FACES:
            b = QToolButton()
            b.setText(name.capitalize())
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(30)
            b.clicked.connect(lambda _c=False, n=name: self.face_picked.emit(n))
            self._buttons[name] = b
            sl.addWidget(b)
        row.addWidget(seg)
        self.set_active("bench")

    def set_active(self, name: str) -> None:
        names = list(FACES)
        for i, (n, b) in enumerate(self._buttons.items()):
            on = n == name
            b.setChecked(on)
            radius = ("border-top-left-radius:5px;border-bottom-left-radius:5px;"
                      if i == 0 else
                      "border-top-right-radius:5px;border-bottom-right-radius:5px;"
                      if i == len(names) - 1 else "")
            b.setStyleSheet(
                "QToolButton{padding:5px 16px;font-size:12px;border:1px solid #39414c;"
                + radius
                + ("background:#2f6f8f;color:#ffffff;font-weight:800;" if on
                   else "background:#232830;color:#93a3ad;font-weight:600;")
                + "}"
                "QToolButton:hover{background:%s;color:#eaf5fb;}"
                % ("#38809f" if on else "#2d3540")
            )


# ======================================================================
# Camera -- the plain camera app the copal desktop boots into
# ======================================================================
class CameraFace(QWidget):
    """Preview, shutter, mode strip, camera picker. Nothing that needs the
    operating guide -- every dial lives in Bench, one click away."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setStyleSheet("background:#1b1f24;")
        self._last_thumb = 0.0

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.preview_slot = QVBoxLayout()
        self.preview_slot.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self.preview_slot, 1)

        bar = QWidget()
        bar.setFixedHeight(112)
        bar.setStyleSheet("background:#20252b;border-top:1px solid #333a44;")
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 10, 18, 10)
        h.setSpacing(18)

        # Last shot -> the Library face.
        self.btn_thumb = QPushButton()
        self.btn_thumb.setFixedSize(92, 69)
        self.btn_thumb.setCursor(Qt.PointingHandCursor)
        self.btn_thumb.setToolTip("Open the Library")
        self.btn_thumb.setStyleSheet(
            "QPushButton{background:#0d1013;border:1px solid #39414c;"
            "border-radius:4px;color:#7a8791;font-size:10px;}")
        self.btn_thumb.setText("no shots\nyet")
        self.btn_thumb.clicked.connect(lambda: self.win.set_face("library"))
        h.addWidget(self.btn_thumb)

        h.addStretch(1)
        self.tuner = ModeTuner(win.MODES, int(win.cfg.get("shoot_mode", 0)))
        self.tuner.changed.connect(win.tuner.set_index)
        h.addWidget(self.tuner, 4)

        self.btn_shutter = QPushButton()
        self.btn_shutter.setFixedSize(78, 78)
        self.btn_shutter.setCursor(Qt.PointingHandCursor)
        self.btn_shutter.clicked.connect(self._shutter)
        self._style_shutter(running=False)
        h.addWidget(self.btn_shutter)
        h.addStretch(1)

        self.cmb_camera = QComboBox()
        self.cmb_camera.setMinimumWidth(190)
        self.cmb_camera.setToolTip("Which camera drives capture.")
        h.addWidget(self.cmb_camera)
        v.addWidget(bar)

    def _style_shutter(self, running: bool) -> None:
        color = "#a03020" if running else "#e8edf2"
        self.btn_shutter.setStyleSheet(
            "QPushButton{border-radius:39px;border:3px solid %s;background:%s;}"
            "QPushButton:pressed{background:#b9c2ca;}" % ("#e8edf2", color))
        self.btn_shutter.setToolTip("Stop" if running else "Take")

    def _shutter(self) -> None:
        """A camera app's shutter: one photo in Stills, start/stop elsewhere.

        Backends without ``single`` get a burst of one -- a session folder per
        press, which is what the engine honestly offers there.
        """
        win = self.win
        if win.engine.state in ("burst", "rapid", "drain", "timelapse",
                                "video", "birdflight"):
            win.engine.send("stop")
            return
        key = win.MODES[max(0, min(int(win.cfg.get("shoot_mode", 0)),
                                   len(win.MODES) - 1))][1]
        if key == "burst":
            if "single" in win.caps():
                win.engine.send("single", save=True)
            else:
                win.engine.send("burst", count=1)
        else:
            win.go_clicked()

    # -- hooks the window calls ----------------------------------------
    def update_go(self, running: bool, state: str, label: str) -> None:
        self._style_shutter(running)

    def sync_mode(self, idx: int) -> None:
        self.tuner.set_index(idx)

    def on_frame(self, payload: Dict[str, Any]) -> None:
        path = payload.get("path")
        now = time.monotonic()
        if not path or now - self._last_thumb < 0.6:
            return
        self._last_thumb = now
        pm = QPixmap(path)
        if pm.isNull():
            return
        self.btn_thumb.setText("")
        self.btn_thumb.setIcon(_icon_from(pm, 88, 65))
        self.btn_thumb.setIconSize(self.btn_thumb.size() * 0.94)


def _icon_from(pm: QPixmap, w: int, h: int):
    from PyQt5.QtGui import QIcon
    return QIcon(pm.scaled(w, h, Qt.KeepAspectRatioByExpanding,
                           Qt.SmoothTransformation))


# ======================================================================
# Field -- the instrument outdoors
# ======================================================================
# The Bird Flight gates, in judging order. Each row: (title, reason string
# the detector emits when it fails, value key in the sighting dict).
GATES = (
    ("motion", "no motion", "motion_frac"),
    ("sky in frame", "frame not sky enough", "sky_frac"),
    ("subject found", "no subject", None),
    ("subject size", "subject too small", "area_frac"),
    ("sky ring", "not against sky", "ring_sky_frac"),
    ("inside margin", "subject too near the edge", None),
    ("boundary sharp", "boundary not sharp enough", "sharpness"),
)


class GateLadder(QWidget):
    """Every auto-take gate with its live value against its threshold.

    This is the mode's *why*: when Bird Flight holds fire, the ladder shows
    exactly which rung it is failing on, with the number, so tuning gates is
    reading rather than guessing.
    """

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setStyleSheet(
            "QWidget{background:#14202a;border:none;}"
            "QLabel{border:none;background:transparent;}")
        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)

        head = QLabel("AUTO-TAKE GATES")
        head.setStyleSheet("color:#9fd0ff;font-weight:800;font-size:10px;"
                           "letter-spacing:1px;padding:7px 10px;")
        v.addWidget(head)

        self._rows = []
        for title, _reason, _kv in GATES:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 4, 10, 4)
            name = QLabel(title)
            name.setStyleSheet("color:#cfe3ef;font-weight:600;font-size:12px;")
            val = QLabel("-")
            val.setStyleSheet("color:#9fd0ff;font-family:monospace;font-size:11px;")
            thr = QLabel("")
            thr.setStyleSheet("color:#7a8791;font-family:monospace;font-size:10px;")
            thr.setMinimumWidth(74)
            thr.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            tick = QLabel("-")
            tick.setFixedWidth(16)
            tick.setAlignment(Qt.AlignCenter)
            rl.addWidget(name, 1)
            rl.addWidget(val)
            rl.addWidget(thr)
            rl.addWidget(tick)
            v.addWidget(row)
            self._rows.append((val, thr, tick))

        self.footer = QLabel("mode not running")
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet(
            "color:#93a3ad;font-size:12px;font-weight:700;padding:8px 10px;"
            "background:#101820;")
        v.addWidget(self.footer)
        self._take_until = 0.0
        self.set_idle("mode not running")

    # ------------------------------------------------------------------
    def _thresholds(self):
        c = self.cfg
        return (
            "≥ %.2f%%" % (float(c["bf_motion_min"]) * 100),
            "≥ %.0f%%" % (float(c["bf_sky_min_frac"]) * 100),
            "≤ luma %d" % int(c["bf_subject_luma_max"]),
            "%.2f–%.1f%%" % (float(c["bf_min_area_frac"]) * 100,
                                  float(c["bf_max_area_frac"]) * 100),
            "≥ %.0f%%" % (float(c["bf_ring_sky_frac"]) * 100),
            "≥ %.0f%%" % (float(c["bf_margin_frac"]) * 100),
            "≥ %.1f" % float(c["bf_min_sharpness"]),
        )

    def set_idle(self, text: str) -> None:
        for val, thr, tick in self._rows:
            val.setText("-")
            tick.setText("–")
            tick.setStyleSheet("color:%s;font-weight:800;" % NA_COLOR)
        for (v_, thr, t_), txt in zip(self._rows, self._thresholds()):
            thr.setText(txt)
        self.footer.setText(text)
        self.footer.setStyleSheet(
            "color:#93a3ad;font-size:12px;font-weight:700;padding:8px 10px;"
            "background:#101820;")

    def update_sighting(self, s: Dict[str, Any], take_n: Optional[int]) -> None:
        reasons = s.get("reasons") or []
        motion_off = not bool(self.cfg["bf_require_motion"])
        no_subject = "no subject" in reasons

        # Live values, where the sighting carries a number.
        c = s.get("centroid")
        margin_val = "-"
        if c:
            edge = min(c[0] / ANALYSIS_W, 1.0 - c[0] / ANALYSIS_W,
                       c[1] / ANALYSIS_H, 1.0 - c[1] / ANALYSIS_H)
            margin_val = "%.0f%%" % (edge * 100)
        values = (
            "off" if motion_off else "%.2f%%" % (s.get("motion_frac", 0) * 100),
            "%.0f%%" % (s.get("sky_frac", 0) * 100),
            "no" if no_subject else "yes",
            "-" if no_subject else "%.2f%%" % (s.get("area_frac", 0) * 100),
            "-" if no_subject else "%.0f%%" % (s.get("ring_sky_frac", 0) * 100),
            margin_val,
            "-" if no_subject else "%.1f" % s.get("sharpness", 0.0),
        )
        for i, ((val, thr, tick), (title, reason, _kv), txt) in enumerate(
                zip(self._rows, GATES, self._thresholds())):
            val.setText(values[i])
            thr.setText(txt)
            if title == "motion" and motion_off:
                state = None
            elif no_subject and i >= 2:
                state = (False if title == "subject found" else None)
            elif title == "subject size":
                state = not any(r.startswith("subject too small")
                                or r.startswith("subject too large")
                                for r in reasons)
            else:
                state = reason not in reasons
            if state is None:
                tick.setText("–")
                tick.setStyleSheet("color:%s;font-weight:800;" % NA_COLOR)
            else:
                tick.setText("✓" if state else "✗")
                tick.setStyleSheet("color:%s;font-weight:800;"
                                   % (PASS_COLOR if state else FAIL_COLOR))

        now = time.monotonic()
        if take_n is not None:
            self._take_until = now + 3.0
            self.footer.setText(
                "ALL GATES PASS — TAKE #%d · burst %d · cooldown %.1f s"
                % (take_n, int(self.cfg["bf_burst"]),
                   float(self.cfg["bf_cooldown_s"])))
            self.footer.setStyleSheet(
                "color:#7fe3a2;font-size:12px;font-weight:800;padding:8px 10px;"
                "background:#173a24;")
        elif now >= self._take_until:
            why = ", ".join(reasons) or "judging..."
            self.footer.setText("holding: %s" % why)
            self.footer.setStyleSheet(
                "color:#e0a828;font-size:12px;font-weight:700;padding:8px 10px;"
                "background:#101820;")


class FieldFace(QWidget):
    """Big targets, glanceable state, the gate ladder beside the image."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setStyleSheet("background:#14181d;")

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # -- top strip: state, mode, camera, storage headroom ----------
        top = QWidget()
        top.setFixedHeight(52)
        top.setStyleSheet("background:#20252b;border-bottom:1px solid #333a44;")
        th = QHBoxLayout(top)
        th.setContentsMargins(12, 8, 12, 8)
        th.setSpacing(12)
        self.lbl_state = QLabel("IDLE")
        self._style_state("idle")
        th.addWidget(self.lbl_state)
        self.lbl_mode = QLabel("")
        self.lbl_mode.setStyleSheet("font-size:13px;font-weight:800;color:#cfe3ef;"
                                    "border:none;")
        th.addWidget(self.lbl_mode)
        self.lbl_camera = QLabel("")
        self.lbl_camera.setStyleSheet("font-size:11px;color:#93a3ad;border:none;")
        th.addWidget(self.lbl_camera)
        th.addStretch(1)
        self._tier_bars = []
        for _ in range(3):
            cell = QWidget()
            cell.setStyleSheet("background:transparent;border:none;")
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(2)
            lab = QLabel("")
            lab.setStyleSheet("font-family:monospace;font-size:9px;color:#7a8791;"
                              "border:none;")
            bar = QProgressBar()
            bar.setFixedSize(92, 6)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                "QProgressBar{background:#2a313a;border:none;border-radius:2px;}"
                "QProgressBar::chunk{background:#4da3cc;border-radius:2px;}")
            cl.addWidget(lab)
            cl.addWidget(bar)
            th.addWidget(cell)
            self._tier_bars.append((cell, lab, bar))
        v.addWidget(top)

        # -- main row: preview + rail -----------------------------------
        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        self.preview_slot = QVBoxLayout()
        self.preview_slot.setContentsMargins(0, 0, 0, 0)
        mid.addLayout(self.preview_slot, 1)

        rail = QWidget()
        rail.setFixedWidth(380)
        rail.setStyleSheet("background:#1b1f24;border-left:1px solid #333a44;")
        rv = QVBoxLayout(rail)
        rv.setContentsMargins(12, 12, 12, 12)
        rv.setSpacing(12)

        self.ladder = GateLadder(win.cfg)
        rv.addWidget(self.ladder)

        self.lbl_takes = QLabel("takes 0")
        self.lbl_takes.setStyleSheet("font-family:monospace;font-size:11px;"
                                     "color:#93a3ad;border:none;")
        rv.addWidget(self.lbl_takes)

        # Same modes, same order, shorter names -- 380 px must hold all five.
        compact = [(label.replace("Timelapse", "Lapse")
                    .replace("Bird Flight", "Bird"), key, hint)
                   for label, key, hint in win.MODES]
        self.tuner = ModeTuner(compact, int(win.cfg.get("shoot_mode", 0)),
                               font_px=11)
        self.tuner.changed.connect(win.tuner.set_index)
        rv.addWidget(self.tuner)

        self.btn_go = QPushButton()
        self.btn_go.setCheckable(True)
        self.btn_go.setMinimumHeight(72)
        self.btn_go.setCursor(Qt.PointingHandCursor)
        self.btn_go.clicked.connect(win.go_clicked)
        rv.addWidget(self.btn_go)

        orow = QHBoxLayout()
        self.btn_outdoor = QToolButton()
        self.btn_outdoor.setText("OUTDOOR MODE")
        self.btn_outdoor.setCheckable(True)
        self.btn_outdoor.setCursor(Qt.PointingHandCursor)
        self.btn_outdoor.setMinimumHeight(46)
        self.btn_outdoor.setSizePolicy(self.tuner.sizePolicy())
        self._style_outdoor(False)
        self.btn_outdoor.toggled.connect(self._outdoor_toggled)
        orow.addWidget(self.btn_outdoor, 1)
        self._style_buttons = []
        for i, name in enumerate(("boost", "edges")):
            b = QToolButton()
            b.setText(name)
            b.setCheckable(True)
            b.setMinimumHeight(46)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, n=i: self._pick_style(n))
            orow.addWidget(b)
            self._style_buttons.append(b)
        self._restyle_style_buttons(0)
        rv.addLayout(orow)

        rv.addStretch(1)
        hint = QLabel("changed gates apply on the next watch — tune them in Bench")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-family:monospace;font-size:10px;color:#7a8791;"
                           "border:none;")
        rv.addWidget(hint)
        mid.addWidget(rail)
        v.addLayout(mid, 1)

        # -- bottom strip ------------------------------------------------
        bot = QWidget()
        bot.setFixedHeight(32)
        bot.setStyleSheet("background:#20252b;border-top:1px solid #333a44;")
        bh = QHBoxLayout(bot)
        bh.setContentsMargins(14, 4, 14, 4)
        self.lbl_session = QLabel("-")
        self.lbl_session.setStyleSheet("font-family:monospace;font-size:11px;"
                                       "color:#93a3ad;border:none;")
        bh.addWidget(self.lbl_session)
        bh.addStretch(1)
        self.lbl_free = QLabel("-")
        self.lbl_free.setStyleSheet("font-family:monospace;font-size:11px;"
                                    "color:#93a3ad;border:none;")
        bh.addWidget(self.lbl_free)
        v.addWidget(bot)

    # ------------------------------------------------------------------
    def _style_state(self, state: str) -> None:
        running = state in ("burst", "rapid", "drain", "timelapse", "video",
                            "birdflight")
        name = {"birdflight": "WATCHING"}.get(state, state.upper())
        self.lbl_state.setText(name)
        self.lbl_state.setStyleSheet(
            "QLabel{font-size:14px;font-weight:800;letter-spacing:0.5px;"
            "border-radius:5px;padding:6px 16px;%s}"
            % ("background:#1f7a3f;color:#ffffff;border:2px solid #7fe3a2;"
               if running else
               "background:#232830;color:#93a3ad;border:1px solid #39414c;"))

    def _style_outdoor(self, on: bool) -> None:
        self.btn_outdoor.setStyleSheet(
            "QToolButton{border-radius:5px;font-size:13px;font-weight:800;%s}"
            % ("background:#6a5a10;color:#ffe628;border:2px solid #ffe628;" if on
               else "background:#1b1f24;color:#ffe628;border:2px solid #6a5a10;"))

    def _restyle_style_buttons(self, active: int) -> None:
        for i, b in enumerate(self._style_buttons):
            on = i == active
            b.setChecked(on)
            b.setStyleSheet(
                "QToolButton{border-radius:5px;font-size:12px;padding:0 16px;%s}"
                % ("background:#2f6f8f;color:#ffffff;font-weight:800;"
                   "border:1px solid #7fd0f0;" if on else
                   "background:#232830;color:#93a3ad;font-weight:600;"
                   "border:1px solid #39414c;"))

    def _outdoor_toggled(self, on: bool) -> None:
        self._style_outdoor(on)
        # The bench checkbox is the master; its handler does the real work.
        if self.win.chk_outdoor.isChecked() != on:
            self.win.chk_outdoor.setChecked(on)

    def _pick_style(self, idx: int) -> None:
        self._restyle_style_buttons(idx)
        if self.win.cmb_outdoor.currentIndex() != idx:
            self.win.cmb_outdoor.setCurrentIndex(idx)

    # -- hooks the window calls ----------------------------------------
    def sync_mode(self, idx: int) -> None:
        self.tuner.set_index(idx)
        label = self.win.MODES[max(0, min(idx, len(self.win.MODES) - 1))][0]
        self.lbl_mode.setText(label.upper())

    def set_outdoor(self, on: bool, style_idx: int) -> None:
        if self.btn_outdoor.isChecked() != on:
            self.btn_outdoor.blockSignals(True)
            self.btn_outdoor.setChecked(on)
            self.btn_outdoor.blockSignals(False)
        self._style_outdoor(on)
        self._restyle_style_buttons(style_idx)

    def update_go(self, running: bool, state: str, label: str) -> None:
        self._style_state(state)
        self.btn_go.setChecked(running)
        self.btn_go.setText(("STOP — %s" % state.upper()) if running
                            else "START — %s" % label.upper())
        self.btn_go.setStyleSheet(
            "QPushButton{font-size:17px;font-weight:800;border-radius:6px;"
            "background:%s;color:white;border:none;}"
            % ("#a03020" if running else "#1f7a3f"))
        if state != "birdflight":
            self.ladder.set_idle(
                "select Bird Flight and START to watch"
                if state in ("idle", "preview") else "mode not running")

    def on_bird(self, payload: Dict[str, Any]) -> None:
        s = payload.get("sighting") or {}
        take_n = payload.get("take_n") if payload.get("phase") == "take" else None
        self.ladder.update_sighting(s, take_n)
        if take_n is not None:
            self.lbl_takes.setText(
                "takes %d · last %s · limit %s"
                % (take_n, time.strftime("%H:%M:%S"),
                   int(self.win.cfg["bf_takes"]) or "off"))

    def set_camera_label(self, text: str) -> None:
        self.lbl_camera.setText(text)

    def refresh_status(self, session: str, free_text: str,
                       tiers: Optional[List[Dict[str, Any]]]) -> None:
        self.lbl_session.setText(session)
        self.lbl_free.setText(free_text)
        for i, (cell, lab, bar) in enumerate(self._tier_bars):
            if tiers and i < len(tiers):
                t = tiers[i]
                total = float(t.get("total_mb") or 0) or 1.0
                free = float(t.get("free_mb") or 0)
                lab.setText("%s  %.0fG free" % (t.get("label", "?"), free / 1024))
                bar.setValue(int(100 * max(0.0, total - free) / total))
                cell.setVisible(True)
            elif not tiers and i == 0:
                lab.setText("free  %s" % free_text)
                bar.setValue(0)
                cell.setVisible(False)   # a bare number reads better than a bar
            else:
                cell.setVisible(False)


# ======================================================================
# Library -- the darkroom
# ======================================================================
VERDICT_COLORS = {"ok": "#5fd07a", "dark": "#5aa0ff", "blown": "#ff6a44",
                  "empty": "#e0a828"}
THUMB_BATCH = 32          # thumbnails loaded per timer tick, to keep Qt live
THUMB_LIMIT = 480         # a rapid run can hold thousands; show the newest


def _read_index(session_path: str) -> List[Dict[str, Any]]:
    """index.jsonl entries for a session, oldest first; [] when there is none."""
    import json
    path = os.path.join(session_path, "index.jsonl")
    out: List[Dict[str, Any]] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def _scan_folder(session_path: str) -> List[Dict[str, Any]]:
    """Fallback for folders without an index: plain entries from the JPEGs."""
    out = []
    for root, _dirs, files in os.walk(session_path):
        if "_rejected" in root:
            continue
        for f in sorted(files):
            if f.lower().endswith((".jpg", ".jpeg")):
                out.append({"file": os.path.relpath(os.path.join(root, f),
                                                    session_path)})
    return out


class LibraryFace(QWidget):
    """Sessions on the left, frames in the middle, one frame's story on the
    right. Verdicts come from the capture-time gates via index.jsonl --
    nothing is re-scored here."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setStyleSheet("background:#1b1f24;")
        self._entries: List[Dict[str, Any]] = []
        self._session_path: Optional[str] = None
        self._load_pos = 0
        self._filter = "all"
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(30)
        self._thumb_timer.timeout.connect(self._load_more_thumbs)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # -- toolbar ------------------------------------------------------
        bar = QWidget()
        bar.setFixedHeight(44)
        bar.setStyleSheet("background:#1e2329;border-bottom:1px solid #333a44;")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(14, 6, 14, 6)
        bh.setSpacing(10)
        self._filter_buttons = {}
        for name, label in (("all", "All"), ("ok", "OK only"), ("takes", "Takes")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, n=name: self._set_filter(n))
            self._filter_buttons[name] = b
            bh.addWidget(b)
        self._restyle_filters()
        bh.addStretch(1)
        for text, slot in (("Offload → USB", lambda: win.storage.offload_now()),
                           ("Open folder", self._open_session),
                           ("Rescan", self.refresh)):
            b = QPushButton(text)
            b.setMinimumHeight(30)
            b.clicked.connect(slot)
            bh.addWidget(b)
        v.addWidget(bar)

        # -- main splitter --------------------------------------------------
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        head = QLabel("SESSIONS")
        head.setStyleSheet("color:#7a8791;font-size:10px;letter-spacing:1px;"
                           "padding:9px 12px;border-bottom:1px solid #2a313a;")
        lv.addWidget(head)
        self.lst_sessions = QListWidget()
        self.lst_sessions.setStyleSheet(
            "QListWidget{background:#20252b;border:none;font-size:12px;}"
            "QListWidget::item{padding:8px 12px;border-bottom:1px solid #2a313a;}"
            "QListWidget::item:selected{background:#2f6f8f;color:#ffffff;}")
        self.lst_sessions.currentItemChanged.connect(self._session_picked)
        lv.addWidget(self.lst_sessions, 1)
        split.addWidget(left)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setIconSize(QPixmap(160, 120).size())
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(10)
        self.grid.setStyleSheet(
            "QListWidget{background:#1b1f24;border:none;font-size:10px;}"
            "QListWidget::item{color:#93a3ad;}"
            "QListWidget::item:selected{background:#2f6f8f;color:#ffffff;}")
        self.grid.currentItemChanged.connect(self._frame_picked)
        split.addWidget(self.grid)

        right = QWidget()
        right.setStyleSheet("background:#20252b;")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(14, 14, 14, 14)
        rv.setSpacing(10)
        self.lbl_big = QLabel("select a frame")
        self.lbl_big.setAlignment(Qt.AlignCenter)
        self.lbl_big.setMinimumHeight(196)
        self.lbl_big.setStyleSheet("background:#0d1013;border:1px solid #39414c;"
                                   "border-radius:4px;color:#7a8791;")
        rv.addWidget(self.lbl_big)
        self.lbl_facts = QLabel("-")
        self.lbl_facts.setWordWrap(True)
        self.lbl_facts.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_facts.setStyleSheet("font-family:monospace;font-size:11px;"
                                     "color:#cfd6dd;")
        rv.addWidget(self.lbl_facts)
        self.lbl_trigger = QLabel("")
        self.lbl_trigger.setWordWrap(True)
        self.lbl_trigger.setStyleSheet("font-family:monospace;font-size:11px;"
                                       "color:#9fd0ff;")
        rv.addWidget(self.lbl_trigger)
        rv.addStretch(1)
        self.btn_ref = QPushButton("Use as sharpness reference")
        self.btn_ref.setMinimumHeight(38)
        self.btn_ref.clicked.connect(self._use_as_reference)
        rv.addWidget(self.btn_ref)
        row = QHBoxLayout()
        b_open = QPushButton("Open file")
        b_open.clicked.connect(self._open_frame)
        row.addWidget(b_open)
        self.btn_del = QPushButton("Delete file")
        self.btn_del.setStyleSheet("color:#ff8a70;")
        self.btn_del.clicked.connect(self._delete_frame)
        row.addWidget(self.btn_del)
        rv.addLayout(row)
        split.addWidget(right)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([260, 640, 280])
        v.addWidget(split, 1)

        # -- encode, folded in from the old Process tab ---------------------
        enc_area = QScrollArea()
        enc_area.setWidgetResizable(True)
        enc_area.setFrameShape(QFrame.NoFrame)
        enc_area.setMaximumHeight(360)
        self.enc_acc = Accordion("Encode photos into a movie", expanded=False)
        self.enc_acc.set_summary("pick a session above, then expand")
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(6, 0, 6, 4)
        hv.addWidget(self.enc_acc)
        enc_area.setWidget(holder)
        v.addWidget(enc_area)
        self._enc_area = enc_area
        enc_area.setMaximumHeight(64)
        self.enc_acc.toggled_open.connect(
            lambda on: enc_area.setMaximumHeight(360 if on else 64))

    def adopt_encode_page(self, page: QWidget) -> None:
        """The encode UI is built by the main window (it owns the job); the
        Library face is simply where it lives now."""
        self.enc_acc.addWidget(page)

    # ------------------------------------------------------------------
    def _restyle_filters(self) -> None:
        for name, b in self._filter_buttons.items():
            on = name == self._filter
            b.setChecked(on)
            b.setStyleSheet(
                "QToolButton{border-radius:4px;padding:5px 14px;font-size:12px;%s}"
                % ("background:#2f6f8f;color:#ffffff;font-weight:800;"
                   "border:1px solid #7fd0f0;" if on else
                   "background:#232830;color:#93a3ad;font-weight:600;"
                   "border:1px solid #39414c;"))

    def _set_filter(self, name: str) -> None:
        self._filter = name
        self._restyle_filters()
        self._reload_grid()

    def refresh(self) -> None:
        """Rescan the data root. Called on entering the face and on demand."""
        from .. import timelapse as tl
        current = self._session_path
        self.lst_sessions.blockSignals(True)
        self.lst_sessions.clear()
        kinds = {"bird": "BIRD", "tlc": "TIMELAPSE", "rapid": "RAPID"}
        for s in tl.list_sessions(self.win.cfg["data_root"]):
            kind = next((v for k, v in kinds.items() if s["id"].startswith(k)), "")
            label = "%s%s — %s frames" % (
                s["id"], ("   [%s]" % kind) if kind else "",
                s.get("frames") or "?")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, s["path"])
            self.lst_sessions.addItem(item)
            if s["path"] == current:
                self.lst_sessions.setCurrentItem(item)
        self.lst_sessions.blockSignals(False)
        if self.lst_sessions.currentRow() < 0 and self.lst_sessions.count():
            self.lst_sessions.setCurrentRow(0)   # fires _session_picked

    def _session_picked(self, item, _prev=None) -> None:
        if item is None:
            return
        self._session_path = item.data(Qt.UserRole)
        # Point the encode source at what is being looked at.
        cmb = getattr(self.win, "cmb_source", None)
        if cmb is not None:
            idx = cmb.findData(self._session_path)
            if idx >= 0:
                cmb.setCurrentIndex(idx)
        self._reload_grid()

    def _reload_grid(self) -> None:
        self.grid.clear()
        self._load_pos = 0
        if not self._session_path:
            return
        entries = _read_index(self._session_path) or _scan_folder(self._session_path)
        entries = [e for e in entries if e.get("file")]
        if self._filter == "ok":
            entries = [e for e in entries if e.get("verdict", "ok") == "ok"]
        elif self._filter == "takes":
            entries = [e for e in entries if e.get("bird")]
        self._entries = entries[-THUMB_LIMIT:]
        skipped = len(entries) - len(self._entries)
        if skipped > 0:
            head = QListWidgetItem("+%d older\nnot shown" % skipped)
            head.setFlags(Qt.NoItemFlags)
            self.grid.addItem(head)
        self._thumb_timer.start()

    def _load_more_thumbs(self) -> None:
        end = min(self._load_pos + THUMB_BATCH, len(self._entries))
        for i in range(self._load_pos, end):
            e = self._entries[i]
            path = os.path.join(self._session_path, e["file"])
            pm = QPixmap(path)
            item = QListWidgetItem()
            verdict = e.get("verdict", "ok")
            tag = "TAKE %.1f" % (e.get("bird") or {}).get("sharpness", 0.0) \
                if e.get("bird") else verdict
            item.setText(tag)
            from PyQt5.QtGui import QColor
            item.setForeground(QColor("#e0a828" if e.get("bird")
                                      else VERDICT_COLORS.get(verdict, "#93a3ad")))
            if not pm.isNull():
                item.setIcon(_icon_from(pm, 160, 120))
            item.setData(Qt.UserRole, i)
            self.grid.addItem(item)
        self._load_pos = end
        if end >= len(self._entries):
            self._thumb_timer.stop()

    def _current_entry(self) -> Optional[Dict[str, Any]]:
        item = self.grid.currentItem()
        if item is None or item.data(Qt.UserRole) is None:
            return None
        try:
            return self._entries[int(item.data(Qt.UserRole))]
        except (IndexError, ValueError):
            return None

    def _frame_picked(self, item, _prev=None) -> None:
        e = self._current_entry()
        if e is None:
            return
        path = os.path.join(self._session_path, e["file"])
        pm = QPixmap(path)
        if not pm.isNull():
            self.lbl_big.setPixmap(pm.scaled(
                self.lbl_big.width() - 8, 260,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        metrics = e.get("metrics") or {}
        from ..naming import describe_shutter
        facts = [os.path.join(os.path.basename(self._session_path), e["file"])]
        if e.get("shutter_us"):
            facts.append("%s · gain %.2f" % (describe_shutter(e["shutter_us"]),
                                                  e.get("gain", 0.0)))
        if metrics:
            facts.append("sharpness %.1f · verdict %s · clip %.2f%%"
                         % (metrics.get("sharpness_norm", 0.0),
                            e.get("verdict", "?"),
                            100.0 * metrics.get("clip_hi", 0.0)))
        self.lbl_facts.setText("\n".join(facts))
        bird = e.get("bird")
        if bird:
            self.lbl_trigger.setText(
                "trigger that fired this take:\nsharp %.1f · size %.2f%% · "
                "ring %.0f%% · sky %.0f%% · motion %.2f%%"
                % (bird.get("sharpness", 0.0), 100 * bird.get("area_frac", 0.0),
                   100 * bird.get("ring_sky_frac", 0.0),
                   100 * bird.get("sky_frac", 0.0),
                   100 * bird.get("motion_frac", 0.0)))
        else:
            self.lbl_trigger.setText("")
        measured = bool(metrics.get("focus_measured"))
        self.btn_ref.setEnabled(measured)
        self.btn_ref.setToolTip("" if measured else
                                "this frame carries no focus measurement")

    def _use_as_reference(self) -> None:
        e = self._current_entry()
        metrics = (e or {}).get("metrics") or {}
        value = metrics.get("sharpness_norm")
        if not value:
            return
        self.win.cfg["sharpness_reference"] = float(value)
        self.win.cfg["blur_threshold"] = round(float(value) * 0.5, 2)
        self.win.cfg.save()
        self.win.log("sharp reference %.1f from library, blur gate now %.1f"
                     % (value, self.win.cfg["blur_threshold"]))

    def _open_frame(self) -> None:
        e = self._current_entry()
        if e:
            self.win.open_path(os.path.join(self._session_path, e["file"]))

    def _open_session(self) -> None:
        self.win.open_path(self._session_path or self.win.cfg["data_root"])

    def _delete_frame(self) -> None:
        from PyQt5.QtWidgets import QMessageBox
        e = self._current_entry()
        if e is None:
            return
        path = os.path.join(self._session_path, e["file"])
        reply = QMessageBox.question(
            self, "Delete file",
            "Delete this frame from disk?\n\n%s\n\nThe index keeps its record."
            % path, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            os.remove(path)
        except OSError as exc:
            self.win.log("delete failed: %s" % exc)
            return
        row = self.grid.currentRow()
        self.grid.takeItem(row)
        self.win.log("deleted %s" % os.path.basename(path))
