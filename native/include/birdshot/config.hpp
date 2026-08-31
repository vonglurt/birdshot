// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Persisted settings, calibration set-points and resume state. Same file,
// same keys, same deep-merge-over-defaults behaviour as the 1.x line:
// ~/.config/birdshot/settings.json is shared property, and a native install
// dropped onto a machine that ran the Python line picks up its tuning.
#pragma once

#include <mutex>
#include <string>

#include "birdshot/geo.hpp"
#include "birdshot/json.hpp"

namespace bs {

// Sensor limits for the IMX477 as reported by libcamera on the 1.x unit.
constexpr double kExposureMinUs = 114;
constexpr double kExposureMaxUs = 60000000;
constexpr double kGainMin = 1.0;
constexpr double kGainMax = 22.26;

std::string expand_user(const std::string& path);  // leading ~ -> home
std::string default_config_path();                 // ~/.config/birdshot/settings.json

class Config {
 public:
  explicit Config(std::string path = default_config_path());

  static Json defaults();

  // Typed accessors with the defaults tree as the fallback of last resort.
  double num(const std::string& key, double fallback = 0.0) const;
  bool boolean(const std::string& key, bool fallback = false) const;
  std::string str(const std::string& key, const std::string& fallback = "") const;
  Json get(const std::string& key) const;
  void set(const std::string& key, Json value);

  // The nested resume-state object ("state": {...}).
  Json state(const std::string& key) const;
  void set_state(const std::string& key, Json value);

  Site site() const;  // from the site_* keys

  void load();        // merge file over defaults; missing file is fine
  bool save() const;  // atomic: temp file + rename

  Json snapshot() const;
  const std::string& path() const { return path_; }

 private:
  std::string path_;
  mutable std::mutex mu_;
  Json data_;
};

}  // namespace bs
