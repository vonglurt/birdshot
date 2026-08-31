// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/engine.hpp"

#include <chrono>
#include <cmath>
#include <cstdio>
#include <thread>

#include "birdshot/analysis.hpp"
#include "birdshot/birdflight.hpp"
#include "birdshot/exposure.hpp"
#include "birdshot/jpeg.hpp"
#include "birdshot/naming.hpp"

namespace bs {

namespace {

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

const char* mode_kind(Mode m) {
  switch (m) {
    case Mode::Rapid: return "rapid";
    case Mode::Timelapse: return "tlc";
    default: return "sess";
  }
}

}  // namespace

Engine::Engine(Config& cfg, Backend& backend) : cfg_(cfg), backend_(backend) {}

EngineReport Engine::run(const EngineOptions& opts) {
  EngineReport rep;
  const double t_start = mono_now();

  const std::string data_root = expand_user(cfg_.str("data_root", "~/birdshot-data"));
  Session session = Session::create(data_root, mode_kind(opts.mode));
  if (!session.valid()) {
    rep.clean = false;
    rep.stop_reason = "could not create session directory under " + data_root;
    return rep;
  }
  rep.session_dir = session.dir();
  log("session " + session.name() + " -> " + session.dir());

  ExposureController ae(cfg_);
  ae.set_limits(backend_.limits());

  const bool auto_exposure = cfg_.boolean("auto_exposure", true);
  int64_t exposure_us = static_cast<int64_t>(
      auto_exposure ? cfg_.state("last_shutter_us").number(2000)
                    : cfg_.num("manual_shutter_us", 2000));
  double gain = auto_exposure ? cfg_.state("last_gain").number(1.0)
                              : cfg_.num("manual_gain", 1.0);
  if (exposure_us <= 0) exposure_us = 2000;
  if (gain < kGainMin) gain = kGainMin;

  const int jpeg_quality = static_cast<int>(cfg_.num("jpeg_quality", 92));
  const bool save_pgm = cfg_.boolean("save_pgm", false);
  const double min_free_mb = cfg_.num("min_free_mb", 2048);
  const std::string reject_action = cfg_.str("reject_action", "flag");

  int64_t count = opts.count;
  if (count <= 0) {
    switch (opts.mode) {
      case Mode::Collect: count = static_cast<int64_t>(cfg_.num("burst_count", 0)); break;
      case Mode::Rapid: count = static_cast<int64_t>(cfg_.num("rapid_count", 0)); break;
      case Mode::Timelapse: count = static_cast<int64_t>(cfg_.num("timelapse_count", 0)); break;
      case Mode::BirdFlight: count = static_cast<int64_t>(cfg_.num("bf_takes", 0)); break;
    }
  }
  double interval_s = opts.interval_s;
  if (interval_s < 0.0) interval_s = cfg_.num("timelapse_interval_s", 5.0);

  BirdFlightDetector detector(cfg_);
  const int bf_burst = std::max(1, static_cast<int>(cfg_.num("bf_burst", 5)));
  const double bf_cooldown_s = cfg_.num("bf_cooldown_s", 3.0);
  int burst_left = 0;        // frames still owed to a fired take
  double cooldown_until = 0; // monotonic
  int64_t seq = 0;

  // Seed from the learned lux constant so the first frame of a run is
  // usable instead of thrown away while AE converges.
  {
    const Frame probe = backend_.capture(exposure_us, gain);
    if (auto seeded = ae.seed(probe.lux)) {
      exposure_us = seeded->first;
      gain = seeded->second;
    }
  }

  double next_due = mono_now();
  while (!stop_.load()) {
    if (opts.mode == Mode::Timelapse) {
      const double now = mono_now();
      if (now < next_due)
        std::this_thread::sleep_for(std::chrono::duration<double>(next_due - now));
      next_due += interval_s;
    }

    if (free_space_mb(session.dir()) < min_free_mb) {
      rep.clean = false;
      rep.stop_reason = "free space below the floor; capture stopped";
      log(rep.stop_reason);
      break;
    }

    Frame frame = backend_.capture(exposure_us, gain);
    ++rep.frames;
    ++seq;

    const bool rapid = opts.mode == Mode::Rapid;
    const Gray8& hires = frame.full.empty() ? frame.y : frame.full;
    FrameStats st = rapid ? meter_only(frame.y, cfg_)
                          : analyse(frame.y, cfg_, hires.centre_crop(512));
    if (tap_) tap_(frame, st);

    // Bird Flight judges before anything is saved; only a take writes.
    bool save = true;
    if (opts.mode == Mode::BirdFlight) {
      const Sighting sighting = detector.update(frame.y);
      const double now = mono_now();
      bool fired = false;
      if (burst_left > 0) {
        --burst_left;
      } else if (sighting.take && now >= cooldown_until) {
        fired = true;
        ++rep.takes;
        burst_left = bf_burst - 1;
        cooldown_until = now + bf_cooldown_s;
        log("take " + std::to_string(rep.takes) + ": sharpness " +
            std::to_string(sighting.sharpness));
      } else {
        save = false;
        if (opts.verbose && sighting.present) {
          std::string why;
          for (const auto& r : sighting.reasons) why += (why.empty() ? "" : ", ") + r;
          log("holding fire: " + why);
        }
      }
      if (sighting_tap_) sighting_tap_(sighting, rep.takes, fired);
      if (save) {
        Json rec = st.to_json();
        rec["sighting"] = sighting.to_json();
        rec["name"] = timestamp_name(frame.ts);
        rec["exposure_us"] = static_cast<double>(frame.exposure_us);
        rec["gain"] = frame.gain;
        rec["take"] = static_cast<double>(rep.takes);
        session.append_index(rec);
      }
    }

    // Quality gates: what the verdict costs depends on reject_action.
    bool write_frame = save;
    if (save && !rapid && st.verdict != "ok") {
      ++rep.rejected;
      if (reject_action == "delete") write_frame = false;
      // "quarantine" and "flag" both keep the file in an RC build; the
      // verdict is recorded either way and assembly filters on it.
    }

    if (write_frame) {
      const std::string part =
          session.claim_frame(frame.ts, frame.exposure_us, ".jpg", rapid);
      if (!part.empty()) {
        const std::vector<uint8_t> encoded =
            encode_jpeg(frame.full.empty() ? frame.y : frame.full, jpeg_quality);
        std::FILE* f = std::fopen(part.c_str(), "wb");
        bool ok = f != nullptr;
        if (f) {
          ok = std::fwrite(encoded.data(), 1, encoded.size(), f) == encoded.size();
          ok = std::fclose(f) == 0 && ok;
        }
        if (ok && Session::commit_frame(part)) {
          ++rep.saved;
          if (save_pgm) {
            const std::string jpg = part.substr(0, part.size() - 5);
            write_pgm(frame.full.empty() ? frame.y : frame.full,
                      jpg.substr(0, jpg.size() - 4) + ".pgm");
          }
        }
      }
    }

    if (opts.mode != Mode::BirdFlight) {
      Json rec = st.to_json();
      rec["name"] = timestamp_name(frame.ts);
      rec["exposure_us"] = static_cast<double>(frame.exposure_us);
      rec["gain"] = frame.gain;
      rec["lux"] = frame.lux;
      session.append_index(rec);
    }

    // AE: given how that frame came out, decide the next one. The clock
    // handed to the controller is frame cadence, not wall time: the PID
    // gains were tuned at the 1.x line's 3-4 fps (dt ~ 0.25 s), and feeding
    // real dt at this engine's hundreds of fps makes the derivative term
    // amplify per-frame metering noise by two orders of magnitude.
    if (auto_exposure) {
      const ExposureDecision d =
          ae.update(st, frame.exposure_us, frame.gain, frame.lux, seq * 0.25);
      exposure_us = d.exposure_us;
      gain = d.gain;
      if (opts.verbose)
        log("frame " + std::to_string(seq) + ": meter " + std::to_string(st.meter) +
            " verdict " + st.verdict + " -> " + describe_shutter(exposure_us) + " gain " +
            std::to_string(gain) + " [" + d.mode + "]");
    } else if (opts.verbose) {
      log("frame " + std::to_string(seq) + ": verdict " + st.verdict);
    }

    const int64_t done = opts.mode == Mode::BirdFlight ? rep.takes : rep.frames;
    if (count > 0 && done >= count && burst_left == 0) break;
  }

  rep.seconds = mono_now() - t_start;
  rep.fps = rep.seconds > 0 ? static_cast<double>(rep.frames) / rep.seconds : 0.0;

  // Resume state, like the 1.x line: a restart picks up where this left off.
  cfg_.set_state("last_shutter_us", Json(static_cast<double>(exposure_us)));
  cfg_.set_state("last_gain", Json(gain));
  cfg_.set_state("last_session", Json(session.name()));
  ae.persist();
  cfg_.save();

  Json summary = Json::object();
  summary["session"] = session.name();
  summary["mode"] = mode_kind(opts.mode);
  summary["frames"] = static_cast<double>(rep.frames);
  summary["saved"] = static_cast<double>(rep.saved);
  summary["rejected"] = static_cast<double>(rep.rejected);
  summary["takes"] = static_cast<double>(rep.takes);
  summary["seconds"] = rep.seconds;
  summary["fps"] = rep.fps;
  summary["clean"] = rep.clean;
  session.close(summary);
  return rep;
}

}  // namespace bs
