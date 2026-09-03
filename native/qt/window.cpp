// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "window.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <thread>

#include <QAction>
#include <QApplication>
#include <QScreen>
#include <QCheckBox>
#include <QCompleter>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QInputDialog>
#include <QListWidget>
#include <QMessageBox>
#include <QPushButton>
#include <QRegularExpression>
#include <QSpinBox>
#include <QSplitter>
#include <QDateTime>
#include <QStandardPaths>
#include <QStatusBar>
#include <QStringListModel>
#include <QTime>
#include <QUrl>

#include <QFile>

#include "birdshot/naming.hpp"
#include "birdshot/solar.hpp"
#include "birdshot/storage.hpp"
#include "birdshot/version.hpp"
#include "theme.hpp"

namespace fs = std::filesystem;

namespace {

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

// Keys that never ride a profile: machine identity and machine paths, so a
// profile can move between installs without pointing capture at a missing
// disk.
const char* kMachineKeys[] = {"version",   "state",         "calibration", "ui_face",
                              "data_root", "usb_root",      "cascade_tiers", "min_free_mb"};

bool isMachineKey(const std::string& k) {
  for (const char* m : kMachineKeys)
    if (k == m) return true;
  return false;
}

QString jsonRepr(const bs::Json& v) { return QString::fromStdString(v.dump()); }

}  // namespace

MainWindow::MainWindow(bs::Config& cfg, const QString& face) : cfg_(cfg) {
  setWindowTitle(QStringLiteral("birdshot"));
  resize(1400, 860);
  counts_ = {{QStringLiteral("ok"), 0},
             {QStringLiteral("dark"), 0},
             {QStringLiteral("blown"), 0},
             {QStringLiteral("empty"), 0}};

  modes_ = {
      {QStringLiteral("Stills"), QStringLiteral("stills"),
       QStringLiteral("full pipeline: quality gates, s<N> folders"), bs::Mode::Collect},
      {QStringLiteral("Rapid"), QStringLiteral("rapid"),
       QStringLiteral("fastest: flat YYYYMMDDHHMMSScc names, no gates"), bs::Mode::Rapid},
      {QStringLiteral("Timelapse"), QStringLiteral("timelapse"),
       QStringLiteral("one frame every N seconds"), bs::Mode::Timelapse},
      {QStringLiteral("Video"), QStringLiteral("video"),
       QStringLiteral("H.264 to MP4, hardware encoder"), bs::Mode::Collect},
      {QStringLiteral("Bird Flight"), QStringLiteral("birdflight"),
       QStringLiteral("watch the sky; burst when a bird is sharp against it"),
       bs::Mode::BirdFlight},
  };

  capture_ = new CaptureController(cfg_, this);
  connect(capture_, &CaptureController::frameReady, this, &MainWindow::onFrame);
  connect(capture_, &CaptureController::sightingReady, this, &MainWindow::onSighting);
  connect(capture_, &CaptureController::recordingStarted, this, &MainWindow::onRecordingStarted);
  connect(capture_, &CaptureController::recordingFinished, this,
          &MainWindow::onRecordingFinished);
  connect(capture_, &CaptureController::logLine, this, &MainWindow::log);

  // --- shell ---
  auto* central = new QWidget;
  auto* cv = new QVBoxLayout(central);
  cv->setContentsMargins(0, 0, 0, 0);
  cv->setSpacing(0);

  facebar_ = new FaceBar(QStringLiteral("v") + QString::fromLatin1(bs::kVersion));
  connect(facebar_, &FaceBar::facePicked, this, &MainWindow::setFace);
  cv->addWidget(facebar_);

  preview_ = new PreviewWidget;
  connect(preview_, &PreviewWidget::doubleClicked, this, &MainWindow::toggleFullscreen);
  connect(preview_, &PreviewWidget::overlaysToggled, this, &MainWindow::setAllOverlays);
  histogram_ = new HistogramWidget;

  stack_ = new QStackedWidget;
  faceCamera_ = new CameraFace(this);
  faceField_ = new FieldFace(this);
  faceLibrary_ = new LibraryFace(this);
  stack_->addWidget(faceCamera_);
  stack_->addWidget(faceField_);
  stack_->addWidget(buildBench());
  stack_->addWidget(faceLibrary_);
  cv->addWidget(stack_, 1);
  setCentralWidget(central);

  overlay_ = new BlockingOverlay(this);

  // status bar
  auto* status = statusBar();
  btnDoctorChip_ = new QPushButton(QStringLiteral("doctor: ..."));
  btnDoctorChip_->setFlat(true);
  btnDoctorChip_->setCursor(Qt::PointingHandCursor);
  btnDoctorChip_->setStyleSheet(
      "QPushButton{border:none;background:transparent;color:#888;font-family:monospace;"
      "font-size:11px;}");
  connect(btnDoctorChip_, &QPushButton::clicked, this, [this] {
    setFace(QStringLiteral("bench"));
    selectTab(QStringLiteral("Machine"));
    if (sections_.contains(QStringLiteral("Install health - doctor")))
      sections_[QStringLiteral("Install health - doctor")]->setExpanded(true);
  });
  status->addPermanentWidget(btnDoctorChip_);
  lblStateBar_ = new QLabel(QStringLiteral("IDLE"));
  status->addPermanentWidget(lblStateBar_);
  lblFreeBar_ = new QLabel(QStringLiteral("-"));
  status->addPermanentWidget(lblFreeBar_);

  // shortcuts
  auto addShortcut = [this](const QKeySequence& seq, std::function<void()> fn) {
    auto* a = new QAction(this);
    a->setShortcut(seq);
    connect(a, &QAction::triggered, this, [fn] { fn(); });
    addAction(a);
  };
  addShortcut(Qt::Key_F11, [this] { toggleFullscreen(); });
  addShortcut(Qt::Key_BracketLeft, [this] { stepMode(-1); });
  addShortcut(Qt::Key_BracketRight, [this] { stepMode(+1); });
  const QStringList faceNames{QStringLiteral("camera"), QStringLiteral("field"),
                              QStringLiteral("bench"), QStringLiteral("library")};
  for (int i = 0; i < 4; ++i)
    addShortcut(QKeySequence(QStringLiteral("Ctrl+%1").arg(i + 1)),
                [this, faceNames, i] { setFace(faceNames[i]); });
  addShortcut(Qt::Key_Escape, [this] { dismissOverlay(); });

  connect(faceCamera_->cmbCamera, QOverload<int>::of(&QComboBox::activated), this,
          &MainWindow::switchCamera);
  const bool haveFfmpeg =
      !QStandardPaths::findExecutable(QStringLiteral("ffmpeg")).isEmpty();
  faceLibrary_->adoptEncodePage(
      buildEncodePage(),
      haveFfmpeg ? QString() : QStringLiteral("ffmpeg is not installed on this machine"));
  refreshEncodeSources();
  populateCameras();
  indexSettings();
  applyCapabilities();
  modeChanged(static_cast<int>(cfg_.num("shoot_mode", 0)));
  refreshProfiles();

  static const QStringList kFaces{QStringLiteral("camera"), QStringLiteral("field"),
                                  QStringLiteral("bench"), QStringLiteral("library")};
  setFace(kFaces.contains(face) ? face : QStringLiteral("bench"));

  capture_->startPreview();

  tick_ = new QTimer(this);
  tick_->setInterval(1000);
  connect(tick_, &QTimer::timeout, this, &MainWindow::refreshStatusTick);
  tick_->start();
  QTimer::singleShot(3000, this, &MainWindow::runDoctor);
  outdoorChanged(cfg_.boolean("outdoor_mode", false));
}

MainWindow::~MainWindow() = default;

QSet<QString> MainWindow::caps() const {
  const QStringList caps = capture_->capabilities();
  return QSet<QString>(caps.begin(), caps.end());
}

QString MainWindow::stateName() const {
  if (!capture_->recording()) return QStringLiteral("idle");
  switch (capture_->mode()) {
    case bs::Mode::Rapid: return QStringLiteral("rapid");
    case bs::Mode::Timelapse: return QStringLiteral("timelapse");
    case bs::Mode::BirdFlight: return QStringLiteral("birdflight");
    default: return QStringLiteral("burst");
  }
}

// ----------------------------------------------------------- face switch --

void MainWindow::setFace(const QString& nameIn) {
  static const QStringList kFaces{QStringLiteral("camera"), QStringLiteral("field"),
                                  QStringLiteral("bench"), QStringLiteral("library")};
  const QString name = kFaces.contains(nameIn) ? nameIn : QStringLiteral("bench");

  // The Camera face is a plain camera app: overlays off on entry, restored
  // on the way out.
  if (name == QStringLiteral("camera") && currentFace_ != QStringLiteral("camera")) {
    stash_ = {true,
              preview_->showHud,
              preview_->showZones,
              preview_->showGrid,
              preview_->showZebra,
              preview_->showPeaking,
              preview_->showFocusMap,
              preview_->showSharpness};
    preview_->showHud = preview_->showZones = preview_->showGrid = false;
    preview_->showZebra = false;
    preview_->showPeaking = preview_->showFocusMap = preview_->showSharpness = false;
  } else if (name != QStringLiteral("camera") && currentFace_ == QStringLiteral("camera") &&
             stash_.valid) {
    preview_->showHud = stash_.hud;
    preview_->showZones = stash_.zones;
    preview_->showGrid = stash_.grid;
    preview_->showZebra = stash_.zebra;
    preview_->showPeaking = stash_.peaking;
    preview_->showFocusMap = stash_.fmap;
    preview_->showSharpness = stash_.sharp;
    stash_.valid = false;
  }

  currentFace_ = name;
  stack_->setCurrentIndex(static_cast<int>(kFaces.indexOf(name)));
  facebar_->setActive(name);

  // One preview widget, moved between hosts -- no face pays for a second
  // per-frame pipeline.
  preview_->setParent(nullptr);
  if (name == QStringLiteral("bench")) {
    benchPreviewLayout_->insertWidget(1, preview_, 1);
    preview_->setVisible(true);
  } else if (name == QStringLiteral("camera")) {
    faceCamera_->previewSlot->insertWidget(0, preview_, 1);
    preview_->setVisible(true);
  } else if (name == QStringLiteral("field")) {
    faceField_->previewSlot->insertWidget(0, preview_, 1);
    preview_->setVisible(true);
  } else {
    faceLibrary_->refresh();
  }
  refreshGoButton();
}

bool MainWindow::selectTab(const QString& nameIn) {
  QString name = nameIn.trimmed().toLower();
  if (name == QStringLiteral("process") || name == QStringLiteral("encode")) {
    setFace(QStringLiteral("library"));
    return true;
  }
  static const QStringList kFaces{QStringLiteral("camera"), QStringLiteral("field"),
                                  QStringLiteral("bench"), QStringLiteral("library")};
  if (kFaces.contains(name)) {
    setFace(name);
    return true;
  }
  static const QMap<QString, QString> aliases{
      {QStringLiteral("image"), QStringLiteral("scene")},
      {QStringLiteral("storage"), QStringLiteral("machine")},
      {QStringLiteral("cascade"), QStringLiteral("machine")},
      {QStringLiteral("focus"), QStringLiteral("scene")},
      {QStringLiteral("exposure"), QStringLiteral("scene")},
      {QStringLiteral("quality"), QStringLiteral("scene")},
      {QStringLiteral("rapid"), QStringLiteral("shoot")},
      {QStringLiteral("capture"), QStringLiteral("shoot")}};
  name = aliases.value(name, name);
  for (int i = 0; i < tabs_->count(); ++i) {
    if (tabs_->tabText(i).toLower() == name) {
      setFace(QStringLiteral("bench"));
      tabs_->setCurrentIndex(i);
      return true;
    }
  }
  return false;
}

// ----------------------------------------------------------------- bench --

