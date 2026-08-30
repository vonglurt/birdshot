# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
"""Launch the birdshot GUI on the Pi's display.

With --auto (which is what the desktop autostart entry uses), the GUI looks for
a USB stick carrying an ``autowrite.yes`` file. If it finds one it points
capture at that stick, starts shooting immediately and copies frames across on a
timer -- no clicks required. Without such a stick it just opens normally.
"""

import argparse
import os
import sys


def _apply_dark_palette(app) -> None:
    """Unify the chrome on the palette the custom controls already use.

    The accordion bars, mode tuner, HUD and preview were always dark slate
    and steel; Fusion's stock grey around them made the window read as two
    programs. This is those same hex values, applied to everything.
    """
    from PyQt5.QtGui import QColor, QPalette

    p = QPalette()
    c = QColor
    p.setColor(QPalette.Window, c("#1b1f24"))
    p.setColor(QPalette.WindowText, c("#cfe3ef"))
    p.setColor(QPalette.Base, c("#232830"))
    p.setColor(QPalette.AlternateBase, c("#20252b"))
    p.setColor(QPalette.Text, c("#cfe3ef"))
    p.setColor(QPalette.Button, c("#232830"))
    p.setColor(QPalette.ButtonText, c("#cfe3ef"))
    p.setColor(QPalette.BrightText, c("#ffffff"))
    p.setColor(QPalette.Link, c("#4da3cc"))
    p.setColor(QPalette.Highlight, c("#2f6f8f"))
    p.setColor(QPalette.HighlightedText, c("#ffffff"))
    p.setColor(QPalette.ToolTipBase, c("#25303a"))
    p.setColor(QPalette.ToolTipText, c("#cfe3ef"))
    try:
        p.setColor(QPalette.PlaceholderText, c("#7a8791"))
    except AttributeError:   # Qt < 5.12 on the older Pi image
        pass
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c("#616b76"))
    p.setColor(QPalette.Disabled, QPalette.Highlight, c("#39414c"))
    p.setColor(QPalette.Disabled, QPalette.HighlightedText, c("#93a3ad"))
    app.setPalette(p)
    app.setStyleSheet(
        "QToolTip{background:#25303a;color:#cfe3ef;border:1px solid #39414c;}")


def main() -> int:
    ap = argparse.ArgumentParser(description="birdshot capture GUI")
    ap.add_argument("--config", help="path to settings.json")
    ap.add_argument("--data-root", help="override the capture root")
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"),
                    help="X display to open on (default :0)")
    ap.add_argument("--auto", action="store_true",
                    help="honour an autowrite.yes USB stick and start capturing")
    ap.add_argument("--tab", help="open on a named tab, e.g. Scene or Machine")
    ap.add_argument("--face",
                    choices=["auto", "camera", "field", "bench", "library"],
                    default="auto",
                    help="which face to open on (default: config ui_face, "
                         "then auto-resolve per install)")
    ap.add_argument("--no-maximize", action="store_true",
                    help="open windowed instead of filling the screen")
    ap.add_argument("--fullscreen", action="store_true",
                    help="open with no window decorations at all")
    ap.add_argument("--backend", choices=["auto", "picamera2", "opencv", "synthetic"],
                    help="camera backend (default: config, then auto)")
    ap.add_argument("--screenshot", metavar="PATH",
                    help="save a window grab a few seconds after startup and "
                         "exit -- for CI and docs")
    args = ap.parse_args()

    os.environ.setdefault("DISPLAY", args.display)
    # The CM4 has no usable GL for Qt over a remote session; software raster is
    # both faster and far more reliable here than the vc4 GL path.
    os.environ.setdefault("QT_QUICK_BACKEND", "software")

    from PyQt5.QtWidgets import QApplication

    from birdshot import autostart, backends
    from birdshot.config import Config
    from birdshot.gui import MainWindow
    from birdshot.storage import Storage

    cfg = Config(args.config) if args.config else Config()
    if args.data_root:
        cfg["data_root"] = args.data_root
    # Applied to config (not passed per-call) so the in-GUI camera selector
    # can change it later without the flag overriding every rebuild.
    if args.backend:
        cfg["backend"] = args.backend

    auto_summary = None
    if args.auto:
        found = autostart.detect()
        if found:
            auto_summary = autostart.apply(cfg, found)
            print(autostart.describe(auto_summary), flush=True)
        else:
            print("--auto: no %s found on any mounted volume, "
                  "starting normally" % autostart.MARKER, flush=True)
    cfg.save()

    storage = Storage(cfg)
    backends.warm_up(cfg)   # macOS camera-permission dialog needs main thread

    app = QApplication(sys.argv)
    app.setApplicationName("birdshot")
    app.setStyle("Fusion")
    _apply_dark_palette(app)

    window = MainWindow(cfg, storage,
                        lambda on_event: backends.create_engine(
                            cfg, storage, on_event),
                        auto=auto_summary,
                        face=None if args.face == "auto" else args.face)
    if args.tab and not window.select_tab(args.tab):
        print("no tab named %r" % args.tab, flush=True)

    if args.screenshot:
        from PyQt5.QtCore import QTimer as _ShotTimer

        def _grab_and_quit():
            ok = window.grab().save(args.screenshot)
            print("screenshot%s: %s" % ("" if ok else " FAILED", args.screenshot),
                  flush=True)
            window.close()
            app.quit()

        _ShotTimer.singleShot(5000, _grab_and_quit)

    # Unattended runs get killed rather than closed -- a power-down, a reboot,
    # or a plain `pkill`. Qt never runs closeEvent for those, which would leave
    # the session unclosed and the final copy to USB undone. Catch the signals
    # and shut down through the normal path instead.
    import signal

    def _terminate(signum, _frame):
        print("received signal %d, closing cleanly..." % signum, flush=True)
        window.close()
        app.quit()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _terminate)
        except (OSError, ValueError):
            pass
    # Python signal handlers only run between bytecodes, and Qt's event loop
    # blocks in C. A periodic no-op timer gives the interpreter a chance to run.
    from PyQt5.QtCore import QTimer as _QTimer

    _wake = _QTimer()
    _wake.timeout.connect(lambda: None)
    _wake.start(400)

    if args.fullscreen:
        window.showFullScreen()
    elif args.no_maximize:
        window.show()
    else:
        # Size to the screen first, so that un-maximizing lands somewhere sane
        # rather than at the default geometry.
        screen = app.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            window.resize(avail.width(), avail.height())
            window.move(avail.left(), avail.top())
        window.showMaximized()

    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
