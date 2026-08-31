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

namespace bs {
Gray8 letterbox(const Gray8& src, int w, int h) {
  Gray8 out(w, h, 0);
  if (src.empty() || w <= 0 || h <= 0) return out;
  const double scale =
      std::min(static_cast<double>(w) / src.w, static_cast<double>(h) / src.h);
  const int dw = std::max(1, static_cast<int>(src.w * scale));
  const int dh = std::max(1, static_cast<int>(src.h * scale));
  const int ox = (w - dw) / 2, oy = (h - dh) / 2;
  for (int y = 0; y < dh; ++y) {
    const int sy = std::min(src.h - 1, static_cast<int>(y / scale));
    for (int x = 0; x < dw; ++x) {
      const int sx = std::min(src.w - 1, static_cast<int>(x / scale));
      out.at(ox + x, oy + y) = src.at(sx, sy);
    }
  }
  return out;
}
}  // namespace bs

namespace bs {

Gray8 to_luma(const Rgb8& rgb) {
  Gray8 out(rgb.w, rgb.h);
  for (int y = 0; y < rgb.h; ++y)
    for (int x = 0; x < rgb.w; ++x) {
      const uint8_t* p = rgb.at(x, y);
      out.at(x, y) =
          static_cast<uint8_t>((77 * p[0] + 150 * p[1] + 29 * p[2] + 128) >> 8);
    }
  return out;
}

Rgb8 letterbox(const Rgb8& src, int w, int h) {
  Rgb8 out(w, h);
  if (src.empty() || w <= 0 || h <= 0) return out;
  const double scale =
      std::min(static_cast<double>(w) / src.w, static_cast<double>(h) / src.h);
  const int dw = std::max(1, static_cast<int>(src.w * scale));
  const int dh = std::max(1, static_cast<int>(src.h * scale));
  const int ox = (w - dw) / 2, oy = (h - dh) / 2;
  for (int y = 0; y < dh; ++y) {
    const int sy = std::min(src.h - 1, static_cast<int>(y / scale));
    for (int x = 0; x < dw; ++x) {
      const int sx = std::min(src.w - 1, static_cast<int>(x / scale));
      const uint8_t* s = src.at(sx, sy);
      uint8_t* d = out.at(ox + x, oy + y);
      d[0] = s[0];
      d[1] = s[1];
      d[2] = s[2];
    }
  }
  return out;
}

}  // namespace bs
