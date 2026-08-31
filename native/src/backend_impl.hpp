// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Internal seams between make_backend and the per-platform backends.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "birdshot/backend.hpp"

namespace bs {

class Config;

std::unique_ptr<Backend> make_synthetic_backend(const Config& cfg);

#ifdef __APPLE__
// The AVFoundation webcam backend (avfoundation.mm).
std::vector<CameraInfo> avf_list_cameras();
std::unique_ptr<Backend> make_avf_backend(const Config& cfg, int index, std::string* err);
#endif

}  // namespace bs
