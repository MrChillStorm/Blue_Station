"""New only: a baseline of the devices around you, so the Scan list can
show just the ones that turn up afterwards, and say so when one does.
No Qt.

Phones, earbuds and watches change their address every 15 minutes or
so, and a new address looks like a new device. One whose change was
heard (links.py) stays known, and so does one that advertises the same
name as a known one. The rest come back as new after a while, so a
baseline works best for a short watch, or for devices that keep their
address."""
from blue_station.core.devices import GONE_AFTER

SETTLE = 3.0  # seconds a newcomer is heard before it's announced: its name often comes a moment later


def _name(device) -> str | None:
    return device.name.strip().casefold() if device.name and device.name.strip() else None


class Baseline:
    def __init__(self, now: float, devices):
        self.since = now
        self.addresses: set[str] = set()
        self.names: set[str] = set()
        self._announced: set[str] = set()
        for d in devices:
            self._know(d)

    def _know(self, device) -> None:
        self.addresses.add(device.address)
        name = _name(device)
        if name:
            self.names.add(name)

    def is_new(self, device) -> bool:
        if device.address in self.addresses or any(a in self.addresses for a, _ in device.earlier):
            return False  # known, or known under an earlier address
        return _name(device) not in self.names

    def arrivals(self, now: float, devices, near=None) -> list:
        """The new devices to announce now, each once: heard for a few
        seconds, still in range, and near enough when near() is given."""
        due = []
        for d in devices:
            if d.address in self._announced or d.superseded_by is not None or not self.is_new(d):
                continue
            if now - d.first_seen < SETTLE or now - d.last_seen > GONE_AFTER:
                continue
            if near is not None and not near(d):
                continue
            self._announced.add(d.address)
            due.append(d)
        return due

    def count_new(self, devices, now: float) -> int:
        return sum(1 for d in devices if d.superseded_by is None and self.is_new(d)
                   and now - d.last_seen <= GONE_AFTER)
