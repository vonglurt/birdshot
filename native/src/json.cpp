// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
#include "birdshot/json.hpp"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace bs {

bool Json::operator==(const Json& o) const {
  if (type_ != o.type_) return false;
  switch (type_) {
    case Type::Null: return true;
    case Type::Bool: return bool_ == o.bool_;
    case Type::Number: return num_ == o.num_;
    case Type::String: return str_ == o.str_;
    case Type::Array: return arr_ == o.arr_;
    case Type::Object: return obj_ == o.obj_;
  }
  return false;
}

// ---------------------------------------------------------------- writing --

static void write_escaped(std::string& out, const std::string& s) {
  out += '"';
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof buf, "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  out += '"';
}

static void write_number(std::string& out, double v) {
  if (std::isnan(v) || std::isinf(v)) { out += "null"; return; }
  // Integral values print without a fraction, so exposure_us stays an int on
  // disk and round-trips against the Python line byte-identically.
  if (v == std::floor(v) && std::fabs(v) < 1e15) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "%lld", static_cast<long long>(v));
    out += buf;
    return;
  }
  char buf[32];
  std::snprintf(buf, sizeof buf, "%.17g", v);
  // Trim to the shortest representation that survives a round-trip.
  for (int prec = 1; prec < 17; ++prec) {
    char t[32];
    std::snprintf(t, sizeof t, "%.*g", prec, v);
    if (std::strtod(t, nullptr) == v) { out += t; return; }
  }
  out += buf;
}

void Json::write(std::string& out, int indent, int depth) const {
  const bool pretty = indent >= 0;
  const std::string pad = pretty ? std::string(static_cast<size_t>(indent) * (depth + 1), ' ') : "";
  const std::string padc = pretty ? std::string(static_cast<size_t>(indent) * depth, ' ') : "";
  const char* nl = pretty ? "\n" : "";
  const char* sp = pretty ? " " : "";

  switch (type_) {
    case Type::Null: out += "null"; break;
    case Type::Bool: out += bool_ ? "true" : "false"; break;
    case Type::Number: write_number(out, num_); break;
    case Type::String: write_escaped(out, str_); break;
    case Type::Array: {
      if (arr_.empty()) { out += "[]"; break; }
      out += '[';
      out += nl;
      for (size_t i = 0; i < arr_.size(); ++i) {
        out += pad;
        arr_[i].write(out, indent, depth + 1);
        if (i + 1 < arr_.size()) out += ',';
        out += nl;
      }
      out += padc;
      out += ']';
      break;
    }
    case Type::Object: {
      if (obj_.empty()) { out += "{}"; break; }
      out += '{';
      out += nl;
      size_t i = 0;
      for (const auto& kv : obj_) {
        out += pad;
        write_escaped(out, kv.first);
        out += ':';
        out += sp;
        kv.second.write(out, indent, depth + 1);
        if (++i < obj_.size()) out += ',';
        out += nl;
      }
      out += padc;
      out += '}';
      break;
    }
  }
}

std::string Json::dump(int indent) const {
  std::string out;
  write(out, indent, 0);
  return out;
}

// ---------------------------------------------------------------- parsing --

namespace {

struct Parser {
  const char* p;
  const char* end;
  std::string err;

  void skip_ws() {
    while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) ++p;
  }

  bool fail(const std::string& why) {
    if (err.empty()) err = why;
    return false;
  }

  bool literal(const char* word, size_t n) {
    if (static_cast<size_t>(end - p) < n || std::memcmp(p, word, n) != 0)
      return fail(std::string("expected '") + word + "'");
    p += n;
    return true;
  }

