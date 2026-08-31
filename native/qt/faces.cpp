// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "faces.hpp"

#include <algorithm>
#include <chrono>
#include <filesystem>

#include <QDesktopServices>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QIcon>
#include <QMessageBox>
#include <QPixmap>
#include <QScrollArea>
#include <QSplitter>
#include <QTime>
#include <QTimer>
#include <QUrl>

#include "birdshot/config.hpp"
#include "birdshot/naming.hpp"
#include "birdshot/storage.hpp"
#include "theme.hpp"
#include "widgets.hpp"
#include "window.hpp"

namespace fs = std::filesystem;

namespace {

constexpr int kAnalysisW = 640;
constexpr int kAnalysisH = 480;
constexpr int kThumbBatch = 32;
constexpr int kThumbLimit = 480;

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

QIcon iconFrom(const QPixmap& pm, int w, int h) {
  return QIcon(pm.scaled(w, h, Qt::KeepAspectRatioByExpanding, Qt::SmoothTransformation));
}

// The gate table, in judging order: title, the detector's reason string,
// and how the value column renders.
struct GateSpec {
  const char* title;
  const char* reason;
};
const GateSpec kGates[] = {
    {"motion", "no motion"},
    {"sky in frame", "frame not sky enough"},
    {"subject found", "no subject"},
    {"subject size", "subject too small"},
    {"sky ring", "not against sky"},
    {"inside margin", "subject too near the edge"},
    {"boundary sharp", "boundary not sharp enough"},
};

bool hasReason(const bs::Sighting& s, const std::string& r) {
  for (const auto& x : s.reasons)
    if (x.rfind(r, 0) == 0) return true;
  return false;
}

}  // namespace

// ------------------------------------------------------------ GateLadder --

GateLadder::GateLadder(MainWindow* win, QWidget* parent) : QWidget(parent), win_(win) {
  setStyleSheet(
      "QWidget{background:#14202a;border:none;}QLabel{border:none;background:transparent;}");
  auto* lay = new QVBoxLayout(this);
  lay->setContentsMargins(1, 1, 1, 1);
  lay->setSpacing(0);

  auto* head = new QLabel(QStringLiteral("AUTO-TAKE GATES"));
  head->setStyleSheet(
      "color:#9fd0ff;font-weight:800;font-size:10px;letter-spacing:1px;padding:7px 10px;");
  lay->addWidget(head);

  for (const auto& g : kGates) {
    auto* row = new QWidget;
    auto* rl = new QHBoxLayout(row);
    rl->setContentsMargins(10, 4, 10, 4);
    auto* name = new QLabel(QString::fromLatin1(g.title));
    name->setStyleSheet("color:#cfe3ef;font-weight:600;font-size:12px;");
    rl->addWidget(name, 1);
    Row r;
    r.val = new QLabel(QStringLiteral("-"));
    r.val->setStyleSheet("color:#9fd0ff;font-family:monospace;font-size:11px;");
    rl->addWidget(r.val);
    r.thr = new QLabel;
    r.thr->setStyleSheet("color:#7a8791;font-family:monospace;font-size:10px;");
    r.thr->setMinimumWidth(74);
    r.thr->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    rl->addWidget(r.thr);
    r.tick = new QLabel(QStringLiteral("-"));
    r.tick->setFixedWidth(16);
    r.tick->setAlignment(Qt::AlignCenter);
    rl->addWidget(r.tick);
    rows_ << r;
    lay->addWidget(row);
  }
  footer_ = new QLabel;
  footer_->setWordWrap(true);
  lay->addWidget(footer_);
  setIdle(QStringLiteral("mode not running"));
}

void GateLadder::refreshThresholds() {
  // Re-read the live config every update so edits in Bench show at once.
  bs::Config& cfg = win_->cfg();
  rows_[0].thr->setText(QStringLiteral("≥ %1%").arg(cfg.num("bf_motion_min", 0.0005) * 100, 0,
                                                    'f', 2));
  rows_[1].thr->setText(
      QStringLiteral("≥ %1%").arg(cfg.num("bf_sky_min_frac", 0.5) * 100, 0, 'f', 0));
  rows_[2].thr->setText(QStringLiteral("≤ luma %1")
                            .arg(static_cast<int>(cfg.num("bf_subject_luma_max", 80))));
  rows_[3].thr->setText(QStringLiteral("%1–%2%")
                            .arg(cfg.num("bf_min_area_frac", 0.0004) * 100, 0, 'f', 2)
                            .arg(cfg.num("bf_max_area_frac", 0.05) * 100, 0, 'f', 1));
  rows_[4].thr->setText(
      QStringLiteral("≥ %1%").arg(cfg.num("bf_ring_sky_frac", 0.85) * 100, 0, 'f', 0));
  rows_[5].thr->setText(
      QStringLiteral("≥ %1%").arg(cfg.num("bf_margin_frac", 0.08) * 100, 0, 'f', 0));
  rows_[6].thr->setText(QStringLiteral("≥ %1").arg(cfg.num("bf_min_sharpness", 12.0), 0, 'f', 1));
}

