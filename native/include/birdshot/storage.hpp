// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// Session layout on disk, identical to the 1.x line and to the runCam.sh
// folders before it:
//
//   <data_root>/sess-<epoch>/       one directory per COLLECT run
//     ms18/ s191/ ...               split by shutter duration
//       2026073113353247.jpg        YYYYMMDDHHMMSScc, centisecond names
//     index.jsonl                   one JSON line per frame
//     session.json                  summary written on close
//   <data_root>/rapid-<epoch>/      flat rapid runs
//   <data_root>/tlc-<epoch>/        timelapse runs
//
// Frames are written to a .part sibling and renamed once complete, and names
// are claimed with O_EXCL, so a sync can never pick up a half-written file
// and concurrent writers cannot collide.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "birdshot/json.hpp"

namespace bs {

// Claim `path` exclusively (O_EXCL). If taken, retry with _001.. suffixes
// before the extension. Returns the path actually claimed, or empty on error.
std::string claim_exclusive(const std::string& path);

// Atomic file write: .part sibling, then rename over.
bool write_file_atomic(const std::string& path, const void* data, size_t n);

uint64_t free_space_mb(const std::string& path);

class Session {
 public:
  // kind: "sess" | "rapid" | "tlc". Creates <data_root>/<kind>-<epoch>/.
  static Session create(const std::string& data_root, const std::string& kind);
  // Open an existing session directory (for listing / alignment).
  static Session open(const std::string& dir);

  const std::string& dir() const { return dir_; }
  const std::string& name() const { return name_; }
  bool valid() const { return !dir_.empty(); }

  // Claim a frame filename for `when` under the right shutter bucket
  // ("" bucket = flat, rapid style). Returns the claimed .part path to write
  // to; finish with commit_frame().
  std::string claim_frame(double when, int64_t exposure_us, const std::string& ext,
                          bool flat = false);
  static bool commit_frame(const std::string& part_path);

  // One JSON line per frame.
  bool append_index(const Json& record);

  // Summary written on close.
  bool close(const Json& summary);

  // Every frame recorded in index.jsonl (empty when the index is missing).
  std::vector<Json> read_index() const;

  int frames_written() const { return frames_; }

 private:
  std::string dir_;
  std::string name_;
  int frames_ = 0;
};

// Session directories under a data root, newest first.
std::vector<std::string> list_sessions(const std::string& data_root);

}  // namespace bs
