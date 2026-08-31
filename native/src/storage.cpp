// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/storage.hpp"

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <sstream>

#include "birdshot/naming.hpp"

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <share.h>
#include <sys/stat.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace fs = std::filesystem;

namespace bs {

namespace {

// O_EXCL claim of one exact path. True if we created it.
bool try_claim(const std::string& path) {
#ifdef _WIN32
  int fd = -1;
  if (_sopen_s(&fd, path.c_str(), _O_CREAT | _O_EXCL | _O_WRONLY, _SH_DENYNO,
               _S_IREAD | _S_IWRITE) != 0 || fd < 0)
    return false;
  _close(fd);
  return true;
#else
  const int fd = ::open(path.c_str(), O_CREAT | O_EXCL | O_WRONLY, 0644);
  if (fd < 0) return false;
  ::close(fd);
  return true;
#endif
}

}  // namespace

std::string claim_exclusive(const std::string& path) {
  if (try_claim(path)) return path;
  const fs::path p(path);
  const std::string stem = (p.parent_path() / p.stem()).string();
  const std::string ext = p.extension().string();
  // At 100 name slots per second the suffix is a safety net, not the normal
  // case; three encoder threads finishing together is what it exists for.
  for (int i = 1; i <= 999; ++i) {
    char buf[16];
    std::snprintf(buf, sizeof buf, "_%03d", i);
    const std::string candidate = stem + buf + ext;
    if (try_claim(candidate)) return candidate;
  }
  return "";
}

bool write_file_atomic(const std::string& path, const void* data, size_t n) {
  const std::string part = path + ".part";
  {
    std::FILE* f = std::fopen(part.c_str(), "wb");
    if (!f) return false;
    const bool ok = n == 0 || std::fwrite(data, 1, n, f) == n;
    if (std::fclose(f) != 0 || !ok) {
      std::remove(part.c_str());
      return false;
    }
  }
  std::error_code ec;
  fs::rename(part, path, ec);
  if (ec) {
    fs::remove(part, ec);
    return false;
  }
  return true;
}

uint64_t free_space_mb(const std::string& path) {
  std::error_code ec;
  const fs::space_info si = fs::space(path, ec);
  if (ec) return 0;
  return si.available / (1024ull * 1024ull);
}

Session Session::create(const std::string& data_root, const std::string& kind) {
  Session s;
  const auto epoch = static_cast<long long>(std::time(nullptr));
  char name[64];
  std::snprintf(name, sizeof name, "%s-%lld", kind.c_str(), epoch);
  fs::path dir = fs::path(data_root) / name;
  std::error_code ec;
  // A second run inside the same second gets a suffixed directory rather
  // than sharing one.
  for (int i = 0; fs::exists(dir, ec) && i < 100; ++i) {
    std::snprintf(name, sizeof name, "%s-%lld_%02d", kind.c_str(), epoch, i + 1);
    dir = fs::path(data_root) / name;
  }
  fs::create_directories(dir, ec);
  if (ec) return s;
  s.dir_ = dir.string();
  s.name_ = name;
  return s;
}

Session Session::open(const std::string& dir) {
  Session s;
  std::error_code ec;
  if (!fs::is_directory(dir, ec)) return s;
  s.dir_ = dir;
  s.name_ = fs::path(dir).filename().string();
  return s;
}

std::string Session::claim_frame(double when, int64_t exposure_us, const std::string& ext,
                                 bool flat) {
  fs::path parent(dir_);
  if (!flat) {
    parent /= shutter_dir(exposure_us);
    std::error_code ec;
    fs::create_directories(parent, ec);
    if (ec) return "";
  }
  const std::string base = timestamp_name(when) + ext;
  const std::string claimed = claim_exclusive((parent / base).string());
  if (claimed.empty()) return "";
  ++frames_;
  return claimed + ".part";
}

bool Session::commit_frame(const std::string& part_path) {
  if (part_path.size() < 6) return false;
  const std::string final_path = part_path.substr(0, part_path.size() - 5);
  std::error_code ec;
  fs::rename(part_path, final_path, ec);
  return !ec;
}

bool Session::append_index(const Json& record) {
  std::ofstream out(fs::path(dir_) / "index.jsonl", std::ios::app | std::ios::binary);
  if (!out) return false;
  out << record.dump() << '\n';
  return static_cast<bool>(out);
}

bool Session::close(const Json& summary) {
  const std::string payload = summary.dump(2) + "\n";
  return write_file_atomic((fs::path(dir_) / "session.json").string(), payload.data(),
                           payload.size());
}

std::vector<Json> Session::read_index() const {
  std::vector<Json> out;
  std::ifstream in(fs::path(dir_) / "index.jsonl", std::ios::binary);
  if (!in) return out;
  std::string line;
  while (std::getline(in, line)) {
    if (line.empty()) continue;
    std::string err;
    Json rec = Json::parse(line, &err);
    if (err.empty() && rec.is_object()) out.push_back(std::move(rec));
  }
  return out;
}

std::vector<std::string> list_sessions(const std::string& data_root) {
  std::vector<std::string> out;
  std::error_code ec;
  for (const auto& entry : fs::directory_iterator(data_root, ec)) {
    if (!entry.is_directory(ec)) continue;
    const std::string name = entry.path().filename().string();
    if (name.rfind("sess-", 0) == 0 || name.rfind("rapid-", 0) == 0 ||
        name.rfind("tlc-", 0) == 0)
      out.push_back(entry.path().string());
  }
  std::sort(out.rbegin(), out.rend());
  return out;
}

}  // namespace bs