void GateLadder::updateSighting(const bs::Sighting& s, qint64 takeN, bool fired) {
  refreshThresholds();
  bs::Config& cfg = win_->cfg();
  const bool requireMotion = cfg.boolean("bf_require_motion", true);
  const bool noSubject = hasReason(s, "no subject");

  rows_[0].val->setText(requireMotion
                            ? QStringLiteral("%1%").arg(s.motion_frac * 100, 0, 'f', 2)
                            : QStringLiteral("off"));
  rows_[1].val->setText(QStringLiteral("%1%").arg(s.sky_frac * 100, 0, 'f', 0));
  rows_[2].val->setText(noSubject ? QStringLiteral("no") : QStringLiteral("yes"));
  rows_[3].val->setText(noSubject ? QStringLiteral("-")
                                  : QStringLiteral("%1%").arg(s.area_frac * 100, 0, 'f', 2));
  rows_[4].val->setText(noSubject ? QStringLiteral("-")
                                  : QStringLiteral("%1%").arg(s.ring_sky_frac * 100, 0, 'f', 0));
  if (s.has_subject_box) {
    const double cx = s.centroid_x / kAnalysisW, cy = s.centroid_y / kAnalysisH;
    const double edge = std::min({cx, 1.0 - cx, cy, 1.0 - cy});
    rows_[5].val->setText(QStringLiteral("%1%").arg(edge * 100, 0, 'f', 0));
  } else {
    rows_[5].val->setText(QStringLiteral("-"));
  }
  rows_[6].val->setText(noSubject ? QStringLiteral("-")
                                  : QString::number(s.sharpness, 'f', 1));

  auto tick = [&](int i, int state) {  // 1 pass, 0 fail, -1 n/a
    const char* glyph = state > 0 ? "✓" : state == 0 ? "✗" : "–";
    const char* color = state > 0 ? theme::kPass : state == 0 ? theme::kFail : theme::kNa;
    rows_[i].tick->setText(QString::fromUtf8(glyph));
    rows_[i].tick->setStyleSheet(QStringLiteral("color:%1;font-weight:800;").arg(color));
  };
  for (int i = 0; i < rows_.size(); ++i) {
    if (i == 0 && !requireMotion) { tick(i, -1); continue; }
    if (noSubject && i > 2) { tick(i, -1); continue; }
    if (i == 2) { tick(i, noSubject ? 0 : 1); continue; }
    if (i == 3) {
      tick(i, hasReason(s, "subject too small") || hasReason(s, "subject too large") ? 0 : 1);
      continue;
    }
    tick(i, hasReason(s, kGates[i].reason) ? 0 : 1);
  }

  const double now = mono_now();
  if (fired) takeUntil_ = now + 3.0;
  if (now < takeUntil_) {
    footer_->setText(QStringLiteral("ALL GATES PASS — TAKE #%1 · burst %2 · cooldown %3 s")
                         .arg(takeN)
                         .arg(static_cast<int>(cfg.num("bf_burst", 5)))
                         .arg(cfg.num("bf_cooldown_s", 3.0), 0, 'f', 1));
    footer_->setStyleSheet(
        "color:#7fe3a2;font-size:12px;font-weight:800;padding:8px 10px;background:#173a24;");
  } else {
    QString why;
    for (const auto& r : s.reasons) {
      if (!why.isEmpty()) why += QStringLiteral(", ");
      why += QString::fromStdString(r);
    }
    footer_->setText(QStringLiteral("holding: %1")
                         .arg(why.isEmpty() ? QStringLiteral("judging...") : why));
    footer_->setStyleSheet(
        "color:#e0a828;font-size:12px;font-weight:700;padding:8px 10px;background:#101820;");
  }
}

void GateLadder::setIdle(const QString& text) {
  refreshThresholds();
  for (auto& r : rows_) {
    r.val->setText(QStringLiteral("-"));
    r.tick->setText(QString::fromUtf8("–"));
    r.tick->setStyleSheet(QStringLiteral("color:%1;font-weight:800;").arg(theme::kNa));
  }
  footer_->setText(text);
  footer_->setStyleSheet(
      "color:#93a3ad;font-size:12px;font-weight:700;padding:8px 10px;background:#101820;");
}

// ------------------------------------------------------------ CameraFace --

CameraFace::CameraFace(MainWindow* win, QWidget* parent) : QWidget(parent), win_(win) {
  setStyleSheet("background:#1b1f24;");
  auto* lay = new QVBoxLayout(this);
  lay->setContentsMargins(0, 0, 0, 0);
  lay->setSpacing(0);

  previewSlot = new QVBoxLayout;
  previewSlot->setContentsMargins(0, 0, 0, 0);
  lay->addLayout(previewSlot, 1);

  auto* bar = new QWidget;
  bar->setFixedHeight(112);
  bar->setStyleSheet("background:#20252b;border-top:1px solid #333a44;");
  auto* bl = new QHBoxLayout(bar);
  bl->setContentsMargins(18, 10, 18, 10);
  bl->setSpacing(18);

  btnThumb_ = new QPushButton(QStringLiteral("no shots\nyet"));
  btnThumb_->setFixedSize(92, 69);
  btnThumb_->setCursor(Qt::PointingHandCursor);
  btnThumb_->setToolTip(QStringLiteral("Open the Library"));
  btnThumb_->setStyleSheet(
      "QPushButton{background:#0d1013;border:1px solid #39414c;border-radius:4px;color:#7a8791;"
      "font-size:10px;}");
  connect(btnThumb_, &QPushButton::clicked, this,
          [this] { win_->setFace(QStringLiteral("library")); });
  bl->addWidget(btnThumb_);
  bl->addStretch(1);

  QStringList labels;
  for (const auto& m : win_->modes()) labels << m.label;
  tuner = new ModeTuner(labels, static_cast<int>(win_->cfg().num("shoot_mode", 0)));
  connect(tuner, &ModeTuner::changed, this, [this](int i) { win_->tuner()->setIndex(i); });
  bl->addWidget(tuner, 4);

  btnShutter_ = new QPushButton;
  btnShutter_->setFixedSize(78, 78);
  styleShutter(false);
  connect(btnShutter_, &QPushButton::clicked, this, &CameraFace::shutterClicked);
  bl->addWidget(btnShutter_);
  bl->addStretch(1);

  cmbCamera = new QComboBox;
  cmbCamera->setMinimumWidth(190);
  cmbCamera->setToolTip(QStringLiteral("Which camera drives capture."));
  bl->addWidget(cmbCamera);

  lay->addWidget(bar);
}

