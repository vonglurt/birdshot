// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/config.hpp"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>

namespace fs = std::filesystem;

namespace bs {

std::string expand_user(const std::string& path) {
  if (path.empty() || path[0] != '~') return path;
  const char* home = std::getenv("HOME");
#ifdef _WIN32
  if (!home) home = std::getenv("USERPROFILE");
#endif
  if (!home) return path;
  if (path.size() == 1) return home;
  if (path[1] == '/' || path[1] == '\\') return std::string(home) + path.substr(1);
  return path;
}

std::string default_config_path() {
  return expand_user("~/.config/birdshot/settings.json");
}

Json Config::defaults() {
  Json d = Json::object();
  d["version"] = 2;

  // ---- backend -------------------------------------------------------
  // "synthetic" is the one backend every platform carries; the platform
  // camera backends register themselves where the OS provides one.
  d["backend"] = "synthetic";
  d["camera_index"] = 0;

  // ---- site (the Horizons keys: where on Earth the camera stands) ----
  // Everything the ephemeris, the planner and multi-day alignment do reads
  // these. Unset (NaN would not survive JSON) is lat/lon 0,0 with set=false.
  d["site_set"] = false;
  d["site_lat"] = 0.0;
  d["site_lon"] = 0.0;
  d["site_elev_m"] = 0.0;
  d["site_name"] = "";
  // Lens geometry for the planner's field-of-view check: a fixed sunset
  // frame must hold the seasonal azimuth swing, and whether it can is pure
  // trigonometry on these two numbers. IMX477 sensor width by default.
  d["lens_focal_mm"] = 6.0;
  d["sensor_width_mm"] = 6.287;

  // ---- storage -------------------------------------------------------
  d["data_root"] = "~/birdshot-data";
  d["min_free_mb"] = 2048;

  // ---- geometry / encode ---------------------------------------------
  d["capture_width"] = 640;
  d["capture_height"] = 480;
  d["jpeg_quality"] = 92;
  d["save_pgm"] = false;  // loss-free sibling saves for the alignment pass

  // ---- exposure ------------------------------------------------------
  d["auto_exposure"] = true;
  d["manual_shutter_us"] = 2000;
  d["manual_gain"] = 1.0;
  d["motion_limit_us"] = 2000;      // 1/500 s: freeze wingbeats
  d["gain_preferred_max"] = 4.0;    // raise gain to here before lengthening shutter
  d["shutter_hard_max_us"] = 33000; // daylight bird work never needs longer
  d["pid_kp"] = 0.55;
  d["pid_ki"] = 0.10;
  d["pid_kd"] = 0.12;
  d["pid_deadband_ev"] = 0.20;
  d["pid_slew_ev"] = 1.5;
  d["pid_integral_clamp_ev"] = 2.0;
  d["ae_average_n"] = 3;
  d["ae_average_mode"] = "median";  // median | mean | none
  d["ae_damping"] = 0.5;
  d["prefer_exposure_time"] = true;

  // ---- metering targets (overwritten by the calibration wizard) -------
  d["target_luma"] = 118.0;
  d["max_clip_frac"] = 0.020;
  d["sky_clip_tolerance"] = 0.60;
  d["sky_zone_frac"] = 0.40;
  d["sky_weight"] = 0.15;
  d["subject_weight"] = 1.0;

  // ---- quality gates -------------------------------------------------
  d["dark_p95_max"] = 40.0;
  d["blown_clip_frac"] = 0.35;
  d["blur_threshold"] = 2.0;
  d["content_std_min"] = 8.0;
  d["reject_action"] = "flag";  // flag | delete | quarantine

  // ---- timelapse / rapid / burst -------------------------------------
  d["timelapse_interval_s"] = 5.0;
  d["timelapse_fps"] = 60;
  d["timelapse_count"] = 0;
  d["burst_count"] = 0;
  d["rapid_count"] = 0;

  // ---- Bird Flight ---------------------------------------------------
  d["bf_burst"] = 5;
  d["bf_cooldown_s"] = 3.0;
  d["bf_takes"] = 0;
  d["bf_min_sharpness"] = 12.0;
  d["bf_min_area_frac"] = 0.0004;
  d["bf_max_area_frac"] = 0.05;
  d["bf_subject_luma_max"] = 80;
  d["bf_sky_luma_min"] = 110;
  d["bf_sky_min_frac"] = 0.5;
  d["bf_ring_sky_frac"] = 0.85;
  d["bf_margin_frac"] = 0.08;
  d["bf_require_motion"] = true;
  d["bf_motion_min"] = 0.0005;

  // ---- calibration (populated by the wizard) -------------------------
  Json cal = Json::object();
  cal["done"] = false;
  cal["timestamp"] = Json();
  cal["sky"] = Json();
  cal["treeline"] = Json();
  cal["subject"] = Json();
  cal["dynamic_range_ev"] = Json();
  d["calibration"] = cal;

  // ---- resume state --------------------------------------------------
  Json st = Json::object();
  st["last_session"] = Json();
  st["last_shutter_us"] = 2000;
  st["last_gain"] = 1.0;
  st["frame_seq"] = 0;
  st["k_lux"] = Json();
  d["state"] = st;

  return d;
}

namespace {

Json deep_merge(const Json& base, const Json& over) {
  if (!base.is_object() || !over.is_object()) return over;
  Json out = base;
  for (const auto& kv : over.obj()) {
    const Json& existing = out.get(kv.first);
    if (kv.second.is_object() && existing.is_object())
      out[kv.first] = deep_merge(existing, kv.second);
    else
      out[kv.first] = kv.second;
  }
  return out;
}

}  // namespace

Config::Config(std::string path) : path_(std::move(path)), data_(defaults()) { load(); }

void Config::load() {
  std::ifstream in(path_, std::ios::binary);
  if (!in) return;
  std::ostringstream ss;
  ss << in.rdbuf();
  std::string err;
  Json stored = Json::parse(ss.str(), &err);
  if (!err.empty() || !stored.is_object()) return;  // corrupt file: keep defaults
  std::lock_guard<std::mutex> lk(mu_);
  data_ = deep_merge(defaults(), stored);
}

bool Config::save() const {
  std::string payload;
  {
    std::lock_guard<std::mutex> lk(mu_);
    payload = data_.dump(2);
  }
  std::error_code ec;
  const fs::path p(path_);
  fs::create_directories(p.parent_path(), ec);
  const fs::path tmp = p.parent_path() / (p.filename().string() + ".tmp");
  {
    std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
    if (!out) return false;
    out << payload << '\n';
    out.flush();
    if (!out) return false;
  }
  fs::rename(tmp, p, ec);
  if (ec) {
    fs::remove(tmp, ec);
    return false;
  }
  return true;
}

double Config::num(const std::string& key, double fallback) const {
  std::lock_guard<std::mutex> lk(mu_);
  const Json& v = data_.get(key);
  return v.is_number() ? v.number() : fallback;
}

bool Config::boolean(const std::string& key, bool fallback) const {
  std::lock_guard<std::mutex> lk(mu_);
  const Json& v = data_.get(key);
  return v.is_null() ? fallback : v.boolean(fallback);
}

std::string Config::str(const std::string& key, const std::string& fallback) const {
  std::lock_guard<std::mutex> lk(mu_);
  return data_.get(key).str_or(fallback);
}

Json Config::get(const std::string& key) const {
  std::lock_guard<std::mutex> lk(mu_);
  return data_.get(key);
}

void Config::set(const std::string& key, Json value) {
  std::lock_guard<std::mutex> lk(mu_);
  data_[key] = std::move(value);
}

Json Config::state(const std::string& key) const {
  std::lock_guard<std::mutex> lk(mu_);
  return data_.get("state").get(key);
}

void Config::set_state(const std::string& key, Json value) {
  std::lock_guard<std::mutex> lk(mu_);
  if (!data_.get("state").is_object()) data_["state"] = Json::object();
  data_["state"][key] = std::move(value);
}

Site Config::site() const {
  Site s;
  s.lat_deg = num("site_lat", 0.0);
  s.lon_deg = num("site_lon", 0.0);
  s.elev_m = num("site_elev_m", 0.0);
  s.name = str("site_name", "");
  if (!boolean("site_set", false)) s.name.clear();
  return s;
}

Json Config::snapshot() const {
  std::lock_guard<std::mutex> lk(mu_);
  return data_;
}

}  // namespace bs
