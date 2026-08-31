// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The synthetic scene: sky over treeline with a bird crossing, responding to
// exposure like a sensor would. It exists so every pipeline stage -- AE,
// gates, Bird Flight, storage -- runs and tests identically on a machine
// with no camera at all. When the config carries a site, the scene's light
// follows the real solar elevation for that spot, which is what lets the
// planner and the capture path be exercised together.
#include <chrono>
#include <cmath>
#include <cstdio>

#include "backend_impl.hpp"
#include "birdshot/config.hpp"
#include "birdshot/mathkit.hpp"
#include "birdshot/solar.hpp"

namespace bs {

namespace {

double now_unix() {
  using namespace std::chrono;
  return duration<double>(system_clock::now().time_since_epoch()).count();
}

// Deterministic per-pixel hash noise; no global RNG state to contend over.
inline uint32_t hash32(uint32_t x) {
  x ^= x >> 16;
  x *= 0x7feb352du;
  x ^= x >> 15;
  x *= 0x846ca68bu;
  x ^= x >> 16;
  return x;
}

class SyntheticBackend final : public Backend {
 public:
  explicit SyntheticBackend(const Config& cfg)
      : w_(static_cast<int>(cfg.num("capture_width", 640))),
        h_(static_cast<int>(cfg.num("capture_height", 480))),
        site_(cfg.site()),
        site_set_(cfg.boolean("site_set", false)),
        t0_(now_unix()) {
    if (w_ < 64) w_ = 64;
    if (h_ < 48) h_ = 48;
  }

  std::string name() const override { return "synthetic"; }

  std::vector<std::string> capabilities() const override {
    return {"stills", "rapid", "timelapse", "birdflight", "exposure", "lux"};
  }

  SensorLimits limits() const override {
    SensorLimits l;
    l.exposure_min_us = kExposureMinUs;
    l.exposure_max_us = kExposureMaxUs;
    l.gain_min = kGainMin;
    l.gain_max = kGainMax;
    return l;
  }

  Frame capture(int64_t exposure_us, double gain) override {
    Frame f;
    f.ts = now_unix();
    f.exposure_us = quantise_exposure(exposure_us);
    f.gain = quantise_gain(gain);
    f.lux = scene_lux(f.ts);
    f.y = render(f.ts, static_cast<double>(f.exposure_us) * f.gain, f.lux, &f.color);
    ++seq_;
    return f;
  }

 private:
  // The IMX477's habits, reproduced: exposure quantises to line times and
  // gain snaps to its own steps. The AE loop has to live with both.
  static int64_t quantise_exposure(int64_t us) {
    const int64_t line_us = 14;
    const int64_t q = (us / line_us) * line_us;
    return q < static_cast<int64_t>(kExposureMinUs) ? static_cast<int64_t>(kExposureMinUs) : q;
  }
  static double quantise_gain(double g) {
    const double q = std::floor(g * 16.0) / 16.0;
    return clamp(q, kGainMin, kGainMax);
  }

  double scene_lux(double ts) const {
    if (site_set_) {
      // Real light for the configured spot: full daylight scaled by solar
      // elevation, twilight decaying below the horizon.
      const SunPos sp = sun_position(ts, site_.lat_deg, site_.lon_deg);
      if (sp.elevation_deg > 0.0)
        return 400.0 + 100000.0 * std::sin(deg2rad(clamp(sp.elevation_deg, 0.0, 90.0)));
      // Twilight decays ~e-fold per 2.5 deg; the floor is a moonlit night,
      // which is what the legacy s191 exposures were pointed at.
      return std::max(0.05, 400.0 * std::exp(sp.elevation_deg / 2.5));
    }
    // No site: a bright, slowly breathing daylight scene.
    return 20000.0 * (1.0 + 0.15 * std::sin((ts - t0_) / 30.0));
  }

