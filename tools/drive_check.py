"""Checks the tracker watch against a drive: a Blue Station packet log (Develop,
Record, All devices) and a GPS track of the same trip, a GPX file from any
logging app on your phone. A log recorded with Coordinates is its own track.

    python3 tools/drive_check.py "Packets 2026-09-28 1400.csv" track.gpx
    python3 tools/drive_check.py "Packets 2026-09-28 1400.csv"

The track only tells when you moved, when you stopped, and which stops were
the same place. That's all it's boiled down to, and nothing this prints or
saves holds a coordinate: --save-key writes just that, so the GPX can go.
The log is replayed through the app's own code, starting from what the
watch already knows (your places, like Home), without changing any of it."""
import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blue_station.core import prefs  # noqa: E402
from blue_station.core.devices import DeviceStore  # noqa: E402
from blue_station.core.links import Linker  # noqa: E402
from blue_station.core.packets import read as read_packets  # noqa: E402
from blue_station.core.position import SETUP_METRES, SETUP_SECONDS, Fix, read_gpx as read_fixes  # noqa: E402
from blue_station.core.watch import (  # noqa: E402
    CHECK_EVERY, FOLLOWING, GROUPS, LANDMARK_AGE, NEW_PLACE_AGE, TRAVELLING, Watcher)

STAY_METRES = 100  # a stop: this close to where it began...
STAY_SECONDS = 120  # ... for this long (traffic lights and queues are driving)
PLACE_METRES = 200  # two stops this close are the same place
STILL = 0.5  # m/s: slower than this is standing still (a long pause between two track points too)


@dataclass
class Stay:
    start: float
    end: float
    lat: float
    lon: float
    place: int = 0


def read_gpx(path: Path) -> list[tuple[float, float, float]]:
    """(unix time, latitude, longitude) of each track point, in time order."""
    return [(f.t, f.lat, f.lon) for f in read_fixes(path)]


def points_from_log(path: Path) -> list[tuple[float, float, float]]:
    """A log recorded with Coordinates (or with a track added) is its own
    track: where you were, a point a second."""
    points = []
    with Path(path).open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("latitude"):
                points.append((float(r["unix_time"]), float(r["latitude"]), float(r["longitude"])))
    points.sort()
    out = []
    for p in points:
        if not out or p[0] - out[-1][0] >= 1:
            out.append(p)
    return out


def metres(a, b) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    dx = math.radians(b[2] - a[2]) * math.cos(lat) * 6371000
    dy = math.radians(b[1] - a[1]) * 6371000
    return math.hypot(dx, dy)


def pace(run) -> float:
    """Metres a second along the track, taking a point every half a minute:
    walking adds up, a standing phone's jitter doesn't."""
    kept = [run[0]]
    for p in run[1:]:
        if p[0] - kept[-1][0] >= 30:
            kept.append(p)
    if kept[-1] is not run[-1]:
        kept.append(run[-1])
    return sum(metres(a, b) for a, b in zip(kept, kept[1:])) / max(run[-1][0] - run[0][0], 1)


def stays(points) -> list[Stay]:
    """Where the track stood still for a while: near where it began, and
    slow, as walking up and down a street is no stop. A logging app may
    save nothing while you stand still: a long, slow pause counts as
    standing."""
    out, i = [], 0
    while i < len(points):
        j = i + 1
        while j < len(points) and metres(points[i], points[j]) <= STAY_METRES:
            j += 1
        end = points[j - 1][0]
        if j < len(points) and points[j][0] - end > STAY_SECONDS:
            if metres(points[j - 1], points[j]) / (points[j][0] - end) < STILL:
                end = points[j][0]
        if end - points[i][0] >= STAY_SECONDS and pace(points[i:j]) < STILL:
            run = points[i:j]
            out.append(Stay(points[i][0], end, statistics.fmean(p[1] for p in run), statistics.fmean(p[2] for p in run)))
            i = j
        else:
            i += 1
    places: list[Stay] = []  # the first stay at each place
    for s in out:
        same = next((p for p in places if metres((0, s.lat, s.lon), (0, p.lat, p.lon)) <= PLACE_METRES), None)
        s.place = same.place if same else len(places) + 1
        if same is None:
            places.append(s)
    return out


def like_owntracks(points) -> list[Fix]:
    """The track as OwnTracks would send it, set up by Blue Station's link:
    a position every SETUP_METRES or SETUP_SECONDS, whichever comes first."""
    out = []
    for t, lat, lon in points:
        if not out or t - out[-1][0] >= SETUP_SECONDS or metres(out[-1], (t, lat, lon)) >= SETUP_METRES:
            out.append((t, lat, lon))
    return [Fix(t, lat, lon) for t, lat, lon in out]


