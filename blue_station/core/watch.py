"""Watching for item trackers that travel with you. No Qt.

A tracker counts as following you once it has been with you for 10
minutes, in two different places. With your phone's position, one with
you for 15 minutes on the move, over a kilometre, is with you on the
move: less than following (on a bus, everyone is), but worth a look. With your phone's position (Develop's
Coordinates, position.py), that says where you are: moving, you're
between places; stopped for 2 minutes near a place you've been, you're
there; stopped for 5 somewhere new, that's a new place. Without it, a
laptop has no GPS, so places are recognized by their landmarks: named devices that stay put (TVs,
printers, speakers, a fridge), never the kinds people carry (phones,
watches, headphones, laptops): in traffic or a car park, other people's
phones stay near you for minutes and would make a new "place" every few
minutes, and a stranger passing then "follow" you from one to the next.
When the landmarks of where you were have gone quiet and most around
you are new, you've moved; new ones turning up while the old are still
heard are more of the same place. Somewhere never seen before is a place
once you've stayed, its landmarks heard for 5 minutes: walking past, a
house is heard for 3 at most. One already known is recognized after 3.
Named devices that come along (your phone, or anything else travelling
with you) describe no place, so they're learned as companions and stop
counting as landmarks. That's all it changes: they're still watched,
because only you can say a device is yours. You can also say "I've
moved" yourself. While your phone says where you are, places still learn
their landmarks, so they're known again when it goes quiet. Where a
place is, is kept for this session only: nothing about where you are is
saved.

A tracker is only credited to a place that's settled: its landmarks are
heard right now, or you marked it yourself. While you're between places,
nothing is credited, so a stranger's tag at the café isn't blamed on
your home before the café is recognized.

Item trackers are watched by default; other kinds of device can be
added (GROUPS). Most phones, earbuds and watches change their address
every 15 minutes or so. A change heard as it happens carries the record
over (links.py, hand_over); one out of earshot starts it over, so the
ones with a fixed address are the easiest to follow. Devices you say
are yours are left alone.

A device that sends several advertisements (links.py) is watched once,
as what it's listed as: a Mac's Nearby Info advert is a computer's, and
an Apple TV's is a TV's, which stays put and isn't watched at all."""
from dataclasses import dataclass, field

from blue_station.core.links import listed
from blue_station.core.position import Fix, distance

