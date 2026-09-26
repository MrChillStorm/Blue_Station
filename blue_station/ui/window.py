"""The window: a slim header with the four jobs, the scan button and a
menu; one timer that feeds the scanner's packets to everything that
wants them. Each job is its own page; a device's own page opens from any
of them and goes back to where it came from. The tracker watch runs
whichever job is on screen, and with the menu bar on, it keeps running
after the window is closed."""
import csv
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QActionGroup, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton,
    QStackedWidget, QStatusBar, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from blue_station.core import login, names, prefs
from blue_station.core.alerts import GONE, Alerts
from blue_station.core.links import Linker
from blue_station.core.devices import Device, DeviceStore, span_text
from blue_station.core.packets import PacketLog
from blue_station.core.watch import DEFAULT_GROUPS, FOLLOWING, GROUPS, Watcher
from blue_station.ui import icons, theme
from blue_station.ui.develop import DevelopPage
from blue_station.ui.devices import DevicesPage
from blue_station.ui.menubar import MenuBar
from blue_station.ui.survey import SurveyPage
from blue_station.ui.track import TrackPage
from blue_station.ui.trackers import TrackersPage, timeline
from blue_station.ui.widgets import ScanIndicator, Segmented, dbm, refresh_tool_icons, tool_button

SCAN, TRACKERS, SURVEY, DEVELOP = range(4)
JOBS = ["Scan", "Trackers", "Survey", "Develop"]
JOB_TIPS = [
    "Every device around you, live (⌘1)",
    "Item trackers around you, and whether one is following you (⌘2)",
    "Check beacons and map coverage on a floor plan (⌘3)",
    "Packets, timing, a packet log and GATT for one device (⌘4)",
]
TICK_MS = 200
SAVE_WATCH_EVERY = 60

HELP = """<h3>Blue Station</h3>
<p>Pick the job at the top. Each shows only what that job needs:</p>
<p><b>Scan</b>: every Bluetooth Low Energy device around you, with a live signal bar each. Hover one for its
details and last minute of signal.</p>
<p><b>Trackers</b>: AirTags and other item trackers, and whether one is following you, meaning it has been
with you in two different places. It watches in the background whichever job you're in. <b>Watch for</b> adds
other kinds of device, and <b>This is mine</b> on a device's page leaves one of yours alone.</p>
<p><b>Survey</b>: checks beacons (a silent one shows up as gone quiet) and maps coverage on a floor plan.
Click where you stand and hold still for five seconds.</p>
<p><b>Develop</b>: one device, the way its firmware's author sees it: packet timing, payload changes, a packet
log that records only when you press Record, and a GATT explorer with live notifications.</p>
<p>Click a device in any job for its own page: a big live signal with its trend (walk toward something you've
lost), rough distance, history and the decoded advertisement. Its <b>speaker</b> beeps faster as you get closer,
and its <b>bell</b> tells you when it goes out of range or comes back.</p>
<p>Closing the window leaves Blue Station watching in the menu bar. Quit from its menu there, or with ⌘Q.</p>
<p>Distances are rough. Many phones change their Bluetooth address every few minutes for privacy, so one phone
can show up as several devices over time.</p>
<table cellpadding='2'>
<tr><td><b>⌘1 – ⌘4</b></td><td>Scan, Trackers, Survey, Develop</td></tr>
<tr><td><b>Space</b></td><td>Scan or pause</td></tr>
<tr><td><b>⌘F</b></td><td>Filter the list</td></tr>
<tr><td><b>Enter</b></td><td>Open the selected device</td></tr>
<tr><td><b>Esc</b></td><td>Back from a device, or clear the filter</td></tr>
<tr><td><b>⌘Z</b></td><td>Take back the last survey point</td></tr>
<tr><td><b>⌘E</b></td><td>Export what's on screen</td></tr>
</table>"""


URGENT, GENTLE, NOTICE = "Sosumi", "Glass", "Ping"  # macOS sounds: left behind or followed; back; new


def notification_script(title: str, text: str, sound: str | None = None) -> str:
    script = f"display notification {json.dumps(text, ensure_ascii=False)} with title " \
             f"{json.dumps(title, ensure_ascii=False)}"
    return script + (f" sound name {json.dumps(sound)}" if sound else "")


