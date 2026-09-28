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

from blue_station.core import gatt, login
from blue_station.core.alerts import BACK, GONE, Alerts
from blue_station.core.baseline import Baseline
from blue_station.core.links import Linker, lead, listed
from blue_station.core.devices import Device, DeviceStore, Sighting, gap_stats
from blue_station.core.packets import PacketLog, identity_text, read as read_packets
from blue_station.core.position import Fix, PhoneReceiver, Track, owntracks_fixes
from blue_station.core.scanner import Scanner
from blue_station.core.survey import COUNT, DEVICE, HOLD, NOT_HEARD, STRONGEST, Point, Survey
from blue_station.core.decode import decode
from blue_station.core.watch import (DEFAULT_GROUPS, FOLLOWING, PASSING, STAYING, TRAVELLING, TrackerRecord, Watcher,
                                     group)


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

    def stay(self, start: int, end: int, heard: dict[str, dict], at=None) -> None:
        """at: where your phone says you are, (latitude, longitude) or a
        function of the time; None: no phone."""
        for t in range(start, end, 15):
            self.store.ingest([Sighting(address, -60, t, **kw) for address, kw in heard.items()])
            if at is not None:
                self.watcher.add_positions([Fix(t, *(at(t) if callable(at) else at), 10)])
            self.watcher.update(t, list(self.store.devices.values()))

    def verdict(self, address: str) -> str:
        return self.watcher.trackers[address].verdict


HOME = {"TV": {"name": "Living Room TV"}, "FRIDGE": {"name": "Fridge"}, "PRINTER": {"name": "Printer"}}
CAFE = {"ESPRESSO": {"name": "Espresso Machine"}, "SPEAKER": {"name": "Café Speaker"}, "TILL": {"name": "Till"}}
MINE = {"PHONE": {"name": "My Phone"}}
HOME_AT, SHOP_AT = (60.0, 25.0), (60.018, 25.0)  # made up, 2 km apart
NEAR_HOME = (60.00135, 25.0)  # 150 m from home


