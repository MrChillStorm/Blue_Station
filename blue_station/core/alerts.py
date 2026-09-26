"""Alerts for devices you care about: when one goes out of range (left
behind?) and when one comes back. No Qt.

Only what happens while scanning runs without a break counts. Pausing,
Bluetooth going off or the Mac sleeping make every device look gone, so
they start the watch over instead of alerting. After a start, the room
is heard for a while before anything counts as having arrived."""
from blue_station.core.devices import GONE_AFTER

SETTLE = GONE_AFTER  # seconds after scanning (re)starts before anything alerts
GAP = 10.0  # a tick this much later than the last means the app was asleep
COOLDOWN = 5 * 60  # the same alert for the same device at most this often: one on the edge of range
GONE, BACK = "gone", "back"


class Alerts:
    def __init__(self):
        self.since: float | None = None  # scanning without a break since
        self._tick: float | None = None
        self._here: dict[str, bool] = {}  # every listed device: in range at the last tick?
        self._alerted: dict[tuple[str, str], float] = {}

    def hand_over(self, old: str, new: str) -> None:
        """The new address is a device that was here all along: not an arrival."""
        self._here[new] = True
        self._here.pop(old, None)

    def update(self, now: float, devices, scanning: bool) -> list[tuple[object, str]]:
        """Returns (device, GONE or BACK) for each alert due."""
        if not scanning or (self._tick is not None and now - self._tick > GAP):
            self.since = None
        self._tick = now
        if not scanning:
            return []
        if self.since is None:
            self.since = now
            self._here.clear()
        settled = now - self.since >= SETTLE
        due, here = [], {}
        for d in devices:
            if d.superseded_by is not None:  # it changed its address, it didn't leave
                continue
            here[d.address] = now_here = not d.gone(now)
            was = self._here.get(d.address, False)  # first heard just now: it arrived
            if not settled or was == now_here:
                continue
            what = GONE if was else BACK
            if not (d.alert_gone if what == GONE else d.alert_back):
                continue
            if now - self._alerted.get((d.address, what), float("-inf")) < COOLDOWN:
                continue
            self._alerted[(d.address, what)] = now
            due.append((d, what))
        self._here = here
        return due