QWidget* MainWindow::buildBench() {
  auto* split = new QSplitter(Qt::Horizontal);

  auto* left = new QWidget;
  benchPreviewLayout_ = new QVBoxLayout(left);
  benchPreviewLayout_->setContentsMargins(6, 6, 6, 6);
  benchPreviewLayout_->setSpacing(3);

  lblBanner_ = new QLabel;
  lblBanner_->setWordWrap(true);
  lblBanner_->setStyleSheet(
      "background:#7a4a10;color:#ffe8c0;padding:7px;border-radius:4px;font-weight:600;"
      "font-family:monospace;");
  lblBanner_->hide();
  benchPreviewLayout_->addWidget(lblBanner_);
  benchPreviewLayout_->addWidget(preview_, 1);
  benchPreviewLayout_->addWidget(histogram_);
  connect(histogram_, &HistogramWidget::levelsChanged, this, [this](double b, double w) {
    cfg_.set("tone_black", bs::Json(std::round(b * 10000) / 10000.0));
    cfg_.set("tone_white", bs::Json(std::round(w * 10000) / 10000.0));
    saveCfg();
    statusBar()->showMessage(QStringLiteral("levels  black %1  white %2")
                                 .arg(b, 0, 'f', 2)
                                 .arg(w, 0, 'f', 2),
                             4000);
  });

  auto* levelsRow = new QHBoxLayout;
  levelsRow->setContentsMargins(6, 0, 6, 0);
  auto* levelsHint = new QLabel(QStringLiteral(
      "levels: click left = black, right = white, arrows nudge, double-click resets"));
  levelsHint->setStyleSheet("color:#7a7a84;font-size:11px;");
  levelsRow->addWidget(levelsHint, 1);
  benchPreviewLayout_->addLayout(levelsRow);

  benchPreviewLayout_->addWidget(buildReadout());
  benchPreviewLayout_->addWidget(buildViewRow());

  // the rail
  auto* railScroll = new QScrollArea;
  railScroll->setWidgetResizable(true);
  railScroll->setMinimumWidth(400);
  railScroll->setMaximumWidth(480);
  auto* rail = new QWidget;
  auto* rl = new QVBoxLayout(rail);
  rl->setContentsMargins(0, 0, 0, 0);
  rl->setSpacing(4);
  rl->addWidget(buildModeHeader());

  tabs_ = new QTabWidget;
  tabs_->addTab(tabShoot(), QStringLiteral("Shoot"));
  tabs_->addTab(tabScene(), QStringLiteral("Scene"));
  tabs_->addTab(tabMachine(), QStringLiteral("Machine"));
  rl->addWidget(tabs_, 1);

  auto* foot = new QHBoxLayout;
  foot->setContentsMargins(8, 0, 8, 4);
  lblChanged_ = new QLabel(QStringLiteral("stock configuration"));
  lblChanged_->setStyleSheet("color:#93a3ad;font-size:11px;");
  foot->addWidget(lblChanged_, 1);
  auto* btnReset = new QPushButton(QStringLiteral("reset..."));
  btnReset->setFlat(true);
  btnReset->setCursor(Qt::PointingHandCursor);
  btnReset->setStyleSheet(
      "QPushButton{border:none;background:transparent;color:#4da3cc;font-size:11px;}"
      "QPushButton:hover{text-decoration:underline;}");
  connect(btnReset, &QPushButton::clicked, this, &MainWindow::openResetDialog);
  foot->addWidget(btnReset);
  rl->addLayout(foot);

  railScroll->setWidget(rail);
  split->addWidget(left);
  split->addWidget(railScroll);
  split->setStretchFactor(0, 1);
  split->setStretchFactor(1, 0);
  split->setCollapsible(1, true);
  return split;
}

QWidget* MainWindow::buildModeHeader() {
  auto* box = new QGroupBox;
  auto* v = new QVBoxLayout(box);

  // camera row
  auto* camRow = new QHBoxLayout;
  camRow->addWidget(new QLabel(QStringLiteral("camera")));
  cmbCameraRail_ = new QComboBox;
  cmbCameraRail_->setToolTip(QStringLiteral(
      "Which device drives capture. 'Synthetic sky' is the built-in\ndemo scene -- no hardware "
      "needed. Picking a webcam opens it\n(macOS may ask for camera permission the first "
      "time)."));
  connect(cmbCameraRail_, QOverload<int>::of(&QComboBox::activated), this,
          &MainWindow::switchCamera);
  camRow->addWidget(cmbCameraRail_, 1);
  auto* btnRescan = new QPushButton(QStringLiteral("rescan"));
  btnRescan->setToolTip(QStringLiteral("Look for cameras again, after plugging one in."));
  connect(btnRescan, &QPushButton::clicked, this, &MainWindow::populateCameras);
  camRow->addWidget(btnRescan);
  v->addLayout(camRow);

  // profile row
  auto* profRow = new QHBoxLayout;
  profRow->addWidget(new QLabel(QStringLiteral("profile")));
  cmbProfile_ = new QComboBox;
  cmbProfile_->setToolTip(QStringLiteral(
      "Named settings profiles. Picking one activates it: every value\nit carries is applied, "
      "and machine paths are never touched."));
  connect(cmbProfile_, QOverload<int>::of(&QComboBox::activated), this,
          &MainWindow::profileActivated);
  profRow->addWidget(cmbProfile_, 1);
  auto* btnSave = new QPushButton(QStringLiteral("save"));
  btnSave->setToolTip(QStringLiteral(
      "Save the current settings over the selected profile (or as a new one when none is "
      "selected)."));
  connect(btnSave, &QPushButton::clicked, this, [this] { profileSave(false); });
  auto* btnNew = new QPushButton(QStringLiteral("new..."));
  btnNew->setToolTip(QStringLiteral("Save the current settings as a new profile."));
  connect(btnNew, &QPushButton::clicked, this, [this] { profileSave(true); });
  auto* btnDel = new QPushButton(QStringLiteral("del"));
  btnDel->setToolTip(QStringLiteral(
      "Delete the selected profile (the file only -- current settings stay as they are)."));
  connect(btnDel, &QPushButton::clicked, this, &MainWindow::profileDelete);
  profRow->addWidget(btnSave);
  profRow->addWidget(btnNew);
  profRow->addWidget(btnDel);
  v->addLayout(profRow);

  edSearch_ = new QLineEdit;
  edSearch_->setClearButtonEnabled(true);
  edSearch_->setPlaceholderText(
      QStringLiteral("find a setting...   \"clip\", \"pid\", \"cooldown\""));
  v->addWidget(edSearch_);

  QStringList labels;
  for (const auto& m : modes_) labels << m.label;
  tuner_ = new ModeTuner(labels, static_cast<int>(cfg_.num("shoot_mode", 0)));
  connect(tuner_, &ModeTuner::changed, this, &MainWindow::modeChanged);
  v->addWidget(tuner_);

  lblModeHint_ = new QLabel;
  lblModeHint_->setStyleSheet("color:#888;");
  v->addWidget(lblModeHint_);

  btnGo_ = new QPushButton;
  btnGo_->setCheckable(true);
  btnGo_->setMinimumHeight(70);
  connect(btnGo_, &QPushButton::clicked, this, &MainWindow::goClicked);
  v->addWidget(btnGo_);
  return box;
}

QWidget* MainWindow::buildReadout() {
  auto* w = new QWidget;
  w->setMaximumHeight(26);
  auto* h = new QHBoxLayout(w);
  h->setContentsMargins(6, 0, 6, 0);
  h->setSpacing(10);
  lblLine_ = new QLabel(QStringLiteral("-"));
  lblLine_->setFont(theme::mono(10));
  h->addWidget(lblLine_, 1);
  lblVerdictRead_ = new QLabel;
  lblVerdictRead_->setFont(theme::sans(10, true));
  h->addWidget(lblVerdictRead_);
  lblSessionRead_ = new QLabel;
  lblSessionRead_->setFont(theme::mono(10));
  lblSessionRead_->setStyleSheet("color:#8d949c;");
  h->addWidget(lblSessionRead_);
  return w;
}

QWidget* MainWindow::buildViewRow() {
  auto* w = new QWidget;
  auto* h = new QHBoxLayout(w);
  h->setContentsMargins(6, 0, 6, 0);
  auto* btnFs = new QPushButton(QStringLiteral("Fullscreen  (F11)"));
  connect(btnFs, &QPushButton::clicked, this, &MainWindow::toggleFullscreen);
  h->addWidget(btnFs);
  chkOutdoor_ = check(QStringLiteral("outdoor_mode"), QStringLiteral("Outdoor mode"),
                      [this](bool on) { outdoorChanged(on); });
  chkOutdoor_->setToolTip(QStringLiteral(
      "Contrast-stretches the preview and burns in its edges, so the\nsubject stays findable on "
      "a screen washed out by sunlight."));
  h->addWidget(chkOutdoor_);
  cmbOutdoor_ = comboStr(QStringLiteral("outdoor_style"),
                         {QStringLiteral("boost"), QStringLiteral("edges")},
                         [this](const QString& style) {
                           preview_->outdoorStyle = style;
                           faceField_->setOutdoor(chkOutdoor_->isChecked(),
                                                  cmbOutdoor_->currentIndex());
                         });
  h->addWidget(cmbOutdoor_);
  h->addWidget(new QLabel(QStringLiteral("stripe")));
  h->addWidget(spinInt(QStringLiteral("outdoor_stripe_px"), 1, 12, 1, QStringLiteral(" px"),
                       [this](double v) { preview_->stripePx = static_cast<int>(v); }));
  h->addWidget(new QLabel(QStringLiteral("sensitivity")));
  h->addWidget(spinDouble(QStringLiteral("outdoor_strength"), 0.2, 6.0, 0.2, 1, {},
                          [this](double v) { preview_->outdoorStrength = v; }));
  h->addStretch(1);
  return w;
}

// -------------------------------------------------------------- bindings --

void MainWindow::registerBind(const QString& key, QWidget* w, std::function<void()> refresh) {
  Bind b;
  b.key = key;
  b.widget = w;
  b.refresh = std::move(refresh);
  binds_ << b;
}

// A key may be bound in two places (exif_enabled lives on the Machine tab
// and the encode panel); keep every sibling in step when one of them writes.
void MainWindow::refreshBinds(const QString& key, QWidget* except) {
  for (const Bind& b : binds_)
    if (b.key == key && b.widget != except && b.refresh) b.refresh();
}

void MainWindow::saveCfg() { cfg_.save(); }

QWidget* MainWindow::spinInt(const QString& key, int lo, int hi, int step,
                             const QString& suffix, std::function<void(double)> onChange) {
  auto* s = new QSpinBox;
  s->setRange(lo, hi);
  s->setSingleStep(step);
  if (!suffix.isEmpty()) s->setSuffix(suffix);
  s->setValue(static_cast<int>(cfg_.num(key.toStdString())));
  connect(s, QOverload<int>::of(&QSpinBox::valueChanged), this, [this, key, s, onChange](int v) {
    cfg_.set(key.toStdString(), bs::Json(v));
    saveCfg();
    refreshBinds(key, s);
    if (onChange) onChange(v);
  });
  registerBind(key, s, [this, s, key] {
    const QSignalBlocker block(s);
    s->setValue(static_cast<int>(cfg_.num(key.toStdString())));
  });
  return s;
}

QWidget* MainWindow::spinDouble(const QString& key, double lo, double hi, double step,
                                int decimals, const QString& suffix,
                                std::function<void(double)> onChange) {
  auto* s = new QDoubleSpinBox;
  s->setRange(lo, hi);
  s->setSingleStep(step);
  s->setDecimals(decimals);
  if (!suffix.isEmpty()) s->setSuffix(suffix);
  s->setValue(cfg_.num(key.toStdString()));
  connect(s, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this,
          [this, key, s, onChange](double v) {
            cfg_.set(key.toStdString(), bs::Json(v));
            saveCfg();
            refreshBinds(key, s);
            if (onChange) onChange(v);
          });
  registerBind(key, s, [this, s, key] {
    const QSignalBlocker block(s);
    s->setValue(cfg_.num(key.toStdString()));
  });
  return s;
}

QCheckBox* MainWindow::check(const QString& key, const QString& text,
                             std::function<void(bool)> onChange) {
  auto* c = new QCheckBox(text);
  c->setChecked(cfg_.boolean(key.toStdString()));
  connect(c, &QCheckBox::toggled, this, [this, key, c, onChange](bool on) {
    cfg_.set(key.toStdString(), bs::Json(on));
    saveCfg();
    refreshBinds(key, c);
    if (onChange) onChange(on);
  });
  registerBind(key, c, [this, c, key] {
    const QSignalBlocker block(c);
    c->setChecked(cfg_.boolean(key.toStdString()));
  });
  return c;
}

QComboBox* MainWindow::comboStr(const QString& key, const QStringList& options,
                                std::function<void(QString)> onChange) {
  auto* c = new QComboBox;
  c->addItems(options);
  const QString cur = QString::fromStdString(cfg_.str(key.toStdString()));
  const int idx = options.indexOf(cur);
  c->setCurrentIndex(idx >= 0 ? idx : 0);
  connect(c, QOverload<int>::of(&QComboBox::currentIndexChanged), this,
          [this, key, c, options, onChange](int i) {
            if (i < 0 || i >= options.size()) return;
            cfg_.set(key.toStdString(), bs::Json(options[i].toStdString()));
            saveCfg();
            refreshBinds(key, c);
            if (onChange) onChange(options[i]);
          });
  registerBind(key, c, [this, c, key, options] {
    const QSignalBlocker block(c);
    const int i = options.indexOf(QString::fromStdString(cfg_.str(key.toStdString())));
    c->setCurrentIndex(i >= 0 ? i : 0);
  });
  return c;
}

QLineEdit* MainWindow::line(const QString& key) {
  auto* e = new QLineEdit(QString::fromStdString(cfg_.str(key.toStdString())));
  connect(e, &QLineEdit::editingFinished, this, [this, key, e] {
    cfg_.set(key.toStdString(), bs::Json(e->text().toStdString()));
    saveCfg();
    refreshBinds(key, e);
  });
  registerBind(key, e, [this, e, key] {
    const QSignalBlocker block(e);
    e->setText(QString::fromStdString(cfg_.str(key.toStdString())));
  });
  return e;
}

