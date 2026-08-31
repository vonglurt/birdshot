// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "preview.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <vector>

#include <QKeyEvent>
#include <QMouseEvent>
#include <QPainter>
#include <QWheelEvent>

#include "theme.hpp"

namespace {

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

// Percentile over an 8-bit plane via its histogram -- the numpy call's job.
double percentile(const std::vector<int>& hist, long total, double pct) {
  if (total <= 0) return 0.0;
  const long want = static_cast<long>(total * pct / 100.0);
  long seen = 0;
  for (int v = 0; v < 256; ++v) {
    seen += hist[v];
    if (seen >= want) return v;
  }
  return 255.0;
}

}  // namespace

PreviewWidget::PreviewWidget(QWidget* parent) : QWidget(parent) {
  setMinimumSize(480, 360);
  setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
  setAutoFillBackground(true);
}

void PreviewWidget::copyViewSettings(const PreviewWidget& o) {
  showZebra = o.showZebra;
  showPeaking = o.showPeaking;
  showZones = o.showZones;
  showGrid = o.showGrid;
  showFocusMap = o.showFocusMap;
  showSharpness = o.showSharpness;
  showHud = o.showHud;
  outdoor = o.outdoor;
  outdoorStyle = o.outdoorStyle;
  outdoorStrength = o.outdoorStrength;
  stripePx = o.stripePx;
  skyZoneFrac = o.skyZoneFrac;
}

// Build the display image from the luma plane: greyscale RGB with the
// outdoor rendering, zebras and peaking baked into the pixels, exactly as
// the prototype burned them into its numpy buffer.
QImage PreviewWidget::renderFrame(const bs::Gray8& y, const bs::Rgb8* color) const {
  const int w = y.w, h = y.h;
  const bool colored = color && color->w == w && color->h == h;
  QImage img(w, h, QImage::Format_RGB888);

  if (outdoor) {
    // Percentile stretch on the luma, then hazard-striped edges: solid
    // yellow vanishes against bright sky and black against shadow, so
    // alternating them means one of the two always contrasts.
    std::vector<int> hist(256, 0);
    for (uint8_t v : y.px) ++hist[v];
    const double lo = percentile(hist, static_cast<long>(y.px.size()), 2.0);
    const double hi = percentile(hist, static_cast<long>(y.px.size()), 98.0);
    const double span = std::max(8.0, hi - lo);

    // Adjacent-difference gradient; a central difference draws a 3-4 px
    // band and smears exactly the fine detail being judged.
    std::vector<float> mag(static_cast<size_t>(w) * h, 0.f);
    for (int r = 0; r < h; ++r) {
      for (int c = 0; c < w; ++c) {
        float m = 0.f;
        const int v = y.at(c, r);
        if (c > 0) m += std::abs(v - y.at(c - 1, r));
        if (r > 0) m += std::abs(v - y.at(c, r - 1));
        mag[static_cast<size_t>(r) * w + c] = m;
      }
    }
    std::vector<float> sorted(mag);
    std::nth_element(sorted.begin(), sorted.begin() + static_cast<size_t>(sorted.size() * 0.96),
                     sorted.end());
    const double p96 = sorted[static_cast<size_t>(sorted.size() * 0.96)];
    const double thresh = std::max(4.0, p96 / std::max(outdoorStrength, 0.1));
    const int band = stripePx > 0 ? stripePx : 3;
    const bool edgesOnly = outdoorStyle == QStringLiteral("edges");

    for (int r = 0; r < h; ++r) {
      uchar* line = img.scanLine(r);
      for (int c = 0; c < w; ++c) {
        const float m = mag[static_cast<size_t>(r) * w + c];
        const bool stripe = ((r + c) / band) % 2 == 0;
        uchar rr, gg, bb;
        if (m > thresh) {
          if (stripe) { rr = 255; gg = 238; bb = 0; }
          else { rr = 0; gg = 0; bb = 0; }
        } else if (edgesOnly) {
          if (m > thresh * 0.45) { rr = 70; gg = 66; bb = 20; }
          else { rr = 10; gg = 12; bb = 14; }
        } else {
          const double s = std::clamp((y.at(c, r) - lo) * 255.0 / span, 0.0, 255.0);
          rr = gg = bb = static_cast<uchar>(s);
        }
        line[c * 3] = rr;
        line[c * 3 + 1] = gg;
        line[c * 3 + 2] = bb;
      }
    }
  } else {
    for (int r = 0; r < h; ++r) {
      uchar* line = img.scanLine(r);
      for (int c = 0; c < w; ++c) {
        const uchar v = y.at(c, r);
        uchar rr, gg, bb;
        if (colored) {
          const uint8_t* p = color->at(c, r);
          rr = p[0]; gg = p[1]; bb = p[2];
        } else {
          rr = gg = bb = v;
        }
        if (showZebra && v >= 250 && ((r + c) / 6) % 2 == 0) {
          rr = 255; gg = 0; bb = 255;  // ZEBRA_COLOR
        }
        line[c * 3] = rr;
        line[c * 3 + 1] = gg;
        line[c * 3 + 2] = bb;
      }
    }
    if (showPeaking) {
      for (int r = 1; r < h - 1; ++r) {
        uchar* line = img.scanLine(r);
        for (int c = 1; c < w - 1; ++c) {
          const int gx = y.at(c + 1, r) - y.at(c - 1, r);
          const int gy = y.at(c, r + 1) - y.at(c, r - 1);
          if (std::abs(gx) + std::abs(gy) > peakThreshold) {
            line[c * 3] = 255;  // PEAK_COLOR
            line[c * 3 + 1] = 40;
            line[c * 3 + 2] = 40;
          }
        }
      }
    }
  }
  return img;
}

