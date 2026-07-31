# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Always-on-top focus monitor for the manual-focus C-mount lens.

Three things make manual focus on this rig hard: the lens has no feedback, the
preview is a 640-pixel downscale that hides real softness, and the subject is
usually a small dark shape against bright sky.

So this window shows the one view that actually settles it -- a 1:1 crop of
native sensor pixels from the centre of the frame -- alongside a numeric focus
score with peak-hold. Turn the focus ring until the number stops climbing; the
peak-hold marker remembers the best you have achieved so you can tell whether
you have gone past it.

It floats above other windows and can sit in a corner of the Pi's desktop while
you work, which is the "live monitor on the desktop" job that reloading a
wallpaper was doing before -- but at full sensor resolution and updating at the
preview rate rather than whenever a still happens to land.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import numpy as np
from PyQt5.QtCore import Qt, QPoint, QRect
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

PEAK_DECAY_S = 6.0  # how long a peak-hold reading survives before it decays


class OnePixelView(QWidget):
    """Draws the native-resolution crop, optionally with peaking."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._image: Optional[QImage] = None
        self._buf: Optional[np.ndarray] = None
        self.show_peaking = True
        self.zoom = 1.0
        self._msg = "enable focus assist"

    def set_crop(self, rgb: Optional[np.ndarray]) -> None:
        if rgb is None or rgb.size == 0:
            return
        out = np.ascontiguousarray(rgb.copy())
        if self.show_peaking:
            g = out[:, :, 1].astype(np.int16)
            gx = np.zeros_like(g)
            gy = np.zeros_like(g)
            gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
            gy[1:-1, :] = g[2:, :] - g[:-2, :]
            out[(np.abs(gx) + np.abs(gy)) > 40] = (255, 40, 40)
        h, w = out.shape[:2]
        self._buf = out
        self._image = QImage(out.data, w, h, 3 * w, QImage.Format_RGB888)
        self._msg = ""
        self.update()

    def set_message(self, text: str) -> None:
        self._msg = text
        self._image = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(12, 12, 14))
        if self._image is None:
            p.setPen(QColor(150, 150, 155))
            p.drawText(self.rect(), Qt.AlignCenter, self._msg)
            p.end()
            return
        iw, ih = self._image.width(), self._image.height()
        side = int(min(self.width(), self.height()))
        src_side = max(16, int(min(iw, ih) / max(self.zoom, 0.05)))
        sx, sy = (iw - src_side) // 2, (ih - src_side) // 2
        target = QRect((self.width() - side) // 2, (self.height() - side) // 2, side, side)
        p.drawImage(target, self._image, QRect(sx, sy, src_side, src_side))
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        p.drawRect(target)
        p.setPen(QColor(180, 180, 190))
        p.setFont(QFont("DejaVu Sans Mono", 8))
        p.drawText(target.left() + 6, target.bottom() - 6,
                   "%d x %d native px  @ %.0f%%" % (src_side, src_side, self.zoom * 100))
        p.end()


class FocusBar(QWidget):
    """Focus score with a decaying peak-hold marker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.value = 0.0
        self.peak = 0.0
        self._peak_at = 0.0
        self.scale = 60.0  # score that fills the bar; auto-grows

    def set_value(self, value: float) -> None:
        self.value = float(value)
        now = time.monotonic()
        if self.value >= self.peak or (now - self._peak_at) > PEAK_DECAY_S:
            self.peak = self.value
            self._peak_at = now
        self.scale = max(self.scale, self.peak * 1.15, 20.0)
        self.update()

    def reset_peak(self) -> None:
        self.peak = self.value
        self._peak_at = time.monotonic()
        self.scale = max(self.value * 1.5, 20.0)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(20, 20, 23))
        w, h = self.width(), self.height()
        bar_h = 16
        top = h - bar_h - 4

        frac = min(1.0, self.value / max(self.scale, 1e-6))
        peak_frac = min(1.0, self.peak / max(self.scale, 1e-6))
        # Green once we are within 5% of the best reading seen -- that is the
        # cue to stop turning the ring.
        near = self.peak > 0 and self.value >= self.peak * 0.95
        color = QColor(95, 208, 122) if near else QColor(90, 160, 255)

        p.fillRect(0, top, w, bar_h, QColor(38, 38, 44))
        p.fillRect(0, top, int(w * frac), bar_h, color)
        px = int(w * peak_frac)
        p.setPen(QPen(QColor(255, 210, 60), 2))
        p.drawLine(px, top - 3, px, top + bar_h + 3)

        p.setPen(QColor(235, 235, 240))
        p.setFont(QFont("DejaVu Sans Mono", 17, QFont.Bold))
        p.drawText(6, top - 8, "%.1f" % self.value)
        p.setFont(QFont("DejaVu Sans", 9))
        p.setPen(QColor(150, 150, 160))
        p.drawText(w - 130, top - 8, "best %.1f" % self.peak)
        p.end()