def walking(start: int, frm, to, metres_a_second: float = 1.0):
    """Where you are, walking in a straight line from frm towards to."""
    gap = ((to[0] - frm[0]) * 111_320, (to[1] - frm[1]) * 55_800)
    length = (gap[0] ** 2 + gap[1] ** 2) ** 0.5
    def at(t):
        f = min(1.0, (t - start) * metres_a_second / length)
        return frm[0] + f * (to[0] - frm[0]), frm[1] + f * (to[1] - frm[1])
    return at


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
        self.assertFalse(any("PHONE" in p.landmarks for p in walk.watcher.places.values()))  # people carry phones
        self.assertEqual(walk.verdict("TAG"), FOLLOWING)
        self.assertEqual(walk.watcher.trackers["STRANGER"].places, {cafe.id})
        self.assertNotEqual(walk.verdict("STRANGER"), FOLLOWING)
        self.assertEqual(walk.watcher.trackers["NEIGHBOUR"].places, {home.id})

        walk.stay(2400, 3600, {**HOME, **MINE})  # back home is recognized, not a third place
        self.assertEqual(walk.watcher.settled.id, home.id)
        self.assertEqual(len(walk.watcher.places), 2)

    def test_more_landmarks_turning_up_are_more_of_the_same_place(self):
        # home learned from the first two; three more are heard long enough to count a few minutes later
        walk, tag = Walk(), {"TAG": {"manufacturer_data": FOLLOWER}}
        first = {"TV": {"name": "Living Room TV"}, "FRIDGE": {"name": "Fridge"}}
        later = {"BEDROOM-TV": {"name": "Bedroom TV"}, "PRINTER": {"name": "Printer"}, "SPEAKER": {"name": "Kitchen Speaker"}}
        walk.stay(0, 120, {**first, **tag})
        walk.stay(120, 1800, {**first, **later, **tag})
        self.assertEqual(len(walk.watcher.places), 1)
        self.assertEqual(walk.watcher.settled.landmarks, set(first) | set(later))  # stronger for it
        self.assertEqual(walk.watcher.companions, set())
        self.assertEqual(walk.verdict("TAG"), STAYING)  # half an hour at home, not following

    def test_a_move_needs_two_checks_in_a_row(self):
        walk = Walk()
        shop = {"TILL-2": {"name": "Till 2"}, "OVEN": {"name": "Oven"}, "SIGN": {"name": "Shop Sign"}}
        passed = {"X": {"name": "Vending Machine"}, "Y": {"name": "Parking Meter"}}
        walk.stay(0, 900, HOME)
        walk.stay(900, 1200, {})
        walk.stay(1200, 1800, shop)  # the shop, once: a place of its own
        walk.stay(1800, 2100, {})
        walk.stay(2100, 2700, HOME)
        walk.stay(2700, 3000, {})
        walk.stay(3000, 3105, passed)  # a short stop: both count on just one check (at 3180)...
        walk.stay(3105, 3195, {"X": passed["X"]})
        walk.stay(3195, 3600, {})  # ... then the road: that run of checks is over
        walk.stay(3600, 3781, shop)  # back at the shop, its landmarks count from 3780: one check isn't a move
        self.assertIsNone(walk.watcher.settled)
        walk.stay(3781, 3800, shop)
        self.assertEqual(walk.watcher.settled.landmarks, set(shop))
        self.assertEqual(len(walk.watcher.places), 2)

    def test_a_walk_is_no_place(self):
        # a new pair of houses every 3 minutes, each heard for 4 as you pass
        walk = Walk(groups=["trackers", "phones"])
        walk.stay(0, 900, {**HOME, **MINE})
        for minute in range(15, 45):
            houses = {f"{kind}{n}": {"name": f"{kind} {n}"} for n in range(10) for kind in ("TV", "Garden Light")
                      if 3 * n <= minute - 15 < 3 * n + 4}
            walk.stay(minute * 60, minute * 60 + 60, {**houses, **MINE})
            self.assertEqual(len(walk.watcher.places), 1, f"minute {minute}")
        self.assertIsNone(walk.watcher.settled)
        walk.stay(2700, 2985, {**CAFE, **MINE})  # then a café: somewhere new is a place once you've stayed 5 minutes
        self.assertIsNone(walk.watcher.settled)
        walk.stay(2985, 3100, {**CAFE, **MINE})
        self.assertEqual(walk.watcher.settled.landmarks, set(CAFE))

    def test_devices_driven_past_are_no_landmarks(self):
        walk = Walk()
        street = {"LAMP": {"name": "Street Light"}, "KIOSK": {"name": "Kiosk"}}
        walk.stay(0, 900, {**HOME, **street})  # heard all the while at home
        walk.stay(900, 1800, {})
        walk.stay(1800, 1860, street)  # driving past them on the way back, a minute
        self.assertEqual(len(walk.watcher.places), 1)
        self.assertIsNone(walk.watcher.settled)

    def test_forgetting_the_ones_following_you(self):
        walk, follower = Walk(), {"TAG": {"manufacturer_data": FOLLOWER}}
        walk.stay(0, 900, {**HOME, **follower, "NEIGHBOUR": {"service_uuids": TILE}})
        walk.stay(900, 1500, follower)
        walk.stay(1500, 2400, {**CAFE, **follower})
        self.assertEqual(walk.verdict("TAG"), FOLLOWING)
        self.assertEqual(walk.watcher.forget_following(), 1)
        self.assertNotIn("TAG", walk.watcher.trackers)
        self.assertIn("NEIGHBOUR", walk.watcher.trackers)  # the others stay, and the places too
        self.assertEqual(len(walk.watcher.places), 2)
        walk.stay(2400, 2700, {**CAFE, **follower})  # still around: it starts over
        self.assertEqual(walk.verdict("TAG"), PASSING)

    def houses_passed(self, walk, start, minutes, at=None):
        """A new pair of houses every 3 minutes, each heard for 7: a slow stroll."""
        for minute in range(minutes):
            houses = {f"{kind}{n}": {"name": f"{kind} {n}"} for n in range(12) for kind in ("TV", "Garden Light")
                      if 3 * n <= minute < 3 * n + 7}
            t = start + minute * 60
            walk.stay(t, t + 60, {**houses, **MINE}, at)

    def test_with_your_phone_a_slow_stroll_is_no_place(self):
        without = Walk(groups=["trackers", "phones"])
        without.stay(0, 900, {**HOME, **MINE})
        self.houses_passed(without, 900, 30)
        self.assertGreater(len(without.watcher.places), 1)  # the landmarks alone: you seemed to stay
        walk = Walk(groups=["trackers", "phones"])
        walk.stay(0, 900, {**HOME, **MINE}, at=HOME_AT)
        home = walk.watcher.settled
        self.assertTrue(walk.watcher.by_position)
        self.houses_passed(walk, 900, 30, at=walking(900, HOME_AT, SHOP_AT))  # but your phone says you moved
        self.assertEqual(list(walk.watcher.places), [home.id])
        self.assertTrue(walk.watcher.moving)

    def test_with_your_phone_a_stop_is_a_place_with_few_landmarks(self):
        walk, tag = Walk(), {"TAG": {"manufacturer_data": FOLLOWER}}
        walk.stay(0, 900, {**HOME, **tag}, at=HOME_AT)
        walk.stay(900, 1100, tag, at=walking(900, HOME_AT, SHOP_AT, 10))  # driving 2 km
        walk.stay(1100, 1395, {"KIOSK": {"name": "Kiosk"}, **tag}, at=SHOP_AT)  # one landmark: never a place alone
        self.assertIsNone(walk.watcher.settled)  # stopped, but not for 5 minutes yet
        walk.stay(1395, 2400, {"KIOSK": {"name": "Kiosk"}, **tag}, at=SHOP_AT)
        shop = walk.watcher.settled
        self.assertEqual(len(walk.watcher.places), 2)
        self.assertEqual(shop.landmarks, {"KIOSK"})
        self.assertEqual(walk.verdict("TAG"), FOLLOWING)

    def test_with_your_phone_a_spot_near_a_place_is_that_place(self):
        walk = Walk(groups=["other"])
        walk.stay(0, 900, HOME, at=HOME_AT)
        home = walk.watcher.settled
        walk.stay(900, 1050, {}, at=walking(900, HOME_AT, NEAR_HOME))
        yard = {"LAMP": {"name": "Garden Light"}, "GATE": {"name": "Gate"}, "FRIDGE": HOME["FRIDGE"]}
        walk.stay(1050, 1800, yard, at=NEAR_HOME)  # 150 m away, 12 minutes: still home
        self.assertIs(walk.watcher.settled, home)
        self.assertEqual(len(walk.watcher.places), 1)

    def test_when_the_phone_goes_quiet_the_landmarks_take_over(self):
        walk = Walk()
        walk.stay(0, 900, HOME, at=HOME_AT)
        home = walk.watcher.settled
        walk.stay(900, 1500, HOME)  # no more positions
        self.assertFalse(walk.watcher.by_position)
        self.assertIs(walk.watcher.settled, home)

    def test_with_your_phone_what_came_along_is_learned(self):
        speaker = {"SPEAKER": {"name": "Party Box"}}
        walk = Walk(groups=["other"])
        walk.stay(0, 900, {**HOME, **speaker}, at=HOME_AT)
        self.assertIn("SPEAKER", walk.watcher.settled.landmarks)
        walk.stay(900, 1100, speaker, at=walking(900, HOME_AT, SHOP_AT, 10))
        walk.stay(1100, 1800, {**CAFE, **speaker}, at=SHOP_AT)  # heard 2 km from home: it came along
        self.assertIn("SPEAKER", walk.watcher.companions)
        self.assertFalse(any("SPEAKER" in p.landmarks for p in walk.watcher.places.values()))

    def test_with_you_on_the_move(self):
        walk, tag = Walk(), {"TAG": {"manufacturer_data": FOLLOWER}}
        stranger = {"STRANGER": {"manufacturer_data": FOLLOWER}}
        walk.stay(0, 300, HOME, at=HOME_AT)
        out = walking(300, HOME_AT, SHOP_AT, 1.2)
        walk.stay(300, 720, {**tag, **stranger}, at=out)  # a stranger along the way, 7 minutes
        walk.stay(720, 1500, tag, at=out)  # the tag all the way: 20 minutes, 1.4 km
        self.assertEqual(walk.verdict("STRANGER"), PASSING)
        self.assertEqual(walk.verdict("TAG"), TRAVELLING)
        seconds, metres = walk.watcher.trackers["TAG"].travelled  # the first minute out of the door is still "stopped"
        self.assertTrue(1100 <= seconds <= 1200 and 1300 <= metres <= 1450, (seconds, metres))
        walk.stay(1500, 2400, {}, at=out)  # it's gone: it stays flagged, until you forget it
        self.assertEqual(walk.verdict("TAG"), TRAVELLING)
        self.assertEqual(walk.watcher.forget_travelling(), 1)
        self.assertNotIn("TAG", walk.watcher.trackers)
        self.assertIn("STRANGER", walk.watcher.trackers)

        without = Walk()  # no phone: moving can't be told from staying
        without.stay(0, 300, HOME)
        without.stay(300, 1500, tag)
        self.assertEqual(without.watcher.trackers["TAG"].travelled, [0.0, 0.0])

    def test_a_landmark_heard_from_another_place_isnt_following(self):
        # home's fridge, heard at a café round the corner: the places are near, the fridge didn't move
        walk = Walk(groups=["other"])
        walk.stay(0, 900, HOME)
        walk.stay(900, 1200, {})
        walk.stay(1200, 1800, CAFE)
        walk.stay(1800, 2700, {**CAFE, "FRIDGE": HOME["FRIDGE"]})
        record = walk.watcher.trackers["FRIDGE"]
        self.assertEqual(len(record.places), 2)  # heard in both...
        self.assertTrue(record.landmark)
        self.assertEqual(record.verdict, STAYING)  # ... but it describes home: it stays put
        self.assertTrue(Watcher(walk.watcher.to_dict()).trackers["FRIDGE"].landmark)  # remembered

    def test_a_link_taken_back_splits_the_record(self):
        watcher = Watcher(groups=["phones"])
        record = watcher.trackers["NEW"] = TrackerRecord("NEW", "Phone", 0, 900, 700, {1, 2}, "phones",
                                                         [[1, 0, 400], [2, 500, 900]])
        watcher.mine.add("OLD")
        watcher.mine.add("NEW")  # carried over with the link
        watcher.take_back("OLD", ["NEW"], 450)
        old, new = watcher.trackers["OLD"], watcher.trackers["NEW"]
        self.assertIs(old, record)
        self.assertEqual((old.visits, old.places, old.seen_seconds, old.last_seen), ([[1, 0, 400]], {1}, 300, 400))
        self.assertEqual((new.visits, new.places, new.seen_seconds, new.first_seen), ([[2, 500, 900]], {2}, 400, 500))
        self.assertEqual(watcher.mine, {"OLD"})

    def test_forgetting_one_device_under_all_it_sends(self):
        from types import SimpleNamespace
        watcher, now = Watcher(), 1000.0
        for address in ("FIXED", "ROTATING", "OTHER"):
            watcher.trackers[address] = TrackerRecord(address, "Computer", now, now, group="trackers")
        mac = SimpleNamespace(address="FIXED", partners=[(SimpleNamespace(address="ROTATING"), "same")])
        self.assertTrue(watcher.forget_device(mac))
        self.assertEqual(set(watcher.trackers), {"OTHER"})
        self.assertFalse(watcher.forget_device(mac))

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

    def test_timeline_and_named_places(self):
        walk = Walk()
        follower = {"TAG": {"manufacturer_data": FOLLOWER}}
        walk.stay(0, 900, {**HOME, **follower})
        home = walk.watcher.settled
        walk.watcher.name_place(home, "  Home ")
        walk.stay(900, 1500, follower)
        walk.stay(1500, 2400, {**CAFE, **follower})
        visits = walk.watcher.trackers["TAG"].visits
        self.assertEqual([walk.watcher.place_name(v[0]) for v in visits],  # home takes a few minutes to recognize
                         ["unknown place", "Home", "unknown place", f"place {walk.watcher.settled.id}"])
        self.assertTrue(all(a[2] <= b[1] for a, b in zip(visits, visits[1:])))  # in order, no overlaps
        again = Watcher(walk.watcher.to_dict())
        self.assertEqual(again.places[home.id].name, "Home")
        self.assertEqual(again.trackers["TAG"].visits, visits)
        again._prune(home.last_seen + 90 * 86400)
        self.assertIn(home.id, again.places)  # named: kept for good
        walk.watcher.name_place(home, " ")
        self.assertIsNone(home.name)

    def test_a_marked_place_that_turns_out_known_keeps_its_name(self):
        walk = Walk()
        walk.stay(0, 900, {**HOME, "TAG": {"manufacturer_data": FOLLOWER}})
        home = walk.watcher.settled
        marked = walk.watcher.moved(900)  # pressed by mistake...
        walk.watcher.name_place(marked, "Home")  # ... and named straight away
        walk.stay(900, 1200, {**HOME, "TAG": {"manufacturer_data": FOLLOWER}})
        self.assertEqual(walk.watcher.settled.id, home.id)
        self.assertEqual(home.name, "Home")
        self.assertNotIn(marked.id, [v[0] for v in walk.watcher.trackers["TAG"].visits])

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
        walk = Walk(("trackers", "other"))
        gadget = {"name": "Gadget 7"}  # named and of no kind people carry, so it's also a landmark at home
        walk.stay(0, 900, {**HOME, "GADGET": gadget})
        walk.stay(900, 1500, {"GADGET": gadget})
        walk.stay(1500, 2400, {**CAFE, "GADGET": gadget})
        self.assertIn("GADGET", walk.watcher.companions)  # it describes no place any more...
        self.assertEqual(walk.watcher.mine, set())
        self.assertEqual(walk.verdict("GADGET"), FOLLOWING)  # ... and is still flagged

    def test_ones_people_carry_make_no_place(self):
        # in a queue of cars, strangers' phones and watches stay near you for minutes
        walk = Walk(("trackers", "phones"))
        strangers = {f"PHONE-{i}": {"name": f"Phone {i}"} for i in range(4)}
        walk.stay(0, 900, {**HOME, **strangers})
        home = walk.watcher.settled
        walk.stay(900, 1500, strangers)  # the landmarks gone: only the phones stay
        self.assertIsNone(walk.watcher.settled)
        self.assertEqual(set(walk.watcher.places), {home.id})

    def test_a_minute_either_side_of_a_move_isnt_following(self):
        walk = Walk()
        tag = {"TAG": {"manufacturer_data": FOLLOWER}}
        walk.stay(0, 870, HOME)
        walk.stay(870, 900, {**HOME, **tag})  # heard as you left...
        walk.stay(900, 1500, {})
        walk.stay(1500, 2400, CAFE)
        walk.stay(2400, 2430, {**CAFE, **tag})  # ... and for a moment where you arrived
        record = walk.watcher.trackers["TAG"]
        self.assertEqual(len(record.places), 2)
        self.assertNotEqual(record.verdict, FOLLOWING)

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

    def sends(self, groups, now: float = 100):
        """A Mac: Nearby Info from an address that changes, the same from a fixed named one, and an AirPlay
        target; and an Apple TV: an AirPlay target and Nearby Info. Each listed as one device."""
        nearby = {0x004C: bytes.fromhex("10060b1c2d3e4f50")}
        store, watcher = DeviceStore(), Watcher(groups=groups)
        store.ingest([Sighting("MAC-NEARBY", -70, now, manufacturer_data=nearby),
                      Sighting("MAC-FIXED", -70, now, name="Office MacBook", manufacturer_data=nearby),
                      Sighting("MAC-AIRPLAY", -70, now, manufacturer_data={0x004C: bytes.fromhex("0908" + "13" * 8)}),
                      Sighting("TV-AIRPLAY", -75, now, name="Living Room TV"),
                      Sighting("TV-NEARBY", -75, now, manufacturer_data={0x004C: bytes.fromhex("10060a0b0c0d0e0f")})])
        d = store.devices
        for group_ in (("MAC-NEARBY", "MAC-FIXED", "MAC-AIRPLAY"), ("TV-AIRPLAY", "TV-NEARBY")):
            for a in group_:
                d[a].partners = [(d[b], None) for b in group_ if b != a]
        return store, watcher

    def test_watched_as_what_its_listed_as(self):
        store, watcher = self.sends(("trackers", "phones"))
        watcher.update(100, list(store.devices.values()), force=True)
        self.assertEqual(watcher.watched(), [])  # a Mac is no phone, and a TV stays put
        store, watcher = self.sends(("trackers", "phones", "other"))
        watcher.update(100, list(store.devices.values()), force=True)
        self.assertEqual([(r.address, r.kind, r.group) for r in watcher.watched()],
                         [("MAC-FIXED", "Computer", "other")])  # once, not three times

    def test_what_the_watch_knew_joins_the_listed_advertisement(self):
        store, watcher = self.sends(("trackers", "phones", "other"))
        nearby = store.devices["MAC-NEARBY"]
        partners, nearby.partners = nearby.partners, []  # not known to be one device yet
        watcher.moved(0)
        for t in range(100, 1400, 15):
            store.ingest([Sighting("MAC-NEARBY", -70, t, manufacturer_data=nearby.manufacturer_data)])
            watcher.update(t, [nearby], force=True)
        self.assertEqual(watcher.record("MAC-NEARBY").verdict, STAYING)
        nearby.partners = partners  # now it is
        store.ingest([Sighting("MAC-FIXED", -70, 1400, name="Office MacBook")])
        watcher.update(1400, list(store.devices.values()), force=True)
        self.assertNotIn("MAC-NEARBY", watcher.trackers)
        record = watcher.record("MAC-FIXED")
        self.assertEqual(record.verdict, STAYING)  # its time with you came along
        self.assertEqual(record.visits[0][1], 100)

    def test_other_kinds_that_passed_by_are_forgotten_sooner(self):
        walk = Walk(("trackers", "phones"))
        walk.stay(0, 300, {**HOME, "PHONE": {"name": "Galaxy S24"}, "TAG": {"manufacturer_data": FOLLOWER}})
        walk.watcher.update(4 * 3600, [], force=True)
        self.assertNotIn("PHONE", walk.watcher.trackers)
        self.assertIn("TAG", walk.watcher.trackers)


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.store = DeviceStore({"BAG": {"alert_gone": True, "alert_back": True}})
        self.alerts = Alerts()

    def seconds(self, start: int, end: int, heard: list[str], scanning: bool = True) -> list[tuple[str, str]]:
        due = []
        for t in range(start, end):
            if scanning:
                self.store.ingest([Sighting(a, -60, t) for a in heard])
            due += [(d.address, what) for d, what in self.alerts.update(t, list(self.store.devices.values()),
                                                                          scanning)]
        return due

    def test_left_behind_and_back(self):
        self.assertEqual(self.seconds(0, 60, ["BAG", "TV"]), [])
        self.assertEqual(self.seconds(60, 120, ["TV"]), [("BAG", GONE)])  # quiet for 30 s: gone
        self.assertEqual(self.seconds(120, 130, ["BAG", "TV"]), [("BAG", BACK)])

    def test_only_devices_with_alerts(self):
        self.seconds(0, 60, ["BAG", "TV"])
        self.assertEqual(self.seconds(60, 120, ["BAG"]), [])  # the TV left, but nobody asked about it

    def test_arriving_after_the_room_is_heard(self):
        self.assertEqual(self.seconds(0, 40, ["TV"]), [])
        self.assertEqual(self.seconds(40, 45, ["TV", "BAG"]), [("BAG", BACK)])

    def test_starting_up_isnt_arriving(self):
        self.assertEqual(self.seconds(0, 60, ["BAG"]), [])

    def test_pausing_or_sleeping_isnt_leaving(self):
        self.seconds(0, 60, ["BAG"])
        self.assertEqual(self.seconds(60, 200, [], scanning=False), [])
        self.assertEqual(self.seconds(200, 260, []), [])  # scanning again, and it's not around: no news
        self.seconds(260, 300, ["BAG"])
        self.assertEqual(self.seconds(1000, 1060, []), [])  # asleep from 300 to 1000

    def test_one_on_the_edge_of_range_doesnt_nag(self):
        self.seconds(0, 60, ["BAG"])
        due = self.seconds(60, 100, []) + self.seconds(100, 110, ["BAG"]) + self.seconds(110, 150, [])
        self.assertEqual(due, [("BAG", GONE), ("BAG", BACK)])  # the second GONE waits out the cooldown


