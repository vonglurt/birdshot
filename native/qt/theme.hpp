// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The 1.x look, verbatim: the Fusion dark palette and the slate/steel
// color vocabulary every face shares. One place, so the port cannot drift
// shade by shade from the prototype it is replacing.
#pragma once

#include <QApplication>
#include <QColor>
#include <QFont>
#include <QPalette>

namespace theme {

// faces.py module constants.
constexpr const char* kPass = "#7fe3a2";
constexpr const char* kFail = "#ff6a44";
constexpr const char* kNa = "#6a7480";
inline QColor verdictColor(const QString& v) {
  if (v == "ok") return QColor("#5fd07a");
  if (v == "dark") return QColor("#5aa0ff");
  if (v == "blown") return QColor("#ff6a44");
  if (v == "empty") return QColor("#e0a828");
  return QColor("#93a3ad");
}

// The prototype names DejaVu everywhere; macOS has neither, so give the
// families a fallback chain and let Qt resolve per platform.
inline QFont sans(int px, bool bold = false) {
  QFont f(QStringLiteral("DejaVu Sans"));
  f.setStyleHint(QFont::SansSerif);
  f.setPixelSize(px);
  f.setBold(bold);
  return f;
}
inline QFont mono(int px, bool bold = false) {
  QFont f(QStringLiteral("DejaVu Sans Mono"));
  f.setStyleHint(QFont::Monospace);
  f.setPixelSize(px);
  f.setBold(bold);
  return f;
}

// app.py::_apply_dark_palette, color for color.
inline void applyDarkPalette(QApplication& app) {
  app.setStyle(QStringLiteral("Fusion"));
  QPalette p;
  p.setColor(QPalette::Window, QColor("#1b1f24"));
  p.setColor(QPalette::WindowText, QColor("#cfe3ef"));
  p.setColor(QPalette::Base, QColor("#232830"));
  p.setColor(QPalette::AlternateBase, QColor("#20252b"));
  p.setColor(QPalette::Text, QColor("#cfe3ef"));
  p.setColor(QPalette::Button, QColor("#232830"));
  p.setColor(QPalette::ButtonText, QColor("#cfe3ef"));
  p.setColor(QPalette::BrightText, QColor("#ffffff"));
  p.setColor(QPalette::Link, QColor("#4da3cc"));
  p.setColor(QPalette::Highlight, QColor("#2f6f8f"));
  p.setColor(QPalette::HighlightedText, QColor("#ffffff"));
  p.setColor(QPalette::ToolTipBase, QColor("#25303a"));
  p.setColor(QPalette::ToolTipText, QColor("#cfe3ef"));
  p.setColor(QPalette::PlaceholderText, QColor("#7a8791"));
  p.setColor(QPalette::Disabled, QPalette::WindowText, QColor("#616b76"));
  p.setColor(QPalette::Disabled, QPalette::Text, QColor("#616b76"));
  p.setColor(QPalette::Disabled, QPalette::ButtonText, QColor("#616b76"));
  p.setColor(QPalette::Disabled, QPalette::Highlight, QColor("#39414c"));
  p.setColor(QPalette::Disabled, QPalette::HighlightedText, QColor("#93a3ad"));
  app.setPalette(p);
  app.setStyleSheet(
      QStringLiteral("QToolTip{background:#25303a;color:#cfe3ef;border:1px solid #39414c;}"));
}

}  // namespace theme
