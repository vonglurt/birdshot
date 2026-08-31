// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The native line's front ends and what they share.
//
// PreviewPump is the idle capture loop -- backend + AE, no storage --
// delivering every frame to a sink. Two consumers: the Viewfinder (the
// `birdshot gui` loopback HTTP server, your browser as the display) and
// the Qt Widgets front end under native/qt/. Recording is not done here:
// a front end that records runs the Engine, whose frame tap plays the
// same role as the sink.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace bs {

class Backend;
class Config;
struct ExposureDecision;
struct Frame;
struct FrameStats;

// The engine's loop minus storage: capture, analyse, let AE steer (fed
// virtual frame-cadence time, exactly as the engine feeds it), hand the
// frame over, sleep to the gui_preview_fps cadence. The sink runs on the
// pump's thread; it copies what it needs and returns quickly.
class PreviewPump {
 public:
  using Sink =
      std::function<void(const Frame&, const FrameStats&, const ExposureDecision&, double fps)>;

  PreviewPump(Config& cfg, Backend& backend);
  ~PreviewPump();

  PreviewPump(const PreviewPump&) = delete;
  PreviewPump& operator=(const PreviewPump&) = delete;

  void start(Sink sink);
  void stop();
  bool running() const { return !stopped_.load(); }

 private:
  void loop();

  Config& cfg_;
  Backend& backend_;
  Sink sink_;
  std::atomic<bool> stopped_{true};
  std::thread thread_;
};

struct GuiOptions {
  int port = 0;              // 0 = the gui_port config key, default 8477
  bool open_browser = true;  // launch the platform's opener on start
};

// The loopback HTTP server behind `birdshot gui`. Split from run_gui so
// the selftest can start one on an ephemeral port, fetch from it over
// loopback, and stop it -- no browser involved.
class Viewfinder {
 public:
  explicit Viewfinder(Config& cfg);
  ~Viewfinder();

  Viewfinder(const Viewfinder&) = delete;
  Viewfinder& operator=(const Viewfinder&) = delete;

  // Bind 127.0.0.1:port (0 = ephemeral), start the pump and the accept
  // thread. False, with a message, when the bind fails.
  bool start(int port, std::string* err);
  void stop();

  int port() const { return port_; }

 private:
  void accept_loop();
  void serve(intptr_t client);
  void on_frame(const Frame& frame, const FrameStats& st, const ExposureDecision& dec,
                double fps);

  Config& cfg_;
  int port_ = 0;
  intptr_t listen_fd_ = -1;
  std::atomic<bool> stopping_{false};
  std::atomic<int> clients_{0};  // stop() waits these out; they are detached

  std::unique_ptr<Backend> backend_;
  std::unique_ptr<PreviewPump> pump_;

  // The latest frame and its numbers, swapped in whole under the mutex.
  // MJPEG streamers sleep on the condition variable keyed by generation.
  std::mutex mu_;
  std::condition_variable frame_cv_;
  std::vector<uint8_t> jpeg_;
  std::string status_json_;
  uint64_t generation_ = 0;

  std::thread accept_thread_;
};

// `birdshot gui`: serve the viewfinder until Ctrl-C. Returns an exit code.
int run_gui(Config& cfg, const GuiOptions& opts);

}  // namespace bs
