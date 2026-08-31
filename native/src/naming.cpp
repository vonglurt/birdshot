// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/naming.hpp"

#include <cmath>
#include <cstdio>
#include <ctime>

namespace bs {

namespace {

constexpr int64_t kDeciUs = 100000;   // one tenth of a second, the "s" unit
constexpr int64_t kMsUnitUs = 100;    // the "ms" unit, one tenth of a millisecond
constexpr int64_t kUsBucket = 10;     // rounding grid below 1 ms

void local_tm(time_t t, std::tm* out) {
#ifdef _WIN32
  localtime_s(out, &t);
#else
  localtime_r(&t, out);
#endif
}

}  // namespace

std::string timestamp_name(double when) {
  const time_t sec = static_cast<time_t>(std::floor(when));
  std::tm lt{};
  local_tm(sec, &lt);
  const double frac = when - std::floor(when);
  const int centis = lt.tm_sec * 100 + static_cast<int>(frac * 100.0);
  char buf[24];
  std::snprintf(buf, sizeof buf, "%04d%02d%02d%02d%02d%04d", lt.tm_year + 1900,
                lt.tm_mon + 1, lt.tm_mday, lt.tm_hour, lt.tm_min, centis);
  return buf;
}

std::optional<double> parse_timestamp_name(const std::string& name) {
  if (name.size() < 16) return std::nullopt;
  for (int i = 0; i < 16; ++i)
    if (name[static_cast<size_t>(i)] < '0' || name[static_cast<size_t>(i)] > '9')
      return std::nullopt;
  auto num = [&](int off, int len) {
    int v = 0;
    for (int i = off; i < off + len; ++i) v = v * 10 + (name[static_cast<size_t>(i)] - '0');
    return v;
  };
  std::tm tm{};
  tm.tm_year = num(0, 4) - 1900;
  tm.tm_mon = num(4, 2) - 1;
  tm.tm_mday = num(6, 2);
  tm.tm_hour = num(8, 2);
  tm.tm_min = num(10, 2);
  tm.tm_sec = 0;
  tm.tm_isdst = -1;
  if (tm.tm_mon < 0 || tm.tm_mon > 11 || tm.tm_mday < 1 || tm.tm_mday > 31 ||
      tm.tm_hour > 23 || tm.tm_min > 59)
    return std::nullopt;
  const int centis = num(12, 4);
  if (centis > 5999) return std::nullopt;
  const time_t base = std::mktime(&tm);
  if (base == static_cast<time_t>(-1)) return std::nullopt;
  return static_cast<double>(base) + centis / 100.0;
}

std::string shutter_dir(int64_t exposure_us) {
  char buf[24];
  if (exposure_us >= kDeciUs && exposure_us % kDeciUs == 0) {
    std::snprintf(buf, sizeof buf, "s%02lld", static_cast<long long>(exposure_us / kDeciUs));
  } else if (exposure_us >= 1000) {
    const long long n = static_cast<long long>(
        std::llround(static_cast<double>(exposure_us) / static_cast<double>(kMsUnitUs)));
    std::snprintf(buf, sizeof buf, "ms%lld", n);
  } else {
    const long long n = static_cast<long long>(std::llround(
                            static_cast<double>(exposure_us) / static_cast<double>(kUsBucket))) *
                        kUsBucket;
    std::snprintf(buf, sizeof buf, "us%lld", n);
  }
  return buf;
}

std::optional<int64_t> parse_shutter_dir(const std::string& name) {
  size_t i = 0;
  while (i < name.size() && (name[i] == ' ' || name[i] == '\t')) ++i;
  size_t end = name.size();
  while (end > i && (name[end - 1] == ' ' || name[end - 1] == '\t')) --end;
  const std::string s = name.substr(i, end - i);

  int64_t unit;
  size_t off;
  if (s.rfind("ms", 0) == 0) { unit = kMsUnitUs; off = 2; }
  else if (s.rfind("us", 0) == 0) { unit = 1; off = 2; }
  else if (s.rfind("s", 0) == 0) { unit = kDeciUs; off = 1; }
  else return std::nullopt;

  if (off >= s.size()) return std::nullopt;
  int64_t digits = 0;
  for (size_t k = off; k < s.size(); ++k) {
    if (s[k] < '0' || s[k] > '9') return std::nullopt;
    digits = digits * 10 + (s[k] - '0');
  }
  return digits * unit;
}

std::string describe_shutter(int64_t exposure_us) {
  char buf[64];
  const double seconds = static_cast<double>(exposure_us) / 1e6;
  if (seconds >= 1.0) {
    std::snprintf(buf, sizeof buf, "%.1f s", seconds);
  } else if (exposure_us >= 1000) {
    std::snprintf(buf, sizeof buf, "1/%lld s (%.1f ms)",
                  static_cast<long long>(std::llround(1.0 / seconds)),
                  static_cast<double>(exposure_us) / 1000.0);
  } else {
    std::snprintf(buf, sizeof buf, "1/%lld s (%lld us)",
                  seconds > 0 ? static_cast<long long>(std::llround(1.0 / seconds)) : 0LL,
                  static_cast<long long>(exposure_us));
  }
  return buf;
}

const std::vector<int64_t> kPresetShuttersUs = {
    125,        // ~1/8000 - clipping-bright sky
    250,        //
    500,        // 1/2000 - fast wingbeat freeze
    1000,       // 1/1000
    2000,       // 1/500  - default motion limit for birds
    4000,       // 1/250
    8000,       // 1/125
    16000,      // 1/60
    33000,      // 1/30
    100000,     // 0.1 s   (s01)
    400000,     // 0.4 s   (s04)
    1600000,    // 1.6 s   (s16)
    6400000,    // 6.4 s   (s64)
    19100000,   // 19.1 s  (s191, the legacy night setting)
};

}  // namespace bs
