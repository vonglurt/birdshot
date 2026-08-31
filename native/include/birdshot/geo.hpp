// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Earth coordinates. The site a camera stands on is a lat/lon/elevation
// triple; everything the planner and the alignment pass need about geometry
// on the ellipsoid lives here. WGS84, all in-tree.
#pragma once

#include <string>

namespace bs {

struct Site {
  double lat_deg = 0.0;   // +N
  double lon_deg = 0.0;   // +E
  double elev_m = 0.0;
  std::string name;

  bool valid() const {
    return lat_deg >= -90.0 && lat_deg <= 90.0 && lon_deg >= -180.0 && lon_deg <= 180.0;
  }
};

// WGS84 ellipsoid.
constexpr double kWgs84A = 6378137.0;          // semi-major axis, m
constexpr double kWgs84F = 1.0 / 298.257223563;

struct Ecef { double x = 0, y = 0, z = 0; };  // metres

Ecef to_ecef(const Site& s);

// Great-circle distance in metres (haversine on the mean radius -- good to
// ~0.5% everywhere, which is far tighter than anyone places a tripod).
double haversine_m(double lat1, double lon1, double lat2, double lon2);

// Initial bearing from point 1 to point 2, degrees clockwise from true north.
double initial_bearing_deg(double lat1, double lon1, double lat2, double lon2);

// Great-circle destination from a start point, bearing and distance.
void destination(double lat, double lon, double bearing_deg, double dist_m,
                 double* out_lat, double* out_lon);

// "40.7128N 74.0060W" style formatting for reports.
std::string format_latlon(double lat_deg, double lon_deg);

// Parse "40.7128,-74.0060" or "40.7128 -74.0060". Returns false on garbage.
bool parse_latlon(const std::string& text, double* lat, double* lon);

}  // namespace bs
