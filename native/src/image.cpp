// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/image.hpp"

#include <algorithm>
#include <cstdio>

namespace bs {

Gray8 Gray8::downsample(int factor) const {
  if (factor <= 1 || empty()) return *this;
  Gray8 out(w / factor, h / factor);
  for (int y = 0; y < out.h; ++y) {
    const uint8_t* src = &px[static_cast<size_t>(y) * factor * w];
    uint8_t* dst = &out.px[static_cast<size_t>(y) * out.w];
    for (int x = 0; x < out.w; ++x) dst[x] = src[static_cast<size_t>(x) * factor];
  }
  return out;
}

Gray8 Gray8::centre_crop(int side) const {
  if (empty()) return {};
  const int cw = side < w ? side : w;
  const int ch = side < h ? side : h;
  const int x0 = (w - cw) / 2, y0 = (h - ch) / 2;
  Gray8 out(cw, ch);
  for (int y = 0; y < ch; ++y) {
    const uint8_t* src = &px[static_cast<size_t>(y0 + y) * w + x0];
    std::copy(src, src + cw, &out.px[static_cast<size_t>(y) * cw]);
  }
  return out;
}

bool write_pgm(const Gray8& img, const std::string& path) {
  if (img.empty()) return false;
  std::FILE* f = std::fopen(path.c_str(), "wb");
  if (!f) return false;
  std::fprintf(f, "P5\n%d %d\n255\n", img.w, img.h);
  const bool ok = std::fwrite(img.px.data(), 1, img.px.size(), f) == img.px.size();
  return std::fclose(f) == 0 && ok;
}

bool read_pgm(const std::string& path, Gray8* out) {
  std::FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  int w = 0, h = 0, maxv = 0;
  char magic[3] = {0};
  // Header: magic, then three numbers, comments allowed between tokens.
  if (std::fscanf(f, "%2s", magic) != 1 || magic[0] != 'P' || magic[1] != '5') {
    std::fclose(f);
    return false;
  }
  int vals[3];
  for (int i = 0; i < 3;) {
    int c = std::fgetc(f);
    if (c == '#') {
      while (c != '\n' && c != EOF) c = std::fgetc(f);
    } else if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
      continue;
    } else if (c >= '0' && c <= '9') {
      int v = 0;
      while (c >= '0' && c <= '9') {
        v = v * 10 + (c - '0');
        c = std::fgetc(f);
      }
      vals[i++] = v;
      if (i == 3 && c != EOF) break;  // single whitespace after maxval consumed
    } else {
      std::fclose(f);
      return false;
    }
  }
  w = vals[0];
  h = vals[1];
  maxv = vals[2];
  if (w <= 0 || h <= 0 || maxv != 255 || w > 65535 || h > 65535) {
    std::fclose(f);
    return false;
  }
  Gray8 img(w, h);
  const bool ok = std::fread(img.px.data(), 1, img.px.size(), f) == img.px.size();
  std::fclose(f);
  if (!ok) return false;
  *out = std::move(img);
  return true;
}

}  // namespace bs