class BaselineTest(unittest.TestCase):
    def test_only_what_turns_up_afterwards_is_new(self):
        store = DeviceStore()
        store.ingest([Sighting("KEYBOARD", -50, 0, name="Desk Keyboard"), Sighting("TAG", -80, 0)])
        baseline = Baseline(10, store.devices.values())
        store.ingest([Sighting("STRANGER", -60, 20),
                      Sighting("KEYBOARD-2", -50, 20, name=" desk keyboard ")])  # a known name, a new address
        new = [d.address for d in store.devices.values() if baseline.is_new(d)]
        self.assertEqual(new, ["STRANGER"])
        self.assertEqual(baseline.count_new(store.devices.values(), 21), 1)
        self.assertEqual(baseline.count_new(store.devices.values(), 60), 0)  # out of range by then

    def test_arrivals_are_announced_once_and_near_enough(self):
        store = DeviceStore()
        baseline = Baseline(0, [])
        store.ingest([Sighting("CLOSE", -55, t) for t in (10, 11, 14)] + [Sighting("FAR", -92, t) for t in (10, 14)])
        devices = list(store.devices.values())
        self.assertEqual(baseline.arrivals(11, devices), [])  # heard for a moment: its name may still come
        near = lambda d: d.smoothed >= -80
        self.assertEqual([d.address for d in baseline.arrivals(14, devices, near)], ["CLOSE"])
        self.assertEqual(baseline.arrivals(15, devices, near), [])  # once


