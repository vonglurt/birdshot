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

// ---------------------------------------------------------- the decoder --
// Baseline sequential only. The encoder above writes grayscale; cameras
// and phones write 3-component 4:2:0 -- both land here when a replay
// folder is pointed at them. Chroma blocks are entropy-decoded to keep
// the bitstream honest, then discarded.

namespace bs {

namespace {

struct HuffTable {
  // Canonical code table, decoded by walking code lengths.
  uint8_t counts[17] = {0};   // counts[len] for len 1..16
  std::vector<uint8_t> symbols;
  int32_t mincode[17] = {0};
  int32_t maxcode[17] = {0};
  int32_t valptr[17] = {0};
  bool present = false;

  void finish() {
    int32_t code = 0, k = 0;
    for (int len = 1; len <= 16; ++len) {
      valptr[len] = k;
      mincode[len] = code;
      code += counts[len];
      k += counts[len];
      maxcode[len] = counts[len] ? code - 1 : -1;
      code <<= 1;
    }
    present = true;
  }
};

struct BitReader {
  const uint8_t* p;
  const uint8_t* end;
  uint32_t bits = 0;
  int nbits = 0;
  bool bad = false;

  BitReader(const uint8_t* data, const uint8_t* stop) : p(data), end(stop) {}

  // Refill honoring byte stuffing; a marker mid-entropy is an error the
  // caller surfaces as a failed decode.
  void need(int n) {
    while (nbits < n) {
      if (p >= end) { bad = true; bits <<= 8; nbits += 8; continue; }
      uint8_t byte = *p++;
      if (byte == 0xFF) {
        if (p < end && *p == 0x00) { ++p; }
        else { bad = true; --p; byte = 0; }
      }
      bits = (bits << 8) | byte;
      nbits += 8;
    }
  }
  int get(int n) {
    if (n == 0) return 0;
    need(n);
    const int v = static_cast<int>((bits >> (nbits - n)) & ((1u << n) - 1));
    nbits -= n;
    return v;
  }
  int decode(const HuffTable& t) {
    int32_t code = get(1);
    for (int len = 1; len <= 16; ++len) {
      if (t.maxcode[len] >= 0 && code <= t.maxcode[len])
        return t.symbols[t.valptr[len] + code - t.mincode[len]];
      code = (code << 1) | get(1);
    }
    bad = true;
    return 0;
  }
  void align() { nbits = 0; bits = 0; }
};

int extend(int v, int n) { return v < (1 << (n - 1)) ? v - (1 << n) + 1 : v; }

// Separable float IDCT with the level shift folded in at the end.
void idct8x8(const int32_t block[64], const uint16_t quant[64], uint8_t* out, int stride) {
  static const int kZigzag[64] = {
      0,  1,  8,  16, 9,  2,  3,  10, 17, 24, 32, 25, 18, 11, 4,  5,
      12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,  7,  14, 21, 28,
      35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
      58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63};
  static float cs[8][8];
  static bool init = false;
  if (!init) {
    for (int u = 0; u < 8; ++u)
      for (int x = 0; x < 8; ++x)
        cs[u][x] = static_cast<float>((u == 0 ? 0.353553390593 : 0.5) *
                                      std::cos((2 * x + 1) * u * 3.14159265358979 / 16.0));
    init = true;
  }
  float coeff[64];
  for (int i = 0; i < 64; ++i) coeff[kZigzag[i]] = static_cast<float>(block[i] * quant[i]);
  float tmp[64];
  for (int y = 0; y < 8; ++y)
    for (int x = 0; x < 8; ++x) {
      float acc = 0;
      for (int u = 0; u < 8; ++u) acc += cs[u][x] * coeff[y * 8 + u];
      tmp[y * 8 + x] = acc;
    }
  for (int x = 0; x < 8; ++x)
    for (int y = 0; y < 8; ++y) {
      float acc = 0;
      for (int v = 0; v < 8; ++v) acc += cs[v][y] * tmp[v * 8 + x];
      const int px = static_cast<int>(std::lround(acc + 128.0f));
      out[y * stride + x] = static_cast<uint8_t>(px < 0 ? 0 : px > 255 ? 255 : px);
    }
}

struct Component {
  int id = 0, h = 1, v = 1, tq = 0, td = 0, ta = 0;
  int pred = 0;
};

}  // namespace

bool decode_jpeg(const std::vector<uint8_t>& data, Gray8* out) {
  if (!out || data.size() < 4 || data[0] != 0xFF || data[1] != 0xD8) return false;

  uint16_t quant[4][64] = {};
  HuffTable dc[4], ac[4];
  Component comps[3];
  int ncomp = 0, width = 0, height = 0, restart = 0;
  size_t i = 2;

  auto u16 = [&](size_t at) {
    return (static_cast<int>(data[at]) << 8) | data[at + 1];
  };

  while (i + 4 <= data.size()) {
    if (data[i] != 0xFF) return false;
    const uint8_t marker = data[i + 1];
    if (marker == 0xD8) { i += 2; continue; }
    const size_t len = static_cast<size_t>(u16(i + 2));
    if (i + 2 + len > data.size()) return false;
    const size_t seg = i + 4;

    if (marker == 0xDB) {  // DQT
      size_t at = seg;
      while (at < i + 2 + len) {
        const int prec = data[at] >> 4, id = data[at] & 15;
        if (id > 3) return false;
        ++at;
        for (int k = 0; k < 64; ++k) {
          quant[id][k] = prec ? static_cast<uint16_t>(u16(at)) : data[at];
          at += prec ? 2 : 1;
        }
      }
    } else if (marker == 0xC4) {  // DHT
      size_t at = seg;
      while (at < i + 2 + len) {
        const int cls = data[at] >> 4, id = data[at] & 15;
        if (id > 3) return false;
        ++at;
        HuffTable& t = cls ? ac[id] : dc[id];
        t = HuffTable();
        int total = 0;
        for (int len2 = 1; len2 <= 16; ++len2) {
          t.counts[len2] = data[at++];
          total += t.counts[len2];
        }
        t.symbols.assign(data.begin() + at, data.begin() + at + total);
        at += total;
        t.finish();
      }
    } else if (marker == 0xC0 || marker == 0xC1) {  // SOF0/1 baseline
      height = u16(seg + 1);
      width = u16(seg + 3);
      ncomp = data[seg + 5];
      if (ncomp != 1 && ncomp != 3) return false;
      for (int c = 0; c < ncomp; ++c) {
        comps[c].id = data[seg + 6 + c * 3];
        comps[c].h = data[seg + 7 + c * 3] >> 4;
        comps[c].v = data[seg + 7 + c * 3] & 15;
        comps[c].tq = data[seg + 8 + c * 3];
        if (comps[c].h < 1 || comps[c].h > 4 || comps[c].v < 1 || comps[c].v > 4) return false;
      }
    } else if (marker == 0xC2) {
      return false;  // progressive: not this decoder's job
    } else if (marker == 0xDD) {  // DRI
      restart = u16(seg);
    } else if (marker == 0xDA) {  // SOS -- the scan
      const int ns = data[seg];
      if (ns != ncomp || width <= 0 || height <= 0) return false;
      for (int c = 0; c < ns; ++c) {
        const int id = data[seg + 1 + c * 2];
        for (int k = 0; k < ncomp; ++k)
          if (comps[k].id == id) {
            comps[k].td = data[seg + 2 + c * 2] >> 4;
            comps[k].ta = data[seg + 2 + c * 2] & 15;
          }
      }
      int hmax = 1, vmax = 1;
      for (int c = 0; c < ncomp; ++c) {
        hmax = std::max(hmax, comps[c].h);
        vmax = std::max(vmax, comps[c].v);
      }
      const int mcux = (width + 8 * hmax - 1) / (8 * hmax);
      const int mcuy = (height + 8 * vmax - 1) / (8 * vmax);

      // Luma plane, MCU-padded; cropped at the end.
      const int lw = mcux * 8 * comps[0].h, lh = mcuy * 8 * comps[0].v;
      std::vector<uint8_t> luma(static_cast<size_t>(lw) * lh, 0);

      BitReader br(data.data() + i + 2 + len, data.data() + data.size());
      for (int c = 0; c < ncomp; ++c) comps[c].pred = 0;
      int until_restart = restart;

      for (int my = 0; my < mcuy; ++my) {
        for (int mx = 0; mx < mcux; ++mx) {
          for (int c = 0; c < ncomp; ++c) {
            Component& comp = comps[c];
            if (!dc[comp.td].present || !ac[comp.ta].present) return false;
            for (int by = 0; by < comp.v; ++by) {
              for (int bx = 0; bx < comp.h; ++bx) {
                int32_t block[64] = {0};
                const int s = br.decode(dc[comp.td]);
                const int diff = s ? extend(br.get(s), s) : 0;
                comp.pred += diff;
                block[0] = comp.pred;
                for (int k = 1; k < 64;) {
                  const int rs = br.decode(ac[comp.ta]);
                  const int r = rs >> 4, size = rs & 15;
                  if (size == 0) {
                    if (r != 15) break;  // EOB unless ZRL
                    k += 16;
                    continue;
                  }
                  k += r;
                  if (k > 63) break;
                  block[k++] = extend(br.get(size), size);
                }
                if (br.bad) return false;
                if (c == 0) {
                  const int px = (mx * comp.h + bx) * 8, py = (my * comp.v + by) * 8;
                  idct8x8(block, quant[comp.tq], &luma[static_cast<size_t>(py) * lw + px], lw);
                }
              }
            }
          }
          if (restart && --until_restart == 0 && !(my == mcuy - 1 && mx == mcux - 1)) {
            br.align();
            // Consume the RSTn marker.
            if (br.p + 2 <= br.end && br.p[0] == 0xFF && br.p[1] >= 0xD0 && br.p[1] <= 0xD7)
              br.p += 2;
            for (int c = 0; c < ncomp; ++c) comps[c].pred = 0;
            until_restart = restart;
          }
        }
      }

      *out = Gray8(width, height);
      for (int y = 0; y < height; ++y)
        std::copy(luma.begin() + static_cast<size_t>(y) * lw,
                  luma.begin() + static_cast<size_t>(y) * lw + width,
                  out->px.begin() + static_cast<size_t>(y) * width);
      return true;
    } else if (marker == 0xD9) {
      return false;  // EOI before any scan
    }
    i += 2 + len;
  }
  return false;
}

bool read_jpeg(const std::string& path, Gray8* out) {
  std::FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  std::fseek(f, 0, SEEK_END);
  const long size = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  std::vector<uint8_t> data(static_cast<size_t>(size > 0 ? size : 0));
  const bool ok = size > 0 && std::fread(data.data(), 1, data.size(), f) == data.size();
  std::fclose(f);
  return ok && decode_jpeg(data, out);
}

}  // namespace bs
