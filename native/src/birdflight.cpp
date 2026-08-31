// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/birdflight.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>

#include "birdshot/config.hpp"

namespace bs {

namespace {

double round_to(double v, int places) {
  const double f = std::pow(10.0, places);
  return std::round(v * f) / f;
}

// Largest 4-connected component that does NOT touch the frame border.
// Border-touching dark regions are never the subject: the ground strip, a
// roofline, a tree at the edge, or a bird half out of frame -- all things
// the mode must wait out, not shoot. Plain BFS on the small mask.
bool largest_blob(const std::vector<uint8_t>& mask, int w, int h,
                  std::vector<uint8_t>* out_blob, int* out_n) {
  std::vector<uint8_t> seen(mask.size(), 0);
  std::vector<int> best;
  std::vector<int> stack, blob;
  for (int start = 0; start < w * h; ++start) {
    if (!mask[static_cast<size_t>(start)] || seen[static_cast<size_t>(start)]) continue;
    stack.assign(1, start);
    seen[static_cast<size_t>(start)] = 1;
    blob.clear();
    bool touches_border = false;
    while (!stack.empty()) {
      const int idx = stack.back();
      stack.pop_back();
      blob.push_back(idx);
      const int y = idx / w, x = idx % w;
      if (y == 0 || y == h - 1 || x == 0 || x == w - 1) touches_border = true;
      const int nbrs[4] = {idx - w, idx + w, idx - 1, idx + 1};
      const bool ok[4] = {y > 0, y < h - 1, x > 0, x < w - 1};
      for (int i = 0; i < 4; ++i) {
        if (ok[i] && mask[static_cast<size_t>(nbrs[i])] && !seen[static_cast<size_t>(nbrs[i])]) {
          seen[static_cast<size_t>(nbrs[i])] = 1;
          stack.push_back(nbrs[i]);
        }
      }
    }
    if (!touches_border && blob.size() > best.size()) best = blob;
  }
  if (best.empty()) return false;
  out_blob->assign(mask.size(), 0);
  for (int idx : best) (*out_blob)[static_cast<size_t>(idx)] = 1;
  *out_n = static_cast<int>(best.size());
  return true;
}

}  // namespace

Json Sighting::to_json() const {
  Json d = Json::object();
  d["present"] = present;
  d["take"] = take;
  Json::Array rs;
  for (const auto& r : reasons) rs.emplace_back(r);
  d["reasons"] = Json(rs);
  d["motion_frac"] = round_to(motion_frac, 5);
  d["sky_frac"] = round_to(sky_frac, 3);
  d["area_frac"] = round_to(area_frac, 5);
  d["ring_sky_frac"] = round_to(ring_sky_frac, 3);
  d["sharpness"] = round_to(sharpness, 1);
  if (has_subject_box) {
    d["centroid"] = Json(Json::Array{Json(centroid_x), Json(centroid_y)});
    d["bbox"] = Json(Json::Array{Json(bbox_x0), Json(bbox_y0), Json(bbox_x1), Json(bbox_y1)});
  } else {
    d["centroid"] = Json();
    d["bbox"] = Json();
  }
  return d;
}

double boundary_sharpness(const Gray8& y8, int x0, int y0, int x1, int y1) {
  // Sky is flat, so nearly all gradient energy in the (padded) box IS the
  // subject boundary: a sharp wing scores high, a blurred one low.
  const int pad = 6;
  x0 = std::max(0, x0 - pad);
  y0 = std::max(0, y0 - pad);
  x1 = std::min(y8.w, x1 + pad);
  y1 = std::min(y8.h, y1 + pad);
  const int pw = x1 - x0, ph = y1 - y0;
  if (pw < 4 || ph < 4) return 0.0;

  std::vector<float> grads;
  grads.reserve(static_cast<size_t>(pw) * ph * 2);
  for (int r = y0; r < y1; ++r) {
    const uint8_t* row = &y8.px[static_cast<size_t>(r) * y8.w];
    for (int c = x0; c < x1 - 1; ++c)
      grads.push_back(static_cast<float>(std::abs(row[c + 1] - row[c])));
  }
  for (int r = y0; r < y1 - 1; ++r) {
    const uint8_t* row = &y8.px[static_cast<size_t>(r) * y8.w];
    const uint8_t* nxt = &y8.px[static_cast<size_t>(r + 1) * y8.w];
    for (int c = x0; c < x1; ++c)
      grads.push_back(static_cast<float>(std::abs(nxt[c] - row[c])));
  }
  if (grads.empty()) return 0.0;
  const size_t top_n = std::max<size_t>(1, grads.size() / 10);
  std::nth_element(grads.begin(), grads.end() - static_cast<long>(top_n), grads.end());
  double sum = 0.0;
  for (size_t i = grads.size() - top_n; i < grads.size(); ++i) sum += grads[i];
  return (sum / static_cast<double>(top_n)) * (100.0 / 255.0);
}

Sighting BirdFlightDetector::update(const Gray8& y8) {
  Sighting s;
  const Gray8 small = y8.downsample(kBfDown);
  if (small.empty()) {
    s.reasons.push_back("no subject");
    return s;
  }

  const double sky_min = cfg_.num("bf_sky_luma_min", 110);
  std::vector<uint8_t> sky(small.size());
  size_t sky_n = 0;
  for (size_t i = 0; i < small.size(); ++i) {
    sky[i] = small.px[i] >= sky_min ? 1 : 0;
    sky_n += sky[i];
  }
  s.sky_frac = static_cast<double>(sky_n) / static_cast<double>(small.size());

  // Motion gate first: it is nearly free and rejects the static scene.
  if (have_prev_ && prev_.size() == small.size()) {
    size_t moved = 0;
    for (size_t i = 0; i < small.size(); ++i)
      if (std::abs(static_cast<int>(small.px[i]) - static_cast<int>(prev_.px[i])) > 12) ++moved;
    s.motion_frac = static_cast<double>(moved) / static_cast<double>(small.size());
  }
  const bool had_prev = have_prev_;
  prev_ = small;
  have_prev_ = true;
  if (cfg_.boolean("bf_require_motion", true)) {
    if (!had_prev || s.motion_frac < cfg_.num("bf_motion_min", 0.0005))
      s.reasons.push_back("no motion");
  }

  // A subject: dark, discrete, the largest such blob.
  const double subj_max = cfg_.num("bf_subject_luma_max", 80);
  std::vector<uint8_t> subject(small.size());
  for (size_t i = 0; i < small.size(); ++i) subject[i] = small.px[i] <= subj_max ? 1 : 0;

  std::vector<uint8_t> blob;
  int blob_n = 0;
  if (!largest_blob(subject, small.w, small.h, &blob, &blob_n)) {
    s.reasons.push_back("no subject");
    return s;
  }
  s.area_frac = static_cast<double>(blob_n) / static_cast<double>(small.size());

  int bx0 = small.w, bx1 = 0, by0 = small.h, by1 = 0;
  double cx = 0.0, cy = 0.0;
  for (int y = 0; y < small.h; ++y)
    for (int x = 0; x < small.w; ++x)
      if (blob[static_cast<size_t>(y) * small.w + x]) {
        bx0 = std::min(bx0, x);
        bx1 = std::max(bx1, x + 1);
        by0 = std::min(by0, y);
        by1 = std::max(by1, y + 1);
        cx += x;
        cy += y;
      }
  cx = cx / blob_n * kBfDown;
  cy = cy / blob_n * kBfDown;
  s.has_subject_box = true;
  s.centroid_x = cx;
  s.centroid_y = cy;
  s.bbox_x0 = bx0 * kBfDown;
  s.bbox_y0 = by0 * kBfDown;
  s.bbox_x1 = bx1 * kBfDown;
  s.bbox_y1 = by1 * kBfDown;

  if (s.area_frac < cfg_.num("bf_min_area_frac", 0.0004)) {
    s.reasons.push_back("subject too small");
    return s;
  }
  s.present = true;
  if (s.area_frac > cfg_.num("bf_max_area_frac", 0.05))
    s.reasons.push_back("subject too large (not a bird, or too close)");

  // Against sky: the ring around the box must be bright. This is what
  // separates a bird from a branch, a roofline, or the ground strip.
  const int rp = 3;
  const int rx0 = std::max(0, bx0 - rp), ry0 = std::max(0, by0 - rp);
  const int rx1 = std::min(small.w, bx1 + rp), ry1 = std::min(small.h, by1 + rp);
  size_t ring_px = 0, ring_sky = 0;
  for (int y = ry0; y < ry1; ++y)
    for (int x = rx0; x < rx1; ++x) {
      if (y >= by0 && y < by1 && x >= bx0 && x < bx1) continue;  // inside the box
      ++ring_px;
      ring_sky += sky[static_cast<size_t>(y) * small.w + x];
    }
  if (ring_px > 0)
    s.ring_sky_frac = static_cast<double>(ring_sky) / static_cast<double>(ring_px);
  if (s.ring_sky_frac < cfg_.num("bf_ring_sky_frac", 0.85))
    s.reasons.push_back("not against sky");

  // Composition: mostly sky, subject inside the margins.
  if (s.sky_frac < cfg_.num("bf_sky_min_frac", 0.5))
    s.reasons.push_back("frame not sky enough");
  const double margin = cfg_.num("bf_margin_frac", 0.08);
  const double mh = y8.h * margin, mw = y8.w * margin;
  if (!(mw <= cx && cx <= y8.w - mw && mh <= cy && cy <= y8.h - mh))
    s.reasons.push_back("subject too near the edge");

  // Focus, judged where it matters: on the subject boundary.
  s.sharpness = boundary_sharpness(y8, s.bbox_x0, s.bbox_y0, s.bbox_x1, s.bbox_y1);
  if (s.sharpness < cfg_.num("bf_min_sharpness", 12.0))
    s.reasons.push_back("boundary not sharp enough");

  s.take = s.reasons.empty();
  return s;
}

}  // namespace bs
