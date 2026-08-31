// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// A small JSON value type, parser and writer. In-tree because settings.json,
// index.jsonl and session.json are the interchange formats shared with the
// 1.x Python line, and a dependency for that would be the tail wagging the
// dog. Objects keep sorted keys so dumps are stable and diffable, matching
// the Python side's sort_keys=True.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace bs {

class Json {
 public:
  enum class Type { Null, Bool, Number, String, Array, Object };
  using Array = std::vector<Json>;
  using Object = std::map<std::string, Json>;

  Json() : type_(Type::Null) {}
  Json(std::nullptr_t) : type_(Type::Null) {}
  Json(bool b) : type_(Type::Bool), bool_(b) {}
  Json(int v) : type_(Type::Number), num_(v) {}
  Json(long v) : type_(Type::Number), num_(static_cast<double>(v)) {}
  Json(long long v) : type_(Type::Number), num_(static_cast<double>(v)) {}
  Json(double v) : type_(Type::Number), num_(v) {}
  Json(const char* s) : type_(Type::String), str_(s) {}
  Json(std::string s) : type_(Type::String), str_(std::move(s)) {}
  Json(Array a) : type_(Type::Array), arr_(std::move(a)) {}
  Json(Object o) : type_(Type::Object), obj_(std::move(o)) {}

  static Json array() { return Json(Array{}); }
  static Json object() { return Json(Object{}); }

  Type type() const { return type_; }
  bool is_null() const { return type_ == Type::Null; }
  bool is_bool() const { return type_ == Type::Bool; }
  bool is_number() const { return type_ == Type::Number; }
  bool is_string() const { return type_ == Type::String; }
  bool is_array() const { return type_ == Type::Array; }
  bool is_object() const { return type_ == Type::Object; }

  bool boolean(bool fallback = false) const {
    if (type_ == Type::Bool) return bool_;
    if (type_ == Type::Number) return num_ != 0.0;
    return fallback;
  }
  double number(double fallback = 0.0) const {
    return type_ == Type::Number ? num_ : fallback;
  }
  int64_t integer(int64_t fallback = 0) const {
    return type_ == Type::Number ? static_cast<int64_t>(num_ >= 0 ? num_ + 0.5 : num_ - 0.5)
                                 : fallback;
  }
  const std::string& str() const { return str_; }
  std::string str_or(const std::string& fallback) const {
    return type_ == Type::String ? str_ : fallback;
  }

  Array& arr() { return arr_; }
  const Array& arr() const { return arr_; }
  Object& obj() { return obj_; }
  const Object& obj() const { return obj_; }

  // Object access. get() never inserts; operator[] on a non-const object does,
  // like std::map, and turns a Null value into an Object first.
  bool contains(const std::string& key) const {
    return type_ == Type::Object && obj_.find(key) != obj_.end();
  }
  const Json& get(const std::string& key) const {
    static const Json null;
    if (type_ != Type::Object) return null;
    auto it = obj_.find(key);
    return it == obj_.end() ? null : it->second;
  }
  Json& operator[](const std::string& key) {
    if (type_ == Type::Null) { type_ = Type::Object; }
    return obj_[key];
  }

  size_t size() const {
    if (type_ == Type::Array) return arr_.size();
    if (type_ == Type::Object) return obj_.size();
    return 0;
  }

  bool operator==(const Json& o) const;

  // indent < 0: compact single line. indent >= 0: pretty, that many spaces.
  std::string dump(int indent = -1) const;

  // Returns a Null value and sets *err on malformed input.
  static Json parse(const std::string& text, std::string* err = nullptr);

 private:
  void write(std::string& out, int indent, int depth) const;

  Type type_;
  bool bool_ = false;
  double num_ = 0.0;
  std::string str_;
  Array arr_;
  Object obj_;
};

}  // namespace bs
