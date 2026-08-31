// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The capture engine: one loop that runs every mode over any backend.
//
//   COLLECT    full pipeline -- AE, quality gates, s<N> shutter folders,
//              index.jsonl with the complete FrameStats per frame
//   RAPID      metering + AE only, flat centisecond names, fastest path
//   TIMELAPSE  COLLECT pipeline at an interval
//   BIRDFLIGHT the detector watches; a take fires a COLLECT burst
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>

#include "birdshot/backend.hpp"
#include "birdshot/birdflight.hpp"
#include "birdshot/config.hpp"
#include "birdshot/storage.hpp"

namespace bs {

enum class Mode { Collect, Rapid, Timelapse, BirdFlight };

struct EngineOptions {
  Mode mode = Mode::Collect;
  int64_t count = 0;          // frames (or takes for BirdFlight); 0 = config / unlimited
  double interval_s = -1.0;   // timelapse spacing; <0 = config
  bool verbose = false;       // per-frame lines
};

struct EngineReport {
  std::string session_dir;
  int64_t frames = 0;
  int64_t saved = 0;
  int64_t takes = 0;              // Bird Flight only
  int64_t rejected = 0;           // gated frames not counted ok
  double seconds = 0.0;
  double fps = 0.0;
  bool clean = true;              // false when storage stopped the run
  std::string stop_reason;
};

class Engine {
 public:
  Engine(Config& cfg, Backend& backend);

  // Runs to completion (count frames, or until stop()). Blocking; call
  // stop() from another thread or a signal handler to end a free run.
  EngineReport run(const EngineOptions& opts);

  void stop() { stop_.store(true); }

  using LogFn = std::function<void(const std::string&)>;
  void set_log(LogFn fn) { log_ = std::move(fn); }

  // A front end's window into the loop: called from the engine thread with
  // every frame and its numbers, whether or not the frame was saved. The
  // callee copies what it needs and returns quickly.
  using FrameTap = std::function<void(const Frame&, const FrameStats&)>;
  void set_frame_tap(FrameTap fn) { tap_ = std::move(fn); }

  // Bird Flight only: every judged frame's sighting, with the running take
  // count and whether this frame fired one. Same thread rules as the tap.
  using SightingTap = std::function<void(const struct Sighting&, int64_t take_n, bool fired)>;
  void set_sighting_tap(SightingTap fn) { sighting_tap_ = std::move(fn); }

 private:
  void log(const std::string& line) const { if (log_) log_(line); }

  Config& cfg_;
  Backend& backend_;
  std::atomic<bool> stop_{false};
  LogFn log_;
  FrameTap tap_;
  SightingTap sighting_tap_;
};

}  // namespace bs
