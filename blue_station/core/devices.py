"""Everything seen of each device: its advertisement, a signal history,
and what follows from them (smoothed signal, statistics, distance, trend).
No Qt."""
import math
from collections import deque
from dataclasses import dataclass, field

from blue_station.core import names
from blue_station.core.decode import Info, decode

FADE_AFTER = 5.0  # seconds without a packet before a device starts to fade
GONE_AFTER = 30.0  # ... and before it counts as out of range
HISTORY_SECONDS = 15 * 60
HISTORY_MAX = 20_000
SMOOTHING = 1.5  # seconds; the time constant of the smoothed signal
PATH_LOSS = 2.5  # how fast signal falls with distance indoors (2 is open air)
TYPICAL_1M = -59  # dBm at 1 m, when neither a beacon nor a calibration says better
RSSI_MIN, RSSI_MAX = -100, -30  # the ends of every signal bar


@dataclass(frozen=True)
class Sighting:
    """One advertisement as the scanner heard it."""
    address: str
    rssi: int
    t: float
    name: str | None = None
    tx_power: int | None = None
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_uuids: tuple[str, ...] = ()
    service_data: dict[str, bytes] = field(default_factory=dict)
    connectable: bool | None = None
    mac: str | None = None


@dataclass
class Stats:
    low: int
    high: int
    mean: float
    spread: float  # standard deviation: how much the signal jitters
    count: int


def quality(rssi: float | None) -> str:
    if rssi is None:
        return "—"
    return "Excellent" if rssi >= -55 else "Good" if rssi >= -67 else "Fair" if rssi >= -80 else "Weak"


def fraction(rssi: float | None) -> float:
    """Where a signal sits on the bar, 0 to 1."""
    if rssi is None:
        return 0.0
    return min(1.0, max(0.0, (rssi - RSSI_MIN) / (RSSI_MAX - RSSI_MIN)))


def distance_text(metres: float | None) -> str:
    if metres is None:
        return "—"
    if metres < 1:
        return f"≈ {metres:.1f} m"
    return f"≈ {metres:.1f} m" if metres < 10 else f"≈ {metres:.0f} m"


def ago_text(seconds: float) -> str:
    if seconds < 2:
        return "now"
    if seconds < 60:
        return f"{seconds:.0f} s ago"
    if seconds < 3600:
        return f"{seconds // 60:.0f} min ago"
    return f"{seconds // 3600:.0f} h ago"


def duration_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


_EDDYSTONE = "0000feaa-0000-1000-8000-00805f9b34fb"


def payload_key(key) -> str:
    """One name space for maker data (by company) and service data (by UUID)."""
    return f"maker:{key:04X}" if isinstance(key, int) else f"service:{key}"


