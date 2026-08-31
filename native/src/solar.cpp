// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/solar.hpp"

#include <cmath>

#include "birdshot/mathkit.hpp"

namespace bs {

namespace {

// Julian centuries since J2000.0 for a POSIX timestamp.
double julian_century(double unix_utc) {
  const double jd = unix_utc / 86400.0 + 2440587.5;
  return (jd - 2451545.0) / 36525.0;
}

struct SolarTerms {
  double declination_deg;
  double eqtime_min;
};

// The NOAA General Solar Position Calculations, verbatim. Angles in degrees
// throughout, converted at the trig calls.
SolarTerms solar_terms(double t) {
  const double l0 = wrap360(280.46646 + t * (36000.76983 + t * 0.0003032));
  const double m = 357.52911 + t * (35999.05029 - 0.0001537 * t);
  const double e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
  const double c = std::sin(deg2rad(m)) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
                   std::sin(deg2rad(2 * m)) * (0.019993 - 0.000101 * t) +
                   std::sin(deg2rad(3 * m)) * 0.000289;
  const double true_long = l0 + c;
  const double omega = 125.04 - 1934.136 * t;
  const double app_long = true_long - 0.00569 - 0.00478 * std::sin(deg2rad(omega));
  const double eps0 =
      23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0;
  const double eps = eps0 + 0.00256 * std::cos(deg2rad(omega));

  SolarTerms out;
  out.declination_deg = rad2deg(std::asin(std::sin(deg2rad(eps)) * std::sin(deg2rad(app_long))));

  const double y = std::pow(std::tan(deg2rad(eps / 2.0)), 2.0);
  const double eq = y * std::sin(2.0 * deg2rad(l0)) - 2.0 * e * std::sin(deg2rad(m)) +
                    4.0 * e * y * std::sin(deg2rad(m)) * std::cos(2.0 * deg2rad(l0)) -
                    0.5 * y * y * std::sin(4.0 * deg2rad(l0)) -
                    1.25 * e * e * std::sin(2.0 * deg2rad(m));
  out.eqtime_min = 4.0 * rad2deg(eq);
  return out;
}

// NOAA atmospheric refraction correction, degrees, from true elevation.
double refraction_deg(double h) {
  if (h > 85.0) return 0.0;
  const double tanh = std::tan(deg2rad(h));
  double corr;
  if (h > 5.0) {
    corr = 58.1 / tanh - 0.07 / std::pow(tanh, 3.0) + 0.000086 / std::pow(tanh, 5.0);
  } else if (h > -0.575) {
    corr = 1735.0 + h * (-518.2 + h * (103.4 + h * (-12.79 + h * 0.711)));
  } else {
    corr = -20.774 / tanh;
  }
  return corr / 3600.0;
}

double midnight_utc(double unix_in_day) {
  return std::floor(unix_in_day / 86400.0) * 86400.0;
}

}  // namespace

SunPos sun_position(double unix_utc, double lat_deg, double lon_deg) {
  const double t = julian_century(unix_utc);
  const SolarTerms st = solar_terms(t);

  const double minutes_utc = std::fmod(unix_utc, 86400.0) / 60.0;
  double tst = minutes_utc + st.eqtime_min + 4.0 * lon_deg;  // true solar time, min
  tst = std::fmod(tst + 1440.0 * 4.0, 1440.0);
  const double ha = tst / 4.0 - 180.0;  // hour angle, deg; negative = morning

  const double lat = deg2rad(lat_deg);
  const double decl = deg2rad(st.declination_deg);
  const double cos_zen = clamp(std::sin(lat) * std::sin(decl) +
                                   std::cos(lat) * std::cos(decl) * std::cos(deg2rad(ha)),
                               -1.0, 1.0);
  const double zen = rad2deg(std::acos(cos_zen));

  SunPos out;
  out.declination_deg = st.declination_deg;
  out.eqtime_min = st.eqtime_min;
  out.elevation_true_deg = 90.0 - zen;
  out.elevation_deg = out.elevation_true_deg + refraction_deg(out.elevation_true_deg);

  const double sin_zen = std::sin(deg2rad(zen));
  if (sin_zen < 1e-9) {
    out.azimuth_deg = 0.0;  // sun at zenith/nadir: azimuth undefined
  } else {
    const double az_arg =
        clamp((std::sin(lat) * cos_zen - std::sin(decl)) / (std::cos(lat) * sin_zen), -1.0, 1.0);
    const double a = rad2deg(std::acos(az_arg));
    out.azimuth_deg = ha > 0.0 ? wrap360(a + 180.0) : wrap360(540.0 - a);
  }
  return out;
}

std::optional<double> sun_crossing(double unix_in_day, double lat_deg, double lon_deg,
                                   double altitude_deg, bool rising) {
  const double day0 = midnight_utc(unix_in_day);

  // Hour-angle method, iterated: evaluate declination and the equation of
  // time at solar noon first, then re-evaluate at the estimated event time.
  // Two passes land within seconds, well inside the +/-1 min validation.
  double event = day0 + 43200.0;
  for (int pass = 0; pass < 3; ++pass) {
    const SolarTerms st = solar_terms(julian_century(event));
    const double lat = deg2rad(lat_deg);
    const double decl = deg2rad(st.declination_deg);
    const double cos_h0 = (std::sin(deg2rad(altitude_deg)) - std::sin(lat) * std::sin(decl)) /
                          (std::cos(lat) * std::cos(decl));
    if (cos_h0 < -1.0 || cos_h0 > 1.0) return std::nullopt;
    const double h0 = rad2deg(std::acos(cos_h0));  // positive
    const double minutes =
        720.0 - 4.0 * (lon_deg + (rising ? h0 : -h0)) - st.eqtime_min;
    event = day0 + minutes * 60.0;
  }
  return event;
}

double sun_transit(double unix_in_day, double lon_deg) {
  const double day0 = midnight_utc(unix_in_day);
  double noon = day0 + 43200.0;
  for (int pass = 0; pass < 2; ++pass) {
    const SolarTerms st = solar_terms(julian_century(noon));
    noon = day0 + (720.0 - 4.0 * lon_deg - st.eqtime_min) * 60.0;
  }
  return noon;
}

double altitude_rate_deg_per_hour(double lat_deg, double azimuth_deg) {
  // dh/dt = 15 deg/h * cos(lat) * sin(azimuth): positive in the east
  // (rising), negative in the west (setting).
  return 15.0 * std::cos(deg2rad(lat_deg)) * std::sin(deg2rad(azimuth_deg));
}

std::optional<double> sunset_azimuth_deg(double unix_in_day, double lat_deg, double lon_deg) {
  const auto when = sun_crossing(unix_in_day, lat_deg, lon_deg, kAltSunset, false);
  if (!when) return std::nullopt;
  return sun_position(*when, lat_deg, lon_deg).azimuth_deg;
}

}  // namespace bs
