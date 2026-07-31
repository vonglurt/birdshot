# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul
"""Live preview pane and histogram.

The preview is drawn by hand rather than using QGlPicamera2 because the overlays
are the point: with a manual-focus C-mount lens and a subject that is usually a
small dark shape against bright sky, focus peaking and clipping zebras are the
only reliable way to judge a shot on a 640-pixel preview.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
from PyQt5.QtCore import Qt, QRect, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QFont
from PyQt5.QtWidgets import QSizePolicy, QWidget

from ..analysis import FrameStats

ZEBRA_COLOR = (255, 0, 255)
PEAK_COLOR = (255, 40, 40)


class PreviewWidget(QWidget):
    """Scaled live view with optional analysis overlays."""

    double_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(True)

        self.show_zebra = True
        self.show_peaking = False
        self.show_zones = True
        self.show_grid = False
        self.peak_threshold = 28.0
        self.sky_zone_frac = 0.40

        self.show_focus_map = False
        self.show_sharpness = False
        # Outdoor mode: the Pi's screen washes out in sunlight, so the preview
        # is contrast-stretched and edges are drawn in a colour that survives
        # glare. "edges" drops the picture entirely and shows structure only,
        # which is the most legible thing there is on a bright day.
        self.outdoor = False
        self.outdoor_style = "boost"   # boost | edges
        self.outdoor_strength = 1.0
        self.stripe_px = 3        # width of each yellow/black band

        self._image: Optional[QImage] = None
        self._buf: Optional[np.ndarray] = None  # must outlive the QImage
        self._stats: Optional[FrameStats] = None
        self._banner = "waiting for camera..."
        self._fmap: Optional[np.ndarray] = None
        self._fbest = None
        self._fpeak = 0.0
        self._fpeak_hold = 0.0

    # ------------------------------------------------------------------
    def set_banner(self, text: str) -> None:
        self._banner = text
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.double_clicked.emit()

    def set_focus_map(self, fmap, best, peak: float) -> None:
        self._fmap = fmap
        self._fbest = best
        self._fpeak = float(peak or 0.0)
        self._fpeak_hold = max(self._fpeak_hold * 0.99, self._fpeak)

    def reset_focus_peak(self) -> None:
        self._fpeak_hold = self._fpeak

    def set_frame(self, rgb: np.ndarray, y: Optional[np.ndarray], stats: Optional[FrameStats]) -> None:
        """``rgb`` is the display image; ``y`` is the full-resolution lores luma
        used for the overlays (it may be twice the size of ``rgb``)."""
        if rgb is None or rgb.size == 0:
            return
        out = np.ascontiguousarray(rgb.copy())
        h, w = out.shape[:2]

        if self.outdoor:
            out = self._outdoor(out)

        if y is not None and y.size:
            # Match the luma plane to the display image so masks line up.
            ys, xs = max(1, y.shape[0] // h), max(1, y.shape[1] // w)
            ysub = y[::ys, ::xs][:h, :w]
            if ysub.shape[:2] == (h, w):
                if self.show_zebra:
                    rows = np.arange(h)[:, None]
                    cols = np.arange(w)[None, :]
                    stripe = ((rows + cols) // 6) % 2 == 0
                    out[(ysub >= 250) & stripe] = ZEBRA_COLOR
                if self.show_peaking:
                    f = ysub.astype(np.int16)
                    gx = np.zeros_like(f)
                    gy = np.zeros_like(f)
                    gx[:, 1:-1] = f[:, 2:] - f[:, :-2]
                    gy[1:-1, :] = f[2:, :] - f[:-2, :]
                    out[(np.abs(gx) + np.abs(gy)) > self.peak_threshold] = PEAK_COLOR

        self._buf = out
        self._image = QImage(out.data, w, h, 3 * w, QImage.Format_RGB888)
        self._stats = stats
        self._banner = ""
        self.update()

    # ------------------------------------------------------------------
    def _outdoor(self, rgb: np.ndarray) -> np.ndarray:
        """Make edges findable on a sunlit screen.

        Two things fight you outdoors: the panel is dim relative to the sky, and
        real subjects are low-contrast shapes against bright backgrounds. So the
        image is stretched to use the full range, then its own gradient is
        burned in as a bright outline.
        """
        g = rgb[:, :, 1].astype(np.int16)

        # Percentile stretch, so haze and glare do not flatten everything.
        lo, hi = np.percentile(g, 2.0), np.percentile(g, 98.0)
        span = max(8.0, float(hi - lo))
        stretched = np.clip((rgb.astype(np.float32) - lo) * (255.0 / span), 0, 255)

        # Adjacent differences, not central: a central difference straddles two
        # pixels either side and draws a band three or four pixels wide, which
        # smears fine detail. This responds on a single pixel.
        gx = np.zeros_like(g)
        gy = np.zeros_like(g)
        gx[:, 1:] = g[:, 1:] - g[:, :-1]
        gy[1:, :] = g[1:, :] - g[:-1, :]
        mag = np.abs(gx) + np.abs(gy)
        # Threshold relative to the scene's own gradients, so a hazy low-contrast
        # view still shows its edges instead of going blank. Higher strength
        # means a lower bar, hence more edges. The small floor stops pure sensor
        # noise being drawn as structure.
        p96 = float(np.percentile(mag, 96.0))
        thresh = max(4.0, p96 / max(self.outdoor_strength, 0.1))

        # Hazard striping: edges are drawn as alternating yellow and black bands
        # rather than flat yellow. Solid yellow vanishes against a bright sky and
        # black vanishes against shadow; alternating them means one of the two
        # always contrasts, whatever the edge happens to lie on.
        h, w = mag.shape
        band = self.stripe_px if self.stripe_px > 0 else 3
        rows = np.arange(h, dtype=np.int32)[:, None]
        cols = np.arange(w, dtype=np.int32)[None, :]
        stripe = ((rows + cols) // band) % 2 == 0
        edge = mag > thresh

        if self.outdoor_style == "edges":
            # Structure only: nothing else on screen competes with it in sun.
            out = np.zeros_like(rgb)
            out[..., 0] = 10
            out[..., 1] = 12
            out[..., 2] = 14
            out[edge & stripe] = (255, 238, 0)
            out[edge & ~stripe] = (0, 0, 0)
            faint = (mag > thresh * 0.45) & ~edge
            out[faint] = (70, 66, 20)
            return out

        out = stretched.astype(np.uint8)
        out[edge & stripe] = (255, 238, 0)
        out[edge & ~stripe] = (0, 0, 0)
        return out

    def _target_rect(self) -> QRect:
        if self._image is None:
            return self.rect()
        iw, ih = self._image.width(), self._image.height()
        ww, wh = self.width(), self.height()
        scale = min(ww / iw, wh / ih)
        w, h = int(iw * scale), int(ih * scale)
        return QRect((ww - w) // 2, (wh - h) // 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(18, 18, 20))

        if self._image is None:
            painter.setPen(QColor(160, 160, 165))
            painter.setFont(QFont("DejaVu Sans", 11))
            painter.drawText(self.rect(), Qt.AlignCenter, self._banner or "no signal")
            painter.end()
            return

        rect = self._target_rect()
        painter.drawImage(rect, self._image)

        if self.show_zones:
            # The metering split: everything above the line is treated as sky
            # and weighted down, everything below is the subject zone.
            y = rect.top() + int(rect.height() * self.sky_zone_frac)
            pen = QPen(QColor(90, 200, 255, 170))
            pen.setStyle(Qt.DashLine)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(rect.left(), y, rect.right(), y)
            painter.setFont(QFont("DejaVu Sans", 8))
            painter.drawText(rect.left() + 6, y - 4, "sky zone")
            painter.drawText(rect.left() + 6, y + 12, "subject zone")

        if self.show_grid:
            painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
            for i in (1, 2):
                x = rect.left() + rect.width() * i // 3
                yy = rect.top() + rect.height() * i // 3
                painter.drawLine(x, rect.top(), x, rect.bottom())
                painter.drawLine(rect.left(), yy, rect.right(), yy)

        if self.show_focus_map and self._fmap is not None:
            self._paint_focus_map(painter, rect)

        if self.show_peaking:
            # The focus measure is taken from a native-resolution centre crop,
            # so show where that crop actually is.
            side = min(rect.width(), rect.height()) // 4
            cx, cy = rect.center().x(), rect.center().y()
            painter.setPen(QPen(QColor(255, 200, 40, 180), 1, Qt.DotLine))
            painter.drawRect(cx - side, cy - side, side * 2, side * 2)

        if self.show_sharpness and self._stats is not None:
            self._paint_sharpness(painter, rect)

        if self.outdoor:
            painter.setPen(QPen(QColor(255, 230, 40), 2))
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
            painter.setFont(QFont("DejaVu Sans", 11, QFont.Bold))
            label = ("OUTDOOR - EDGES ONLY" if self.outdoor_style == "edges"
                     else "OUTDOOR - CONTRAST BOOST")
            fm = painter.fontMetrics()
            tw = fm.width(label) + 14 if hasattr(fm, "width") else 200
            painter.fillRect(rect.right() - tw - 8, rect.top() + 6, tw, 22,
                             QColor(0, 0, 0, 170))
            painter.setPen(QColor(255, 230, 40))
            painter.drawText(rect.right() - tw - 1, rect.top() + 22, label)

        if self._stats is not None and self._stats.verdict != "ok":
            colors = {"dark": QColor(70, 130, 255), "blown": QColor(255, 90, 60),
                      "empty": QColor(200, 160, 40)}
            c = colors.get(self._stats.verdict, QColor(200, 200, 200))
            painter.setPen(QPen(c, 3))
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
            painter.setFont(QFont("DejaVu Sans", 10, QFont.Bold))
            painter.setPen(c)
            painter.drawText(rect.left() + 10, rect.top() + 22, self._stats.verdict.upper())

        painter.end()

    # ------------------------------------------------------------------
    def _paint_focus_map(self, painter: QPainter, rect: QRect) -> None:
        """Shade each tile by how sharp it is, and ring the sharpest.

        Binary peaking cannot distinguish a genuinely sharp branch from noisy
        sky -- both light up. Ranking the tiles shows you which part of the
        frame the lens is actually resolving.
        """
        rows, cols = self._fmap.shape
        tw = rect.width() / float(cols)
        th = rect.height() / float(rows)

        painter.save()
        for r in range(rows):
            for c in range(cols):
                v = float(self._fmap[r, c])
                if v < 0.12:
                    continue
                x = int(rect.left() + c * tw)
                y = int(rect.top() + r * th)
                w = int(tw) + 1
                h = int(th) + 1
                # Cool blue for soft, through amber, to green at sharpest.
                if v < 0.5:
                    t = v / 0.5
                    color = QColor(int(40 + 215 * t), int(90 + 110 * t), int(220 - 180 * t),
                                   int(30 + 55 * v))
                else:
                    t = (v - 0.5) / 0.5
                    color = QColor(int(255 - 160 * t), 200, int(40 + 80 * t),
                                   int(30 + 55 * v))
                painter.fillRect(x, y, w, h, color)

        if self._fbest is not None:
            br, bc = self._fbest
            x = int(rect.left() + bc * tw)
            y = int(rect.top() + br * th)
            painter.setPen(QPen(QColor(120, 255, 150), 3))
            painter.drawRect(x, y, int(tw), int(th))
            painter.setFont(QFont("DejaVu Sans", 8, QFont.Bold))
            painter.setPen(QColor(120, 255, 150))
            painter.drawText(x + 3, y + int(th) - 4, "sharpest")
        painter.restore()

    def _paint_sharpness(self, painter: QPainter, rect: QRect) -> None:
        """Large focus readout with a peak-hold bar, bottom-right of the frame."""
        st = self._stats
        value = float(st.sharpness_norm)
        peak = max(self._fpeak_hold, 1e-6)
        frac = min(1.0, self._fpeak / peak) if peak > 0 else 0.0
        near = self._fpeak >= peak * 0.95 and self._fpeak > 0

        box_w, box_h = 210, 74
        x = rect.right() - box_w - 12
        y = rect.bottom() - box_h - 12

        painter.save()
        painter.fillRect(x, y, box_w, box_h, QColor(10, 10, 12, 190))
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.drawRect(x, y, box_w, box_h)

        painter.setPen(QColor(150, 150, 160))
        painter.setFont(QFont("DejaVu Sans", 8))
        painter.drawText(x + 10, y + 16, "FOCUS")

        color = QColor(120, 255, 150) if near else QColor(235, 235, 240)
        painter.setPen(color)
        painter.setFont(QFont("DejaVu Sans Mono", 22, QFont.Bold))
        painter.drawText(x + 10, y + 46, "%.1f" % value)

        painter.setPen(QColor(130, 130, 140))
        painter.setFont(QFont("DejaVu Sans", 8))
        painter.drawText(x + 118, y + 46, "peak %.0f" % peak)

        bar_y = y + box_h - 16
        painter.fillRect(x + 10, bar_y, box_w - 20, 8, QColor(45, 45, 52))
        painter.fillRect(x + 10, bar_y, int((box_w - 20) * frac), 8,
                         color if near else QColor(90, 160, 255))
        painter.restore()


class HistogramWidget(QWidget):
    """Luminance histogram with clipping shoulders and the metering target."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._hist: Optional[np.ndarray] = None
        self._stats: Optional[FrameStats] = None
        self.target: float = 118.0

    def set_frame(self, y: Optional[np.ndarray], stats: Optional[FrameStats]) -> None:
        if y is None or y.size == 0:
            return
        hist = np.bincount(y.ravel(), minlength=256).astype(np.float64)
        total = hist.sum()
        if total > 0:
            hist /= total
        self._hist = hist
        self._stats = stats
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(24, 24, 27))
        if self._hist is None:
            painter.end()
            return

        w, h = self.width(), self.height()
        peak = float(self._hist.max()) or 1.0
        # sqrt keeps the tails -- the clipped highlights we care about -- visible
        # next to a huge midtone peak.
        scaled = np.sqrt(self._hist / peak)

        for i in range(256):
            x = int(i * w / 256.0)
            bw = max(1, int(w / 256.0) + 1)
            bh = int(scaled[i] * (h - 12))
            if i >= 250:
                color = QColor(255, 80, 60)
            elif i <= 5:
                color = QColor(70, 130, 255)
            else:
                color = QColor(190, 190, 195)
            painter.fillRect(x, h - bh - 2, bw, bh, color)

        # Metering target marker.
        tx = int(self.target * w / 256.0)
        painter.setPen(QPen(QColor(90, 230, 140), 2))
        painter.drawLine(tx, 0, tx, h)

        if self._stats is not None:
            painter.setPen(QColor(150, 150, 160))
            painter.setFont(QFont("DejaVu Sans Mono", 8))
            painter.drawText(
                4, 12,
                "p50 %d  p95 %d  clip %.2f%%  meter %.0f"
                % (self._stats.p50, self._stats.p95,
                   self._stats.clip_hi * 100.0, self._stats.meter),
            )
        painter.end()


