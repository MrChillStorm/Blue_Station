"""Core tests for the jobs, no Qt and no radio: the tracker watch, the
survey, the packet log, packet timing and the GATT link.
python3 -m unittest discover tests"""
import asyncio
import csv
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blue_station.core import gatt
from blue_station.core.devices import Device, DeviceStore, Sighting, gap_stats
from blue_station.core.packets import PacketLog
from blue_station.core.scanner import Scanner
from blue_station.core.survey import COUNT, DEVICE, HOLD, NOT_HEARD, STRONGEST, Point, Survey
from blue_station.core.decode import decode
from blue_station.core.watch import DEFAULT_GROUPS, FOLLOWING, PASSING, STAYING, Watcher, group


def uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


FOLLOWER = {0x004C: bytes([0x12, 0x19, 0x10]) + bytes(24)}  # a Find My tag away from its owner
TILE = (uuid16(0xFEED),)
BAND = {"service_uuids": (uuid16(0x180D),)}  # a heart rate band, keeping its address


class Walk:
    """Plays a day out: who is heard where, checked every 15 seconds."""

    def __init__(self, groups=DEFAULT_GROUPS):
        self.store = DeviceStore()
        self.watcher = Watcher(groups=groups)

    def stay(self, start: int, end: int, heard: dict[str, dict]) -> None:
        for t in range(start, end, 15):
            self.store.ingest([Sighting(address, -60, t, **kw) for address, kw in heard.items()])
            self.watcher.update(t, list(self.store.devices.values()))

    def verdict(self, address: str) -> str:
        return self.watcher.trackers[address].verdict


HOME = {"TV": {"name": "Living Room TV"}, "FRIDGE": {"name": "Fridge"}, "PRINTER": {"name": "Printer"}}
CAFE = {"ESPRESSO": {"name": "Espresso Machine"}, "SPEAKER": {"name": "Café Speaker"}, "TILL": {"name": "Till"}}
MINE = {"PHONE": {"name": "My Phone"}}


class WatchTest(unittest.TestCase):
    def test_a_day_out(self):
        walk = Walk()
        follower = {"TAG": {"manufacturer_data": FOLLOWER}}
        walk.stay(0, 900, {**HOME, **MINE, **follower, "NEIGHBOUR": {"service_uuids": TILE}})
        home = walk.watcher.settled
        self.assertIsNotNone(home)
        self.assertEqual(walk.watcher.trackers["TAG"].places, {home.id})
        self.assertEqual(walk.verdict("TAG"), PASSING)

        walk.stay(900, 1500, {**MINE, **follower})  # on the way: nothing settled, nothing credited
        self.assertIsNone(walk.watcher.settled)
        walk.stay(1500, 2400, {**CAFE, **MINE, **follower, "STRANGER": {"manufacturer_data": FOLLOWER}})
        cafe = walk.watcher.settled
        self.assertIsNotNone(cafe)
        self.assertNotEqual(cafe.id, home.id)
        self.assertIn("PHONE", walk.watcher.companions)  # it came along, so it describes no place
        self.assertEqual(walk.verdict("TAG"), FOLLOWING)
        self.assertEqual(walk.watcher.trackers["STRANGER"].places, {cafe.id})
        self.assertNotEqual(walk.verdict("STRANGER"), FOLLOWING)
        self.assertEqual(walk.watcher.trackers["NEIGHBOUR"].places, {home.id})

        walk.stay(2400, 3600, {**HOME, **MINE})  # back home is recognized, not a third place
        self.assertEqual(walk.watcher.settled.id, home.id)
        self.assertEqual(len(walk.watcher.places), 2)

    def test_a_neighbours_tag_only_stays(self):
        walk = Walk()
        walk.stay(0, 30 * 60, {**HOME, "NEIGHBOUR": {"service_uuids": TILE}})
        self.assertEqual(walk.verdict("NEIGHBOUR"), STAYING)
        self.assertEqual(walk.watcher.following(), [])

    def test_time_with_you_counts_packets_not_checks(self):
        walk = Walk()
        walk.stay(0, 300, {**HOME, "NEIGHBOUR": {"service_uuids": TILE}})
        record = walk.watcher.trackers["NEIGHBOUR"]
        self.assertAlmostEqual(record.seen_seconds, 285, delta=1)
        walk.stay(300, 400, HOME)  # it goes quiet
        self.assertAlmostEqual(record.last_seen, 285)

    def test_marking_a_place_you_already_know(self):
        walk = Walk()
        walk.stay(0, 900, {**HOME, "TAG": {"manufacturer_data": FOLLOWER}})
        home = walk.watcher.settled
        walk.watcher.moved(900)  # pressed by mistake: still at home
        walk.stay(900, 1200, {**HOME, "TAG": {"manufacturer_data": FOLLOWER}})
        self.assertEqual(walk.watcher.settled.id, home.id)
        self.assertEqual(walk.verdict("TAG"), PASSING)

    def test_memory_round_trip(self):
        walk = Walk()
        walk.stay(0, 900, {**HOME, **MINE, "TAG": {"manufacturer_data": FOLLOWER}})
        walk.watcher.moved(900)
        again = Watcher(walk.watcher.to_dict())
        self.assertEqual(again.places.keys(), walk.watcher.places.keys())
        self.assertEqual(again.trackers["TAG"].places, walk.watcher.trackers["TAG"].places)
        self.assertEqual(again.next_place, walk.watcher.next_place)
        self.assertIsNone(again.current)  # where you are is worked out again, not assumed

    def test_place_numbers_are_never_reused(self):
        watcher = Watcher({"trackers": [{"address": "T", "kind": "Tile tracker", "first_seen": 0, "last_seen": 0,
                                         "places": [7]}]})
        self.assertEqual(watcher.moved(10).id, 8)
        self.assertEqual(watcher.trackers["T"].group, "trackers")  # saved before there were kinds to pick


