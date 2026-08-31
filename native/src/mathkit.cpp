// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/mathkit.hpp"

#include <algorithm>
#include <cmath>

namespace bs {

double wrap360(double deg) {
  double r = std::fmod(deg, 360.0);
  return r < 0.0 ? r + 360.0 : r;
}

double wrap180(double deg) {
  double r = wrap360(deg);
  return r > 180.0 ? r - 360.0 : r;
}

void Hist256::add(const uint8_t* data, size_t n) {
  // Four sub-histograms breaks the store-to-load dependency chain between
  // consecutive pixels that share a bin; measurably faster on wide skies.
  uint64_t h0[256] = {0}, h1[256] = {0}, h2[256] = {0}, h3[256] = {0};
  size_t i = 0;
  for (; i + 4 <= n; i += 4) {
    ++h0[data[i]];
    ++h1[data[i + 1]];
    ++h2[data[i + 2]];
    ++h3[data[i + 3]];
  }
  for (; i < n; ++i) ++h0[data[i]];
  for (int b = 0; b < 256; ++b) bins[static_cast<size_t>(b)] += h0[b] + h1[b] + h2[b] + h3[b];
  total += n;
}

Hist256& Hist256::operator+=(const Hist256& o) {
  for (size_t b = 0; b < 256; ++b) bins[b] += o.bins[b];
  total += o.total;
  return *this;
}

double hist_percentile(const Hist256& h, double q) {
  if (h.total == 0) return 0.0;
  // Mirrors numpy searchsorted on the normalised CDF: the first bin whose
  // cumulative fraction reaches q. Keeps verdicts identical to the 1.x line.
  const double want = q * static_cast<double>(h.total);
  uint64_t cum = 0;
  for (int b = 0; b < 256; ++b) {
    cum += h.bins[static_cast<size_t>(b)];
    if (static_cast<double>(cum) >= want) return static_cast<double>(b);
  }
  return 255.0;
}

HistStats hist_stats(const Hist256& h) {
  HistStats st;
  if (h.total == 0) return st;
  const double total = static_cast<double>(h.total);
  double sum = 0.0, sumsq = 0.0;
  for (int b = 0; b < 256; ++b) {
    const double n = static_cast<double>(h.bins[static_cast<size_t>(b)]);
    sum += n * b;
    sumsq += n * b * b;
  }
  st.mean = sum / total;
  const double var = std::max(0.0, sumsq / total - st.mean * st.mean);
  st.stddev = std::sqrt(var);
  st.p1 = hist_percentile(h, 0.01);
  st.p5 = hist_percentile(h, 0.05);
  st.p50 = hist_percentile(h, 0.50);
  st.p95 = hist_percentile(h, 0.95);
  st.p99 = hist_percentile(h, 0.99);
  uint64_t hi = 0, lo = 0;
  for (int b = 250; b < 256; ++b) hi += h.bins[static_cast<size_t>(b)];
  for (int b = 0; b <= 5; ++b) lo += h.bins[static_cast<size_t>(b)];
  st.clip_hi = static_cast<double>(hi) / total;
  st.clip_lo = static_cast<double>(lo) / total;
  return st;
}

double median(std::vector<double> v) {
  if (v.empty()) return 0.0;
  const size_t mid = v.size() / 2;
  std::nth_element(v.begin(), v.begin() + static_cast<long>(mid), v.end());
  return v[mid];
}

}  // namespace bs
