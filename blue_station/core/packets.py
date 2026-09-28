"""The packet log: every advertisement exactly as heard, for when that
level of detail is wanted. Off until asked for, one device or all of
them, and never more than the last LIMIT packets.

With read_names on, it also asks each device it records who it is, once
per address: name, maker, model and the like, read over a connection.
That's an answer key for address changes that doesn't come from Blue
Station's own guess (links.py), which never sees it: two addresses that
told the same identity are likely one device, two that told different
ones are two. It's exported beside the packets, with the guess.

With your phone's positions (position.py: live, or a GPS track added
afterwards), each packet is exported with where you were when it was
heard."""
import csv
from collections import deque
from datetime import datetime
from pathlib import Path

from blue_station.core.decode import hex_bytes
from blue_station.core.devices import Sighting
from blue_station.core.names import pretty_uuid
from blue_station.core.position import PORT, Fix, PhoneReceiver, Track

LIMIT = 350_000  # about 45 MB, and 12 hours of a home's packets
SHARED_MAX = 200_000  # repeats kept for sharing, before starting over
FRESH = 5.0  # seconds: only a device heard this lately is asked who it is


def payload_text(s: Sighting) -> str:
    """'4C: 10 05 07 … · FEAA: 10 EE 03 …' -- maker data by company, service data by UUID."""
    parts = [f"{cid:04X}: {hex_bytes(data, 31)}" for cid, data in s.manufacturer_data.items()]
    parts += [f"{pretty_uuid(uuid)}: {hex_bytes(data, 31)}" for uuid, data in s.service_data.items()]
    return "  ·  ".join(parts)


def read(path: Path) -> list[Sighting]:
    """The packets of an exported log, back as they were heard: for replaying
    one through the app's own code (tools/drive_check.py)."""
    def pairs(text: str):
        return [part.rsplit(":", 1) for part in text.split("; ") if part]

    out = []
    with Path(path).open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            out.append(Sighting(
                r["address"], int(r["rssi_dbm"]), float(r["unix_time"]), name=r["name"] or None,
                tx_power=int(r["tx_power_dbm"]) if r["tx_power_dbm"] else None,
                manufacturer_data={int(cid, 16): bytes.fromhex(data) for cid, data in pairs(r["manufacturer_data"])},
                service_uuids=tuple(u for u in r["service_uuids"].split("; ") if u),
                service_data={uuid: bytes.fromhex(data) for uuid, data in pairs(r["service_data"])},
                connectable={"yes": True, "no": False}.get(r["connectable"])))
    return out


def identity_text(summary: list[tuple[str, str]]) -> str:
    """'Device name=Mac; Manufacturer=Apple Inc.; Model=Mac16,8'."""
    return "; ".join(f"{label}={value}" for label, value in summary) or "error=nothing it would tell"