void CameraFace::styleShutter(bool running) {
  btnShutter_->setStyleSheet(
      QStringLiteral("QPushButton{border-radius:39px;border:3px solid #e8edf2;background:%1;}"
                     "QPushButton:pressed{background:#b9c2ca;}")
          .arg(running ? QStringLiteral("#a03020") : QStringLiteral("#e8edf2")));
  btnShutter_->setToolTip(running ? QStringLiteral("Stop") : QStringLiteral("Take"));
}

void CameraFace::shutterClicked() {
  // A camera app's shutter: one photo in Stills, start/stop elsewhere.
  if (win_->capture()->recording()) {
    win_->capture()->stopRecording();
    return;
  }
  const int idx = std::clamp(static_cast<int>(win_->cfg().num("shoot_mode", 0)), 0,
                             static_cast<int>(win_->modes().size()) - 1);
  const ModeSpec& m = win_->modes()[idx];
  if (m.key == QStringLiteral("stills")) {
    // No single-frame capability in the native engine yet: a burst of one,
    // which is what the engine honestly offers here.
    win_->capture()->startRecording(bs::Mode::Collect, 1);
    return;
  }
  win_->goClicked();
}

void CameraFace::updateGo(bool running, const QString&, const QString&) { styleShutter(running); }

void CameraFace::syncMode(int idx) { tuner->setIndex(idx); }

void CameraFace::onFrameSaved(const QString& path) {
  const double now = mono_now();
  if (now - lastThumb_ < 0.6) return;
  lastThumb_ = now;
  QPixmap pm(path);
  if (pm.isNull()) return;
  btnThumb_->setText(QString());
  btnThumb_->setIcon(iconFrom(pm, 88, 65));
  btnThumb_->setIconSize(btnThumb_->size() * 0.94);
}

// ------------------------------------------------------------- FieldFace --

