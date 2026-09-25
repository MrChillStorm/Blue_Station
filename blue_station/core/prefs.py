"""Settings and remembered devices, as small JSON files in the system's
usual place for app data: settings.json, and trackers.json for the
tracker watch's memory. BLUE_STATION_SETTINGS points settings.json
elsewhere, and the others go next to it."""
import json
import os
from pathlib import Path

from platformdirs import user_data_dir

SETTINGS, TRACKERS = "settings.json", "trackers.json"


def path(name: str = SETTINGS) -> Path:
    override = os.environ.get("BLUE_STATION_SETTINGS")
    base = Path(override) if override else Path(user_data_dir("Blue Station", appauthor=False)) / SETTINGS
    return base if name == SETTINGS else base.with_name(name)


def load(name: str = SETTINGS) -> dict:
    try:
        data = json.loads(path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict, name: str = SETTINGS) -> None:
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(target)
