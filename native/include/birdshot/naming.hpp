// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Shutter-duration folder naming and centisecond frame filenames, identical
// to the 1.x line and the runCam.sh convention before it: s<N> is tenths of
// a second, ms<N> tenths of a millisecond, us<N> plain microseconds, and
// frame names are YYYYMMDDHHMMSScc in local time. A native session must sort
// and read alongside a decade of existing folders.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace bs {

// YYYYMMDDHHMMSScc for a POSIX timestamp, local time. 16 digits.
std::string timestamp_name(double when);

// Inverse; ignores any suffix or extension. Empty when the name does not
// start with 16 digits or the digits are not a real date.
std::optional<double> parse_timestamp_name(const std::string& name);

// Bucket directory for a shutter duration in microseconds.
std::string shutter_dir(int64_t exposure_us);

// Inverse; nullopt for "sauto" and anything else non-conforming.
std::optional<int64_t> parse_shutter_dir(const std::string& name);

// Human-readable shutter, e.g. "1/500 s (2.0 ms)".
std::string describe_shutter(int64_t exposure_us);

// One-click shutter presets, microseconds; fast end for wingbeats, slow end
// preserves the legacy night-sky durations (s191).
extern const std::vector<int64_t> kPresetShuttersUs;

}  // namespace bs
