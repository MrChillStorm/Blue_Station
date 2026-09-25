"""Names for Bluetooth numbers: companies, services and characteristics,
from the Bluetooth SIG's assigned numbers (via bluetooth-numbers, with
bleak's own table as a fallback for member UUIDs)."""
from functools import lru_cache
from uuid import UUID

import bluetooth_numbers as bn
from bleak.uuids import uuid16_dict

_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"


def _lookup(table, key):
    try:
        return table[key]
    except Exception:  # the tables raise their own errors for unknown or malformed keys
        return None


def short_uuid(uuid: str) -> int | None:
    """0x180F for '0000180f-0000-1000-8000-00805f9b34fb'; None for a
    vendor's own 128-bit UUID."""
    uuid = uuid.lower()
    if uuid.endswith(_BASE_SUFFIX) and uuid.startswith("0000"):
        return int(uuid[4:8], 16)
    return None


def pretty_uuid(uuid: str) -> str:
    """0x180F, or the full UUID when it isn't a SIG one."""
    short = short_uuid(uuid)
    return f"0x{short:04X}" if short is not None else uuid.upper()


@lru_cache(maxsize=4096)
def company(cid: int) -> str | None:
    return _lookup(bn.company, cid)


def _named(table, uuid: str) -> str | None:
    short = short_uuid(uuid)
    if short is not None:
        return _lookup(table, short)
    try:
        return _lookup(table, UUID(uuid))
    except ValueError:
        return None


@lru_cache(maxsize=4096)
def service(uuid: str) -> str | None:
    """'Battery Service', 'Fast Pair Service'; for a member UUID (0xFCxx to
    0xFExx) the company that registered it."""
    name = _named(bn.service, uuid)
    if name:
        return name
    short = short_uuid(uuid)
    if short is not None and 0xFC00 <= short <= 0xFEFF:
        return uuid16_dict.get(short)
    return None


@lru_cache(maxsize=4096)
def service_owner(uuid: str) -> str | None:
    """The company behind a member service UUID, if it is one."""
    short = short_uuid(uuid)
    if short is not None and 0xFC00 <= short <= 0xFEFF:
        return uuid16_dict.get(short)
    return None


@lru_cache(maxsize=4096)
def characteristic(uuid: str) -> str | None:
    return _named(bn.characteristic, uuid)


def service_label(uuid: str) -> str:
    """'Battery Service (0x180F)'."""
    name = service(uuid)
    return f"{name} ({pretty_uuid(uuid)})" if name else pretty_uuid(uuid)


# GAP Appearance categories (the value's top 10 bits), from the SIG's
# assigned numbers
APPEARANCE = {
    0: "Unknown", 1: "Phone", 2: "Computer", 3: "Watch", 4: "Clock", 5: "Display", 6: "Remote control",
    7: "Eyeglasses", 8: "Tag", 9: "Keyring", 10: "Media player", 11: "Barcode scanner", 12: "Thermometer",
    13: "Heart rate sensor", 14: "Blood pressure monitor", 15: "Human interface device", 16: "Glucose meter",
    17: "Running or walking sensor", 18: "Cycling sensor", 19: "Control device", 20: "Network device",
    21: "Sensor", 22: "Light fixture", 23: "Fan", 24: "HVAC", 33: "Audio sink", 34: "Audio source",
    37: "Wearable audio device", 41: "Hearing aid", 42: "Gaming device", 49: "Pulse oximeter", 50: "Weight scale",
}


def appearance(value: int) -> str:
    return APPEARANCE.get(value >> 6, f"Category {value >> 6}")
