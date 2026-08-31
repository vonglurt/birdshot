// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The native line's first face: a viewfinder served over loopback HTTP.
// The binary runs the live pipeline -- capture, metering, AE, gates -- and
// your browser is the display, which keeps the GUI inside the tree's rules:
// C++17 and the standard library, no toolkit, one static binary on every
// platform. The four-face desktop GUI remains 1.x territory until the
// native one grows past a viewfinder (see native/README.md).
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace bs {

class Config;

struct GuiOptions {
  int port = 0;              // 0 = the gui_port config key, default 8477
  bool open_browser = true;  // launch the platform's opener on start
};

// The capture loop and the HTTP server behind `birdshot gui`. Split from
// run_gui so the selftest can start one on an ephemeral port, fetch from
// it over loopback, and stop it -- no browser involved.
class Viewfinder {
 public:
  explicit Viewfinder(Config& cfg);
  ~Viewfinder();

  Viewfinder(const Viewfinder&) = delete;
  Viewfinder& operator=(const Viewfinder&) = delete;

  // Bind 127.0.0.1:port (0 = ephemeral), start the capture and accept
  // threads. False, with a message, when the bind fails.
  bool start(int port, std::string* err);
  void stop();

  int port() const { return port_; }

 private:
  void capture_loop();
  void accept_loop();
  void serve(intptr_t client);

  Config& cfg_;
  int port_ = 0;
  intptr_t listen_fd_ = -1;
  std::atomic<bool> stopping_{false};
  std::atomic<int> clients_{0};  // stop() waits these out; they are detached

  // The latest frame and its numbers, swapped in whole under the mutex.
  // MJPEG streamers sleep on the condition variable keyed by generation.
  std::mutex mu_;
  std::condition_variable frame_cv_;
  std::vector<uint8_t> jpeg_;
  std::string status_json_;
  uint64_t generation_ = 0;

  std::thread capture_thread_;
  std::thread accept_thread_;
};

// `birdshot gui`: serve the viewfinder until Ctrl-C. Returns an exit code.
int run_gui(Config& cfg, const GuiOptions& opts);

}  // namespace bs
