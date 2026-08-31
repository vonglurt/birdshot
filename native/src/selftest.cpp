// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The selftest: naming, JSON, the math kit, geodesy, the ephemeris against
// the numbers the Horizons review validated, the exposure ladder, PID
// convergence on a simulated scene, the quality gates, the JPEG encoder,
// storage layout, Bird Flight on frames built in-line, multi-day alignment
// and an end-to-end engine run on the synthetic backend. Run it after any
// change; a machine that cannot run a check skips it, never fails on it.
#include "birdshot/selftest.hpp"

#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <functional>
#include <string>
#include <vector>

#include "birdshot/align.hpp"
#include "birdshot/analysis.hpp"
#include "birdshot/backend.hpp"
#include "birdshot/birdflight.hpp"
#include "birdshot/config.hpp"
#include "birdshot/engine.hpp"
#include "birdshot/exposure.hpp"
#include "birdshot/geo.hpp"
#include "birdshot/image.hpp"
#include "birdshot/jpeg.hpp"
#include "birdshot/json.hpp"
#include "birdshot/mathkit.hpp"
#include "birdshot/naming.hpp"
#include "birdshot/plan.hpp"
#include "birdshot/solar.hpp"
#include "birdshot/storage.hpp"

namespace fs = std::filesystem;

namespace bs {

namespace {

struct Check {
  const char* name;
  std::function<std::string()> fn;  // "" = pass, "SKIP: ..." = skip, else failure text
};

std::string tmp_root() {
  static std::string root;
  if (root.empty()) {
    root = (fs::temp_directory_path() / ("birdshot-selftest-" +
                                         std::to_string(static_cast<long long>(std::time(nullptr)))))
               .string();
    fs::create_directories(root);
  }
  return root;
}

#define EXPECT(cond)                                        \
  do {                                                      \
    if (!(cond)) return std::string("failed: " #cond);      \
  } while (0)

#define EXPECT_NEAR(a, b, tol)                                                     \
  do {                                                                             \
    const double _a = (a), _b = (b);                                               \
    if (std::fabs(_a - _b) > (tol))                                                \
      return std::string("failed: " #a " = ") + std::to_string(_a) +               \
             ", expected " + std::to_string(_b) + " within " + std::to_string(tol); \
  } while (0)

// Unix timestamp for a UTC date, no libc timegm needed (days-from-civil).
double utc_date(int y, int m, int d, double hours = 12.0) {
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = static_cast<unsigned>(y - era * 400);
  const unsigned doy = static_cast<unsigned>((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1);
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  const long long days = static_cast<long long>(era) * 146097 + static_cast<long long>(doe) - 719468;
  return static_cast<double>(days) * 86400.0 + hours * 3600.0;
}

// ---- the checks ---------------------------------------------------------

std::string t_naming_legacy() {
  EXPECT(shutter_dir(19100000) == "s191");
  EXPECT(shutter_dir(100000) == "s01");
  EXPECT(shutter_dir(400000) == "s04");
  EXPECT(shutter_dir(1600000) == "s16");
  EXPECT(shutter_dir(160000) == "ms1600");  // NOT s1.6
  EXPECT(shutter_dir(50000) == "ms500");
  EXPECT(shutter_dir(2000) == "ms20");
  EXPECT(shutter_dir(500) == "us500");
  EXPECT(shutter_dir(2047) == "ms20");  // bucketed to the 100 us grid
  EXPECT(shutter_dir(507) == "us510");  // 10 us grid below 1 ms
  return "";
}

std::string t_naming_parse() {
  for (int64_t us : kPresetShuttersUs) {
    const auto back = parse_shutter_dir(shutter_dir(us));
    EXPECT(back.has_value());
    // Parsing recovers the bucket: within half a grid step of the duration.
    const int64_t grid = us < 1000 ? 10 : 100;
    EXPECT(std::llabs(*back - us) <= grid / 2);
  }
  EXPECT(!parse_shutter_dir("sauto").has_value());
  EXPECT(!parse_shutter_dir("nonsense").has_value());
  return "";
}

std::string t_naming_timestamp() {
  const double now = std::floor(static_cast<double>(std::time(nullptr))) + 0.47;
  const std::string name = timestamp_name(now);
  EXPECT(name.size() == 16);
  const auto back = parse_timestamp_name(name + ".jpg");
  EXPECT(back.has_value());
  EXPECT_NEAR(*back, now, 0.011);
  EXPECT(!parse_timestamp_name("readme.txt").has_value());
  return "";
}

std::string t_json_roundtrip() {
  const std::string src =
      "{\"a\": [1, 2.5, true, null, \"x\\ny\"], \"b\": {\"c\": -3e2}, \"u\": \"\\u00e9\"}";
  std::string err;
  Json v = Json::parse(src, &err);
  EXPECT(err.empty());
  EXPECT(v.get("a").arr().size() == 5);
  EXPECT(v.get("b").get("c").number() == -300.0);
  EXPECT(v.get("u").str() == "\xc3\xa9");
  Json v2 = Json::parse(v.dump(), &err);
  EXPECT(err.empty());
  EXPECT(v == v2);
  Json v3 = Json::parse(v.dump(2), &err);
  EXPECT(err.empty());
  EXPECT(v == v3);
  Json::parse("{broken", &err);
  EXPECT(!err.empty());
  return "";
}

std::string t_hist_stats() {
  Hist256 h;
  std::vector<uint8_t> data;
  for (int i = 0; i < 100; ++i) data.push_back(static_cast<uint8_t>(i));
  h.add(data.data(), data.size());
  const HistStats st = hist_stats(h);
  EXPECT_NEAR(st.mean, 49.5, 0.01);
  EXPECT_NEAR(st.p50, 49.0, 1.0);
  EXPECT_NEAR(st.p95, 94.0, 1.0);
  EXPECT_NEAR(st.stddev, 28.87, 0.1);
  EXPECT(st.clip_hi == 0.0);
  EXPECT_NEAR(st.clip_lo, 0.06, 0.001);  // values 0..5
  return "";
}

std::string t_geo() {
  // London (51.5007N 0.1246W) to the Eiffel Tower (48.8583N 2.2945E):
  // the great circle is ~340.5 km.
  const double d = haversine_m(51.5007, -0.1246, 48.8583, 2.2945);
  EXPECT_NEAR(d / 1000.0, 340.5, 3.0);
  const double br = initial_bearing_deg(51.5007, -0.1246, 48.8583, 2.2945);
  EXPECT_NEAR(br, 148.0, 2.0);
  const Ecef e = to_ecef({0.0, 0.0, 0.0, ""});
  EXPECT_NEAR(e.x, 6378137.0, 1.0);
  EXPECT_NEAR(e.y, 0.0, 1.0);
  double lat = 0, lon = 0;
  EXPECT(parse_latlon("40.7128, -74.0060", &lat, &lon));
  EXPECT_NEAR(lat, 40.7128, 1e-9);
  EXPECT(!parse_latlon("garbage", &lat, &lon));
  return "";
}

std::string t_solar_declination() {
  // June solstice 2026 (June 21): declination at the tropic.
  const SunPos sp = sun_position(utc_date(2026, 6, 21), 40.0, 0.0);
  EXPECT_NEAR(sp.declination_deg, 23.43, 0.1);
  const SunPos sp2 = sun_position(utc_date(2026, 12, 21), 40.0, 0.0);
  EXPECT_NEAR(sp2.declination_deg, -23.43, 0.1);
  return "";
}

std::string t_solar_sunset_altitude() {
  // At the computed sunset instant the sun's centre must sit at -0.833 true.
  const auto sunset = sun_crossing(utc_date(2026, 8, 30), 40.0, -74.0, kAltSunset, false);
  EXPECT(sunset.has_value());
  const SunPos sp = sun_position(*sunset, 40.0, -74.0);
  EXPECT_NEAR(sp.elevation_true_deg, kAltSunset, 0.05);
  return "";
}

std::string t_solar_azimuth_swing() {
  // The Horizons review's validated number: sunset azimuth swings 62.6 deg
  // over the year at 40N (+/- asin(sin 23.44 / cos 40)).
  const auto az_summer = sunset_azimuth_deg(utc_date(2026, 6, 21), 40.0, 0.0);
  const auto az_winter = sunset_azimuth_deg(utc_date(2026, 12, 21), 40.0, 0.0);
  EXPECT(az_summer.has_value() && az_winter.has_value());
  EXPECT_NEAR(*az_summer - *az_winter, 62.6, 1.5);
  // Equinox sunset is due west, give or take declination's daily walk.
  const auto az_equinox = sunset_azimuth_deg(utc_date(2026, 3, 20), 40.0, 0.0);
  EXPECT(az_equinox.has_value());
  EXPECT_NEAR(*az_equinox, 270.0, 1.5);
  return "";
}

std::string t_solar_events_ordered() {
  const double day = utc_date(2026, 8, 30);
  const auto golden = sun_crossing(day, 40.0, -74.0, kAltGoldenHour, false);
  const auto contact = sun_crossing(day, 40.0, -74.0, kAltLowerLimbTouch, false);
  const auto sunset = sun_crossing(day, 40.0, -74.0, kAltSunset, false);
  const auto civil = sun_crossing(day, 40.0, -74.0, kAltCivil, false);
  EXPECT(golden && contact && sunset && civil);
  EXPECT(*golden < *contact && *contact < *sunset && *sunset < *civil);
  // Contact window at 40N: 3.3-3.9 min across the year per the review.
  const double window_min = (*sunset - *contact) / 60.0;
  EXPECT(window_min > 3.0 && window_min < 4.2);
  // Polar night: no sunset at 80N in December.
  EXPECT(!sun_crossing(utc_date(2026, 12, 21), 80.0, 0.0, kAltSunset, false).has_value());
  return "";
}

std::string t_solar_descent_rate() {
  // 15 deg/h * cos(lat) at an equinox sunset (azimuth 270).
  EXPECT_NEAR(std::fabs(altitude_rate_deg_per_hour(40.0, 270.0)), 11.49, 0.05);
  EXPECT(altitude_rate_deg_per_hour(40.0, 270.0) < 0.0);  // setting
  EXPECT(altitude_rate_deg_per_hour(40.0, 90.0) > 0.0);   // rising
  return "";
}

std::string t_exposure_ladder() {
  const auto rung = [](double energy) {
    return allocate(energy, 2000, 4.0, 33000, 114, 60000000, 1.0, 22.26, false);
  };
  // Rung 1: base gain, shutter below the motion limit.
  auto [t1, g1] = rung(1000.0);
  EXPECT(t1 == 1000 && g1 == 1.0);
  // Rung 2: shutter pinned at the motion limit, gain rising.
  auto [t2, g2] = rung(4000.0);
  EXPECT(t2 == 2000);
  EXPECT_NEAR(g2, 2.0, 1e-9);
  // Rung 3: gain pinned at its preferred cap, shutter lengthening.
  auto [t3, g3] = rung(40000.0);
  EXPECT(t3 == 10000 && g3 == 4.0);
  // Rung 4: shutter at the hard cap, gain to the max.
  auto [t4, g4] = rung(400000.0);
  EXPECT(t4 == 33000);
  EXPECT_NEAR(g4, 400000.0 / 33000.0, 1e-6);
  // Exposure-priority spends duration before gain.
  auto [t5, g5] = allocate(20000.0, 2000, 4.0, 33000, 114, 60000000, 1.0, 22.26, true);
  EXPECT(t5 == 20000 && g5 == 1.0);
  return "";
}

FrameStats plain_stats(double meter) {
  FrameStats st;
  st.meter = meter;
  st.subject_clip_hi = 0.0;
  st.sky_clip_hi = 0.0;
  return st;
}

std::string t_pid_converges() {
  Config cfg((fs::path(tmp_root()) / "pid.json").string());
  ExposureController ae(cfg);
  ae.set_limits(SensorLimits{});
  // Simulated scene: meter proportional to energy; correct at 6000 us*gain.
  const double e_correct = 6000.0;
  int64_t us = 500;
  double gain = 1.0;
  double now = 0.0;
  int settled_at = -1;
  for (int i = 0; i < 40; ++i) {
    const double meter = clamp(118.0 * (static_cast<double>(us) * gain) / e_correct, 1.0, 255.0);
    const ExposureDecision d = ae.update(plain_stats(meter), us, gain, 0.0, now);
    us = d.exposure_us;
    gain = d.gain;
    now += 0.25;
    if (d.settled && settled_at < 0) settled_at = i;
  }
  const double final_meter = 118.0 * (static_cast<double>(us) * gain) / e_correct;
  EXPECT_NEAR(std::log2(118.0 / final_meter), 0.0, 0.3);
  EXPECT(settled_at >= 0 && settled_at < 30);
  return "";
}

std::string t_pid_fast_acquire() {
  Config cfg((fs::path(tmp_root()) / "pid2.json").string());
  ExposureController ae(cfg);
  ae.set_limits(SensorLimits{});
  // 3 EV under target: the first correction must jump, not integrate.
  const ExposureDecision d = ae.update(plain_stats(118.0 / 8.0), 2000, 1.0, 0.0, 0.0);
  EXPECT(d.mode == "acquire");
  EXPECT(d.ev_output > 2.5);
  EXPECT(static_cast<double>(d.exposure_us) * d.gain > 2000.0 * 4.0);
  return "";
}

std::string t_highlight_priority() {
  Config cfg((fs::path(tmp_root()) / "pid3.json").string());
  ExposureController ae(cfg);
  ae.set_limits(SensorLimits{});
  // Subject zone clipping over budget, but the frame meters dark: the
  // clipping term must win and pull exposure DOWN. (Clipping heavy enough
  // to exceed the fast-acquire threshold jumps instead, as in 1.x.)
  FrameStats st = plain_stats(60.0);
  st.subject_clip_hi = 0.08;
  const ExposureDecision d = ae.update(st, 8000, 2.0, 0.0, 0.0);
  EXPECT(d.mode == "highlight");
  EXPECT(d.ev_error < 0.0);
  EXPECT(static_cast<double>(d.exposure_us) * d.gain < 8000.0 * 2.0);
  return "";
}

Gray8 textured_frame(int w, int h, uint8_t sky, uint8_t ground, int seed) {
  Gray8 img(w, h);
  const int horizon = static_cast<int>(h * 0.45);
  uint32_t rng = static_cast<uint32_t>(seed) * 2654435761u + 1u;
  for (int y = 0; y < h; ++y)
    for (int x = 0; x < w; ++x) {
      rng = rng * 1664525u + 1013904223u;
      const int noise = static_cast<int>((rng >> 24) & 63) - 32;
      const int base = y < horizon ? sky : ground + noise;
      img.at(x, y) = static_cast<uint8_t>(clamp(base, 0, 255));
    }
  return img;
}

std::string t_gates() {
  Config cfg((fs::path(tmp_root()) / "gates.json").string());
  // A normal scene: bright sky over textured treeline.
  EXPECT(analyse(textured_frame(320, 240, 220, 100, 1), cfg).verdict == "ok");
  // Dark everywhere.
  EXPECT(analyse(textured_frame(320, 240, 20, 10, 2), cfg).verdict == "dark");
  // Subject zone clipped hard.
  Gray8 blown = textured_frame(320, 240, 220, 100, 3);
  for (int y = 240 * 45 / 100; y < 240; ++y)
    for (int x = 0; x < 320; ++x) blown.at(x, y) = 255;
  EXPECT(analyse(blown, cfg).verdict == "blown");
  // Featureless flat grey: nothing carries contrast.
  const Gray8 flat(320, 240, 128);
  EXPECT(analyse(flat, cfg).verdict == "empty");
  // Clipped SKY alone must NOT read as blown -- that is the whole point.
  Gray8 skyclip = textured_frame(320, 240, 255, 100, 4);
  EXPECT(analyse(skyclip, cfg).verdict == "ok");
  return "";
}

std::string t_focus_measures() {
  // A sharp checkerboard must out-score its blurred (flat) counterpart.
  Gray8 sharp(64, 64);
  for (int y = 0; y < 64; ++y)
    for (int x = 0; x < 64; ++x) sharp.at(x, y) = ((x / 4 + y / 4) % 2) ? 220 : 40;
  const Gray8 flat(64, 64, 128);
  EXPECT(laplacian_variance(sharp) > 100.0);
  EXPECT(laplacian_variance(flat) == 0.0);
  EXPECT(tenengrad(sharp) > tenengrad(flat));
  const FocusMap fm = focus_map(sharp);
  EXPECT(fm.best_raw > 0.0);
  return "";
}

std::string t_jpeg_markers() {
  Gray8 img(96, 64);
  for (int y = 0; y < 64; ++y)
    for (int x = 0; x < 96; ++x)
      img.at(x, y) = static_cast<uint8_t>((x * 255) / 95);
  const std::vector<uint8_t> jpg = encode_jpeg(img, 92);
  EXPECT(jpg.size() > 600);  // headers + tables alone are ~600 bytes
  EXPECT(jpg[0] == 0xff && jpg[1] == 0xd8);                          // SOI
  EXPECT(jpg[jpg.size() - 2] == 0xff && jpg[jpg.size() - 1] == 0xd9);  // EOI
  // SOF0 carries the right dimensions.
  bool sof_ok = false;
  for (size_t i = 2; i + 9 < jpg.size(); ++i) {
    if (jpg[i] == 0xff && jpg[i + 1] == 0xc0) {
      const int h = (jpg[i + 5] << 8) | jpg[i + 6];
      const int w = (jpg[i + 7] << 8) | jpg[i + 8];
      sof_ok = (h == 64 && w == 96);
      break;
    }
  }
  EXPECT(sof_ok);
  // Higher quality must not produce a smaller file on this gradient.
  EXPECT(encode_jpeg(img, 95).size() >= encode_jpeg(img, 30).size());
  return "";
}

std::string t_pgm_roundtrip() {
  const Gray8 img = textured_frame(80, 60, 200, 90, 7);
  const std::string path = (fs::path(tmp_root()) / "roundtrip.pgm").string();
  EXPECT(write_pgm(img, path));
  Gray8 back;
  EXPECT(read_pgm(path, &back));
  EXPECT(back.w == img.w && back.h == img.h && back.px == img.px);
  return "";
}

std::string t_storage_layout() {
  const std::string root = (fs::path(tmp_root()) / "data").string();
  fs::create_directories(root);
  Session s = Session::create(root, "sess");
  EXPECT(s.valid());
  EXPECT(s.name().rfind("sess-", 0) == 0);

  // Two frames in the same centisecond: the second must claim a suffix.
  const double when = static_cast<double>(std::time(nullptr));
  const std::string p1 = s.claim_frame(when, 2000, ".jpg");
  const std::string p2 = s.claim_frame(when, 2000, ".jpg");
  EXPECT(!p1.empty() && !p2.empty() && p1 != p2);
  EXPECT(p2.find("_001") != std::string::npos);
  EXPECT(p1.find("ms20") != std::string::npos);  // the shutter bucket

  // .part commit renames onto the claimed name.
  const char payload[] = "not really a jpeg";
  std::FILE* f = std::fopen(p1.c_str(), "wb");
  EXPECT(f != nullptr);
  std::fwrite(payload, 1, sizeof payload, f);
  std::fclose(f);
  EXPECT(Session::commit_frame(p1));
  EXPECT(fs::exists(p1.substr(0, p1.size() - 5)));

  Json rec = Json::object();
  rec["name"] = "test";
  rec["verdict"] = "ok";
  EXPECT(s.append_index(rec));
  EXPECT(s.read_index().size() == 1);
  Json sum = Json::object();
  sum["frames"] = 2;
  EXPECT(s.close(sum));
  EXPECT(fs::exists(fs::path(s.dir()) / "session.json"));
  EXPECT(list_sessions(root).size() == 1);
  return "";
}

std::string t_config_persistence() {
  const std::string path = (fs::path(tmp_root()) / "cfg" / "settings.json").string();
  {
    Config cfg(path);
    EXPECT(cfg.num("target_luma", 0) == 118.0);
    cfg.set("target_luma", Json(100.0));
    cfg.set_state("k_lux", Json(123.4));
    EXPECT(cfg.save());
  }
  {
    Config cfg(path);
    EXPECT(cfg.num("target_luma", 0) == 100.0);          // persisted override
    EXPECT(cfg.num("pid_kp", 0) == 0.55);                // default fills in
    EXPECT(cfg.state("k_lux").number() == 123.4);        // nested state merged
  }
  return "";
}

Gray8 bird_frame(int bird_x, int bird_y) {
  Gray8 img(640, 480, 200);  // all sky
  for (int y = bird_y; y < bird_y + 24; ++y)
    for (int x = bird_x; x < bird_x + 48; ++x)
      if (y >= 0 && y < 480 && x >= 0 && x < 640) img.at(x, y) = 30;
  return img;
}

std::string t_birdflight_take() {
  Config cfg((fs::path(tmp_root()) / "bf.json").string());
  BirdFlightDetector det(cfg);
  det.update(bird_frame(200, 200));           // prime the motion gate
  const Sighting s = det.update(bird_frame(232, 200));  // moved: motion fires
  if (!s.take) {
    std::string why = "no take:";
    for (const auto& r : s.reasons) why += " [" + r + "]";
    return why;
  }
  EXPECT(s.present);
  EXPECT(s.area_frac > 0.0004 && s.area_frac < 0.05);
  EXPECT(s.ring_sky_frac > 0.85);
  return "";
}

std::string t_birdflight_rejects() {
  Config cfg((fs::path(tmp_root()) / "bf2.json").string());
  {
    // Subject touching the border is never the subject.
    BirdFlightDetector det(cfg);
    det.update(bird_frame(0, 200));
    const Sighting s = det.update(bird_frame(0, 200));
    EXPECT(!s.take);
    bool no_subject = false;
    for (const auto& r : s.reasons) no_subject = no_subject || r == "no subject";
    EXPECT(no_subject);
  }
  {
    // A static scene fails the motion gate.
    BirdFlightDetector det(cfg);
    det.update(bird_frame(200, 200));
    const Sighting s = det.update(bird_frame(200, 200));
    EXPECT(!s.take);
    bool no_motion = false;
    for (const auto& r : s.reasons) no_motion = no_motion || r == "no motion";
    EXPECT(no_motion);
  }
  return "";
}

std::string t_plan() {
  Site site{40.0, -74.0, 10.0, "test site"};
  const ShootPlan plan = make_plan(site, utc_date(2026, 8, 30), 7, 6.0, 6.287);
  EXPECT(plan.days.size() == 7);
  for (const auto& d : plan.days) EXPECT(d.sun_sets);
  // Late August at 40N: sunset walks south ~0.4 deg/day; each day earlier.
  EXPECT(plan.days[1].azimuth_drift_deg < 0.0);
  EXPECT(plan.days[1].sunset_unix < plan.days[0].sunset_unix + 86400.0);
  EXPECT(plan.days[0].contact_window_min > 3.0 && plan.days[0].contact_window_min < 4.2);
  EXPECT_NEAR(plan.lens_fov_deg, 55.4, 0.5);  // the review's 6mm number
  EXPECT(!format_plan(plan).empty());
  return "";
}

std::string t_align_days() {
  const Site site{40.0, -74.0, 0.0, ""};
  const std::string root = (fs::path(tmp_root()) / "align").string();
  fs::create_directories(root);
  // Two consecutive evenings, frames at matching offsets before each sunset.
  int made = 0;
  for (int day = 0; day < 2; ++day) {
    const auto sunset =
        sun_crossing(utc_date(2026, 8, 30) + day * 86400.0, site.lat_deg, site.lon_deg,
                     kAltSunset, false);
    EXPECT(sunset.has_value());
    for (int k = 0; k < 3; ++k) {
      const double ts = *sunset - (30.0 - 10.0 * k) * 60.0;
      const std::string name = timestamp_name(ts) + ".jpg";
      std::FILE* f = std::fopen((fs::path(root) / name).string().c_str(), "wb");
      EXPECT(f != nullptr);
      std::fclose(f);
      ++made;
    }
  }
  EXPECT(made == 6);
  const AlignResult res = align_days({root}, site, 0.25);
  if (!res.error.empty()) return "align error: " + res.error;
  EXPECT(res.days.size() == 2);
  EXPECT(res.frames.size() == 6);
  // Ten minutes apart at ~10 deg/h descent is ~1.7 deg of elevation between
  // frames; a 0.25 deg tolerance must pair like with like, all three rows.
  EXPECT(res.matched_rows == 3);
  return "";
}

std::string t_pixel_shift() {
  const Gray8 a = textured_frame(320, 240, 220, 100, 9);
  Gray8 b(320, 240, 0);
  const int sx = 8, sy = 4;
  for (int y = 0; y < 240; ++y)
    for (int x = 0; x < 320; ++x) {
      const int ox = clamp(x - sx, 0, 319), oy = clamp(y - sy, 0, 239);
      b.at(x, y) = a.at(ox, oy);
    }
  int dx = 0, dy = 0;
  pixel_shift(a, b, 16, &dx, &dy);
  EXPECT_NEAR(dx, sx, 4.0);
  EXPECT_NEAR(dy, sy, 4.0);
  return "";
}

std::string t_engine_collect() {
  const std::string cfg_path = (fs::path(tmp_root()) / "engine" / "settings.json").string();
  Config cfg(cfg_path);
  cfg.set("data_root", Json((fs::path(tmp_root()) / "engine" / "data").string()));
  cfg.set("min_free_mb", Json(1.0));
  auto backend = make_backend(cfg);
  Engine engine(cfg, *backend);
  EngineOptions opts;
  opts.mode = Mode::Collect;
  opts.count = 12;
  const EngineReport rep = engine.run(opts);
  EXPECT(rep.clean);
  EXPECT(rep.frames == 12);
  EXPECT(rep.saved > 0);
  Session s = Session::open(rep.session_dir);
  EXPECT(s.read_index().size() == 12);
  EXPECT(fs::exists(fs::path(rep.session_dir) / "session.json"));
  // COLLECT frames land in shutter buckets, not flat.
  bool bucket_seen = false;
  for (const auto& e : fs::directory_iterator(rep.session_dir))
    if (e.is_directory() && parse_shutter_dir(e.path().filename().string())) bucket_seen = true;
  EXPECT(bucket_seen);
  return "";
}

std::string t_engine_rapid() {
  const std::string cfg_path = (fs::path(tmp_root()) / "engine2" / "settings.json").string();
  Config cfg(cfg_path);
  cfg.set("data_root", Json((fs::path(tmp_root()) / "engine2" / "data").string()));
  cfg.set("min_free_mb", Json(1.0));
  auto backend = make_backend(cfg);
  Engine engine(cfg, *backend);
  EngineOptions opts;
  opts.mode = Mode::Rapid;
  opts.count = 20;
  const EngineReport rep = engine.run(opts);
  EXPECT(rep.clean);
  EXPECT(rep.frames == 20);
  EXPECT(rep.saved == 20);
  // Flat names: files sit directly in the session dir.
  int flat = 0;
  for (const auto& e : fs::directory_iterator(rep.session_dir))
    if (e.is_regular_file() && e.path().extension() == ".jpg") ++flat;
  EXPECT(flat == 20);
  EXPECT(rep.fps > 5.0);  // native rewrite exists for speed; hold a floor
  return "";
}

std::string t_engine_ae_converges() {
  const std::string cfg_path = (fs::path(tmp_root()) / "engine3" / "settings.json").string();
  Config cfg(cfg_path);
  cfg.set("data_root", Json((fs::path(tmp_root()) / "engine3" / "data").string()));
  cfg.set("min_free_mb", Json(1.0));
  auto backend = make_backend(cfg);
  Engine engine(cfg, *backend);
  EngineOptions opts;
  opts.mode = Mode::Collect;
  opts.count = 30;
  const EngineReport rep = engine.run(opts);
  EXPECT(rep.clean);
  // After 30 frames against the synthetic sky the subject zone must sit
  // near the metering target: read the last index line back.
  Session s = Session::open(rep.session_dir);
  const auto index = s.read_index();
  EXPECT(index.size() == 30);
  const double final_meter = index.back().get("meter").number();
  const double target = cfg.num("target_luma", 118.0);
  EXPECT_NEAR(std::log2(final_meter / target), 0.0, 0.45);
  return "";
}

}  // namespace

int run_selftest(bool verbose) {
  (void)verbose;
  const std::vector<Check> checks = {
      {"naming: legacy folder names", t_naming_legacy},
      {"naming: dir round-trips", t_naming_parse},
      {"naming: centisecond timestamps", t_naming_timestamp},
      {"json: round-trips", t_json_roundtrip},
      {"mathkit: histogram statistics", t_hist_stats},
      {"geo: distances and bearings", t_geo},
      {"solar: solstice declination", t_solar_declination},
      {"solar: sunset altitude", t_solar_sunset_altitude},
      {"solar: azimuth swing (62.6 deg at 40N)", t_solar_azimuth_swing},
      {"solar: evening events ordered", t_solar_events_ordered},
      {"solar: descent rate", t_solar_descent_rate},
      {"exposure: the shutter/gain ladder", t_exposure_ladder},
      {"exposure: PID converges", t_pid_converges},
      {"exposure: fast acquire jumps", t_pid_fast_acquire},
      {"exposure: highlight priority wins", t_highlight_priority},
      {"gates: dark / blown / empty / ok", t_gates},
      {"gates: focus measures rank", t_focus_measures},
      {"jpeg: valid baseline stream", t_jpeg_markers},
      {"image: pgm round-trips", t_pgm_roundtrip},
      {"storage: layout and O_EXCL claims", t_storage_layout},
      {"config: persistence and merge", t_config_persistence},
      {"birdflight: a bird fires a take", t_birdflight_take},
      {"birdflight: gates hold fire", t_birdflight_rejects},
      {"plan: a week at 40N", t_plan},
      {"align: two evenings pair up", t_align_days},
      {"align: pixel shift recovered", t_pixel_shift},
      {"engine: COLLECT end to end", t_engine_collect},
      {"engine: RAPID end to end", t_engine_rapid},
      {"engine: AE converges on the synthetic sky", t_engine_ae_converges},
  };

  int failures = 0, skips = 0;
  for (const auto& c : checks) {
    std::string result;
    try {
      result = c.fn();
    } catch (const std::exception& e) {
      result = std::string("exception: ") + e.what();
    }
    if (result.empty()) {
      std::printf("  ok    %s\n", c.name);
    } else if (result.rfind("SKIP", 0) == 0) {
      ++skips;
      std::printf("  skip  %s -- %s\n", c.name, result.c_str());
    } else {
      ++failures;
      std::printf("  FAIL  %s\n        %s\n", c.name, result.c_str());
    }
  }
  std::printf("selftest: %zu checks, %d failed, %d skipped\n", checks.size(), failures, skips);

  // Leave nothing behind on success; keep the evidence on failure.
  if (failures == 0) {
    std::error_code ec;
    fs::remove_all(tmp_root(), ec);
  } else {
    std::printf("artifacts kept in %s\n", tmp_root().c_str());
  }
  return failures;
}

}  // namespace bs
