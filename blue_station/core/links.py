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

Two things are learned from the changes it's sure of:

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

A device may not send the same message set every time (an AirPlay target
now and then slips in a short extra message), so each kind of
advertisement a device sends is remembered, and two addresses are
compared on the kinds they have in common."""
import math
import statistics

from blue_station.core.decode import apple_messages

WINDOW = 60.0  # seconds from the old address's last packet to the new one's first, at most
PAUSE = 20.0  # a change usually leaves a gap of up to this
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
    and flags. The same device keeps these across an address change. (Its
    name is compared apart: macOS knows one address's name after a
    connection, and a new address starts without it.) md: one of the
    advertisements it has sent, by default its latest."""
    md = device.manufacturer_data if md is None else md
    return (_layout(md), tuple(sorted(device.service_uuids)), device.connectable, device.tx_power)


def kind(device, md: dict | None = None) -> str:
    return repr(signature(device, md))


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


def lead(a, b):
    """Of two partners, the one to list: a name, or the more telling kind."""
    return max((a, b), key=lambda d: (d.name is not None, d.info._rank, d.address))


def _edge(device, head: bool) -> float | None:
    points = list(device.history)
    points = points[:EDGE] if head else points[-EDGE:]
    return statistics.median(r for _, r in points) if points else None


def _gap(device) -> float:
    """The usual time between its packets, from the latest ones."""
    times = [t for t, _ in list(device.history)[-12:]]
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    return statistics.median(gaps) if gaps else 2.0