FieldFace::FieldFace(MainWindow* win, QWidget* parent) : QWidget(parent), win_(win) {
  setStyleSheet("background:#14181d;");
  auto* lay = new QVBoxLayout(this);
  lay->setContentsMargins(0, 0, 0, 0);
  lay->setSpacing(0);

  auto* top = new QWidget;
  top->setFixedHeight(52);
  top->setStyleSheet("background:#20252b;border-bottom:1px solid #333a44;");
  auto* tl = new QHBoxLayout(top);
  tl->setContentsMargins(12, 8, 12, 8);
  tl->setSpacing(12);
  lblState_ = new QLabel(QStringLiteral("IDLE"));
  styleState(QStringLiteral("idle"));
  tl->addWidget(lblState_);
  lblMode_ = new QLabel;
  lblMode_->setStyleSheet("font-size:13px;font-weight:800;color:#cfe3ef;border:none;");
  tl->addWidget(lblMode_);
  lblCamera_ = new QLabel;
  lblCamera_->setStyleSheet("font-size:11px;color:#93a3ad;border:none;");
  tl->addWidget(lblCamera_);
  tl->addStretch(1);
  lay->addWidget(top);

  auto* mid = new QHBoxLayout;
  mid->setContentsMargins(0, 0, 0, 0);
  mid->setSpacing(0);
  previewSlot = new QVBoxLayout;
  previewSlot->setContentsMargins(0, 0, 0, 0);
  mid->addLayout(previewSlot, 1);

  auto* rail = new QWidget;
  rail->setFixedWidth(380);
  rail->setStyleSheet("background:#1b1f24;border-left:1px solid #333a44;");
  auto* rl = new QVBoxLayout(rail);
  rl->setContentsMargins(12, 12, 12, 12);
  rl->setSpacing(12);

  ladder = new GateLadder(win_);
  rl->addWidget(ladder);

  lblTakes_ = new QLabel(QStringLiteral("takes 0"));
  lblTakes_->setStyleSheet("font-family:monospace;font-size:11px;color:#93a3ad;border:none;");
  rl->addWidget(lblTakes_);

  // Compact labels for the 380 px rail.
  QStringList labels;
  for (const auto& m : win_->modes()) {
    QString l = m.label;
    l.replace(QStringLiteral("Timelapse"), QStringLiteral("Lapse"));
    l.replace(QStringLiteral("Bird Flight"), QStringLiteral("Bird"));
    labels << l;
  }
  tuner = new ModeTuner(labels, static_cast<int>(win_->cfg().num("shoot_mode", 0)), 11);
  connect(tuner, &ModeTuner::changed, this, [this](int i) { win_->tuner()->setIndex(i); });
  rl->addWidget(tuner);

  btnGo_ = new QPushButton;
  btnGo_->setCheckable(true);
  btnGo_->setMinimumHeight(72);
  connect(btnGo_, &QPushButton::clicked, this, [this] { win_->goClicked(); });
  rl->addWidget(btnGo_);

  auto* orow = new QHBoxLayout;
  btnOutdoor_ = new QToolButton;
  btnOutdoor_->setText(QStringLiteral("OUTDOOR MODE"));
  btnOutdoor_->setCheckable(true);
  btnOutdoor_->setCursor(Qt::PointingHandCursor);
  btnOutdoor_->setMinimumHeight(46);
  btnOutdoor_->setSizePolicy(tuner->sizePolicy());
  styleOutdoor(false);
  connect(btnOutdoor_, &QToolButton::toggled, this, [this](bool on) {
    styleOutdoor(on);
    // The Bench checkbox is the master; its handler does the real work.
    if (win_->chkOutdoor()->isChecked() != on) win_->chkOutdoor()->setChecked(on);
  });
  orow->addWidget(btnOutdoor_, 1);
  btnBoost_ = new QToolButton;
  btnBoost_->setText(QStringLiteral("boost"));
  btnEdges_ = new QToolButton;
  btnEdges_->setText(QStringLiteral("edges"));
  for (QToolButton* b : {btnBoost_, btnEdges_}) {
    b->setMinimumHeight(46);
    b->setCursor(Qt::PointingHandCursor);
    orow->addWidget(b);
  }
  connect(btnBoost_, &QToolButton::clicked, this, [this] {
    restyleStyleButtons(0);
    if (win_->cmbOutdoor()->currentIndex() != 0) win_->cmbOutdoor()->setCurrentIndex(0);
  });
  connect(btnEdges_, &QToolButton::clicked, this, [this] {
    restyleStyleButtons(1);
    if (win_->cmbOutdoor()->currentIndex() != 1) win_->cmbOutdoor()->setCurrentIndex(1);
  });
  restyleStyleButtons(0);
  rl->addLayout(orow);
  rl->addStretch(1);

  auto* hint = new QLabel(
      QStringLiteral("changed gates apply on the next watch — tune them in Bench"));
  hint->setWordWrap(true);
  hint->setStyleSheet("font-family:monospace;font-size:10px;color:#7a8791;border:none;");
  rl->addWidget(hint);

  mid->addWidget(rail);
  lay->addLayout(mid, 1);

  auto* bot = new QWidget;
  bot->setFixedHeight(32);
  bot->setStyleSheet("background:#20252b;border-top:1px solid #333a44;");
  auto* bl = new QHBoxLayout(bot);
  bl->setContentsMargins(14, 4, 14, 4);
  lblSession_ = new QLabel(QStringLiteral("-"));
  lblSession_->setStyleSheet("font-family:monospace;font-size:11px;color:#93a3ad;border:none;");
  bl->addWidget(lblSession_);
  bl->addStretch(1);
  lblFree_ = new QLabel(QStringLiteral("-"));
  lblFree_->setStyleSheet("font-family:monospace;font-size:11px;color:#93a3ad;border:none;");
  bl->addWidget(lblFree_);
  lay->addWidget(bot);

  syncMode(static_cast<int>(win_->cfg().num("shoot_mode", 0)));
}

void FieldFace::styleState(const QString& state) {
  static const QSet<QString> running{"burst", "rapid", "drain", "timelapse", "video",
                                     "birdflight"};
  const bool on = running.contains(state);
  lblState_->setText(state == QStringLiteral("birdflight") ? QStringLiteral("WATCHING")
                                                           : state.toUpper());
  lblState_->setStyleSheet(QStringLiteral(
      "QLabel{font-size:14px;font-weight:800;letter-spacing:0.5px;border-radius:5px;"
      "padding:6px 16px;%1}")
      .arg(on ? "background:#1f7a3f;color:#ffffff;border:2px solid #7fe3a2;"
              : "background:#232830;color:#93a3ad;border:1px solid #39414c;"));
}

void FieldFace::styleOutdoor(bool on) {
  btnOutdoor_->setStyleSheet(QStringLiteral(
      "QToolButton{border-radius:5px;font-size:13px;font-weight:800;%1}")
      .arg(on ? "background:#6a5a10;color:#ffe628;border:2px solid #ffe628;"
              : "background:#1b1f24;color:#ffe628;border:2px solid #6a5a10;"));
}

void FieldFace::restyleStyleButtons(int active) {
  QToolButton* btns[2] = {btnBoost_, btnEdges_};
  for (int i = 0; i < 2; ++i)
    btns[i]->setStyleSheet(QStringLiteral(
        "QToolButton{border-radius:5px;font-size:12px;padding:0 16px;%1}")
        .arg(i == active
                 ? "background:#2f6f8f;color:#ffffff;font-weight:800;border:1px solid #7fd0f0;"
                 : "background:#232830;color:#93a3ad;font-weight:600;border:1px solid #39414c;"));
}

