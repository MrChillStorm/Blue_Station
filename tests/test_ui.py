"""Interface tests, run offscreen on made-up devices and a throwaway
settings file: python3 -m unittest discover tests"""
import csv
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from blue_station.core import prefs
from blue_station.core.devices import Sighting
from blue_station.core.scanner import DemoScanner
from blue_station.core.survey import HOLD
from blue_station.ui import theme
from blue_station.ui.devices import DEVICE, PIN, SIGNAL
from blue_station.ui.window import DEVELOP, SCAN, SURVEY, TRACKERS, MainWindow

app = QApplication.instance() or QApplication([])


class WindowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["BLUE_STATION_SETTINGS"] = str(Path(self.tmp.name) / "settings.json")
        self.scanner = DemoScanner()
        self.window = MainWindow(self.scanner, {})
        self.window.timer.stop()
        self.feed(30)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        del os.environ["BLUE_STATION_SETTINGS"]
        self.tmp.cleanup()

    def feed(self, seconds: float) -> None:
        """Plays the demo room forward, ending now."""
        now = time.time()
        self.scanner._t0 = now - seconds
        self.scanner._last = now - seconds
        steps = int(seconds / 0.2)
        for i in range(1, steps + 1):
            self.window.store.ingest(self.scanner.drain(now - seconds + i * 0.2))
        self.window.devices.model.refresh(now, force=True)
        self.window.tick()

    def device(self, name: str):
        return next(d for d in self.window.store.devices.values() if d.name == name)


class DevicesTest(WindowCase):
    def test_list_is_sorted_by_signal_with_pins_on_top(self):
        model = self.window.devices.model
        self.assertGreater(len(model.rows), 8)
        signals = [d.smoothed for d in model.rows]
        self.assertEqual(signals, sorted(signals, reverse=True))
        self.window.devices.table.pinToggled.emit(self.device("Thermo Sensor 3A"))
        self.assertEqual(model.rows[0].name, "Thermo Sensor 3A")
        self.assertEqual(model.index(0, PIN).data(), "★")
        self.assertIn("SIGNAL", model.headerData(SIGNAL, Qt.Orientation.Horizontal))

    def test_pins_are_remembered(self):
        self.window.devices.table.pinToggled.emit(self.device("Desk Keyboard"))
        address = self.device("Desk Keyboard").address
        self.assertEqual(prefs.load()["known"][address], {"pinned": True})

    def test_sort_by_name(self):
        table = self.window.devices.table
        table.sortByColumn(DEVICE, Qt.SortOrder.AscendingOrder)
        titles = [d.title.lower() for d in self.window.devices.model.rows]
        self.assertEqual(titles, sorted(titles))

    def test_filter(self):
        page = self.window.devices
        page.search.setText("heart")
        self.assertEqual([d.name for d in page.model.rows], ["Fitness Band"])
        page.search.setText("apple")
        self.assertTrue(all("Apple" in (d.info.vendor or "") for d in page.model.rows))
        page.search.setText("nothing like this")
        self.window.tick()
        self.assertEqual(page.model.rows, [])
        self.assertIs(page.stack.currentWidget(), page.empty)
        self.window.escape()
        self.assertEqual(page.search.text(), "")

    def test_order_holds_still_while_frozen(self):
        model = self.window.devices.model
        before = [d.address for d in model.rows]
        model.frozen = True
        weakest = model.rows[-1]
        now = time.time() + 5
        for i in range(30):
            self.window.store.ingest([Sighting(weakest.address, -30, now + i * 0.05)])
        newcomer = Sighting("NEW-DEVICE", -35, now + 2, name="Newcomer")
        self.window.store.ingest([newcomer])
        model.refresh(now + 2)
        after = [d.address for d in model.rows]
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(after[-1], "NEW-DEVICE")
        model.frozen = False
        model.refresh(now + 10)
        self.assertIn(model.rows[0].address, (weakest.address, "NEW-DEVICE"))

    def test_gone_devices_hide_unless_asked(self):
        model = self.window.devices.model
        later = time.time() + 120
        self.window.store.ingest([Sighting("FRESH", -50, later, name="Fresh")])
        model.refresh(later, force=True)
        self.assertEqual([d.address for d in model.rows], ["FRESH"])
        self.window.devices.gone.setChecked(True)
        model.refresh(later, force=True)
        self.assertGreater(len(model.rows), 1)

    def test_subtitles(self):
        from blue_station.ui.widgets import subtitle
        store = self.window.store
        store.ingest([Sighting("S1", -60, time.time(), name="ECO16BT f5fcc6"),
                      Sighting("S2", -60, time.time(), manufacturer_data={0x004C: bytes.fromhex("100507180b3a2c")}),
                      Sighting("S3", -60, time.time(), name="Office iMac",
                               manufacturer_data={0x004C: bytes.fromhex("100507180b3a2c")})])
        self.assertEqual(subtitle(store.devices["S1"]), "Kind and maker not advertised")
        self.assertEqual(subtitle(store.devices["S2"]), "No name advertised")
        self.assertEqual(subtitle(store.devices["S3"]), "Computer  ·  Apple")

    def test_hover_card(self):
        page = self.window.devices
        device = page.model.rows[0]
        page.hover.card.show_device(device, time.time())
        page.hover.card.place(QPoint(100, 100))
        self.assertIn(device.title, page.hover.card.title.text())
        self.assertIn("Address", page.hover.card.details.text())

    def test_clear_and_export(self):
        path = Path(self.tmp.name) / "devices.csv"
        self.window.export_devices(path)
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), len(self.window.store.devices))
        self.assertTrue(any(r["kind"] == "iBeacon" for r in rows))
        self.window.devices.clear()
        self.assertEqual(self.window.store.devices, {})


