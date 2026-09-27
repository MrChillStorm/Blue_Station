"""Following a device through its address changes. No Qt.

Phones, earbuds, watches and other Apple devices change their Bluetooth
address every 15 minutes or so. Signal strength alone can't tell one
device from another: it says how far away a device is, not which one it
is. What gives a change away is the moment itself: the old address goes
quiet, and within a few seconds a new one starts from the same spot
(about the same signal), with the same kind of advertisement. A
recording of an hour at home showed exactly that, every time.

Each candidate is scored by how likely its timing and signal would be
for the same device, against a stranger. A stranger's signal is likelier
where many devices are heard: the crowd is learned from every device
around, so a match among the faint ones at −100 dBm counts for little,
and one at −69 dBm, where few devices are, counts for a lot. Rival
candidates share the confidence, so two identical devices changing at
the same moment don't get linked on a guess.

A link can still be wrong, and one kind of wrong shows: a device never
goes back to an address it has left, so an old address heard again
never changed. Its link is taken back (taken_back), with what it taught.
Recordings on the move had one link in ten found so: a Mac's named
advertisement, whose address macOS keeps, taken to have changed to its
twin's new one; a TV left behind, taken for a stranger's phone.

Three things are learned as it goes:

- Which bytes of each kind of advertisement stay the same across a
  change. An AirPlay target, for one, keeps its network address in its
  advertisement. Enough such bytes make a fingerprint, which recognizes
  the device after a change that wasn't heard: out of range, or while
  the Mac slept. Kinds that change every byte (Apple's Nearby Info, Find
  My) get none, and still rely on timing.
- Which devices change together: two kinds of advertisement that change
  at the same moments, from the same spot, are one device sending both
  (an Apple TV is AirPlay and Nearby Info). Such partners are listed as
  one, and a leader recognized across a gap brings its partner along.
- Which addresses send the same thing at the same time. A Mac sends its
  Nearby Info from an address that changes, and word for word from a
  fixed one too. Apple's messages carry a code that changes with the
  address, so two addresses sending the same one at once are one device.
  They're listed as one, and the fixed address anchors the other through
  each change, heard or not: its new address is the one that sends what
  the anchor sends.
- Which addresses go quiet and come back together. A device switched off
  and on again (its Bluetooth, or itself) stops all it sends within a
  few seconds, and starts it all again together, while everything else
  carries on. A Mac's own Find My advertisement changes address on its
  own schedule and sends nothing like its others: this is how it joins
  them. Three recordings at home, 12 hours of them overnight, showed 14
  such shared cycles, every one of them a single device's.

A device may not send the same message set every time (an AirPlay target
now and then slips in a short extra message), so each kind of
advertisement a device sends is remembered, and two addresses are
compared on the kinds they have in common."""
import ast
import bisect
import functools
import itertools
import math
import statistics

from blue_station.core.decode import apple_messages
from blue_station.core.devices import GONE_AFTER

WINDOW = 60.0  # seconds from the old address's last packet to the new one's first, at most
PAUSE = 20.0  # a change usually leaves a gap of up to this, and a fifth of them longer (up to WINDOW)
# A device heard only every few seconds leaves longer gaps more often: one heard every 10 s (a Find
# My device near its owner) left 1 to 70 s at its changes, half of them over 20. For such a one the
# longest gap is this many of its own gaps between packets, and the longer ones get this share per
# second between packets...
WINDOW_GAPS, LATE_PER_GAP = 8, 0.05
SLOWEST = 10.0  # ... up to one heard this rarely
UNTIMED = 40.0  # heard less often than this, a device can't be timed: its silence looks like a change, and
# a change could be anywhere between its packets. A steady Find My device's last packets before its
# changes came every 2 to 32 s; a faint one heard every 64 s came back after being linked away.
SLACK = 2.0  # the new one may start this much before the old one's last packet
SURE = 0.6  # link only this sure
EDGE = 8  # packets at the end of the old address and the start of the new one that are compared
CHECK_EVERY = 2.0
FRESH = 300.0  # a new address looks for its old one this long