Accordion* MainWindow::section(const QString& title, QWidget* page, bool expanded, int tab) {
  auto* acc = new Accordion(title, expanded);
  acc->addWidget(page);
  sections_[title] = acc;
  // Remember which tab each section lives on for search (title -> tab is
  // recovered in indexSettings by walking parents).
  acc->setProperty("tabIndex", tab);
  return acc;
}

QWidget* MainWindow::wrapTab(const QList<QWidget*>& sections) {
  auto* page = new QWidget;
  auto* v = new QVBoxLayout(page);
  v->setContentsMargins(6, 4, 6, 4);
  v->setSpacing(2);
  for (QWidget* s : sections) v->addWidget(s);
  v->addStretch(1);
  auto* scroll = new QScrollArea;
  scroll->setWidgetResizable(true);
  scroll->setFrameShape(QFrame::NoFrame);
  scroll->setWidget(page);
  tabScrolls_ << scroll;
  return scroll;
}

// ------------------------------------------------------------- tab pages --

QWidget* MainWindow::tabShoot() {
  // Stills
  auto* stills = new QWidget;
  {
    auto* v = new QVBoxLayout(stills);
    auto* hint = new QLabel(QStringLiteral(
        "Runs the sensor as fast as JPEG encoding allows, with auto-exposure\nholding the "
        "shortest shutter that keeps the subject zone exposed.\nFrames land in a new session "
        "folder, split by shutter duration."));
    hint->setStyleSheet("color:#888;");
    v->addWidget(hint);
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Burst limit (0 = unlimited)"),
              spinInt(QStringLiteral("burst_count"), 0, 100000, 10));
    f->addRow(QStringLiteral("JPEG quality"), spinInt(QStringLiteral("jpeg_quality"), 50, 100, 1));
    v->addLayout(f);
    auto* pgm = check(QStringLiteral("save_pgm"),
                      QStringLiteral("Also save the loss-free luma plane (PGM)"));
    v->addWidget(pgm);
    auto* aids = new QLabel(
        QStringLiteral("Overlays and focus aids live in Scene > Focus aids and overlays."));
    aids->setStyleSheet("color:#888;");
    v->addWidget(aids);
  }

  // Rapid
  auto* rapid = new QWidget;
  {
    auto* v = new QVBoxLayout(rapid);
    auto* hint = new QLabel(QStringLiteral(
        "Flat files named YYYYmmddHHMMSS.jpg in one folder per run.\nNo shutter subfolders, no "
        "quality gates - metering and auto-exposure\nonly, so the loop stays out of the sensor's "
        "way."));
    hint->setStyleSheet("color:#888;");
    v->addWidget(hint);
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Frame limit (0 = max)"),
              spinInt(QStringLiteral("rapid_count"), 0, 100000, 10));
    v->addLayout(f);
  }

  // Timelapse
  auto* lapse = new QWidget;
  {
    auto* v = new QVBoxLayout(lapse);
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Interval"),
              spinDouble(QStringLiteral("timelapse_interval_s"), 0.2, 3600.0, 0.5, 1,
                         QStringLiteral(" s")));
    f->addRow(QStringLiteral("Frames (0 = unlimited)"),
              spinInt(QStringLiteral("timelapse_count"), 0, 100000, 10));
    v->addLayout(f);
    auto* note = new QLabel(QStringLiteral(
        "Captures at a fixed interval with auto-exposure running between\nframes, into a "
        "tlc-<timestamp> folder."));
    note->setStyleSheet("color:#888;");
    v->addWidget(note);
  }

  // Video (gated on this backend)
  auto* video = new QWidget;
  {
    auto* v = new QVBoxLayout(video);
    auto* note = new QLabel(QStringLiteral(
        "H.264 via the Pi's hardware encoder, muxed straight to MP4."));
    note->setStyleSheet("color:#888;");
    v->addWidget(note);
  }

  // Bird Flight
  auto* bird = new QWidget;
  {
    auto* v = new QVBoxLayout(bird);
    auto* intro = new QLabel(QStringLiteral(
        "Watches the preview for a dark subject surrounded by bright sky\n(blue or white), sharp "
        "along its boundary and well inside the\nframe. When every gate passes, it fires a burst "
        "on its own."));
    intro->setStyleSheet("color:#888;");
    v->addWidget(intro);
    lblBird_ = new QLabel(QStringLiteral("idle"));
    lblBird_->setWordWrap(true);
    lblBird_->setStyleSheet(
        "background:#14202a;color:#9fd0ff;padding:6px;border-radius:4px;font-family:monospace;");
    v->addWidget(lblBird_);

    v->addWidget(new QLabel(QStringLiteral("<b>Capture</b>")));
    auto* f1 = new QFormLayout;
    f1->addRow(QStringLiteral("Burst per take"), spinInt(QStringLiteral("bf_burst"), 1, 40, 1));
    f1->addRow(QStringLiteral("Cooldown after a take"),
               spinDouble(QStringLiteral("bf_cooldown_s"), 0.0, 60.0, 0.5, 1,
                          QStringLiteral(" s")));
    f1->addRow(QStringLiteral("Stop after takes (0 = keep watching)"),
               spinInt(QStringLiteral("bf_takes"), 0, 1000, 1));
    v->addLayout(f1);

    v->addWidget(new QLabel(QStringLiteral("<b>Auto-take gates</b>")));
    auto* order = new QLabel(QStringLiteral(
        "Listed in the order the gates judge -- the same ladder the Field\nface shows live."));
    order->setStyleSheet("color:#888;");
    v->addWidget(order);
    v->addWidget(check(QStringLiteral("bf_require_motion"),
                       QStringLiteral("Require motion between frames")));
    auto* f2 = new QFormLayout;
    f2->addRow(QStringLiteral("Motion threshold"),
               spinDouble(QStringLiteral("bf_motion_min"), 0.0, 0.05, 0.0005, 4,
                          QStringLiteral(" of frame")));
    f2->addRow(QStringLiteral("Sky brighter than"),
               spinInt(QStringLiteral("bf_sky_luma_min"), 40, 250, 1));
    f2->addRow(QStringLiteral("Min sky in frame"),
               spinDouble(QStringLiteral("bf_sky_min_frac"), 0.0, 1.0, 0.05, 2));
    f2->addRow(QStringLiteral("Subject darker than"),
               spinInt(QStringLiteral("bf_subject_luma_max"), 10, 200, 1));
    f2->addRow(QStringLiteral("Subject size min"),
               spinDouble(QStringLiteral("bf_min_area_frac"), 0.0, 0.2, 0.0002, 4,
                          QStringLiteral(" of frame")));
    f2->addRow(QStringLiteral("Subject size max"),
               spinDouble(QStringLiteral("bf_max_area_frac"), 0.001, 0.5, 0.005, 3,
                          QStringLiteral(" of frame")));
    f2->addRow(QStringLiteral("Sky around subject"),
               spinDouble(QStringLiteral("bf_ring_sky_frac"), 0.0, 1.0, 0.05, 2));
    f2->addRow(QStringLiteral("Edge margin"),
               spinDouble(QStringLiteral("bf_margin_frac"), 0.0, 0.4, 0.01, 2));
    f2->addRow(QStringLiteral("Min boundary sharpness"),
               spinDouble(QStringLiteral("bf_min_sharpness"), 0.0, 100.0, 1.0, 1));
    v->addLayout(f2);
    auto* foot = new QLabel(QStringLiteral(
        "Frames land in a session with the full quality pipeline; the\nsighting that fired the "
        "burst is logged. Changed gates apply from\nthe next watch (restart the mode)."));
    foot->setStyleSheet("color:#888;");
    v->addWidget(foot);
  }

  return wrapTab({
      section(QStringLiteral("Stills - full pipeline, quality gates"), stills, false, 0),
      section(QStringLiteral("Rapid - fastest, flat filenames"), rapid, false, 0),
      section(QStringLiteral("Timelapse"), lapse, false, 0),
      section(QStringLiteral("Video"), video, false, 0),
      section(QStringLiteral("Bird Flight - auto-take, sharp against sky"), bird, false, 0),
  });
}