def notify(title: str, text: str, sound: str | None = None) -> None:
    """A system notification, for alerts that matter while the window is
    in the background, with one of macOS's own sounds if given. macOS
    files these under Script Editor in its notification settings."""
    if sys.platform == "darwin":
        script = notification_script(title, text, sound)
        try:
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


class MainWindow(QMainWindow):
    def __init__(self, scanner, settings: dict | None = None, demo: bool = False):
        super().__init__()
        self.scanner = scanner
        self.settings = prefs.load() if settings is None else settings
        self.demo = demo
        self.store = DeviceStore(self.settings.get("known"))
        groups = [g for g in self.settings.get("watch_for", DEFAULT_GROUPS) if g in GROUPS] or DEFAULT_GROUPS
        self.watcher = Watcher(scanner.watch_history(time.time()) if demo else prefs.load(prefs.TRACKERS), groups)
        self.log = PacketLog()
        self.alerts = Alerts()
        self.linker = Linker(None if demo else self.settings.get("learned_bytes"))
        self.menubar: MenuBar | None = None
        self._quitting = False
        self._hidden_at: float | None = None  # when it was closed to the menu bar
        self.job = SCAN
        self._watch_saved = time.time()
        self.resize(1320, 840)
        self.setMinimumSize(1100, 680)

        central = QWidget()
        central.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        self.pages = QStackedWidget()
        self.devices = DevicesPage(self.store)
        self.trackers = TrackersPage(self.watcher, self.store)
        self.survey = SurveyPage(self.store)
        self.develop = DevelopPage(self.store, self.scanner.connect, self.log)
        self.track = TrackPage(self.scanner.read_gatt)
        self.track.watch_text = self._watch_text
        self.track.mine_state = self._mine_state
        for page in (self.devices, self.trackers, self.survey, self.develop, self.track):
            self.pages.addWidget(page)
            page.message.connect(self._status)
        for page in (self.devices, self.trackers, self.survey, self.develop):
            page.track.connect(self.show_device)
        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 4)
        body.addWidget(self.pages)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self.devices.pinChanged.connect(self._remember)
        self.track.changed.connect(self._remember)
        self.track.back.connect(self.back)
        self.trackers.changed.connect(self._save_watch)
        self.trackers.groupsChanged.connect(self._watch_for)
        self.track.mineToggled.connect(self._toggle_mine)
        self.views.changed.connect(self.show_job)
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.apply_theme())
        self._shortcuts()

        self.devices.gone.setChecked(bool(self.settings.get("show_gone")))
        low, high = self.devices.near_slider.values()
        self.devices.near_slider.setValues(int(self.settings.get("nearby_dbm", low)),
                                           int(self.settings.get("nearby_max", high)))
        self.devices.nearby.setChecked(bool(self.settings.get("nearby_only")))
        self.trackers.gone.setChecked(bool(self.settings.get("trackers_show_gone")))
        geometry = self.settings.get("geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
        self.apply_theme()
        self.show_job(min(max(int(self.settings.get("job", SCAN)), SCAN), DEVELOP))
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.scanner.start()
        self._set_menu_bar(bool(self.settings.get("menu_bar", True)))
        self.tick()

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 10, 12, 10)
        row.setSpacing(10)
        self.logo = QLabel()
        self.logo.setPixmap(icons.pixmap("logo", "#000000", 26))
        name = QLabel("Blue Station")
        name.setObjectName("title")
        self.views = Segmented(JOBS, JOB_TIPS)
        self.indicator = ScanIndicator()
        self.scan_btn = QPushButton("Pause")
        self.scan_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_btn.setToolTip("Scan or pause (Space)")
        self.scan_btn.setMinimumWidth(84)
        self.scan_btn.clicked.connect(self.toggle_scan)
        self.menu_btn = tool_button("gear", "Appearance, export and help")
        self.menu_btn.setPopupMode(self.menu_btn.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setMenu(self._build_menu())
        row.addWidget(self.logo)
        row.addWidget(name)
        if self.demo:
            demo = QLabel("DEMO")
            demo.setObjectName("caps")
            demo.setToolTip("Made-up devices: started with --demo")
            row.addWidget(demo)
        row.addStretch(1)
        row.addWidget(self.views)
        row.addStretch(1)
        row.addWidget(self.indicator)
        row.addWidget(self.scan_btn)
        row.addWidget(self.menu_btn)
        return header

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        looks = menu.addMenu("Appearance")
        group = QActionGroup(self)
        self.appearance_actions = {}
        for key, text in theme.APPEARANCES.items():
            action = looks.addAction(text)
            action.setCheckable(True)
            action.setChecked(key == self.settings.get("appearance", theme.DEFAULT_APPEARANCE))
            action.triggered.connect(lambda _=False, k=key: self.set_appearance(k))
            group.addAction(action)
            self.appearance_actions[key] = action
        menu.addSeparator()
        self.menu_bar_action = menu.addAction("Keep watching in the menu bar")
        self.menu_bar_action.setCheckable(True)
        self.menu_bar_action.setChecked(bool(self.settings.get("menu_bar", True)))
        self.menu_bar_action.setToolTip("Closing the window leaves Blue Station in the menu bar, still watching for "
                                        "trackers and your alerts")
        self.menu_bar_action.toggled.connect(self._menu_bar_toggled)
        self.login_action = None
        if login.supported():
            self.login_action = menu.addAction("Start at login")
            self.login_action.setCheckable(True)
            self.login_action.setChecked(login.enabled())
            self.login_action.setToolTip("Start Blue Station in the menu bar when you log in")
            self.login_action.toggled.connect(self.set_start_at_login)
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        menu.addAction("Export device list…", self.export_devices_dialog)
        menu.addAction("Clear list", lambda: self.devices.clear())
        menu.addSeparator()
        menu.addAction("Help", lambda: QMessageBox.information(self, "Blue Station", HELP))
        return menu

    def _shortcuts(self) -> None:
        for keys, slot in (
            ("Space", self.toggle_scan),
            ("Ctrl+F", self.focus_search),
            ("Escape", self.escape),
            ("Ctrl+1", lambda: self.show_job(SCAN)),
            ("Ctrl+2", lambda: self.show_job(TRACKERS)),
            ("Ctrl+3", lambda: self.show_job(SURVEY)),
            ("Ctrl+4", lambda: self.show_job(DEVELOP)),
            ("Ctrl+E", self.export),
            ("Ctrl+Z", lambda: self.on_page(self.survey) and self.survey.undo()),
        ):
            QShortcut(QKeySequence(keys), self, slot)

    # ---- jobs and pages ---------------------------------------------------------------

    def on_page(self, page: QWidget) -> bool:
        return self.pages.currentWidget() is page

    @property
    def job_page(self) -> QWidget:
        return (self.devices, self.trackers, self.survey, self.develop)[self.job]

    def show_job(self, job: int) -> None:
        self.job = job
        self.views.set_current(job)
        self.pages.setCurrentWidget(self.job_page)
        self.setWindowTitle(f"Blue Station – {JOBS[job]}")
        if job == SCAN:
            self.devices.table.setFocus()
        elif job == TRACKERS:
            self.centralWidget().setFocus()  # not the filter: Space should still pause
        self.tick()

    def show_device(self, device: Device) -> None:
        self.track.set_origin(JOBS[self.job])
        self.track.show_device(device)
        self.pages.setCurrentWidget(self.track)
        self.tick()

    def back(self) -> None:
        self.show_job(self.job)

    def focus_search(self) -> None:
        if self.job == TRACKERS:
            self.show_job(TRACKERS)
            self.trackers.search.setFocus()
            self.trackers.search.selectAll()
            return
        if self.job == DEVELOP:
            self.show_job(DEVELOP)
            self.develop.search.setFocus()
            self.develop.search.selectAll()
            return
        self.show_job(SCAN)
        self.devices.search.setFocus()
        self.devices.search.selectAll()

    def escape(self) -> None:
        if self.on_page(self.track):
            if not self.track.cancel_rename():
                self.back()
        elif self.on_page(self.devices) and self.devices.search.text():
            self.devices.search.clear()
            self.devices.table.setFocus()
        elif self.on_page(self.trackers) and self.trackers.search.text():
            self.trackers.search.clear()

    # ---- scanning and the timer ---------------------------------------------------------

    def toggle_scan(self) -> None:
        if self.scanner.state in ("scanning", "starting"):
            self.scanner.stop()
        else:
            self.scanner.start()
        self.tick()

    def tick(self) -> None:
        now = time.time()
        sightings = self.scanner.drain()
        if sightings:
            self.store.ingest(sightings)
            self.log.add(sightings)
        devices = list(self.store.devices.values())
        for old, new, sure in self.linker.update(now, devices):
            self._hand_over(old, new, sure)
        for record in self.watcher.update(now, devices):
            self._alert(record)
        for device, what in self.alerts.update(now, devices, self.scanner.state == "scanning"):
            self._device_alert(device, what)
        for device in self.devices.arrivals(now):
            self._new_device_alert(device)
        if not self.demo and now - self._watch_saved >= SAVE_WATCH_EVERY:
            self._save_watch()
        if self.survey.survey.update(now, devices):
            self._status(f"Point {len(self.survey.survey.points)} added.")
        self._follow_badge()
        if self.menubar is not None:
            self.menubar.show_state(len(self.watcher.following()), self.scanner.state)

        state, error = self.scanner.state, self.scanner.error
        text = {"scanning": "Scanning", "starting": "Starting…", "error": "Not scanning"}.get(state, "Paused")
        self.indicator.set_state(state, error if state == "error" and error else text)
        running = state in ("scanning", "starting")
        if self.scan_btn.property("primary") != (not running):
            self.scan_btn.setText("Pause" if running else "Scan")
            self.scan_btn.setProperty("primary", not running)
            self.scan_btn.style().unpolish(self.scan_btn)
            self.scan_btn.style().polish(self.scan_btn)

        if self.on_page(self.track):
            current = self.store.devices.get(self.track.device.address)
            if current is not None and current is not self.track.device:
                self.track.show_device(current)  # cleared from the list, then heard again
            self.track.tick(now)
            self.setWindowTitle(f"Blue Station – {self.track.device.title}")
        elif self.on_page(self.devices):
            self.devices.tick(now, state, error)
        else:
            self.pages.currentWidget().tick(now)

    # ---- the tracker watch --------------------------------------------------------------

    def _alert(self, record) -> None:
        places = len(record.places)
        text = f"A {record.kind} may be following you: it has been with you in {places} different places."
        self._status(text)
        if not self.demo and QGuiApplication.platformName() != "offscreen":
            notify("Blue Station", text, URGENT)
        self._save_watch()

    def _hand_over(self, old: Device, new: Device, sure: float) -> None:
        """A device changed its Bluetooth address: everything about it
        carries on under the new one."""
        self.store.hand_over(old, new, sure)
        self.watcher.hand_over(old.address, new.address)
        self.alerts.hand_over(old.address, new.address)
        if not self.demo and new.address in self.store.known:
            self.settings["known"] = self.store.known
            self._save()
        if self.on_page(self.track) and self.track.device is old:
            self.track.show_device(new)  # the page follows the device

    def _device_alert(self, device: Device, what: str) -> None:
        """One you asked for on the device's page: out of range, or back."""
        text = (f"{device.title} is out of range. Did you leave it behind?" if what == GONE
                else f"{device.title} is back in range.")
        self._status(text)
        if QGuiApplication.platformName() != "offscreen":
            notify("Blue Station", text, URGENT if what == GONE else GENTLE)

    def _new_device_alert(self, device: Device) -> None:
        """New only's bell: something turned up that wasn't around before."""
        text = f"New device: {device.title}, at {dbm(device.smoothed)} dBm."
        self._status(text)
        if QGuiApplication.platformName() != "offscreen":
            notify("Blue Station", text, NOTICE)

    def _follow_badge(self) -> None:
        count = len(self.watcher.following())
        button = self.views.button(TRACKERS)
        text = f"Trackers  {count}" if count else "Trackers"
        if button.text() != text:
            button.setText(text)
            button.setProperty("alert", bool(count))
            button.style().unpolish(button)
            button.style().polish(button)

    def _watch_text(self, device: Device) -> str | None:
        if device.address in self.watcher.mine:
            return "You said this is yours, so the tracker watch leaves it alone."
        record = self.watcher.record(device.address)
        if record is None:
            return None
        text = f"Blue Station has heard it with you for {span_text(record.seen_seconds)}"
        text += f", in {len(record.places)} different places." if len(record.places) > 1 else "."
        if record.verdict == FOLLOWING:
            text = ("It may be following you. " + text + " To find it, walk around with this page open: the "
                    "signal gets stronger as you get closer.")
        if record.visits:
            text += "\n\nWith you: " + timeline(self.watcher, record, time.time())
        return text

    def _mine_state(self, device: Device) -> bool | None:
        """True: you said it's yours. False: it's watched, and could be. None:
        not something the watch looks at."""
        if device.address in self.watcher.mine:
            return True
        return False if self.watcher.watches(device.info) else None

    def _toggle_mine(self, device: Device) -> None:
        mine = device.address not in self.watcher.mine
        self.watcher.set_mine(device.address, mine)
        self._save_watch()
        self._status("Marked as yours: the tracker watch leaves it alone. A device that changes its Bluetooth "
                     "address comes back as a new one." if mine else "The tracker watch watches it again.")
        self.tick()

    def _watch_for(self, groups: list[str]) -> None:
        self.settings["watch_for"] = groups
        self._save()

    def _save_watch(self) -> None:
        self._watch_saved = time.time()
        if self.demo:
            return
        try:
            prefs.save(self.watcher.to_dict(), prefs.TRACKERS)
        except OSError as exc:
            self._status(f"Couldn't save the tracker history: {exc}")

    # ---- remembering ------------------------------------------------------------------

    def _remember(self, device: Device) -> None:
        self.store.remember(device)
        if not self.demo:  # made-up devices aren't worth remembering
            self.settings["known"] = self.store.known
            self._save()

    def _save(self) -> None:
        try:
            prefs.save(self.settings)
        except OSError as exc:
            self._status(f"Couldn't save settings: {exc}")

    # ---- export -------------------------------------------------------------------------

    def export(self) -> None:
        page = self.pages.currentWidget()
        if page in (self.track, self.survey, self.develop):
            page.export_dialog()
        else:
            self.export_devices_dialog()

    def export_devices_dialog(self) -> None:
        suggested = str(Path.home() / f"Bluetooth devices {datetime.now():%Y-%m-%d %H%M}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export device list", suggested, "CSV (*.csv)")
        if path:
            self.export_devices(Path(path))

    def export_devices(self, path: Path) -> None:
        now = time.time()
        devices = sorted(self.store.devices.values(), key=lambda d: d.first_seen)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["address", "bluetooth_address", "name", "your_name", "kind", "maker", "rssi_dbm",
                             "min_dbm_1min", "max_dbm_1min", "distance_m", "packets", "first_seen", "last_seen",
                             "connectable", "tx_power_dbm", "services", "manufacturer_data", "decoded"])
            for d in devices:
                stats = d.stats(now, 60)
                distance = d.distance()
                writer.writerow([
                    d.address, d.mac or "", d.name or "", d.nickname or "", d.info.kind or "", d.info.vendor or "",
                    "" if d.smoothed is None else round(d.smoothed), stats.low if stats else "",
                    stats.high if stats else "", "" if distance is None else f"{distance:.1f}", d.packets,
                    datetime.fromtimestamp(d.first_seen).isoformat(timespec="seconds"),
                    datetime.fromtimestamp(d.last_seen).isoformat(timespec="seconds"),
                    {True: "yes", False: "no", None: ""}[d.connectable],
                    "" if d.tx_power is None else d.tx_power,
                    "; ".join(names.service_label(u) for u in d.service_uuids),
                    "; ".join(f"0x{cid:04X}:{data.hex()}" for cid, data in d.manufacturer_data.items()),
                    "; ".join(f"{k}: {v}" for k, v in d.info.fields),
                ])
        self._status(f"Exported {len(devices)} devices to {path.name}.")

    # ---- chrome ---------------------------------------------------------------------------

    def _status(self, text: str) -> None:
        self.statusBar().showMessage(text, 8000)

    def set_appearance(self, key: str) -> None:
        self.settings["appearance"] = key
        theme.set_appearance(key)
        self.apply_theme()
        self._save()

    def apply_theme(self) -> None:
        theme.apply(QApplication.instance())
        refresh_tool_icons(self.menu_btn)
        refresh_tool_icons(self.devices.new_bell, color_key="accent" if self.devices.new_bell.isChecked() else "muted")
        for page in (self.track, self.survey, self.develop):
            page.refresh_icons()
        for search in (self.devices.search, self.trackers.search):
            search.actions()[0].setIcon(icons.icon("search", theme.colors()["faint"], 16))
        for view in (self.devices.table, self.trackers.table, self.survey.table, self.develop.table):
            view.viewport().update()
        self.indicator.update()

    # ---- the menu bar -------------------------------------------------------------------

    def _set_menu_bar(self, on: bool) -> None:
        on = on and QSystemTrayIcon.isSystemTrayAvailable()
        if on and self.menubar is None:
            self.menubar = MenuBar(self)
            self.menubar.set_login(login.supported() and login.enabled())
            self.menubar.show()
        elif not on and self.menubar is not None:
            self.menubar.hide()
            self.menubar.deleteLater()
            self.menubar = None
            if not self.isVisible():
                self.bring_back()

    def _menu_bar_toggled(self, on: bool) -> None:
        self.settings["menu_bar"] = on
        self._save()
        self._set_menu_bar(on)
        if on and self.menubar is None:
            self._status("This system has no menu bar or tray for Blue Station: closing the window quits.")

    def set_start_at_login(self, on: bool) -> None:
        try:
            login.enable() if on else login.disable()
        except OSError as exc:
            self._status(f"Couldn't change starting at login: {exc}")
        on = login.enabled()
        if self.login_action is not None:
            self.login_action.blockSignals(True)
            self.login_action.setChecked(on)
            self.login_action.blockSignals(False)
        if self.menubar is not None:
            self.menubar.set_login(on)
        where = ", in the menu bar" if self.menubar is not None else ""
        self._status(f"Blue Station starts when you log in{where}. The first time, macOS may ask about Bluetooth "
                     "again." if on else "Blue Station no longer starts when you log in.")

    def hide_to_menu_bar(self, tell: bool = True) -> None:
        """The window goes; the watching stays."""
        for page in (self.devices, self.trackers, self.survey, self.develop):
            page.hover.hide()
        self.develop.disconnect()  # no need to keep a device connected for nobody
        if self.isVisible():  # started at login, it never was: keep the window's last size and place
            self._keep_settings()
        self.hide()
        self._hidden_at = time.time()
        self._step_aside(True)
        if tell and not self.settings.get("menu_bar_told"):
            self.settings["menu_bar_told"] = True
            self._save()
            if QGuiApplication.platformName() != "offscreen":
                notify("Blue Station", "Still watching in the menu bar. Quit from its menu there.")

    def bring_back(self) -> None:
        self._hidden_at = None
        self._step_aside(False)
        self.show()
        self.raise_()
        self.activateWindow()
        self.tick()

    def quit_app(self) -> None:
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    def _step_aside(self, hidden: bool) -> None:
        """macOS: closed to the menu bar, the app hides like ⌘H, so clicking it
        in the Dock (or opening it again) brings the window back. Its Dock
        icon stays: taking the app out of the Dock at runtime lost the menu
        bar icon too."""
        if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
            return
        try:
            from AppKit import NSApplication
            app = NSApplication.sharedApplication()
            if hidden:
                app.hide_(None)
            else:
                app.unhide_(None)
                app.activateIgnoringOtherApps_(True)
        except Exception:
            pass

    def watch_app(self, app: QApplication) -> None:
        """Called once by the app: to tell quitting from closing, and to
        notice being opened again."""
        app.installEventFilter(self)
        app.applicationStateChanged.connect(self._app_state)

    def _app_state(self, state) -> None:
        """Clicking Blue Station in the Dock (or opening it again) while it's in
        the menu bar brings the window back."""
        if (state == Qt.ApplicationState.ApplicationActive and self._hidden_at is not None
                and time.time() - self._hidden_at > 1 and not self._quitting):
            self.bring_back()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Quit:  # ⌘Q, logging out: really quit, not just hide
            self._quitting = True
        return False

    # ---- closing --------------------------------------------------------------------------

    def _keep_settings(self) -> None:
        self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.settings["show_gone"] = self.devices.gone.isChecked()
        self.settings["nearby_only"] = self.devices.nearby.isChecked()
        self.settings["nearby_dbm"], self.settings["nearby_max"] = self.devices.near_slider.values()
        self.settings["trackers_show_gone"] = self.trackers.gone.isChecked()
        self.settings["job"] = self.job
        if not self.demo:
            self.settings["known"] = self.store.known
            self.settings["learned_bytes"] = self.linker.to_dict()
        self._save()
        self._save_watch()

    def closeEvent(self, event) -> None:
        if self.menubar is not None and not self._quitting:
            event.ignore()
            self.hide_to_menu_bar()
            return
        self.timer.stop()
        for page in (self.devices, self.trackers, self.survey, self.develop):
            page.hover.hide()
        self.develop.disconnect()
        self._keep_settings()
        self.scanner.close()
        if self.menubar is not None:
            self.menubar.hide()
        event.accept()