LEARN_SURE = 0.8  # only links this sure teach it anything
LEARN_AFTER = 2  # changes of a kind before its steady bytes are fully trusted
MIN_ID_BYTES = 3  # steady bytes (besides Apple's message headers) it takes to tell devices apart
FINGERPRINT = 200.0  # how much likelier a matching fingerprint makes the same device
MISMATCH = 0.02  # ... and a differing one: not impossible, a network address can change too
# after one change a random byte may have stayed the same by chance (1 in 256), so the first
# fingerprint counts for less, hardly counts against, and needs a byte more to bridge a gap
FIRST_FINGERPRINT, FIRST_MISMATCH, FIRST_MIN_BYTES = 30.0, 0.5, 4
TOGETHER = 10.0  # seconds: changes this close together...
TOGETHER_DB = 6.0  # ... from signals this close...
PARTNERS_AFTER = 2  # ... this many times make two advertisements one device
PARTNER_SURE = 0.9
TWIN_BYTES = 6  # an Apple payload this long, heard from exactly two addresses...
TWIN_WINDOW = 30.0  # ... within this many seconds of each other...
TWINS_AFTER = 2  # ... this many different times makes them one device's (a beacon's payload never changes)
TWIN_SURE = 0.98
# going quiet: silent this long, or this many of its own gaps between packets if longer...
QUIET, QUIET_GAPS = 30.0, 6
# ... together: within this, plus this many of the rarer one's gaps (its last packet came that much early)
TOGETHER_OFF, TOGETHER_OFF_GAPS = 3.0, 2.5
OFF_AT_LEAST = 20.0  # both silent for this long together
STEADY = -85  # only devices heard this strongly: faint ones come and go all the time
CYCLE_RECENT = 120.0  # a return this recent is still looked at
CHANGES_WITH_IT, SAME_DATA, OFF_AND_ON = "changes", "same", "cycles"  # how partners are known to be one device


def _layout(md: dict) -> tuple:
    layout = []
    for cid, data in sorted(md.items()):
        if cid == 0x004C:
            layout.append((cid, tuple((kind, len(body)) for kind, body in apple_messages(data))))
        else:
            layout.append((cid, len(data)))
    return tuple(layout)


def signature(device, md: dict | None = None) -> tuple:
    """What a device advertises, apart from values that change anyway:
    its makers and their payload layouts, Apple's message types, services
    (listed, or sending data: Google's Fast Pair and another's make two
    kinds) and flags. The same device keeps these across an address change. (Its
    name is compared apart: macOS knows one address's name after a
    connection, and a new address starts without it.) md: one of the
    advertisements it has sent, by default its latest."""
    md = device.manufacturer_data if md is None else md
    services = set(device.service_uuids) | set(device.service_data)
    return (_layout(md), tuple(sorted(services)), device.connectable, device.tx_power)


def kind(device, md: dict | None = None) -> str:
    return repr(signature(device, md))


@functools.lru_cache(maxsize=4096)
def _parse(k: str) -> tuple:
    return ast.literal_eval(k)


def _loose(k: str) -> tuple:
    """A kind without its TX power: what may match another (see same_kind)."""
    return _parse(k)[:3]


def same_kind(a: str, b: str) -> str | None:
    """The kind two advertisements share, if any: equal ones, or equal but
    for a TX power only one of them has reported yet (a Mac leaves it out
    of the packets that carry its Handoff message, so a new address may go
    minutes without it). The one that knows it names the pair."""
    if a == b:
        return a
    pa, pb = _parse(a), _parse(b)
    if pa[:3] != pb[:3] or (pa[3] is not None and pb[3] is not None):
        return None
    return a if pa[3] is not None else b


def payload(md: dict) -> bytes:
    return b"".join(data for _, data in sorted(md.items()))


def _headers(md: dict) -> set[int]:
    """Where Apple's message types and lengths sit in the payload: the same
    for every device of a kind, so they tell nothing apart."""
    out, offset = set(), 0
    for cid, data in sorted(md.items()):
        if cid == 0x004C:
            i = 0
            while i + 2 <= len(data):
                out |= {offset + i, offset + i + 1}
                i += 2 + data[i + 1]
        offset += len(data)
    return out


