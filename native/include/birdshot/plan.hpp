// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Photography planning: given where the camera stands, what does each
// evening actually offer? Per day: the sunset instant and its azimuth, the
// lower-limb contact window, the descent rate, golden hour and civil dusk.
// Over the range: how far the sunset point walks along the horizon and
// whether the configured lens can hold it in a fixed frame -- the check
// from the Horizons review (a 6mm lens's 55.4 deg clips about two months
// around each solstice at 40N; a 4mm holds the whole year).
#pragma once

#include <string>
#include <vector>

#include "birdshot/geo.hpp"
#include "birdshot/json.hpp"

namespace bs {

struct DayPlan {
  double day_unix = 0;         // any instant in the UTC day
  bool sun_sets = false;       // false in polar day/night
  double sunset_unix = 0;
  double sunset_azimuth_deg = 0;
  double contact_unix = 0;     // lower limb touches the horizon
  double contact_window_min = 0;
  double descent_deg_per_hour = 0;
  double golden_start_unix = 0;  // sun at +6 deg
  double civil_end_unix = 0;     // sun at -6 deg
  double azimuth_drift_deg = 0;  // vs the previous day in the plan
};

struct ShootPlan {
  Site site;
  std::vector<DayPlan> days;
  // Range summary.
  double az_min_deg = 0, az_max_deg = 0;
  // Lens check.
  double lens_fov_deg = 0;
  bool lens_holds_range = false;
};

// `start_unix` is any instant in the first day (UTC days).
ShootPlan make_plan(const Site& site, double start_unix, int days, double lens_focal_mm,
                    double sensor_width_mm);

// Human-readable table, local time.
std::string format_plan(const ShootPlan& plan);

Json plan_to_json(const ShootPlan& plan);

// Horizontal field of view for a rectilinear lens.
double lens_fov_deg(double focal_mm, double sensor_width_mm);

}  // namespace bs
