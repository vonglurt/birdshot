// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Frame analysis: metering, exposure quality gates and focus measures. A
// straight port of the 1.x module -- same zones, same thresholds, same
// verdicts -- so an index.jsonl written here reads identically to one from
// the Pi. Runs on the shared 8-bit luma plane; histogram passes only.
#pragma once

#include <string>
#include <utility>
#include <vector>

#include "birdshot/image.hpp"
#include "birdshot/json.hpp"

namespace bs {

class Config;

constexpr int kTileCols = 8;
constexpr int kTileRows = 6;

// Everything we measure about one frame.
struct FrameStats {
  // Luminance distribution over the whole frame.
  double mean = 0, stddev = 0;
  double p1 = 0, p5 = 0, p50 = 0, p95 = 0, p99 = 0;
  double clip_hi = 0;  // fraction >= 250
  double clip_lo = 0;  // fraction <= 5

  // Zone metering.
  double sky_p50 = 0;
  double sky_clip_hi = 0;
  double subject_clip_hi = 0;  // clipping in the zone we actually expose for
  double subject_p50 = 0;
  double subject_p95 = 0;
  double meter = 0;  // the single number the PID controls

  // Content and focus.
  double sharpness = 0;       // raw laplacian variance
  double sharpness_norm = 0;  // contrast-normalised, this is what we gate on
  double tenengrad = 0;
  bool focus_measured = false;  // false until a focus pass has actually run
  double best_tile_std = 0;
  int contrast_tiles = 0;    // tiles carrying real detail
  double dynamic_range = 0;  // p99 - p1

  // Verdicts.
  bool is_dark = false;
  bool is_blown = false;
  bool is_empty = false;
  bool has_subject = false;
  std::string verdict = "ok";  // ok | dark | blown | empty

  Json to_json() const;
};

// Focus measure that does not depend on how contrasty the scene is:
// normalised by the focus region's own spread, not the whole frame's.
double normalised_sharpness(double region_stddev, double lap_var);

// Variance of a 4-neighbour Laplacian. The classic focus measure.
double laplacian_variance(const Gray8& y);

// Mean Sobel gradient magnitude squared, subsampled by `step`.
double tenengrad(const Gray8& y, int step = 2);

// Per-tile mean and stddev over a rows x cols grid, trimming ragged edges.
void tile_stats(const Gray8& y, int rows, int cols, std::vector<double>* means,
                std::vector<double>* stds);

// Per-tile focus energy normalised 0..1, and the sharpest tile.
struct FocusMap {
  int rows = 0, cols = 0;
  std::vector<float> energy;  // rows*cols, 0..1
  int best_row = 0, best_col = 0;
  double best_raw = 0;
};
FocusMap focus_map(const Gray8& y, int rows = 9, int cols = 12);

// Measure one frame. hires_crop, when non-empty, is a native-resolution
// centre crop that focus is judged on instead of the preview plane.
FrameStats analyse(const Gray8& y, const Config& cfg, const Gray8& hires_crop = {},
                   bool focus = true, bool tiles = true);

// Metering fast path for rapid capture: two histogram passes, nothing else.
FrameStats meter_only(const Gray8& y, const Config& cfg);

// Derive the content verdict from the focus and tile measures.
FrameStats& finalize(FrameStats& st, const Config& cfg);

// Re-measure focus on a native-resolution crop and redo the verdict.
FrameStats& refine_with_hires(FrameStats& st, const Gray8& crop, const Config& cfg);

}  // namespace bs
