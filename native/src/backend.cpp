// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Backend selection. "auto" stays the synthetic scene for now -- flipping
// it to a platform camera is a decision for when the backends have shot
// enough real frames to be trusted as a default; a selector or the config
// picks one explicitly in the meantime.
#include "backend_impl.hpp"

#include <cstdio>

#include "birdshot/config.hpp"

namespace bs {

std::unique_ptr<Backend> make_backend(const Config& cfg) {
  const std::string want = cfg.str("backend", "synthetic");
#ifdef __APPLE__
  if (want == "avfoundation" || want == "webcam") {
    std::string err;
    auto backend = make_avf_backend(cfg, static_cast<int>(cfg.num("camera_index", 0)), &err);
    if (backend) return backend;
    std::fprintf(stderr, "webcam: %s; using synthetic\n", err.c_str());
    return make_synthetic_backend(cfg);
  }
#endif
  if (want != "synthetic" && want != "auto")
    std::fprintf(stderr, "backend '%s' is not built into this binary; using synthetic\n",
                 want.c_str());
  return make_synthetic_backend(cfg);
}

std::vector<CameraInfo> list_cameras(const Config&) {
  std::vector<CameraInfo> out;
#ifdef __APPLE__
  for (const auto& cam : avf_list_cameras()) out.push_back(cam);
#endif
  out.push_back({"synthetic", 0, "Synthetic sky"});
  return out;
}

}  // namespace bs
