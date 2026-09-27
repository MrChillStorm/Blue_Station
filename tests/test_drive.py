"""tools/drive_check.py: a made-up trip, from home to a shop and back, as a
GPS track and the packets heard on the way. No Qt."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blue_station.core.devices import Sighting
from tools import drive_check

T0 = 1_800_000_000.0
HOME, SHOP = (60.1700, 24.9400), (60.1700, 25.0500)  # made up, about 6 km apart
NEARBY = {0x004C: bytes.fromhex("10060b1c2d3e4f50")}  # a phone that comes along


def where(t: float):
    """Home for 10 min, 10 min driving there, 10 min at the shop, 10 min back, home for 10 min."""
    m = (t - T0) / 60
    if m < 10 or m >= 40:
        return HOME
    if 20 <= m < 30:
        return SHOP
    f = (m - 10) / 10 if m < 20 else 1 - (m - 30) / 10
    return HOME[0], HOME[1] + f * (SHOP[1] - HOME[1])


def gpx(path: Path, gaps_while_standing: bool = False) -> None:
    points = []
    for s in range(0, 50 * 60 + 1, 5):
        t = T0 + s
        standing = where(t) in (HOME, SHOP) and where(t - 5) == where(t) and where(t + 5) == where(t)
        if gaps_while_standing and standing and s % 300:
            continue  # like an app that saves nothing while you stand still
        lat, lon = where(t)
        stamp = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"><time>{stamp}</time></trkpt>')
    path.write_text('<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
                    f'<trk><trkseg>{"".join(points)}</trkseg></trk></gpx>')


def packets() -> list[Sighting]:
    out = []
    for s in range(0, 50 * 60, 5):
        t, here = T0 + s, where(T0 + s)
        out.append(Sighting("PHONE", -50, t, manufacturer_data=NEARBY))
        names = {HOME: ("Living Room TV", "Fridge", "Printer"),
                 SHOP: ("Espresso Machine", "Cafe Speaker", "Till")}.get(here)
        if names:
            out += [Sighting(f"{here}-{n}", -70, t + 1, name=n) for n in names]
        else:  # on the road: someone new every half minute, heard for a moment
            out.append(Sighting(f"CAR-{s // 30}", -80, t + 2, name=f"Car {s // 30}"))
    return out


class DriveCheckTest(unittest.TestCase):
    def test_the_track_is_boiled_down_to_stops_and_places(self):
        for gaps in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "track.gpx"
                gpx(path, gaps_while_standing=gaps)
                key = drive_check.key_from_gpx(path)
            places = [p for _, _, p in key["stays"]]
            self.assertEqual(places, [1, 2, 1], f"gaps while standing: {gaps}")
            for (a, b, _), minutes in zip(key["stays"], (10, 10, 10)):
                self.assertAlmostEqual((b - a) / 60, minutes, delta=1.5)
            self.assertNotIn("60.17", str(key))  # times and place numbers only

    def test_walking_up_and_down_a_street_is_no_stop(self):
        lat, lon = HOME
        walking = []
        for s in range(0, 10 * 60, 2):  # 1.3 m/s, turning back every 80 m: never 100 m from where it began
            leg = s * 1.3 % 160
            walking.append((T0 + s, lat + min(leg, 160 - leg) / 111_320, lon))
        self.assertEqual(drive_check.stays(walking), [])
        standing = [(T0 + s, lat + s % 3 / 111_320, lon) for s in range(0, 10 * 60, 2)]  # a few metres of jitter
        self.assertEqual(len(drive_check.stays(standing)), 1)

    def test_the_watch_is_checked_against_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "track.gpx"
            gpx(path)
            key = drive_check.key_from_gpx(path)
        timeline, watcher, store = drive_check.replay(packets(), None, ["trackers", "phones"], None)
        text = "\n".join(drive_check.report(key, timeline, watcher, store))
        stops = [line for line in text.splitlines() if line.startswith("  place ") and ":" in line and "recognized" in line]
        self.assertEqual(len(stops), 3, text)
        self.assertIn("place 1: place 1", text)  # the same answer both times it was home...
        self.assertNotIn("<- one place, several answers", text)
        self.assertNotIn("<- several places, one answer", text)  # ... and a different one for the shop
        following = text.split("FOLLOWING YOU AT THE END")[1]
        self.assertIn("Apple device", following)  # the phone that came along
        self.assertNotIn("Car ", following)  # nobody on the road
        self.assertNotIn("60.1", text)


if __name__ == "__main__":
    unittest.main()