def key_from_points(points, source) -> dict:
    if not points:
        raise SystemExit(f"No positions in {source}.")
    return {"track": [points[0][0], points[-1][0]],
            "stays": [[s.start, s.end, s.place] for s in stays(points)]}


def key_from_gpx(path: Path) -> dict:
    return key_from_points(read_gpx(path), path)


def load_key(path: Path) -> dict:
    return json.loads(Path(path).read_text()) if str(path).endswith(".json") else key_from_gpx(path)


# ---- replaying the log --------------------------------------------------------------------

def replay(packets, known: dict | None, groups, learned: dict | None, fixes=None):
    """The app's own store, linker and watch, fed the log at its own pace,
    and your phone's positions (Fix, position.py) as they'd have arrived.
    Returns the watch's view at each of its checks: (time, settled place)."""
    packets = sorted(packets, key=lambda s: s.t)
    fixes = sorted(fixes or [], key=lambda f: f.t)
    store, linker, watcher = DeviceStore(), Linker(learned), Watcher(known, groups)
    timeline, i, j, now, checked = [], 0, 0, packets[0].t, float("-inf")
    while i < len(packets):
        now += 0.5
        batch = []
        while i < len(packets) and packets[i].t <= now:
            batch.append(packets[i])
            i += 1
        arrived = []
        while j < len(fixes) and fixes[j].t <= now:
            arrived.append(fixes[j])
            j += 1
        watcher.add_positions(arrived)
        store.ingest(batch)
        for old in linker.taken_back(store.devices):
            later = store.take_back(old)
            if later:
                watcher.take_back(old.address, [d.address for d in later], later[0].heard_from)
        for old, new, sure in linker.update(now, list(store.devices.values())):
            store.hand_over(old, new, sure)
            watcher.hand_over(old.address, new.address)
        if now - checked >= CHECK_EVERY:
            checked = now
            watcher.update(now, list(store.devices.values()), force=True)
            timeline.append((now, watcher.settled.id if watcher.settled is not None else None))
    return timeline, watcher, store


# ---- comparing -----------------------------------------------------------------------------

def clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min"


def segments(timeline) -> list[tuple[float, float, int | None]]:
    """The watch's view as stretches: (from, to, settled place or None)."""
    out = []
    for t, place in timeline:
        if out and out[-1][2] == place:
            out[-1][1] = t
        else:
            out.append([t, t, place])
    return [tuple(s) for s in out]