QWidget* MainWindow::tabScene() {
  // Exposure and tone
  auto* expo = new QWidget;
  {
    auto* v = new QVBoxLayout(expo);
    v->addWidget(check(QStringLiteral("auto_exposure"),
                       QStringLiteral("Auto exposure (PID + lux feed-forward)"),
                       [this](bool) { capture_->rebuildBackend(); }));

    auto* manual = new QGroupBox(QStringLiteral("Manual"));
    auto* mf = new QFormLayout(manual);
    auto* preset = new QComboBox;
    static const long long kPresets[] = {125,    250,     500,     1000,    2000,
                                         4000,   8000,    16000,   33000,   100000,
                                         400000, 1600000, 6400000, 19100000};
    for (long long us : kPresets)
      preset->addItem(QString::fromStdString(bs::describe_shutter(us)),
                      QVariant(static_cast<qlonglong>(us)));
    mf->addRow(QStringLiteral("Shutter preset"), preset);
    auto* shutter = spinInt(QStringLiteral("manual_shutter_us"), 114, 60000000, 100,
                            QStringLiteral(" us"));
    mf->addRow(QStringLiteral("Shutter"), shutter);
    connect(preset, QOverload<int>::of(&QComboBox::activated), this, [preset, shutter](int i) {
      const qlonglong us = preset->itemData(i).toLongLong();
      if (us > 0) static_cast<QSpinBox*>(shutter)->setValue(static_cast<int>(us));
    });
    mf->addRow(QStringLiteral("Analogue gain"),
               spinDouble(QStringLiteral("manual_gain"), 1.0, 22.0, 0.5, 2, QStringLiteral("x")));
    v->addWidget(manual);

    auto* targets = new QGroupBox(QStringLiteral("Auto exposure targets"));
    auto* tf = new QFormLayout(targets);
    tf->addRow(QStringLiteral("Target luma (0-255)"),
               spinDouble(QStringLiteral("target_luma"), 20, 240, 2, 1));
    tf->addRow(QStringLiteral("Highlight tolerance"),
               spinDouble(QStringLiteral("max_clip_frac"), 0.0, 0.5, 0.005, 3));
    tf->addRow(QStringLiteral("Sky clip budget"),
               spinDouble(QStringLiteral("sky_clip_tolerance"), 0.0, 1.0, 0.05, 2));
    tf->addRow(QStringLiteral("Sky zone (top fraction)"),
               spinDouble(QStringLiteral("sky_zone_frac"), 0.0, 0.9, 0.05, 2,
                          {}, [this](double v) { preview_->skyZoneFrac = v; }));
    tf->addRow(QStringLiteral("Subject metering weight"),
               spinDouble(QStringLiteral("subject_weight"), 0.0, 2.0, 0.05, 2));
    tf->addRow(QStringLiteral("Sky metering weight"),
               spinDouble(QStringLiteral("sky_weight"), 0.0, 2.0, 0.05, 2));
    auto* tNote = new QLabel(QStringLiteral(
        "Highlight tolerance is the subject zone's clip budget; the sky has\nits own, far looser "
        "budget and only ever trims exposure down."));
    tNote->setStyleSheet("color:#888;");
    tf->addRow(tNote);
    v->addWidget(targets);

    auto* ladder = new QGroupBox(QStringLiteral("Shutter/gain ladder (shortest shutter first)"));
    auto* lf = new QFormLayout(ladder);
    lf->addRow(QStringLiteral("Motion limit"),
               spinInt(QStringLiteral("motion_limit_us"), 114, 100000, 250,
                       QStringLiteral(" us")));
    lf->addRow(QStringLiteral("Preferred max gain"),
               spinDouble(QStringLiteral("gain_preferred_max"), 1.0, 22.0, 0.5, 1,
                          QStringLiteral("x")));
    lf->addRow(QStringLiteral("Hard max shutter"),
               spinInt(QStringLiteral("shutter_hard_max_us"), 1000, 20000000, 1000,
                       QStringLiteral(" us")));
    lf->addRow(check(QStringLiteral("prefer_exposure_time"),
                     QStringLiteral("Spend shutter before gain (lowest noise)")));
    auto* note = new QLabel(QStringLiteral(
        "Checked, the shutter lengthens all the way to its hard cap before\ngain moves at all -- "
        "gain buys brightness at the cost of noise it can\nnever give back. Unchecked, the "
        "shutter pins at the motion limit and\ngain rises to its preferred cap first, so "
        "wingbeats stay frozen."));
    note->setStyleSheet("color:#888;");
    lf->addRow(note);
    v->addWidget(ladder);

    auto* pid = new QGroupBox(QStringLiteral("PID smoothing"));
    auto* pf = new QFormLayout(pid);
    pf->addRow(QStringLiteral("Kp"), spinDouble(QStringLiteral("pid_kp"), 0.0, 3.0, 0.05, 2));
    pf->addRow(QStringLiteral("Ki"), spinDouble(QStringLiteral("pid_ki"), 0.0, 2.0, 0.02, 2));
    pf->addRow(QStringLiteral("Kd"), spinDouble(QStringLiteral("pid_kd"), 0.0, 2.0, 0.02, 2));
    pf->addRow(QStringLiteral("Deadband (EV)"),
               spinDouble(QStringLiteral("pid_deadband_ev"), 0.0, 1.0, 0.02, 2));
    pf->addRow(QStringLiteral("Max step (EV)"),
               spinDouble(QStringLiteral("pid_slew_ev"), 0.1, 5.0, 0.1, 1));
    pf->addRow(QStringLiteral("Integral clamp (EV)"),
               spinDouble(QStringLiteral("pid_integral_clamp_ev"), 0.5, 5.0, 0.25, 2));
    pf->addRow(QStringLiteral("Damping (step fraction)"),
               spinDouble(QStringLiteral("ae_damping"), 0.05, 1.0, 0.05, 2));
    pf->addRow(QStringLiteral("Meter average"),
               spinInt(QStringLiteral("ae_average_n"), 1, 15, 1, QStringLiteral(" frames")));
    pf->addRow(QStringLiteral("Meter average mode"),
               comboStr(QStringLiteral("ae_average_mode"),
                        {QStringLiteral("median"), QStringLiteral("mean"),
                         QStringLiteral("none")}));
    auto* pNote = new QLabel(QStringLiteral(
        "Damping applies only part of each correction -- with a two-frame\ncontrol latency the "
        "full step overshoots. The average window is\ndiscarded on a big scene change, so steps "
        "stay instant."));
    pNote->setStyleSheet("color:#888;");
    pf->addRow(pNote);
    v->addWidget(pid);

    auto* row = new QHBoxLayout;
    auto* btnResetAe = new QPushButton(QStringLiteral("Reset AE loop"));
    connect(btnResetAe, &QPushButton::clicked, this, [this] {
      capture_->rebuildBackend();
      log(QStringLiteral("AE loop reset"));
    });
    row->addWidget(btnResetAe);
    row->addStretch(1);
    v->addLayout(row);
  }

  // Focus aids
  auto* focus = new QWidget;
  {
    auto* v = new QVBoxLayout(focus);
    auto* intro = new QLabel(QStringLiteral(
        "The lens is manual with no feedback, and the subject is usually a\nsmall shape against "
        "bright sky. These are the aids that make that\nworkable."));
    intro->setStyleSheet("color:#888;");
    v->addWidget(intro);

    auto* box = new QGroupBox(QStringLiteral("Overlays on the main preview"));
    auto* bv = new QVBoxLayout(box);
    chkFmap_ = new QCheckBox(
        QStringLiteral("Focus map  -  shade each area by how sharp it is"));
    connect(chkFmap_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showFocusMap = on; });
    bv->addWidget(chkFmap_);
    auto* fmapHint = new QLabel(QStringLiteral(
        "    Ranks the frame by resolved detail and rings the sharpest area.\n    Peaking alone "
        "cannot tell a sharp branch from noisy sky; this can."));
    fmapHint->setStyleSheet("color:#777;font-size:11px;");
    bv->addWidget(fmapHint);
    chkSharpNum_ = new QCheckBox(
        QStringLiteral("Sharpness readout  -  large number with peak-hold"));
    connect(chkSharpNum_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showSharpness = on; });
    bv->addWidget(chkSharpNum_);
    chkPeak2_ = new QCheckBox(QStringLiteral("Focus peaking  -  highlight high-contrast edges"));
    connect(chkPeak2_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showPeaking = on; });
    bv->addWidget(chkPeak2_);
    chkZebra2_ = new QCheckBox(QStringLiteral("Clipping zebras"));
    chkZebra2_->setChecked(true);
    connect(chkZebra2_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showZebra = on; });
    bv->addWidget(chkZebra2_);
    chkZones_ = new QCheckBox(
        QStringLiteral("Metering zones  -  the sky/subject split line"));
    chkZones_->setChecked(true);
    connect(chkZones_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showZones = on; });
    bv->addWidget(chkZones_);
    chkGrid_ = new QCheckBox(QStringLiteral("Thirds grid"));
    connect(chkGrid_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showGrid = on; });
    bv->addWidget(chkGrid_);
    chkHud_ = new QCheckBox(QStringLiteral("HUD  -  exposure readout in the corner"));
    chkHud_->setChecked(true);
    connect(chkHud_, &QCheckBox::toggled, this,
            [this](bool on) { preview_->showHud = on; });
    bv->addWidget(chkHud_);
    auto* wheelHint = new QLabel(
        QStringLiteral("    Mouse wheel over the preview toggles every overlay at once."));
    wheelHint->setStyleSheet("color:#777;font-size:11px;");
    bv->addWidget(wheelHint);
    auto* rrow = new QHBoxLayout;
    auto* btnPeak = new QPushButton(QStringLiteral("Reset peak-hold"));
    connect(btnPeak, &QPushButton::clicked, this, [this] { preview_->resetFocusPeak(); });
    rrow->addWidget(btnPeak);
    rrow->addStretch(1);
    bv->addLayout(rrow);
    v->addWidget(box);

    auto* blur = new QGroupBox(QStringLiteral("Blur gate calibration"));
    auto* blv = new QVBoxLayout(blur);
    auto* blurNote = new QLabel(QStringLiteral(
        "What counts as 'sharp' depends on the lens, aperture and subject,\nso the blur gate is "
        "referenced to a frame you call focused rather\nthan to a fixed number. Focus carefully, "
        "then press this."));
    blurNote->setStyleSheet("color:#888;");
    blv->addWidget(blurNote);
    auto* btnRef = new QPushButton(QStringLiteral("Use current view as the sharp reference"));
    connect(btnRef, &QPushButton::clicked, this, &MainWindow::setSharpnessReference);
    blv->addWidget(btnRef);
    lblSharpRef_ = new QLabel;
    lblSharpRef_->setStyleSheet("font-family:monospace;color:#9a9;");
    blv->addWidget(lblSharpRef_);
    v->addWidget(blur);

    auto* monitor = new QGroupBox(QStringLiteral("1:1 focus monitor"));
    auto* mv = new QVBoxLayout(monitor);
    auto* btnMonitor = new QPushButton(QStringLiteral("Open focus monitor"));
    btnMonitor->setMinimumHeight(46);
    btnMonitor->setStyleSheet("font-weight:600;");
    connect(btnMonitor, &QPushButton::clicked, this, [this] {
      if (focusMonitor_ && focusMonitor_->isVisible()) {
        focusMonitor_->raise();
        focusMonitor_->activateWindow();
        return;
      }
      if (!focusMonitor_) focusMonitor_ = new FocusMonitor(this);
      // Parked against the right edge, so it does not cover the preview.
      if (QScreen* screen = QApplication::primaryScreen()) {
        const QRect g = screen->availableGeometry();
        focusMonitor_->move(g.right() - focusMonitor_->width() - 20, g.top() + 40);
      }
      focusMonitor_->show();
      log(QStringLiteral("focus monitor open"));
    });
    mv->addWidget(btnMonitor);
    auto* mNote = new QLabel(QStringLiteral(
        "A frameless, always-on-top window showing real sensor pixels at\n100-400%, with "
        "peaking and a peak-hold score. Turn the ring until\nthe number stops climbing."));
    mNote->setStyleSheet("color:#888;");
    mv->addWidget(mNote);
    v->addWidget(monitor);

    auto* live = new QGroupBox(QStringLiteral("Live focus reading"));
    auto* lv = new QVBoxLayout(live);
    lblFocusLive_ = new QLabel(QStringLiteral("-"));
    lblFocusLive_->setStyleSheet("font-family:monospace;font-size:12px;");
    lv->addWidget(lblFocusLive_);
    v->addWidget(live);
  }

  // Quality gates
  auto* quality = new QWidget;
  {
    auto* v = new QVBoxLayout(quality);
    auto* intro = new QLabel(QStringLiteral(
        "Every frame is scored from the free 640x480 luma plane, plus a\nnative-resolution "
        "centre crop for the focus measure."));
    intro->setStyleSheet("color:#888;");
    v->addWidget(intro);
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Dark if p95 below"),
              spinDouble(QStringLiteral("dark_p95_max"), 1, 200, 2, 1));
    f->addRow(QStringLiteral("Blown if subject clip above"),
              spinDouble(QStringLiteral("blown_clip_frac"), 0.01, 1.0, 0.01, 2));
    f->addRow(QStringLiteral("Soft if sharpness below"),
              spinDouble(QStringLiteral("blur_threshold"), 0.0, 200.0, 1.0, 1));
    f->addRow(QStringLiteral("Detail tile threshold"),
              spinDouble(QStringLiteral("content_std_min"), 0.0, 80.0, 0.5, 1));
    f->addRow(QStringLiteral("Rejected frames"),
              comboStr(QStringLiteral("reject_action"),
                       {QStringLiteral("flag"), QStringLiteral("quarantine"),
                        QStringLiteral("delete")}));
    v->addLayout(f);
    auto* legend = new QLabel(QStringLiteral(
        "flag       keep everything, record the verdict in index.jsonl\nquarantine keep, but "
        "move under _rejected/ so syncs stay clean\ndelete     never written to disk (the index "
        "still records it)"));
    legend->setStyleSheet("color:#888;font-family:monospace;font-size:11px;");
    v->addWidget(legend);

    auto* sess = new QGroupBox(QStringLiteral("This session"));
    auto* sv = new QVBoxLayout(sess);
    lblCounts_ = new QLabel(QStringLiteral("-"));
    sv->addWidget(lblCounts_);
    v->addWidget(sess);

    v->addWidget(new QLabel(QStringLiteral("Log")));
    logView_ = new QTextEdit;
    logView_->setReadOnly(true);
    logView_->setMaximumHeight(220);
    v->addWidget(logView_);
  }

  return wrapTab({
      section(QStringLiteral("Exposure and tone"), expo, true, 1),
      section(QStringLiteral("Focus aids and overlays"), focus, false, 1),
      section(QStringLiteral("Quality gates"), quality, false, 1),
  });
}

