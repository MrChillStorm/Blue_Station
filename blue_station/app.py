"""Starts Blue Station: python3 -m blue_station (--demo for made-up devices,
--background to start in the menu bar without a window)"""
import argparse
import getpass
import signal
import sys

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
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


def call_it_blue_station() -> None:
    """Started as Python, macOS would call the app Python in its menu and the
    Dock. Renaming it has to happen before Qt starts."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Blue Station"
    except Exception:
        pass


def already_running(name: str, show: bool) -> bool:
    """True when Blue Station is running already. Then, unless starting in
    the background, it's asked to show its window."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(500):
        return False
    if show:
        socket.write(b"show")
        socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def listen(name: str, window: MainWindow) -> QLocalServer:
    """Another start of Blue Station brings this one's window back."""
    server = QLocalServer(window)
    QLocalServer.removeServer(name)  # one left over from a crash
    server.listen(name)

    def knock():
        socket = server.nextPendingConnection()

        def read():
            if socket.readAll().data() == b"show":
                window.bring_back()
        socket.readyRead.connect(read)
        socket.disconnected.connect(socket.deleteLater)
        if socket.bytesAvailable():  # it may have arrived already
            read()
    server.newConnection.connect(knock)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(prog="blue-station", description="A live Bluetooth Low Energy scanner.")
    parser.add_argument("--demo", action="store_true", help="show made-up devices instead of scanning")
    parser.add_argument("--background", action="store_true",
                        help="start in the menu bar without a window (how starting at login does it)")
    args, qt_args = parser.parse_known_args()

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a terminal quits, even inside Qt's loop
    call_it_blue_station()
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Blue Station")
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(icons.pixmap("logo", "#000000", 256, dpr=1.0)))
    name = f"blue-station-{getpass.getuser()}" + ("-demo" if args.demo else "")
    if already_running(name, show=not args.background):
        sys.exit(0)
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
    window.watch_app(app)
    server = listen(name, window)
    if args.background and window.menubar is not None:
        window.hide_to_menu_bar(tell=False)
    else:
        window.show()
    activity = stay_awake()
    code = app.exec()
    del activity, server  # held until here: the app no longer needs to stay awake, or to be found
    sys.exit(code)
