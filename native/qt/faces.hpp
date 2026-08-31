// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The four faces from the prototype's faces.py: Camera (a plain camera
// app), Field (the instrument outdoors, with the Bird Flight gate ladder),
// Bench (built by the main window) and Library (the darkroom). Each face
// borrows the one shared preview widget when it is frontmost.
#pragma once

#include <QComboBox>
#include <QLabel>
#include <QListWidget>
#include <QProgressBar>
#include <QPushButton>
#include <QToolButton>
#include <QVBoxLayout>
#include <QWidget>

#include "birdshot/birdflight.hpp"
#include "birdshot/json.hpp"

class MainWindow;
class ModeTuner;

// The Bird Flight ladder: when the mode holds fire, this shows exactly
// which rung it failed on, with the number, so tuning gates is reading
// rather than guessing.
class GateLadder : public QWidget {
  Q_OBJECT

 public:
  explicit GateLadder(MainWindow* win, QWidget* parent = nullptr);
  void updateSighting(const bs::Sighting& s, qint64 takeN, bool fired);
  void setIdle(const QString& text);

 private:
  void refreshThresholds();
  MainWindow* win_;
  struct Row {
    QLabel* val;
    QLabel* thr;
    QLabel* tick;
  };
  QList<Row> rows_;
  QLabel* footer_;
  double takeUntil_ = 0.0;
};

class CameraFace : public QWidget {
  Q_OBJECT

 public:
  explicit CameraFace(MainWindow* win, QWidget* parent = nullptr);
  QVBoxLayout* previewSlot = nullptr;
  QComboBox* cmbCamera = nullptr;
  ModeTuner* tuner = nullptr;

  void updateGo(bool running, const QString& state, const QString& label);
  void syncMode(int idx);
  void onFrameSaved(const QString& path);

 private:
  void styleShutter(bool running);
  void shutterClicked();
  MainWindow* win_;
  QPushButton* btnThumb_;
  QPushButton* btnShutter_;
  double lastThumb_ = 0.0;
};

class FieldFace : public QWidget {
  Q_OBJECT

 public:
  explicit FieldFace(MainWindow* win, QWidget* parent = nullptr);
  QVBoxLayout* previewSlot = nullptr;
  ModeTuner* tuner = nullptr;
  GateLadder* ladder = nullptr;

  void updateGo(bool running, const QString& state, const QString& label);
  void syncMode(int idx);
  void setCameraLabel(const QString& text);
  void setOutdoor(bool on, int styleIdx);
  void refreshStatus(const QString& sessionText, const QString& freeText);
  void onSighting(const bs::Sighting& s, qint64 takeN, bool fired);

 private:
  void styleState(const QString& state);
  void styleOutdoor(bool on);
  void restyleStyleButtons(int active);

  MainWindow* win_;
  QLabel* lblState_;
  QLabel* lblMode_;
  QLabel* lblCamera_;
  QLabel* lblTakes_;
  QLabel* lblSession_;
  QLabel* lblFree_;
  QPushButton* btnGo_;
  QToolButton* btnOutdoor_;
  QToolButton* btnBoost_;
  QToolButton* btnEdges_;
};

class LibraryFace : public QWidget {
  Q_OBJECT

 public:
  explicit LibraryFace(MainWindow* win, QWidget* parent = nullptr);
  void refresh();

 private:
  struct Entry {
    QString file;      // absolute path
    QString verdict;
    double sharpness = 0.0;
    double clipHi = 0.0;
    bool focusMeasured = false;
    qint64 shutterUs = 0;
    double gain = 1.0;
    bool hasBird = false;
    double birdSharp = 0.0, birdArea = 0.0, birdRing = 0.0, birdSky = 0.0, birdMotion = 0.0;
  };
  void restyleFilters();
  void sessionPicked();
  void reloadGrid();
  void framePicked();
  void loadMoreThumbs();
  void useAsReference();
  void openFile();
  void deleteFile();
  QList<Entry> readSession(const QString& dir) const;

  MainWindow* win_;
  QString filter_ = QStringLiteral("all");
  QList<QToolButton*> filterButtons_;
  QListWidget* lstSessions_;
  QListWidget* grid_;
  QLabel* lblBig_;
  QLabel* lblFacts_;
  QLabel* lblTrigger_;
  QPushButton* btnRef_;
  QString sessionPath_;
  QList<Entry> entries_;
  int thumbNext_ = 0;
  QTimer* thumbTimer_;
};
