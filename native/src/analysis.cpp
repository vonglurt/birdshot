// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/analysis.hpp"

#include <cmath>

#include "birdshot/config.hpp"
#include "birdshot/mathkit.hpp"

namespace bs {

namespace {

constexpr double kEps = 1e-6;

double round4(double v) { return std::round(v * 10000.0) / 10000.0; }

Hist256 hist_of_rows(const Gray8& y, int row0, int row1) {
  Hist256 h;
  if (row1 > row0 && !y.empty())
    h.add(&y.px[static_cast<size_t>(row0) * y.w],
          static_cast<size_t>(row1 - row0) * static_cast<size_t>(y.w));
  return h;
}

}  // namespace

Json FrameStats::to_json() const {
  Json d = Json::object();
  d["mean"] = round4(mean);
  d["std"] = round4(stddev);
  d["p1"] = round4(p1);
  d["p5"] = round4(p5);
  d["p50"] = round4(p50);
  d["p95"] = round4(p95);
  d["p99"] = round4(p99);
  d["clip_hi"] = round4(clip_hi);
  d["clip_lo"] = round4(clip_lo);
  d["sky_p50"] = round4(sky_p50);
  d["sky_clip_hi"] = round4(sky_clip_hi);
  d["subject_clip_hi"] = round4(subject_clip_hi);
  d["subject_p50"] = round4(subject_p50);
  d["subject_p95"] = round4(subject_p95);
  d["meter"] = round4(meter);
  d["sharpness"] = round4(sharpness);
  d["sharpness_norm"] = round4(sharpness_norm);
  d["tenengrad"] = round4(tenengrad);
  d["focus_measured"] = focus_measured;
  d["best_tile_std"] = round4(best_tile_std);
  d["contrast_tiles"] = contrast_tiles;
  d["dynamic_range"] = round4(dynamic_range);
  d["is_dark"] = is_dark;
  d["is_blown"] = is_blown;
  d["is_empty"] = is_empty;
  d["has_subject"] = has_subject;
  d["verdict"] = verdict;
  return d;
}

double normalised_sharpness(double region_stddev, double lap_var) {
  // Values near 100 mean edges as strong as the local contrast allows
  // (sharp); single digits mean detail has been smeared away.
  return 100.0 * std::sqrt(lap_var > 0.0 ? lap_var : 0.0) /
         (region_stddev > 1.0 ? region_stddev : 1.0);
}

double laplacian_variance(const Gray8& y) {
  if (y.h < 3 || y.w < 3) return 0.0;
  double sum = 0.0, sumsq = 0.0;
  const int64_t n = static_cast<int64_t>(y.h - 2) * (y.w - 2);
  for (int r = 1; r < y.h - 1; ++r) {
    const uint8_t* up = &y.px[static_cast<size_t>(r - 1) * y.w];
    const uint8_t* mid = &y.px[static_cast<size_t>(r) * y.w];
    const uint8_t* dn = &y.px[static_cast<size_t>(r + 1) * y.w];
    for (int c = 1; c < y.w - 1; ++c) {
      const int lap = 4 * mid[c] - up[c] - dn[c] - mid[c - 1] - mid[c + 1];
      sum += lap;
      sumsq += static_cast<double>(lap) * lap;
    }
  }
  const double m = sum / static_cast<double>(n);
  return sumsq / static_cast<double>(n) - m * m;
}

double tenengrad(const Gray8& y, int step) {
  if (step < 1) step = 1;
  const int sh = y.h / step, sw = y.w / step;
  if (sh < 3 || sw < 3) return 0.0;
  auto at = [&](int r, int c) -> int {
    return y.px[static_cast<size_t>(r) * step * y.w + static_cast<size_t>(c) * step];
  };
  double total = 0.0;
  const int64_t n = static_cast<int64_t>(sh - 2) * (sw - 2);
  for (int r = 1; r < sh - 1; ++r) {
    for (int c = 1; c < sw - 1; ++c) {
      const int gx = at(r - 1, c + 1) + 2 * at(r, c + 1) + at(r + 1, c + 1) -
                     at(r - 1, c - 1) - 2 * at(r, c - 1) - at(r + 1, c - 1);
      const int gy = at(r + 1, c - 1) + 2 * at(r + 1, c) + at(r + 1, c + 1) -
                     at(r - 1, c - 1) - 2 * at(r - 1, c) - at(r - 1, c + 1);
      total += static_cast<double>(gx) * gx + static_cast<double>(gy) * gy;
    }
  }
  return total / static_cast<double>(n);
}

void tile_stats(const Gray8& y, int rows, int cols, std::vector<double>* means,
                std::vector<double>* stds) {
  means->assign(static_cast<size_t>(rows) * cols, 0.0);
  stds->assign(static_cast<size_t>(rows) * cols, 0.0);
  const int th = y.h / rows, tw = y.w / cols;
  if (th < 2 || tw < 2) return;
  for (int tr = 0; tr < rows; ++tr) {
    for (int tc = 0; tc < cols; ++tc) {
      double sum = 0.0, sumsq = 0.0;
      for (int r = tr * th; r < (tr + 1) * th; ++r) {
        const uint8_t* row = &y.px[static_cast<size_t>(r) * y.w + static_cast<size_t>(tc) * tw];
        for (int c = 0; c < tw; ++c) {
          sum += row[c];
          sumsq += static_cast<double>(row[c]) * row[c];
        }
      }
      const double n = static_cast<double>(th) * tw;
      const double m = sum / n;
      (*means)[static_cast<size_t>(tr) * cols + tc] = m;
      const double var = sumsq / n - m * m;
      (*stds)[static_cast<size_t>(tr) * cols + tc] = std::sqrt(var > 0.0 ? var : 0.0);
    }
  }
}

FocusMap focus_map(const Gray8& y, int rows, int cols) {
  FocusMap out;
  out.rows = rows;
  out.cols = cols;
  out.energy.assign(static_cast<size_t>(rows) * cols, 0.0f);
  if (y.h < rows * 3 || y.w < cols * 3) return out;

  const int lh = y.h - 2, lw = y.w - 2;
  const int th = lh / rows, tw = lw / cols;
  std::vector<double> energy(static_cast<size_t>(rows) * cols, 0.0);
  for (int r = 1; r < 1 + th * rows; ++r) {
    const int tr = (r - 1) / th;
    const uint8_t* up = &y.px[static_cast<size_t>(r - 1) * y.w];
    const uint8_t* mid = &y.px[static_cast<size_t>(r) * y.w];
    const uint8_t* dn = &y.px[static_cast<size_t>(r + 1) * y.w];
    for (int c = 1; c < 1 + tw * cols; ++c) {
      const int tc = (c - 1) / tw;
      const double lap = 4 * mid[c] - up[c] - dn[c] - mid[c - 1] - mid[c + 1];
      energy[static_cast<size_t>(tr) * cols + tc] += lap * lap;
    }
  }
  const double tile_px = static_cast<double>(th) * tw;
  double peak = 0.0;
  for (size_t i = 0; i < energy.size(); ++i) {
    energy[i] /= tile_px;
    if (energy[i] > peak) {
      peak = energy[i];
      out.best_row = static_cast<int>(i) / cols;
      out.best_col = static_cast<int>(i) % cols;
    }
  }
  out.best_raw = peak;
  if (peak > 0.0) {
    // Square root compresses the range so mid-focus regions stay visible
    // next to one very sharp edge.
    for (size_t i = 0; i < energy.size(); ++i)
      out.energy[i] = static_cast<float>(std::sqrt(energy[i] / peak));
  }
  return out;
}

FrameStats meter_only(const Gray8& y, const Config& cfg) {
  FrameStats st;
  if (y.empty()) return st;

  const double sky_frac = cfg.num("sky_zone_frac", 0.40);
  const int split = clamp(static_cast<int>(y.h * sky_frac), 1, y.h - 1);

  const Hist256 h_sky = hist_of_rows(y, 0, split);
  const Hist256 h_sub = hist_of_rows(y, split, y.h);
  Hist256 h_all = h_sky;
  h_all += h_sub;

  const HistStats all = hist_stats(h_all);
  st.mean = all.mean;
  st.stddev = all.stddev;
  st.p1 = all.p1;
  st.p5 = all.p5;
  st.p50 = all.p50;
  st.p95 = all.p95;
  st.p99 = all.p99;
  st.clip_hi = all.clip_hi;
  st.clip_lo = all.clip_lo;
  st.dynamic_range = st.p99 - st.p1;

  const HistStats sky = hist_stats(h_sky);
  st.sky_p50 = sky.p50;
  st.sky_clip_hi = sky.clip_hi;
  const HistStats sub = hist_stats(h_sub);
  st.subject_p50 = sub.p50;
  st.subject_p95 = sub.p95;
  st.subject_clip_hi = sub.clip_hi;

  const double sw = cfg.num("sky_weight", 0.15);
  const double uw = cfg.num("subject_weight", 1.0);
  st.meter = (uw * st.subject_p50 + sw * st.sky_p50) / (uw + sw > kEps ? uw + sw : kEps);

  // Exposure gates still apply; content and focus ones do not.
  st.is_dark = st.p95 < cfg.num("dark_p95_max", 40.0);
  st.is_blown = st.subject_clip_hi > cfg.num("blown_clip_frac", 0.35);
  st.best_tile_std = st.stddev;
  st.contrast_tiles = 1;
  st.verdict = st.is_blown ? "blown" : (st.is_dark ? "dark" : "ok");
  return st;
}

FrameStats analyse(const Gray8& y, const Config& cfg, const Gray8& hires_crop, bool focus,
                   bool tiles) {
  FrameStats st;
  if (y.empty()) {
    st.verdict = "empty";
    st.is_empty = true;
    return st;
  }

  st = meter_only(y, cfg);  // whole-frame + zone histograms

  if (focus) {
    const Gray8& focus_src = hires_crop.empty() ? y : hires_crop;
    st.sharpness = laplacian_variance(focus_src);
    st.tenengrad = tenengrad(focus_src);
    Hist256 h;
    h.add(focus_src.px.data(), focus_src.size());
    st.sharpness_norm = normalised_sharpness(hist_stats(h).stddev, st.sharpness);
    st.focus_measured = true;
  }

  const double content_min = cfg.num("content_std_min", 8.0);
  if (tiles) {
    std::vector<double> tmean, tstd;
    tile_stats(y, kTileRows, kTileCols, &tmean, &tstd);
    double best = 0.0;
    int carrying = 0;
    for (double s : tstd) {
      if (s > best) best = s;
      if (s > content_min) ++carrying;
    }
    st.best_tile_std = best;
    st.contrast_tiles = carrying;
  } else {
    // Metering-only: no content claim either way, so leave the gates alone.
    st.best_tile_std = st.stddev;
    st.contrast_tiles = 1;
  }

  return finalize(st, cfg);
}

FrameStats& finalize(FrameStats& st, const Config& cfg) {
  const double content_min = cfg.num("content_std_min", 8.0);
  // Empty means either nothing has contrast anywhere, or the whole frame is
  // soft. Until a focus pass has run, sharpness is 0 and would read as
  // "soft"; the verdict stays provisional rather than falsely condemning.
  const bool soft = st.focus_measured && st.sharpness_norm < cfg.num("blur_threshold", 12.0);
  const bool featureless = st.contrast_tiles == 0;
  st.is_empty = featureless || (soft && st.best_tile_std < content_min * 2.0);
  st.has_subject = st.contrast_tiles > 0 && !soft;

  if (st.is_blown) st.verdict = "blown";
  else if (st.is_dark) st.verdict = "dark";
  else if (st.is_empty) st.verdict = "empty";
  else st.verdict = "ok";
  return st;
}

FrameStats& refine_with_hires(FrameStats& st, const Gray8& crop, const Config& cfg) {
  if (crop.empty()) return st;
  st.sharpness = laplacian_variance(crop);
  st.tenengrad = tenengrad(crop);
  Hist256 h;
  h.add(crop.px.data(), crop.size());
  st.sharpness_norm = normalised_sharpness(hist_stats(h).stddev, st.sharpness);
  st.focus_measured = true;
  return finalize(st, cfg);
}

}  // namespace bs
