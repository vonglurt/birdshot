// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// birdshot-gui -- the Qt Widgets front end over the native core. Startup
// mirrors the prototype's app.py: parse args, load the config, apply the
// Fusion dark palette, build the one window, resolve the boot face.
#include <cstdio>
#include <string>

#include <QApplication>
#include <QFileInfo>
#include <QCommandLineParser>
#include <QScreen>
#include <QTimer>

#include "birdshot/config.hpp"
#include "theme.hpp"
#include "window.hpp"

namespace {

// ui_face: auto resolves per install, most specific first: a developer
// tree gets the Bench; a Mac gets the Library (the darkroom); anything
// else gets the plain Camera app. (The Pi -> Field rule joins when the
// platform camera backends land.)
QString resolveFace(const bs::Config& cfg) {
  const QString configured =
      QString::fromStdString(cfg.str("ui_face", "auto")).trimmed().toLower();
  static const QStringList faces{QStringLiteral("camera"), QStringLiteral("field"),
                                 QStringLiteral("bench"), QStringLiteral("library")};
  if (faces.contains(configured)) return configured;
#if defined(__APPLE__)
  const QString fallback = QStringLiteral("library");
#else
  const QString fallback = QStringLiteral("camera");
#endif
  return QFileInfo(QCoreApplication::applicationDirPath() +
                   QStringLiteral("/../../.git")).isDir()
             ? QStringLiteral("bench")
             : fallback;
}

}  // namespace

int main(int argc, char** argv) {
  QApplication app(argc, argv);
  app.setApplicationName(QStringLiteral("birdshot"));
  theme::applyDarkPalette(app);

  QCommandLineParser parser;
  parser.setApplicationDescription(QStringLiteral("birdshot capture GUI"));
  parser.addHelpOption();
  const QCommandLineOption optConfig(QStringLiteral("config"),
                                     QStringLiteral("path to settings.json"),
                                     QStringLiteral("path"));
  const QCommandLineOption optDataRoot(QStringLiteral("data-root"),
                                       QStringLiteral("override the capture root"),
                                       QStringLiteral("path"));
  const QCommandLineOption optFace(
      QStringLiteral("face"),
      QStringLiteral("which face to open on: auto|camera|field|bench|library"),
      QStringLiteral("name"), QStringLiteral("auto"));
  const QCommandLineOption optTab(QStringLiteral("tab"),
                                  QStringLiteral("open on a named tab, e.g. Scene or Machine"),
                                  QStringLiteral("name"));
  const QCommandLineOption optNoMax(QStringLiteral("no-maximize"),
                                    QStringLiteral("open windowed instead of filling the screen"));
  const QCommandLineOption optFull(QStringLiteral("fullscreen"),
                                   QStringLiteral("open with no window decorations at all"));
  const QCommandLineOption optBackend(QStringLiteral("backend"),
                                      QStringLiteral("camera backend (default: config)"),
                                      QStringLiteral("name"));
  const QCommandLineOption optStart(
      QStringLiteral("start"),
      QStringLiteral("press START on the selected mode once the window is up -- unattended "
                     "runs and testing"));
  const QCommandLineOption optShot(
      QStringLiteral("screenshot"),
      QStringLiteral("save a window grab a few seconds after startup and exit -- for CI and "
                     "docs"),
      QStringLiteral("path"));
  parser.addOptions({optConfig, optDataRoot, optFace, optTab, optNoMax, optFull, optBackend,
                     optStart, optShot});
  parser.process(app);

  static bs::Config cfg(parser.isSet(optConfig) ? parser.value(optConfig).toStdString()
                                                : bs::default_config_path());
  if (parser.isSet(optDataRoot))
    cfg.set("data_root", bs::Json(parser.value(optDataRoot).toStdString()));
  // Written into the config, not passed per-call, so an in-GUI camera
  // selector can change it later without the flag overriding every rebuild.
  if (parser.isSet(optBackend))
    cfg.set("backend", bs::Json(parser.value(optBackend).toStdString()));
  cfg.save();

  const QString faceArg = parser.value(optFace).trimmed().toLower();
  const QString face = faceArg == QStringLiteral("auto") ? resolveFace(cfg) : faceArg;

  MainWindow window(cfg, face);

  if (parser.isSet(optTab) && !window.selectTab(parser.value(optTab)))
    std::fprintf(stderr, "no tab named '%s'\n", qPrintable(parser.value(optTab)));

  if (parser.isSet(optStart))
    QTimer::singleShot(1500, &window, [&window] { window.goClicked(); });

  if (parser.isSet(optShot)) {
    const QString path = parser.value(optShot);
    QTimer::singleShot(5000, &window, [&window, path] {
      const bool ok = window.grab().save(path);
      std::printf("screenshot%s: %s\n", ok ? "" : " FAILED", qPrintable(path));
      window.close();
      QApplication::quit();
    });
  }

  if (parser.isSet(optFull)) {
    window.showFullScreen();
  } else if (parser.isSet(optNoMax)) {
    window.show();
  } else {
    // Size to the screen first, so un-maximizing lands somewhere sane.
    if (QScreen* screen = app.primaryScreen()) window.setGeometry(screen->availableGeometry());
    window.showMaximized();
  }
  return app.exec();
}