void PreviewWidget::setFrame(const bs::Gray8& y, const bs::FrameStats& stats,
                             const bs::Rgb8* color) {
  if (y.empty()) return;
  srcW_ = y.w;
  srcH_ = y.h;
  image_ = renderFrame(y, color);
  stats_ = stats;
  haveStats_ = true;
  banner_.clear();
  update();
}

void PreviewWidget::setHud(const HudInfo& hud) { hud_ = hud; }

void PreviewWidget::setBanner(const QString& text) {
  banner_ = text;
  update();
}

void PreviewWidget::setBird(const QRect& bbox, const QString& label, bool take, double ttlS) {
  const double now = mono_now();
  if (take) birdTakeUntil_ = now + 1.6;
  birdBox_ = bbox;
  birdLabel_ = label;
  birdTake_ = take || now < birdTakeUntil_;
  birdExpiry_ = now + ttlS;
}

void PreviewWidget::setFocusMap(const bs::FocusMap& map) {
  fmap_ = map;
  haveFmap_ = map.rows > 0;
  fpeak_ = map.best_raw;
  fpeakHold_ = std::max(fpeakHold_ * 0.99, fpeak_);
}

void PreviewWidget::resetFocusPeak() { fpeakHold_ = fpeak_; }

QRect PreviewWidget::targetRect() const {
  if (image_.isNull()) return rect();
  const double scale =
      std::min(double(width()) / image_.width(), double(height()) / image_.height());
  const int w = static_cast<int>(image_.width() * scale);
  const int h = static_cast<int>(image_.height() * scale);
  return QRect((width() - w) / 2, (height() - h) / 2, w, h);
}