def report(key: dict, timeline, watcher: Watcher, store: DeviceStore, phone: bool = False) -> list[str]:
    start, end = key["track"]
    stays_ = [(a, b, p) for a, b, p in key["stays"]]
    name = lambda pid: watcher.place_name(pid) if pid is not None else "between places"
    lines = ["THE TRIP, from the GPS track"]
    last = start
    began = stays_[0][2] if stays_ and stays_[0][0] - start <= 60 else None
    for a, b, p in stays_:
        if a - last > 60:
            lines.append(f"  {clock(last)}-{clock(a)}  moving, {minutes(a - last)}")
        lines.append(f"  {clock(a)}-{clock(b)}  stopped at place {p}" + (" (where it began)" if p == began else ""))
        last = b
    if end - last > 60:
        lines.append(f"  {clock(last)}-{clock(end)}  moving, {minutes(end - last)}")

    lines += ["", "WHAT THE WATCH MADE OF IT",
              "  (with your phone's position: moving is between places, stopped 2 min is a place it knows,"
              f" {minutes(NEW_PLACE_AGE)} one it doesn't)" if phone else
              f"  (like the app just started, it needs named devices heard for {minutes(LANDMARK_AGE)} to recognize a"
              f" place, {minutes(NEW_PLACE_AGE)} to make a new one: each stop is told that much after arriving)"]
    for a, b, pid in segments(timeline):
        lines.append(f"  {clock(a)}-{clock(b)}  {name(pid)}")

    lines += ["", "STOP BY STOP"]
    seen: dict[int, Counter] = {}
    for a, b, p in stays_:
        during = [pid for t, pid in timeline if a <= t <= b]
        settled = [pid for pid in during if pid is not None]
        if not during:
            lines.append(f"  place {p} {clock(a)}-{clock(b)}: not in the recording")
            continue
        if not settled:
            lines.append(f"  place {p} {clock(a)}-{clock(b)}: never recognized ({minutes(b - a)} there)")
            continue
        pid = Counter(settled).most_common(1)[0][0]
        seen.setdefault(p, Counter())[pid] += 1
        first = next(t for t, x in timeline if a <= t <= b and x == pid)
        left = next((t for t, x in timeline if t > b and x != pid), None)  # when it stopped saying so
        text = f"  place {p} {clock(a)}-{clock(b)}: {name(pid)}, recognized {minutes(first - a)} after arriving"
        if left is not None and left - b >= 60:
            text += f", kept {minutes(left - b)} after leaving"
        lines.append(text + f"; settled {len(settled) / len(during):.0%} of the stop")

    moving = [(t, pid) for t, pid in timeline
              if not any(a <= t <= b for a, b, _ in stays_) and start <= t <= end]
    wrong = [t for t, pid in moving if pid is not None]
    if moving:
        lines += ["", f"WHILE MOVING: settled at a place for {len(wrong) / len(moving):.0%} of it"
                      f" (counting the minutes right after leaving a stop)"]

    lines += ["", "SAME PLACE, SAME ANSWER"]
    by_watch: dict[int, set] = {}
    for p, counts in sorted(seen.items()):
        pids = sorted(counts)
        for pid in pids:
            by_watch.setdefault(pid, set()).add(p)
        lines.append(f"  place {p}: " + ", ".join(name(pid) for pid in pids)
                     + ("" if len(pids) == 1 else "  <- one place, several answers"))
    for pid, ps in by_watch.items():
        if len(ps) > 1:
            lines.append(f"  {name(pid)} was places {sorted(ps)}  <- several places, one answer")

    lines += ["", "FOLLOWING YOU AT THE END"]
    following = [r for r in watcher.watched() if r.verdict == FOLLOWING]
    for r in sorted(following, key=lambda r: -r.seen_seconds):
        d = store.devices.get(r.address)
        lines.append(f"  {(d.title if d else r.kind)[:30]:30} with you {minutes(r.seen_seconds)}, at "
                     + ", ".join(name(pid) for pid in sorted(r.places)))
    if not following:
        lines.append("  nothing")
    travelling = [r for r in watcher.watched() if r.verdict == TRAVELLING]
    if phone:
        lines += ["", "WITH YOU ON THE MOVE AT THE END"]
        for r in sorted(travelling, key=lambda r: -r.travelled[0]):
            d = store.devices.get(r.address)
            lines.append(f"  {(d.title if d else r.kind)[:30]:30} {minutes(r.travelled[0])} over"
                         f" {r.travelled[1] / 1000:.1f} km")
        if not travelling:
            lines.append("  nothing")
    return lines


def before(saved: dict, start: float) -> dict:
    """What the watch knew as the trip began: your places and your devices,
    but not what it has seen, nor places it learned on this very trip (the
    app has saved them since)."""
    known = {k: v for k, v in saved.items() if k != "trackers"}
    known["places"] = [p for p in saved.get("places", []) if p["first_seen"] < start]
    return known


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path, help="a packet log exported from Develop")
    parser.add_argument("track", type=Path, nargs="?",
                        help="the GPS track (.gpx), or a key saved with --save-key (.json); leave it out when the "
                             "log was recorded with Coordinates")
    parser.add_argument("--save-key", type=Path, help="write the track boiled down (no coordinates) to this .json")
    parser.add_argument("--fresh", action="store_true", help="start the watch knowing no places")
    parser.add_argument("--watch", choices=("saved", "all"), default="saved",
                        help="kinds of device to watch: as in your settings, or all of them")
    parser.add_argument("--phone", action="store_true",
                        help="the watch has your phone's position too (the track's), as with Coordinates on")
    args = parser.parse_args(argv)
    points = (read_gpx(args.track) if args.track and not str(args.track).endswith(".json")
              else points_from_log(args.log) if not args.track else [])
    key = load_key(args.track) if args.track else key_from_points(points, args.log)
    if args.phone and not points:
        raise SystemExit("--phone needs the positions themselves: a GPX track, or a log with coordinates.")
    if args.save_key:
        args.save_key.write_text(json.dumps(key))
    settings = prefs.load()
    groups = list(GROUPS) if args.watch == "all" else settings.get("watch_for", ["trackers"])
    packets = read_packets(args.log)
    known = None if args.fresh else before(prefs.load(prefs.TRACKERS), min(s.t for s in packets))
    timeline, watcher, store = replay(packets, known, groups, settings.get("learned_bytes"),
                                      like_owntracks(points) if args.phone else None)
    print("\n".join(report(key, timeline, watcher, store, phone=args.phone)))


if __name__ == "__main__":
    main()
