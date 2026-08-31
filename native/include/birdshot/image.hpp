// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The one image type the whole pipeline shares: an 8-bit luma plane. All
// metering and gating runs on this, exactly as the 1.x line ran on the
// 640x480 lores stream. PGM in/out is the loss-free interchange format the
// alignment pass reads; JPEG (jpeg.hpp) is what sessions ship.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace bs {

struct Gray8 {
  int w = 0;
  int h = 0;
  std::vector<uint8_t> px;  // row-major, w*h

  Gray8() = default;
  Gray8(int width, int height, uint8_t fill = 0)
      : w(width), h(height), px(static_cast<size_t>(width) * static_cast<size_t>(height), fill) {}

  bool empty() const { return w <= 0 || h <= 0 || px.empty(); }
  size_t size() const { return px.size(); }
  uint8_t at(int x, int y) const { return px[static_cast<size_t>(y) * w + x]; }
  uint8_t& at(int x, int y) { return px[static_cast<size_t>(y) * w + x]; }

  // Nearest-neighbour subsample by an integer factor.
  Gray8 downsample(int factor) const;

  // Centre crop of at most side x side native pixels (smaller at the edges).
  Gray8 centre_crop(int side) const;
};

// Binary PGM (P5). Returns false on I/O failure.
bool write_pgm(const Gray8& img, const std::string& path);
bool read_pgm(const std::string& path, Gray8* out);

}  // namespace bs
