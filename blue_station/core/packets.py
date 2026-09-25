"""The packet log: every advertisement exactly as heard, for when that
level of detail is wanted. Off until asked for, one device or all of
them, and never more than the last LIMIT packets."""
import csv
from collections import deque
from datetime import datetime
from pathlib import Path

from blue_station.core.decode import hex_bytes
from blue_station.core.devices import Sighting
from blue_station.core.names import pretty_uuid

LIMIT = 100_000


def payload_text(s: Sighting) -> str:
    """'4C: 10 05 07 … · FEAA: 10 EE 03 …' -- maker data by company, service data by UUID."""
    parts = [f"{cid:04X}: {hex_bytes(data, 31)}" for cid, data in s.manufacturer_data.items()]
    parts += [f"{pretty_uuid(uuid)}: {hex_bytes(data, 31)}" for uuid, data in s.service_data.items()]
    return "  ·  ".join(parts)


class PacketLog:
    def __init__(self, limit: int = LIMIT):
        self.rows: deque[Sighting] = deque(maxlen=limit)
        self.recording = False
        self.address: str | None = None  # None: every device
        self.total = 0  # recorded since the last clear, including any that fell off the front

    def start(self, address: str | None) -> None:
        self.recording, self.address = True, address

    def stop(self) -> None:
        self.recording = False

    def clear(self) -> None:
        self.rows.clear()
        self.total = 0

    def add(self, sightings: list[Sighting]) -> int:
        if not self.recording:
            return 0
        kept = sorted((s for s in sightings if self.address is None or s.address == self.address),
                      key=lambda s: s.t)
        self.rows.extend(kept)
        self.total += len(kept)
        return len(kept)

    def latest(self, count: int) -> list[Sighting]:
        """The newest packets, newest first."""
        out = []
        for s in reversed(self.rows):
            if len(out) == count:
                break
            out.append(s)
        return out

    def export(self, path: Path, titles: dict[str, str] | None = None) -> int:
        titles = titles or {}
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "unix_time", "address", "device", "rssi_dbm", "name", "tx_power_dbm",
                             "connectable", "manufacturer_data", "service_uuids", "service_data"])
            for s in self.rows:
                writer.writerow([
                    datetime.fromtimestamp(s.t).isoformat(timespec="milliseconds"), f"{s.t:.3f}", s.address,
                    titles.get(s.address, ""), s.rssi, s.name or "", "" if s.tx_power is None else s.tx_power,
                    {True: "yes", False: "no", None: ""}[s.connectable],
                    "; ".join(f"0x{cid:04X}:{data.hex()}" for cid, data in s.manufacturer_data.items()),
                    "; ".join(s.service_uuids),
                    "; ".join(f"{uuid}:{data.hex()}" for uuid, data in s.service_data.items()),
                ])
        return len(self.rows)