FOLLOW_PLACES = 2
FOLLOW_SECONDS = 10 * 60  # ... and with you this long: seconds either side of a move are nobody following
TRAVEL_SECONDS = 15 * 60  # with you this long on the move, by your phone's position...
TRAVEL_METRES = 1000  # ... over this far: with you on the move. On a 33 km drive and two walks, a stranger's device
# stayed with you 7 minutes at most (4.1 km, in traffic); your phone, all the way.
STAY_SECONDS = 20 * 60  # this long with you in one place: staying near you
GAP = 5 * 60  # a longer silence doesn't count as time with you
HEARD_WITHIN = 60  # a tracker is with you if heard in the last minute
LANDMARK_AGE = 3 * 60  # a named device heard this long can describe a place: heard all that time, never
# silent for LANDMARK_HEARD. First heard that long ago isn't enough: coming home, the street's devices were
# first heard on the way out, and driving past them made a place out of them.
LANDMARK_HEARD = 90
NEW_PLACE_AGE = 5 * 60  # a place you've never been is made of landmarks heard this long: you've stayed. Walking,
# the houses you pass are heard for 3 minutes at most, and made a place of every street walked twice.
SWITCH_NEW = 0.6  # when this share of the landmarks around you are new to the place...
SWITCH_STRIKES = 2  # ... on this many checks running, you've moved
SETTLED_NEW = 0.25  # at most this share new, and you're still there. In between: in transit.
# More landmarks turning up (ones just heard long enough to count) while this place's own are still
# heard is more of the same place, not a move: it once named a home by two, called the third one a
# new place, and had the whole flat following the user. A move needs this place's landmarks gone.
HERE_RECENT = 10 * 60  # the landmarks that described this place over this long, the last time you were here...
STILL_HERE = 0.5  # ... more than this share of them heard right now: still here, whatever else turns up
STILL_HEARD = 5 * 60  # ... one heard this lately may still be around (faint ones come and go)
GONE_SHARE = 0.25  # ... and a move needs all but this share of them unheard that long
LEARN_WITH = 3 * 60  # one turning up among them joins the place once heard with them this long: coming
# home, the café's landmarks linger for LANDMARK_HEARD, and mustn't make home part of the café
MATCH = 0.5  # a known place is recognized when it has this share of the landmarks around you
# With your phone's position, it says where you are:
STILL_METRES = 50  # within this of your latest position (or its own accuracy, if worse)...
STILL_WINDOW = 2 * 60  # ... this long, you've stopped (a traffic light is shorter); moving, you're between places
PLACE_METRES = 200  # a place whose centre is this near is where you are
FIX_FRESH = 2 * 60  # a position older than this: the phone has gone quiet, and the landmarks decide again
CAME_ALONG_METRES = 500  # a landmark of a place this far off, heard where you are, came along: walking past, no
# house was heard over more than 320 m of street
KEEP_FIXES = 15 * 60
CHECK_EVERY = 15
KEEP_TRACKERS = 48 * 3600  # AirTags away from their owner change address daily
KEEP_PASSERS_BY = 3 * 3600  # other kinds that only passed by: there are many, and most change address anyway
KEEP_PLACES = 60 * 86400  # places you've named are kept for good
MAX_VISITS = 100  # a device's timeline: this many stretches with you, newest last
MAX_LANDMARKS = 200

FOLLOWING, TRAVELLING, STAYING, PASSING = "following", "travelling", "staying", "passing"

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


def heard_for(d) -> float:
    """How long it has been heard without a silence of LANDMARK_HEARD, up to
    when it was last heard (looking back as far as NEW_PLACE_AGE needs)."""
    start = None
    for t, _ in reversed(d.history):
        if start is not None and start - t > LANDMARK_HEARD:
            break
        start = t
        if d.last_seen - t >= NEW_PLACE_AGE:
            break
    return d.last_seen - (d.first_seen if start is None else start)


def carried(info) -> bool:
    """A kind of device people carry about: never a landmark."""
    return info.tracker or group(info) in ("trackers", "headphones", "wearables", "phones") or info.icon == "laptop"


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
    name: str | None = None  # yours to give: 'Home', 'Office'
    centre: tuple[float, float] | None = None  # where it is, from your phone: this session only, never saved


@dataclass
class TrackerRecord:
    address: str
    kind: str
    first_seen: float
    last_seen: float
    seen_seconds: float = 0.0
    places: set[int] = field(default_factory=set)
    group: str = "trackers"
    # stretches it was heard with you: [place id, or None while no place is recognized, from, to]
    visits: list[list] = field(default_factory=list)
    # a landmark of one of your places: it stays put (that's what made it one), so it's never following you.
    # Heard from a place made nearby, a neighbour's TV once gained a second place and was flagged.
    landmark: bool = False
    # on the move with you, by your phone's position: this stretch (seconds, metres you moved, when last heard
    # with you; a longer silence starts one over), and the longest (seconds, metres)
    trip: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    travelled: list[float] = field(default_factory=lambda: [0.0, 0.0])

    def visit(self, place: int | None, t: float) -> None:
        last = self.visits[-1] if self.visits else None
        if last is not None and last[0] == place and 0 <= t - last[2] <= GAP:
            last[2] = t
        elif last is None or t > last[2]:
            self.visits.append([place, t, t])
            del self.visits[:-MAX_VISITS]

    @property
    def verdict(self) -> str:
        if len(self.places) >= FOLLOW_PLACES and self.seen_seconds >= FOLLOW_SECONDS and not self.landmark:
            return FOLLOWING
        if self.travelled[0] >= TRAVEL_SECONDS and self.travelled[1] >= TRAVEL_METRES and not self.landmark:
            return TRAVELLING
        return STAYING if self.seen_seconds >= STAY_SECONDS else PASSING


