// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Auto-exposure: EV-space PID with highlight priority and a lux feed-forward.
// A behavioural port of the 1.x controller -- same gains, same deadband, same
// fast-acquire jump, same constrained-equilibrium "settled" rule -- because
// every one of those numbers was tuned against the real sky. See the 1.x
// module docstring for the full reasoning; it all still applies.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "birdshot/analysis.hpp"
#include "birdshot/config.hpp"

namespace bs {

// Beyond this error we stop trusting the PID and jump straight to the answer.
constexpr double kFastAcquireEv = 1.5;
constexpr double kFastAcquireClampEv = 4.0;
constexpr int kSettledFrames = 3;
// Below this correction the loop is holding station, even if the error itself
// cannot be closed because another constraint is pushing back.
constexpr double kStableOutputEv = 0.10;

struct ExposureDecision {
  int64_t exposure_us = 0;
  double gain = 1.0;
  double ev_error = 0.0;
  double ev_output = 0.0;
  double meter = 0.0;
  double target = 0.0;
  bool settled = false;
  std::string mode = "pid";  // pid | acquire | highlight | settled
  double p = 0.0, i = 0.0, d = 0.0;

  Json to_json() const;
};

struct SensorLimits {
  double exposure_min_us = kExposureMinUs;
  double exposure_max_us = kExposureMaxUs;
  double gain_min = kGainMin;
  double gain_max = kGainMax;
};

// Split a required exposure "energy" (us x gain) into shutter and gain along
// the ladder: base gain to the motion limit, then gain to its preferred cap,
// then shutter to its hard cap, then gain to the sensor maximum.
std::pair<int64_t, double> allocate(double energy, double motion_limit_us,
                                    double gain_preferred_max, double shutter_hard_max_us,
                                    double exposure_min_us, double exposure_max_us,
                                    double gain_min, double gain_max,
                                    bool prefer_exposure = false);

class ExposureController {
 public:
  explicit ExposureController(Config& cfg);

  void set_limits(const SensorLimits& limits) { limits_ = limits; }

  // Adopt what the sensor actually did as the new operating point (a request
  // was clamped; the integral accumulated against an unreachable target).
  void resync();

  void reset();

  // Metering target, biased by calibration when the wizard has run.
  double target_luma() const;

  // Given how the last frame came out, decide the next frame's exposure.
  ExposureDecision update(const FrameStats& stats, int64_t exposure_us, double gain,
                          double lux = 0.0, double now = -1.0);

  // Cold-start guess from a lux reading, if the constant has been learned.
  std::optional<std::pair<int64_t, double>> seed(double lux);

  // Checkpoint the learned feed-forward constant at session end.
  void persist();

 private:
  std::pair<int64_t, double> allocate_cfg(double energy) const;
  void learn(double lux, double energy, bool settled);

  Config& cfg_;
  SensorLimits limits_;
  double integral_ = 0.0;
  double prev_error_ = 0.0;
  double prev_time_ = -1.0;
  int settled_count_ = 0;
  int stable_count_ = 0;
  std::vector<double> window_;
  double k_lux_ = 0.0;  // 0 = not learned
  int k_samples_ = 0;
};

}  // namespace bs