class WatchForTest(unittest.TestCase):
    def test_kinds(self):
        ibeacon = {0x004C: bytes.fromhex("0215" + "3b8f1c24a7e54d0b9c3f5e6a7b8c9d0e" + "0001002ac5")}
        self.assertEqual(group(decode(None, FOLLOWER, [], {})), "trackers")
        self.assertEqual(group(decode(None, {}, list(TILE), {})), "trackers")
        self.assertEqual(group(decode(None, {}, list(BAND["service_uuids"]), {})), "wearables")
        self.assertEqual(group(decode("Galaxy S24", {}, [], {})), "phones")
        self.assertEqual(group(decode("WH-1000XM5", {}, [], {})), "headphones")
        self.assertEqual(group(decode(None, {}, [], {})), "other")
        for stays_put in (decode("Living Room TV", {}, [], {}), decode("Kitchen Speaker", {}, [], {}),
                          decode(None, ibeacon, [], {})):
            self.assertIsNone(group(stays_put))

    def test_a_band_that_follows_you_counts_only_when_watched(self):
        for groups, verdict in ((DEFAULT_GROUPS, None), (("trackers", "wearables"), FOLLOWING)):
            walk = Walk(groups)
            walk.stay(0, 900, {**HOME, "BAND": BAND})
            walk.stay(900, 1500, {"BAND": BAND})
            walk.stay(1500, 2400, {**CAFE, "BAND": BAND})
            record = walk.watcher.record("BAND")
            self.assertEqual(record and record.verdict, verdict)

    def test_coming_along_never_makes_a_device_yours(self):
        walk = Walk(("trackers", "wearables"))
        band = {"name": "Band 7", **BAND}  # named, so it's also a landmark at home
        walk.stay(0, 900, {**HOME, "BAND": band})
        walk.stay(900, 1500, {"BAND": band})
        walk.stay(1500, 2400, {**CAFE, "BAND": band})
        self.assertIn("BAND", walk.watcher.companions)  # it describes no place any more...
        self.assertEqual(walk.watcher.mine, set())
        self.assertEqual(walk.verdict("BAND"), FOLLOWING)  # ... and is still flagged

    def test_turning_a_kind_off_hides_it_but_keeps_it(self):
        walk = Walk(("trackers", "wearables"))
        walk.stay(0, 300, {**HOME, "BAND": BAND})
        walk.watcher.set_groups(DEFAULT_GROUPS)
        self.assertIsNone(walk.watcher.record("BAND"))
        self.assertEqual(walk.watcher.watched(), [])
        walk.watcher.set_groups(("trackers", "wearables"))
        self.assertEqual(walk.watcher.record("BAND").group, "wearables")

    def test_yours_is_left_alone(self):
        walk = Walk(("trackers", "wearables"))
        walk.stay(0, 300, {**HOME, "BAND": BAND})
        walk.watcher.set_mine("BAND")
        self.assertNotIn("BAND", walk.watcher.trackers)
        walk.stay(300, 600, {**HOME, "BAND": BAND})
        self.assertNotIn("BAND", walk.watcher.trackers)
        self.assertEqual(Watcher(walk.watcher.to_dict()).mine, {"BAND"})
        walk.watcher.forget()
        self.assertEqual(walk.watcher.mine, {"BAND"})  # forgetting what it has seen keeps what's yours
        walk.watcher.set_mine("BAND", False)
        walk.stay(600, 700, {**HOME, "BAND": BAND})
        self.assertIsNotNone(walk.watcher.record("BAND"))

    def test_other_kinds_that_passed_by_are_forgotten_sooner(self):
        walk = Walk(("trackers", "phones"))
        walk.stay(0, 300, {**HOME, "PHONE": {"name": "Galaxy S24"}, "TAG": {"manufacturer_data": FOLLOWER}})
        walk.watcher.update(4 * 3600, [], force=True)
        self.assertNotIn("PHONE", walk.watcher.trackers)
        self.assertIn("TAG", walk.watcher.trackers)