def lead(*devices):
    """Of one device's advertisements, the one to list: a name, or the more telling kind."""
    return max(devices, key=lambda d: (d.name is not None, d.info._rank, d.address))


def listed(device):
    """The advertisement a device is listed under, the same on every page:
    of the ones it sends together, the lead among those heard lately (a
    Mac's fixed one may go quiet while the others carry on)."""
    if not device.partners:
        return device
    sent = [device, *(p for p, _ in device.partners)]
    latest = max(d.last_seen for d in sent)
    return lead(*(d for d in sent if latest - d.last_seen <= GONE_AFTER))


def _edge(device, head: bool) -> float | None:
    points = list(itertools.islice(device.history if head else reversed(device.history), EDGE))
    return statistics.median(r for _, r in points) if points else None


def _gap(device) -> float:
    """The usual time between its packets, from the latest ones. macOS often
    reports one advertisement twice within a few milliseconds: that's one."""
    times = [t for t, _ in itertools.islice(reversed(device.history), 12)][::-1]
    gaps = [b - a for a, b in zip(times, times[1:]) if b - a > 0.2]
    return statistics.median(gaps) if gaps else 2.0


def _timing(old) -> tuple[float, float]:
    """The longest gap its change may leave, and the share of changes
    longer than PAUSE: both more for a device heard only now and then."""
    gap = min(_gap(old), SLOWEST)
    return max(WINDOW, WINDOW_GAPS * gap), max(0.2, LATE_PER_GAP * gap)