void PreviewWidget::paintEvent(QPaintEvent*) {
  QPainter p(this);
  p.fillRect(rect(), QColor(18, 18, 20));
  if (image_.isNull()) {
    p.setPen(QColor(160, 160, 165));
    p.setFont(theme::sans(11));
    p.drawText(rect(), Qt::AlignCenter, banner_.isEmpty() ? QStringLiteral("no signal") : banner_);
    return;
  }
  const QRect t = targetRect();
  p.drawImage(t, image_);

  if (showZones) {
    const int yLine = t.top() + static_cast<int>(t.height() * skyZoneFrac);
    QPen pen(QColor(90, 200, 255, 170));
    pen.setStyle(Qt::DashLine);
    p.setPen(pen);
    p.drawLine(t.left(), yLine, t.right(), yLine);
    p.setFont(theme::sans(8));
    p.drawText(t.left() + 6, yLine - 4, QStringLiteral("sky zone"));
    p.drawText(t.left() + 6, yLine + 12, QStringLiteral("subject zone"));
  }

  if (showGrid) {
    p.setPen(QColor(255, 255, 255, 60));
    for (int i = 1; i < 3; ++i) {
      p.drawLine(t.left() + t.width() * i / 3, t.top(), t.left() + t.width() * i / 3, t.bottom());
      p.drawLine(t.left(), t.top() + t.height() * i / 3, t.right(), t.top() + t.height() * i / 3);
    }
  }

  if (showFocusMap && haveFmap_) {
    const double tw = double(t.width()) / fmap_.cols;
    const double th = double(t.height()) / fmap_.rows;
    for (int r = 0; r < fmap_.rows; ++r) {
      for (int c = 0; c < fmap_.cols; ++c) {
        const double v = fmap_.energy[static_cast<size_t>(r) * fmap_.cols + c];
        if (v < 0.12) continue;
        QColor col;
        const int alpha = static_cast<int>(30 + 55 * v);
        if (v < 0.5) {
          const double f = v / 0.5;
          col = QColor(int(40 + 215 * f), int(90 + 110 * f), int(220 - 180 * f), alpha);
        } else {
          const double f = (v - 0.5) / 0.5;
          col = QColor(int(255 - 160 * f), 200, int(40 + 80 * f), alpha);
        }
        p.fillRect(QRectF(t.left() + c * tw, t.top() + r * th, tw, th), col);
      }
    }
    QPen best(QColor(120, 255, 150));
    best.setWidth(3);
    p.setPen(best);
    const QRectF br(t.left() + fmap_.best_col * tw, t.top() + fmap_.best_row * th, tw, th);
    p.drawRect(br);
    p.setFont(theme::sans(8, true));
    p.drawText(QPointF(br.left() + 3, br.bottom() - 4), QStringLiteral("sharpest"));
  }

  if (showPeaking) {
    // Focus is judged on a native-resolution centre crop; show it.
    const int side = std::min(t.width(), t.height()) / 4;
    QPen pen(QColor(255, 200, 40, 180));
    pen.setStyle(Qt::DotLine);
    p.setPen(pen);
    p.drawRect(t.center().x() - side, t.center().y() - side, 2 * side, 2 * side);
  }

  if (showSharpness && haveStats_) {
    const int bx = t.right() - 222, by = t.bottom() - 86;
    p.fillRect(bx, by, 210, 74, QColor(10, 10, 12, 190));
    p.setPen(QColor(255, 255, 255, 40));
    p.drawRect(bx, by, 210, 74);
    p.setPen(QColor(150, 150, 160));
    p.setFont(theme::sans(8));
    p.drawText(bx + 10, by + 16, QStringLiteral("FOCUS"));
    const double peak = std::max(fpeakHold_, 1e-6);
    const bool near = fpeak_ >= peak * 0.95 && fpeak_ > 0;
    p.setPen(near ? QColor(120, 255, 150) : QColor(235, 235, 240));
    p.setFont(theme::mono(22, true));
    p.drawText(bx + 10, by + 46, QString::number(stats_.sharpness_norm, 'f', 1));
    p.setPen(QColor(130, 130, 140));
    p.setFont(theme::sans(8));
    p.drawText(bx + 118, by + 46, QStringLiteral("peak %1").arg(peak, 0, 'f', 0));
    p.fillRect(bx + 10, by + 74 - 16, 190, 8, QColor(45, 45, 52));
    const double frac = std::min(1.0, fpeak_ / peak);
    p.fillRect(bx + 10, by + 74 - 16, static_cast<int>(190 * frac), 8,
               near ? QColor(120, 255, 150) : QColor(90, 160, 255));
  }

  if (showHud && hud_.valid) {
    const QFontMetrics fmBig(theme::mono(11, true));
    const QFontMetrics fmSmall(theme::mono(9));
    const int wBox = std::max({fmBig.horizontalAdvance(hud_.line1),
                               fmSmall.horizontalAdvance(hud_.line2),
                               fmSmall.horizontalAdvance(hud_.ae)}) + 18;
    const int hx = t.left() + 8, hy = t.top() + 8;
    p.fillRect(hx, hy, wBox, 78, QColor(0, 0, 0, 155));
    p.setPen(QColor(240, 240, 245));
    p.setFont(theme::mono(11, true));
    p.drawText(hx + 9, hy + 20, hud_.line1);
    p.setPen(QColor(175, 180, 190));
    p.setFont(theme::mono(9));
    p.drawText(hx + 9, hy + 38, hud_.line2);
    p.drawText(hx + 9, hy + 70, hud_.ae);
    p.setPen(theme::verdictColor(hud_.verdict));
    p.setFont(theme::sans(10, true));
    p.drawText(hx + 9, hy + 55, hud_.verdict);
  }

  // The countdown draws regardless of the HUD flag: during a timelapse the
  // whole question is whether it is still running and when the next frame
  // lands, and a scroll of the wheel should not hide that.
  if (hud_.countdown >= 0.0) {
    const double total = std::max(0.001, hud_.interval > 0 ? hud_.interval : 1.0);
    const double frac = std::clamp(hud_.countdown / total, 0.0, 1.0);
    const int cx = t.right() - 110, cy = t.top() + 14;
    p.setRenderHint(QPainter::Antialiasing, true);
    p.setPen(Qt::NoPen);
    p.setBrush(QColor(0, 0, 0, 165));
    p.drawEllipse(cx, cy, 96, 96);
    p.setBrush(Qt::NoBrush);
    QPen track(QColor(60, 70, 80));
    track.setWidth(6);
    p.setPen(track);
    p.drawEllipse(cx + 6, cy + 6, 84, 84);
    QPen arc(QColor(95, 208, 122));
    arc.setWidth(6);
    arc.setCapStyle(Qt::RoundCap);
    p.setPen(arc);
    p.drawArc(cx + 6, cy + 6, 84, 84, 90 * 16, -static_cast<int>(360 * 16 * (1 - frac)));
    p.setPen(QColor(240, 245, 250));
    p.setFont(theme::sans(20, true));
    p.drawText(QRect(cx, cy + 30, 96, 34), Qt::AlignCenter,
               QString::number(hud_.countdown, 'f', 1));
    p.setPen(QColor(160, 170, 180));
    p.setFont(theme::sans(8));
    p.drawText(QRect(cx, cy + 58, 96, 16), Qt::AlignCenter, QStringLiteral("s to next"));
    p.setRenderHint(QPainter::Antialiasing, false);
  }

  if (!birdBox_.isNull() && mono_now() < birdExpiry_) {
    const double sx = double(t.width()) / srcW_, sy = double(t.height()) / srcH_;
    QRect b(t.left() + static_cast<int>(birdBox_.left() * sx) - 6,
            t.top() + static_cast<int>(birdBox_.top() * sy) - 6,
            static_cast<int>(birdBox_.width() * sx) + 12,
            static_cast<int>(birdBox_.height() * sy) + 12);
    const bool green = birdTake_ || mono_now() < birdTakeUntil_;
    const QColor col = green ? QColor(127, 227, 162) : QColor(127, 208, 240);
    QPen pen(col);
    pen.setWidth(2);
    p.setPen(pen);
    p.drawRect(b);
    const QFontMetrics fm(theme::mono(10, true));
    const int lw = fm.horizontalAdvance(birdLabel_) + 12;
    const int ly = std::max(t.top() + 2, b.top() - 22);
    p.fillRect(b.left(), ly, lw, 18, QColor(0, 0, 0, 165));
    p.setPen(col);
    p.setFont(theme::mono(10, true));
    p.drawText(b.left() + 6, ly + 13, birdLabel_);
  }

  if (outdoor) {
    QPen pen(QColor(255, 230, 40));
    pen.setWidth(2);
    p.setPen(pen);
    p.drawRect(t.adjusted(1, 1, -2, -2));
    const QString label = outdoorStyle == QStringLiteral("edges")
                              ? QStringLiteral("OUTDOOR - EDGES ONLY")
                              : QStringLiteral("OUTDOOR - CONTRAST BOOST");
    p.setFont(theme::sans(11, true));
    const int tw = QFontMetrics(p.font()).horizontalAdvance(label);
    p.fillRect(t.right() - tw - 8 - 7, t.top() + 6, tw + 14, 22, QColor(0, 0, 0, 170));
    p.setPen(QColor(255, 230, 40));
    p.drawText(t.right() - tw - 8, t.top() + 22, label);
  }

  if (haveStats_ && stats_.verdict != "ok") {
    QColor col(200, 200, 200);
    if (stats_.verdict == "dark") col = QColor(70, 130, 255);
    else if (stats_.verdict == "blown") col = QColor(255, 90, 60);
    else if (stats_.verdict == "empty") col = QColor(200, 160, 40);
    QPen pen(col);
    pen.setWidth(3);
    p.setPen(pen);
    p.drawRect(t.adjusted(1, 1, -2, -2));
    p.setFont(theme::sans(10, true));
    p.drawText(t.left() + 10, t.top() + 22, QString::fromStdString(stats_.verdict).toUpper());
  }
}

