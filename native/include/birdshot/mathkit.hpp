// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The in-house math kit: histogram statistics for 8-bit planes, the small
// numeric helpers everything else leans on. All of birdshot's metering is
// histogram-based -- one pass over the pixels replaces the half-dozen passes
// that separate mean/std/percentile/count calls would each make, and it is
// exact for 8-bit data.
#pragma once

#include <array>
#include <cstdint>
#include <vector>

namespace bs {

constexpr double kPi = 3.14159265358979323846;

inline double deg2rad(double d) { return d * kPi / 180.0; }
inline double rad2deg(double r) { return r * 180.0 / kPi; }

// Wrap an angle to [0, 360).
double wrap360(double deg);
// Wrap to (-180, 180].
double wrap180(double deg);

template <typename T>
T clamp(T v, T lo, T hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// 256-bin luminance histogram.
struct Hist256 {
  std::array<uint64_t, 256> bins{};
  uint64_t total = 0;

  void add(const uint8_t* data, size_t n);
  Hist256& operator+=(const Hist256& o);
};

// Everything the metering path reads, from one histogram.
struct HistStats {
  double mean = 0.0;
  double stddev = 0.0;
  double p1 = 0.0, p5 = 0.0, p50 = 0.0, p95 = 0.0, p99 = 0.0;
  double clip_hi = 0.0;  // fraction >= 250
  double clip_lo = 0.0;  // fraction <= 5
};

HistStats hist_stats(const Hist256& h);

// Value below which fraction q of the histogram lies (0..255, exact for 8-bit).
double hist_percentile(const Hist256& h, double q);

double median(std::vector<double> v);  // by value: it sorts

}  // namespace bs