QWidget* MainWindow::tabMachine() {
  // Cascade (gated: not in the native line yet)
  auto* cascade = new QWidget;
  {
    auto* v = new QVBoxLayout(cascade);
    auto* note = new QLabel(QStringLiteral(
        "Frames are written in groups; background workers copy each finished\ngroup down to the "
        "next tier, verify it, then free the source."));
    note->setStyleSheet("color:#888;");
    v->addWidget(note);
  }

  // Paths
  auto* paths = new QWidget;
  {
    auto* v = new QVBoxLayout(paths);
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Capture root"), line(QStringLiteral("data_root")));
    f->addRow(QStringLiteral("Stop below"),
              spinInt(QStringLiteral("min_free_mb"), 100, 100000, 100,
                      QStringLiteral(" MB free")));
    f->addRow(QStringLiteral("Boot face"),
              comboStr(QStringLiteral("ui_face"),
                       {QStringLiteral("auto"), QStringLiteral("camera"),
                        QStringLiteral("field"), QStringLiteral("bench"),
                        QStringLiteral("library")}));
    v->addLayout(f);
    auto* bootNote = new QLabel(QStringLiteral(
        "auto: a developer tree boots the Bench, a Mac the Library, anything\nelse the Camera. "
        "Launch with --start to press START unattended; the\nUSB offload of 1.x migrates with "
        "the Pi backend it serves."));
    bootNote->setStyleSheet("color:#888;");
    v->addWidget(bootNote);
    auto* row = new QHBoxLayout;
    auto* btnOpen = new QPushButton(QStringLiteral("Open capture folder"));
    connect(btnOpen, &QPushButton::clicked, this, [this] {
      openPath(QString::fromStdString(
          bs::expand_user(cfg_.str("data_root", "~/birdshot-data"))));
    });
    row->addWidget(btnOpen);
    row->addStretch(1);
    v->addLayout(row);
  }

  // Site (the horizons layer's anchor: sun, plan, align all need it)
  auto* site = new QWidget;
  {
    auto* v = new QVBoxLayout(site);
    auto* note = new QLabel(QStringLiteral(
        "Where the instrument stands. The sun position, shoot planning and\nmulti-day "
        "alignment all reason from these coordinates."));
    note->setStyleSheet("color:#888;");
    v->addWidget(note);
    // Typing real coordinates IS setting the site: arm site_set on the
    // first edit so the sun readout, plan and doctor stop disagreeing with
    // visibly-correct numbers. The checkbox stays the master for off.
    auto siteEdited = [this](double) {
      if (!cfg_.boolean("site_set", false)) {
        cfg_.set("site_set", bs::Json(true));
        saveCfg();
        refreshBinds(QStringLiteral("site_set"));
        runDoctor();
      }
      refreshSunLabel();
    };
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Latitude (+N)"),
              spinDouble(QStringLiteral("site_lat"), -90.0, 90.0, 0.001, 5,
                         QStringLiteral("\u00b0"), siteEdited));
    f->addRow(QStringLiteral("Longitude (+E)"),
              spinDouble(QStringLiteral("site_lon"), -180.0, 180.0, 0.001, 5,
                         QStringLiteral("\u00b0"), siteEdited));
    f->addRow(QStringLiteral("Elevation"),
              spinDouble(QStringLiteral("site_elev_m"), -430.0, 9000.0, 1.0, 0,
                         QStringLiteral(" m")));
    f->addRow(QStringLiteral("Name"), line(QStringLiteral("site_name")));
    v->addLayout(f);
    v->addWidget(check(QStringLiteral("site_set"),
                       QStringLiteral("Site is set (sun, plan and align use it)"),
                       [this](bool) { refreshSunLabel(); runDoctor(); }));
    lblSun_ = new QLabel(QStringLiteral("-"));
    lblSun_->setStyleSheet("font-family:monospace;font-size:11px;color:#9fd0ff;");
    lblSun_->setWordWrap(true);
    v->addWidget(lblSun_);

    auto* lensBox = new QGroupBox(QStringLiteral("Lens geometry"));
    auto* lg = new QFormLayout(lensBox);
    lg->addRow(QStringLiteral("Focal length"),
               spinDouble(QStringLiteral("lens_focal_mm"), 1.0, 2000.0, 0.5, 1,
                          QStringLiteral(" mm")));
    lg->addRow(QStringLiteral("Sensor width"),
               spinDouble(QStringLiteral("sensor_width_mm"), 1.0, 100.0, 0.1, 3,
                          QStringLiteral(" mm")));
    auto* lensNote = new QLabel(QStringLiteral(
        "The planner's field-of-view check: whether a fixed mount holds the\nseason's sunset "
        "azimuth swing is pure trigonometry on these two\nnumbers. IMX477 sensor width by "
        "default."));
    lensNote->setStyleSheet("color:#888;");
    lg->addRow(lensNote);
    v->addWidget(lensBox);
  }

  // Doctor
  auto* health = new QWidget;
  {
    auto* v = new QVBoxLayout(health);
    auto* note = new QLabel(QStringLiteral(
        "Platform, backend, encoder, storage and config --\nthe same checklist as birdshot "
        "doctor."));
    note->setStyleSheet("color:#888;");
    v->addWidget(note);
    txtDoctor_ = new QTextEdit;
    txtDoctor_->setReadOnly(true);
    txtDoctor_->setMaximumHeight(260);
    txtDoctor_->setStyleSheet("font-family:monospace;font-size:11px;");
    txtDoctor_->setText(QStringLiteral("checking..."));
    v->addWidget(txtDoctor_);
    auto* row = new QHBoxLayout;
    auto* btnRun = new QPushButton(QStringLiteral("Run doctor again"));
    connect(btnRun, &QPushButton::clicked, this, &MainWindow::runDoctor);
    row->addWidget(btnRun);
    lblDoctorStamp_ = new QLabel(QStringLiteral("running at startup..."));
    lblDoctorStamp_->setStyleSheet("color:#888;");
    row->addWidget(lblDoctorStamp_);
    row->addStretch(1);
    v->addLayout(row);
  }

  // Identity: what `birdshot exif` and assembly stamp into each JPEG.
  auto* identity = new QWidget;
  {
    auto* v = new QVBoxLayout(identity);
    v->addWidget(check(QStringLiteral("exif_enabled"),
                       QStringLiteral("Write EXIF when encoding or assembling")));
    auto* f = new QFormLayout;
    f->addRow(QStringLiteral("Camera make"), line(QStringLiteral("exif_make")));
    f->addRow(QStringLiteral("Camera model"), line(QStringLiteral("exif_model")));
    f->addRow(QStringLiteral("Lens"), line(QStringLiteral("exif_lens")));
    f->addRow(QStringLiteral("Focal length (0 = not recorded)"),
              spinDouble(QStringLiteral("exif_focal_mm"), 0.0, 2000.0, 1.0, 1,
                         QStringLiteral(" mm")));
    f->addRow(QStringLiteral("F-number (0 = not recorded)"),
              spinDouble(QStringLiteral("exif_fnumber"), 0.0, 64.0, 0.1, 1));
    f->addRow(QStringLiteral("Artist"), line(QStringLiteral("exif_artist")));
    f->addRow(QStringLiteral("Copyright"), line(QStringLiteral("exif_copyright")));
    v->addLayout(f);
  }

  return wrapTab({
      section(QStringLiteral("Cascade - tiers, RAM buffer, flush"), cascade, false, 2),
      section(QStringLiteral("Paths and unattended start"), paths, false, 2),
      section(QStringLiteral("Site - horizons"), site, false, 2),
      section(QStringLiteral("Install health - doctor"), health, false, 2),
      section(QStringLiteral("Identity - EXIF"), identity, false, 2),
  });
}

QWidget* MainWindow::buildEncodePage() {
  auto* page = new QWidget;
  auto* v = new QVBoxLayout(page);
  v->addWidget(new QLabel(QStringLiteral("Encode a folder of photos into a video.")));

  auto* src = new QGroupBox(QStringLiteral("Source"));
  auto* sf = new QFormLayout(src);
  cmbSource_ = new QComboBox;
  sf->addRow(QStringLiteral("Folder"), cmbSource_);
  auto* srow = new QHBoxLayout;
  auto* btnBrowse = new QPushButton(QStringLiteral("Browse..."));
  connect(btnBrowse, &QPushButton::clicked, this, [this] {
    const QString dir = QFileDialog::getExistingDirectory(
        this, QStringLiteral("Choose a folder of photos"),
        QString::fromStdString(bs::expand_user(cfg_.str("data_root", "~/birdshot-data"))));
    if (dir.isEmpty()) return;
    cmbSource_->addItem(dir, dir);
    cmbSource_->setCurrentIndex(cmbSource_->count() - 1);
  });
  srow->addWidget(btnBrowse);
  auto* btnRefresh = new QPushButton(QStringLiteral("Refresh"));
  connect(btnRefresh, &QPushButton::clicked, this, &MainWindow::refreshEncodeSources);
  srow->addWidget(btnRefresh);
  srow->addStretch(1);
  sf->addRow(srow);
  auto* chkExif = check(QStringLiteral("exif_enabled"),
                        QStringLiteral("Write EXIF into the source frames first"));
  chkExif->setToolTip(QStringLiteral(
      "Stamps date/time to the centisecond, exposure, ISO from gain, and\nbirdshot's own "
      "metrics into each JPEG. Modifies the source files in\nplace, losslessly - the JPEG is "
      "not re-encoded."));
  sf->addRow(chkExif);
  chkEncodeOk_ = check(QStringLiteral("encode_only_ok"),
                       QStringLiteral("Only frames that passed the quality gates"));
  sf->addRow(chkEncodeOk_);
  v->addWidget(src);

  auto* outBox = new QGroupBox(QStringLiteral("Output"));
  auto* of = new QFormLayout(outBox);
  of->addRow(QStringLiteral("Frame rate"),
             spinInt(QStringLiteral("encode_fps"), 1, 240, 1, QStringLiteral(" fps")));
  auto* width = static_cast<QSpinBox*>(
      spinInt(QStringLiteral("encode_width"), 0, 4096, 1, QStringLiteral(" px wide")));
  width->setSpecialValueText(QStringLiteral("native"));
  of->addRow(QStringLiteral("Scale"), width);
  of->addRow(QStringLiteral("Quality (CRF, lower = better)"),
             spinInt(QStringLiteral("encode_crf"), 0, 51, 1));
  of->addRow(QStringLiteral("Encoder preset"),
             comboStr(QStringLiteral("encode_preset"),
                      {QStringLiteral("ultrafast"), QStringLiteral("superfast"),
                       QStringLiteral("veryfast"), QStringLiteral("faster"),
                       QStringLiteral("fast"), QStringLiteral("medium"),
                       QStringLiteral("slow")}));
  v->addWidget(outBox);

  auto* brow = new QHBoxLayout;
  btnEncode_ = new QPushButton(QStringLiteral("Encode"));
  btnEncode_->setMinimumHeight(44);
  btnEncode_->setStyleSheet("font-weight:600;");
  connect(btnEncode_, &QPushButton::clicked, this, &MainWindow::startEncode);
  brow->addWidget(btnEncode_);
  btnEncodeCancel_ = new QPushButton(QStringLiteral("Cancel"));
  btnEncodeCancel_->setEnabled(false);
  connect(btnEncodeCancel_, &QPushButton::clicked, this, [this] {
    if (encodeProc_) encodeProc_->kill();
  });
  brow->addWidget(btnEncodeCancel_);
  v->addLayout(brow);

  progEncode_ = new QProgressBar;
  progEncode_->hide();
  v->addWidget(progEncode_);
  lblEncodeStatus_ = new QLabel(QStringLiteral("-"));
  lblEncodeStatus_->setWordWrap(true);
  lblEncodeStatus_->setStyleSheet("font-family:monospace;font-size:11px;");
  v->addWidget(lblEncodeStatus_);
  return page;
}

void MainWindow::refreshEncodeSources() {
  if (!cmbSource_) return;
  const QSignalBlocker block(cmbSource_);
  cmbSource_->clear();
  const std::string root = bs::expand_user(cfg_.str("data_root", "~/birdshot-data"));
  int select = -1;
  for (const auto& dir : bs::list_sessions(root)) {
    bs::Session sess = bs::Session::open(dir);
    cmbSource_->addItem(QStringLiteral("%1  (%2 frames)")
                            .arg(QString::fromStdString(sess.name()))
                            .arg(sess.read_index().size()),
                        QString::fromStdString(dir));
    if (QString::fromStdString(dir) == faceLibrary_->sessionPath())
      select = cmbSource_->count() - 1;
  }
  if (select >= 0) cmbSource_->setCurrentIndex(select);
}

// The GUI shells out to its own CLI: `birdshot assemble` owns the frame
// selection, the EXIF pass and the ffmpeg invocation, so there is exactly
// one encode path to trust.
void MainWindow::startEncode() {
  if (encodeProc_) {
    QMessageBox::information(this, QStringLiteral("Encode"),
                             QStringLiteral("An encode is already running."));
    return;
  }
  const QString dir = cmbSource_->currentData().toString();
  if (dir.isEmpty()) {
    QMessageBox::information(this, QStringLiteral("Encode"),
                             QStringLiteral("Choose a source folder first."));
    return;
  }
  QStringList args{QStringLiteral("assemble"), dir, QStringLiteral("--config"),
                   QString::fromStdString(cfg_.path())};
  if (!chkEncodeOk_->isChecked()) args << QStringLiteral("--all");

  encodeProc_ = new QProcess(this);
  encodeProc_->setProcessChannelMode(QProcess::MergedChannels);
  connect(encodeProc_, &QProcess::readyReadStandardOutput, this, [this] {
    const QString text = QString::fromUtf8(encodeProc_->readAllStandardOutput()).trimmed();
    if (!text.isEmpty()) lblEncodeStatus_->setText(text.section(QChar('\n'), -1));
  });
  connect(encodeProc_, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
          [this](int code, QProcess::ExitStatus) {
            progEncode_->hide();
            btnEncode_->setEnabled(true);
            btnEncodeCancel_->setEnabled(false);
            encodeProc_->deleteLater();
            encodeProc_ = nullptr;
            log(code == 0 ? QStringLiteral("encode finished")
                          : QStringLiteral("encode failed (%1)").arg(code));
          });
  btnEncode_->setEnabled(false);
  btnEncodeCancel_->setEnabled(true);
  progEncode_->setRange(0, 0);  // busy; ffmpeg owns the real progress
  progEncode_->show();
  lblEncodeStatus_->setText(QStringLiteral("encoding..."));
  log(QStringLiteral("encoding %1").arg(QFileInfo(dir).fileName()));
  encodeProc_->start(QCoreApplication::applicationDirPath() + QStringLiteral("/birdshot"),
                     args);
}

void MainWindow::refreshSunLabel() {
  if (!lblSun_) return;
  if (!cfg_.boolean("site_set", false)) {
    lblSun_->setText(QStringLiteral("site not set -- sun/plan/align refuse to guess; "
                                    "set it here or with `birdshot site set`"));
    return;
  }
  const bs::Site site = cfg_.site();
  const double now = QDateTime::currentSecsSinceEpoch();
  const bs::SunPos sun = bs::sun_position(now, site.lat_deg, site.lon_deg);
  QString text = QStringLiteral("sun now: elevation %1\u00b0  azimuth %2\u00b0")
                     .arg(sun.elevation_deg, 0, 'f', 1)
                     .arg(sun.azimuth_deg, 0, 'f', 1);
  if (auto set = bs::sun_crossing(now, site.lat_deg, site.lon_deg, bs::kAltSunset, false)) {
    const QDateTime when = QDateTime::fromSecsSinceEpoch(static_cast<qint64>(*set));
    text += QStringLiteral("\nsunset %1").arg(when.toString(QStringLiteral("HH:mm")));
    if (auto az = bs::sunset_azimuth_deg(now, site.lat_deg, site.lon_deg))
      text += QStringLiteral(" at azimuth %1\u00b0").arg(*az, 0, 'f', 1);
  }
  lblSun_->setText(text);
}