void FieldFace::updateGo(bool running, const QString& state, const QString& label) {
  btnGo_->setChecked(running);
  btnGo_->setText(running ? QStringLiteral("STOP — %1").arg(state.toUpper())
                          : QStringLiteral("START — %1").arg(label.toUpper()));
  btnGo_->setStyleSheet(QStringLiteral(
      "QPushButton{font-size:17px;font-weight:800;border-radius:6px;background:%1;color:white;"
      "border:none;}")
      .arg(running ? QStringLiteral("#a03020") : QStringLiteral("#1f7a3f")));
  styleState(state);
  if (state != QStringLiteral("birdflight"))
    ladder->setIdle(state == QStringLiteral("idle") || state == QStringLiteral("preview")
                        ? QStringLiteral("select Bird Flight and START to watch")
                        : QStringLiteral("mode not running"));
}

void FieldFace::syncMode(int idx) {
  tuner->setIndex(idx);
  if (idx >= 0 && idx < win_->modes().size())
    lblMode_->setText(win_->modes()[idx].label.toUpper());
}

void FieldFace::setCameraLabel(const QString& text) { lblCamera_->setText(text); }

void FieldFace::setOutdoor(bool on, int styleIdx) {
  const QSignalBlocker block(btnOutdoor_);
  btnOutdoor_->setChecked(on);
  styleOutdoor(on);
  restyleStyleButtons(styleIdx);
}

void FieldFace::refreshStatus(const QString& sessionText, const QString& freeText) {
  lblSession_->setText(sessionText);
  lblFree_->setText(freeText);
}

void FieldFace::onSighting(const bs::Sighting& s, qint64 takeN, bool fired) {
  ladder->updateSighting(s, takeN, fired);
  if (fired) {
    const int limit = static_cast<int>(win_->cfg().num("bf_takes", 0));
    lblTakes_->setText(QStringLiteral("takes %1 · last %2 · limit %3")
                           .arg(takeN)
                           .arg(QTime::currentTime().toString(QStringLiteral("HH:mm:ss")))
                           .arg(limit ? QString::number(limit) : QStringLiteral("off")));
  }
}

// ----------------------------------------------------------- LibraryFace --