void PreviewWidget::mouseDoubleClickEvent(QMouseEvent*) { emit doubleClicked(); }

void PreviewWidget::wheelEvent(QWheelEvent* e) {
  // One gesture over the image beats hunting four checkboxes on another
  // tab: wheel up = every overlay on, wheel down = a clean image.
  const int dy = e->angleDelta().y();
  if (dy != 0) emit overlaysToggled(dy > 0);
  e->accept();
}

// ------------------------------------------------------------ Histogram --

HistogramWidget::HistogramWidget(QWidget* parent) : QWidget(parent) {
  setMinimumHeight(74);
  setMaximumHeight(88);
  setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
  setFocusPolicy(Qt::StrongFocus);
  setCursor(Qt::SizeHorCursor);
  setToolTip(QStringLiteral(
      "Click left half = black point, right half = white point.\nArrow keys nudge the last one "
      "you set."));
}

void HistogramWidget::setFrame(const bs::Gray8& y, const bs::FrameStats& stats) {
  hist_.fill(0.0);
  for (uint8_t v : y.px) hist_[v] += 1.0;
  const double total = static_cast<double>(y.px.size());
  if (total > 0)
    for (double& h : hist_) h /= total;
  haveHist_ = true;
  stats_ = stats;
  update();
}

void HistogramWidget::setLevels(double black, double white) {
  black_ = black;
  white_ = white;
  update();
}

