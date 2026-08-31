// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Multi-day alignment. Frames from different evenings never line up by
// clock time -- sunset walks earlier or later every day -- but they DO line
// up by where the sun actually was. Each frame's timestamp name plus the
// site gives its solar elevation and azimuth; matching frames across days
// by elevation (same phase of the descent) yields the sequences a
// day-over-day blend or a season timelapse wants. A second, optional pass
// refines the pairing per-pixel from the loss-free PGM siblings.
#pragma once

#include <string>
#include <vector>

#include "birdshot/geo.hpp"
#include "birdshot/image.hpp"
#include "birdshot/json.hpp"

namespace bs {

struct AlignFrame {
  std::string path;
  double ts = 0;        // from the YYYYMMDDHHMMSScc name
  double elev_deg = 0;  // solar, apparent
  double az_deg = 0;
  int day = 0;          // index into AlignResult::days
};

struct AlignRow {
  double elev_deg = 0;                 // the elevation this row aligns on
  std::vector<int> frame_of_day;       // index into frames, -1 = no match that day
};

struct AlignResult {
  std::vector<std::string> days;       // "YYYY-MM-DD" local, sorted
  std::vector<AlignFrame> frames;
  std::vector<AlignRow> rows;
  int matched_rows = 0;                // rows with a frame on every day
  std::string error;                   // non-empty = nothing usable found

  Json to_json() const;
  std::string summary() const;
};

// Scan session directories (or bare folders of frames) for timestamp-named
// images and align them across the days they span. `tolerance_deg` is the
// largest elevation mismatch that still counts as the same moment.
AlignResult align_days(const std::vector<std::string>& dirs, const Site& site,
                       double tolerance_deg = 0.25, bool evenings_only = true);

// Integer pixel offset (dx, dy) that best maps `b` onto `a`, by SAD over a
// +/- `search` px window on 4x-downsampled planes. For deghosting stacked
// evenings once the solar match has picked the frames.
void pixel_shift(const Gray8& a, const Gray8& b, int search, int* dx, int* dy);

}  // namespace bs
