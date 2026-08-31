// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Baseline JFIF encoder, in-tree. Sessions are luma-first (every gate and
// meter runs on the Y plane), so the encoder writes single-component
// grayscale JPEGs: universally decodable, no chroma to invent. Standard
// Annex K quantisation and Huffman tables, quality-scaled the same way
// libjpeg scales them, so `quality: 92` means what it meant on the Pi.
#pragma once

#include <string>
#include <vector>

#include "birdshot/image.hpp"

namespace bs {

// Encode to memory. quality 1..100. The Gray8 overload writes the
// single-component luma JPEG the pipeline always has; the Rgb8 overload
// writes YCbCr 4:2:0, which is what colour sources deserve to keep.
std::vector<uint8_t> encode_jpeg(const Gray8& img, int quality);
std::vector<uint8_t> encode_jpeg(const Rgb8& img, int quality);

// Encode straight to a file; false on I/O failure.
bool write_jpeg(const Gray8& img, const std::string& path, int quality);

// Baseline sequential decoder, in-tree like the encoder: Huffman, DQT,
// restart markers, 1- and 3-component scans with any common subsampling.
// Only the luma plane is reconstructed by the Gray8 overload; the Rgb8
// overload rebuilds full colour. Progressive JPEGs are refused.
bool decode_jpeg(const std::vector<uint8_t>& data, Gray8* out);
bool decode_jpeg(const std::vector<uint8_t>& data, Rgb8* out);
bool read_jpeg(const std::string& path, Gray8* out);
bool read_jpeg(const std::string& path, Rgb8* out);

}  // namespace bs