void HistogramWidget::pickAt(double pos) {
  active_ = pos < 0.5 ? QStringLiteral("black") : QStringLiteral("white");
  applyDrag(pos);
}

void HistogramWidget::applyDrag(double pos) {
  pos = std::clamp(pos, 0.0, 1.0);
  if (active_ == QStringLiteral("black"))
    black_ = std::min(pos, white_ - 0.02);
  else
    white_ = std::max(pos, black_ + 0.02);
  update();
  emit levelsChanged(black_, white_);
}

void HistogramWidget::nudge(double delta) {
  if (active_ == QStringLiteral("black"))
    black_ = std::clamp(black_ + delta, 0.0, white_ - 0.02);
  else
    white_ = std::clamp(white_ + delta, black_ + 0.02, 1.0);
  update();
  emit levelsChanged(black_, white_);
}

void HistogramWidget::mousePressEvent(QMouseEvent* e) {
  setFocus();
  pickAt(double(e->pos().x()) / std::max(1, width()));
}

void HistogramWidget::mouseMoveEvent(QMouseEvent* e) {
  if (e->buttons() & Qt::LeftButton) applyDrag(double(e->pos().x()) / std::max(1, width()));
}

void HistogramWidget::mouseDoubleClickEvent(QMouseEvent*) {
  black_ = 0.0;
  white_ = 1.0;
  update();
  emit levelsChanged(black_, white_);
}

