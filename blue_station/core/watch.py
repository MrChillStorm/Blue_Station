"""Watching for item trackers that travel with you. No Qt.

A tracker counts as following you once it has been with you in two
different places. A laptop has no GPS, so places are recognized by their
landmarks: named devices that stay put (TVs, printers, speakers, a
fridge). When most of the landmarks around you are new, you've moved.
Named devices that come along (your phone, or anything else travelling
with you) describe no place, so they're learned as companions and stop
counting as landmarks. That's all it changes: they're still watched,
because only you can say a device is yours. You can also say "I've
moved" yourself.

A tracker is only credited to a place that's settled: its landmarks are
heard right now, or you marked it yourself. While you're between places,
nothing is credited, so a stranger's tag at the café isn't blamed on
your home before the café is recognized.

Item trackers are watched by default; other kinds of device can be
added (GROUPS). Most phones, earbuds and watches change their address
every 15 minutes or so, so only the ones with a fixed address can be
followed from place to place. Devices you say are yours are left
alone."""
from dataclasses import dataclass, field

FOLLOW_PLACES = 2
STAY_SECONDS = 20 * 60  # this long with you in one place: staying near you
GAP = 5 * 60  # a longer silence doesn't count as time with you
HEARD_WITHIN = 60  # a tracker is with you if heard in the last minute
LANDMARK_AGE = 3 * 60  # a named device around this long can describe a place
LANDMARK_HEARD = 90
SWITCH_NEW = 0.6  # when this share of the landmarks around you are new to the place...
SWITCH_STRIKES = 2  # ... on this many checks running, you've moved
SETTLED_NEW = 0.25  # at most this share new, and you're still there. In between: in transit.
MATCH = 0.5  # a known place is recognized when it has this share of the landmarks around you
CHECK_EVERY = 15
KEEP_TRACKERS = 48 * 3600  # AirTags away from their owner change address daily
KEEP_PASSERS_BY = 3 * 3600  # other kinds that only passed by: there are many, and most change address anyway
KEEP_PLACES = 60 * 86400
MAX_LANDMARKS = 200

FOLLOWING, STAYING, PASSING = "following", "staying", "passing"

# what the watch can look for: only things that travel
GROUPS = {
    "trackers": "Item trackers",
    "headphones": "Headphones and earbuds",
    "wearables": "Watches, bands and health devices",
    "phones": "Phones and tablets",
    "other": "Other devices",
}
DEFAULT_GROUPS = ("trackers",)
_ICON_GROUPS = {"headphones": "headphones", "watch": "wearables", "heart": "wearables", "phone": "phones"}
_STAY_PUT = {"tv", "speaker", "beacon"}


def group(info) -> str | None:
    """Which of GROUPS a device belongs to, from what its advertisement
    says. None for things that stay put (TVs, speakers, beacons): they
    describe places, and are never watched."""
    if info.tracker:
        return "trackers"
    if info.beacon or info.icon in _STAY_PUT:
        return None
    return _ICON_GROUPS.get(info.icon, "other")


@dataclass
class Place:
    id: int
    first_seen: float
    last_seen: float
    landmarks: set[str] = field(default_factory=set)
    manual: bool = False  # marked by you: settled until its landmarks say otherwise


@dataclass
class TrackerRecord:
    address: str
    kind: str
    first_seen: float
    last_seen: float
    seen_seconds: float = 0.0
    places: set[int] = field(default_factory=set)
    group: str = "trackers"

    @property
    def verdict(self) -> str:
        if len(self.places) >= FOLLOW_PLACES:
            return FOLLOWING
        return STAYING if self.seen_seconds >= STAY_SECONDS else PASSING


