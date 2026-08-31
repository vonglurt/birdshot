// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/plan.hpp"

#include <cmath>
#include <cstdio>
#include <ctime>

#include "birdshot/mathkit.hpp"
#include "birdshot/solar.hpp"

namespace bs {

namespace {

std::string local_hm(double unix_ts) {
  const time_t t = static_cast<time_t>(unix_ts);
  std::tm lt{};
#ifdef _WIN32
  localtime_s(&lt, &t);
#else
  localtime_r(&t, &lt);
#endif
  char buf[8];
  std::snprintf(buf, sizeof buf, "%02d:%02d", lt.tm_hour, lt.tm_min);
  return buf;
}

std::string local_date(double unix_ts) {
  const time_t t = static_cast<time_t>(unix_ts);
  std::tm lt{};
#ifdef _WIN32
  localtime_s(&lt, &t);
#else
  localtime_r(&t, &lt);
#endif
  char buf[16];
  std::snprintf(buf, sizeof buf, "%04d-%02d-%02d", lt.tm_year + 1900, lt.tm_mon + 1, lt.tm_mday);
  return buf;
}

}  // namespace

double lens_fov_deg(double focal_mm, double sensor_width_mm) {
  if (focal_mm <= 0.0 || sensor_width_mm <= 0.0) return 0.0;
  return 2.0 * rad2deg(std::atan(sensor_width_mm / (2.0 * focal_mm)));
}

ShootPlan make_plan(const Site& site, double start_unix, int days, double lens_focal_mm,
                    double sensor_width_mm) {
  ShootPlan plan;
  plan.site = site;
  plan.lens_fov_deg = lens_fov_deg(lens_focal_mm, sensor_width_mm);

  double prev_az = -1.0;
  bool any = false;
  for (int i = 0; i < days; ++i) {
    DayPlan d;
    d.day_unix = start_unix + i * 86400.0;

    const auto sunset = sun_crossing(d.day_unix, site.lat_deg, site.lon_deg, kAltSunset, false);
    if (!sunset) {
      plan.days.push_back(d);
      prev_az = -1.0;
      continue;
    }
    d.sun_sets = true;
    d.sunset_unix = *sunset;
    d.sunset_azimuth_deg = sun_position(*sunset, site.lat_deg, site.lon_deg).azimuth_deg;
    d.descent_deg_per_hour =
        altitude_rate_deg_per_hour(site.lat_deg, d.sunset_azimuth_deg);

    const auto contact =
        sun_crossing(d.day_unix, site.lat_deg, site.lon_deg, kAltLowerLimbTouch, false);
    if (contact) {
      d.contact_unix = *contact;
      d.contact_window_min = (*sunset - *contact) / 60.0;
    }
    const auto golden =
        sun_crossing(d.day_unix, site.lat_deg, site.lon_deg, kAltGoldenHour, false);
    if (golden) d.golden_start_unix = *golden;
    const auto civil = sun_crossing(d.day_unix, site.lat_deg, site.lon_deg, kAltCivil, false);
    if (civil) d.civil_end_unix = *civil;

    if (prev_az >= 0.0) d.azimuth_drift_deg = wrap180(d.sunset_azimuth_deg - prev_az);
    prev_az = d.sunset_azimuth_deg;

    if (!any) {
      plan.az_min_deg = plan.az_max_deg = d.sunset_azimuth_deg;
      any = true;
    } else {
      if (d.sunset_azimuth_deg < plan.az_min_deg) plan.az_min_deg = d.sunset_azimuth_deg;
      if (d.sunset_azimuth_deg > plan.az_max_deg) plan.az_max_deg = d.sunset_azimuth_deg;
    }
    plan.days.push_back(d);
  }

  // A fixed frame centred on the middle of the swing holds the whole plan
  // when the swing fits the FOV with a little margin for framing error.
  const double swing = plan.az_max_deg - plan.az_min_deg;
  plan.lens_holds_range = plan.lens_fov_deg > 0.0 && swing <= plan.lens_fov_deg * 0.9;
  return plan;
}

std::string format_plan(const ShootPlan& plan) {
  std::string out;
  char line[256];
  std::snprintf(line, sizeof line, "site %s  (elev %.0f m)%s%s\n",
                format_latlon(plan.site.lat_deg, plan.site.lon_deg).c_str(), plan.site.elev_m,
                plan.site.name.empty() ? "" : "  ", plan.site.name.c_str());
  out += line;
  out += "date         golden  contact  sunset  dusk    azimuth   drift   descent  window\n";
  for (const auto& d : plan.days) {
    if (!d.sun_sets) {
      std::snprintf(line, sizeof line, "%s  the sun does not set at this latitude today\n",
                    local_date(d.day_unix).c_str());
      out += line;
      continue;
    }
    std::snprintf(line, sizeof line,
                  "%s   %s   %s    %s   %s   %6.1f%s  %+5.2f%s  %5.1f%s/h  %.1f min\n",
                  local_date(d.sunset_unix).c_str(),
                  d.golden_start_unix > 0 ? local_hm(d.golden_start_unix).c_str() : "--:--",
                  d.contact_unix > 0 ? local_hm(d.contact_unix).c_str() : "--:--",
                  local_hm(d.sunset_unix).c_str(),
                  d.civil_end_unix > 0 ? local_hm(d.civil_end_unix).c_str() : "--:--",
                  d.sunset_azimuth_deg, "\xC2\xB0", d.azimuth_drift_deg, "\xC2\xB0",
                  d.descent_deg_per_hour, "\xC2\xB0", d.contact_window_min);
    out += line;
  }
  const double swing = plan.az_max_deg - plan.az_min_deg;
  std::snprintf(line, sizeof line,
                "\nsunset azimuth swings %.1f%s over the plan (%.1f%s to %.1f%s)\n", swing,
                "\xC2\xB0", plan.az_min_deg, "\xC2\xB0", plan.az_max_deg, "\xC2\xB0");
  out += line;
  if (plan.lens_fov_deg > 0.0) {
    std::snprintf(line, sizeof line,
                  "lens holds %.1f%s: a fixed frame %s this range%s\n", plan.lens_fov_deg,
                  "\xC2\xB0", plan.lens_holds_range ? "HOLDS" : "CLIPS",
                  plan.lens_holds_range
                      ? ""
                      : " -- re-aim during the plan, or go wider");
    out += line;
  }
  return out;
}

Json plan_to_json(const ShootPlan& plan) {
  Json out = Json::object();
  out["site_lat"] = plan.site.lat_deg;
  out["site_lon"] = plan.site.lon_deg;
  out["site_name"] = plan.site.name;
  out["lens_fov_deg"] = plan.lens_fov_deg;
  out["lens_holds_range"] = plan.lens_holds_range;
  out["azimuth_min_deg"] = plan.az_min_deg;
  out["azimuth_max_deg"] = plan.az_max_deg;
  Json::Array days;
  for (const auto& d : plan.days) {
    Json j = Json::object();
    j["date"] = local_date(d.day_unix);
    j["sun_sets"] = d.sun_sets;
    if (d.sun_sets) {
      j["sunset_unix"] = d.sunset_unix;
      j["sunset_azimuth_deg"] = d.sunset_azimuth_deg;
      j["contact_unix"] = d.contact_unix;
      j["contact_window_min"] = d.contact_window_min;
      j["descent_deg_per_hour"] = d.descent_deg_per_hour;
      j["golden_start_unix"] = d.golden_start_unix;
      j["civil_end_unix"] = d.civil_end_unix;
      j["azimuth_drift_deg"] = d.azimuth_drift_deg;
    }
    days.push_back(std::move(j));
  }
  out["days"] = Json(std::move(days));
  return out;
}

}  // namespace bs