class SurveyTest(unittest.TestCase):
    def device(self, address: str, points) -> Device:
        d = Device(address, points[0][0])
        for t, rssi in points:
            d.update(Sighting(address, rssi, t))
        return d

    def test_a_point_is_the_packets_after_the_click(self):
        survey = Survey()
        survey.start_point(0.5, 1.4, 100)
        self.assertEqual(survey.pending.y, 1.0)  # kept on the plan
        a = self.device("A", [(99, -40), (100.5, -60), (101.5, -64), (100 + HOLD + 1, -30)])
        b = self.device("B", [(90, -70)])
        self.assertIsNone(survey.update(100 + HOLD - 0.1, [a, b]))
        point = survey.update(100 + HOLD, [a, b])
        self.assertEqual(point.readings, {"A": -62})
        self.assertIsNone(survey.pending)

    def test_what_the_map_shows(self):
        point = Point(0, 0, 0, {"A": -60, "B": -90, "C": -70})
        beacons = {"A", "B"}
        self.assertEqual(Survey.value(point, DEVICE, "C", beacons), -70)
        self.assertIsNone(Survey.value(point, DEVICE, "D", beacons))
        self.assertEqual(Survey.value(point, STRONGEST, None, beacons), -60)
        self.assertEqual(Survey.value(point, COUNT, None, beacons), 1)
        self.assertIsNone(Survey.value(point, STRONGEST, None, {"X"}))

    def test_the_estimate_is_smooth_and_bounded(self):
        values = [(0.1, 0.1, -50.0), (0.9, 0.9, -90.0), (0.5, 0.5, None)]
        grid = Survey.grid(values, 20, 20)
        flat = [v for row in grid for v, _ in row]
        self.assertTrue(all(NOT_HEARD - 0.01 <= v <= -50 + 0.01 for v in flat))
        self.assertAlmostEqual(grid[2][2][0], -50, delta=2)  # right by the strong point
        self.assertAlmostEqual(grid[10][10][0], NOT_HEARD, delta=6)  # where nothing was heard
        self.assertLess(grid[0][0][1], 0.15)  # distance to the nearest point
        self.assertGreater(grid[0][19][1], 0.5)

    def test_undo_and_export(self):
        survey = Survey()
        survey.points = [Point(0.1, 0.2, 0, {"A": -61.25}), Point(0.3, 0.4, 0, {})]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.csv"
            survey.export(path, {"A": "Beacon A"})
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(rows, [{"point": "1", "x": "0.1000", "y": "0.2000", "time": rows[0]["time"],
                                 "address": "A", "device": "Beacon A", "rssi_dbm": "-61.2"}])
        survey.start_point(0.5, 0.5, 0)
        self.assertTrue(survey.undo())  # the pending one first
        self.assertEqual(len(survey.points), 2)
        self.assertTrue(survey.undo())
        self.assertEqual(len(survey.points), 1)


