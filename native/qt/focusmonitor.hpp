// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The 1:1 focus monitor: a frameless, always-on-top window showing real
// sensor pixels from the frame centre at 100-400%, with peaking and a
// peak-hold score. Three things make manual focus on this rig hard: the
// lens has no feedback, the preview is a downscale that hides real
// softness, and the subject is usually a small dark shape against bright
// sky. Turn the ring until the number stops climbing.
#pragma once

#include <QComboBox>
#include <QImage>
#include <QLabel>
#include <QPoint>
#include <QWidget>

#include "capture.hpp"

class OnePixelView : public QWidget {
  Q_OBJECT

 public:
  explicit OnePixelView(QWidget* parent = nullptr);
  bool showPeaking = true;
  double zoom = 1.0;
  void setCrop(const bs::Gray8& crop);
  void setMessage(const QString& text);

 protected:
  void paintEvent(QPaintEvent* e) override;

 private:
  QImage image_;
  QString msg_ = QStringLiteral("starting focus assist...");
};

class FocusBar : public QWidget {
  Q_OBJECT

 public:
  explicit FocusBar(QWidget* parent = nullptr);
  void setValue(double v);
  void resetPeak();

 protected:
  void paintEvent(QPaintEvent* e) override;

 private:
  double value_ = 0.0, peak_ = 0.0, peakAt_ = 0.0, scale_ = 60.0;
};

class FocusMonitor : public QWidget {
  Q_OBJECT

 public:
  explicit FocusMonitor(QWidget* parent = nullptr);
  void handleFrame(const FramePacket& p);

 protected:
  void mousePressEvent(QMouseEvent* e) override;
  void mouseMoveEvent(QMouseEvent* e) override;

 private:
  OnePixelView* view_;
  FocusBar* meter_;
  QLabel* info_;
  QPoint dragOffset_;
};