def span_text(seconds: float) -> str:
    """'52 min', '3 h 05 min': for stretches where seconds don't matter."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


class Device:
    def __init__(self, address: str, t: float):
        self.address = address
        self.mac: str | None = None
        self.name: str | None = None
        self.tx_power: int | None = None
        self.connectable: bool | None = None
        self.manufacturer_data: dict[int, bytes] = {}
        self.service_uuids: list[str] = []
        self.service_data: dict[str, bytes] = {}
        self.eddystone: dict[int, bytes] = {}  # the latest frame of each type
        # the value each payload had before its last change, for showing what changed
        self.previous: dict[str, bytes] = {}
        self.payload_changes = 0
        self.first_seen = self.last_seen = t
        self.rssi: int | None = None  # the last packet's
        self.smoothed: float | None = None
        self.packets = 0
        self.history: deque[tuple[float, int]] = deque()
        self.info: Info = decode(None, {}, [], {})
        self._payload = None
        # what the user told us, remembered between sessions
        self.nickname: str | None = None
        self.pinned = False
        self.calibration: int | None = None  # the signal measured at 1 m
        self.alert_gone = False  # a notification when it goes out of range...
        self.alert_back = False  # ... and when it comes back

    # ---- taking in packets -------------------------------------------------------

    def update(self, s: Sighting) -> None:
        if s.name:
            self.name = s.name
        if s.tx_power is not None:
            self.tx_power = s.tx_power
        if s.connectable is not None:
            self.connectable = s.connectable
        if s.mac:
            self.mac = s.mac
        for key, old, new in [(payload_key(cid), self.manufacturer_data.get(cid), data)
                              for cid, data in s.manufacturer_data.items()] + \
                             [(payload_key(uuid), self.service_data.get(uuid), data)
                              for uuid, data in s.service_data.items()]:
            if old is not None and old != new and not self._eddystone_turn(key, old, new):
                self.previous[key] = old
                self.payload_changes += 1
        self.manufacturer_data.update(s.manufacturer_data)
        self.service_uuids += [u for u in s.service_uuids if u not in self.service_uuids]
        self.service_data.update(s.service_data)
        for uuid, data in s.service_data.items():
            if names.short_uuid(uuid) == 0xFEAA and data:
                self.eddystone[data[0]] = data
        payload = (self.name, tuple(self.manufacturer_data.items()), tuple(self.service_uuids),
                   tuple(self.service_data.items()))
        if payload != self._payload:
            self._payload = payload
            self.info = decode(self.name, self.manufacturer_data, self.service_uuids, self.service_data,
                               self.eddystone or None)

        # CoreBluetooth says 127 when it has no reading for a packet
        if -127 < s.rssi < 20:
            if self.smoothed is None:
                self.smoothed = float(s.rssi)
            else:
                weight = 1 - math.exp(-max(s.t - self.last_seen, 0.0) / SMOOTHING)
                self.smoothed += weight * (s.rssi - self.smoothed)
            self.rssi = s.rssi
            self.history.append((s.t, s.rssi))
            while self.history and (self.history[0][0] < s.t - HISTORY_SECONDS or len(self.history) > HISTORY_MAX):
                self.history.popleft()
        self.last_seen = max(self.last_seen, s.t)
        self.packets += 1

    def _eddystone_turn(self, key: str, old: bytes, new: bytes) -> bool:
        """An Eddystone beacon switching between frame types isn't a change of
        payload."""
        return key == payload_key(_EDDYSTONE) and old[:1] != new[:1]

    # ---- what follows ------------------------------------------------------------

    @property
    def title(self) -> str:
        return self.nickname or self.name or self.info.kind or "Unknown device"

    def age(self, now: float) -> float:
        return max(0.0, now - self.last_seen)

    def gone(self, now: float) -> bool:
        return self.age(now) > GONE_AFTER

    def fade(self, now: float) -> float:
        """1 while fresh, easing to 0.35 as the device goes quiet."""
        age = self.age(now)
        if age <= FADE_AFTER:
            return 1.0
        return max(0.35, 1 - 0.65 * (age - FADE_AFTER) / (GONE_AFTER - FADE_AFTER))

    def recent(self, now: float, seconds: float) -> list[tuple[float, int]]:
        out = []
        for point in reversed(self.history):
            if point[0] < now - seconds:
                break
            out.append(point)
        out.reverse()
        return out

    def stats(self, now: float, seconds: float = 60) -> Stats | None:
        values = [r for _, r in self.recent(now, seconds)]
        if not values:
            return None
        mean = sum(values) / len(values)
        spread = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
        return Stats(min(values), max(values), mean, spread, len(values))

    def rate(self, now: float, seconds: float = 10) -> float:
        """Packets heard per second lately."""
        span = min(seconds, max(now - self.first_seen, 1.0))
        return len(self.recent(now, span)) / span

    @property
    def reference(self) -> tuple[int, str]:
        """The signal expected at 1 m, and where that number comes from."""
        if self.calibration is not None:
            return self.calibration, "your calibration"
        if self.info.ref_1m is not None:
            return self.info.ref_1m, "the beacon's own figure"
        return TYPICAL_1M, "a typical device"

    def distance(self) -> float | None:
        """A rough distance in metres from the log-distance path-loss model:
        walls, bodies and antennas easily make it off by half or double."""
        if self.smoothed is None:
            return None
        return 10 ** ((self.reference[0] - self.smoothed) / (10 * PATH_LOSS))

    def gaps(self, now: float, seconds: float = 60) -> list[float]:
        """Milliseconds between packets heard lately. The Mac hears only some
        of a device's packets, so these are multiples of its real interval:
        the shortest common gap is the best guess at it."""
        times = [t for t, _ in self.recent(now, seconds)]
        return [(b - a) * 1000 for a, b in zip(times, times[1:]) if b > a]

    def trend(self, now: float, seconds: float = 8, longest: float = 30) -> float | None:
        """How fast the signal is changing, in dB per second (a least-squares
        slope, so single noisy packets barely move it). Over the last
        `seconds`, or further back for a device the Mac hears only now and
        then: a Mac passes on a device's packets only a few times a second
        at best, a TV's about once a second, a tag's every several."""
        points = self.recent(now, seconds)
        while len(points) < 5 and seconds < longest:
            seconds = min(longest, seconds * 2)
            points = self.recent(now, seconds)
        if len(points) < 5 or points[-1][0] - points[0][0] < seconds / 4:
            return None
        n = len(points)
        mt = sum(t for t, _ in points) / n
        mr = sum(r for _, r in points) / n
        var = sum((t - mt) ** 2 for t, _ in points)
        return sum((t - mt) * (r - mr) for t, r in points) / var if var else None


@dataclass
class GapStats:
    shortest: float
    typical: float  # the 10th percentile: the shortest gap that happens often
    median: float
    longest: float
    count: int


def gap_stats(gaps: list[float]) -> GapStats | None:
    if not gaps:
        return None
    ordered = sorted(gaps)

    def at(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return GapStats(ordered[0], at(0.1), at(0.5), ordered[-1], len(ordered))


class DeviceStore:
    """All devices seen this session, plus what's remembered about some of
    them between sessions (nickname, pinned, calibration, alerts)."""

    def __init__(self, known: dict | None = None):
        self.devices: dict[str, Device] = {}
        self.known: dict[str, dict] = dict(known or {})

    def ingest(self, sightings: list[Sighting]) -> int:
        """Returns how many devices were new."""
        new = 0
        for s in sightings:
            device = self.devices.get(s.address)
            if device is None:
                device = self.devices[s.address] = Device(s.address, s.t)
                prefs = self.known.get(s.address, {})
                device.nickname = prefs.get("nickname")
                device.pinned = bool(prefs.get("pinned"))
                device.calibration = prefs.get("calibration")
                device.alert_gone = bool(prefs.get("alert_gone"))
                device.alert_back = bool(prefs.get("alert_back"))
                new += 1
            device.update(s)
        return new

    def remember(self, device: Device) -> None:
        prefs = {"nickname": device.nickname, "pinned": device.pinned or None, "calibration": device.calibration,
                 "alert_gone": device.alert_gone or None, "alert_back": device.alert_back or None}
        prefs = {k: v for k, v in prefs.items() if v is not None}
        if prefs:
            self.known[device.address] = prefs
        else:
            self.known.pop(device.address, None)

    def clear(self) -> None:
        """Forgets this session's devices, except pinned ones and ones with
        alerts (or they'd seem to arrive when heard again)."""
        self.devices = {a: d for a, d in self.devices.items() if d.pinned or d.alert_gone or d.alert_back}