class FocusMonitor(QWidget):
    """Frameless, always-on-top live focus view."""

    def __init__(self, engine, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.engine = engine
        self.setWindowTitle("birdshot focus monitor")
        self.resize(430, 620)
        self.setStyleSheet(
            "QWidget{background:#141416;color:#ddd;}"
            "QPushButton{background:#2a2a30;border:1px solid #3a3a44;padding:4px 8px;"
            "border-radius:4px;}"
            "QPushButton:hover{background:#35353d;}"
        )
        self._drag: Optional[QPoint] = None

        title = QLabel("  FOCUS MONITOR")
        title.setStyleSheet("font-weight:700;letter-spacing:1px;color:#8ab;")
        btn_close = QPushButton("x")
        btn_close.setFixedWidth(28)
        btn_close.clicked.connect(self.close)
        bar = QHBoxLayout()
        bar.addWidget(title, 1)
        bar.addWidget(btn_close)

        self.view = OnePixelView()
        self.meter = FocusBar()

        self.cmb_zoom = QComboBox()
        for label, z in (("100%", 1.0), ("200%", 2.0), ("400%", 4.0), ("fit", 0.5)):
            self.cmb_zoom.addItem(label, z)
        self.cmb_zoom.currentIndexChanged.connect(
            lambda i: setattr(self.view, "zoom", self.cmb_zoom.itemData(i))
        )
        self.chk_peak = QCheckBox("peaking")
        self.chk_peak.setChecked(True)
        self.chk_peak.toggled.connect(lambda s: setattr(self.view, "show_peaking", s))
        btn_reset = QPushButton("reset best")
        btn_reset.clicked.connect(self.meter.reset_peak)

        row = QHBoxLayout()
        row.addWidget(QLabel("zoom"))
        row.addWidget(self.cmb_zoom)
        row.addWidget(self.chk_peak)
        row.addStretch(1)
        row.addWidget(btn_reset)

        self.info = QLabel("-")
        self.info.setStyleSheet("font-family:monospace;font-size:11px;color:#9a9aa5;")
        self.hint = QLabel("Turn the focus ring until the number stops rising.\n"
                           "The yellow marker is the best reading so far.")
        self.hint.setStyleSheet("color:#70707a;font-size:11px;")

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 8)
        v.addLayout(bar)
        v.addWidget(self.view, 1)
        v.addWidget(self.meter)
        v.addLayout(row)
        v.addWidget(self.info)
        v.addWidget(self.hint)

        self.view.set_message("starting focus assist...")
        self.engine.send("focus_assist", on=True)

    # ------------------------------------------------------------------
    def handle_preview(self, payload: Dict[str, Any]) -> None:
        crop = payload.get("focus_view")
        if crop is not None:
            self.view.set_crop(crop)
        stats = payload.get("stats")
        if stats is not None:
            self.meter.set_value(stats.sharpness_norm)
            self.info.setText(
                "shutter %-10s gain %4.2f  p50 %3d  clip %.2f%%  tiles %d"
                % (_short(payload.get("shutter_us") or 0), payload.get("gain") or 0,
                   int(stats.p50), stats.clip_hi * 100.0, stats.contrast_tiles)
            )

    # -- frameless window dragging --------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None

    def closeEvent(self, event) -> None:  # noqa: N802
        # Stop paying for the full-frame copy once nothing is watching.
        self.engine.send("focus_assist", on=False)
        super().closeEvent(event)


def _short(us: int) -> str:
    if us >= 1_000_000:
        return "%.1fs" % (us / 1e6)
    if us >= 1000:
        return "%.1fms" % (us / 1000.0)
    return "%dus" % us