LibraryFace::LibraryFace(MainWindow* win, QWidget* parent) : QWidget(parent), win_(win) {
  setStyleSheet("background:#1b1f24;");
  auto* lay = new QVBoxLayout(this);
  lay->setContentsMargins(0, 0, 0, 0);
  lay->setSpacing(0);

  auto* bar = new QWidget;
  bar->setFixedHeight(44);
  bar->setStyleSheet("background:#1e2329;border-bottom:1px solid #333a44;");
  auto* bl = new QHBoxLayout(bar);
  bl->setContentsMargins(14, 6, 14, 6);
  bl->setSpacing(10);
  const QStringList filters{QStringLiteral("All"), QStringLiteral("OK only"),
                            QStringLiteral("Takes")};
  const QStringList names{QStringLiteral("all"), QStringLiteral("ok"), QStringLiteral("takes")};
  for (int i = 0; i < filters.size(); ++i) {
    auto* b = new QToolButton;
    b->setText(filters[i]);
    b->setCursor(Qt::PointingHandCursor);
    const QString name = names[i];
    connect(b, &QToolButton::clicked, this, [this, name] {
      filter_ = name;
      restyleFilters();
      reloadGrid();
    });
    filterButtons_ << b;
    bl->addWidget(b);
  }
  bl->addStretch(1);
  auto* btnOpen = new QPushButton(QStringLiteral("Open folder"));
  auto* btnRescan = new QPushButton(QStringLiteral("Rescan"));
  for (QPushButton* b : {btnOpen, btnRescan}) {
    b->setMinimumHeight(30);
    bl->addWidget(b);
  }
  connect(btnOpen, &QPushButton::clicked, this, [this] {
    win_->openPath(sessionPath_.isEmpty()
                       ? QString::fromStdString(
                             bs::expand_user(win_->cfg().str("data_root", "~/birdshot-data")))
                       : sessionPath_);
  });
  connect(btnRescan, &QPushButton::clicked, this, &LibraryFace::refresh);
  lay->addWidget(bar);
  restyleFilters();

  auto* split = new QSplitter(Qt::Horizontal);

  auto* left = new QWidget;
  auto* ll = new QVBoxLayout(left);
  ll->setContentsMargins(0, 0, 0, 0);
  ll->setSpacing(0);
  auto* head = new QLabel(QStringLiteral("SESSIONS"));
  head->setStyleSheet(
      "color:#7a8791;font-size:10px;letter-spacing:1px;padding:9px 12px;"
      "border-bottom:1px solid #2a313a;");
  ll->addWidget(head);
  lstSessions_ = new QListWidget;
  lstSessions_->setStyleSheet(
      "QListWidget{background:#20252b;border:none;font-size:12px;}"
      "QListWidget::item{padding:8px 12px;border-bottom:1px solid #2a313a;}"
      "QListWidget::item:selected{background:#2f6f8f;color:#ffffff;}");
  connect(lstSessions_, &QListWidget::currentRowChanged, this, [this](int) { sessionPicked(); });
  ll->addWidget(lstSessions_, 1);
  split->addWidget(left);

  grid_ = new QListWidget;
  grid_->setViewMode(QListView::IconMode);
  grid_->setResizeMode(QListView::Adjust);
  grid_->setIconSize(QSize(160, 120));
  grid_->setUniformItemSizes(true);
  grid_->setSpacing(10);
  grid_->setStyleSheet(
      "QListWidget{background:#1b1f24;border:none;font-size:10px;}"
      "QListWidget::item{color:#93a3ad;}"
      "QListWidget::item:selected{background:#2f6f8f;color:#ffffff;}");
  connect(grid_, &QListWidget::currentRowChanged, this, [this](int) { framePicked(); });
  split->addWidget(grid_);

  auto* right = new QWidget;
  right->setStyleSheet("background:#20252b;");
  auto* rl = new QVBoxLayout(right);
  rl->setContentsMargins(14, 14, 14, 14);
  rl->setSpacing(10);
  lblBig_ = new QLabel(QStringLiteral("select a frame"));
  lblBig_->setAlignment(Qt::AlignCenter);
  lblBig_->setMinimumHeight(196);
  lblBig_->setStyleSheet(
      "background:#0d1013;border:1px solid #39414c;border-radius:4px;color:#7a8791;");
  rl->addWidget(lblBig_);
  lblFacts_ = new QLabel(QStringLiteral("-"));
  lblFacts_->setWordWrap(true);
  lblFacts_->setTextInteractionFlags(Qt::TextSelectableByMouse);
  lblFacts_->setStyleSheet("font-family:monospace;font-size:11px;color:#cfd6dd;");
  rl->addWidget(lblFacts_);
  lblTrigger_ = new QLabel;
  lblTrigger_->setWordWrap(true);
  lblTrigger_->setStyleSheet("font-family:monospace;font-size:11px;color:#9fd0ff;");
  rl->addWidget(lblTrigger_);
  rl->addStretch(1);
  btnRef_ = new QPushButton(QStringLiteral("Use as sharpness reference"));
  btnRef_->setMinimumHeight(38);
  connect(btnRef_, &QPushButton::clicked, this, &LibraryFace::useAsReference);
  rl->addWidget(btnRef_);
  auto* row = new QHBoxLayout;
  auto* btnFile = new QPushButton(QStringLiteral("Open file"));
  connect(btnFile, &QPushButton::clicked, this, &LibraryFace::openFile);
  row->addWidget(btnFile);
  auto* btnDel = new QPushButton(QStringLiteral("Delete file"));
  btnDel->setStyleSheet("color:#ff8a70;");
  connect(btnDel, &QPushButton::clicked, this, &LibraryFace::deleteFile);
  row->addWidget(btnDel);
  rl->addLayout(row);
  split->addWidget(right);

  split->setStretchFactor(0, 0);
  split->setStretchFactor(1, 1);
  split->setStretchFactor(2, 0);
  split->setSizes({260, 640, 280});
  lay->addWidget(split, 1);

  // The encode area lives under the splitter; adoptEncodePage fills it.
  thumbTimer_ = new QTimer(this);
  thumbTimer_->setInterval(30);
  connect(thumbTimer_, &QTimer::timeout, this, &LibraryFace::loadMoreThumbs);
}

void LibraryFace::restyleFilters() {
  const QStringList names{QStringLiteral("all"), QStringLiteral("ok"), QStringLiteral("takes")};
  for (int i = 0; i < filterButtons_.size(); ++i)
    filterButtons_[i]->setStyleSheet(QStringLiteral(
        "QToolButton{border-radius:4px;padding:5px 14px;font-size:12px;%1}")
        .arg(names[i] == filter_
                 ? "background:#2f6f8f;color:#ffffff;font-weight:800;border:1px solid #7fd0f0;"
                 : "background:#232830;color:#93a3ad;font-weight:600;border:1px solid #39414c;"));
}

void LibraryFace::refresh() {
  const QString root =
      QString::fromStdString(bs::expand_user(win_->cfg().str("data_root", "~/birdshot-data")));
  QString keep;
  if (lstSessions_->currentItem())
    keep = lstSessions_->currentItem()->data(Qt::UserRole).toString();
  const QSignalBlocker block(lstSessions_);
  lstSessions_->clear();
  int selectRow = -1;
  const auto sessions = bs::list_sessions(root.toStdString());
  for (const auto& dir : sessions) {
    bs::Session s = bs::Session::open(dir);
    const auto index = s.read_index();
    QString id = QString::fromStdString(s.name());
    QString kind;
    if (id.startsWith(QStringLiteral("bird"))) kind = QStringLiteral("BIRD");
    else if (id.startsWith(QStringLiteral("tlc"))) kind = QStringLiteral("TIMELAPSE");
    else if (id.startsWith(QStringLiteral("rapid"))) kind = QStringLiteral("RAPID");
    auto* item = new QListWidgetItem(
        QStringLiteral("%1%2 — %3 frames")
            .arg(id, kind.isEmpty() ? QString() : QStringLiteral("   [%1]").arg(kind))
            .arg(index.size()));
    item->setData(Qt::UserRole, QString::fromStdString(dir));
    lstSessions_->addItem(item);
    if (QString::fromStdString(dir) == keep) selectRow = lstSessions_->count() - 1;
  }
  if (lstSessions_->count() > 0) {
    lstSessions_->setCurrentRow(selectRow >= 0 ? selectRow : 0);
    sessionPicked();
  } else {
    sessionPath_.clear();
    reloadGrid();
  }
}

