// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/exif.hpp"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>

#include "birdshot/config.hpp"
#include "birdshot/storage.hpp"

namespace bs {

namespace {

// TIFF tag ids used here.
constexpr uint16_t kMake = 0x010F;
constexpr uint16_t kModel = 0x0110;
constexpr uint16_t kSoftware = 0x0131;
constexpr uint16_t kDateTime = 0x0132;
constexpr uint16_t kArtist = 0x013B;
constexpr uint16_t kCopyright = 0x8298;
constexpr uint16_t kExifIfdPointer = 0x8769;
constexpr uint16_t kExposureTime = 0x829A;
constexpr uint16_t kFNumber = 0x829D;
constexpr uint16_t kIso = 0x8827;
constexpr uint16_t kDateTimeOriginal = 0x9003;
constexpr uint16_t kSubSecTimeOriginal = 0x9291;
constexpr uint16_t kFocalLength = 0x920A;
constexpr uint16_t kUserComment = 0x9286;
constexpr uint16_t kLensModel = 0xA434;

constexpr uint16_t kTypeAscii = 2;
constexpr uint16_t kTypeShort = 3;
constexpr uint16_t kTypeLong = 4;
constexpr uint16_t kTypeRational = 5;
constexpr uint16_t kTypeUndefined = 7;

// One IFD entry plus, when the value is over four bytes, its out-of-line
// payload. Offsets are patched once the layout is known.
struct Entry {
  uint16_t tag = 0;
  uint16_t type = 0;
  uint32_t count = 0;
  std::vector<uint8_t> value;  // the raw value bytes, unpadded
};

void put16(std::vector<uint8_t>& out, uint16_t v) {
  out.push_back(v & 0xFF);
  out.push_back(v >> 8);
}
void put32(std::vector<uint8_t>& out, uint32_t v) {
  out.push_back(v & 0xFF);
  out.push_back((v >> 8) & 0xFF);
  out.push_back((v >> 16) & 0xFF);
  out.push_back(v >> 24);
}

Entry ascii(uint16_t tag, const std::string& text) {
  Entry e;
  e.tag = tag;
  e.type = kTypeAscii;
  e.value.assign(text.begin(), text.end());
  e.value.push_back(0);
  e.count = static_cast<uint32_t>(e.value.size());
  return e;
}

Entry rational(uint16_t tag, uint32_t num, uint32_t den) {
  Entry e;
  e.tag = tag;
  e.type = kTypeRational;
  e.count = 1;
  put32(e.value, num);
  put32(e.value, den);
  return e;
}

Entry shortval(uint16_t tag, uint16_t v) {
  Entry e;
  e.tag = tag;
  e.type = kTypeShort;
  e.count = 1;
  put16(e.value, v);
  return e;
}

Entry undefined(uint16_t tag, const std::vector<uint8_t>& bytes) {
  Entry e;
  e.tag = tag;
  e.type = kTypeUndefined;
  e.count = static_cast<uint32_t>(bytes.size());
  e.value = bytes;
  return e;
}

// Serialise one IFD at `at` (offset within the TIFF payload), appending
// its oversize values to `heap` which begins at `heap_at`.
void write_ifd(std::vector<uint8_t>& tiff, const std::vector<Entry>& entries,
               uint32_t next_ifd = 0) {
  put16(tiff, static_cast<uint16_t>(entries.size()));
  // Value heap starts after: count(2) + entries(12 each) + next(4).
  uint32_t heap_at =
      static_cast<uint32_t>(tiff.size()) - 2 + static_cast<uint32_t>(entries.size()) * 12 + 2 + 4;
  std::vector<uint8_t> heap;
  for (const Entry& e : entries) {
    put16(tiff, e.tag);
    put16(tiff, e.type);
    put32(tiff, e.count);
    if (e.value.size() <= 4) {
      std::vector<uint8_t> padded = e.value;
      padded.resize(4, 0);
      tiff.insert(tiff.end(), padded.begin(), padded.end());
    } else {
      put32(tiff, heap_at + static_cast<uint32_t>(heap.size()));
      heap.insert(heap.end(), e.value.begin(), e.value.end());
      if (heap.size() % 2) heap.push_back(0);  // word-align the next value
    }
  }
  put32(tiff, next_ifd);
  tiff.insert(tiff.end(), heap.begin(), heap.end());
}

std::string exif_datetime(double when) {
  const time_t t = static_cast<time_t>(when);
  std::tm lt{};
#ifdef _WIN32
  localtime_s(&lt, &t);
#else
  localtime_r(&t, &lt);
#endif
  char buf[24];
  std::snprintf(buf, sizeof buf, "%04d:%02d:%02d %02d:%02d:%02d", lt.tm_year + 1900,
                lt.tm_mon + 1, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec);
  return buf;
}

}  // namespace

ExifInfo exif_from_config(const Config& cfg) {
  ExifInfo info;
  info.make = cfg.str("exif_make", "Raspberry Pi");
  info.model = cfg.str("exif_model", "IMX477 HQ Camera");
  info.software = cfg.str("exif_software", "birdshot");
  info.lens = cfg.str("exif_lens", "");
  info.artist = cfg.str("exif_artist", "");
  info.copyright = cfg.str("exif_copyright", "");
  info.fnumber = cfg.num("exif_fnumber", 0.0);
  info.focal_mm = cfg.num("exif_focal_mm", 0.0);
  return info;
}

std::vector<uint8_t> build_exif_app1(const ExifInfo& info) {
  // The Exif IFD first, laid out after IFD0; its offset needs IFD0's size,
  // so build both against a two-pass layout: IFD0 always carries exactly
  // one oversize-value heap, computed by write_ifd itself, which makes the
  // Exif IFD's start simply "wherever IFD0 ended".
  std::vector<Entry> ifd0;
  if (!info.make.empty()) ifd0.push_back(ascii(kMake, info.make));
  if (!info.model.empty()) ifd0.push_back(ascii(kModel, info.model));
  if (!info.software.empty()) ifd0.push_back(ascii(kSoftware, info.software));
  if (info.when > 0) ifd0.push_back(ascii(kDateTime, exif_datetime(info.when)));
  if (!info.artist.empty()) ifd0.push_back(ascii(kArtist, info.artist));
  if (!info.copyright.empty()) ifd0.push_back(ascii(kCopyright, info.copyright));

  std::vector<Entry> exif;
  if (info.exposure_us > 0)
    exif.push_back(rational(kExposureTime, static_cast<uint32_t>(info.exposure_us), 1000000));
  if (info.fnumber > 0)
    exif.push_back(rational(kFNumber, static_cast<uint32_t>(info.fnumber * 100), 100));
  if (info.gain > 0)
    exif.push_back(shortval(kIso, static_cast<uint16_t>(std::lround(info.gain * 100))));
  if (info.when > 0) {
    exif.push_back(ascii(kDateTimeOriginal, exif_datetime(info.when)));
    const int centis = static_cast<int>(std::llround(info.when * 100)) % 100;
    char cs[4];
    std::snprintf(cs, sizeof cs, "%02d", centis < 0 ? 0 : centis);
    exif.push_back(ascii(kSubSecTimeOriginal, cs));
  }
  if (info.focal_mm > 0)
    exif.push_back(rational(kFocalLength, static_cast<uint32_t>(info.focal_mm * 100), 100));
  if (!info.user_comment.empty()) {
    std::vector<uint8_t> comment{'A', 'S', 'C', 'I', 'I', 0, 0, 0};
    comment.insert(comment.end(), info.user_comment.begin(), info.user_comment.end());
    exif.push_back(undefined(kUserComment, comment));
  }
  if (!info.lens.empty()) exif.push_back(ascii(kLensModel, info.lens));

  // Pass 1: measure IFD0 with a placeholder pointer to learn where the
  // Exif IFD lands. Pass 2: write it for real.
  std::vector<uint8_t> tiff;
  put16(tiff, 0x4949);  // "II" little-endian
  put16(tiff, 42);
  put32(tiff, 8);  // IFD0 at offset 8

  std::vector<Entry> ifd0_full = ifd0;
  {
    Entry pointer;
    pointer.tag = kExifIfdPointer;
    pointer.type = kTypeLong;
    pointer.count = 1;
    put32(pointer.value, 0);
    ifd0_full.push_back(pointer);
  }
  std::vector<uint8_t> probe = tiff;
  write_ifd(probe, ifd0_full);
  const uint32_t exif_at = static_cast<uint32_t>(probe.size());
  ifd0_full.back().value.clear();
  put32(ifd0_full.back().value, exif_at);

  write_ifd(tiff, ifd0_full);
  write_ifd(tiff, exif);

  std::vector<uint8_t> app1;
  app1.push_back(0xFF);
  app1.push_back(0xE1);
  const size_t length = 2 + 6 + tiff.size();
  app1.push_back(static_cast<uint8_t>(length >> 8));
  app1.push_back(static_cast<uint8_t>(length & 0xFF));
  const char header[6] = {'E', 'x', 'i', 'f', 0, 0};
  app1.insert(app1.end(), header, header + 6);
  app1.insert(app1.end(), tiff.begin(), tiff.end());
  return app1;
}

std::vector<uint8_t> inject_exif(const std::vector<uint8_t>& jpeg, const ExifInfo& info) {
  if (jpeg.size() < 4 || jpeg[0] != 0xFF || jpeg[1] != 0xD8) return {};
  const std::vector<uint8_t> app1 = build_exif_app1(info);

  std::vector<uint8_t> out;
  out.reserve(jpeg.size() + app1.size());
  out.push_back(0xFF);
  out.push_back(0xD8);
  out.insert(out.end(), app1.begin(), app1.end());

  // Copy the rest, dropping any APP1 Exif segment already there.
  size_t i = 2;
  while (i + 4 <= jpeg.size() && jpeg[i] == 0xFF && jpeg[i + 1] == 0xE1) {
    const size_t seg = (static_cast<size_t>(jpeg[i + 2]) << 8) + jpeg[i + 3];
    if (i + 2 + seg > jpeg.size()) break;
    if (seg >= 8 && std::memcmp(&jpeg[i + 4], "Exif\0\0", 6) == 0) {
      i += 2 + seg;
      continue;
    }
    break;
  }
  out.insert(out.end(), jpeg.begin() + static_cast<long>(i), jpeg.end());
  return out;
}

bool inject_exif_file(const std::string& path, const ExifInfo& info) {
  std::FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  std::fseek(f, 0, SEEK_END);
  const long size = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  std::vector<uint8_t> jpeg(static_cast<size_t>(size > 0 ? size : 0));
  const bool read_ok = size > 0 && std::fread(jpeg.data(), 1, jpeg.size(), f) == jpeg.size();
  std::fclose(f);
  if (!read_ok) return false;

  const std::vector<uint8_t> out = inject_exif(jpeg, info);
  if (out.empty()) return false;
  return write_file_atomic(path, out.data(), out.size());
}

}  // namespace bs
