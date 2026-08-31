// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/geo.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>

#include "birdshot/mathkit.hpp"

namespace bs {

static constexpr double kMeanRadiusM = 6371008.8;

Ecef to_ecef(const Site& s) {
  const double lat = deg2rad(s.lat_deg), lon = deg2rad(s.lon_deg);
  const double e2 = kWgs84F * (2.0 - kWgs84F);
  const double sinlat = std::sin(lat);
  const double n = kWgs84A / std::sqrt(1.0 - e2 * sinlat * sinlat);
  Ecef out;
  out.x = (n + s.elev_m) * std::cos(lat) * std::cos(lon);
  out.y = (n + s.elev_m) * std::cos(lat) * std::sin(lon);
  out.z = (n * (1.0 - e2) + s.elev_m) * sinlat;
  return out;
}

double haversine_m(double lat1, double lon1, double lat2, double lon2) {
  const double p1 = deg2rad(lat1), p2 = deg2rad(lat2);
  const double dp = deg2rad(lat2 - lat1), dl = deg2rad(lon2 - lon1);
  const double a = std::sin(dp / 2) * std::sin(dp / 2) +
                   std::cos(p1) * std::cos(p2) * std::sin(dl / 2) * std::sin(dl / 2);
  return 2.0 * kMeanRadiusM * std::asin(std::sqrt(clamp(a, 0.0, 1.0)));
}

double initial_bearing_deg(double lat1, double lon1, double lat2, double lon2) {
  const double p1 = deg2rad(lat1), p2 = deg2rad(lat2);
  const double dl = deg2rad(lon2 - lon1);
  const double y = std::sin(dl) * std::cos(p2);
  const double x = std::cos(p1) * std::sin(p2) - std::sin(p1) * std::cos(p2) * std::cos(dl);
  return wrap360(rad2deg(std::atan2(y, x)));
}

void destination(double lat, double lon, double bearing_deg, double dist_m,
                 double* out_lat, double* out_lon) {
  const double d = dist_m / kMeanRadiusM;
  const double br = deg2rad(bearing_deg);
  const double p1 = deg2rad(lat), l1 = deg2rad(lon);
  const double p2 = std::asin(std::sin(p1) * std::cos(d) + std::cos(p1) * std::sin(d) * std::cos(br));
  const double l2 = l1 + std::atan2(std::sin(br) * std::sin(d) * std::cos(p1),
                                    std::cos(d) - std::sin(p1) * std::sin(p2));
  *out_lat = rad2deg(p2);
  *out_lon = wrap180(rad2deg(l2));
}

std::string format_latlon(double lat_deg, double lon_deg) {
  char buf[64];
  std::snprintf(buf, sizeof buf, "%.4f%c %.4f%c", std::fabs(lat_deg),
                lat_deg >= 0 ? 'N' : 'S', std::fabs(lon_deg), lon_deg >= 0 ? 'E' : 'W');
  return buf;
}

bool parse_latlon(const std::string& text, double* lat, double* lon) {
  const char* p = text.c_str();
  char* end = nullptr;
  double a = std::strtod(p, &end);
  if (end == p) return false;
  while (*end == ',' || *end == ' ' || *end == '\t') ++end;
  p = end;
  double b = std::strtod(p, &end);
  if (end == p) return false;
  if (a < -90.0 || a > 90.0 || b < -180.0 || b > 180.0) return false;
  *lat = a;
  *lon = b;
  return true;
}

}  // namespace bs