  bool parse_string(std::string& out) {
    if (p >= end || *p != '"') return fail("expected string");
    ++p;
    while (p < end && *p != '"') {
      char c = *p++;
      if (c != '\\') { out += c; continue; }
      if (p >= end) return fail("truncated escape");
      char e = *p++;
      switch (e) {
        case '"': out += '"'; break;
        case '\\': out += '\\'; break;
        case '/': out += '/'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        case 'u': {
          if (end - p < 4) return fail("truncated \\u escape");
          unsigned cp = 0;
          for (int i = 0; i < 4; ++i) {
            char h = *p++;
            cp <<= 4;
            if (h >= '0' && h <= '9') cp |= static_cast<unsigned>(h - '0');
            else if (h >= 'a' && h <= 'f') cp |= static_cast<unsigned>(h - 'a' + 10);
            else if (h >= 'A' && h <= 'F') cp |= static_cast<unsigned>(h - 'A' + 10);
            else return fail("bad \\u escape");
          }
          // Surrogate pair -> code point.
          if (cp >= 0xD800 && cp <= 0xDBFF && end - p >= 6 && p[0] == '\\' && p[1] == 'u') {
            unsigned lo = 0;
            bool ok = true;
            for (int i = 2; i < 6; ++i) {
              char h = p[i];
              lo <<= 4;
              if (h >= '0' && h <= '9') lo |= static_cast<unsigned>(h - '0');
              else if (h >= 'a' && h <= 'f') lo |= static_cast<unsigned>(h - 'a' + 10);
              else if (h >= 'A' && h <= 'F') lo |= static_cast<unsigned>(h - 'A' + 10);
              else { ok = false; break; }
            }
            if (ok && lo >= 0xDC00 && lo <= 0xDFFF) {
              cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
              p += 6;
            }
          }
          // UTF-8 encode.
          if (cp < 0x80) {
            out += static_cast<char>(cp);
          } else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
          } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
          } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
          }
          break;
        }
        default: return fail("bad escape");
      }
    }
    if (p >= end) return fail("unterminated string");
    ++p;  // closing quote
    return true;
  }

  bool parse_value(Json& out, int depth) {
    if (depth > 128) return fail("nesting too deep");
    skip_ws();
    if (p >= end) return fail("unexpected end of input");
    switch (*p) {
      case 'n': if (!literal("null", 4)) return false; out = Json(); return true;
      case 't': if (!literal("true", 4)) return false; out = Json(true); return true;
      case 'f': if (!literal("false", 5)) return false; out = Json(false); return true;
      case '"': {
        std::string s;
        if (!parse_string(s)) return false;
        out = Json(std::move(s));
        return true;
      }
      case '[': {
        ++p;
        Json::Array arr;
        skip_ws();
        if (p < end && *p == ']') { ++p; out = Json(std::move(arr)); return true; }
        while (true) {
          Json v;
          if (!parse_value(v, depth + 1)) return false;
          arr.push_back(std::move(v));
          skip_ws();
          if (p < end && *p == ',') { ++p; continue; }
          if (p < end && *p == ']') { ++p; break; }
          return fail("expected ',' or ']'");
        }
        out = Json(std::move(arr));
        return true;
      }
      case '{': {
        ++p;
        Json::Object obj;
        skip_ws();
        if (p < end && *p == '}') { ++p; out = Json(std::move(obj)); return true; }
        while (true) {
          skip_ws();
          std::string key;
          if (!parse_string(key)) return false;
          skip_ws();
          if (p >= end || *p != ':') return fail("expected ':'");
          ++p;
          Json v;
          if (!parse_value(v, depth + 1)) return false;
          obj[std::move(key)] = std::move(v);
          skip_ws();
          if (p < end && *p == ',') { ++p; continue; }
          if (p < end && *p == '}') { ++p; break; }
          return fail("expected ',' or '}'");
        }
        out = Json(std::move(obj));
        return true;
      }
      default: {
        char* num_end = nullptr;
        double v = std::strtod(p, &num_end);
        if (num_end == p) return fail("unexpected character");
        p = num_end;
        out = Json(v);
        return true;
      }
    }
  }
};

}  // namespace

Json Json::parse(const std::string& text, std::string* err) {
  Parser ps{text.data(), text.data() + text.size(), {}};
  Json out;
  if (!ps.parse_value(out, 0)) {
    if (err) *err = ps.err;
    return Json();
  }
  ps.skip_ws();
  if (ps.p != ps.end) {
    if (err) *err = "trailing content";
    return Json();
  }
  if (err) err->clear();
  return out;
}

}  // namespace bs
