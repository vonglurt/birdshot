# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
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
    QFrame, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
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
        self._button.setStyleSheet(
            "QToolButton{border:none;font-weight:600;font-size:13px;padding:6px 2px;"
            "text-align:left;}"
            "QToolButton:hover{color:#8ab;}"
        )
        self._button.clicked.connect(self._on_click)

        self._summary = QLabel()
        self._summary.setStyleSheet("color:#888;font-size:11px;padding-left:18px;")
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
        outer.setSpacing(0)
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
