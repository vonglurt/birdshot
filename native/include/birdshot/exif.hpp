// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// EXIF injection, in-tree: an APP1 (TIFF/Exif) segment built by hand and
// spliced into a JPEG without re-encoding -- the same lossless
// preprocessing step the 1.x line ran before assembly. Little-endian
// TIFF, IFD0 for identity, an Exif IFD for the capture parameters, and
// birdshot's own metrics in UserComment.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace bs {

class Config;

struct ExifInfo {
  std::string make;
  std::string model;
  std::string software;
  std::string lens;
  std::string artist;
  std::string copyright;
  double fnumber = 0.0;      // 0 = not recorded
  double focal_mm = 0.0;     // 0 = not recorded
  int64_t exposure_us = 0;   // 0 = not recorded
  double gain = 0.0;         // ISO = gain * 100, the 1.x convention
  double when = 0.0;         // POSIX seconds, centisecond precision kept
  std::string user_comment;  // birdshot metrics, free text
};

// The identity half, from the exif_* config keys.
ExifInfo exif_from_config(const Config& cfg);

// The complete APP1 segment: FF E1, length, "Exif\0\0", TIFF payload.
std::vector<uint8_t> build_exif_app1(const ExifInfo& info);

// Splice the segment into a JPEG in memory, right after SOI. An existing
// APP1 Exif segment is replaced, not duplicated.
std::vector<uint8_t> inject_exif(const std::vector<uint8_t>& jpeg, const ExifInfo& info);

// In place on disk, atomically (temp + rename). False on I/O or parse
// failure; the original file is never left half-written.
bool inject_exif_file(const std::string& path, const ExifInfo& info);

}  // namespace bs