class Watcher:
    def __init__(self, data: dict | None = None, groups=DEFAULT_GROUPS):
        data = data or {}
        self.groups: set[str] = set(groups)
        self.places: dict[int, Place] = {}
        for p in data.get("places", []):
            self.places[p["id"]] = Place(p["id"], p["first_seen"], p["last_seen"], set(p.get("landmarks", [])),
                                         bool(p.get("manual")), p.get("name"))
        self.trackers: dict[str, TrackerRecord] = {}
        for t in data.get("trackers", []):
            self.trackers[t["address"]] = TrackerRecord(t["address"], t.get("kind", "Tracker"), t["first_seen"],
                                                        t["last_seen"], t.get("seen_seconds", 0.0),
                                                        set(t.get("places", [])), t.get("group", "trackers"),
                                                        [list(v) for v in t.get("visits", [])],
                                                        bool(t.get("landmark")),
                                                        list(t.get("trip", [0.0, 0.0, 0.0])),
                                                        list(t.get("travelled", [0.0, 0.0])))
        self.mine: set[str] = set(data.get("mine", []))  # yours: never watched
        self.companions: set[str] = set(data.get("companions", []))  # came along: no landmarks, still watched
        # place numbers are never reused, or a tracker's history would mix two places
        used = [0, *self.places, *(i for t in self.trackers.values() for i in t.places)]
        self.next_place = max(int(data.get("next_place", 1)), max(used) + 1)
        self.current: Place | None = self.places.get(data["current"]) if data.get("current") else None
        self.settled: Place | None = None
        self._strikes = 0
        self._checked = float("-inf")
        self._here: dict[str, float] = {}  # the current place's landmarks, and when they were last heard here
        self._with: dict[str, float] = {}  # new landmarks heard with the current place's, and since when
        self._fixes: list[Fix] = []  # your phone's latest positions
        self.by_position = False  # where you are comes from your phone's position (not the landmarks)
        self.moving = False  # ... and it says you're on the move
        self._at_now: tuple | None = None  # where your phone has you (_where), this check...
        self._at: tuple | None = None  # ... and (check, latitude, longitude) the last
        self._step: tuple[float, float] | None = None  # moving since the last check: (seconds, metres)

    def to_dict(self) -> dict:
        return {
            "places": [{"id": p.id, "first_seen": p.first_seen, "last_seen": p.last_seen,
                        "landmarks": sorted(p.landmarks), "manual": p.manual, "name": p.name}
                       for p in self.places.values()],
            "trackers": [{"address": t.address, "kind": t.kind, "first_seen": t.first_seen, "last_seen": t.last_seen,
                          "seen_seconds": round(t.seen_seconds, 1), "places": sorted(t.places), "group": t.group,
                          "landmark": t.landmark, "trip": [round(x, 1) for x in t.trip],
                          "travelled": [round(x, 1) for x in t.travelled],
                          "visits": t.visits}
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
        self._step = self._travel(now)
        newly = self._trackers(now, devices)
        self._prune(now)
        return newly

    def add_positions(self, fixes) -> None:
        """Your phone's positions (position.py), as they arrive."""
        if fixes:
            self._fixes = sorted([*self._fixes, *fixes], key=lambda f: f.t)
            self._fixes = [f for f in self._fixes if self._fixes[-1].t - f.t <= KEEP_FIXES]

    def _where(self, now: float) -> tuple[float, float, float] | None:
        """(latitude, longitude, seconds you've stayed there) from your phone,
        or None without a fresh position."""
        if not self._fixes or now - self._fixes[-1].t > FIX_FRESH:
            return None
        latest = since = self._fixes[-1]
        for f in reversed(self._fixes):
            if distance(f.lat, f.lon, latest.lat, latest.lon) > STILL_METRES + max(f.acc or 0, latest.acc or 0):
                break
            since = f
        return latest.lat, latest.lon, now - since.t

    def _travel(self, now: float) -> tuple[float, float] | None:
        """How long and how far you moved since the last check, by your
        phone; None when not moving (or after a sleep)."""
        where = self._at_now
        last, self._at = self._at, ((now, where[0], where[1]) if where is not None else None)
        if where is None or last is None or not self.moving or now - last[0] > 4 * CHECK_EVERY:
            return None
        return now - last[0], distance(last[1], last[2], where[0], where[1])

    def _heard(self, now: float, devices) -> dict[str, float]:
        """The devices that can be landmarks, and how long each has been heard."""
        return {d.address: heard_for(d) for d in devices
                if d.name and not carried(d.info) and d.address not in self.companions
                and d.age(now) <= LANDMARK_HEARD}

    def _place(self, now: float, devices) -> Place | None:
        devices = list(devices)
        spans = self._heard(now, devices)
        marks = {a for a, s in spans.items() if s >= LANDMARK_AGE}
        lasting = {a for a, s in spans.items() if s >= NEW_PLACE_AGE}  # enough for a place never seen before
        where = self._where(now)
        self.by_position, self.moving = where is not None, where is not None and where[2] < STILL_WINDOW
        self._at_now = where
        if where is not None:
            return self._by_position(now, where, marks, lasting)
        current = self.current
        if current is None:
            if len(marks) >= 2 and self._arrive(now, marks, lasting):
                self._here, self._with = {a: now for a in marks & self.current.landmarks}, {}
                return self.current
            return None
        if len(marks) < 2:
            self._strikes = 0  # "on checks running": one between places ends a run
            if current.manual:
                current.last_seen = now
                return current
            return None
        if not current.landmarks:  # a place you just marked learns its landmarks...
            known = self._match(marks, besides=current)
            if known is None:
                current.landmarks = set(marks)
            else:  # ... or turns out to be one already known
                current = self._merge_into(known)
        new = len(marks - current.landmarks) / len(marks)
        self._here = {a: t for a, t in self._here.items() if current.last_seen - t <= HERE_RECENT}
        recent = set(self._here) or (marks & current.landmarks)  # or, just started here: the ones heard now
        heard = {d.address: d.last_seen for d in devices}
        still_now = len(recent & marks) / len(recent) if recent else 0.0
        still_lately = (sum(1 for a in recent if now - heard.get(a, float("-inf")) <= STILL_HEARD) / len(recent)
                        if recent else 0.0)
        if new <= SETTLED_NEW or still_now > STILL_HERE:  # the same place, or more of it found
            return self._still_at(now, current, marks)
        if new >= SWITCH_NEW and still_lately <= GONE_SHARE:
            self._strikes += 1
            came_along = marks & current.landmarks
            if self._strikes >= SWITCH_STRIKES and self._arrive(now, marks - came_along, lasting - came_along):
                self._strikes = 0
                self.companions |= came_along
                for place in self.places.values():
                    place.landmarks -= came_along
                self._here, self._with = {a: now for a in marks & self.current.landmarks}, {}
                return self.current
            return None  # moving on, or somewhere new not stayed at long enough yet
        # in between: arriving somewhere while the last place's landmarks linger, or they flicker.
        # Learning from this would blend two places into one.
        self._strikes = 0
        return None

    def _still_at(self, now: float, place: Place, marks: set[str]) -> Place:
        """Still at the place. Landmarks turning up join it: at once while few
        of those around are new, else once heard with it for LEARN_WITH."""
        self._strikes = 0
        newcomers = marks - place.landmarks
        self._with = {a: self._with.get(a, now) for a in newcomers}  # a gap starts one over
        if marks and len(newcomers) / len(marks) > SETTLED_NEW:  # more of it: once they've kept company long enough
            marks = marks - {a for a, t in self._with.items() if now - t < LEARN_WITH}
        if len(place.landmarks) < MAX_LANDMARKS:
            place.landmarks |= marks
        self._here.update((a, now) for a in marks)
        place.last_seen = now
        return place

    def _merge_into(self, known: Place) -> Place:
        """The place you marked (I've moved) turns out to be one already known."""
        current = self.current
        for record in self.trackers.values():
            if current.id in record.places:
                record.places.discard(current.id)
                record.places.add(known.id)
            for v in record.visits:
                if v[0] == current.id:
                    v[0] = known.id
        known.name = known.name or current.name
        del self.places[current.id]
        self.current = known
        return known

    def _by_position(self, now: float, where, marks: set[str], lasting: set[str]) -> Place | None:
        """Where you are, from your phone. Moving, you're between places.
        Stopped, you're at the place whose centre is near; or at one its
        landmarks tell, which learns where it is; or, after NEW_PLACE_AGE,
        somewhere new. Places keep learning their landmarks meanwhile, for
        when the phone goes quiet."""
        lat, lon, still = where
        if still < STILL_WINDOW:
            self._strikes = 0
            return None
        near = min(((distance(lat, lon, *p.centre), p) for p in self.places.values() if p.centre is not None),
                   key=lambda x: x[0], default=(None, None))
        place = near[1] if near[0] is not None and near[0] <= PLACE_METRES else None
        current = self.current
        if place is None:
            known = self._match(marks, besides=current) if len(marks) >= 2 else None
            if known is not None and known.centre is None:  # known by its landmarks, not yet by where it is
                place = known
            elif current is not None and current.centre is None and (current.manual or self.settled is current):
                place = current  # where you said you are, or where the landmarks had you as the phone began
            elif still >= NEW_PLACE_AGE:
                place = self._new(now, lasting)
            else:
                return None  # stopped somewhere new: not for long enough yet
        if (current is not None and current is not place and current.manual and not current.landmarks
                and current.centre is None):
            place = self._merge_into(place)  # "I've moved", but it's a place already known
        if place.centre is None:
            place.centre = (lat, lon)
        if place is not self.current:
            marks = marks - self._came_to(now, place, marks)
        return self._still_at(now, place, marks)

    def _came_to(self, now: float, place: Place, marks: set[str]) -> set[str]:
        """Arriving at a place, by your phone. Landmarks of the last one heard
        here came along, if that one is too far to be heard from here. Returns them."""
        last, came_along = self.current, set()
        if (last is not None and last.centre is not None
                and distance(*last.centre, *place.centre) > CAME_ALONG_METRES):
            came_along = marks & last.landmarks
            self.companions |= came_along
            for p in self.places.values():
                p.landmarks -= came_along
        self.current, self._strikes = place, 0
        self._here, self._with = {a: now for a in (marks - came_along) & place.landmarks}, {}
        return came_along

    def _match(self, marks: set[str], besides: Place | None = None) -> Place | None:
        """The known place with the most of these landmarks, if it has enough of them."""
        best = max((p for p in self.places.values() if p is not besides), key=lambda p: len(marks & p.landmarks),
                   default=None)
        if best is not None and marks and len(marks & best.landmarks) / len(marks) >= MATCH:
            return best
        return None

    def _arrive(self, now: float, marks: set[str], lasting: set[str]) -> bool:
        """A known place, recognized by the landmarks around you, or a new one
        made of those heard long enough. False while neither: on the move."""
        best = self._match(marks)
        if best is not None:
            best.landmarks |= marks
            best.last_seen = now
            self.current = best
        elif len(lasting) >= 2:
            self.current = self._new(now, lasting)
        else:
            return False
        return True

    def _new(self, now: float, marks: set[str], manual: bool = False) -> Place:
        place = Place(self.next_place, now, now, set(marks), manual)
        self.next_place += 1
        self.places[place.id] = place
        return place

    def _trackers(self, now: float, devices) -> list[TrackerRecord]:
        newly = []
        stay_put = set().union(*(p.landmarks for p in self.places.values()))
        for d in devices:
            shown = listed(d)
            if shown is not d:  # one device, one record: under the advertisement it's listed as
                self._carry(d.address, shown.address)
                continue
            kind_group = group(d.info)
            record = self.trackers.get(d.address)
            if record is not None:
                record.group = kind_group  # what it turns out to be decides whether it's watched
                record.landmark = d.address in stay_put
            if kind_group not in self.groups or self.is_mine(d) or d.age(now) > HEARD_WITHIN:
                continue
            heard = d.last_seen
            if record is None:
                record = self.trackers[d.address] = TrackerRecord(d.address, d.info.kind or "Device", heard, heard,
                                                                  group=kind_group, landmark=d.address in stay_put)
            elif 0 < heard - record.last_seen <= GAP:
                record.seen_seconds += heard - record.last_seen
            record.last_seen = max(record.last_seen, heard)
            record.kind = d.info.kind or record.kind
            before = record.verdict
            if heard - record.trip[2] > GAP:  # a new stretch with you
                record.trip = [0.0, 0.0, heard]
            if self._step is not None:  # on the move, and it's with you
                record.trip[0] += self._step[0]
                record.trip[1] += self._step[1]
                record.travelled = max(record.travelled, record.trip[:2])
            record.trip[2] = heard
            if self.settled is not None:
                record.places.add(self.settled.id)
            record.visit(self.settled.id if self.settled is not None else None, heard)
            if record.verdict == FOLLOWING and before != FOLLOWING:
                newly.append(record)
        return newly

    def _carry(self, other: str, listed_as: str) -> None:
        """A record kept under another of the device's advertisements joins
        the one it's listed as. They were heard side by side, so its time
        with you is the longer of the two, not both."""
        record = self.trackers.pop(other, None)
        if record is None:
            return
        other_record = self.trackers.get(listed_as)
        if other_record is not None:  # the newer one's stretches go after the older one's
            older, newer = sorted((record, other_record), key=lambda r: r.visits[0][1] if r.visits else r.first_seen)
            for place, start, end in newer.visits:
                older.visit(place, start)
                older.visit(place, end)
            older.first_seen = min(older.first_seen, newer.first_seen)
            older.last_seen = max(older.last_seen, newer.last_seen)
            older.seen_seconds = max(older.seen_seconds, newer.seen_seconds)
            older.places |= newer.places
            older.travelled = max(older.travelled, newer.travelled)
            older.trip = max(older.trip, newer.trip, key=lambda trip: trip[2])
            record = older
        record.address = listed_as
        self.trackers[listed_as] = record

    def _prune(self, now: float) -> None:
        self.trackers = {a: t for a, t in self.trackers.items()
                         if now - t.last_seen <= (KEEP_TRACKERS if t.group == "trackers" or t.verdict != PASSING
                                                  else KEEP_PASSERS_BY)}
        self.places = {i: p for i, p in self.places.items()
                       if now - p.last_seen <= KEEP_PLACES or p is self.current or p.name}

    # ---- asked for -----------------------------------------------------------------------

    def moved(self, now: float) -> Place:
        """You say you're somewhere new. The place learns its landmarks as
        they turn up."""
        self._strikes = 0
        self._here, self._with = {}, {}
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
        self._here, self._with = {}, {}

    def forget_following(self) -> int:
        """Forgets the ones that may be following you, and nothing else. One
        still around starts over: 10 more minutes with you, in two places,
        before it's flagged again."""
        gone = [t.address for t in self.following()]
        for address in gone:
            del self.trackers[address]
        return len(gone)

    def forget_travelling(self) -> int:
        """Forgets the ones that have been with you on the move, and nothing else."""
        gone = [t.address for t in self.travelling()]
        for address in gone:
            del self.trackers[address]
        return len(gone)

    def forget_device(self, device) -> bool:
        """Forgets what it has seen of one device, under every advertisement it sends (links.py)."""
        found = False
        for d in (device, *(p for p, _ in device.partners)):
            found = self.trackers.pop(d.address, None) is not None or found
        return found

    def hand_over(self, old: str, new: str) -> None:
        """A device changed its address (see links.py): its record, and
        whether it's yours, carry on under the new one."""
        record = self.trackers.pop(old, None)
        if record is not None:
            later = self.trackers.get(new)
            record.address = new
            if later is not None:  # heard under the new address before the change was spotted
                record.last_seen = max(record.last_seen, later.last_seen)
                record.seen_seconds += later.seen_seconds
                record.places |= later.places
                record.travelled = max(record.travelled, later.travelled)
                for place, start, end in later.visits:
                    record.visit(place, start)
                    record.visit(place, end)
            self.trackers[new] = record
        if old in self.mine:
            self.mine.add(new)
        if old in self.companions:
            self.companions.add(new)
        for place in self.places.values():
            if old in place.landmarks:
                place.landmarks.add(new)

    def take_back(self, old: str, later: list[str], since: float) -> None:
        """A link found wrong (links.py, DeviceStore.take_back): the old
        address never changed to the later ones, the first of which began at
        since. The record they shared is split there, and the later ones
        are no longer yours, companions or landmarks for the old one's sake."""
        record = self.trackers.pop(later[-1], None) if later else None
        if record is not None:
            before = [[p, a, min(b, since)] for p, a, b in record.visits if a < since]
            after = [[p, max(a, since), b] for p, a, b in record.visits if b >= since]
            with_it = sum(b - a for _, a, b in after)
            if after:
                self.trackers[later[-1]] = TrackerRecord(
                    later[-1], record.kind, after[0][1], record.last_seen, with_it,
                    {p for p, _, _ in after if p is not None}, record.group, after)
            record.visits, record.seen_seconds = before, max(0.0, record.seen_seconds - with_it)
            record.places = ({p for p, _, _ in before if p is not None}
                             | (record.places - {p for p, _, _ in after}))  # and older ones, out of its timeline
            record.last_seen = before[-1][2] if before else record.first_seen
            record.address = old
            again = self.trackers.get(old)  # heard since it came back
            if again is not None:
                record.last_seen = max(record.last_seen, again.last_seen)
                record.seen_seconds += again.seen_seconds
                record.places |= again.places
                for place, start, end in again.visits:
                    record.visit(place, start)
                    record.visit(place, end)
            self.trackers[old] = record
        taken = set(later)
        if old in self.mine:
            self.mine -= taken
        if old in self.companions:
            self.companions -= taken
        for place in self.places.values():
            if old in place.landmarks:
                place.landmarks -= taken

    def name_place(self, place: Place, name: str) -> None:
        place.name = name.strip() or None

    def place_name(self, place_id: int | None) -> str:
        if place_id is None:
            return "unknown place"
        place = self.places.get(place_id)
        return place.name if place is not None and place.name else f"place {place_id}"

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

    def record_for(self, device) -> TrackerRecord | None:
        """The watch's record of a device, under any of the advertisements it
        sends (links.py): the one that says most, following you first."""
        found = [r for d in (device, *(p for p, _ in device.partners)) if (r := self.record(d.address)) is not None]
        order = (FOLLOWING, TRAVELLING, STAYING, PASSING)
        return min(found, key=lambda r: (order.index(r.verdict), -r.seen_seconds),
                   default=None)

    def is_mine(self, device) -> bool:
        return any(d.address in self.mine for d in (device, *(p for p, _ in device.partners)))

    def following(self) -> list[TrackerRecord]:
        return [t for t in self.watched() if t.verdict == FOLLOWING]

    def travelling(self) -> list[TrackerRecord]:
        return [t for t in self.watched() if t.verdict == TRAVELLING]