class Linker:
    def __init__(self, learned: dict | None = None):
        self.links: dict[str, tuple[str, float]] = {}  # new address -> (old address, how sure)
        self.steady: dict[str, set[int]] = {}  # kind -> byte positions the same at every change seen
        self.changes: dict[str, int] = {}  # kind -> changes seen, sure ones
        for k, v in (learned or {}).items():
            self.steady[k], self.changes[k] = set(v.get("steady", [])), int(v.get("changes", 0))
        self._before = {k: set(v) for k, v in self.steady.items()}  # learned in earlier sessions
        self._taught: dict[str, list[tuple[str, set[int]]]] = {}  # new address -> (kind, bytes the same) its link taught
        self._left: dict[str, float] = {}  # old address linked away -> its last packet then
        self._wrong: set[tuple[str, str]] = set()  # (old, new) links it has taken back: never made again
        self.pairs: set[frozenset] = set()  # partner chains, by the first address of each
        self._variants: dict[str, dict[str, dict]] = {}  # address -> kind -> its latest advertisement of that kind
        self._together: dict[frozenset, int] = {}
        self._changed: list[tuple] = []  # recent sure changes: (new first heard, old last heard, signal, root, kinds)
        self._expect: list[tuple] = []  # partners due to change: (partner root, when, signal, left, until)
        self.twins: set[frozenset] = set()  # one device's two addresses sending the same, by their roots
        self._payloads: dict[tuple, dict[str, float]] = {}  # an Apple payload heard lately -> address -> when
        self._alike: dict[frozenset, set] = {}  # two roots -> different payloads they've sent alike
        self.cycled: set[frozenset] = set()  # one device's advertisements that went off and on together, by roots
        self._heard: list[tuple[float, int]] = []  # (check, devices heard since the last one): was the Mac listening?
        self._checked = float("-inf")
        self._cache: dict | None = None  # during a check: address -> its kinds of advertisement
        self._learned_version = 0  # goes up with everything learned about bytes: fingerprints to work out again
        self._prints: dict[str, tuple] = {}  # address -> (learned version, last heard, what bridges a gap for it)

    def to_dict(self) -> dict:
        return {k: {"steady": sorted(self.steady[k]), "changes": self.changes[k]} for k in self.steady}

    # ---- what each device sends -------------------------------------------------------

    def adverts(self, device) -> dict[str, dict]:
        """Each kind of advertisement it has sent: kind -> the latest of that kind."""
        if self._cache is not None and device.address in self._cache:
            return self._cache[device.address]
        out = dict(self._variants.get(device.address, {}))
        out[kind(device)] = dict(device.manufacturer_data)
        if self._cache is not None:
            self._cache[device.address] = out
        return out

    def _shared(self, old, new) -> list[tuple[str, str, str]]:
        """The kinds of advertisement both have sent: (the kind, old's, new's)."""
        theirs, out = self.adverts(old), []
        for kn in self.adverts(new):
            ko = kn if kn in theirs else next((k for k in theirs if same_kind(k, kn)), None)
            if ko is not None:
                out.append((same_kind(ko, kn), ko, kn))
        return out

    def _memo(self, key, work):
        """work(), remembered for the rest of the check: a crowd asks the same many times over."""
        if self._cache is None:
            return work()
        if key not in self._cache:
            self._cache[key] = work()
        return self._cache[key]

    def _learned(self, k: str) -> str:
        """The kind its learning is kept under: itself, or the same kind with its TX power."""
        if k in self.changes:
            return k
        return next((c for c in self.changes if same_kind(c, k) == c), k)

    # ---- fingerprints ---------------------------------------------------------------

    def learn(self, old, new, sure: float) -> None:
        if sure < LEARN_SURE:
            return
        before, after = self.adverts(old), self.adverts(new)
        for k, ko, kn in self._shared(old, new):
            a, b = payload(before[ko]), payload(after[kn])
            if not a or len(a) != len(b):
                continue
            same = {i for i in range(len(a)) if a[i] == b[i]}
            self.steady[k] = same if k not in self.steady else self.steady[k] & same  # only ever fewer
            self.changes[k] = self.changes.get(k, 0) + 1
            self._taught.setdefault(new.address, []).append((k, same))
            self._learned_version += 1

    def _unteach(self, new: str) -> None:
        """Forgets what a link taught: its kinds' steady bytes are worked
        out again from the other links (and earlier sessions)."""
        taught = self._taught.pop(new, [])
        for k in {k for k, _ in taught}:
            self.changes[k] -= sum(1 for kk, _ in taught if kk == k)
            evidence = [same for ts in self._taught.values() for kk, same in ts if kk == k]
            if k in self._before:
                evidence.append(self._before[k])
            if evidence:
                self.steady[k] = set.intersection(*evidence)
            else:
                self.steady.pop(k, None)
                self.changes.pop(k, None)
        if taught:
            self._learned_version += 1

    def taken_back(self, devices: dict) -> list:
        """Old addresses heard again since they were linked away: those links
        were wrong, as a device that changes its address never goes back to
        the old one. Each is undone here, with what it taught, and never
        made again. devices: address -> device. Returns the old devices, for
        DeviceStore.take_back and whatever else carried them over."""
        out = []
        for address, left in list(self._left.items()):
            old = devices.get(address)
            if old is None or old.last_seen <= left:
                continue
            del self._left[address]
            new = old.superseded_by
            if self.links.get(new, ("",))[0] == address:
                del self.links[new]
            self._wrong.add((address, new))
            self._unteach(new)
            out.append(old)
        return out

    def _fingerprint(self, k: str, md: dict) -> bytes | None:
        k = self._learned(k)
        if self.changes.get(k, 0) < 1:
            return None
        positions = sorted(self.steady[k] - _headers(md))
        data = payload(md)
        if len(positions) < MIN_ID_BYTES or not data or positions[-1] >= len(data):
            return None
        return bytes(data[i] for i in positions)

    def fingerprint(self, device) -> bytes | None:
        """The bytes its kind keeps through its changes, if there are enough
        of them to tell devices apart: from its kind's first sure change on,
        tentatively until a second one confirms it."""
        found = [(self.changes[self._learned(k)] >= LEARN_AFTER, fp) for k, md in self.adverts(device).items()
                 if (fp := self._fingerprint(k, md)) is not None]
        return max(found)[1] if found else None

    def bridging(self, device) -> list[tuple[str, bytes]]:
        """(kind, bytes) of each fingerprint it can be recognized by across a
        change nobody heard: confirmed ones, and tentative ones long enough.
        Kept between checks: a device long gone doesn't change."""
        kept = self._prints.get(device.address)
        if kept is not None and kept[:2] == (self._learned_version, device.last_seen):
            return kept[2]
        out = []
        for k, md in self.adverts(device).items():
            fp = self._fingerprint(k, md)
            if fp is not None and (self.changes[self._learned(k)] >= LEARN_AFTER or len(fp) >= FIRST_MIN_BYTES):
                out.append((self._learned(k), fp))
        self._prints[device.address] = (self._learned_version, device.last_seen, out)
        return out

    def confirmed(self, device) -> bool:
        """Its fingerprint has held through more than one change."""
        return any(self.changes.get(self._learned(k), 0) >= LEARN_AFTER and self._fingerprint(k, md) is not None
                   for k, md in self.adverts(device).items())

    # ---- scoring ----------------------------------------------------------------------

    def _crowd(self, medians: list[float], rssi: float) -> float:
        """How likely a stranger is to be heard this strong, per dB. medians: sorted."""
        near = bisect.bisect_right(medians, rssi + 5) - bisect.bisect_left(medians, rssi - 5)
        return max(1, near) / (max(1, len(medians)) * 10)

    def ratio(self, old, new, medians: list[float], fingerprints: bool = True) -> float:
        """How much likelier the same device is than a stranger. 0: can't be.
        fingerprints=False: from timing and signal alone, which is what may
        teach it: a fingerprint mustn't vouch for itself."""
        shared = self._shared(old, new)
        if not shared or (old.name and new.name and old.name != new.name):
            return 0.0
        dt = new.first_seen - old.last_seen
        if dt < -SLACK:
            return 0.0
        evidence = None  # (matched, confirmed, bytes): a match beats a mismatch, confirmed beats tentative
        if fingerprints:
            before, after = self.adverts(old), self.adverts(new)
            for k, ko, kn in shared:
                a = self._memo(("print", old.address, k, ko), lambda: self._fingerprint(k, before[ko]))
                b = self._memo(("print", new.address, k, kn), lambda: self._fingerprint(k, after[kn]))
                if a is not None and b is not None:
                    found = (a == b, self.changes[self._learned(k)] >= LEARN_AFTER, len(b))
                    evidence = found if evidence is None else max(evidence, found)
        window, late = _timing(old)
        if dt > window or _gap(old) > UNTIMED:  # a change nobody heard, or one that can't be timed:
            # only a fingerprint can tell
            if evidence is None or not evidence[0] or (not evidence[1] and evidence[2] < FIRST_MIN_BYTES):
                return 0.0
            return FINGERPRINT if evidence[1] else FIRST_FINGERPRINT
        tail = self._memo(("tail", old.address), lambda: _edge(old, head=False))
        head = self._memo(("head", new.address), lambda: _edge(new, head=True))
        if tail is None or head is None:
            return 0.0
        same_time = 1 / PAUSE if dt <= PAUSE else late / (window - PAUSE)
        stranger_time = 1 / (window + SLACK)
        heard = min(len(old.history), len(new.history))
        sigma = 3 + 8 / math.sqrt(max(1, heard))  # a few packets say little about the signal
        same_signal = math.exp(-((tail - head) / sigma) ** 2 / 2) / (sigma * math.sqrt(2 * math.pi))
        r = (same_time / stranger_time) * (same_signal / self._crowd(medians, head))
        if evidence is not None:
            match, mismatch = (FINGERPRINT, MISMATCH) if evidence[1] else (FIRST_FINGERPRINT, FIRST_MISMATCH)
            r *= match if evidence[0] else mismatch
        return r

    # ---- every few seconds --------------------------------------------------------------

    def _sample(self, now: float, devices) -> None:
        """Remembers each kind of advertisement the devices heard lately sent."""
        self._heard = [h for h in self._heard if now - h[0] <= 30 * 60]
        self._heard.append((now, sum(1 for d in devices if now - d.last_seen <= CHECK_EVERY + 1)))
        for d in devices:
            if now - d.last_seen <= CHECK_EVERY + 1 and d.manufacturer_data:
                self._variants.setdefault(d.address, {})[kind(d)] = dict(d.manufacturer_data)
                if 0x004C in d.manufacturer_data and len(payload(d.manufacturer_data)) >= TWIN_BYTES:
                    self._payloads.setdefault(tuple(sorted(d.manufacturer_data.items())), {})[d.address] = d.last_seen

    # ---- off and on together ---------------------------------------------------------

    @staticmethod
    def _silence(d, now: float) -> tuple[float, float] | None:
        """(last packet before, first after) its latest silence long enough to
        be it going quiet, if it came back lately. Its history runs on across
        address changes, so coming back under a new address counts too."""
        quiet = max(QUIET, QUIET_GAPS * min(_gap(d), SLOWEST))
        later = None
        for t, _ in reversed(d.history):
            if later is not None and later - t >= quiet:
                return t, later
            if now - t > CYCLE_RECENT:
                return None
            later = t
        return None

    def _listening(self, start: float, end: float) -> bool:
        """The Mac kept hearing other devices meanwhile: it wasn't the one asleep."""
        checks = [n for t, n in self._heard if start < t < end]
        return bool(checks) and statistics.median(checks) >= 3

    def _off_and_on(self, now: float, devices) -> None:
        back = [(d, *s) for d in devices
                if d.superseded_by is None and 0x004C in d.manufacturer_data and d.smoothed is not None
                and d.smoothed >= STEADY and len(d.history) >= 10 and (s := self._silence(d, now)) is not None]
        for i, (a, off_a, on_a) in enumerate(back):
            for b, off_b, on_b in back[i + 1:]:
                if a.root == b.root or frozenset((a.root, b.root)) in self.cycled:
                    continue
                slack = TOGETHER_OFF + TOGETHER_OFF_GAPS * max(min(_gap(a), SLOWEST), min(_gap(b), SLOWEST))
                off, on = max(off_a, off_b), min(on_a, on_b)
                if (abs(off_a - off_b) <= slack and abs(on_a - on_b) <= slack and on - off >= OFF_AT_LEAST
                        and self._listening(off, on)):
                    self.cycled.add(frozenset((a.root, b.root)))

    # ---- twins ------------------------------------------------------------------------

    def _pairs_alike(self, now: float, by_address: dict) -> list[tuple[tuple, object, object]]:
        """(payload, device, device): two addresses sending the same Apple payload lately.
        Heard from more than two, it's one many devices send alike."""
        out = []
        for key, heard in list(self._payloads.items()):
            for address in [a for a, t in heard.items() if now - t > TWIN_WINDOW]:
                del heard[address]
            if not heard:
                del self._payloads[key]
            elif len(heard) == 2:
                a, b = (by_address.get(x) for x in heard)
                if a is not None and b is not None:
                    out.append((key, a, b))
        return out

    def _find_twins(self, alike) -> None:
        for key, a, b in alike:
            if a.root != b.root:
                pair = frozenset((a.root, b.root))
                seen = self._alike.setdefault(pair, set())
                if len(seen) < TWINS_AFTER:
                    seen.add(key)
                if len(seen) >= TWINS_AFTER:
                    self.twins.add(pair)

    def _anchored(self, now: float, devices, alike, taken_old: set, taken_new: set) -> list:
        """A new address sending what the anchor of a known twin sends is
        the twin's new address, whenever its last change was. The anchor
        vouches for both, whatever mix of messages each sent: a Mac coming
        back on may send Handoff where it sent only Nearby Info before."""
        current = {d.root: d for d in devices if d.superseded_by is None}
        out = []
        for _, x, y in alike:
            for new, anchor in ((x, y), (y, x)):
                if (new.address in self.links or new.earlier or new.superseded_by is not None
                        or new.address in taken_new or now - new.first_seen > FRESH):
                    continue
                for pair in self.twins:
                    if anchor.root not in pair or new.root in pair:
                        continue
                    old = current.get(next(r for r in pair if r != anchor.root))
                    if (old is None or old is new or old.address in taken_old or old.address in taken_new
                            or old.last_seen > new.first_seen + SLACK or (old.address, new.address) in self._wrong):
                        continue
                    taken_old.add(old.address)
                    taken_new.add(new.address)
                    out.append((old, new, TWIN_SURE))
                    break
        return out

    def update(self, now: float, devices, force: bool = False) -> list[tuple[object, object, float]]:
        """Returns (old device, new device, how sure) for each new link."""
        if not force and now - self._checked < CHECK_EVERY:
            return []
        self._checked = now
        devices = list(devices)
        self._sample(now, devices)
        self._cache = {}
        try:
            return self._check(now, devices)
        finally:
            self._cache = None

    def _check(self, now: float, devices) -> list[tuple[object, object, float]]:
        alike = self._pairs_alike(now, {d.address: d for d in devices})
        self._find_twins(alike)
        self._off_and_on(now, devices)
        medians = sorted(d.smoothed for d in devices if d.smoothed is not None)
        used_old = {old for old, _ in self.links.values()}
        fresh = [d for d in devices if d.address not in self.links and not d.earlier and d.superseded_by is None
                 and now - d.first_seen <= FRESH and (len(d.history) >= 3 or now - d.first_seen >= 10)]
        # gone quiet long enough to be sure it isn't still talking; long gone only with a fingerprint
        longest = max(WINDOW, WINDOW_GAPS * SLOWEST)
        quiet = []
        for d in devices:
            age = now - d.last_seen
            if d.superseded_by is not None or d.address in used_old or age < 8:
                continue
            if age > FRESH + longest:  # long gone: only the bytes it keeps can tell
                if self.bridging(d):
                    quiet.append(d)
            elif age >= min(25.0, max(8.0, 2 * _gap(d))) and (age <= FRESH + _timing(d)[0] or self.bridging(d)):
                quiet.append(d)
        # Only an old address of the same kind that went quiet shortly before a new one started can be timed
        # with it, and one gone longer only by the very bytes the new one keeps: in a crowd, a handful each.
        quiet.sort(key=lambda d: d.last_seen)
        by_kind: dict[tuple, list] = {}
        by_print: dict[tuple, list] = {}
        for d in quiet:
            for key in {_loose(k) for k in self.adverts(d)}:
                by_kind.setdefault(key, []).append(d)
            for key in self.bridging(d):
                by_print.setdefault(key, []).append(d)
        went_quiet = {key: [d.last_seen for d in ds] for key, ds in by_kind.items()}
        ratios = {}
        for new in fresh:
            candidates = {}
            for key in {_loose(k) for k in self.adverts(new)}:
                if key in by_kind:
                    times = went_quiet[key]
                    for d in by_kind[key][bisect.bisect_left(times, new.first_seen - longest):
                                          bisect.bisect_right(times, new.first_seen + SLACK)]:
                        candidates[d.address] = d
            for key in self.bridging(new):
                for d in by_print.get(key, ()):
                    candidates.setdefault(d.address, d)
            for old in candidates.values():
                if old is not new and (old.address, new.address) not in self._wrong:
                    r = self.ratio(old, new, medians)
                    if r > 0:
                        ratios[(old.address, new.address)] = (old, new, r)
        out_sum, in_sum = {}, {}
        for (o, n), (_, _, r) in ratios.items():
            out_sum[o] = out_sum.get(o, 0.0) + r
            in_sum[n] = in_sum.get(n, 0.0) + r
        # the 1: the old one simply left, or the new one is a stranger
        sure = sorted(((r / (1 + max(out_sum[o], in_sum[n])), old, new) for (o, n), (old, new, r) in ratios.items()),
                      key=lambda x: -x[0])
        taken_old, taken_new = set(), set()
        due = self._anchored(now, devices, alike, taken_old, taken_new)  # the surest first
        for p, old, new in sure:
            if (p < SURE or old.address in taken_old or new.address in taken_new
                    or old.address in taken_new or new.address in taken_old):  # never both ways round
                continue
            taken_old.add(old.address)
            taken_new.add(new.address)
            due.append((old, new, p))
        due += self._partners_along(now, devices, due, taken_old, taken_new, used_old)
        for old, new, p in due:
            self.links[new.address] = (old.address, p)
            self._left[old.address] = old.last_seen
            by_timing = self.ratio(old, new, medians, fingerprints=False)
            heard = by_timing / (1 + by_timing)  # 0 for a change nobody heard, or a partner carried along
            self.learn(old, new, heard)
            self._together_with(now, old, new, heard)
        self._annotate(now, devices, due)
        return due

    # ---- partners ---------------------------------------------------------------------

    def _together_with(self, now: float, old, new, p: float) -> None:
        """Counts changes that happen together with another device's."""
        if p < LEARN_SURE:
            return
        root, head, kinds = old.root, _edge(new, head=True), frozenset(self.adverts(new))
        self._changed = [c for c in self._changed if now - c[0] <= 5 * 60]
        for first, last, signal, other, other_kinds in self._changed:
            if (other != root and not any(same_kind(a, b) for a in kinds for b in other_kinds)
                    and abs(first - new.first_seen) <= TOGETHER
                    and abs(last - old.last_seen) <= TOGETHER and head is not None and signal is not None
                    and abs(signal - head) <= TOGETHER_DB):
                pair = frozenset((root, other))
                self._together[pair] = self._together.get(pair, 0) + 1
                if self._together[pair] >= PARTNERS_AFTER:
                    self.pairs.add(pair)
        self._changed.append((new.first_seen, old.last_seen, head, root, kinds))

    def _partners_along(self, now, devices, due, taken_old, taken_new, used_old) -> list:
        """A device recognized after a change brings its partner along: the
        partner's new address turns up with it, from the same spot."""
        for old, new, _ in due:
            for pair in self.pairs:
                if old.root in pair:
                    other = next(r for r in pair if r != old.root)
                    self._expect.append((other, new.first_seen, _edge(new, head=True), old.last_seen, now + 30))
        self._expect = [e for e in self._expect if e[4] >= now]
        current = {d.root: d for d in devices if d.superseded_by is None}
        out = []
        for root, when, signal, left, until in list(self._expect):
            partner = current.get(root)
            # its latest address must have gone quiet with the leader's: one left over from a
            # change that went unlinked is long quiet, and isn't the one that changed now
            if (partner is None or partner.address in taken_old or partner.address in used_old
                    or now - partner.last_seen < 8 or abs(partner.last_seen - left) > TOGETHER):
                continue
            candidates = [d for d in devices if d.address not in self.links and d.address not in taken_new
                          and not d.earlier and d is not partner and (partner.address, d.address) not in self._wrong
                          and self._shared(partner, d)
                          and abs(d.first_seen - when) <= TOGETHER and signal is not None
                          and (_edge(d, head=True) is not None and abs(_edge(d, head=True) - signal) <= TOGETHER_DB)]
            if len(candidates) == 1:
                taken_old.add(partner.address)
                taken_new.add(candidates[0].address)
                out.append((partner, candidates[0], PARTNER_SURE))
                self._expect.remove((root, when, signal, left, until))
        return out

    def _annotate(self, now: float, devices, due) -> None:
        """Marks devices for the lists: partners, and how they're recognized."""
        current = {d.root: d for d in devices if d.superseded_by is None}
        # a new address carries the old one's chain only after hand_over; until then use the link
        for old, new, _ in due:
            current[old.root] = new
        # one device's advertisements, however they're known to go together
        how = {pair: OFF_AND_ON for pair in self.cycled}  # known more than one way: the most telling
        how.update({pair: CHANGES_WITH_IT for pair in self.pairs})
        how.update({pair: SAME_DATA for pair in self.twins})
        group: dict[str, set] = {}
        for pair in how:
            a, b = tuple(pair)
            members = group.get(a, {a}) | group.get(b, {b})
            for root in members:
                group[root] = members
        for d in devices:
            d.partners = []
        for root, members in group.items():
            d = current.get(root)
            if d is not None:
                latest = max(current[r].last_seen for r in members if r in current)
                d.partners = [(current[r], how.get(frozenset((root, r)))) for r in sorted(members - {root})
                              if r in current and latest - current[r].last_seen <= FRESH]  # not one long gone
        for d in current.values():
            if now - d.last_seen > FRESH:  # long gone: it keeps what it was last shown with
                continue
            fp = self.fingerprint(d)
            d.fingerprint_bytes = len(fp) if fp else 0
            d.fingerprint_confirmed = bool(fp) and self.confirmed(d)