  Gray8 render(double ts, double energy, double lux, Rgb8* color) const {
    Gray8 img(w_, h_);
    *color = Rgb8(w_, h_);
    const double t = ts - t0_;

    // Colour follows the light. High sun: blue sky. Low sun: the sky warms
    // toward the horizon exactly when the planner says golden hour is --
    // the same solar elevation that drives the scene's lux.
    double warmth = 0.0;
    if (site_set_) {
      const SunPos sp = sun_position(ts, site_.lat_deg, site_.lon_deg);
      if (sp.elevation_deg < 15.0)
        warmth = clamp((15.0 - sp.elevation_deg) / 20.0, 0.0, 1.0);
    }
    const double sky_r = 0.72 + 0.30 * warmth;
    const double sky_g = 0.84 - 0.08 * warmth;
    const double sky_b = 1.00 - 0.36 * warmth;

    // Sensor response: luma = reflectance * lux * energy * k, with k chosen
    // so 2000 us x 1.0 against ~5000 lux puts the treeline near the metering
    // target. Saturates at 255 like the real thing.
    const double scale = 3.4e-5 * lux * energy;

    const int horizon = static_cast<int>(h_ * 0.45);
    const uint32_t fseed = static_cast<uint32_t>(seq_ * 2654435761u);

    // Bird: crosses the sky every ~7 s, wingbeat modulating its height.
    const double phase = std::fmod(t, 7.0) / 7.0;
    const double bird_cx = phase * (w_ + 80.0) - 40.0;
    const double bird_cy = h_ * 0.18 + 18.0 * std::sin(t * 1.3);
    const double wing = 1.0 + 0.6 * std::sin(t * 22.0);
    const double bird_rx = w_ * 0.018;
    const double bird_ry = h_ * 0.008 * wing + 1.5;

    for (int y = 0; y < h_; ++y) {
      uint8_t* row = &img.px[static_cast<size_t>(y) * w_];
      uint8_t* crow = &color->px[static_cast<size_t>(y) * w_ * 3];
      const bool sky_row = y < horizon;
      // Treeline boundary wobbles per column below.
      for (int x = 0; x < w_; ++x) {
        double refl;
        double cr, cg, cb;  // channel weights around the luma
        const uint32_t colh = hash32(static_cast<uint32_t>(x) * 7919u);
        const int tree_top = horizon + static_cast<int>((colh & 31)) - 16;
        if (sky_row && y < tree_top) {
          // Sky: bright, slightly darker toward the top; warmer nearer the
          // horizon when the sun is low.
          refl = 0.95 - 0.15 * (1.0 - static_cast<double>(y) / horizon);
          const double low = static_cast<double>(y) / horizon;  // 1 at the treeline
          cr = sky_r + 0.10 * warmth * low;
          cg = sky_g;
          cb = sky_b - 0.10 * warmth * low;
          // The bird.
          const double dx = (x - bird_cx) / bird_rx;
          const double dy = (y - bird_cy) / bird_ry;
          if (dx * dx + dy * dy < 1.0) {
            refl = 0.05;
            cr = cg = cb = 1.0;  // a silhouette has no colour to speak of
          }
        } else {
          // Treeline: dark, heavily textured -- the contrast tiles feed.
          const uint32_t n = hash32(static_cast<uint32_t>(y * w_ + x) * 2246822519u);
          refl = 0.28 + 0.18 * (static_cast<double>(n & 255) / 255.0 - 0.5);
          cr = 0.78;
          cg = 0.92;
          cb = 0.55;  // conifer olive
        }
        // Photon-ish noise, per frame.
        const uint32_t nz = hash32((static_cast<uint32_t>(y * w_ + x)) ^ fseed);
        const double noise = (static_cast<double>(nz & 63) - 31.5) / 10.0;
        const double v = refl * scale + noise;
        row[x] = static_cast<uint8_t>(clamp(v, 0.0, 255.0));
        crow[x * 3] = static_cast<uint8_t>(clamp(v * cr, 0.0, 255.0));
        crow[x * 3 + 1] = static_cast<uint8_t>(clamp(v * cg, 0.0, 255.0));
        crow[x * 3 + 2] = static_cast<uint8_t>(clamp(v * cb, 0.0, 255.0));
      }
    }
    return img;
  }

  int w_, h_;
  Site site_;
  bool site_set_;
  double t0_;
  int64_t seq_ = 0;
};

}  // namespace

std::unique_ptr<Backend> make_synthetic_backend(const Config& cfg) {
  return std::make_unique<SyntheticBackend>(cfg);
}

}  // namespace bs