void LibraryFace::sessionPicked() {
  auto* item = lstSessions_->currentItem();
  sessionPath_ = item ? item->data(Qt::UserRole).toString() : QString();
  reloadGrid();
  win_->refreshEncodeSources();  // point the encode source at what is being looked at
}

void LibraryFace::adoptEncodePage(QWidget* page, const QString& gateReason) {
  auto* area = new QScrollArea;
  area->setWidgetResizable(true);
  area->setFrameShape(QFrame::NoFrame);
  auto* holder = new QWidget;
  auto* hv = new QVBoxLayout(holder);
  hv->setContentsMargins(6, 0, 6, 4);
  auto* acc = new Accordion(QStringLiteral("Encode photos into a movie"), false);
  acc->addWidget(page);
  acc->setSummary(QStringLiteral("pick a session above, then expand"));
  if (!gateReason.isEmpty()) acc->setGated(gateReason);
  hv->addWidget(acc);
  area->setMaximumHeight(64);
  connect(acc, &Accordion::toggledOpen, this,
          [area](bool open) { area->setMaximumHeight(open ? 360 : 64); });
  area->setWidget(holder);
  static_cast<QVBoxLayout*>(layout())->addWidget(area);
}

QList<LibraryFace::Entry> LibraryFace::readSession(const QString& dir) const {
  // The index carries verdicts and stats keyed by centisecond name; the
  // walk finds where each frame actually landed (its shutter bucket).
  QMap<QString, QString> stemToPath;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(dir.toStdString(), ec);
       it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (ec) break;
    if (!it->is_regular_file(ec)) continue;
    const auto p = it->path();
    const auto ext = p.extension().string();
    if (ext != ".jpg" && ext != ".jpeg") continue;
    if (p.string().find("_rejected") != std::string::npos) continue;
    stemToPath[QString::fromStdString(p.stem().string())] = QString::fromStdString(p.string());
  }

  QList<Entry> out;
  bs::Session s = bs::Session::open(dir.toStdString());
  for (const auto& rec : s.read_index()) {
    Entry e;
    const QString stem = QString::fromStdString(rec.get("name").str_or(""));
    if (!stemToPath.contains(stem)) continue;
    e.file = stemToPath.take(stem);
    e.verdict = QString::fromStdString(rec.get("verdict").str_or("ok"));
    e.sharpness = rec.get("sharpness_norm").number(0.0);
    e.clipHi = rec.get("clip_hi").number(0.0);
    e.focusMeasured = rec.get("focus_measured").boolean(false);
    e.shutterUs = static_cast<qint64>(rec.get("exposure_us").number(0));
    e.gain = rec.get("gain").number(1.0);
    if (rec.contains("sighting")) {
      const bs::Json& b = rec.get("sighting");
      e.hasBird = true;
      e.birdSharp = b.get("sharpness").number(0.0);
      e.birdArea = b.get("area_frac").number(0.0);
      e.birdRing = b.get("ring_sky_frac").number(0.0);
      e.birdSky = b.get("sky_frac").number(0.0);
      e.birdMotion = b.get("motion_frac").number(0.0);
    }
    out << e;
  }
  // Frames on disk that the index does not mention (or no index at all).
  for (auto it = stemToPath.cbegin(); it != stemToPath.cend(); ++it) {
    Entry e;
    e.file = it.value();
    out << e;
  }
  return out;
}

void LibraryFace::reloadGrid() {
  thumbTimer_->stop();
  grid_->clear();
  entries_.clear();
  thumbNext_ = 0;
  if (sessionPath_.isEmpty()) return;

  QList<Entry> all = readSession(sessionPath_);
  for (const Entry& e : all) {
    if (filter_ == QStringLiteral("ok") && e.verdict != QStringLiteral("ok")) continue;
    if (filter_ == QStringLiteral("takes") && !e.hasBird) continue;
    entries_ << e;
  }
  int dropped = 0;
  if (entries_.size() > kThumbLimit) {
    // A rapid run can hold thousands; show the newest.
    dropped = entries_.size() - kThumbLimit;
    entries_ = entries_.mid(dropped);
  }
  if (dropped > 0) {
    auto* headItem =
        new QListWidgetItem(QStringLiteral("+%1 older\nnot shown").arg(dropped));
    headItem->setFlags(Qt::NoItemFlags);
    grid_->addItem(headItem);
  }
  for (int i = 0; i < entries_.size(); ++i) {
    const Entry& e = entries_[i];
    auto* item = new QListWidgetItem(
        e.hasBird ? QStringLiteral("TAKE %1").arg(e.birdSharp, 0, 'f', 1) : e.verdict);
    item->setForeground(e.hasBird ? QColor("#e0a828") : theme::verdictColor(e.verdict));
    item->setData(Qt::UserRole, i);
    // Uniform item sizes lock to the first hint, which would be text-only
    // before the lazy thumbnails land -- reserve the full cell up front.
    item->setSizeHint(QSize(174, 150));
    grid_->addItem(item);
  }
  if (!entries_.isEmpty()) thumbTimer_->start();  // thumbnails load in batches, to keep Qt live
}