// ------------------------------------------------------------- behavior --

void MainWindow::modeChanged(int idx) {
  idx = std::clamp(idx, 0, static_cast<int>(modes_.size()) - 1);
  cfg_.set("shoot_mode", bs::Json(idx));
  saveCfg();
  lblModeHint_->setText(modes_[idx].hint);
  faceField_->syncMode(idx);
  faceCamera_->syncMode(idx);
  // Expand the mode's own section; collapse the other four.
  for (auto it = sections_.cbegin(); it != sections_.cend(); ++it) {
    for (int m = 0; m < modes_.size(); ++m) {
      if (it.key().startsWith(modes_[m].label, Qt::CaseInsensitive)) {
        it.value()->setExpanded(m == idx);
        break;
      }
    }
  }
  refreshGoButton();
}

void MainWindow::refreshGoButton() {
  const QString state = stateName();
  const bool running = capture_->recording();
  const int idx =
      std::clamp(static_cast<int>(cfg_.num("shoot_mode", 0)), 0,
                 static_cast<int>(modes_.size()) - 1);
  const QString label = modes_[idx].label;
  btnGo_->setChecked(running);
  btnGo_->setText(running ? QStringLiteral("STOP  -  %1").arg(state.toUpper())
                          : QStringLiteral("START  -  %1").arg(label.toUpper()));
  btnGo_->setStyleSheet(QStringLiteral(
      "QPushButton{font-size:18px;font-weight:700;border-radius:8px;background:%1;color:white;}")
      .arg(running ? QStringLiteral("#a03020") : QStringLiteral("#1f7a3f")));
  faceField_->updateGo(running, state, label);
  faceCamera_->updateGo(running, state, label);
  lblStateBar_->setText(state.toUpper());
}

void MainWindow::goClicked() {
  if (capture_->recording()) {
    capture_->stopRecording();
    return;
  }
  const int idx =
      std::clamp(static_cast<int>(cfg_.num("shoot_mode", 0)), 0,
                 static_cast<int>(modes_.size()) - 1);
  const ModeSpec& m = modes_[idx];
  if (!caps().contains(m.key)) {
    log(QStringLiteral("%1 is not available on this camera").arg(m.label));
    return;
  }
  qint64 count = 0;
  double interval = -1.0;
  if (m.mode == bs::Mode::Collect) count = static_cast<qint64>(cfg_.num("burst_count", 0));
  if (m.mode == bs::Mode::Rapid) count = static_cast<qint64>(cfg_.num("rapid_count", 0));
  if (m.mode == bs::Mode::Timelapse) {
    count = static_cast<qint64>(cfg_.num("timelapse_count", 0));
    interval = cfg_.num("timelapse_interval_s", 5.0);
  }
  if (m.mode == bs::Mode::BirdFlight) count = static_cast<qint64>(cfg_.num("bf_takes", 0));
  counts_ = {{QStringLiteral("ok"), 0},
             {QStringLiteral("dark"), 0},
             {QStringLiteral("blown"), 0},
             {QStringLiteral("empty"), 0}};
  sessionFrames_ = 0;
  capture_->startRecording(m.mode, count, interval);
}

void MainWindow::applyCapabilities() {
  const QSet<QString> c = caps();
  const QString cam = capture_->backendName();
  QStringList why;
  for (const auto& m : modes_)
    why << (c.contains(m.key)
                ? QString()
                : QStringLiteral("%1 is not available on %2").arg(m.label, cam));
  tuner_->setAvailable(why);
  faceField_->tuner->setAvailable(why);
  faceCamera_->tuner->setAvailable(why);

  auto gate = [&](const QString& title, const QString& capName, const QString& reason) {
    if (!sections_.contains(title)) return;
    sections_[title]->setGated(c.contains(capName) ? QString() : reason);
  };
  gate(QStringLiteral("Rapid - fastest, flat filenames"), QStringLiteral("rapid"),
       QStringLiteral("not available on %1").arg(cam));
  gate(QStringLiteral("Video"), QStringLiteral("video"),
       QStringLiteral("video capture is a 2.0.0 platform-backend port"));
  gate(QStringLiteral("Bird Flight - auto-take, sharp against sky"),
       QStringLiteral("birdflight"), QStringLiteral("not available on %1").arg(cam));
  gate(QStringLiteral("Cascade - tiers, RAM buffer, flush"), QStringLiteral("cascade"),
       QStringLiteral("the cascade is not in the native line yet"));
  gate(QStringLiteral("Exposure and tone"), QStringLiteral("exposure"),
       QStringLiteral("%1 owns its own exposure - these have no effect here").arg(cam));

  // Park the tuner on an available mode.
  const int idx = static_cast<int>(cfg_.num("shoot_mode", 0));
  if (idx >= 0 && idx < why.size() && !why[idx].isEmpty()) {
    for (int i = 0; i < why.size(); ++i)
      if (why[i].isEmpty()) {
        tuner_->setIndex(i);
        break;
      }
  }
  refreshGoButton();
}

void MainWindow::stepMode(int delta) {
  if (capture_->recording()) return;  // never switch mode mid-capture
  tuner_->step(delta);
}

void MainWindow::toggleFullscreen() {
  if (fullscreen_) {
    fullscreen_->close();
    return;
  }
  fullscreenPreview_ = new PreviewWidget;
  fullscreenPreview_->copyViewSettings(*preview_);
  connect(fullscreenPreview_, &PreviewWidget::doubleClicked, this,
          &MainWindow::toggleFullscreen);
  connect(fullscreenPreview_, &PreviewWidget::overlaysToggled, this,
          &MainWindow::setAllOverlays);
  fullscreen_ = new FullscreenPreview(fullscreenPreview_);
  connect(fullscreen_, &FullscreenPreview::closed, this, [this] {
    fullscreen_->deleteLater();
    fullscreen_ = nullptr;
    fullscreenPreview_ = nullptr;
  });
  fullscreen_->showFullScreen();
}

void MainWindow::setAllOverlays(bool on) {
  for (PreviewWidget* p : {preview_, fullscreenPreview_}) {
    if (!p) continue;
    p->showZebra = p->showZones = p->showGrid = p->showPeaking = on;
    p->showFocusMap = p->showSharpness = p->showHud = on;
  }
  for (QCheckBox* c : {chkFmap_, chkSharpNum_, chkPeak2_, chkZebra2_, chkZones_, chkGrid_,
                       chkHud_}) {
    const QSignalBlocker block(c);
    c->setChecked(on);
  }
  statusBar()->showMessage(
      on ? QStringLiteral("all overlays on") : QStringLiteral("all overlays off"), 2000);
}

void MainWindow::outdoorChanged(bool on) {
  preview_->outdoor = on;
  preview_->outdoorStyle = QString::fromStdString(cfg_.str("outdoor_style", "boost"));
  preview_->stripePx = static_cast<int>(cfg_.num("outdoor_stripe_px", 3));
  preview_->outdoorStrength = cfg_.num("outdoor_strength", 1.0);
  faceField_->setOutdoor(on, cmbOutdoor_->currentIndex());
}

void MainWindow::log(const QString& msg) {
  if (logView_)
    logView_->append(QStringLiteral("%1  %2")
                         .arg(QTime::currentTime().toString(QStringLiteral("HH:mm:ss")), msg));
  statusBar()->showMessage(msg, 6000);
}

void MainWindow::openPath(const QString& path) {
  if (!QDesktopServices::openUrl(QUrl::fromLocalFile(path)))
    log(QStringLiteral("open failed: %1").arg(path));
}

// -------------------------------------------------------- capture events --

void MainWindow::onFrame(const FramePacket& p) {
  if (!p.y || p.y->empty()) return;
  lastFrameAt_ = mono_now();
  last_ = p;

  preview_->skyZoneFrac = cfg_.num("sky_zone_frac", 0.40);
  preview_->setFrame(*p.y, p.stats, p.color.get());
  histogram_->target = cfg_.num("target_luma", 118.0);
  histogram_->setFrame(*p.y, p.stats);

  if (preview_->showFocusMap || preview_->showSharpness)
    preview_->setFocusMap(bs::focus_map(*p.y));

  const QString shutter = QString::fromStdString(bs::describe_shutter(p.exposureUs));
  const QString shutterDir =
      QString::fromStdString(bs::shutter_dir(p.exposureUs));
  lblLine_->setText(QStringLiteral("%1   g%2   %3   %4 lux   %5 fps   clip %6%   sharp %7")
                        .arg(shutter)
                        .arg(p.gain, 0, 'f', 2)
                        .arg(shutterDir)
                        .arg(p.lux, 0, 'f', 0)
                        .arg(p.fps, 0, 'f', 1)
                        .arg(p.stats.clip_hi * 100, 0, 'f', 2)
                        .arg(p.stats.focus_measured
                                 ? QString::number(p.stats.sharpness_norm, 'f', 1)
                                 : QStringLiteral("-")));
  const QString verdict = QString::fromStdString(p.stats.verdict);
  lblVerdictRead_->setText(verdict.toUpper());
  lblVerdictRead_->setStyleSheet(QStringLiteral("color:%1;font-weight:600;")
                                     .arg(theme::verdictColor(verdict).name()));

  HudInfo hud;
  hud.valid = true;
  hud.line1 = QStringLiteral("%1   g%2   %3")
                  .arg(shutter)
                  .arg(p.gain, 0, 'f', 2)
                  .arg(shutterDir);
  hud.line2 = QStringLiteral("%1 lux   %2 fps   clip %3%")
                  .arg(p.lux, 0, 'f', 0)
                  .arg(p.fps, 0, 'f', 1)
                  .arg(p.stats.clip_hi * 100, 0, 'f', 2);
  hud.ae = p.recording ? QStringLiteral("engine · recording")
                       : QStringLiteral("%1%2  meter %3 -> %4")
                             .arg(p.aeMode, p.settled ? QStringLiteral(" ✓") : QString())
                             .arg(p.stats.meter, 0, 'f', 0)
                             .arg(p.target, 0, 'f', 0);
  hud.verdict = verdict;
  if (p.recording && capture_->mode() == bs::Mode::Timelapse) {
    const double interval = cfg_.num("timelapse_interval_s", 5.0);
    hud.interval = interval;
    hud.countdown = interval;  // reset by each frame; painted decreasing on the tick
  }
  lastHud_ = hud;
  preview_->setHud(hud);

  if (focusMonitor_ && focusMonitor_->isVisible()) focusMonitor_->handleFrame(p);
  if (fullscreenPreview_) {
    fullscreenPreview_->setFrame(*p.y, p.stats, p.color.get());
    fullscreenPreview_->setHud(hud);
    if (fullscreenPreview_->showFocusMap || fullscreenPreview_->showSharpness)
      fullscreenPreview_->setFocusMap(bs::focus_map(*p.y));
  }

  if (p.stats.focus_measured) lastSharpness_ = p.stats.sharpness_norm;
  lblFocusLive_->setText(QStringLiteral("sharpness %1   peak %2\nshutter %3 gain %4   contrast "
                                        "tiles %5")
                             .arg(p.stats.sharpness_norm, 6, 'f', 1)
                             .arg(preview_->focusPeakHold(), 8, 'f', 0)
                             .arg(shutter, -12)
                             .arg(p.gain, 4, 'f', 2)
                             .arg(p.stats.contrast_tiles));
  const double ref = cfg_.num("sharpness_reference", 0.0);
  lblSharpRef_->setText(
      ref > 0 ? QStringLiteral("reference %1   ->   frames below %2 are flagged soft")
                    .arg(ref, 0, 'f', 1)
                    .arg(cfg_.num("blur_threshold", 2.0), 0, 'f', 1)
              : QStringLiteral("not set - blur gate is at %1 and will not reject anything")
                    .arg(cfg_.num("blur_threshold", 2.0), 0, 'f', 1));

  if (p.recording) {
    sessionFrames_ = p.seq > 0 ? p.seq : sessionFrames_ + 1;
    counts_[verdict] += 1;
    lblCounts_->setText(QStringLiteral("ok %1 | dark %2 | blown %3 | empty %4  (%5 frames)")
                            .arg(counts_[QStringLiteral("ok")])
                            .arg(counts_[QStringLiteral("dark")])
                            .arg(counts_[QStringLiteral("blown")])
                            .arg(counts_[QStringLiteral("empty")])
                            .arg(sessionFrames_));
  }
}