class ToneCurveWidget(QWidget):
    """Compact plot of the output tone curve against the stock HQ-cam one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._curve = None
        self._stock = None

    def set_curves(self, curve, stock) -> None:
        self._curve, self._stock = curve, stock
        self.update()

    def _poly(self, xs, ys, rect):
        from PyQt5.QtCore import QPointF
        return [QPointF(rect.left() + x * rect.width(),
                        rect.bottom() - y * rect.height()) for x, y in zip(xs, ys)]

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 27))
        m = 8
        rect = self.rect().adjusted(m, m, -m, -m)

        p.setPen(QPen(QColor(255, 255, 255, 28), 1))
        for i in range(1, 4):
            x = rect.left() + rect.width() * i // 4
            y = rect.top() + rect.height() * i // 4
            p.drawLine(x, rect.top(), x, rect.bottom())
            p.drawLine(rect.left(), y, rect.right(), y)
        # Linear reference.
        p.setPen(QPen(QColor(120, 120, 130), 1, Qt.DashLine))
        p.drawLine(rect.left(), rect.bottom(), rect.right(), rect.top())

        if self._stock:
            p.setPen(QPen(QColor(90, 160, 255, 150), 1))
            p.drawPolyline(*self._poly(self._stock[0], self._stock[1], rect))
        if self._curve:
            p.setPen(QPen(QColor(95, 208, 122), 2))
            p.drawPolyline(*self._poly(self._curve[0], self._curve[1], rect))

        p.setPen(QColor(140, 140, 150))
        p.setFont(QFont("DejaVu Sans", 8))
        p.drawText(rect.left() + 2, rect.top() + 11, "out")
        p.drawText(rect.right() - 24, rect.bottom() - 3, "in")
        p.setPen(QColor(95, 208, 122))
        p.drawText(rect.left() + 2, rect.top() + 24, "active")
        p.setPen(QColor(90, 160, 255, 200))
        p.drawText(rect.left() + 2, rect.top() + 36, "stock ISP")
        p.end()
