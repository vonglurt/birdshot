// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "focusmonitor.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>

#include <QCheckBox>
#include <QHBoxLayout>
#include <QMouseEvent>
#include <QPainter>
#include <QPushButton>
#include <QVBoxLayout>

#include "birdshot/naming.hpp"
#include "theme.hpp"

namespace {

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

constexpr double kPeakDecayS = 6.0;

}  // namespace

// ---------------------------------------------------------- OnePixelView --

OnePixelView::OnePixelView(QWidget* parent) : QWidget(parent) {
  setMinimumSize(320, 320);
  setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
}

void OnePixelView::setCrop(const bs::Gray8& crop) {
  if (crop.empty()) return;
  QImage img(crop.w, crop.h, QImage::Format_RGB888);
  for (int r = 0; r < crop.h; ++r) {
    uchar* line = img.scanLine(r);
    for (int c = 0; c < crop.w; ++c) {
      const uchar v = crop.at(c, r);
      line[c * 3] = line[c * 3 + 1] = line[c * 3 + 2] = v;
    }
  }
  if (showPeaking) {
    for (int r = 1; r < crop.h - 1; ++r) {
      uchar* line = img.scanLine(r);
      for (int c = 1; c < crop.w - 1; ++c) {
        const int gx = crop.at(c + 1, r) - crop.at(c - 1, r);
        const int gy = crop.at(c, r + 1) - crop.at(c, r - 1);
        if (std::abs(gx) + std::abs(gy) > 40) {
          line[c * 3] = 255;
          line[c * 3 + 1] = 40;
          line[c * 3 + 2] = 40;
        }
      }
    }
  }
  image_ = img;
  msg_.clear();
  update();
}

void OnePixelView::setMessage(const QString& text) {
  msg_ = text;
  update();
}

void OnePixelView::paintEvent(QPaintEvent*) {
  QPainter p(this);
  p.fillRect(rect(), QColor(12, 12, 14));
  if (image_.isNull()) {
    p.setPen(QColor(150, 150, 155));
    p.drawText(rect(), Qt::AlignCenter, msg_);
    return;
  }
  const int side = std::min(width(), height());
  const QRect target((width() - side) / 2, (height() - side) / 2, side, side);
  const int srcSide = std::max(
      16, static_cast<int>(std::min(image_.width(), image_.height()) / std::max(zoom, 0.05)));
  const QRect src((image_.width() - srcSide) / 2, (image_.height() - srcSide) / 2, srcSide,
                  srcSide);
  p.drawImage(target, image_, src);
  p.setPen(QColor(255, 255, 255, 50));
  p.drawRect(target);
  p.setFont(theme::mono(8));
  p.setPen(QColor(180, 180, 190));
  p.drawText(target.left() + 6, target.bottom() - 6,
             QStringLiteral("%1 x %1 native px  @ %2%")
                 .arg(srcSide)
                 .arg(zoom * 100, 0, 'f', 0));
}

// -------------------------------------------------------------- FocusBar --

FocusBar::FocusBar(QWidget* parent) : QWidget(parent) {
  setMinimumHeight(56);
  setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
}

void FocusBar::setValue(double v) {
  value_ = v;
  const double now = mono_now();
  if (v >= peak_ || now - peakAt_ > kPeakDecayS) {
    peak_ = v;
    peakAt_ = now;
  }
  scale_ = std::max({scale_, peak_ * 1.15, 20.0});
  update();
}

void FocusBar::resetPeak() {
  peak_ = value_;
  peakAt_ = mono_now();
  scale_ = std::max(value_ * 1.5, 20.0);
  update();
}

void FocusBar::paintEvent(QPaintEvent*) {
  QPainter p(this);
  p.fillRect(rect(), QColor(20, 20, 23));
  const int barH = 16, top = height() - 20;
  p.fillRect(0, top, width(), barH, QColor(38, 38, 44));
  // Near the best reading so far: the cue to stop turning the ring.
  const bool near = peak_ > 0 && value_ >= peak_ * 0.95;
  const int fill = static_cast<int>(width() * std::min(1.0, value_ / scale_));
  p.fillRect(0, top, fill, barH, near ? QColor(95, 208, 122) : QColor(90, 160, 255));
  const int px = static_cast<int>(width() * std::min(1.0, peak_ / scale_));
  p.fillRect(px - 1, top - 3, 2, barH + 6, QColor(255, 210, 60));
  p.setFont(theme::mono(17, true));
  p.setPen(QColor(235, 235, 240));
  p.drawText(6, top - 8, QString::number(value_, 'f', 1));
  p.setFont(theme::sans(9));
  p.setPen(QColor(150, 150, 160));
  p.drawText(width() - 130, top - 8, QStringLiteral("best %1").arg(peak_, 0, 'f', 1));
}