class PacketLogTest(unittest.TestCase):
    def test_off_until_asked_and_bounded(self):
        log = PacketLog(limit=5)
        log.add([Sighting("A", -60, 1)])
        self.assertEqual(len(log.rows), 0)
        log.start("A")
        log.add([Sighting("A", -60, 3), Sighting("B", -60, 2), Sighting("A", -61, 2)])
        self.assertEqual([s.t for s in log.rows], [2, 3])  # in the order heard, one device
        log.stop()
        log.start(None)
        log.add([Sighting("B", -60, t) for t in range(4, 10)])
        self.assertEqual(len(log.rows), 5)
        self.assertEqual(log.total, 8)
        self.assertEqual([s.t for s in log.latest(2)], [9, 8])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.csv"
            self.assertEqual(log.export(path, {"B": "Beacon"}), 5)
            self.assertIn("Beacon", path.read_text(encoding="utf-8"))


class TimingAndPayloadTest(unittest.TestCase):
    def test_gaps(self):
        d = Device("A", 0)
        for t in (0, 0.1, 0.2, 0.4, 0.5, 1.0):
            d.update(Sighting("A", -60, t))
        gaps = d.gaps(1.0)
        self.assertEqual([round(g) for g in gaps], [100, 100, 200, 100, 500])
        stats = gap_stats(gaps)
        self.assertEqual((round(stats.shortest), round(stats.median), round(stats.longest)), (100, 100, 500))
        self.assertIsNone(gap_stats([]))

    def test_payload_changes_are_counted_and_remembered(self):
        d = Device("A", 0)
        d.update(Sighting("A", -60, 0, manufacturer_data={0x0059: b"\x01\x02"}))
        d.update(Sighting("A", -60, 1, manufacturer_data={0x0059: b"\x01\x02"}))
        self.assertEqual(d.payload_changes, 0)
        d.update(Sighting("A", -60, 2, manufacturer_data={0x0059: b"\x01\x03"}))
        self.assertEqual(d.payload_changes, 1)
        self.assertEqual(d.previous["maker:0059"], b"\x01\x02")

    def test_eddystone_frames_are_decoded_together(self):
        d = Device("E", 0)
        eddy = uuid16(0xFEAA)
        url = bytes([0x10, 0xEE, 0x03]) + b"example" + bytes([0x07])
        tlm = bytes([0x20, 0x00, 0x0B, 0xB8, 0x19, 0x80, 0, 0, 0x03, 0xE8, 0, 0, 0x8C, 0xA0])
        d.update(Sighting("E", -60, 0, service_uuids=(eddy,), service_data={eddy: url}))
        d.update(Sighting("E", -60, 1, service_data={eddy: tlm}))
        fields = dict(d.info.fields)
        self.assertEqual(fields["URL"], "https://example.com")
        self.assertEqual(fields["Beacon battery"], "3.00 V")
        self.assertEqual(d.info.detail, "example.com · battery 3.00 V")
        self.assertTrue(d.info.beacon)
        self.assertEqual(d.payload_changes, 0)  # taking turns between frames isn't a change

    def test_details(self):
        from blue_station.core.decode import decode
        ibeacon = decode(None, {0x004C: bytes.fromhex("0215" + "00" * 16 + "0007" + "0102" + "c5")}, [], {})
        self.assertEqual(ibeacon.detail, "major 7 · minor 258")
        pods = decode(None, {0x004C: bytes.fromhex("0719" "01" "0e20" "2b" "9a" "85" + "00" * 19)}, [], {})
        self.assertEqual(pods.detail, "90 % · 100 % · case 50 %")


