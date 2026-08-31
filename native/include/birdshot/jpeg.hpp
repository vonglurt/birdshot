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

// Encode to memory. quality 1..100.
std::vector<uint8_t> encode_jpeg(const Gray8& img, int quality);

// Encode straight to a file; false on I/O failure.
bool write_jpeg(const Gray8& img, const std::string& path, int quality);

// Baseline sequential decoder, in-tree like the encoder: Huffman, DQT,
// restart markers, 1- and 3-component scans with any common subsampling.
// Only the luma plane is reconstructed -- chroma coefficients are entropy-
// decoded (the stream demands it) and discarded, because everything this
// program does runs on Y. Progressive JPEGs are refused.
bool decode_jpeg(const std::vector<uint8_t>& data, Gray8* out);
bool read_jpeg(const std::string& path, Gray8* out);

}  // namespace bs