// ---------------------------------------------------------- FocusMonitor --

FocusMonitor::FocusMonitor(QWidget* parent)
    : QWidget(parent, Qt::Tool | Qt::FramelessWindowHint | Qt::WindowStaysOnTopHint) {
  setWindowTitle(QStringLiteral("birdshot focus monitor"));
  resize(430, 620);
  setStyleSheet(
      "QWidget{background:#141416;color:#ddd;}"
      "QPushButton{background:#2a2a30;border:1px solid #3a3a44;padding:4px 8px;"
      "border-radius:4px;}"
      "QPushButton:hover{background:#35353d;}");

  auto* v = new QVBoxLayout(this);
  v->setContentsMargins(8, 6, 8, 8);

  auto* titleRow = new QHBoxLayout;
  auto* title = new QLabel(QStringLiteral("  FOCUS MONITOR"));
  title->setStyleSheet("font-weight:700;letter-spacing:1px;color:#8ab;");
  titleRow->addWidget(title, 1);
  auto* btnClose = new QPushButton(QStringLiteral("x"));
  btnClose->setFixedWidth(28);
  connect(btnClose, &QPushButton::clicked, this, &QWidget::close);
  titleRow->addWidget(btnClose);
  v->addLayout(titleRow);

  view_ = new OnePixelView;
  v->addWidget(view_, 1);
  meter_ = new FocusBar;
  v->addWidget(meter_);

  auto* controls = new QHBoxLayout;
  controls->addWidget(new QLabel(QStringLiteral("zoom")));
  auto* cmbZoom = new QComboBox;
  cmbZoom->addItem(QStringLiteral("100%"), 1.0);
  cmbZoom->addItem(QStringLiteral("200%"), 2.0);
  cmbZoom->addItem(QStringLiteral("400%"), 4.0);
  cmbZoom->addItem(QStringLiteral("fit"), 0.5);
  connect(cmbZoom, QOverload<int>::of(&QComboBox::currentIndexChanged), this,
          [this, cmbZoom](int i) { view_->zoom = cmbZoom->itemData(i).toDouble(); });
  controls->addWidget(cmbZoom);
  auto* chkPeak = new QCheckBox(QStringLiteral("peaking"));
  chkPeak->setChecked(true);
  connect(chkPeak, &QCheckBox::toggled, this,
          [this](bool on) { view_->showPeaking = on; });
  controls->addWidget(chkPeak);
  controls->addStretch(1);
  auto* btnReset = new QPushButton(QStringLiteral("reset best"));
  connect(btnReset, &QPushButton::clicked, this, [this] { meter_->resetPeak(); });
  controls->addWidget(btnReset);
  v->addLayout(controls);

  info_ = new QLabel(QStringLiteral("-"));
  info_->setStyleSheet("font-family:monospace;font-size:11px;color:#9a9aa5;");
  v->addWidget(info_);
  auto* hint = new QLabel(QStringLiteral(
      "Turn the focus ring until the number stops rising.\nThe yellow marker is the best "
      "reading so far."));
  hint->setStyleSheet("color:#70707a;font-size:11px;");
  v->addWidget(hint);
}

void FocusMonitor::handleFrame(const FramePacket& p) {
  if (!p.y) return;
  // The crop comes from the native plane when the backend delivers one --
  // real sensor pixels, which is the whole point of this window.
  const bs::Gray8& base = p.full ? *p.full : *p.y;
  view_->setCrop(base.centre_crop(512));
  meter_->setValue(p.stats.sharpness_norm);
  info_->setText(QStringLiteral("shutter %1 gain %2  p50 %3  clip %4%  tiles %5")
                     .arg(QString::fromStdString(bs::describe_shutter(p.exposureUs)), -12)
                     .arg(p.gain, 4, 'f', 2)
                     .arg(static_cast<int>(p.stats.p50))
                     .arg(p.stats.clip_hi * 100, 0, 'f', 2)
                     .arg(p.stats.contrast_tiles));
}

void FocusMonitor::mousePressEvent(QMouseEvent* e) {
  dragOffset_ = e->globalPosition().toPoint() - frameGeometry().topLeft();
}

void FocusMonitor::mouseMoveEvent(QMouseEvent* e) {
  if (e->buttons() & Qt::LeftButton)
    move(e->globalPosition().toPoint() - dragOffset_);
}