class Linker:
    def __init__(self, learned: dict | None = None):
        self.links: dict[str, tuple[str, float]] = {}  # new address -> (old address, how sure)
        self.steady: dict[str, set[int]] = {}  # kind -> byte positions the same at every change seen
        self.changes: dict[str, int] = {}  # kind -> changes seen, sure ones
        for k, v in (learned or {}).items():
            self.steady[k], self.changes[k] = set(v.get("steady", [])), int(v.get("changes", 0))
        self.pairs: set[frozenset] = set()  # partner chains, by the first address of each
        self._variants: dict[str, dict[str, dict]] = {}  # address -> kind -> its latest advertisement of that kind
        self._together: dict[frozenset, int] = {}
        self._changed: list[tuple] = []  # recent sure changes: (new first heard, old last heard, signal, root, kinds)
        self._expect: list[tuple] = []  # partners due to change: (partner root, when, signal, until)
        self._checked = float("-inf")

    def to_dict(self) -> dict:
        return {k: {"steady": sorted(self.steady[k]), "changes": self.changes[k]} for k in self.steady}

    # ---- what each device sends -------------------------------------------------------

    def adverts(self, device) -> dict[str, dict]:
        """Each kind of advertisement it has sent: kind -> the latest of that kind."""
        out = dict(self._variants.get(device.address, {}))
        out[kind(device)] = dict(device.manufacturer_data)
        return out

    def _shared(self, old, new) -> list[str]:
        theirs = self.adverts(old)
        return [k for k in self.adverts(new) if k in theirs]

    # ---- fingerprints ---------------------------------------------------------------

    def learn(self, old, new, sure: float) -> None:
        if sure < LEARN_SURE:
            return
        before, after = self.adverts(old), self.adverts(new)
        for k in self._shared(old, new):
            a, b = payload(before[k]), payload(after[k])
            if not a or len(a) != len(b):
                continue
            same = {i for i in range(len(a)) if a[i] == b[i]}
            self.steady[k] = same if k not in self.steady else self.steady[k] & same  # only ever fewer
            self.changes[k] = self.changes.get(k, 0) + 1

    def _fingerprint(self, k: str, md: dict) -> bytes | None:
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
        found = [(self.changes[k] >= LEARN_AFTER, fp) for k, md in self.adverts(device).items()
                 if (fp := self._fingerprint(k, md)) is not None]
        return max(found)[1] if found else None

    def confirmed(self, device) -> bool:
        """Its fingerprint has held through more than one change."""
        return any(self.changes.get(k, 0) >= LEARN_AFTER and self._fingerprint(k, md) is not None
                   for k, md in self.adverts(device).items())

    # ---- scoring ----------------------------------------------------------------------

    def _crowd(self, medians: list[float], rssi: float) -> float:
        """How likely a stranger is to be heard this strong, per dB."""
        near = sum(1 for m in medians if abs(m - rssi) <= 5)
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
            for k in shared:
                a, b = self._fingerprint(k, before[k]), self._fingerprint(k, after[k])
                if a is not None and b is not None:
                    found = (a == b, self.changes[k] >= LEARN_AFTER, len(b))
                    evidence = found if evidence is None else max(evidence, found)
        if dt > WINDOW:  # a change nobody heard: only a fingerprint can tell
            if evidence is None or not evidence[0] or (not evidence[1] and evidence[2] < FIRST_MIN_BYTES):
                return 0.0
            return FINGERPRINT if evidence[1] else FIRST_FINGERPRINT
        tail, head = _edge(old, head=False), _edge(new, head=True)
        if tail is None or head is None:
            return 0.0
        same_time = 1 / PAUSE if dt <= PAUSE else 0.2 / (WINDOW - PAUSE)
        stranger_time = 1 / (WINDOW + SLACK)
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
        for d in devices:
            if now - d.last_seen <= CHECK_EVERY + 1 and d.manufacturer_data:
                self._variants.setdefault(d.address, {})[kind(d)] = dict(d.manufacturer_data)

    def update(self, now: float, devices, force: bool = False) -> list[tuple[object, object, float]]:
        """Returns (old device, new device, how sure) for each new link."""
        if not force and now - self._checked < CHECK_EVERY:
            return []
        self._checked = now
        devices = list(devices)
        self._sample(now, devices)
        medians = [d.smoothed for d in devices if d.smoothed is not None]
        used_old = {old for old, _ in self.links.values()}
        fresh = [d for d in devices if d.address not in self.links and not d.earlier
                 and now - d.first_seen <= FRESH and (len(d.history) >= 3 or now - d.first_seen >= 10)]
        # gone quiet long enough to be sure it isn't still talking; long gone only with a fingerprint
        quiet = [d for d in devices if d.address not in used_old and d.superseded_by is None
                 and now - d.last_seen >= min(25.0, max(8.0, 2 * _gap(d)))
                 and (now - d.last_seen <= FRESH + WINDOW or self.fingerprint(d) is not None)]
        ratios = {}
        for new in fresh:
            for old in quiet:
                if old is not new and old.last_seen <= new.first_seen + SLACK:
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
        due, taken_old, taken_new = [], set(), set()
        for p, old, new in sure:
            if p < SURE or old.address in taken_old or new.address in taken_new:
                continue
            taken_old.add(old.address)
            taken_new.add(new.address)
            due.append((old, new, p))
        due += self._partners_along(now, devices, due, taken_old, taken_new, used_old)
        for old, new, p in due:
            self.links[new.address] = (old.address, p)
            by_timing = self.ratio(old, new, medians, fingerprints=False)
            heard = by_timing / (1 + by_timing)  # 0 for a change nobody heard, or a partner carried along
            self.learn(old, new, heard)
            self._together_with(now, old, new, heard)
        self._annotate(devices, due)
        return due

    # ---- partners ---------------------------------------------------------------------

    def _together_with(self, now: float, old, new, p: float) -> None:
        """Counts changes that happen together with another device's."""
        if p < LEARN_SURE:
            return
        root, head, kinds = old.root, _edge(new, head=True), frozenset(self.adverts(new))
        self._changed = [c for c in self._changed if now - c[0] <= 5 * 60]
        for first, last, signal, other, other_kinds in self._changed:
            if (other != root and kinds.isdisjoint(other_kinds) and abs(first - new.first_seen) <= TOGETHER
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
                    self._expect.append((other, new.first_seen, _edge(new, head=True), now + 30))
        self._expect = [e for e in self._expect if e[3] >= now]
        current = {d.root: d for d in devices if d.superseded_by is None}
        out = []
        for root, when, signal, until in list(self._expect):
            partner = current.get(root)
            if (partner is None or partner.address in taken_old or partner.address in used_old
                    or now - partner.last_seen < 8):
                continue
            candidates = [d for d in devices if d.address not in self.links and d.address not in taken_new
                          and not d.earlier and d is not partner and self._shared(partner, d)
                          and abs(d.first_seen - when) <= TOGETHER and signal is not None
                          and (_edge(d, head=True) is not None and abs(_edge(d, head=True) - signal) <= TOGETHER_DB)]
            if len(candidates) == 1:
                taken_old.add(partner.address)
                taken_new.add(candidates[0].address)
                out.append((partner, candidates[0], PARTNER_SURE))
                self._expect.remove((root, when, signal, until))
        return out

    def _annotate(self, devices, due) -> None:
        """Marks devices for the lists: partners, and how they're recognized."""
        current = {d.root: d for d in devices if d.superseded_by is None}
        # a new address carries the old one's chain only after hand_over; until then use the link
        for old, new, _ in due:
            current[old.root] = new
        for d in devices:
            d.partner = None
        for pair in self.pairs:
            a, b = (current.get(r) for r in pair)
            if a is not None and b is not None:
                a.partner, b.partner = b, a
        for d in current.values():
            fp = self.fingerprint(d)
            d.fingerprint_bytes = len(fp) if fp else 0
            d.fingerprint_confirmed = bool(fp) and self.confirmed(d)
