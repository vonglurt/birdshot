// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/align.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <map>

#include "birdshot/naming.hpp"
#include "birdshot/solar.hpp"

namespace fs = std::filesystem;

namespace bs {

namespace {

std::string local_day(double unix_ts) {
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

bool frame_file(const fs::path& p) {
  const std::string ext = p.extension().string();
  return ext == ".jpg" || ext == ".jpeg" || ext == ".pgm" || ext == ".png";
}

}  // namespace

Json AlignResult::to_json() const {
  Json out = Json::object();
  if (!error.empty()) {
    out["error"] = error;
    return out;
  }
  Json::Array days_j;
  for (const auto& d : days) days_j.emplace_back(d);
  out["days"] = Json(std::move(days_j));
  out["matched_rows"] = matched_rows;
  Json::Array rows_j;
  for (const auto& row : rows) {
    Json r = Json::object();
    r["elev_deg"] = std::round(row.elev_deg * 1000.0) / 1000.0;
    Json::Array cells;
    for (int fi : row.frame_of_day) {
      if (fi < 0) {
        cells.emplace_back(Json());
        continue;
      }
      const AlignFrame& f = frames[static_cast<size_t>(fi)];
      Json c = Json::object();
      c["path"] = f.path;
      c["elev_deg"] = std::round(f.elev_deg * 1000.0) / 1000.0;
      c["az_deg"] = std::round(f.az_deg * 100.0) / 100.0;
      cells.push_back(std::move(c));
    }
    r["frames"] = Json(std::move(cells));
    rows_j.push_back(std::move(r));
  }
  out["rows"] = Json(std::move(rows_j));
  return out;
}

std::string AlignResult::summary() const {
  if (!error.empty()) return error;
  char buf[160];
  std::snprintf(buf, sizeof buf,
                "%zu frames across %zu days -> %zu aligned rows (%d complete on every day)",
                frames.size(), days.size(), rows.size(), matched_rows);
  return buf;
}

AlignResult align_days(const std::vector<std::string>& dirs, const Site& site,
                       double tolerance_deg, bool evenings_only) {
  AlignResult res;
  if (!site.valid()) {
    res.error = "no site configured -- set site_lat/site_lon first";
    return res;
  }

  // Collect timestamp-named frames, recursively; sessions keep frames in
  // shutter subdirectories, so a flat scan would miss COLLECT output.
  std::map<std::string, int> day_index;
  for (const auto& dir : dirs) {
    std::error_code ec;
    if (!fs::is_directory(dir, ec)) continue;
    for (fs::recursive_directory_iterator it(dir, ec), end; it != end && !ec; it.increment(ec)) {
      if (!it->is_regular_file(ec) || !frame_file(it->path())) continue;
      const auto ts = parse_timestamp_name(it->path().filename().string());
      if (!ts) continue;
      AlignFrame f;
      f.path = it->path().string();
      f.ts = *ts;
      const SunPos sp = sun_position(f.ts, site.lat_deg, site.lon_deg);
      f.elev_deg = sp.elevation_deg;
      f.az_deg = sp.azimuth_deg;
      // Evenings only: the sun west of the meridian. Morning frames of the
      // same elevation are a different scene (light from the other side).
      if (evenings_only && f.az_deg < 180.0) continue;
      res.frames.push_back(std::move(f));
    }
  }
  if (res.frames.empty()) {
    res.error = "no timestamp-named frames found";
    return res;
  }

  for (auto& f : res.frames) {
    const std::string day = local_day(f.ts);
    auto it = day_index.find(day);
    if (it == day_index.end()) it = day_index.emplace(day, 0).first;
  }
  for (auto& kv : day_index) {
    kv.second = static_cast<int>(res.days.size());
    res.days.push_back(kv.first);  // std::map iterates sorted
  }
  for (auto& f : res.frames) f.day = day_index[local_day(f.ts)];

  if (res.days.size() < 2) {
    res.error = "alignment needs frames from at least two days";
    return res;
  }

  // Per-day frame lists sorted by elevation.
  std::vector<std::vector<int>> by_day(res.days.size());
  for (size_t i = 0; i < res.frames.size(); ++i)
    by_day[static_cast<size_t>(res.frames[i].day)].push_back(static_cast<int>(i));
  for (auto& v : by_day)
    std::sort(v.begin(), v.end(), [&](int a, int b) {
      return res.frames[static_cast<size_t>(a)].elev_deg <
             res.frames[static_cast<size_t>(b)].elev_deg;
    });

  // The day with the most frames is the reference; every one of its frames
  // becomes a row, matched to the elevation-nearest frame on each other day.
  size_t ref = 0;
  for (size_t d = 1; d < by_day.size(); ++d)
    if (by_day[d].size() > by_day[ref].size()) ref = d;

  for (int ref_fi : by_day[ref]) {
    const double want = res.frames[static_cast<size_t>(ref_fi)].elev_deg;
    AlignRow row;
    row.elev_deg = want;
    row.frame_of_day.assign(res.days.size(), -1);
    row.frame_of_day[ref] = ref_fi;
    bool complete = true;
    for (size_t d = 0; d < by_day.size(); ++d) {
      if (d == ref) continue;
      int best = -1;
      double best_err = tolerance_deg;
      for (int fi : by_day[d]) {
        const double err = std::fabs(res.frames[static_cast<size_t>(fi)].elev_deg - want);
        if (err <= best_err) {
          best_err = err;
          best = fi;
        }
      }
      row.frame_of_day[d] = best;
      if (best < 0) complete = false;
    }
    if (complete) ++res.matched_rows;
    res.rows.push_back(std::move(row));
  }
  return res;
}

void pixel_shift(const Gray8& a, const Gray8& b, int search, int* dx, int* dy) {
  *dx = 0;
  *dy = 0;
  if (a.empty() || b.empty() || a.w != b.w || a.h != b.h) return;
  const Gray8 sa = a.downsample(4);
  const Gray8 sb = b.downsample(4);
  const int s = std::max(1, search / 4);
  double best = 1e300;
  int bx = 0, by = 0;
  for (int oy = -s; oy <= s; ++oy) {
    for (int ox = -s; ox <= s; ++ox) {
      double sad = 0;
      long n = 0;
      for (int y = s; y < sa.h - s; y += 2) {
        const uint8_t* ra = &sa.px[static_cast<size_t>(y) * sa.w];
        const uint8_t* rb = &sb.px[static_cast<size_t>(y + oy) * sb.w];
        for (int x = s; x < sa.w - s; x += 2) {
          sad += std::abs(static_cast<int>(ra[x]) - static_cast<int>(rb[x + ox]));
          ++n;
        }
      }
      if (n > 0) sad /= static_cast<double>(n);
      if (sad < best) {
        best = sad;
        bx = ox;
        by = oy;
      }
    }
  }
  *dx = bx * 4;
  *dy = by * 4;
}

}  // namespace bs
