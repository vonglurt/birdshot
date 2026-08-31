// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The shared custom widgets, ported behavior-for-behavior from the
// prototype's widgets.py: the accordion section, the mode dial, the face
// bar, the fullscreen preview window and the blocking storage overlay.
#pragma once

#include <QColor>
#include <QFrame>
#include <QLabel>
#include <QStringList>
#include <QToolButton>
#include <QVBoxLayout>
#include <QWidget>

// A titled section that collapses to a single row. The summary line means a
// closed panel still tells you what it is set to; gating greys a section out
// with the reason instead of hiding it.
class Accordion : public QWidget {
  Q_OBJECT

 public:
  explicit Accordion(const QString& title, bool expanded = false, QWidget* parent = nullptr);

  void addWidget(QWidget* w);
  void addLayout(QLayout* l);
  void setSummary(const QString& text);
  void setGated(const QString& reason);  // empty = clear the gate
  void setExpanded(bool open);
  bool isExpanded() const { return header_->isChecked(); }
  QWidget* body() { return body_; }
  QString title() const { return header_->text(); }

 signals:
  void toggledOpen(bool open);

 private:
  void applyOpen(bool open);
  QToolButton* header_;
  QLabel* summary_;
  QFrame* body_;
  QVBoxLayout* bodyLayout_;
  QString summaryText_;
  bool gated_ = false;
};

// The mode dial: every mode visible at once, the current one lit, flanked
// by step arrows that skip unavailable modes.
class ModeTuner : public QWidget {
  Q_OBJECT

 public:
  ModeTuner(const QStringList& labels, int current, int fontPx = 14, QWidget* parent = nullptr);

  int index() const { return index_; }
  void setIndex(int i);                       // clamps; no-op (no signal) when unchanged
  void setAvailable(const QStringList& why);  // empty string = available; else the reason
  void step(int delta);                       // skips unavailable, wraps

 signals:
  void changed(int index);

 private:
  void restyle();
  QList<QToolButton*> buttons_;
  QStringList why_;
  int index_ = 0;
  int fontPx_;
};

// The 44 px title bar: wordmark, version, and the segmented face switcher.
class FaceBar : public QWidget {
  Q_OBJECT

 public:
  explicit FaceBar(const QString& version, QWidget* parent = nullptr);
  void setActive(const QString& face);

 signals:
  void facePicked(QString face);

 private:
  QList<QToolButton*> buttons_;
};

// Full-window notice for conditions the user must actually deal with --
// every storage tier full. Swallows clicks; Esc dismisses it only once
// space is actually available (the window decides that).
class BlockingOverlay : public QWidget {
  Q_OBJECT

 public:
  explicit BlockingOverlay(QWidget* parent);
  void showMessage(const QString& title, const QString& detail, const QColor& accent = {});

 protected:
  void paintEvent(QPaintEvent* e) override;

 private:
  QString title_ = QStringLiteral("OUT OF SPACE");
  QString detail_;
  QColor accent_ = QColor(200, 60, 40);
};

// A separate window for fullscreen rather than hiding the panels, so the
// main window carries on running. Esc/F11/double-click closes.
class FullscreenPreview : public QWidget {
  Q_OBJECT

 public:
  explicit FullscreenPreview(QWidget* preview, QWidget* parent = nullptr);

 signals:
  void closed();

 protected:
  void keyPressEvent(QKeyEvent* e) override;
  void mouseDoubleClickEvent(QMouseEvent* e) override;
  void closeEvent(QCloseEvent* e) override;
};
