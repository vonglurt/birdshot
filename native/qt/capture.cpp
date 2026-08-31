// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "capture.hpp"

#include <chrono>

#include "birdshot/analysis.hpp"
#include "birdshot/exposure.hpp"

namespace {

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

const char* mode_name(bs::Mode m) {
  switch (m) {
    case bs::Mode::Rapid: return "RAPID";
    case bs::Mode::Timelapse: return "TIMELAPSE";
    case bs::Mode::BirdFlight: return "BIRD FLIGHT";
    default: return "COLLECT";
  }
}

}  // namespace

CaptureController::CaptureController(bs::Config& cfg, QObject* parent)
    : QObject(parent), cfg_(cfg) {
  qRegisterMetaType<FramePacket>("FramePacket");
  qRegisterMetaType<bs::Sighting>("bs::Sighting");
  backend_ = bs::make_backend(cfg_);
  pump_.reset(new bs::PreviewPump(cfg_, *backend_));
}

CaptureController::~CaptureController() {
  stopRecording();
  joinEngineThread();
  if (pump_) pump_->stop();
}

QString CaptureController::backendName() const {
  return QString::fromStdString(backend_->name());
}

QStringList CaptureController::capabilities() const {
  QStringList out;
  for (const auto& c : backend_->capabilities()) out << QString::fromStdString(c);
  return out;
}

void CaptureController::rebuildBackend() {
  if (recording_.load()) return;
  const bool was_previewing = pump_ && pump_->running();
  if (pump_) pump_->stop();
  pump_.reset();
  backend_ = bs::make_backend(cfg_);
  pump_.reset(new bs::PreviewPump(cfg_, *backend_));
  if (was_previewing) startPreview();
}

void CaptureController::startPreview() {
  if (recording_.load() || !pump_ || pump_->running()) return;
  pump_->start([this](const bs::Frame& f, const bs::FrameStats& st,
                      const bs::ExposureDecision& d, double fps) {
    deliver(f, st, d.target, d.mode, d.settled, fps);
  });
}

void CaptureController::stopPreview() {
  if (pump_) pump_->stop();
}

bool CaptureController::startRecording(bs::Mode mode, qint64 count, double intervalS) {
  if (recording_.exchange(true)) return false;
  joinEngineThread();
  stopPreview();
  mode_ = mode;
  tap_fps_ = 0.0;
  tap_last_ = 0.0;
  tap_seq_ = 0;

  engine_.reset(new bs::Engine(cfg_, *backend_));
  engine_->set_log([this](const std::string& line) {
    emit logLine(QString::fromStdString(line));
  });
  engine_->set_frame_tap([this](const bs::Frame& f, const bs::FrameStats& st) {
    ++tap_seq_;
    const double now = mono_now();
    if (tap_last_ > 0.0) {
      const double dt = now - tap_last_;
      if (dt > 0) tap_fps_ = tap_fps_ == 0.0 ? 1.0 / dt : 0.9 * tap_fps_ + 0.1 / dt;
    }
    tap_last_ = now;
    // The engine can run at hundreds of fps; the GUI paints at preview
    // rate. Emitting every tapped frame would flood the Qt event queue,
    // so the tap drops frames the screen would never have shown anyway.
    if (now - tap_emit_last_ >= 1.0 / 15.0) {
      tap_emit_last_ = now;
      deliver(f, st, 0.0, "engine", false, tap_fps_);
    }
  });
  engine_->set_sighting_tap([this](const bs::Sighting& s, int64_t takeN, bool fired) {
    const double now = mono_now();
    if (!fired && now - sight_emit_last_ < 0.1) return;  // fired takes always land
    sight_emit_last_ = now;
    emit sightingReady(s, static_cast<qlonglong>(takeN), fired);
  });

  bs::EngineOptions opts;
  opts.mode = mode;
  opts.count = count > 0 ? count : 0;
  opts.interval_s = intervalS;

  emit recordingStarted(QString::fromLatin1(mode_name(mode)));
  engine_thread_ = std::thread([this, opts] {
    const bs::EngineReport rep = engine_->run(opts);
    recording_.store(false);
    QString summary = QStringLiteral("%1 frames, %2 saved, %3 fps")
                          .arg(rep.frames)
                          .arg(rep.saved)
                          .arg(rep.fps, 0, 'f', 1);
    if (rep.takes > 0) summary += QStringLiteral(", %1 takes").arg(rep.takes);
    if (!rep.clean) summary += QStringLiteral(" -- %1").arg(QString::fromStdString(rep.stop_reason));
    emit recordingFinished(summary, QString::fromStdString(rep.session_dir), rep.clean);
  });
  return true;
}

void CaptureController::stopRecording() {
  if (engine_ && recording_.load()) engine_->stop();
}

void CaptureController::joinEngineThread() {
  if (engine_thread_.joinable()) engine_thread_.join();
}

void CaptureController::deliver(const bs::Frame& frame, const bs::FrameStats& st, double target,
                                const std::string& aeMode, bool settled, double fps) {
  FramePacket p;
  p.y = std::make_shared<bs::Gray8>(frame.y);
  p.stats = st;
  p.exposureUs = frame.exposure_us;
  p.gain = frame.gain;
  p.lux = frame.lux;
  p.target = target;
  p.fps = fps;
  p.settled = settled;
  p.recording = recording_.load();
  p.seq = tap_seq_;
  p.aeMode = QString::fromStdString(aeMode);
  emit frameReady(p);
}