class TrackTest(WindowCase):
    def open(self, name: str):
        device = self.device(name)
        self.window.show_device(device)
        self.window.tick()
        return device

    def test_opens_and_shows_everything(self):
        device = self.open("Fitness Band")
        track = self.window.track
        self.assertTrue(self.window.on_page(track))
        self.assertEqual(track.back_btn.text(), "‹  Scan")
        self.assertEqual(track.title.text(), "Fitness Band")
        self.assertIn("dBm", track.number.text())
        self.assertIn("Heart Rate", track.ad.text())
        self.assertNotEqual(track.tiles["packets"].value.text(), "—")
        self.assertIn("Blue Station – Fitness Band", self.window.windowTitle())
        self.window.escape()
        self.assertTrue(self.window.on_page(self.window.devices))
        self.window.show_job(DEVELOP)
        self.window.show_device(device)
        self.assertEqual(track.back_btn.text(), "‹  Develop")
        self.window.escape()
        self.assertTrue(self.window.on_page(self.window.develop))

    def test_tracker_note(self):
        tag = next(d for d in self.window.store.devices.values() if d.info.kind == "Find My device")
        self.window.show_device(tag)
        self.assertTrue(self.window.track.note.isVisibleTo(self.window.track))
        self.assertIn("travelling with you", self.window.track.note.text())

    def test_rename_calibrate_and_remember(self):
        device = self.open("Desk Keyboard")
        track = self.window.track
        track._start_rename()
        track.rename.setText("Office keyboard")
        track._commit_rename()
        self.assertEqual(device.title, "Office keyboard")
        track._calibrate()
        self.assertIsNotNone(device.calibration)
        known = prefs.load()["known"][device.address]
        self.assertEqual(known["nickname"], "Office keyboard")
        self.assertEqual(known["calibration"], device.calibration)
        track._start_rename()
        track.rename.setText("")
        track._commit_rename()
        self.assertEqual(device.title, "Desk Keyboard")

    def test_gatt_read(self):
        self.open("Fitness Band")
        track = self.window.track
        track.read()
        self.assertFalse(track.read_btn.isEnabled())
        deadline = time.time() + 5
        while track._future is not None and time.time() < deadline:
            time.sleep(0.1)
            track.tick(time.time())
        self.assertIn("Example Devices", track.gatt.text())
        self.assertIn("Battery", track.gatt.text())
        self.assertTrue(track.read_btn.isEnabled())

    def test_gatt_failure_is_shown(self):
        self.open("Thermo Sensor 3A")  # the demo's sensor doesn't accept connections
        track = self.window.track
        track.read()
        deadline = time.time() + 5
        while track._future is not None and time.time() < deadline:
            time.sleep(0.1)
            track.tick(time.time())
        self.assertIn("Couldn't read it", track.gatt_status.text())

    def test_export_log(self):
        device = self.open("Living Room TV")
        path = Path(self.tmp.name) / "log.csv"
        self.window.track.export(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "time,unix_time,rssi_dbm")
        self.assertEqual(len(lines) - 1, len(device.history))


class TrackersJobTest(WindowCase):
    def test_background_watch_and_alert_badge(self):
        tag = next(d for d in self.window.store.devices.values() if d.info.kind == "Find My device")
        watcher = self.window.watcher
        # it was with you somewhere else this morning...
        morning = watcher.moved(time.time() - 7200)
        watcher.update(time.time(), list(self.window.store.devices.values()), force=True)
        record = watcher.trackers[tag.address]
        self.assertEqual(record.places, {morning.id})
        self.assertEqual(record.verdict, "passing")
        # ... and now you're somewhere new
        self.window.trackers.moved()
        watcher._checked = float("-inf")
        self.window.show_job(SCAN)  # the watch runs whichever job is on screen
        self.window.tick()
        self.assertEqual(record.verdict, "following")
        following = len(watcher.following())
        self.assertGreaterEqual(following, 1)
        self.assertEqual(self.window.views.button(TRACKERS).text(), f"Trackers  {following}")
        self.assertIn("following you", self.window.statusBar().currentMessage())
        self.window.show_job(TRACKERS)
        page = self.window.trackers
        self.assertIn("may be following you", page.headline.text())
        self.assertEqual(page.model.rows[0].verdict, "following")
        page.table.opened.emit(record)
        self.assertTrue(self.window.on_page(self.window.track))
        self.assertIn("It may be following you", self.window.track.note.text())
        saved = prefs.load(prefs.TRACKERS)
        self.assertIn(tag.address, [t["address"] for t in saved["trackers"]])

    def test_empty_and_forget(self):
        self.window.show_job(TRACKERS)
        page = self.window.trackers
        self.window.watcher.forget()
        self.window.tick()
        self.assertIn("Nothing is following you", page.headline.text())


