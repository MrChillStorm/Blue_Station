"""Core tests, no Qt and no radio: python3 -m unittest discover tests"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blue_station.core import gatt, names, prefs
from blue_station.core.decode import decode, eddystone_url, short_company
from blue_station.core.devices import GONE_AFTER, TYPICAL_1M, Device, DeviceStore, Sighting, fraction, quality
from blue_station.core.scanner import DemoScanner, Scanner, _friendly


def uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


class NamesTest(unittest.TestCase):
    def test_lookups(self):
        self.assertEqual(names.company(0x004C), "Apple, Inc.")
        self.assertIsNone(names.company(0xFFFE + 5))
        self.assertEqual(names.short_uuid(uuid16(0x180F)), 0x180F)
        self.assertIsNone(names.short_uuid("6e400001-b5a3-f393-e0a9-e50e24dcca9e"))
        self.assertEqual(names.service_label(uuid16(0x180F)), "Battery Service (0x180F)")
        self.assertEqual(names.characteristic(uuid16(0x2A19)), "Battery Level")
        self.assertIn("Google", names.service_owner(uuid16(0xFE2C)))
        self.assertEqual(names.appearance(0x00C1), "Watch")

    def test_short_company(self):
        self.assertEqual(short_company("Apple, Inc."), "Apple")
        self.assertEqual(short_company("Samsung Electronics Co. Ltd."), "Samsung Electronics")
        self.assertEqual(short_company("Nordic Semiconductor ASA"), "Nordic Semiconductor")
        self.assertEqual(short_company("Microsoft"), "Microsoft")


class DecodeTest(unittest.TestCase):
    def test_ibeacon(self):
        data = bytes.fromhex("0215" "e2c56db5dffb48d2b060d0f5a71096e0" "0007" "0102" "c5")
        info = decode(None, {0x004C: data}, [], {})
        self.assertEqual(info.kind, "iBeacon")
        self.assertEqual(info.ref_1m, -59)
        fields = dict(info.fields)
        self.assertEqual(fields["iBeacon UUID"], "E2C56DB5-DFFB-48D2-B060-D0F5A71096E0")
        self.assertEqual(fields["Major · minor"], "7 · 258")
        self.assertEqual(info.vendor, "Apple, Inc.")

    def test_airpods(self):
        data = bytes.fromhex("0719" "01" "0e20" "2b" "9a" "85" + "00" * 19)
        info = decode(None, {0x004C: data}, [], {})
        self.assertEqual(info.kind, "AirPods Pro")
        self.assertEqual(info.icon, "headphones")
        fields = dict(info.fields)
        self.assertEqual(fields["Earbud battery"], "90 % · 100 %")
        self.assertEqual(fields["Case battery"], "50 %")

    def test_airpods_unknown_battery_is_left_out(self):
        info = decode(None, {0x004C: bytes.fromhex("0719" "01" "0220" "00" "ff" "ff" + "00" * 19)}, [], {})
        self.assertEqual(info.kind, "AirPods")
        self.assertNotIn("Earbud battery", dict(info.fields))
        self.assertNotIn("Case battery", dict(info.fields))

    def test_find_my(self):
        away = decode(None, {0x004C: bytes([0x12, 0x19, 0x90]) + bytes(24)}, [], {})
        self.assertTrue(away.tracker)
        self.assertEqual(dict(away.fields)["Find My"], "Away from its owner")
        self.assertEqual(dict(away.fields)["Battery hint"], "Low")
        self.assertIsNotNone(away.note)
        near = decode(None, {0x004C: bytes([0x12, 0x02, 0x00, 0x01])}, [], {})
        self.assertFalse(near.tracker)
        self.assertEqual(dict(near.fields)["Find My"], "Near its owner")

    def test_several_apple_messages_pick_the_most_specific(self):
        info = decode(None, {0x004C: bytes.fromhex("100507180b3a2c" "0b03010203")}, [], {})
        self.assertEqual(info.kind, "Apple Watch")
        self.assertEqual(dict(info.fields)["Apple messages"], "Nearby Info, Magic Switch")
        self.assertEqual(dict(info.fields)["Activity"], "Screen on")

    def test_truncated_payloads_dont_raise(self):
        for data in (b"", b"\x02", b"\x02\x15\x01", b"\x07\x19\x01", b"\x12"):
            decode(None, {0x004C: data}, [], {})
        for data in (b"", b"\x00", b"\x10\xee", b"\x20\x00\x01"):
            decode(None, {}, [uuid16(0xFEAA)], {uuid16(0xFEAA): data})
        decode(None, {0x0006: b""}, [], {})

    def test_eddystone(self):
        url = bytes([0x10, 0xEE, 0x03]) + b"example" + bytes([0x07])
        self.assertEqual(eddystone_url(url), "https://example.com")
        info = decode(None, {}, [uuid16(0xFEAA)], {uuid16(0xFEAA): url})
        self.assertEqual(info.kind, "Eddystone beacon")
        self.assertEqual(info.ref_1m, -18 - 41)
        uid = bytes([0x00, 0xF0]) + bytes(range(10)) + bytes(range(6)) + b"\x00\x00"
        fields = dict(decode(None, {}, [], {uuid16(0xFEAA): uid}).fields)
        self.assertEqual(fields["Namespace"], "00010203040506070809")
        tlm = bytes([0x20, 0x00, 0x0B, 0xB8, 0x19, 0x80, 0, 0, 0x03, 0xE8, 0, 0, 0x8C, 0xA0])
        fields = dict(decode(None, {}, [], {uuid16(0xFEAA): tlm}).fields)
        self.assertEqual(fields["Beacon battery"], "3.00 V")
        self.assertEqual(fields["Beacon temperature"], "25.5 °C")
        self.assertEqual(fields["Packets sent"], "1 000")
        self.assertEqual(fields["Beacon uptime"], "1.0 h")

    def test_microsoft(self):
        self.assertEqual(decode(None, {0x0006: bytes.fromhex("012f2002") + bytes(20)}, [], {}).kind, "Windows laptop")
        self.assertEqual(decode(None, {0x0006: bytes.fromhex("030080")}, [], {}).kind, "Swift Pair accessory")

    def test_services_and_names(self):
        self.assertEqual(decode(None, {}, [uuid16(0x180D)], {}).kind, "Heart rate sensor")
        tile = decode(None, {}, [uuid16(0xFEED)], {})
        self.assertTrue(tile.tracker)
        self.assertIn("Tile", tile.vendor)
        fast = decode(None, {}, [], {uuid16(0xFE2C): bytes.fromhex("0a1b2c")})
        self.assertEqual(dict(fast.fields)["Fast Pair model"], "0x0A1B2C")
        self.assertEqual(decode("Kitchen Speaker", {}, [], {}).icon, "speaker")
        self.assertEqual(decode("Galaxy S30", {}, [], {}).kind, "Phone")
        # a service says more than a name
        self.assertEqual(decode("Band 7", {}, [uuid16(0x180D)], {}).kind, "Heart rate sensor")
        self.assertIsNone(decode("xyz", {}, [], {}).kind)
        # a name says more than Apple's generic Nearby Info
        mac = decode("Someone's MacBook Pro", {0x004C: bytes.fromhex("100507180b3a2c")}, [], {})
        self.assertEqual((mac.kind, mac.icon), ("Computer", "laptop"))


def sighting(rssi: int, t: float, **kw) -> Sighting:
    return Sighting(address="A", rssi=rssi, t=t, **kw)


class DeviceTest(unittest.TestCase):
    def test_smoothing_follows_the_signal(self):
        d = Device("A", 0)
        d.update(sighting(-80, 0))
        self.assertEqual(d.smoothed, -80)
        for i in range(1, 60):
            d.update(sighting(-50, i * 0.1))
        self.assertAlmostEqual(d.smoothed, -50, delta=1.5)
        self.assertEqual(d.rssi, -50)
        self.assertEqual(d.packets, 60)

    def test_unknown_rssi_is_ignored(self):
        d = Device("A", 0)
        d.update(sighting(127, 0, name="Thing"))
        self.assertIsNone(d.smoothed)
        self.assertEqual(d.packets, 1)
        self.assertEqual(d.title, "Thing")
        self.assertIsNone(d.distance())

    def test_advertisement_pieces_are_merged(self):
        d = Device("A", 0)
        d.update(sighting(-60, 0, name="Scale", service_uuids=(uuid16(0x181D),)))
        d.update(sighting(-60, 1, service_uuids=(uuid16(0x180F),), tx_power=4, connectable=True))
        self.assertEqual(d.name, "Scale")
        self.assertEqual(d.service_uuids, [uuid16(0x181D), uuid16(0x180F)])
        self.assertEqual((d.tx_power, d.connectable), (4, True))
        self.assertEqual(d.info.kind, "Weight scale")

    def test_stats_rate_and_age(self):
        d = Device("A", 100)
        for i, r in enumerate([-60, -62, -64, -66, -68]):
            d.update(sighting(r, 100 + i))
        s = d.stats(104, 60)
        self.assertEqual((s.low, s.high, s.mean, s.count), (-68, -60, -64, 5))
        self.assertAlmostEqual(s.spread, 2.828, places=2)
        self.assertAlmostEqual(d.rate(104), 5 / 4)
        self.assertFalse(d.gone(104 + GONE_AFTER - 1))
        self.assertTrue(d.gone(104 + GONE_AFTER + 1))
        self.assertEqual(d.fade(104), 1.0)
        self.assertLess(d.fade(104 + 20), 1.0)

    def test_distance_references(self):
        d = Device("A", 0)
        d.update(sighting(TYPICAL_1M, 0))
        self.assertAlmostEqual(d.distance(), 1.0)
        self.assertEqual(d.reference[1], "a typical device")
        d.calibration = TYPICAL_1M + 10  # this device is louder than most at 1 m
        self.assertGreater(d.distance(), 2)
        self.assertEqual(d.reference[1], "your calibration")
        beacon = Device("B", 0)
        beacon.update(Sighting("B", -70, 0, manufacturer_data={0x004C: bytes.fromhex(
            "0215" + "00" * 16 + "00010002" + "c0")}))  # -64 dBm at 1 m
        self.assertEqual(beacon.reference, (-64, "the beacon's own figure"))

    def test_trend(self):
        d = Device("A", 0)
        for i in range(20):
            d.update(sighting(-80 + i, i * 0.4))  # 2.5 dB per second closer
        self.assertAlmostEqual(d.trend(7.6), 2.5, places=1)
        flat = Device("B", 0)
        flat.update(sighting(-70, 0))
        self.assertIsNone(flat.trend(0))
        slow = Device("C", 0)  # heard only every two seconds, as a Mac hears a TV
        for i in range(12):
            slow.update(sighting(-90 + 2 * i, i * 2.0))
        self.assertAlmostEqual(slow.trend(22.0), 1.0, places=1)

    def test_history_is_trimmed(self):
        d = Device("A", 0)
        d.update(sighting(-60, 0))
        d.update(sighting(-60, 20 * 60))
        self.assertEqual(len(d.history), 1)

    def test_helpers(self):
        self.assertEqual(quality(-50), "Excellent")
        self.assertEqual(quality(-90), "Weak")
        self.assertEqual(fraction(-100), 0)
        self.assertEqual(fraction(-20), 1)


class StoreTest(unittest.TestCase):
    def test_known_devices_and_clear(self):
        store = DeviceStore({"A": {"nickname": "Keys", "pinned": True, "calibration": -55}})
        self.assertEqual(store.ingest([Sighting("A", -60, 0), Sighting("B", -70, 0), Sighting("A", -61, 1)]), 2)
        a, b = store.devices["A"], store.devices["B"]
        self.assertEqual((a.title, a.pinned, a.calibration), ("Keys", True, -55))
        b.nickname = "Bike"
        store.remember(b)
        self.assertEqual(store.known["B"], {"nickname": "Bike"})
        b.nickname = None
        store.remember(b)
        self.assertNotIn("B", store.known)
        store.clear()
        self.assertEqual(list(store.devices), ["A"])


class GattTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(gatt.format_value(0x2A19, b"\x55"), "85 %")
        self.assertEqual(gatt.format_value(0x2A01, (0x00C1).to_bytes(2, "little")), "Watch (0x00C1)")
        self.assertEqual(gatt.format_value(0x2A29, b"Example Devices\x00"), "Example Devices")
        self.assertEqual(gatt.format_value(0x2A50, bytes([1, 0x4C, 0, 0x34, 0x12, 0x01, 0x00])),
                         "Apple, Inc. · product 0x1234 · version 0x0001")
        self.assertEqual(gatt.format_value(0x2A23, b"\x01\x02\xff"), "01 02 FF")


class ScannerTest(unittest.TestCase):
    def test_friendly_errors(self):
        self.assertIn("turned off", _friendly(Exception("Bluetooth device is turned off")))
        self.assertIn("Privacy & Security", _friendly(Exception("Bluetooth access is denied by the user")))
        self.assertEqual(_friendly(Exception("odd")), "odd")

    def test_detected_advertisements_are_queued(self):
        scanner = Scanner()
        device = SimpleNamespace(address="X", name="OS name", details=None)
        adv = SimpleNamespace(rssi=-61, local_name=None, tx_power=None, manufacturer_data={0x004C: b"\x10\x01\x07"},
                              service_uuids=[uuid16(0x180F)], service_data={}, platform_data=())
        scanner._detected(device, adv)
        (s,) = scanner.drain()
        self.assertEqual((s.address, s.rssi, s.name, s.service_uuids), ("X", -61, "OS name", (uuid16(0x180F),)))
        self.assertEqual(scanner.drain(), [])

    def test_scan_state_settles_after_rapid_presses(self):
        class FakeBleak:
            instances = []

            def __init__(self, detection_callback=None):
                self.running = False
                FakeBleak.instances.append(self)

            async def start(self):
                await asyncio.sleep(0.05)
                self.running = True

            async def stop(self):
                await asyncio.sleep(0.05)
                self.running = False

        def settle(scanner, state):
            deadline = time.time() + 3
            while scanner.state != state and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.2)  # anything still queued gets to run
            return scanner.state

        with mock.patch("bleak.BleakScanner", FakeBleak):
            scanner = Scanner()
            scanner.start()
            self.assertEqual(scanner.state, "starting")
            self.assertEqual(settle(scanner, "scanning"), "scanning")
            self.assertTrue(FakeBleak.instances[0].running)
            for _ in range(3):
                scanner.stop()
                scanner.start()
            scanner.stop()
            self.assertEqual(settle(scanner, "idle"), "idle")
            self.assertFalse(FakeBleak.instances[0].running)
            scanner.start()
            scanner.stop()
            scanner.start()
            self.assertEqual(settle(scanner, "scanning"), "scanning")
            self.assertTrue(FakeBleak.instances[0].running)
            self.assertEqual(len(FakeBleak.instances), 1)
            scanner.close()
            self.assertFalse(FakeBleak.instances[0].running)

    def test_scan_errors_are_explained(self):
        class Off:
            def __init__(self, detection_callback=None):
                pass

            async def start(self):
                raise RuntimeError("Bluetooth device is turned off")

        with mock.patch("bleak.BleakScanner", Off):
            scanner = Scanner()
            scanner.start()
            deadline = time.time() + 3
            while scanner.state == "starting" and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(scanner.state, "error")
            self.assertIn("turned off", scanner.error)
            scanner.close()

    def test_demo(self):
        demo = DemoScanner()
        self.assertEqual(demo.drain(), [])
        demo.start()
        now = time.time()
        sightings = demo.drain(now + 5)
        self.assertGreater(len({s.address for s in sightings}), 8)
        future = demo.read_gatt(sightings[0].address)
        result = future.result(timeout=5)
        self.assertTrue(result.summary)


class PrefsTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BLUE_STATION_SETTINGS"] = str(Path(tmp) / "sub" / "settings.json")
            try:
                self.assertEqual(prefs.load(), {})
                prefs.save({"appearance": "light", "known": {"A": {"pinned": True}}})
                self.assertEqual(prefs.load()["known"]["A"], {"pinned": True})
            finally:
                del os.environ["BLUE_STATION_SETTINGS"]


if __name__ == "__main__":
    unittest.main()