class Watcher:
    def __init__(self, data: dict | None = None, groups=DEFAULT_GROUPS):
        data = data or {}
        self.groups: set[str] = set(groups)
        self.places: dict[int, Place] = {}
        for p in data.get("places", []):
            self.places[p["id"]] = Place(p["id"], p["first_seen"], p["last_seen"], set(p.get("landmarks", [])),
                                         bool(p.get("manual")))
        self.trackers: dict[str, TrackerRecord] = {}
        for t in data.get("trackers", []):
            self.trackers[t["address"]] = TrackerRecord(t["address"], t.get("kind", "Tracker"), t["first_seen"],
                                                        t["last_seen"], t.get("seen_seconds", 0.0),
                                                        set(t.get("places", [])), t.get("group", "trackers"))
        self.mine: set[str] = set(data.get("mine", []))  # yours: never watched
        self.companions: set[str] = set(data.get("companions", []))  # came along: no landmarks, still watched
        # place numbers are never reused, or a tracker's history would mix two places
        used = [0, *self.places, *(i for t in self.trackers.values() for i in t.places)]
        self.next_place = max(int(data.get("next_place", 1)), max(used) + 1)
        self.current: Place | None = self.places.get(data["current"]) if data.get("current") else None
        self.settled: Place | None = None
        self._strikes = 0
        self._checked = float("-inf")

    def to_dict(self) -> dict:
        return {
            "places": [{"id": p.id, "first_seen": p.first_seen, "last_seen": p.last_seen,
                        "landmarks": sorted(p.landmarks), "manual": p.manual} for p in self.places.values()],
            "trackers": [{"address": t.address, "kind": t.kind, "first_seen": t.first_seen, "last_seen": t.last_seen,
                          "seen_seconds": round(t.seen_seconds, 1), "places": sorted(t.places), "group": t.group}
                         for t in self.trackers.values()],
            "mine": sorted(self.mine),
            "companions": sorted(self.companions),
            "next_place": self.next_place,
        }

    # ---- every few seconds -------------------------------------------------------------

    def update(self, now: float, devices, force: bool = False) -> list[TrackerRecord]:
        """Returns the trackers that have just started following you."""
        if not force and now - self._checked < CHECK_EVERY:
            return []
        self._checked = now
        self.settled = self._place(now, devices)
        newly = self._trackers(now, devices)
        self._prune(now)
        return newly

    def landmarks(self, now: float, devices) -> set[str]:
        return {d.address for d in devices
                if d.name and not d.info.tracker and d.address not in self.companions
                and now - d.first_seen >= LANDMARK_AGE and d.age(now) <= LANDMARK_HEARD}

    def _place(self, now: float, devices) -> Place | None:
        marks = self.landmarks(now, devices)
        current = self.current
        if current is None:
            if len(marks) >= 2:
                self._arrive(now, marks)
                return self.current
            return None
        if len(marks) < 2:
            if current.manual:
                current.last_seen = now
                return current
            return None
        if not current.landmarks:  # a place you just marked learns its landmarks...
            known = self._match(marks, besides=current)
            if known is None:
                current.landmarks = set(marks)
            else:  # ... or turns out to be one already known
                for record in self.trackers.values():
                    if current.id in record.places:
                        record.places.discard(current.id)
                        record.places.add(known.id)
                del self.places[current.id]
                current = self.current = known
        new = len(marks - current.landmarks) / len(marks)
        if new >= SWITCH_NEW:
            self._strikes += 1
            if self._strikes >= SWITCH_STRIKES:
                self._strikes = 0
                came_along = marks & current.landmarks
                self.companions |= came_along
                for place in self.places.values():
                    place.landmarks -= came_along
                self._arrive(now, marks - came_along)
                return self.current
            return None
        if new > SETTLED_NEW:
            # half old, half new: arriving somewhere while the last place's landmarks
            # still linger. Learning from this would blend the two places into one.
            return None
        self._strikes = 0
        if len(current.landmarks) < MAX_LANDMARKS:
            current.landmarks |= marks
        current.last_seen = now
        return current

    def _match(self, marks: set[str], besides: Place | None = None) -> Place | None:
        """The known place with the most of these landmarks, if it has enough of them."""
        best = max((p for p in self.places.values() if p is not besides), key=lambda p: len(marks & p.landmarks),
                   default=None)
        if best is not None and marks and len(marks & best.landmarks) / len(marks) >= MATCH:
            return best
        return None

    def _arrive(self, now: float, marks: set[str]) -> None:
        best = self._match(marks)
        if best is not None:
            best.landmarks |= marks
            best.last_seen = now
            self.current = best
        else:
            self.current = self._new(now, marks)

    def _new(self, now: float, marks: set[str], manual: bool = False) -> Place:
        place = Place(self.next_place, now, now, set(marks), manual)
        self.next_place += 1
        self.places[place.id] = place
        return place

    def _trackers(self, now: float, devices) -> list[TrackerRecord]:
        newly = []
        for d in devices:
            kind_group = group(d.info)
            if kind_group not in self.groups or d.address in self.mine or d.age(now) > HEARD_WITHIN:
                continue
            heard = d.last_seen
            record = self.trackers.get(d.address)
            if record is None:
                record = self.trackers[d.address] = TrackerRecord(d.address, d.info.kind or "Device", heard, heard,
                                                                  group=kind_group)
            elif 0 < heard - record.last_seen <= GAP:
                record.seen_seconds += heard - record.last_seen
            record.last_seen = max(record.last_seen, heard)
            record.kind = d.info.kind or record.kind
            record.group = kind_group
            before = record.verdict
            if self.settled is not None:
                record.places.add(self.settled.id)
            if record.verdict == FOLLOWING and before != FOLLOWING:
                newly.append(record)
        return newly

    def _prune(self, now: float) -> None:
        self.trackers = {a: t for a, t in self.trackers.items()
                         if now - t.last_seen <= (KEEP_TRACKERS if t.group == "trackers" or t.verdict != PASSING
                                                  else KEEP_PASSERS_BY)}
        self.places = {i: p for i, p in self.places.items()
                       if now - p.last_seen <= KEEP_PLACES or p is self.current}

    # ---- asked for -----------------------------------------------------------------------

    def moved(self, now: float) -> Place:
        """You say you're somewhere new. The place learns its landmarks as
        they turn up."""
        self._strikes = 0
        self.current = self.settled = self._new(now, set(), manual=True)
        return self.current

    def forget(self) -> None:
        """Forgets what it has seen. Which devices are yours stays."""
        self.places.clear()
        self.trackers.clear()
        self.companions.clear()
        self.current = self.settled = None
        self.next_place = 1
        self._strikes = 0

    def set_groups(self, groups) -> None:
        """What to watch for. Records of a group turned off are kept, just
        not shown, until they'd be forgotten anyway."""
        self.groups = set(groups)

    def set_mine(self, address: str, mine: bool = True) -> None:
        if mine:
            self.mine.add(address)
            self.trackers.pop(address, None)
        else:
            self.mine.discard(address)

    def watches(self, info) -> bool:
        return group(info) in self.groups

    def watched(self) -> list[TrackerRecord]:
        return [t for t in self.trackers.values() if t.group in self.groups]

    def record(self, address: str) -> TrackerRecord | None:
        t = self.trackers.get(address)
        return t if t is not None and t.group in self.groups else None

    def following(self) -> list[TrackerRecord]:
        return [t for t in self.watched() if t.verdict == FOLLOWING]
