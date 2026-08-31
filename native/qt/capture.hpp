// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// CaptureController: the one object between the Qt front end and the core.
// Idle, it runs the shared PreviewPump; recording, it runs the real Engine
// on a worker thread with the engine's frame tap feeding the same signal,
// so every face paints from one stream whether or not frames are being
// written. All signals are emitted from worker threads and must be taken
// over queued connections (the default across threads).
#pragma once

#include <atomic>
#include <memory>
#include <thread>

#include <QObject>
#include <QString>
#include <QStringList>

#include "birdshot/backend.hpp"
#include "birdshot/birdflight.hpp"
#include "birdshot/config.hpp"
#include "birdshot/engine.hpp"
#include "birdshot/gui.hpp"

// One frame, ready for the GUI thread: the luma plane (shared, immutable
// once emitted) and every number the faces read off it.
struct FramePacket {
  std::shared_ptr<const bs::Gray8> y;
  std::shared_ptr<const bs::Gray8> full;   // native luma when the backend delivers one
  std::shared_ptr<const bs::Rgb8> color;   // display colour, letterboxed to match y
  bs::FrameStats stats;
  qint64 exposureUs = 0;
  double gain = 1.0;
  double lux = 0.0;
  double target = 0.0;
  double fps = 0.0;
  bool settled = false;
  bool recording = false;
  qint64 seq = 0;  // engine frames judged so far (not throttled)
  QString aeMode = QStringLiteral("pid");
};
Q_DECLARE_METATYPE(FramePacket)
Q_DECLARE_METATYPE(bs::Sighting)

class CaptureController : public QObject {
  Q_OBJECT

 public:
  explicit CaptureController(bs::Config& cfg, QObject* parent = nullptr);
  ~CaptureController() override;

  bs::Config& config() { return cfg_; }
  QString backendName() const;
  QStringList capabilities() const;

  bool recording() const { return recording_.load(); }
  bs::Mode mode() const { return mode_; }

  // Tear the backend down and build it again from the config -- the same
  // gesture the prototype used when a settings change named a different
  // camera. Safe only while not recording. Also the honest "Reset AE
  // loop": the pump restarts with a fresh controller.
  void rebuildBackend();

  void startPreview();
  void stopPreview();

  // Launch the engine on a worker thread. count <= 0 means the config's
  // default (unlimited for most modes). Returns false while recording.
  bool startRecording(bs::Mode mode, qint64 count = 0, double intervalS = -1.0);
  void stopRecording();  // asks the engine to stop; finished() reports

 signals:
  void frameReady(FramePacket packet);
  void sightingReady(bs::Sighting sighting, qlonglong takeN, bool fired);
  void recordingStarted(QString modeName);
  void recordingFinished(QString summary, QString sessionDir, bool clean);
  void logLine(QString line);

 private:
  void deliver(const bs::Frame& frame, const bs::FrameStats& st, double target,
               const std::string& aeMode, bool settled, double fps);
  void joinEngineThread();

  bs::Config& cfg_;
  std::unique_ptr<bs::Backend> backend_;
  std::unique_ptr<bs::PreviewPump> pump_;
  std::unique_ptr<bs::Engine> engine_;
  std::thread engine_thread_;
  std::atomic<bool> recording_{false};
  bs::Mode mode_ = bs::Mode::Collect;

  // fps over the engine's tap, measured here (the pump measures its own).
  double tap_fps_ = 0.0;
  double tap_last_ = 0.0;
  double tap_emit_last_ = 0.0;
  double sight_emit_last_ = 0.0;
  qint64 tap_seq_ = 0;
};
