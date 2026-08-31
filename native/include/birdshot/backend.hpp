// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The capture backend interface. Everything above this line -- metering,
// AE, gates, Bird Flight, storage -- is backend-blind, exactly as in the
// 1.x line. The synthetic scene is the one backend every platform carries;
// platform camera backends (V4L2, AVFoundation, Media Foundation, libcamera)
// slot in behind the same five calls. Capability names gate the UI and the
// CLI the same way the Python CAPABILITIES sets did.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "birdshot/exposure.hpp"
#include "birdshot/image.hpp"

namespace bs {

struct Frame {
  Gray8 y;                 // the analysis-and-save luma plane
  int64_t exposure_us = 0; // what the sensor actually did (may be quantised)
  double gain = 1.0;
  double lux = 0.0;        // scene luminance estimate, 0 = unknown
  double ts = 0.0;         // POSIX capture instant
};

class Backend {
 public:
  virtual ~Backend() = default;
  virtual std::string name() const = 0;
  virtual std::vector<std::string> capabilities() const = 0;
  virtual SensorLimits limits() const = 0;
  // Capture one frame at the requested exposure. Blocking.
  virtual Frame capture(int64_t exposure_us, double gain) = 0;
};

// The backend the config asks for. Unknown names fall back to synthetic
// with a warning on stderr rather than refusing to run.
std::unique_ptr<Backend> make_backend(const class Config& cfg);

}  // namespace bs