class SurveyJobTest(WindowCase):
    def test_points_map_and_export(self):
        self.window.show_job(SURVEY)
        page = self.window.survey
        beacons = [d for d in self.window.store.devices.values() if d.info.beacon]
        self.assertEqual({d.address for d in page.model.rows}, {d.address for d in beacons})
        page.table.pick(beacons[0])
        self.assertIs(page.target, beacons[0])
        start = time.time() - 5  # a point measured over packets already in the history
        page.survey.start_point(0.2, 0.3, start - 10)
        page.survey.pending.t = start
        self.window.store.ingest([Sighting(beacons[0].address, -60, start + 1)])
        self.window.tick()
        self.assertEqual(len(page.survey.points), 1)
        heard = [r for t, r in beacons[0].history if start <= t <= start + HOLD]
        self.assertAlmostEqual(page.survey.points[0].readings[beacons[0].address], sum(heard) / len(heard))
        page.survey.points.append(__import__("blue_station.core.survey", fromlist=["Point"]).Point(
            0.8, 0.7, start, {beacons[0].address: -80}))
        self.window.tick()
        self.assertIsNotNone(page.canvas.heat)
        self.assertEqual(len(page.canvas.values), 2)
        page.modes.changed.emit(2)  # how many are usable
        self.window.tick()
        self.assertEqual(page.mode, "count")
        image = page.canvas.render()
        self.assertFalse(image.isNull())
        path = Path(self.tmp.name) / "points.csv"
        page.export_csv(path)
        self.assertIn("rssi_dbm", path.read_text(encoding="utf-8"))
        page.undo()
        self.assertEqual(len(page.survey.points), 1)

    def test_floor_plan(self):
        from PySide6.QtGui import QImage
        page = self.window.survey
        path = Path(self.tmp.name) / "plan.png"
        image = QImage(400, 200, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFF)
        image.save(str(path))
        self.assertTrue(page.load_plan(path))
        self.assertAlmostEqual(page.canvas.aspect, 0.5)
        self.assertFalse(page.load_plan(Path(self.tmp.name) / "nothing.png"))


class DevelopJobTest(WindowCase):
    def setUp(self):
        super().setUp()
        self.window.show_job(DEVELOP)
        self.page = self.window.develop
        self.band = self.device("Fitness Band")
        self.page.table.pick(self.band)

    def test_packets_and_timing(self):
        page = self.page
        self.assertIs(page.device, self.band)
        self.assertEqual(page.work.currentIndex(), 1)
        self.window.tick()
        self.assertNotEqual(page.tiles["typical"].value.text(), "—")
        self.assertIn("02 01 A4", page.payload.text())

    def test_packet_log_is_off_until_asked(self):
        page, log = self.page, self.window.log
        self.window.store.ingest(self.scanner.drain(time.time() + 2))
        self.assertEqual(len(log.rows), 0)
        self.assertIn("off", page.log_off.text())
        page.toggle_record()
        self.assertTrue(log.recording)
        self.scanner._last = time.time() - 3
        self.window.tick()
        self.assertTrue(log.rows)
        self.assertTrue(all(s.address == self.band.address for s in log.rows))
        page.toggle_record()
        self.assertFalse(log.recording)
        path = Path(self.tmp.name) / "packets.csv"
        page.export(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines) - 1, len(log.rows))
        page.clear_log()
        self.assertEqual(len(log.rows), 0)

    def test_gatt_explorer_with_notifications(self):
        page = self.page
        page.toggle_connection()
        deadline = time.time() + 3
        while not page._items and time.time() < deadline:
            time.sleep(0.1)
            self.window.tick()
        self.assertIn(15, page._items)  # the heart rate measurement
        page.link.notify(15, True)
        page.link.read(18)
        time.sleep(1.2)
        self.window.tick()
        self.assertIn("bpm", page.notes.toPlainText())
        self.assertEqual(page._items[18].text(1), "Chest")
        page.toggle_connection()
        self.assertIsNone(page.link)

    def test_switching_device_disconnects(self):
        page = self.page
        page.toggle_connection()
        link = page.link
        page.table.pick(self.device("Desk Keyboard"))
        self.assertIsNone(page.link)
        self.assertEqual(link.state, "closed")


class ThemeTest(WindowCase):
    def test_switching_appearance(self):
        self.window.set_appearance("light")
        self.assertEqual(theme.colors(), theme.LIGHT)
        self.assertEqual(prefs.load()["appearance"], "light")
        self.window.show_device(self.device("Fitness Band"))
        self.window.set_appearance("dark")
        self.assertEqual(theme.colors(), theme.DARK)


if __name__ == "__main__":
    unittest.main()