void MainWindow::onSighting(const bs::Sighting& s, qlonglong takeN, bool fired) {
  if (s.has_subject_box) {
    const QRect box(s.bbox_x0, s.bbox_y0, s.bbox_x1 - s.bbox_x0, s.bbox_y1 - s.bbox_y0);
    preview_->setBird(box,
                      QStringLiteral("sharp %1 · %2%")
                          .arg(s.sharpness, 0, 'f', 1)
                          .arg(s.area_frac * 100, 0, 'f', 2),
                      fired, fired ? 1.6 : 0.5);
  }
  faceField_->onSighting(s, takeN, fired);
  if (fired) {
    lblBird_->setText(QStringLiteral("TAKE #%1  sharp %2, %3% of frame")
                          .arg(takeN)
                          .arg(s.sharpness, 0, 'f', 1)
                          .arg(s.area_frac * 100, 0, 'f', 2));
    statusBar()->showMessage(QStringLiteral("bird! burst fired"), 3000);
  } else if (s.present) {
    QString why;
    for (const auto& r : s.reasons) {
      if (!why.isEmpty()) why += QStringLiteral(", ");
      why += QString::fromStdString(r);
    }
    lblBird_->setText(QStringLiteral("subject seen -- holding: %1")
                          .arg(why.isEmpty() ? QStringLiteral("judging...") : why));
  }
}

void MainWindow::onRecordingStarted(const QString& modeName) {
  log(QStringLiteral("%1 started").arg(modeName));
  refreshGoButton();
}

void MainWindow::onRecordingFinished(const QString& summary, const QString& sessionDir,
                                     bool clean) {
  log((clean ? QStringLiteral("session closed: %1") : QStringLiteral("session stopped: %1"))
          .arg(summary));
  lblSessionRead_->setText(QFileInfo(sessionDir).fileName());
  refreshGoButton();
  if (stateName() == QStringLiteral("idle")) lblBird_->setText(QStringLiteral("idle"));
  capture_->startPreview();
  // Hand the Camera face its thumbnail: the newest frame of the session.
  QString newest;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(sessionDir.toStdString(), ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) break;
    if (it->is_regular_file(ec) && it->path().extension() == ".jpg") {
      const QString p = QString::fromStdString(it->path().string());
      if (p > newest) newest = p;
    }
  }
  if (!newest.isEmpty()) faceCamera_->onFrameSaved(newest);
  if (currentFace_ == QStringLiteral("library")) faceLibrary_->refresh();
}

// ------------------------------------------------------------------ tick --

void MainWindow::refreshStatusTick() {
  const std::string root = bs::expand_user(cfg_.str("data_root", "~/birdshot-data"));
  const double freeMb = static_cast<double>(bs::free_space_mb(root));
  lblFreeBar_->setText(QStringLiteral("%1 GB free").arg(freeMb / 1024.0, 0, 'f', 1));

  faceField_->refreshStatus(
      capture_->recording()
          ? QStringLiteral("recording · %1 frames").arg(sessionFrames_)
          : QStringLiteral("no session"),
      QStringLiteral("free %1 GB").arg(freeMb / 1024.0, 0, 'f', 1));

  // Timelapse countdown: the ring runs down on the wall clock between
  // frames, over the last frame's HUD lines.
  if (capture_->recording() && capture_->mode() == bs::Mode::Timelapse && lastFrameAt_ > 0 &&
      lastHud_.valid) {
    lastHud_.interval = cfg_.num("timelapse_interval_s", 5.0);
    lastHud_.countdown = std::max(0.0, lastHud_.interval - (mono_now() - lastFrameAt_));
    preview_->setHud(lastHud_);
    preview_->update();
  }
  refreshSunLabel();

  // The blocking overlay: capture has a floor, and when the disk is under
  // it the engine will refuse to run. Impossible to miss, by design.
  const double minFree = cfg_.num("min_free_mb", 2048);
  const bool full = freeMb < minFree;
  if (full && !spaceFull_) {
    spaceFull_ = true;
    overlay_->showMessage(
        QStringLiteral("OUT OF SPACE"),
        QStringLiteral("Capture has stopped. The capture drive is under the floor.\n\n  %1: %2 "
                       "GB free (floor %3 GB)\n\nFree space on the capture drive, or lower the "
                       "floor in Machine > Paths.")
            .arg(QString::fromStdString(root))
            .arg(freeMb / 1024.0, 0, 'f', 1)
            .arg(minFree / 1024.0, 0, 'f', 1));
    log(QStringLiteral("OUT OF SPACE - capture stopped"));
  } else if (!full && spaceFull_) {
    spaceFull_ = false;
    overlay_->hide();
  }

  refreshSummaries();
  refreshProvenance();
}

void MainWindow::dismissOverlay() {
  if (!spaceFull_) overlay_->hide();
}

void MainWindow::refreshSummaries() {
  auto summary = [&](const QString& prefix, const QString& text) {
    for (auto it = sections_.cbegin(); it != sections_.cend(); ++it)
      if (it.key().startsWith(prefix)) it.value()->setSummary(text);
  };
  const bool ae = cfg_.boolean("auto_exposure", true);
  summary(QStringLiteral("Stills"),
          QStringLiteral("640x480, quality gates on, %1")
              .arg(ae ? QStringLiteral("auto exposure") : QStringLiteral("manual")));
  const int rc = static_cast<int>(cfg_.num("rapid_count", 0));
  summary(QStringLiteral("Rapid"),
          QStringLiteral("%1 frame limit")
              .arg(rc ? QString::number(rc) : QStringLiteral("no")));
  const int tc = static_cast<int>(cfg_.num("timelapse_count", 0));
  summary(QStringLiteral("Timelapse"),
          QStringLiteral("every %1 s, %2 frames")
              .arg(cfg_.num("timelapse_interval_s", 5.0), 0, 'f', 1)
              .arg(tc ? QString::number(tc) : QStringLiteral("unlimited")));
  summary(QStringLiteral("Exposure"),
          QStringLiteral("target %1, %2")
              .arg(cfg_.num("target_luma", 118.0), 0, 'f', 0)
              .arg(ae ? QStringLiteral("auto") : QStringLiteral("manual")));
  summary(QStringLiteral("Focus"),
          QStringLiteral("map %1, peaking %2, outdoor %3")
              .arg(preview_->showFocusMap ? QStringLiteral("on") : QStringLiteral("off"),
                   preview_->showPeaking ? QStringLiteral("on") : QStringLiteral("off"),
                   preview_->outdoor ? QStringLiteral("on") : QStringLiteral("off")));
  summary(QStringLiteral("Quality"),
          QStringLiteral("dark<%1, blown>%2%, blur<%3, rejects: %4")
              .arg(cfg_.num("dark_p95_max", 40.0), 0, 'f', 0)
              .arg(cfg_.num("blown_clip_frac", 0.35) * 100, 0, 'f', 0)
              .arg(cfg_.num("blur_threshold", 2.0), 0, 'f', 1)
              .arg(QString::fromStdString(cfg_.str("reject_action", "flag"))));
  summary(QStringLiteral("Paths"),
          QString::fromStdString(cfg_.str("data_root", "~/birdshot-data")));
  const int bfTakes = static_cast<int>(cfg_.num("bf_takes", 0));
  summary(QStringLiteral("Bird Flight"),
          QStringLiteral("burst %1, %2 take limit")
              .arg(static_cast<int>(cfg_.num("bf_burst", 5)))
              .arg(bfTakes ? QString::number(bfTakes) : QStringLiteral("no")));
}

// ---------------------------------------------------- search + provenance --

void MainWindow::indexSettings() {
  // Find each bound widget's form label and owning section.
  for (Bind& b : binds_) {
    QWidget* w = b.widget;
    for (QWidget* up = w ? w->parentWidget() : nullptr; up; up = up->parentWidget()) {
      // owning accordion?
      for (auto it = sections_.cbegin(); it != sections_.cend(); ++it) {
        if (it.value()->body() == up) {
          b.sectionAcc = it.value();
          b.tab = it.value()->property("tabIndex").toInt();
        }
      }
      // form label?
      if (!b.labelWidget && up->layout()) {
        if (auto* form = qobject_cast<QFormLayout*>(up->layout())) {
          if (QWidget* l = form->labelForField(w)) {
            b.labelWidget = l;
            b.label = static_cast<QLabel*>(l)->text();
          }
        }
      }
    }
    if (!b.labelWidget) {
      if (auto* c = qobject_cast<QCheckBox*>(b.widget)) {
        b.labelWidget = c;
        b.label = c->text();
      } else {
        b.label = b.key;
      }
    }
  }

  static const QMap<int, QString> tabNames{{0, QStringLiteral("Shoot")},
                                           {1, QStringLiteral("Scene")},
                                           {2, QStringLiteral("Machine")}};
  QStringList entries;
  for (const Bind& b : binds_) {
    QString sectionShort = b.sectionAcc ? b.sectionAcc->title() : QStringLiteral("?");
    const int cut = sectionShort.indexOf(QStringLiteral(" - "));
    if (cut > 0) sectionShort = sectionShort.left(cut);
    entries << QStringLiteral("%1   [%2 > %3]")
                   .arg(b.label, tabNames.value(b.tab, QStringLiteral("?")), sectionShort);
  }
  entries.sort();
  auto* completer = new QCompleter(entries, this);
  completer->setCaseSensitivity(Qt::CaseInsensitive);
  completer->setFilterMode(Qt::MatchContains);
  completer->setMaxVisibleItems(14);
  edSearch_->setCompleter(completer);
  connect(completer, QOverload<const QString&>::of(&QCompleter::activated), this,
          &MainWindow::searchPicked);
}

void MainWindow::searchPicked(const QString& text) {
  QTimer::singleShot(0, this, [this] { edSearch_->clear(); });
  const QString label = text.section(QStringLiteral("   ["), 0, 0);
  for (const Bind& b : binds_) {
    if (b.label != label) continue;
    setFace(QStringLiteral("bench"));
    if (b.tab >= 0 && b.tab < tabs_->count()) tabs_->setCurrentIndex(b.tab);
    if (b.sectionAcc) b.sectionAcc->setExpanded(true);
    QWidget* w = b.widget;
    const int tab = b.tab;
    QTimer::singleShot(60, this, [this, w, tab] {
      if (tab >= 0 && tab < tabScrolls_.size())
        tabScrolls_[tab]->ensureWidgetVisible(w, 50, 120);
      const QString old = w->styleSheet();
      w->setStyleSheet(old + QStringLiteral(";border:1px solid #e0a828;"));
      QTimer::singleShot(1600, w, [w, old] { w->setStyleSheet(old); });
    });
    return;
  }
}

void MainWindow::refreshProvenance() {
  const bs::Json defaults = bs::Config::defaults();
  QSet<QString> changedKeys;  // a key bound twice still counts once
  for (Bind& b : binds_) {
    const std::string key = b.key.toStdString();
    if (!defaults.contains(key)) continue;
    const bool diff = cfg_.get(key).dump() != defaults.get(key).dump();
    if (diff) changedKeys.insert(b.key);
    if (diff == b.wasChanged) continue;
    b.wasChanged = diff;
    if (b.labelWidget) {
      b.labelWidget->setStyleSheet(diff ? QStringLiteral("color:#e0a828;") : QString());
      b.labelWidget->setToolTip(
          diff ? QStringLiteral("changed from default (%1)").arg(jsonRepr(defaults.get(key)))
               : QString());
    }
  }
  const int changed = changedKeys.size();
  lblChanged_->setText(changed == 0
                           ? QStringLiteral("stock configuration")
                           : QStringLiteral("%1 setting%2 differ%3 from defaults")
                                 .arg(changed)
                                 .arg(changed == 1 ? QString() : QStringLiteral("s"))
                                 .arg(changed == 1 ? QStringLiteral("s") : QString()));
}

void MainWindow::openResetDialog() {
  const bs::Json defaults = bs::Config::defaults();
  QStringList changedKeys;
  for (const Bind& b : binds_) {
    const std::string key = b.key.toStdString();
    if (defaults.contains(key) && cfg_.get(key).dump() != defaults.get(key).dump())
      changedKeys << b.key;
  }
  changedKeys.sort();
  changedKeys.removeDuplicates();

  QDialog dlg(this);
  dlg.setWindowTitle(QStringLiteral("Settings changed from defaults"));
  dlg.resize(560, 420);
  auto* v = new QVBoxLayout(&dlg);
  auto* list = new QListWidget;
  if (changedKeys.isEmpty()) {
    list->addItem(QStringLiteral("stock configuration - nothing to reset"));
  } else {
    for (const QString& k : changedKeys)
      list->addItem(QStringLiteral("%1\n      now %2,  default %3")
                        .arg(k, jsonRepr(cfg_.get(k.toStdString())),
                             jsonRepr(defaults.get(k.toStdString()))));
  }
  v->addWidget(list, 1);
  auto* buttons = new QDialogButtonBox;
  auto* btnReset =
      buttons->addButton(QStringLiteral("Reset all to defaults"), QDialogButtonBox::DestructiveRole);
  btnReset->setEnabled(!changedKeys.isEmpty());
  buttons->addButton(QDialogButtonBox::Close);
  connect(buttons, &QDialogButtonBox::rejected, &dlg, &QDialog::reject);
  connect(btnReset, &QPushButton::clicked, &dlg, [&] {
    for (const QString& k : changedKeys)
      cfg_.set(k.toStdString(), defaults.get(k.toStdString()));
    saveCfg();
    afterSettingsSwap();
    log(QStringLiteral("restored %1 setting(s) to defaults").arg(changedKeys.size()));
    dlg.accept();
  });
  v->addWidget(buttons);
  dlg.exec();
}

