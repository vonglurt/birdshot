// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The replay backend: a folder of stills played through the real pipeline
// -- analysis, AE bookkeeping, gates, Bird Flight -- exactly as the 1.x
// replay backend did. This is how real-bird footage tunes the detector on
// a desk. Frames come from the in-tree JPEG decoder, so any baseline JPEG
// plays: birdshot's own sessions or a camera's card.
#include "backend_impl.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <thread>

#include "birdshot/config.hpp"
#include "birdshot/jpeg.hpp"

namespace fs = std::filesystem;

namespace bs {

namespace {

class ReplayBackend : public Backend {
 public:
  ReplayBackend(std::string dir, std::vector<std::string> files)
      : dir_(std::move(dir)), files_(std::move(files)) {}

  std::string name() const override {
    return "replay " + fs::path(dir_).filename().string() + " (" +
           std::to_string(files_.size()) + " frames)";
  }

  std::vector<std::string> capabilities() const override {
    // Footage has whatever exposure it was shot with; the pipeline can
    // only judge, not steer.
    return {"stills", "timelapse", "birdflight"};
  }

  SensorLimits limits() const override { return {}; }

  Frame capture(int64_t exposure_us, double gain) override {
    using namespace std::chrono;
    Frame frame;
    frame.exposure_us = exposure_us;
    frame.gain = gain;
    frame.ts = duration<double>(system_clock::now().time_since_epoch()).count();

    // Paced so a watch is watchable; the engine judging faster than the
    // footage was shot teaches nothing about the gates.
    std::this_thread::sleep_for(milliseconds(40));

    Rgb8 color;
    for (size_t tries = 0; tries < files_.size(); ++tries) {
      const std::string& path = files_[next_++ % files_.size()];
      if (read_jpeg(path, &color) && !color.empty()) break;
      if (!warned_) {
        std::fprintf(stderr, "replay: %s did not decode (baseline JPEG only)\n", path.c_str());
        warned_ = true;
      }
      color = Rgb8();
    }
    if (color.empty()) {
      frame.y = Gray8(640, 480, 0);
      return frame;
    }
    Gray8 native = to_luma(color);
    frame.y = (native.w == 640 && native.h == 480) ? native : letterbox(native, 640, 480);
    if (native.w != 640 || native.h != 480) frame.full = std::move(native);
    frame.color = std::move(color);
    return frame;
  }

 private:
  std::string dir_;
  std::vector<std::string> files_;
  size_t next_ = 0;
  bool warned_ = false;
};

}  // namespace

std::unique_ptr<Backend> make_replay_backend(const Config& cfg, std::string* err) {
  const std::string dir = expand_user(cfg.str("replay_path", ""));
  if (dir.empty()) {
    if (err) *err = "no replay folder configured (replay_path)";
    return nullptr;
  }
  std::vector<std::string> files;
  std::error_code ec;
  for (auto it = fs::recursive_directory_iterator(dir, ec);
       !ec && it != fs::recursive_directory_iterator(); it.increment(ec)) {
    if (!it->is_regular_file(ec)) continue;
    const auto ext = it->path().extension().string();
    if (ext != ".jpg" && ext != ".jpeg") continue;
    if (it->path().string().find("_rejected") != std::string::npos) continue;
    files.push_back(it->path().string());
  }
  if (files.empty()) {
    if (err) *err = "no .jpg frames under " + dir;
    return nullptr;
  }
  std::sort(files.begin(), files.end());
  return std::make_unique<ReplayBackend>(dir, std::move(files));
}

}  // namespace bs
