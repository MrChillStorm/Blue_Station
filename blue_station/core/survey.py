"""A walk-around survey: points on a floor plan, each holding what every
device measured there. You click where you stand and hold still; the
next few seconds of packets become the point. Because every device is
kept, the map can show any of them afterwards. No Qt."""
import csv
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HOLD = 5.0  # seconds of packets that make a point: a Mac hears a slow beacon only about once a second
NOT_HEARD = -105.0  # what a device that wasn't heard at a point counts as on the map
USABLE = -85  # a beacon this strong or better is usable for positioning

DEVICE, STRONGEST, COUNT = "device", "strongest", "count"


@dataclass
class Point:
    x: float  # 0 to 1 across the plan
    y: float  # 0 to 1 down it
    t: float
    readings: dict[str, float] = field(default_factory=dict)  # address -> mean dBm


class Survey:
    def __init__(self):
        self.points: list[Point] = []
        self.pending: Point | None = None

    def start_point(self, x: float, y: float, now: float) -> None:
        self.pending = Point(min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0), now)

    def update(self, now: float, devices) -> Point | None:
        """Finishes the pending point once its seconds are up."""
        p = self.pending
        if p is None or now - p.t < HOLD:
            return None
        for d in devices:
            values = [r for t, r in d.recent(now, now - p.t + 0.001) if p.t <= t <= p.t + HOLD]
            if values:
                p.readings[d.address] = sum(values) / len(values)
        self.points.append(p)
        self.pending = None
        return p

    def undo(self) -> bool:
        if self.pending is not None:
            self.pending = None
            return True
        if self.points:
            self.points.pop()
            return True
        return False

    def clear(self) -> None:
        self.points.clear()
        self.pending = None

    # ---- what the map shows ----------------------------------------------------------

    @staticmethod
    def value(point: Point, mode: str, target: str | None, beacons: set[str]) -> float | None:
        """The number a point contributes: dBm (None if not heard), or a count."""
        if mode == DEVICE:
            return point.readings.get(target) if target else None
        heard = [r for a, r in point.readings.items() if a in beacons]
        if mode == STRONGEST:
            return max(heard) if heard else None
        return float(sum(1 for r in heard if r >= USABLE))

    def values(self, mode: str, target: str | None, beacons: set[str]) -> list[tuple[float, float, float | None]]:
        return [(p.x, p.y, self.value(p, mode, target, beacons)) for p in self.points]

    @staticmethod
    def grid(values: list[tuple[float, float, float | None]], cols: int, rows: int, aspect: float = 1.0,
             reach: float = 0.12) -> list[list[tuple[float, float]]]:
        """A smooth estimate over the plan (Gaussian-weighted average of the
        points, reach in plan widths): for each cell, the value and how far
        it is from the nearest point, so the map can fade where nothing was
        measured. aspect: the plan's height over its width."""
        known = [(x, y, NOT_HEARD if v is None else v) for x, y, v in values]
        out = []
        for r in range(rows):
            y = (r + 0.5) / rows
            row = []
            for c in range(cols):
                x = (c + 0.5) / cols
                distances = [math.hypot(x - px, (y - py) * aspect) for px, py, _ in known]
                if not distances:
                    row.append((NOT_HEARD, math.inf))
                    continue
                nearest = min(distances)
                # weights relative to the nearest point, so far corners don't underflow to nothing
                weights = [math.exp(-((d * d - nearest * nearest) / (reach * reach))) for d in distances]
                total = sum(weights)
                row.append((sum(w * v for w, (_, _, v) in zip(weights, known)) / total, nearest))
            out.append(row)
        return out

    def export(self, path: Path, titles: dict[str, str]) -> int:
        """One row per point and device, so any spreadsheet can pivot it."""
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["point", "x", "y", "time", "address", "device", "rssi_dbm"])
            for i, p in enumerate(self.points, 1):
                when = datetime.fromtimestamp(p.t).isoformat(timespec="seconds")
                for address, rssi in sorted(p.readings.items(), key=lambda kv: -kv[1]):
                    writer.writerow([i, f"{p.x:.4f}", f"{p.y:.4f}", when, address, titles.get(address, ""),
                                     f"{rssi:.1f}"])
        return len(self.points)