void MainWindow::afterSettingsSwap() {
  for (const Bind& b : binds_)
    if (b.refresh) b.refresh();
  outdoorChanged(cfg_.boolean("outdoor_mode", false));
  tuner_->setIndex(static_cast<int>(cfg_.num("shoot_mode", 0)));
  capture_->rebuildBackend();
  applyCapabilities();
  refreshSummaries();
  refreshProvenance();
}

// --------------------------------------------------------------- cameras --

void MainWindow::populateCameras() {
  cameras_.clear();
  for (const auto& cam : bs::list_cameras(cfg_)) cameras_ << cam;

  const std::string wantBackend = cfg_.str("backend", "synthetic");
  const int wantIndex = static_cast<int>(cfg_.num("camera_index", 0));
  int selected = cameras_.size() - 1;  // synthetic is always last
  QStringList labels;
  for (int i = 0; i < cameras_.size(); ++i) {
    const auto& cam = cameras_[i];
    labels << (cam.backend == "synthetic"
                   ? QString::fromStdString(cam.model)
                   : QStringLiteral("%1  (%2)").arg(QString::fromStdString(cam.model),
                                                    QString::fromStdString(cam.backend)));
    if (cam.backend == wantBackend && cam.index == wantIndex) selected = i;
  }
  for (QComboBox* combo : {cmbCameraRail_, faceCamera_->cmbCamera}) {
    const QSignalBlocker block(combo);
    combo->clear();
    combo->addItems(labels);
    combo->setCurrentIndex(selected);
  }
  if (selected >= 0 && selected < labels.size()) faceField_->setCameraLabel(labels[selected]);
}

void MainWindow::switchCamera(int idx) {
  if (idx < 0 || idx >= cameras_.size()) return;
  if (capture_->recording()) {
    log(QStringLiteral("stop the capture before switching cameras"));
    populateCameras();  // snap the combos back to what is actually driving
    return;
  }
  const bs::CameraInfo cam = cameras_[idx];
  if (cam.backend == "replay") {
    // Re-picking replay re-asks, which is how you change folders.
    const QString start = QString::fromStdString(bs::expand_user(
        cfg_.str("replay_path", cfg_.str("data_root", "~/birdshot-data"))));
    const QString dir = QFileDialog::getExistingDirectory(
        this, QStringLiteral("Folder of stills to replay"), start);
    if (dir.isEmpty()) {
      populateCameras();
      return;
    }
    cfg_.set("replay_path", bs::Json(dir.toStdString()));
  }
  cfg_.set("backend", bs::Json(cam.backend));
  cfg_.set("camera_index", bs::Json(cam.index));
  saveCfg();
  capture_->rebuildBackend();

  const QString got = capture_->backendName();
  if (cam.backend != "synthetic" && got.startsWith(QStringLiteral("synthetic"))) {
    // make_backend fell back; say so where it cannot be missed.
    lblBanner_->setText(QStringLiteral(
        "could not open %1 -- using the synthetic sky (check camera permission)")
        .arg(QString::fromStdString(cam.model)));
    lblBanner_->show();
  } else {
    lblBanner_->hide();
    log(QStringLiteral("camera: %1").arg(got));
  }
  applyCapabilities();
  populateCameras();
}

// -------------------------------------------------------------- profiles --

static QString profilesDir(const bs::Config& cfg) {
  return QFileInfo(QString::fromStdString(cfg.path())).absolutePath() +
         QStringLiteral("/profiles");
}

void MainWindow::refreshProfiles(const QString& select) {
  const QSignalBlocker block(cmbProfile_);
  cmbProfile_->clear();
  cmbProfile_->addItem(QStringLiteral("(no profile active)"));
  std::error_code ec;
  QStringList names;
  for (auto it = fs::directory_iterator(profilesDir(cfg_).toStdString(), ec);
       !ec && it != fs::directory_iterator(); it.increment(ec)) {
    if (it->path().extension() == ".json")
      names << QString::fromStdString(it->path().stem().string());
  }
  names.sort(Qt::CaseInsensitive);
  for (const QString& n : names) {
    cmbProfile_->addItem(n);
    if (n == select) cmbProfile_->setCurrentIndex(cmbProfile_->count() - 1);
  }
}

void MainWindow::profileActivated(int idx) {
  if (idx <= 0) return;
  const QString name = cmbProfile_->itemText(idx);
  const QString path = profilesDir(cfg_) + QStringLiteral("/") + name + QStringLiteral(".json");
  QFile f(path);
  if (!f.open(QIODevice::ReadOnly)) {
    log(QStringLiteral("profile '%1' failed: could not read it").arg(name));
    refreshProfiles();
    return;
  }
  std::string err;
  const bs::Json data = bs::Json::parse(f.readAll().toStdString(), &err);
  if (!err.empty() || !data.is_object()) {
    log(QStringLiteral("profile '%1' failed: %2").arg(name, QString::fromStdString(err)));
    return;
  }
  int changed = 0;
  for (const auto& kv : data.obj()) {
    if (isMachineKey(kv.first)) continue;
    if (cfg_.get(kv.first).dump() != kv.second.dump()) {
      cfg_.set(kv.first, kv.second);
      ++changed;
    }
  }
  saveCfg();
  afterSettingsSwap();
  log(QStringLiteral("profile '%1' activated - %2 setting(s) changed").arg(name).arg(changed));
}

void MainWindow::profileSave(bool asNew) {
  QString name;
  if (!asNew && cmbProfile_->currentIndex() > 0) {
    name = cmbProfile_->currentText();
  } else {
    bool ok = false;
    name = QInputDialog::getText(
        this, QStringLiteral("New profile"),
        QStringLiteral(
            "Save the current settings as:\n(letters, digits, dots, dashes, spaces; 40 max)"),
        QLineEdit::Normal, QString(), &ok);
    if (!ok || name.trimmed().isEmpty()) return;
    name = name.trimmed();
    static const QRegularExpression valid(
        QStringLiteral("^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$"));
    if (!valid.match(name).hasMatch()) {
      QMessageBox::warning(this, QStringLiteral("Profile"),
                           QStringLiteral("That name will not work as a file name."));
      return;
    }
  }
  const bs::Json snap = cfg_.snapshot();
  bs::Json out = bs::Json::object();
  if (snap.is_object())
    for (const auto& kv : snap.obj())
      if (!isMachineKey(kv.first)) out[kv.first] = kv.second;
  const QString dir = profilesDir(cfg_);
  std::error_code ec;
  fs::create_directories(dir.toStdString(), ec);
  QFile f(dir + QStringLiteral("/") + name + QStringLiteral(".json"));
  if (!f.open(QIODevice::WriteOnly)) {
    log(QStringLiteral("profile save failed: could not write the file"));
    return;
  }
  f.write(QByteArray::fromStdString(out.dump(2)));
  refreshProfiles(name);
  log(QStringLiteral("profile '%1' saved").arg(name));
}

void MainWindow::profileDelete() {
  const int idx = cmbProfile_->currentIndex();
  if (idx <= 0) return;
  const QString name = cmbProfile_->currentText();
  const auto answer = QMessageBox::question(
      this, QStringLiteral("Delete profile"),
      QStringLiteral(
          "Delete the profile '%1'?\n\nCurrent settings stay as they are; only the saved file "
          "goes.")
          .arg(name),
      QMessageBox::Yes | QMessageBox::No, QMessageBox::No);
  if (answer != QMessageBox::Yes) return;
  std::error_code ec;
  fs::remove((profilesDir(cfg_) + QStringLiteral("/") + name + QStringLiteral(".json"))
                 .toStdString(),
             ec);
  refreshProfiles();
  log(QStringLiteral("profile '%1' deleted").arg(name));
}

// ---------------------------------------------------------------- doctor --

void MainWindow::runDoctor() {
  if (doctorRunning_) return;
  doctorRunning_ = true;
  // The checks probe the backend and the disk, so they run off the GUI
  // thread and re-enter through a queued call.
  std::thread([this] {
    struct DocRow {
      bool ok;
      QString name;
      QString detail;
      bool note = false;  // a choice not yet made, not a fault: never a FAIL
    };
    QList<DocRow> rows;
    rows << DocRow{true, QStringLiteral("version"),
                   QStringLiteral("%1 (%2)").arg(QString::fromLatin1(bs::kVersion),
                                                 QString::fromLatin1(bs::kCodename))};
    rows << DocRow{cfg_.save(), QStringLiteral("config writable"),
                   QString::fromStdString(cfg_.path())};
    const std::string root = bs::expand_user(cfg_.str("data_root", "~/birdshot-data"));
    const double freeMb = static_cast<double>(bs::free_space_mb(root));
    rows << DocRow{freeMb > cfg_.num("min_free_mb", 2048), QStringLiteral("storage"),
                   QStringLiteral("%1 (%2 GB free)")
                       .arg(QString::fromStdString(root))
                       .arg(freeMb / 1024.0, 0, 'f', 1)};
    rows << DocRow{true, QStringLiteral("backend"), capture_->backendName()};
    // The site is optional: capture and the synthetic scene run without one;
    // only sun/plan/align need it. Unset is a note, not a failure.
    const bool site = cfg_.boolean("site_set", false);
    rows << DocRow{true, QStringLiteral("site"),
                   site ? QStringLiteral("set")
                        : QStringLiteral("not set (optional) -- sun/plan/align need it"),
                   !site};
    QMetaObject::invokeMethod(this, [this, rows] {
      doctorRunning_ = false;
      QStringList lines, fails, notes;
      for (const auto& r : rows) {
        lines << QStringLiteral("%1  %2 %3")
                     .arg(r.note ? QStringLiteral("--  ")
                                 : r.ok ? QStringLiteral("ok  ") : QStringLiteral("BAD "),
                          r.name.leftJustified(14), r.detail);
        if (!r.ok) fails << r.name;
        else if (r.note) notes << r.name;
      }
      txtDoctor_->setText(lines.join(QStringLiteral("\n")));
      lblDoctorStamp_->setText(QStringLiteral("checked %1").arg(
          QTime::currentTime().toString(QStringLiteral("HH:mm:ss"))));
      const QString chip =
          !fails.isEmpty() ? QStringLiteral("doctor: FAIL - %1").arg(fails.join(QStringLiteral(", ")))
          : !notes.isEmpty() ? QStringLiteral("doctor: ok (%1 unset)").arg(notes.join(QStringLiteral(", ")))
                             : QStringLiteral("doctor: ok");
      btnDoctorChip_->setText(chip);
      btnDoctorChip_->setStyleSheet(
          QStringLiteral("QPushButton{border:none;background:transparent;color:%1;"
                         "font-family:monospace;font-size:11px;}"
                         "QPushButton:hover{text-decoration:underline;}")
              .arg(fails.isEmpty() ? QStringLiteral("#5fd07a") : QStringLiteral("#ff6a44")));
      if (sections_.contains(QStringLiteral("Install health - doctor")))
        sections_[QStringLiteral("Install health - doctor")]->setSummary(chip);
    }, Qt::QueuedConnection);
  }).detach();
}

void MainWindow::setSharpnessReference() {
  if (lastSharpness_ <= 0) {
    QMessageBox::information(this, QStringLiteral("No reading yet"),
                             QStringLiteral("Wait for a live frame with a sharpness reading "
                                            "first."));
    return;
  }
  cfg_.set("sharpness_reference", bs::Json(lastSharpness_));
  cfg_.set("blur_threshold", bs::Json(std::round(lastSharpness_ * 0.5 * 100) / 100.0));
  saveCfg();
  log(QStringLiteral("sharp reference %1, blur gate now %2")
          .arg(lastSharpness_, 0, 'f', 1)
          .arg(lastSharpness_ * 0.5, 0, 'f', 1));
}

// ----------------------------------------------------------------- shell --

void MainWindow::resizeEvent(QResizeEvent* e) {
  overlay_->setGeometry(rect());
  QMainWindow::resizeEvent(e);
}

void MainWindow::closeEvent(QCloseEvent* e) {
  statusBar()->showMessage(QStringLiteral("finishing capture..."));
  capture_->stopRecording();
  capture_->stopPreview();
  cfg_.save();
  QMainWindow::closeEvent(e);
}
