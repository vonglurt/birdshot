// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/jpeg.hpp"

#include <cmath>
#include <cstdio>
#include <cstring>

#include "birdshot/mathkit.hpp"

namespace bs {

namespace {

// ITU-T T.81 Annex K, Table K.1 -- luminance quantisation, natural order.
const int kQuantLuma[64] = {
    16, 11, 10, 16, 24,  40,  51,  61,  //
    12, 12, 14, 19, 26,  58,  60,  55,  //
    14, 13, 16, 24, 40,  57,  69,  56,  //
    14, 17, 22, 29, 51,  87,  80,  62,  //
    18, 22, 37, 56, 68,  109, 103, 77,  //
    24, 35, 55, 64, 81,  104, 113, 92,  //
    49, 64, 78, 87, 103, 121, 120, 101, //
    72, 92, 95, 98, 112, 100, 103, 99};

const int kZigzag[64] = {
    0,  1,  8,  16, 9,  2,  3,  10, 17, 24, 32, 25, 18, 11, 4,  5,   //
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,  7,  14, 21, 28,  //
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,  //
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63};

// Annex K Huffman tables for luminance DC and AC: bit counts then symbols.
const uint8_t kDcBits[17] = {0, 0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0};
const uint8_t kDcVals[12] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11};
const uint8_t kAcBits[17] = {0, 0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7d};
const uint8_t kAcVals[162] = {
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61,
    0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xa1, 0x08, 0x23, 0x42, 0xb1, 0xc1, 0x15, 0x52,
    0xd1, 0xf0, 0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0a, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x25,
    0x26, 0x27, 0x28, 0x29, 0x2a, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4a, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5a, 0x63, 0x64,
    0x65, 0x66, 0x67, 0x68, 0x69, 0x6a, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7a, 0x83,
    0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8a, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99,
    0x9a, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7, 0xa8, 0xa9, 0xaa, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6,
    0xb7, 0xb8, 0xb9, 0xba, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7, 0xc8, 0xc9, 0xca, 0xd2, 0xd3,
    0xd4, 0xd5, 0xd6, 0xd7, 0xd8, 0xd9, 0xda, 0xe1, 0xe2, 0xe3, 0xe4, 0xe5, 0xe6, 0xe7, 0xe8,
    0xe9, 0xea, 0xf1, 0xf2, 0xf3, 0xf4, 0xf5, 0xf6, 0xf7, 0xf8, 0xf9, 0xfa};

struct HuffCode {
  uint16_t code = 0;
  uint8_t len = 0;
};

// Expand a (bits, vals) table description into per-symbol codes.
void build_codes(const uint8_t bits[17], const uint8_t* vals, size_t nvals, HuffCode out[256]) {
  uint16_t code = 0;
  size_t k = 0;
  for (int len = 1; len <= 16; ++len) {
    for (int i = 0; i < bits[len] && k < nvals; ++i, ++k) {
      out[vals[k]].code = code++;
      out[vals[k]].len = static_cast<uint8_t>(len);
    }
    code = static_cast<uint16_t>(code << 1);
  }
}

struct BitWriter {
  std::vector<uint8_t>& out;
  uint32_t acc = 0;
  int nbits = 0;

  explicit BitWriter(std::vector<uint8_t>& o) : out(o) {}

  void put(uint16_t code, int len) {
    acc = (acc << len) | (code & ((1u << len) - 1));
    nbits += len;
    while (nbits >= 8) {
      const uint8_t b = static_cast<uint8_t>((acc >> (nbits - 8)) & 0xff);
      out.push_back(b);
      if (b == 0xff) out.push_back(0x00);  // byte stuffing
      nbits -= 8;
    }
  }

  void flush() {
    if (nbits > 0) put(static_cast<uint16_t>((1u << (8 - nbits)) - 1), 8 - nbits);
  }
};

void put16(std::vector<uint8_t>& out, uint16_t v) {
  out.push_back(static_cast<uint8_t>(v >> 8));
  out.push_back(static_cast<uint8_t>(v & 0xff));
}

// Forward 8x8 DCT, separable, float. Not the hot path of the application --
// the histogram passes are -- and a plain implementation optimises well.
void fdct8x8(const double in[64], double out[64]) {
  static double cs[8][8];
  static bool init = false;
  if (!init) {
    for (int u = 0; u < 8; ++u)
      for (int x = 0; x < 8; ++x) cs[u][x] = std::cos((2 * x + 1) * u * kPi / 16.0);
    init = true;
  }
  double tmp[64];
  for (int y = 0; y < 8; ++y)
    for (int u = 0; u < 8; ++u) {
      double s = 0;
      for (int x = 0; x < 8; ++x) s += in[y * 8 + x] * cs[u][x];
      tmp[y * 8 + u] = s;
    }
  for (int u = 0; u < 8; ++u)
    for (int v = 0; v < 8; ++v) {
      double s = 0;
      for (int y = 0; y < 8; ++y) s += tmp[y * 8 + u] * cs[v][y];
      const double cu = u == 0 ? 1.0 / std::sqrt(2.0) : 1.0;
      const double cv = v == 0 ? 1.0 / std::sqrt(2.0) : 1.0;
      out[v * 8 + u] = 0.25 * cu * cv * s;
    }
}

int bit_length(int v) {
  int n = 0;
  while (v) {
    ++n;
    v >>= 1;
  }
  return n;
}

}  // namespace

