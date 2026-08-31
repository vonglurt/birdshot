// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Capture Bird Flight: decide, frame by frame, whether NOW is the shot. The
// detector watches for a bird-shaped opportunity -- a discrete dark subject
// surrounded by bright sky, sharp along its boundary, well inside the frame,
// in a frame that is mostly sky -- and the engine fires a burst when every
// gate agrees. Same bf_* keys, same gate order, same reason strings as 1.x.
#pragma once

#include <string>
#include <vector>

#include "birdshot/image.hpp"
#include "birdshot/json.hpp"

namespace bs {

class Config;

// The subject search runs on a 4x-downsampled mask: at 160x120 the
// connected-component pass is microseconds, and a bird smaller than 4 px was
// never going to pass the sharpness gate anyway.
constexpr int kBfDown = 4;

struct Sighting {
  bool present = false;  // a plausible subject exists at all
  bool take = false;     // every gate passed
  std::vector<std::string> reasons;  // why NOT taken
  // Measurements, for the readout and the EXIF UserComment.
  double motion_frac = 0.0;
  double sky_frac = 0.0;
  double area_frac = 0.0;
  double ring_sky_frac = 0.0;
  double sharpness = 0.0;
  bool has_subject_box = false;
  double centroid_x = 0.0, centroid_y = 0.0;    // full-res px
  int bbox_x0 = 0, bbox_y0 = 0, bbox_x1 = 0, bbox_y1 = 0;

  Json to_json() const;
};

// How crisp the subject's edges are, 0..~100: mean of the top-decile
// gradient magnitudes inside the (padded) subject box.
double boundary_sharpness(const Gray8& y8, int x0, int y0, int x1, int y1);

class BirdFlightDetector {
 public:
  explicit BirdFlightDetector(const Config& cfg) : cfg_(cfg) {}

  Sighting update(const Gray8& y8);

 private:
  const Config& cfg_;
  Gray8 prev_;
  bool have_prev_ = false;
};

}  // namespace bs
