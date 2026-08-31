// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Solar ephemeris, written rather than imported (vendor/ is empty by policy).
// The NOAA solar-position algorithm: good to a small fraction of a degree for
// centuries either side of J2000, validated in the Horizons design review to
// within a minute of the almanac for rise/set events.
//
// Everything takes POSIX timestamps in UTC and site coordinates in degrees
// (+N, +E). Azimuth is degrees clockwise from true north; elevation is
// degrees above the horizon.
#pragma once

#include <optional>

namespace bs {

struct SunPos {
  double azimuth_deg = 0.0;
  double elevation_deg = 0.0;        // apparent (refraction-corrected)
  double elevation_true_deg = 0.0;   // geometric
  double declination_deg = 0.0;
  double eqtime_min = 0.0;           // equation of time, minutes
};

// Event altitudes (of the sun's centre, apparent). The two limb constants are
// the ones the Horizons review validated: the upper limb disappears when the
// centre reaches -0.833 deg (refraction 0.567 + semidiameter 0.267), and the
// lower limb first touches the horizon at -0.203 deg.
constexpr double kAltSunset = -0.833;
constexpr double kAltLowerLimbTouch = -0.203;
constexpr double kAltCivil = -6.0;
constexpr double kAltNautical = -12.0;
constexpr double kAltAstronomical = -18.0;
constexpr double kAltGoldenHour = 6.0;

SunPos sun_position(double unix_utc, double lat_deg, double lon_deg);

// The moment on the given UTC day when the sun's centre crosses
// `altitude_deg` (rising or setting). `unix_in_day` is any timestamp within
// that UTC day. Empty when the sun never reaches the altitude that day
// (polar day/night, or a twilight that never ends in summer).
std::optional<double> sun_crossing(double unix_in_day, double lat_deg, double lon_deg,
                                   double altitude_deg, bool rising);

// Solar transit (local solar noon) on the given UTC day.
double sun_transit(double unix_in_day, double lon_deg);

// Rate of change of solar altitude, degrees per hour, from the exact
// identity dh/dt = -15 deg/h * cos(latitude) * sin(azimuth). Negative when
// setting. Validated: 11.5 deg/h at a 40N equinox sunset.
double altitude_rate_deg_per_hour(double lat_deg, double azimuth_deg);

// Where on the horizon the sun sets: azimuth at the sunset crossing.
// The seasonal swing of this number (62.6 deg over the year at 40N) is why
// a fixed sunset camera needs the lens FOV checked against the site.
std::optional<double> sunset_azimuth_deg(double unix_in_day, double lat_deg, double lon_deg);

}  // namespace bs