void LibraryFace::loadMoreThumbs() {
  int loaded = 0;
  for (int row = 0; row < grid_->count() && loaded < kThumbBatch; ++row) {
    auto* item = grid_->item(row);
    if (!item->icon().isNull() || !(item->flags() & Qt::ItemIsEnabled)) continue;
    const int idx = item->data(Qt::UserRole).toInt();
    if (idx < thumbNext_) continue;
    QPixmap pm(entries_[idx].file);
    if (!pm.isNull()) item->setIcon(iconFrom(pm, 160, 120));
    thumbNext_ = idx + 1;
    ++loaded;
  }
  if (loaded == 0) thumbTimer_->stop();
}

void LibraryFace::framePicked() {
  auto* item = grid_->currentItem();
  if (!item || !(item->flags() & Qt::ItemIsEnabled)) return;
  const int idx = item->data(Qt::UserRole).toInt();
  if (idx < 0 || idx >= entries_.size()) return;
  const Entry& e = entries_[idx];

  QPixmap pm(e.file);
  if (!pm.isNull())
    lblBig_->setPixmap(pm.scaled(lblBig_->width() - 8, 260, Qt::KeepAspectRatio,
                                 Qt::SmoothTransformation));

  QStringList facts;
  const QFileInfo fi(e.file);
  facts << QStringLiteral("%1/%2").arg(QFileInfo(sessionPath_).fileName(), fi.fileName());
  if (e.shutterUs > 0)
    facts << QStringLiteral("%1 · gain %2")
                 .arg(QString::fromStdString(bs::describe_shutter(e.shutterUs)))
                 .arg(e.gain, 0, 'f', 2);
  if (e.focusMeasured || e.sharpness > 0)
    facts << QStringLiteral("sharpness %1 · verdict %2 · clip %3%")
                 .arg(e.sharpness, 0, 'f', 1)
                 .arg(e.verdict)
                 .arg(e.clipHi * 100, 0, 'f', 2);
  lblFacts_->setText(facts.join(QStringLiteral("\n")));

  lblTrigger_->setText(
      e.hasBird ? QStringLiteral("trigger that fired this take:\nsharp %1 · size %2% · ring %3% "
                                 "· sky %4% · motion %5%")
                      .arg(e.birdSharp, 0, 'f', 1)
                      .arg(e.birdArea * 100, 0, 'f', 2)
                      .arg(e.birdRing * 100, 0, 'f', 0)
                      .arg(e.birdSky * 100, 0, 'f', 0)
                      .arg(e.birdMotion * 100, 0, 'f', 2)
                : QString());

  btnRef_->setEnabled(e.focusMeasured);
  btnRef_->setToolTip(e.focusMeasured ? QString()
                                      : QStringLiteral("this frame carries no focus measurement"));
}

void LibraryFace::useAsReference() {
  auto* item = grid_->currentItem();
  if (!item) return;
  const int idx = item->data(Qt::UserRole).toInt();
  if (idx < 0 || idx >= entries_.size()) return;
  const double v = entries_[idx].sharpness;
  if (v <= 0) return;
  win_->cfg().set("sharpness_reference", bs::Json(v));
  win_->cfg().set("blur_threshold", bs::Json(std::round(v * 0.5 * 100) / 100.0));
  win_->cfg().save();
  win_->log(QStringLiteral("sharp reference %1 from library, blur gate now %2")
                .arg(v, 0, 'f', 1)
                .arg(v * 0.5, 0, 'f', 1));
}

void LibraryFace::openFile() {
  auto* item = grid_->currentItem();
  if (!item) return;
  const int idx = item->data(Qt::UserRole).toInt();
  if (idx >= 0 && idx < entries_.size()) win_->openPath(entries_[idx].file);
}

void LibraryFace::deleteFile() {
  auto* item = grid_->currentItem();
  if (!item) return;
  const int idx = item->data(Qt::UserRole).toInt();
  if (idx < 0 || idx >= entries_.size()) return;
  const QString path = entries_[idx].file;
  const auto answer = QMessageBox::question(
      this, QStringLiteral("Delete file"),
      QStringLiteral("Delete this frame from disk?\n\n%1\n\nThe index keeps its record.")
          .arg(path),
      QMessageBox::Yes | QMessageBox::No, QMessageBox::No);
  if (answer != QMessageBox::Yes) return;
  std::error_code ec;
  if (!fs::remove(path.toStdString(), ec) || ec) {
    win_->log(QStringLiteral("delete failed: %1").arg(QString::fromStdString(ec.message())));
    return;
  }
  // Keep the grid and the entry list in step (the prototype forgot to).
  entries_.removeAt(idx);
  delete grid_->takeItem(grid_->currentRow());
  for (int row = 0; row < grid_->count(); ++row) {
    auto* it = grid_->item(row);
    const int v = it->data(Qt::UserRole).toInt();
    if (v > idx) it->setData(Qt::UserRole, v - 1);
  }
  win_->log(QStringLiteral("deleted %1").arg(QFileInfo(path).fileName()));
}