class PacketLog:
    def __init__(self, limit: int = LIMIT):
        self.rows: deque[Sighting] = deque(maxlen=limit)
        self.recording = False
        self.address: str | None = None  # None: every device
        self.total = 0  # recorded since the last clear, including any that fell off the front
        self.read_names = False
        self.identities: dict[str, str] = {}  # address -> identity_text(), or "error=why"
        self.asked: set[str] = set()
        self._shared: dict = {}  # a repeated part of a packet -> the one copy kept of it
        self.track = Track()  # where you were: from your phone, or a GPS track added
        self.phone: PhoneReceiver | None = None  # taking your phone's positions, while asked to
        self.port = PORT

    def start(self, address: str | None) -> None:
        self.recording, self.address = True, address

    def stop(self) -> None:
        self.recording = False

    def clear(self) -> None:
        self.rows.clear()
        self.total = 0
        self.identities.clear()
        self.asked.clear()
        self._shared.clear()
        self.track.clear()

    # ---- where you were ------------------------------------------------------------------

    def listen(self, word: str) -> str | None:
        """Takes your phone's positions until unlisten(). None, or why it can't."""
        if self.phone is None:
            self.phone = PhoneReceiver(word, self.port)
        error = self.phone.start()
        if error:
            self.phone = None
        return error

    def unlisten(self) -> None:
        if self.phone is not None:
            self.phone.stop()
            self.take_positions()
            self.phone = None

    def take_positions(self) -> list[Fix]:
        """The positions the phone sent since the last time: into the track, and returned."""
        fixes = self.phone.drain() if self.phone is not None else []
        self.track.add(fixes)
        return fixes

    def add_track(self, fixes: list[Fix]) -> tuple[int, int]:
        """A GPS track, say from a GPX file. Returns (positions it added,
        one per moment, and of them how many fall within the packets
        recorded, give or take a minute)."""
        added = self.track.add(fixes)
        if not self.rows:
            return added, 0
        first, last = self.rows[0].t - 60, self.rows[-1].t + 60
        return added, len({f.t for f in fixes if first <= f.t <= last})

    def add(self, sightings: list[Sighting]) -> int:
        if not self.recording:
            return 0
        kept = sorted((self._compact(s) for s in sightings if self.address is None or s.address == self.address),
                      key=lambda s: s.t)
        self.rows.extend(kept)
        self.total += len(kept)
        return len(kept)

    def _one(self, value):
        key = (type(value), tuple(value.items()) if isinstance(value, dict) else value)
        return self._shared.setdefault(key, value)

    def _compact(self, s: Sighting) -> Sighting:
        """The same packet, sharing the parts it repeats (its address, name
        and payload) with the packets before it: a quarter of the memory."""
        if len(self._shared) > SHARED_MAX:
            self._shared.clear()
        one = self._one
        return Sighting(one(s.address), s.rssi, s.t, s.name and one(s.name), s.tx_power, one(s.manufacturer_data),
                        one(s.service_uuids), one(s.service_data), s.connectable, s.mac and one(s.mac))

    def next_to_ask(self, now: float, devices, near=None):
        """The next device to ask who it is, if any: one this log records,
        that accepts connections, heard just now, whose address hasn't
        been asked yet. The strongest first; near(device), if given, has
        the last word."""
        if not (self.recording and self.read_names):
            return None
        due = [d for d in devices
               if d.connectable and d.superseded_by is None and d.address not in self.asked
               and (self.address is None or d.address == self.address) and now - d.last_seen <= FRESH
               and (near is None or near(d))]
        return max(due, key=lambda d: -999.0 if d.smoothed is None else d.smoothed, default=None)

    def asking(self, address: str) -> None:
        self.asked.add(address)

    def answer(self, address: str, identity: str) -> None:
        if address in self.asked:  # not thrown away meanwhile
            self.identities[address] = identity

    def read_counts(self) -> tuple[int, int, int]:
        """(told, didn't, still being asked)"""
        failed = sum(1 for v in self.identities.values() if v.startswith("error="))
        return len(self.identities) - failed, failed, len(self.asked) - len(self.identities)

    def latest(self, count: int) -> list[Sighting]:
        """The newest packets, newest first."""
        out = []
        for s in reversed(self.rows):
            if len(out) == count:
                break
            out.append(s)
        return out

    def export(self, path: Path, titles: dict[str, str] | None = None,
               links: dict[str, tuple[str, float]] | None = None) -> int:
        """links: address -> (the address it changed from, how sure), Blue
        Station's guess, beside the identity each address told."""
        titles, links = titles or {}, links or {}
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "unix_time", "address", "device", "rssi_dbm", "name", "tx_power_dbm",
                             "connectable", "manufacturer_data", "service_uuids", "service_data",
                             "identity", "changed_from", "change_sure",
                             "latitude", "longitude", "position_accuracy_m", "position_gap_s"])
            for s in self.rows:
                writer.writerow([
                    datetime.fromtimestamp(s.t).isoformat(timespec="milliseconds"), f"{s.t:.3f}", s.address,
                    titles.get(s.address, ""), s.rssi, s.name or "", "" if s.tx_power is None else s.tx_power,
                    {True: "yes", False: "no", None: ""}[s.connectable],
                    "; ".join(f"0x{cid:04X}:{data.hex()}" for cid, data in s.manufacturer_data.items()),
                    "; ".join(s.service_uuids),
                    "; ".join(f"{uuid}:{data.hex()}" for uuid, data in s.service_data.items()),
                    self.identities.get(s.address, ""),
                    links[s.address][0] if s.address in links else "",
                    f"{links[s.address][1]:.2f}" if s.address in links else "",
                    *position_cells(self.track.at(s.t) if self.track else None),
                ])
        return len(self.rows)


def position_cells(where) -> list[str]:
    """latitude, longitude, accuracy (m) and seconds to the nearest real position, for a CSV row."""
    if where is None:
        return ["", "", "", ""]
    lat, lon, acc, gap = where
    return [f"{lat:.6f}", f"{lon:.6f}", "" if acc is None else f"{acc:.0f}", f"{gap:.0f}"]
