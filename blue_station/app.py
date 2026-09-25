"""Starts Blue Station: python3 -m blue_station (--demo for made-up devices)"""
import argparse
import signal
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from blue_station.core import prefs
from blue_station.ui import icons, theme
from blue_station.ui.window import MainWindow


def stay_awake():
    """macOS's App Nap may slow a hidden app's timers, and the tracker watch
    has to keep running behind other windows. This is Apple's way of saying
    the work was asked for; the Mac can still sleep when idle. (Hidden for
    100 s, timers kept time with and without it: a safeguard for longer.)
    Returns a token to keep for as long as the app runs."""
    if sys.platform != "darwin":
        return None
    try:
        from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSProcessInfo
        return NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "Watching for Bluetooth devices and trackers")
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="blue-station", description="A live Bluetooth Low Energy scanner.")
    parser.add_argument("--demo", action="store_true", help="show made-up devices instead of scanning")
    args, qt_args = parser.parse_known_args()

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a terminal quits, even inside Qt's loop
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Blue Station")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(icons.pixmap("logo", "#000000", 256, dpr=1.0)))
    settings = prefs.load()
    theme.set_appearance(settings.get("appearance", theme.DEFAULT_APPEARANCE))
    theme.apply(app)

    if args.demo:
        from blue_station.core.scanner import DemoScanner
        scanner = DemoScanner()
    else:
        from blue_station.core.scanner import Scanner
        scanner = Scanner()
    window = MainWindow(scanner, settings, demo=args.demo)
    window.show()
    activity = stay_awake()
    code = app.exec()
    del activity  # held until here: the app no longer needs to stay awake
    sys.exit(code)