class ValueFormatTest(unittest.TestCase):
    def test_sensor_values(self):
        self.assertEqual(gatt.format_value(0x2A37, bytes([0x00, 72])), "72 bpm")
        self.assertEqual(gatt.format_value(0x2A37, bytes([0x01, 0x2C, 0x01])), "300 bpm")
        self.assertEqual(gatt.format_value(0x2A38, b"\x02"), "Wrist")
        self.assertEqual(gatt.format_value(0x2A6E, (-250).to_bytes(2, "little", signed=True)), "-2.50 °C")
        self.assertEqual(gatt.format_value(0x2A6F, (4550).to_bytes(2, "little")), "45.50 %")


class FakeChar:
    def __init__(self, uuid, handle, properties):
        self.uuid, self.handle, self.properties, self.description = uuid, handle, properties, "Unknown"


class FakeService:
    def __init__(self, uuid, handle, characteristics):
        self.uuid, self.handle, self.characteristics, self.description = uuid, handle, characteristics, "Unknown"


class FakeClient:
    """Stands in for bleak's BleakClient."""
    last = None

    def __init__(self, target, disconnected_callback=None, timeout=20):
        self.lost = disconnected_callback
        self.notify = {}
        self.services = [FakeService(uuid16(0x180D), 12, [FakeChar(uuid16(0x2A37), 14, ["notify"]),
                                                          FakeChar(uuid16(0x2A38), 17, ["read"])])]
        FakeClient.last = self

    async def connect(self):
        await asyncio.sleep(0.01)

    async def read_gatt_char(self, char):
        return bytearray(b"\x01")

    async def start_notify(self, char, callback):
        self.notify[char.handle] = callback

    async def stop_notify(self, char):
        self.notify.pop(char.handle, None)

    async def disconnect(self):
        pass


class LinkTest(unittest.TestCase):
    """The real Link, against a fake bleak client on the scanner's loop."""

    def wait(self, link, until, seconds=3):
        deadline, events = time.time() + seconds, []
        while time.time() < deadline:
            events += link.drain()
            if until(events):
                break
            time.sleep(0.02)
        return events

    def test_connect_read_follow_and_lose(self):
        scanner = Scanner()
        try:
            with mock.patch("bleak.BleakClient", FakeClient):
                link = scanner.connect("ADDRESS")
                self.wait(link, lambda _e: link.state == gatt.CONNECTED)
                self.assertEqual(link.state, gatt.CONNECTED)
                self.assertEqual([c.name for c in link.services[0].characteristics],
                                 ["Heart Rate Measurement", "Body Sensor Location"])
                link.read(17)
                events = self.wait(link, lambda e: e)
                self.assertEqual((events[0].kind, events[0].text), ("read", "Chest"))
                link.notify(14, True)
                self.wait(link, lambda _e: 14 in link.subscribed)
                client = FakeClient.last
                scanner._loop.call_soon_threadsafe(client.notify[14], None, bytearray([0x00, 64]))
                events = self.wait(link, lambda e: e)
                self.assertEqual((events[0].kind, events[0].text, events[0].handle), ("notify", "64 bpm", 14))
                scanner._loop.call_soon_threadsafe(client.lost, client)
                self.wait(link, lambda _e: link.state == gatt.CLOSED)
                self.assertEqual(link.error, "The device disconnected.")
        finally:
            scanner.close()


if __name__ == "__main__":
    unittest.main()
