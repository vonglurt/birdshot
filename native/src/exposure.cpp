// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/exposure.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>

#include "birdshot/config.hpp"
#include "birdshot/mathkit.hpp"

namespace bs {

namespace {

constexpr double kEps = 1e-6;

double round3(double v) { return std::round(v * 1000.0) / 1000.0; }

double monotonic_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

}  // namespace

Json ExposureDecision::to_json() const {
  Json d = Json::object();
  d["exposure_us"] = static_cast<double>(exposure_us);
  d["gain"] = round3(gain);
  d["ev_error"] = round3(ev_error);
  d["ev_output"] = round3(ev_output);
  d["meter"] = std::round(meter * 100.0) / 100.0;
  d["target"] = std::round(target * 100.0) / 100.0;
  d["settled"] = settled;
  d["mode"] = mode;
  return d;
}

std::pair<int64_t, double> allocate(double energy, double motion_limit_us,
                                    double gain_preferred_max, double shutter_hard_max_us,
                                    double exposure_min_us, double exposure_max_us,
                                    double gain_min, double gain_max, bool prefer_exposure) {
  motion_limit_us = std::max(1.0, motion_limit_us);
  shutter_hard_max_us = std::max(motion_limit_us, shutter_hard_max_us);
  gain_min = std::max(1e-3, gain_min);
  gain_preferred_max = std::max(gain_min, gain_preferred_max);
  gain_max = std::max(gain_preferred_max, gain_max);
  energy = std::max(energy, exposure_min_us * gain_min);
  const double e1 = motion_limit_us * gain_min;
  const double e2 = motion_limit_us * gain_preferred_max;
  const double e3 = shutter_hard_max_us * gain_preferred_max;

  double t, g;
  if (prefer_exposure) {
    // Exposure-priority: spend duration first and treat gain as the last
    // resort. Gain buys brightness at the cost of noise it can never give
    // back; a longer exposure is free until motion smears.
    if (energy <= shutter_hard_max_us * gain_min) {
      t = energy / gain_min;
      g = gain_min;
    } else {
      t = shutter_hard_max_us;
      g = energy / shutter_hard_max_us;
    }
  } else if (energy <= e1) {
    t = energy / gain_min;
    g = gain_min;
  } else if (energy <= e2) {
    t = motion_limit_us;
    g = energy / motion_limit_us;
  } else if (energy <= e3) {
    t = energy / gain_preferred_max;
    g = gain_preferred_max;
  } else {
    t = shutter_hard_max_us;
    g = energy / shutter_hard_max_us;
  }

  const int64_t us = static_cast<int64_t>(
      std::llround(clamp(t, exposure_min_us, exposure_max_us)));
  return {us, clamp(g, gain_min, gain_max)};
}

ExposureController::ExposureController(Config& cfg) : cfg_(cfg) { reset(); }

void ExposureController::resync() {
  integral_ = 0.0;
  prev_error_ = 0.0;
  settled_count_ = 0;
  stable_count_ = 0;
  window_.clear();
}

void ExposureController::reset() {
  integral_ = 0.0;
  prev_error_ = 0.0;
  stable_count_ = 0;
  settled_count_ = 0;
  prev_time_ = -1.0;
  window_.clear();
  // Learned feed-forward constant: energy * lux ~= K for a fixed lens.
  const Json k = cfg_.state("k_lux");
  k_lux_ = k.is_number() ? k.number() : 0.0;
  k_samples_ = 0;
}

double ExposureController::target_luma() const {
  const double base = cfg_.num("target_luma", 118.0);
  const Json cal = cfg_.get("calibration");
  const Json dr = cal.get("dynamic_range_ev");
  if (!cal.get("done").boolean() || !dr.is_number() || dr.number() <= 0.0) return base;
  // With sky and treeline both measured we know the scene's dynamic range;
  // pull the target down by up to 25% as the range widens past 4 EV.
  const double excess = clamp(dr.number() - 4.0, 0.0, 4.0);
  return base * (1.0 - 0.0625 * excess);
}

void ExposureController::learn(double lux, double energy, bool settled) {
  if (!settled || lux <= kEps) return;
  const double k = energy * lux;
  k_lux_ = k_lux_ <= 0.0 ? k : 0.9 * k_lux_ + 0.1 * k;
  if (++k_samples_ % 20 == 0) cfg_.set_state("k_lux", Json(k_lux_));
}

void ExposureController::persist() {
  if (k_lux_ > 0.0) cfg_.set_state("k_lux", Json(k_lux_));
}

std::optional<std::pair<int64_t, double>> ExposureController::seed(double lux) {
  if (lux <= kEps || k_lux_ <= 0.0) return std::nullopt;
  return allocate_cfg(k_lux_ / lux);
}

std::pair<int64_t, double> ExposureController::allocate_cfg(double energy) const {
  double e_lo = limits_.exposure_min_us, e_hi = limits_.exposure_max_us;
  double g_lo = limits_.gain_min, g_hi = limits_.gain_max;
  // Belt and braces: a bad or unknown range must never collapse the ladder.
  if (!(e_hi > e_lo && e_lo > 0)) {
    e_lo = kExposureMinUs;
    e_hi = kExposureMaxUs;
  }
  if (!(g_hi > g_lo && g_lo > 0)) {
    g_lo = kGainMin;
    g_hi = kGainMax;
  }
  const double hard_max = std::max(1.0, std::min(cfg_.num("shutter_hard_max_us", 33000), e_hi));
  return allocate(energy,
                  std::max(1.0, std::min(cfg_.num("motion_limit_us", 2000), hard_max)),
                  std::min(cfg_.num("gain_preferred_max", 4.0), g_hi), hard_max,
                  std::max(kExposureMinUs, e_lo), std::min(kExposureMaxUs, e_hi),
                  std::max(kGainMin, g_lo), std::min(kGainMax, g_hi),
                  cfg_.boolean("prefer_exposure_time", true));
}

ExposureDecision ExposureController::update(const FrameStats& stats, int64_t exposure_us,
                                            double gain, double lux, double now) {
  if (now < 0.0) now = monotonic_now();
  const double dt = prev_time_ < 0.0 ? 0.25 : clamp(now - prev_time_, 1e-3, 2.0);
  prev_time_ = now;

  const double target = target_luma();

  // Average the last N readings; use the raw reading when the scene has
  // genuinely changed so big moves stay instant while small ones stop
  // chasing noise. (See the 1.x module for the measured comparison.)
  const int n = std::max(1, static_cast<int>(cfg_.num("ae_average_n", 3)));
  const std::string avg_mode = cfg_.str("ae_average_mode", "median");
  const double raw = std::max(stats.meter, 1.0);
  window_.push_back(raw);
  while (static_cast<int>(window_.size()) > n) window_.erase(window_.begin());

  double meter;
  if (std::fabs(std::log2(std::max(target, 1.0) / raw)) > kFastAcquireEv) {
    meter = raw;              // the light really moved
    window_.assign(1, raw);   // do not average across the step
  } else if (avg_mode == "mean") {
    double s = 0.0;
    for (double v : window_) s += v;
    meter = s / static_cast<double>(window_.size());
  } else if (avg_mode == "median") {
    meter = median(window_);
  } else {
    meter = raw;
  }
  meter = std::max(meter, 1.0);

  // Brightness error in EV. Positive => the frame needs more light.
  double err = std::log2(std::max(target, 1.0) / meter);
  std::string mode = "pid";

  // Highlight priority: clipping can only ever demand LESS exposure, and it
  // overrides the brightness demand when it fires. Measured on the subject
  // zone; the sky has its own far looser budget and only ever trims.
  const double max_clip = std::max(cfg_.num("max_clip_frac", 0.020), 1e-4);
  const double sky_tol = std::max(cfg_.num("sky_clip_tolerance", 0.60), 1e-4);

  double clip_err = 0.0;
  if (stats.subject_clip_hi > max_clip) {
    const double overage = (stats.subject_clip_hi - max_clip) / max_clip;
    clip_err = -std::min(3.0, 0.5 * std::log2(1.0 + overage));
  }
  if (stats.sky_clip_hi > sky_tol) {
    const double overage = (stats.sky_clip_hi - sky_tol) / sky_tol;
    clip_err = std::min(clip_err, -std::min(0.75, 0.35 * std::log2(1.0 + overage)));
  }
  if (clip_err < 0.0 && clip_err < err) {
    err = clip_err;
    mode = "highlight";
  }

  // Deadband: stop hunting once we are close enough.
  const double deadband = cfg_.num("pid_deadband_ev", 0.20);
  if (std::fabs(err) < deadband && mode != "highlight") {
    ++settled_count_;
    integral_ *= 0.85;  // bleed off so it cannot creep
    prev_error_ = err;
    learn(lux, static_cast<double>(exposure_us) * gain, true);
    ExposureDecision d;
    d.exposure_us = exposure_us;
    d.gain = gain;
    d.ev_error = err;
    d.ev_output = 0.0;
    d.meter = meter;
    d.target = target;
    d.settled = settled_count_ >= kSettledFrames;
    d.mode = "settled";
    return d;
  }
  settled_count_ = 0;

  // Fast acquire: a large error means the scene changed, not that the loop
  // is mistuned. Jump rather than integrate.
  if (std::fabs(err) > kFastAcquireEv) {
    const double out = clamp(err, -kFastAcquireClampEv, kFastAcquireClampEv);
    integral_ = 0.0;
    prev_error_ = err;
    const double energy = static_cast<double>(exposure_us) * gain * std::exp2(out);
    const auto [new_us, new_gain] = allocate_cfg(energy);
    ExposureDecision d;
    d.exposure_us = new_us;
    d.gain = new_gain;
    d.ev_error = err;
    d.ev_output = out;
    d.meter = meter;
    d.target = target;
    d.mode = "acquire";
    return d;
  }

  // ---- PID -------------------------------------------------------------
  const double kp = cfg_.num("pid_kp", 0.55);
  const double ki = cfg_.num("pid_ki", 0.10);
  const double kd = cfg_.num("pid_kd", 0.12);
  const double iclamp = cfg_.num("pid_integral_clamp_ev", 2.0);
  const double slew = cfg_.num("pid_slew_ev", 1.5);

  const double p_term = kp * err;
  integral_ = clamp(integral_ + err * dt, -iclamp, iclamp);
  const double i_term = ki * integral_;
  const double d_term = kd * (err - prev_error_) / dt;
  prev_error_ = err;

  double out = p_term + i_term + d_term;
  // Move only part of the way toward the correction: with a two-frame
  // control latency the full step overshoots -- that is the dance.
  out *= clamp(cfg_.num("ae_damping", 0.5), 0.05, 1.0);

  // Anti-windup: if the slew limiter is saturating, unwind the integral
  // rather than letting it accumulate against a limit it cannot beat.
  if (std::fabs(out) > slew) {
    integral_ -= (out - std::copysign(slew, out)) / std::max(ki, kEps) * 0.5;
    integral_ = clamp(integral_, -iclamp, iclamp);
    out = std::copysign(slew, out);
  }

  const double energy = static_cast<double>(exposure_us) * gain * std::exp2(out);
  const auto [new_us, new_gain] = allocate_cfg(energy);

  // A constrained equilibrium counts as settled: when highlight priority and
  // the brightness term pull against each other the error never enters the
  // deadband, but the correction has gone to nothing -- which is exactly the
  // scene this camera is pointed at, and is when the lux constant must learn.
  if (std::fabs(out) < kStableOutputEv) ++stable_count_;
  else stable_count_ = 0;
  const bool converged = stable_count_ >= kSettledFrames;
  learn(lux, static_cast<double>(exposure_us) * gain, converged);

  ExposureDecision d;
  d.exposure_us = new_us;
  d.gain = new_gain;
  d.ev_error = err;
  d.ev_output = out;
  d.meter = meter;
  d.target = target;
  d.settled = converged;
  d.mode = mode;
  d.p = p_term;
  d.i = i_term;
  d.d = d_term;
  return d;
}

}  // namespace bs