NEARBY_INFO = {0x004C: bytes.fromhex("1006" + "0b1c2d3e4f50")}  # an Apple device's Nearby Info
HANDOFF = {0x004C: bytes.fromhex("0c0e" + "00" * 14)}


class AddressChangeTest(unittest.TestCase):
    """A device changing its address, the way an hour at home showed it."""

    def play(self, spans: dict, until: int, names: dict | None = None, extra: dict | None = None,
             deaf: tuple | None = None):
        """spans: address -> (from, to, rssi, advert), a packet a second; an advert that's a list takes
        turns, and one that's a function says what's sent when (None: nothing). names: the ones macOS
        knows. extra: more of an address's packet, like its TX power. deaf: (from, to) when the Mac
        itself hears nothing, asleep."""
        names, extra = names or {}, extra or {}
        store, linker, links = DeviceStore(), Linker(), []
        crowd = {f"FAR-{i}": (0, until, -95 - i, {0x0075: bytes([i])}) for i in range(8)}  # the faint ones around
        for t in range(until * 2):
            t /= 2
            sent = {a: (rssi, advert(t) if callable(advert) else advert[int(t) % len(advert)]
                        if isinstance(advert, list) else advert)
                    for a, (start, end, rssi, advert) in {**spans, **crowd}.items() if start <= t <= end and t % 1 == 0}
            if deaf and deaf[0] < t < deaf[1]:
                sent = {}
            store.ingest([Sighting(a, rssi, t, name=names.get(a), **extra.get(a, {}), manufacturer_data=md)
                          for a, (rssi, md) in sent.items() if md is not None])
            for old in linker.taken_back(store.devices):  # the links left are the ones that stood
                links = [x for x in links if (x[0], x[1]) != (old.address, old.superseded_by)]
                store.take_back(old)
            for old, new, sure in linker.update(t, store.devices.values()):
                store.hand_over(old, new, sure)
                links.append((old.address, new.address, sure))
        self.linker = linker
        return store, links

    def test_a_device_changing_its_address_is_followed(self):
        store, links = self.play({"OLD": (0, 100, -69, NEARBY_INFO), "NEW": (103, 200, -68, NEARBY_INFO)}, 200)
        self.assertEqual([(o, n) for o, n, _ in links], [("OLD", "NEW")])
        self.assertGreater(links[0][2], 0.8)
        new, old = store.devices["NEW"], store.devices["OLD"]
        self.assertEqual(old.superseded_by, "NEW")
        self.assertEqual(new.earlier, [("OLD", links[0][2])])
        self.assertEqual(new.first_seen, 0)  # one device, heard since the start
        self.assertEqual(new.history[0][0], 0)

    def test_a_name_carries_over(self):
        # macOS knew the old address's name after a connection; the new address starts without one
        store, links = self.play({"OLD": (0, 100, -69, NEARBY_INFO), "NEW": (103, 200, -68, NEARBY_INFO)}, 200,
                                 names={"OLD": "Office Mac"})
        self.assertEqual([(o, n) for o, n, _ in links], [("OLD", "NEW")])
        self.assertEqual(store.devices["NEW"].title, "Office Mac")

    def test_two_names_are_two_devices(self):
        _, links = self.play({"OLD": (0, 100, -69, NEARBY_INFO), "NEW": (103, 200, -68, NEARBY_INFO)}, 200,
                             names={"OLD": "Office Mac", "NEW": "Kitchen iPad"})
        self.assertEqual(links, [])

    def test_a_stranger_isnt_it(self):
        _, links = self.play({"OLD": (0, 100, -69, NEARBY_INFO),
                              "OTHER-KIND": (102, 200, -69, HANDOFF),  # a different advertisement
                              "FAR-AWAY": (102, 200, -97, NEARBY_INFO)}, 200)  # the same, but from elsewhere
        self.assertEqual(links, [])

    def test_two_alike_changing_together_stay_apart(self):
        _, links = self.play({"A": (0, 100, -69, NEARBY_INFO), "B": (0, 100, -70, NEARBY_INFO),
                              "A2": (102, 200, -69, NEARBY_INFO), "B2": (103, 200, -70, NEARBY_INFO)}, 200)
        self.assertEqual(links, [])  # could be either way round: no guessing

    def test_one_still_talking_wasnt_replaced(self):
        _, links = self.play({"OLD": (0, 150, -69, NEARBY_INFO), "NEW": (103, 200, -69, NEARBY_INFO)}, 200)
        self.assertEqual(links, [])

    def test_one_that_only_paused_is_taken_back(self):
        # a Mac's named advertisement, whose address macOS keeps, quiet for half a minute while its
        # twin turns up at a new address: taken for a change, until the old address talks again
        paused = lambda t: NEARBY_INFO if t <= 100 or t >= 130 else None
        store, links = self.play({"OLD": (0, 200, -69, paused), "NEW": (103, 200, -68, NEARBY_INFO)}, 200,
                                 names={"OLD": "Office Mac"})
        self.assertEqual(links, [])
        self.assertIn(("OLD", "NEW"), self.linker._wrong)  # it was made, and taken back
        old, new = store.devices["OLD"], store.devices["NEW"]
        self.assertIsNone(old.superseded_by)
        self.assertEqual((old.title, old.first_seen, old.history[0][0]), ("Office Mac", 0, 0))
        self.assertEqual((new.earlier, new.first_seen, new.history[0][0]), ([], 103, 103))
        self.assertNotEqual(new.title, "Office Mac")  # the name it was lent is gone
        self.assertEqual(new.packets, 97)  # its own, from 103 s on
        self.assertEqual(self.linker.to_dict(), {})  # and what the link taught

    def test_taking_back_one_link_of_a_chain(self):
        # A only paused, but B took over from it and later changed to C: B and C are one device, without A
        store = DeviceStore()
        store.ingest([Sighting("A", -60, t, name="Speaker") for t in range(0, 10)])
        store.ingest([Sighting("B", -60, t) for t in range(20, 30)])
        store.hand_over(store.devices["A"], store.devices["B"], 0.7)
        store.ingest([Sighting("C", -60, t) for t in range(40, 50)])
        store.hand_over(store.devices["B"], store.devices["C"], 0.9)
        self.assertEqual((store.devices["C"].title, store.devices["C"].first_seen), ("Speaker", 0))
        store.ingest([Sighting("A", -60, 60)])
        later = store.take_back(store.devices["A"])
        self.assertEqual([d.address for d in later], ["B", "C"])
        c = store.devices["C"]
        self.assertEqual((c.earlier, c.first_seen, c.packets, c.name), ([("B", 0.9)], 20, 20, None))
        self.assertEqual([t for t, _ in c.history], list(range(20, 30)) + list(range(40, 50)))
        self.assertEqual(store.devices["B"].superseded_by, "C")

    def test_a_tx_power_not_heard_yet_doesnt_hide_it(self):
        # a Mac leaves its TX power out of some packets: a new address may go minutes without it
        spans = {"OLD": (0, 100, -69, NEARBY_INFO), "NEW": (103, 200, -68, NEARBY_INFO)}
        _, links = self.play(spans, 200, extra={"OLD": {"tx_power": 12}})
        self.assertEqual([(o, n) for o, n, _ in links], [("OLD", "NEW")])
        _, links = self.play(spans, 200, extra={"OLD": {"tx_power": 12}, "NEW": {"tx_power": 4}})
        self.assertEqual(links, [])  # two different ones known: two kinds of device

    def test_different_service_data_is_another_kind(self):
        # one headset sends Google's Fast Pair data from one address and another maker's from another
        spans = {"FAST-PAIR": (0, 100, -69, {}), "OTHER": (103, 200, -68, {})}
        _, links = self.play(spans, 200, extra={"FAST-PAIR": {"service_data": {uuid16(0xFE2C): b"\x0a\x1b"}},
                                                "OTHER": {"service_data": {uuid16(0xFE03): b""}}})
        self.assertEqual(links, [])

    @staticmethod
    def every(seconds: int, advert: dict):
        return lambda t: advert if t % seconds == 0 else None

    def test_one_heard_every_10_s_may_leave_a_longer_gap(self):
        # like a Find My device near its owner: a packet every 10 s, and a minute's gap at a change
        find_my = {0x004C: bytes.fromhex("12020001")}
        _, links = self.play({"OLD": (0, 300, -72, self.every(10, find_my)),
                              "NEW": (365, 700, -72, self.every(10, find_my))}, 700)
        self.assertEqual([(o, n) for o, n, _ in links], [("OLD", "NEW")])

    def test_one_heard_once_a_minute_cant_be_timed(self):
        find_my = {0x004C: bytes.fromhex("12020001")}
        _, links = self.play({"OLD": (0, 300, -72, self.every(60, find_my)),
                              "NEW": (310, 900, -72, self.every(2, find_my))}, 900)
        self.assertEqual(links, [])  # its silence looks like a change, and the change could be anywhere

    def test_two_addresses_never_link_both_ways(self):
        # two addresses at the edge of range, each heard just before and after the other
        store, linker = DeviceStore(), Linker()
        store.ingest([Sighting(f"FAR-{i}", -95 - i, t, manufacturer_data={0x0075: bytes([i])})
                      for i in range(8) for t in range(17)])  # the faint ones around
        store.ingest([Sighting("A", -69, t, manufacturer_data=NEARBY_INFO) for t in (0, 2.5)]
                     + [Sighting("B", -69, t, manufacturer_data=NEARBY_INFO) for t in (1, 1.5, 2)])
        links = []
        for now in (12, 14, 16):
            for old, new, sure in linker.update(now, store.devices.values(), force=True):
                store.hand_over(old, new, sure)
                links.append((old.address, new.address))
        self.assertLessEqual(len(links), 1)

    @staticmethod
    def airplay(ident: bytes, salt: int) -> dict:
        """Like an AirPlay target: steady bytes (its network address) and a rotating part."""
        return {0x004C: bytes([0x09, 8]) + ident + bytes([0x16, 8]) + bytes((salt * 37 + i * 11) % 256 for i in range(8))}

    @staticmethod
    def nearby(salt: int) -> dict:
        """Like Apple's Nearby Info: every byte but the header changes."""
        return {0x004C: bytes([0x10, 6]) + bytes((salt * 53 + i * 29) % 256 for i in range(6))}

    def test_learned_bytes_recognize_a_change_nobody_heard(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        spans = {"A1": (0, 100, -69, self.airplay(home, 1)), "A2": (103, 200, -69, self.airplay(home, 2)),
                 "A3": (203, 300, -69, self.airplay(home, 3)),
                 "A4": (520, 700, -75, self.airplay(home, 4)),  # 220 s of silence: the Mac slept
                 "STRANGER": (521, 700, -74, self.airplay(bytes.fromhex("1302c0a8004b1b58"), 5))}
        _, links = self.play(spans, 700)
        self.assertEqual([(o, n) for o, n, _ in links], [("A1", "A2"), ("A2", "A3"), ("A3", "A4")])
        self.assertGreater(links[-1][2], 0.9)

    def test_one_change_is_enough_to_start_recognizing(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        spans = {"A1": (0, 100, -69, self.airplay(home, 1)), "A2": (103, 200, -69, self.airplay(home, 2)),
                 "A3": (420, 600, -75, self.airplay(home, 3)),  # after one heard change, one nobody heard
                 "STRANGER": (421, 600, -74, self.airplay(bytes.fromhex("1302c0a8004b1b58"), 5))}
        store, links = self.play(spans, 600)
        self.assertEqual([(o, n) for o, n, _ in links], [("A1", "A2"), ("A2", "A3")])
        self.assertFalse(store.devices["A3"].fingerprint_confirmed)  # tentative so far

    def test_a_byte_the_same_by_chance_doesnt_block_the_next_change(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        first, second = self.airplay(home, 1), self.airplay(home, 2)
        second[0x004C] = second[0x004C][:12] + first[0x004C][12:13] + second[0x004C][13:]  # one rotating byte alike
        spans = {"A1": (0, 100, -69, first), "A2": (103, 200, -69, second),
                 "A3": (203, 300, -69, self.airplay(home, 3))}  # heard, and that byte differs now
        store, links = self.play(spans, 300)
        self.assertEqual([(o, n) for o, n, _ in links], [("A1", "A2"), ("A2", "A3")])
        self.assertEqual(store.devices["A3"].fingerprint_bytes, 8)  # the chance byte dropped: the steady 8 are left

    def test_an_occasional_extra_message_doesnt_hide_it(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        usual = self.airplay(home, 1)
        extra = {0x004C: usual[0x004C][:10] + bytes([0x15, 2, 0x00, 0x34]) + usual[0x004C][10:]}
        # the old address's last packet happens to carry the extra message
        _, links = self.play({"OLD": (0, 99, -69, [extra, usual, usual]),
                              "NEW": (102, 200, -69, self.airplay(home, 2))}, 200)
        self.assertEqual([(o, n) for o, n, _ in links], [("OLD", "NEW")])

    def test_a_status_byte_isnt_a_fingerprint(self):
        spans = {f"T{i}": (i * 103, i * 103 + 100, -69, {0x004C: bytes([0x12, 2, 0x00, i % 4])}) for i in range(3)}
        spans["T3"] = (600, 700, -69, {0x004C: bytes([0x12, 2, 0x00, 3])})
        _, links = self.play(spans, 700)
        self.assertNotIn(("T2", "T3"), [(o, n) for o, n, _ in links])  # unheard, and nothing to go on

    def test_partners_change_together_and_come_along(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        spans = {}
        for i, (start, end) in enumerate([(0, 100), (103, 200), (203, 300), (520, 700)]):
            spans[f"TV{i}"] = (start, end, -69, self.airplay(home, i))
            spans[f"NI{i}"] = (start + 1, end, -68, self.nearby(i))  # the same box's other advertisement
        store, links = self.play(spans, 700)
        pairs = [(o, n) for o, n, _ in links]
        self.assertIn(("TV2", "TV3"), pairs)  # by its fingerprint...
        self.assertIn(("NI2", "NI3"), pairs)  # ... and its partner along with it
        tv, ni = store.devices["TV3"], store.devices["NI3"]
        self.assertEqual(tv.partners, [(ni, "changes")])

    def test_a_partner_left_over_from_a_missed_change_isnt_carried(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        spans = {"TV0": (0, 100, -69, self.airplay(home, 0)), "TV1": (103, 200, -69, self.airplay(home, 1)),
                 "TV2": (203, 596, -69, self.airplay(home, 2)), "TV3": (599, 700, -69, self.airplay(home, 3))}
        spans.update({"NI0": (1, 100, -68, self.nearby(0)), "NI1": (104, 200, -68, self.nearby(1)),
                      "NI2": (204, 300, -68, self.nearby(2)),
                      "NI3": (400, 600, -68, self.nearby(3)),  # a change missed: 100 s of silence
                      "NI4": (602, 700, -68, self.nearby(4))})
        _, links = self.play(spans, 700)
        pairs = [(o, n) for o, n, _ in links]
        self.assertIn(("TV2", "TV3"), pairs)
        self.assertIn(("NI3", "NI4"), pairs)  # its latest address, not NI2, quiet for 5 minutes by then

    def test_a_fixed_twin_carries_the_other_through_a_change_nobody_heard(self):
        # a Mac sends its Nearby Info from an address that changes, and the same bytes from a fixed one
        def fixed(t):
            if 200 < t < 330:
                return None  # asleep: nothing heard from anyone
            return self.nearby(1) if t <= 101 else self.nearby(2) if t <= 200 else self.nearby(3)
        spans = {"R1": (0, 100, -69, self.nearby(1)), "R2": (103, 200, -69, self.nearby(2)),
                 "R3": (330, 500, -69, self.nearby(3)), "FIXED": (0, 500, -68, fixed)}
        store, links = self.play(spans, 500, names={"FIXED": "Office Mac"})
        self.assertIn(("R2", "R3", 0.98), links)  # 130 s apart, and Nearby Info keeps no byte
        r3, mac = store.devices["R3"], store.devices["FIXED"]
        self.assertEqual(r3.partners, [(mac, "same")])
        self.assertEqual(mac.partners, [(r3, "same")])
        self.assertIs(lead(r3, mac), mac)  # listed once, by its name

    def test_listed_under_one_still_heard(self):
        store = DeviceStore()
        store.ingest([Sighting("FIXED", -70, 0, name="Office Mac"), Sighting("ROT", -70, 100, manufacturer_data=NEARBY_INFO)])
        fixed, rotating = store.devices["FIXED"], store.devices["ROT"]
        fixed.partners, rotating.partners = [(rotating, "same")], [(fixed, "same")]
        self.assertIs(listed(fixed), rotating)  # the named one has been quiet for 100 s: the other stands in
        store.ingest([Sighting("FIXED", -70, 101, name="Office Mac")])
        self.assertIs(listed(rotating), fixed)

    def test_two_beacons_set_up_alike_stay_two(self):
        ibeacon = {0x004C: bytes.fromhex("0215" "3b8f1c24a7e54d0b9c3f5e6a7b8c9d0e" "0001002ac5")}
        store, links = self.play({"B1": (0, 300, -69, ibeacon), "B2": (0, 300, -75, ibeacon)}, 300)
        self.assertEqual(links, [])
        self.assertEqual((store.devices["B1"].partners, store.devices["B2"].partners), ([], []))

    def mac_switched_off(self, off=(200, 300)):
        """A Mac's Nearby Info (a new address when it's back), its fixed named advert, and its own Find My
        advert, heard every 10 s, switched off and on again; a phone nearby carries on."""
        find_my = {0x004C: bytes.fromhex("12020001")}
        running = lambda advert: lambda t: None if off[0] < t < off[1] else advert
        return {"NEARBY-1": (0, off[0], -70, self.nearby(1)), "NEARBY-2": (off[1] + 1, 500, -70, self.nearby(2)),
                "FIXED": (0, 500, -70, running(self.nearby(9))),
                "FIND-MY": (0, 500, -72, lambda t: None if off[0] - 8 < t < off[1] or t % 10 else find_my),
                "PHONE": (0, 500, -65, {0x004C: bytes.fromhex("1005031c2d3e4f")})}

    def test_going_off_and_on_together_is_one_device(self):
        store, _ = self.play(self.mac_switched_off(), 500, names={"FIXED": "Office Mac"})
        find_my, mac = store.devices["FIND-MY"], store.devices["FIXED"]
        self.assertIn((mac, "cycles"), find_my.partners)
        self.assertNotIn(store.devices["PHONE"], [p for p, _ in find_my.partners])  # it kept talking

    def test_the_mac_asleep_isnt_everything_switched_off(self):
        spans = self.mac_switched_off()
        spans["FIXED"] = (0, 500, -70, self.nearby(9))
        spans["FIND-MY"] = (0, 500, -72, self.every(10, {0x004C: bytes.fromhex("12020001")}))
        store, _ = self.play(spans, 500, names={"FIXED": "Office Mac"}, deaf=(200, 300))
        self.assertEqual(store.devices["FIND-MY"].partners, [])

    def test_two_tvs_on_one_power_strip_stay_two(self):
        off = lambda t: None if 200 < t < 300 else {}
        store, _ = self.play({"TV": (0, 500, -70, off), "SOUNDBAR": (0, 500, -71, off)}, 500,
                             names={"TV": "Living Room TV", "SOUNDBAR": "Soundbar"})
        self.assertEqual(store.devices["TV"].partners, [])  # only Apple's advertisements are joined this way

    def test_what_is_learned_is_kept(self):
        home = bytes.fromhex("1302c0a8004a1b58")
        store = DeviceStore()
        store.ingest([Sighting("A", -69, 0, manufacturer_data=self.airplay(home, 1)),
                      Sighting("B", -69, 1, manufacturer_data=self.airplay(home, 2))])
        linker = Linker()
        for _ in range(2):
            linker.learn(store.devices["A"], store.devices["B"], 0.9)
        again = Linker(linker.to_dict())
        self.assertEqual(again.fingerprint(store.devices["B"]), home)

    def test_what_carries_over(self):
        store = DeviceStore({"OLD": {"nickname": "Kitchen iPad", "pinned": True, "alert_gone": True}})
        store.ingest([Sighting("OLD", -69, 0), Sighting("NEW", -69, 5)])
        store.hand_over(store.devices["OLD"], store.devices["NEW"], 0.9)
        new = store.devices["NEW"]
        self.assertEqual((new.nickname, new.pinned, new.alert_gone), ("Kitchen iPad", True, True))
        self.assertEqual(store.known, {"NEW": {"nickname": "Kitchen iPad", "pinned": True, "alert_gone": True}})
        watcher = Watcher(groups=("trackers", "phones"))
        watcher.trackers["OLD"] = __import__("blue_station.core.watch", fromlist=["TrackerRecord"]).TrackerRecord(
            "OLD", "Phone", 0, 4, 4.0, {1, 2}, "phones", [[1, 0, 2], [2, 3, 4]])
        watcher.mine.add("OLD")
        watcher.hand_over("OLD", "NEW")
        self.assertEqual(watcher.trackers["NEW"].places, {1, 2})  # still following you
        self.assertIn("NEW", watcher.mine)
        alerts = Alerts()
        alerts.hand_over("OLD", "NEW")
        self.assertTrue(alerts._here["NEW"])  # not an arrival
        baseline = Baseline(0, [store.devices["OLD"]])
        self.assertFalse(baseline.is_new(new))  # known under its earlier address


class LoginTest(unittest.TestCase):
    def test_starting_at_login(self):
        import os
        import plistlib
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)  # never the real ~/Library
            self.assertFalse(login.enabled(home))
            login.enable(home, python=sys.executable)
            self.assertTrue(login.enabled(home))
            bundle = login.bundle_path(home)
            job = plistlib.loads(login.agent_path(home).read_bytes())
            self.assertEqual(job["ProgramArguments"], ["/usr/bin/open", "-g", "-a", str(bundle), "--args",
                                                       "--background"])
            self.assertTrue(job["RunAtLoad"])
            info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
            self.assertIn("Bluetooth", info["NSBluetoothAlwaysUsageDescription"])
            script = bundle / "Contents" / "MacOS" / info["CFBundleExecutable"]
            self.assertTrue(os.access(script, os.X_OK))
            run = subprocess.run([str(script), "--help"], capture_output=True, text=True, cwd=tmp, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)  # it finds this Python and this Blue Station
            self.assertIn("--background", run.stdout)
            login.disable(home)
            self.assertFalse(login.enabled(home))
            self.assertFalse(bundle.exists())


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


class PositionTest(unittest.TestCase):
    def test_a_packet_between_two_positions_is_placed_on_the_line(self):
        track = Track([Fix(1000, 60.0, 25.0, 5), Fix(1100, 60.002, 25.0, 12), Fix(5000, 61.0, 25.0)])
        self.assertEqual(track.at(1000), (60.0, 25.0, 5, 0.0))  # a real position
        lat, lon, acc, gap = track.at(1025)
        self.assertAlmostEqual(lat, 60.0005)
        self.assertEqual((lon, acc, gap), (25.0, 12, 25))  # the less sure of the two, and 25 s from the nearest
        self.assertEqual(track.at(1130)[3], 30)  # a long gap after it: the nearest, if within a minute...
        self.assertIsNone(track.at(1200))  # ... and nothing further off
        self.assertIsNone(track.at(900))
        self.assertEqual(track.add([Fix(1000, 0, 0), Fix(1050, 60.001, 25.0)]), 1)  # one position per moment

    def test_owntracks_posts(self):
        one = b'{"_type":"location","lat":60.1,"lon":24.9,"tst":1700000000,"acc":7,"tid":"ph"}'
        self.assertEqual(owntracks_fixes(one), [Fix(1700000000, 60.1, 24.9, 7)])
        queued = (b'[{"_type":"location","lat":60.1,"lon":24.9,"tst":1700000000},'
                  b'{"_type":"transition","event":"leave"},{"_type":"location","lat":999,"lon":0,"tst":1},'
                  b'{"_type":"location","lat":"x","lon":0,"tst":2}]')
        self.assertEqual(owntracks_fixes(queued), [Fix(1700000000, 60.1, 24.9)])
        self.assertEqual(owntracks_fixes(b"not json"), [])

    def test_the_phone_posts_its_positions(self):
        import base64
        import json
        import re
        import urllib.error
        import urllib.parse
        import urllib.request
        phone = PhoneReceiver("word42", port=0)
        self.assertIsNone(phone.start())
        try:
            base = f"http://127.0.0.1:{phone.port}"
            post = lambda path, body: urllib.request.urlopen(urllib.request.Request(
                base + path, data=body, headers={"Content-Type": "application/json"}), timeout=5)
            reply = post("/word42", b'{"_type":"location","lat":60.1,"lon":24.9,"tst":1700000000,"acc":7}')
            self.assertEqual((reply.status, reply.read()), (200, b"[]"))  # what OwnTracks expects back
            with self.assertRaises(urllib.error.HTTPError) as refused:
                post("/guess", b'{"_type":"location","lat":1,"lon":1,"tst":1}')  # someone else's post
            refused.exception.close()
            with urllib.request.urlopen(base + "/word42", timeout=5) as page:  # opened in the phone's browser
                text = page.read().decode()
            self.assertIn("hears you", text)
            link = re.search(r'href="(owntracks:///config\?inline=[^"]+)"', text).group(1)
            config = json.loads(base64.b64decode(urllib.parse.unquote(link.split("inline=")[1])))
            self.assertEqual((config["mode"], config["url"], config["monitoring"]),
                             (3, f"{base}/word42", 2))  # HTTP mode, to where the phone reached it, Move mode
            self.assertEqual(phone.drain(), [Fix(1700000000, 60.1, 24.9, 7)])
            self.assertEqual(phone.drain(), [])
            self.assertEqual(phone.latest, Fix(1700000000, 60.1, 24.9, 7))
            self.assertIsNotNone(phone.heard)
        finally:
            phone.stop()
        self.assertFalse(phone.listening)

    def test_packets_are_exported_with_where_they_were_heard(self):
        log = PacketLog()
        log.start(None)
        log.add([Sighting("A", -60, t) for t in (1000, 1050, 2000)])
        self.assertEqual(log.add_track([Fix(1000, 60.0, 25.0, 5), Fix(1100, 60.002, 25.0, 5), Fix(9000, 0, 0)]), (3, 2))
        self.assertEqual(log.add_track([Fix(9000, 0, 0)] * 50), (0, 0))  # one moment, fifty times: nothing new
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.csv"
            log.export(path)
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(read_packets(path)), 3)  # still reads back
        self.assertEqual([(r["latitude"], r["longitude"], r["position_accuracy_m"], r["position_gap_s"]) for r in rows],
                         [("60.000000", "25.000000", "5", "0"), ("60.001000", "25.000000", "5", "50"), ("", "", "", "")])
        log.clear()
        self.assertEqual(len(log.track), 0)


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

    def test_an_export_reads_back_as_heard(self):
        log = PacketLog()
        log.start(None)
        heard = [Sighting("A", -60, 1.25, name="Band", tx_power=4, manufacturer_data={0x004C: bytes.fromhex("1006aabb")},
                          service_uuids=(uuid16(0x180D),), service_data={uuid16(0xFE03): b""}, connectable=True),
                 Sighting("B", -71, 2.5)]
        log.add(heard)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.csv"
            log.export(path)
            self.assertEqual(read_packets(path), heard)

    def test_asking_who_each_device_is(self):
        log, store = PacketLog(), DeviceStore()
        store.ingest([Sighting("NEAR", -50, 100, connectable=True), Sighting("FAR", -80, 100, connectable=True),
                      Sighting("BEACON", -40, 100, connectable=False), Sighting("QUIET", -45, 90, connectable=True)])
        devices = list(store.devices.values())
        self.assertIsNone(log.next_to_ask(100, devices))  # not recording
        log.start(None)
        self.assertIsNone(log.next_to_ask(100, devices))  # recording, without Read names
        log.read_names = True
        self.assertEqual(log.next_to_ask(100, devices).address, "NEAR")  # the strongest that can be asked
        log.asking("NEAR")
        self.assertEqual(log.next_to_ask(100, devices).address, "FAR")  # each address once
        self.assertIsNone(log.next_to_ask(100, devices, near=lambda d: d.smoothed >= -60))
        log.stop()
        log.start("NEAR")
        self.assertIsNone(log.next_to_ask(100, devices))  # recording one device: only that one
        log.answer("NEAR", identity_text([("Device name", "Mac"), ("Model", "Mac16,8")]))
        log.asking("FAR")
        self.assertEqual(log.read_counts(), (1, 0, 1))
        log.answer("FAR", "error=the device didn't answer")
        self.assertEqual(log.read_counts(), (1, 1, 0))

        log.add([Sighting("NEAR", -50, 101, connectable=True), Sighting("NEW", -52, 102, connectable=True)])
        log.address = None
        log.add([Sighting("NEW", -52, 102, connectable=True)])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.csv"
            log.export(path, links={"NEW": ("NEAR", 0.834)})
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["identity"], "Device name=Mac; Model=Mac16,8")
        self.assertEqual((rows[0]["changed_from"], rows[0]["change_sure"]), ("", ""))
        self.assertEqual((rows[1]["identity"], rows[1]["changed_from"], rows[1]["change_sure"]), ("", "NEAR", "0.83"))
        log.clear()
        log.answer("NEAR", "Device name=Mac")  # the answer to a question thrown away
        self.assertEqual(log.identities, {})


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


class InfoClient(FakeClient):
    """A device with a model number and a battery level."""
    asked = []

    def __init__(self, target, disconnected_callback=None, timeout=20):
        super().__init__(target, disconnected_callback, timeout)
        self.services = [FakeService(uuid16(0x180A), 10, [FakeChar(uuid16(0x2A24), 11, ["read"])]),
                         FakeService(uuid16(0x180F), 20, [FakeChar(uuid16(0x2A19), 21, ["read", "notify"])])]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def read_gatt_char(self, char):
        InfoClient.asked.append(char.handle)
        return bytearray(b"EX-100" if char.handle == 11 else b"\x4c")


class QuickReadTest(unittest.TestCase):
    def test_the_whole_look_and_just_who_it_is(self):
        with mock.patch("bleak.BleakClient", InfoClient):
            self.assertEqual(asyncio.run(gatt.read("ADDRESS")).summary, [("Model", "EX-100"), ("Battery", "76 %")])
            InfoClient.asked.clear()
            self.assertEqual(asyncio.run(gatt.read("ADDRESS", only=gatt.IDENTITY)).summary, [("Model", "EX-100")])
        self.assertEqual(InfoClient.asked, [11])  # not the battery: that might ask to pair


class FakeBleakScanner:
    """Stands in for bleak's BleakScanner."""
    made = []

    def __init__(self, detection_callback=None):
        self.started = self.stopped = 0
        FakeBleakScanner.made.append(self)

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


class ScannerTest(unittest.TestCase):
    def test_scanning_starts_again_from_scratch(self):
        scanner, made = Scanner(), FakeBleakScanner.made
        made.clear()
        try:
            with mock.patch("bleak.BleakScanner", FakeBleakScanner):
                scanner.start()
                deadline = time.time() + 3
                while scanner.state != "scanning" and time.time() < deadline:
                    time.sleep(0.02)
                scanner.restart()  # the Mac woke up: its scan has quietly stopped
                while not (len(made) == 2 and made[1].started) and time.time() < deadline:
                    time.sleep(0.02)
            self.assertEqual([(m.started, m.stopped) for m in made], [(1, 1), (1, 0)])
            self.assertEqual(scanner.state, "scanning")
        finally:
            scanner.close()


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
