"""Adds where you were to a packet log exported from Develop, from a GPS
track of the same time (a GPX file from any logging app): for a recording
made without Coordinates, or with the phone's positions too far apart.

    python3 tools/add_track.py "Packets 2026-09-28 1400.csv" track.gpx

Writes "Packets 2026-09-28 1400 with track.csv" beside the log (or -o).
Each packet gets the track's position at its time, interpolated between
the track's points (blue_station/core/position.py); one the track
doesn't cover keeps what it had."""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blue_station.core.packets import position_cells  # noqa: E402
from blue_station.core.position import Track, read_gpx  # noqa: E402

COLUMNS = ["latitude", "longitude", "position_accuracy_m", "position_gap_s"]


def add_track(log: Path, track: Track, out: Path) -> tuple[int, int]:
    """Returns (packets, of them given a position by the track)."""
    with log.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or []) + [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        rows = list(reader)
    placed = 0
    for r in rows:
        where = track.at(float(r["unix_time"]))
        if where is not None:
            r.update(zip(COLUMNS, position_cells(where)))
            placed += 1
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), placed


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path, help="a packet log exported from Develop")
    parser.add_argument("track", type=Path, help="the GPS track (.gpx)")
    parser.add_argument("-o", "--out", type=Path, help="where to write it (default: beside the log)")
    args = parser.parse_args(argv)
    track = Track(read_gpx(args.track))
    if not track:
        raise SystemExit(f"No timed track points in {args.track}.")
    out = args.out or args.log.with_name(f"{args.log.stem} with track.csv")
    count, placed = add_track(args.log, track, out)
    print(f"{placed} of {count} packets placed, written to {out}")


if __name__ == "__main__":
    main()
