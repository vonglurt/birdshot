// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The live preview canvas and the histogram, ported from the prototype's
// preview.py. Drawn by hand because the overlays are the point: with a
// manual-focus lens and a subject that is usually a small dark shape
// against bright sky, focus peaking and clipping zebras are the only
// reliable way to judge a shot on a 640-pixel preview.
#pragma once

#include <array>

#include <QImage>
#include <QWidget>

#include "birdshot/analysis.hpp"
#include "birdshot/image.hpp"

struct HudInfo {
  bool valid = false;
  QString line1;      // "1/500 s (2.0 ms)   g1.50   s500"
  QString line2;      // "8000 lux   14.9 fps   clip 0.12%"
  QString ae;         // "pid err +0.12 out -0.04 EV"
  QString verdict;    // lower-case verdict word
  double countdown = -1.0;  // >= 0 while a timelapse waits
  double interval = 0.0;
};

class PreviewWidget : public QWidget {
  Q_OBJECT

 public:
  explicit PreviewWidget(QWidget* parent = nullptr);

  // Overlay flags, exactly the prototype's set.
  bool showZebra = true;
  bool showPeaking = false;
  bool showZones = true;
  bool showGrid = false;
  bool showFocusMap = false;
  bool showSharpness = false;
  bool showHud = true;
  bool outdoor = false;
  QString outdoorStyle = QStringLiteral("boost");  // boost | edges
  double outdoorStrength = 1.0;
  int stripePx = 3;
  double skyZoneFrac = 0.40;
  double peakThreshold = 28.0;

  void setFrame(const bs::Gray8& y, const bs::FrameStats& stats,
                const bs::Rgb8* color = nullptr);
  void setHud(const HudInfo& hud);
  void setBanner(const QString& text);
  void setBird(const QRect& bboxLuma, const QString& label, bool take, double ttlS = 1.6);
  void setFocusMap(const bs::FocusMap& map);
  void resetFocusPeak();
  double focusPeakHold() const { return fpeakHold_; }
  void copyViewSettings(const PreviewWidget& other);

 signals:
  void doubleClicked();
  void overlaysToggled(bool on);

 protected:
  void paintEvent(QPaintEvent* e) override;
  void mouseDoubleClickEvent(QMouseEvent* e) override;
  void wheelEvent(QWheelEvent* e) override;

 private:
  QRect targetRect() const;
  // The display image: the colour plane when one exists, greyscale luma
  // otherwise, with outdoor / zebra / peaking baked into the pixels.
  QImage renderFrame(const bs::Gray8& y, const bs::Rgb8* color) const;

  QImage image_;
  bs::FrameStats stats_;
  bool haveStats_ = false;
  QString banner_ = QStringLiteral("waiting for camera...");
  HudInfo hud_;
  int srcW_ = 640, srcH_ = 480;

  bs::FocusMap fmap_;
  bool haveFmap_ = false;
  double fpeak_ = 0.0, fpeakHold_ = 0.0;

  QRect birdBox_;
  QString birdLabel_;
  bool birdTake_ = false;
  double birdExpiry_ = 0.0;
  double birdTakeUntil_ = 0.0;
};

class HistogramWidget : public QWidget {
  Q_OBJECT

 public:
  explicit HistogramWidget(QWidget* parent = nullptr);

  double target = 118.0;
  void setFrame(const bs::Gray8& y, const bs::FrameStats& stats);
  void setLevels(double black, double white);

 signals:
  void levelsChanged(double black, double white);

 protected:
  void paintEvent(QPaintEvent* e) override;
  void mousePressEvent(QMouseEvent* e) override;
  void mouseMoveEvent(QMouseEvent* e) override;
  void mouseDoubleClickEvent(QMouseEvent* e) override;
  void keyPressEvent(QKeyEvent* e) override;

 private:
  void pickAt(double pos);
  void applyDrag(double pos);
  void nudge(double delta);

  std::array<double, 256> hist_{};
  bool haveHist_ = false;
  bs::FrameStats stats_;
  double black_ = 0.0, white_ = 1.0;
  QString active_ = QStringLiteral("white");
};