std::vector<uint8_t> encode_jpeg(const Gray8& img, int quality) {
  std::vector<uint8_t> out;
  if (img.empty()) return out;
  quality = clamp(quality, 1, 100);

  // libjpeg's quality->scale mapping, so the numbers users know still apply.
  const int scale = quality < 50 ? 5000 / quality : 200 - quality * 2;
  uint8_t qtab[64];
  for (int i = 0; i < 64; ++i)
    qtab[i] = static_cast<uint8_t>(clamp((kQuantLuma[i] * scale + 50) / 100, 1, 255));

  HuffCode dc[256], ac[256];
  build_codes(kDcBits, kDcVals, sizeof kDcVals, dc);
  build_codes(kAcBits, kAcVals, sizeof kAcVals, ac);

  out.reserve(img.size() / 4 + 1024);

  // SOI + JFIF APP0.
  out.insert(out.end(), {0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 'J', 'F', 'I', 'F', 0x00, 0x01,
                         0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00});

  // DQT (zigzag order on the wire).
  out.push_back(0xff);
  out.push_back(0xdb);
  put16(out, 67);
  out.push_back(0x00);
  for (int i = 0; i < 64; ++i) out.push_back(qtab[kZigzag[i]]);

  // SOF0: baseline, 8-bit, one component, no subsampling.
  out.push_back(0xff);
  out.push_back(0xc0);
  put16(out, 11);
  out.push_back(8);
  put16(out, static_cast<uint16_t>(img.h));
  put16(out, static_cast<uint16_t>(img.w));
  out.push_back(1);
  out.push_back(1);     // component id
  out.push_back(0x11);  // 1x1 sampling
  out.push_back(0);     // quant table 0

  // DHT for the two tables.
  auto emit_dht = [&](uint8_t klass, const uint8_t bits[17], const uint8_t* vals, size_t n) {
    out.push_back(0xff);
    out.push_back(0xc4);
    put16(out, static_cast<uint16_t>(2 + 1 + 16 + n));
    out.push_back(klass);
    for (int i = 1; i <= 16; ++i) out.push_back(bits[i]);
    out.insert(out.end(), vals, vals + n);
  };
  emit_dht(0x00, kDcBits, kDcVals, sizeof kDcVals);
  emit_dht(0x10, kAcBits, kAcVals, sizeof kAcVals);

  // SOS.
  out.insert(out.end(), {0xff, 0xda, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3f, 0x00});

  BitWriter bw(out);
  int prev_dc = 0;
  for (int by = 0; by < img.h; by += 8) {
    for (int bx = 0; bx < img.w; bx += 8) {
      double block[64];
      for (int y = 0; y < 8; ++y) {
        const int sy = by + y < img.h ? by + y : img.h - 1;  // edge replication
        for (int x = 0; x < 8; ++x) {
          const int sx = bx + x < img.w ? bx + x : img.w - 1;
          block[y * 8 + x] = static_cast<double>(img.at(sx, sy)) - 128.0;
        }
      }
      double freq[64];
      fdct8x8(block, freq);

      int q[64];
      for (int i = 0; i < 64; ++i) {
        const int nat = kZigzag[i];
        q[i] = static_cast<int>(std::lround(freq[nat] / qtab[nat]));
      }

      // DC.
      const int diff = q[0] - prev_dc;
      prev_dc = q[0];
      const int dlen = bit_length(diff < 0 ? -diff : diff);
      bw.put(dc[dlen].code, dc[dlen].len);
      if (dlen) {
        const int bits = diff < 0 ? diff + (1 << dlen) - 1 : diff;
        bw.put(static_cast<uint16_t>(bits), dlen);
      }

      // AC with run-length of zeros.
      int run = 0;
      for (int i = 1; i < 64; ++i) {
        if (q[i] == 0) {
          ++run;
          continue;
        }
        while (run > 15) {
          bw.put(ac[0xf0].code, ac[0xf0].len);  // ZRL
          run -= 16;
        }
        const int alen = bit_length(q[i] < 0 ? -q[i] : q[i]);
        const int sym = (run << 4) | alen;
        bw.put(ac[sym].code, ac[sym].len);
        const int bits = q[i] < 0 ? q[i] + (1 << alen) - 1 : q[i];
        bw.put(static_cast<uint16_t>(bits), alen);
        run = 0;
      }
      if (run > 0) bw.put(ac[0x00].code, ac[0x00].len);  // EOB
    }
  }
  bw.flush();

  out.push_back(0xff);
  out.push_back(0xd9);  // EOI
  return out;
}

bool write_jpeg(const Gray8& img, const std::string& path, int quality) {
  const std::vector<uint8_t> data = encode_jpeg(img, quality);
  if (data.empty()) return false;
  std::FILE* f = std::fopen(path.c_str(), "wb");
  if (!f) return false;
  const bool ok = std::fwrite(data.data(), 1, data.size(), f) == data.size();
  return std::fclose(f) == 0 && ok;
}

}  // namespace bs
