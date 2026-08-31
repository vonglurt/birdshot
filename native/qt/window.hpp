// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The shell: one window, four faces, the Bench settings rail, the status
// bar, and the wiring between the capture controller and everything that
// paints. A behavioral port of the prototype's main_window.py onto the
// native core -- same layout, same keys, same gating rules; sections whose
// core has not been ported yet (video, cascade, EXIF) are greyed with the
// reason, exactly how the prototype gated what a camera could not do.
#pragma once

#include <functional>

#include <QCheckBox>
#include <QComboBox>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QMap>
#include <QScrollArea>
#include <QSet>
#include <QStackedWidget>
#include <QTabWidget>
#include <QTextEdit>
#include <QTimer>

#include "capture.hpp"
#include "faces.hpp"
#include "preview.hpp"
#include "widgets.hpp"

struct ModeSpec {
  QString label;
  QString key;   // capability name: stills | rapid | timelapse | video | birdflight
  QString hint;
  bs::Mode mode;
};

class MainWindow : public QMainWindow {
  Q_OBJECT

 public:
  MainWindow(bs::Config& cfg, const QString& face);
  ~MainWindow() override;

  // ------- the face <-> shell contract (prototype §7) -------
  bs::Config& cfg() { return cfg_; }
  CaptureController* capture() { return capture_; }
  const QList<ModeSpec>& modes() const { return modes_; }
  QSet<QString> caps() const;
  ModeTuner* tuner() { return tuner_; }
  QCheckBox* chkOutdoor() { return chkOutdoor_; }
  QComboBox* cmbOutdoor() { return cmbOutdoor_; }

  void setFace(const QString& name);
  bool selectTab(const QString& name);
  void goClicked();
  void log(const QString& msg);
  void openPath(const QString& path);
  QString stateName() const;  // idle | burst | rapid | timelapse | birdflight
  double lastSharpness() const { return lastSharpness_; }

 protected:
  void resizeEvent(QResizeEvent* e) override;
  void closeEvent(QCloseEvent* e) override;

 private:
  // construction
  QWidget* buildBench();
  QWidget* buildModeHeader();
  QWidget* buildReadout();
  QWidget* buildViewRow();
  QWidget* tabShoot();
  QWidget* tabScene();
  QWidget* tabMachine();
  Accordion* section(const QString& title, QWidget* page, bool expanded, int tab);
  QWidget* wrapTab(const QList<QWidget*>& sections);

  // binding helpers (the settings registry)
  struct Bind {
    QString key;
    QWidget* widget = nullptr;
    std::function<void()> refresh;
    QWidget* labelWidget = nullptr;
    QString label;
    int tab = 0;
    Accordion* sectionAcc = nullptr;
    bool wasChanged = false;
  };
  QWidget* spinInt(const QString& key, int lo, int hi, int step, const QString& suffix = {},
                   std::function<void(double)> onChange = {});
  QWidget* spinDouble(const QString& key, double lo, double hi, double step, int decimals,
                      const QString& suffix = {}, std::function<void(double)> onChange = {});
  QCheckBox* check(const QString& key, const QString& text,
                   std::function<void(bool)> onChange = {});
  QComboBox* comboStr(const QString& key, const QStringList& options,
                      std::function<void(QString)> onChange = {});
  QLineEdit* line(const QString& key);
  void registerBind(const QString& key, QWidget* w, std::function<void()> refresh);
  void saveCfg();

  // behavior
  void modeChanged(int idx);
  void refreshGoButton();
  void applyCapabilities();
  void stepMode(int delta);
  void toggleFullscreen();
  void setAllOverlays(bool on);
  void outdoorChanged(bool on);
  void outdoorStyleChanged(int idx);
  void refreshStatusTick();
  void refreshSummaries();
  void refreshProvenance();
  void indexSettings();
  void searchPicked(const QString& text);
  void openResetDialog();
  void runDoctor();
  void populateCameras();
  void switchCamera(int idx);
  void refreshProfiles(const QString& select = {});
  void profileActivated(int idx);
  void profileSave(bool asNew);
  void profileDelete();
  void afterSettingsSwap();
  void setSharpnessReference();
  void dismissOverlay();

  // capture wiring
  void onFrame(const FramePacket& p);
  void onSighting(const bs::Sighting& s, qlonglong takeN, bool fired);
  void onRecordingStarted(const QString& modeName);
  void onRecordingFinished(const QString& summary, const QString& sessionDir, bool clean);

  bs::Config& cfg_;
  CaptureController* capture_;
  QList<ModeSpec> modes_;

  // shell
  FaceBar* facebar_;
  QStackedWidget* stack_;
  PreviewWidget* preview_;
  HistogramWidget* histogram_;
  QVBoxLayout* benchPreviewLayout_;
  CameraFace* faceCamera_;
  FieldFace* faceField_;
  LibraryFace* faceLibrary_;
  BlockingOverlay* overlay_;
  FullscreenPreview* fullscreen_ = nullptr;
  PreviewWidget* fullscreenPreview_ = nullptr;
  QString currentFace_ = QStringLiteral("bench");
  struct OverlayStash {
    bool valid = false;
    bool hud, zones, zebra, peaking, fmap, sharp;
  } stash_;

  // bench widgets
  ModeTuner* tuner_;
  QLabel* lblModeHint_;
  QPushButton* btnGo_;
  QLineEdit* edSearch_;
  QComboBox* cmbCameraRail_;
  QList<bs::CameraInfo> cameras_;
  QComboBox* cmbProfile_;
  QLabel* lblBanner_;
  QLabel* lblLine_;
  QLabel* lblVerdictRead_;
  QLabel* lblSessionRead_;
  QCheckBox* chkOutdoor_;
  QComboBox* cmbOutdoor_;
  QTabWidget* tabs_;
  QList<QScrollArea*> tabScrolls_;
  QMap<QString, Accordion*> sections_;
  QList<Bind> binds_;
  QLabel* lblChanged_;
  QTextEdit* logView_;
  QLabel* lblCounts_;
  QLabel* lblBird_;
  QLabel* lblFocusLive_;
  QLabel* lblSharpRef_;
  QCheckBox *chkFmap_, *chkSharpNum_, *chkPeak2_, *chkZebra2_;
  QTextEdit* txtDoctor_;
  QLabel* lblDoctorStamp_;
  QPushButton* btnDoctorChip_;
  QLabel* lblStateBar_;
  QLabel* lblFreeBar_;

  // live state
  qint64 sessionFrames_ = 0;
  double sessionBytes_ = 0.0;
  QMap<QString, int> counts_;
  double lastSharpness_ = 0.0;
  double lastFrameAt_ = 0.0;
  FramePacket last_;
  QTimer* tick_;
  bool doctorRunning_ = false;
  bool spaceFull_ = false;
};
