# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Shared GUI pieces: collapsible sections, fullscreen preview, blocking overlay.

The tab layout leans on :class:`Accordion` so a handful of tabs can hold what
used to need ten. Each section shows its own one-line summary when collapsed, so
a closed panel still tells you what it is set to -- otherwise collapsing just
hides state and you end up opening everything anyway.
"""

from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)


class Accordion(QWidget):
    """A titled section that collapses to a single row."""

    toggled_open = pyqtSignal(bool)

    def __init__(self, title: str, expanded: bool = False, parent=None):
        super().__init__(parent)
        self._button = QToolButton()
        self._button.setText(title)
        self._button.setCheckable(True)
        self._button.setChecked(expanded)
        self._button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self._button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # A prominent, obviously-clickable bar rather than a line of text. The
        # whole width is the hit target, it is coloured, and it lightens on
        # hover so it reads as a control instead of a heading.
        self._button.setMinimumHeight(38)
        self._button.setCursor(Qt.PointingHandCursor)
        self._button.setStyleSheet(
            "QToolButton{border:none;border-left:4px solid #2f6f8f;"
            "background:#25303a;color:#cfe3ef;font-weight:700;font-size:14px;"
            "padding:8px 10px;text-align:left;border-radius:4px;}"
            "QToolButton:hover{background:#31414f;color:#eaf5fb;"
            "border-left:4px solid #4da3cc;}"
            "QToolButton:checked{background:#2f6f8f;color:#ffffff;"
            "border-left:4px solid #7fd0f0;}"
        )
        self._button.clicked.connect(self._on_click)

        self._summary = QLabel()
        self._summary.setStyleSheet(
            "color:#93a3ad;font-size:11px;padding:2px 0 4px 16px;")
        self._summary.setWordWrap(True)

        self._body = QFrame()
        self._body.setFrameShape(QFrame.NoFrame)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(14, 2, 2, 8)
        self._body.setVisible(expanded)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setStyleSheet("color:#333;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addWidget(self._button)
        outer.addWidget(self._summary)
        outer.addWidget(self._body)
        outer.addWidget(rule)
        self._summary.setVisible(not expanded)

    def _on_click(self, checked: bool) -> None:
        self._button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self._body.setVisible(checked)
        self._summary.setVisible(not checked and bool(self._summary.text()))
        self.toggled_open.emit(checked)

    def addWidget(self, w) -> None:  # noqa: N802 - matches Qt layout naming
        self._body_layout.addWidget(w)

    def addLayout(self, lay) -> None:  # noqa: N802
        self._body_layout.addLayout(lay)

    def set_summary(self, text: str) -> None:
        """One-line state shown while collapsed."""
        self._summary.setText(text)
        self._summary.setVisible(not self._button.isChecked() and bool(text))

    def set_expanded(self, on: bool) -> None:
        self._button.setChecked(on)
        self._on_click(on)

    def is_expanded(self) -> bool:
        return self._button.isChecked()


class FullscreenPreview(QWidget):
    """The live image on its own, filling the screen.

    A separate window rather than hiding the panels, so the main window carries
    on running and closing this cannot leave the app in a half-configured state.
    """

    closed = pyqtSignal()

    def __init__(self, preview_widget, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("birdshot - fullscreen")
        self.setStyleSheet("background:#000;")
        self.view = preview_widget
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self._hint = QLabel("Esc or F11 to exit", self)
        self._hint.setStyleSheet(
            "color:#aaa;background:rgba(0,0,0,150);padding:4px 8px;border-radius:4px;")
        self._hint.move(16, 16)
        self._hint.adjustSize()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Escape, Qt.Key_F11):
            self.close()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


class BlockingOverlay(QWidget):
    """Full-window notice for conditions the user must actually deal with.

    Used when every storage tier is full. A status-bar line is the wrong place
    for that -- capture has stopped and frames are being lost, so it needs to be
    impossible to miss.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.title = "OUT OF SPACE"
        self.detail = ""
        self.accent = QColor(200, 60, 40)
        self.hide()

    def show_message(self, title: str, detail: str,
                     accent: Optional[QColor] = None) -> None:
        self.title = title
        self.detail = detail
        if accent is not None:
            self.accent = accent
        if self.parent():
            self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(12, 6, 4, 235))

        band = self.rect().adjusted(40, self.height() // 4, -40, -self.height() // 4)
        p.fillRect(band, QColor(30, 14, 10, 240))
        p.setPen(self.accent)
        for i in range(3):
            p.drawRect(band.adjusted(i, i, -i, -i))

        p.setPen(self.accent)
        p.setFont(QFont("DejaVu Sans", 30, QFont.Bold))
        p.drawText(band.adjusted(30, 24, -30, 0), Qt.AlignTop | Qt.AlignHCenter,
                   self.title)

        p.setPen(QColor(235, 225, 220))
        p.setFont(QFont("DejaVu Sans Mono", 12))
        p.drawText(band.adjusted(30, 92, -30, -24),
                   Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap, self.detail)
        p.end()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.update()


class ModeTuner(QWidget):
    """Mode selector laid out like a tuner dial.

    A dropdown hides everything except the current choice, so there is no way to
    tell what else exists without opening it. This shows every mode at once with
    the current one lit, flanked by step arrows -- you can see the whole band and
    where you are on it.
    """

    changed = pyqtSignal(int)

    def __init__(self, modes, current: int = 0, parent=None):
        super().__init__(parent)
        self.modes = list(modes)
        self._index = max(0, min(int(current), len(self.modes) - 1))
        self._buttons = []

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._prev = QToolButton()
        self._prev.setArrowType(Qt.LeftArrow)
        self._prev.setFixedWidth(34)
        self._prev.setMinimumHeight(46)
        self._prev.setCursor(Qt.PointingHandCursor)
        self._prev.clicked.connect(lambda: self.step(-1))
        row.addWidget(self._prev)

        for i, (label, _key, _hint) in enumerate(self.modes):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setMinimumHeight(46)
            b.setCursor(Qt.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _c, n=i: self.set_index(n))
            self._buttons.append(b)
            row.addWidget(b, 1)

        self._next = QToolButton()
        self._next.setArrowType(Qt.RightArrow)
        self._next.setFixedWidth(34)
        self._next.setMinimumHeight(46)
        self._next.setCursor(Qt.PointingHandCursor)
        self._next.clicked.connect(lambda: self.step(1))
        row.addWidget(self._next)

        self._restyle()

    def _restyle(self) -> None:
        for i, b in enumerate(self._buttons):
            on = i == self._index
            b.setChecked(on)
            b.setStyleSheet(
                "QToolButton{border-radius:5px;font-size:14px;padding:6px 4px;"
                + ("background:#1f7a3f;color:#ffffff;font-weight:800;"
                   "border:2px solid #7fe3a2;"
                   if on else
                   "background:#232830;color:#93a3ad;font-weight:600;"
                   "border:1px solid #39414c;")
                + "}"
                "QToolButton:hover{background:%s;color:#eaf5fb;}"
                % ("#25904a" if on else "#2d3540")
            )

    def index(self) -> int:
        return self._index

    def set_index(self, i: int) -> None:
        i = max(0, min(int(i), len(self.modes) - 1))
        if i == self._index:
            return
        self._index = i
        self._restyle()
        self.changed.emit(i)

    def step(self, delta: int) -> None:
        self.set_index((self._index + delta) % len(self.modes))