void HistogramWidget::keyPressEvent(QKeyEvent* e) {
  const double step = (e->modifiers() & Qt::ShiftModifier) ? 0.05 : 0.01;
  switch (e->key()) {
    case Qt::Key_Left: nudge(-step); return;
    case Qt::Key_Right: nudge(step); return;
    case Qt::Key_Up:
    case Qt::Key_Down:
      active_ = active_ == QStringLiteral("black") ? QStringLiteral("white")
                                                   : QStringLiteral("black");
      update();
      return;
  }
  QWidget::keyPressEvent(e);
}

void HistogramWidget::paintEvent(QPaintEvent*) {
  QPainter p(this);
  p.fillRect(rect(), QColor(24, 24, 27));
  if (!haveHist_) return;
  const int w = width(), h = height();
  double peak = 1e-9;
  for (double v : hist_) peak = std::max(peak, v);
  for (int bin = 0; bin < 256; ++bin) {
    const int x = bin * w / 256;
    const int bw = std::max(1, (bin + 1) * w / 256 - x);
    // sqrt keeps the tails visible next to a huge midtone peak.
    const double scaled = std::sqrt(hist_[bin] / peak);
    const int bh = static_cast<int>(scaled * (h - 12));
    QColor col(190, 190, 195);
    if (bin >= 250) col = QColor(255, 80, 60);
    else if (bin <= 5) col = QColor(70, 130, 255);
    p.fillRect(x, h - bh - 2, bw, bh, col);
  }
  QPen tp(QColor(90, 230, 140));
  tp.setWidth(2);
  p.setPen(tp);
  const int tx = static_cast<int>(target * w / 256.0);
  p.drawLine(tx, 0, tx, h);

  // Levels: shade the excluded regions, draw the two markers.
  const int bx = static_cast<int>(black_ * w), wx = static_cast<int>(white_ * w);
  p.fillRect(0, 0, bx, h, QColor(0, 0, 0, 120));
  p.fillRect(wx, 0, w - wx, h, QColor(0, 0, 0, 120));
  auto marker = [&](int x, const QString& name, const QColor& col) {
    const bool act = active_ == name;
    QPen pen(col);
    pen.setWidth(act ? 3 : 2);
    p.setPen(pen);
    p.drawLine(x, 0, x, h);
    p.setFont(theme::sans(8, act));
    p.drawText(x + 4 > w - 46 ? x - 44 : x + 4, h - 4,
               act ? QStringLiteral("[%1]").arg(name) : name);
  };
  marker(bx, QStringLiteral("black"), QColor(70, 150, 255));
  marker(wx, QStringLiteral("white"), QColor(255, 190, 60));

  p.setPen(QColor(150, 150, 160));
  p.setFont(theme::mono(8));
  p.drawText(4, 12, QStringLiteral("p50 %1  p95 %2  clip %3%  meter %4")
                        .arg(static_cast<int>(stats_.p50))
                        .arg(static_cast<int>(stats_.p95))
                        .arg(stats_.clip_hi * 100, 0, 'f', 2)
                        .arg(stats_.meter, 0, 'f', 0));
}
