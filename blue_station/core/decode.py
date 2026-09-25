"""What an advertisement says about its device: what kind of thing it is,
who made it, and anything its payload decodes to (iBeacon, Eddystone,
AirPods battery, Find My, Windows...). Pure functions, no Qt.

The Apple Continuity, Microsoft CDP and Find My layouts aren't published
by their makers; they're the community's reverse-engineered readings,
widely used by other scanners. Treat those fields as good hints."""
import re
import struct
from dataclasses import dataclass, field
from uuid import UUID

from blue_station.core import names

APPLE, MICROSOFT = 0x004C, 0x0006


@dataclass
class Info:
    kind: str | None = None
    icon: str = "generic"
    vendor: str | None = None
    fields: list[tuple[str, str]] = field(default_factory=list)
    ref_1m: int | None = None  # the RSSI the payload itself says to expect at 1 m
    tracker: bool = False
    beacon: bool = False
    battery: str | None = None  # a beacon's own battery report
    detail: str | None = None  # a few words that tell it apart in a list: 'major 1 · minor 42'
    note: str | None = None  # a sentence worth showing on the device page
    _rank: int = 0

    def offer(self, rank: int, kind: str, icon: str) -> None:
        """Keeps the most specific kind any decoder found."""
        if rank > self._rank:
            self._rank, self.kind, self.icon = rank, kind, icon


_LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "gmbh", "ag", "ab",
          "a/s", "as", "asa", "oy", "oyj", "bv", "b.v", "nv", "n.v", "sa", "s.a", "sas", "spa", "s.p.a", "srl", "s.r.l",
          "pty", "plc", "kk", "k.k"}


def short_company(name: str | None) -> str | None:
    """'Apple' for 'Apple, Inc.', 'Samsung Electronics' for '... Co. Ltd.'."""
    if not name:
        return name
    words = name.replace(",", " ").split()
    while len(words) > 1 and words[-1].strip(".:").lower() in _LEGAL:
        words.pop()
    return " ".join(words).strip(" .,:") or name


def hex_bytes(data: bytes, limit: int = 64) -> str:
    text = data[:limit].hex(" ").upper()
    return text + (" …" if len(data) > limit else "")


def _int8(b: int) -> int:
    return b - 256 if b > 127 else b


# ---- Apple Continuity ------------------------------------------------------------

_APPLE_TYPES = {
    0x02: "iBeacon", 0x03: "AirPrint", 0x05: "AirDrop", 0x06: "HomeKit", 0x07: "Proximity Pairing",
    0x08: "Hey Siri", 0x09: "AirPlay Target", 0x0A: "AirPlay Source", 0x0B: "Magic Switch", 0x0C: "Handoff",
    0x0D: "Tethering Target", 0x0E: "Tethering Source", 0x0F: "Nearby Action", 0x10: "Nearby Info",
    0x12: "Find My",
}
_AIRPODS = {
    0x0220: "AirPods", 0x0F20: "AirPods (2nd generation)", 0x1320: "AirPods (3rd generation)",
    0x0E20: "AirPods Pro", 0x1420: "AirPods Pro (2nd generation)", 0x2420: "AirPods Pro (2nd generation, USB-C)",
    0x0A20: "AirPods Max", 0x0320: "Powerbeats3", 0x0B20: "Powerbeats Pro", 0x0520: "BeatsX",
    0x0620: "Beats Solo3", 0x0920: "Beats Studio3", 0x0C20: "Beats Solo Pro", 0x1020: "Beats Flex",
    0x1120: "Beats Studio Buds", 0x1220: "Beats Fit Pro", 0x1620: "Beats Studio Buds +", 0x1720: "Beats Studio Pro",
}
_NEARBY_ACTIVITY = {
    0x00: "Not known", 0x01: "Reporting off", 0x03: "Idle", 0x05: "Audio playing, screen off",
    0x07: "Screen on", 0x09: "Screen on, video playing", 0x0A: "Watch on wrist and unlocked",
    0x0B: "Recently used", 0x0D: "Driving", 0x0E: "On a call",
}
_FIND_MY_BATTERY = ("Full", "Medium", "Low", "Very low")


def apple_messages(data: bytes) -> list[tuple[int, bytes]]:
    """Apple packs several type-length-value messages into one payload."""
    out, i = [], 0
    while i + 2 <= len(data):
        kind, length = data[i], data[i + 1]
        out.append((kind, data[i + 2:i + 2 + length]))
        i += 2 + length
    return out


def _battery(nibble: int) -> str | None:
    return f"{nibble * 10} %" if nibble <= 10 else None


def _apple(data: bytes, info: Info) -> None:
    messages = apple_messages(data)
    types = [_APPLE_TYPES.get(k, f"0x{k:02X}") for k, _ in messages]
    if types:
        info.fields.append(("Apple messages", ", ".join(types)))
    for kind, body in messages:
        if kind == 0x02 and len(body) >= 21:
            uuid = UUID(bytes=bytes(body[:16]))
            major, minor = struct.unpack(">HH", body[16:20])
            power = _int8(body[20])
            info.offer(90, "iBeacon", "beacon")
            info.beacon, info.detail = True, f"major {major} · minor {minor}"
            info.fields += [("iBeacon UUID", str(uuid).upper()), ("Major · minor", f"{major} · {minor}"),
                            ("Measured power", f"{power} dBm at 1 m")]
            info.ref_1m = power
        elif kind == 0x07 and len(body) >= 6:
            model = int.from_bytes(body[1:3], "big")
            info.offer(95, _AIRPODS.get(model, "AirPods or Beats"), "headphones")
            info.fields.append(("Model code", f"0x{model:04X}"))
            pods = [b for b in (_battery(body[4] >> 4), _battery(body[4] & 0x0F)) if b]
            case = _battery(body[5] & 0x0F)
            if pods:
                info.fields.append(("Earbud battery", " · ".join(pods)))
            if case:
                info.fields.append(("Case battery", case))
            if pods or case:
                info.detail = " · ".join(pods + ([f"case {case}"] if case else []))
        elif kind == 0x12 and body:
            separated = len(body) >= 22
            info.offer(80, "Find My device", "tag")
            info.detail = "Away from its owner" if separated else "Near its owner"
            info.fields.append(("Find My", info.detail))
            info.fields.append(("Battery hint", _FIND_MY_BATTERY[body[0] >> 6]))
            if separated:
                info.tracker = True
                info.note = ("A Find My device (an AirTag, or an Apple device that's offline) that has been away "
                             "from its owner for a while. If it keeps turning up wherever you go, it may be "
                             "travelling with you.")
        elif kind == 0x0B:
            info.offer(70, "Apple Watch", "watch")
        elif kind == 0x09:
            info.offer(65, "AirPlay target", "tv")
        elif kind == 0x0E:
            info.offer(60, "iPhone or iPad (hotspot)", "phone")
        elif kind == 0x06:
            info.offer(55, "HomeKit accessory", "sensor")
        elif kind == 0x10 and body:
            activity = _NEARBY_ACTIVITY.get(body[0] & 0x0F)
            if activity:
                info.fields.append(("Activity", activity))
            info.offer(30, "Apple device", "phone")
        elif kind in (0x0C, 0x05, 0x0F, 0x0D):
            info.offer(25, "Apple device", "laptop" if kind == 0x0C else "phone")


# ---- Microsoft ------------------------------------------------------------------

_WINDOWS_DEVICES = {
    1: ("Xbox One", "tv"), 6: ("iPhone", "phone"), 7: ("iPad", "phone"), 8: ("Android device", "phone"),
    9: ("Windows desktop", "laptop"), 11: ("Windows phone", "phone"), 12: ("Linux device", "laptop"),
    13: ("Windows IoT", "sensor"), 14: ("Surface Hub", "tv"), 15: ("Windows laptop", "laptop"),
    16: ("Windows tablet", "laptop"),
}


def _microsoft(data: bytes, info: Info) -> None:
    if len(data) >= 2 and data[0] == 0x01:
        kind, icon = _WINDOWS_DEVICES.get(data[1] & 0x1F, ("Windows device", "laptop"))
        info.offer(60, kind, icon)
        info.fields.append(("Microsoft", "Connected Devices Platform beacon"))
    elif data[:1] == b"\x03":
        info.offer(50, "Swift Pair accessory", "keyboard")
        info.fields.append(("Microsoft", "Swift Pair: ready to pair with Windows"))


# ---- service data ---------------------------------------------------------------

_URL_SCHEMES = ("http://www.", "https://www.", "http://", "https://")
_URL_CODES = (".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/",
              ".com", ".org", ".edu", ".net", ".info", ".biz", ".gov")


def eddystone_url(data: bytes) -> str | None:
    if len(data) < 3 or data[2] >= len(_URL_SCHEMES):
        return None
    tail = "".join(_URL_CODES[b] if b < len(_URL_CODES) else chr(b) if 0x20 < b < 0x7F else "?" for b in data[3:])
    return _URL_SCHEMES[data[2]] + tail


def _eddystone(data: bytes, info: Info) -> None:
    """One Eddystone frame. A beacon takes turns sending its UID or URL
    frame and its telemetry (TLM), so a device's frames are decoded
    together."""
    if not data:
        return
    frame = data[0]
    info.beacon = True
    if frame in (0x00, 0x10) and len(data) >= 2:
        info.ref_1m = _int8(data[1]) - 41  # Eddystone gives the power at 0 m; a metre costs about 41 dB
        info.fields.append(("Eddystone TX power", f"{_int8(data[1])} dBm at 0 m"))
    if frame == 0x00 and len(data) >= 18:
        info.offer(85, "Eddystone beacon", "beacon")
        info.fields += [("Namespace", data[2:12].hex().upper()), ("Instance", data[12:18].hex().upper())]
        info.detail = f"instance {data[12:18].hex().upper()}"
    elif frame == 0x10:
        info.offer(85, "Eddystone beacon", "beacon")
        url = eddystone_url(data)
        if url:
            info.fields.append(("URL", url))
            info.detail = url.split("://", 1)[-1]
    elif frame == 0x20 and len(data) >= 14 and data[1] == 0:
        info.offer(84, "Eddystone beacon", "beacon")
        volts, temp, count, uptime = struct.unpack(">HhII", data[2:14])
        if volts:
            info.fields.append(("Beacon battery", f"{volts / 1000:.2f} V"))
            info.battery = f"{volts / 1000:.2f} V"
        if temp != -0x8000:
            info.fields.append(("Beacon temperature", f"{temp / 256:.1f} °C"))
        hours = uptime / 36000
        info.fields += [("Packets sent", f"{count:,}".replace(",", " ")),
                        ("Beacon uptime", f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f} h")]
    elif frame == 0x30:
        info.offer(84, "Eddystone beacon", "beacon")
        info.fields.append(("Eddystone", "Ephemeral ID (rotating)"))
    elif frame in (0x40, 0x41):
        info.beacon = False
        info.offer(80, "Find My Device tag", "tag")
        info.tracker = True
        info.fields.append(("Google Find My Device", "Tracking tag" + (" (tracking protection)" if frame == 0x41 else "")))


_TRACKERS = {0xFEED: "Tile tracker", 0xFEEC: "Tile tracker", 0xFD84: "Tile tracker", 0xFD5A: "Samsung SmartTag",
             0xFE33: "Chipolo tracker"}
_SERVICE_KINDS = {
    0x180D: ("Heart rate sensor", "heart", 50), 0x1812: ("Keyboard, mouse or controller", "keyboard", 50),
    0x1816: ("Cycling sensor", "sensor", 50), 0x1818: ("Cycling power meter", "sensor", 50),
    0x1814: ("Running sensor", "sensor", 50), 0x1826: ("Fitness machine", "sensor", 50),
    0x1810: ("Blood pressure monitor", "heart", 50), 0x1808: ("Glucose meter", "heart", 50),
    0x1809: ("Thermometer", "sensor", 50), 0x181D: ("Weight scale", "sensor", 50),
    0x1822: ("Pulse oximeter", "heart", 50), 0x181A: ("Environmental sensor", "sensor", 45),
    0x1854: ("Hearing aid", "headphones", 50), 0x184E: ("LE Audio device", "headphones", 45),
    0x1850: ("LE Audio device", "headphones", 45), 0x1853: ("LE Audio device", "headphones", 45),
    0xFD6F: ("Exposure notification", "phone", 40), 0xFE95: ("Xiaomi smart device", "sensor", 35),
    0xFE59: ("Nordic firmware update", "sensor", 20),
}
_NORDIC_UART = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


def _services(uuids: list[str], service_data: dict[str, bytes], info: Info,
              eddystone: dict[int, bytes] | None = None) -> None:
    for uuid in list(uuids) + [u for u in service_data if u not in uuids]:
        short = names.short_uuid(uuid)
        if short in _TRACKERS:
            info.offer(80, _TRACKERS[short], "tag")
            info.tracker = True
        elif short in _SERVICE_KINDS:
            kind, icon, rank = _SERVICE_KINDS[short]
            info.offer(rank, kind, icon)
        elif uuid.lower() == _NORDIC_UART:
            info.offer(20, "Maker board (Nordic UART)", "sensor")
    for uuid, data in service_data.items():
        short = names.short_uuid(uuid)
        if short == 0xFEAA:
            for frame in (eddystone or {data[:1]: data}).values():
                _eddystone(frame, info)
        elif short == 0xFE2C:
            info.offer(60, "Fast Pair accessory", "headphones")
            if len(data) == 3:
                info.fields.append(("Fast Pair model", f"0x{int.from_bytes(data, 'big'):06X}"))


# ---- names ---------------------------------------------------------------------

_NAME_HINTS = [
    (r"airpods|buds|headphone|headset|earbud|beats|\bwh-|\bwf-|jabra|bose|sennheiser|soundcore|\bjbl", "Headphones", "headphones"),
    (r"speaker|soundbar|sonos|homepod|boom", "Speaker", "speaker"),
    (r"iphone|galaxy|pixel|oneplus|xperia|\bphone", "Phone", "phone"),
    (r"macbook|laptop|\bpc\b|desktop|imac|thinkpad|surface|mac mini|mac studio", "Computer", "laptop"),
    (r"watch|\bband\b|fitbit|garmin|amazfit|polar|suunto", "Watch or band", "watch"),
    (r"\btv\b|bravia|roku|chromecast|fire ?tv|apple tv|projector", "TV", "tv"),
    (r"keyboard|mouse|trackpad|controller|gamepad|dualsense|remote", "Input device", "keyboard"),
    (r"\bhr\b|heart|\bhrm|polar h\d", "Heart rate sensor", "heart"),
    (r"sensor|thermo|hygro|scale|bulb|lamp|plug|lock|tag\b", "Smart device", "sensor"),
]


def _name(name: str | None, info: Info) -> None:
    if not name:
        return
    lowered = name.lower()
    for pattern, kind, icon in _NAME_HINTS:
        if re.search(pattern, lowered):
            info.offer(35, kind, icon)  # beats a bare 'Apple device', not anything more specific
            return


def decode(name: str | None, manufacturer_data: dict[int, bytes], service_uuids: list[str],
           service_data: dict[str, bytes], eddystone: dict[int, bytes] | None = None) -> Info:
    """eddystone: the latest frame of each type, when a device sends several."""
    info = Info()
    for cid, data in manufacturer_data.items():
        if cid == APPLE:
            _apple(data, info)
        elif cid == MICROSOFT:
            _microsoft(data, info)
    _services(service_uuids, service_data, info, eddystone)
    if info.battery:
        info.detail = f"{info.detail} · battery {info.battery}" if info.detail else f"battery {info.battery}"
    _name(name, info)
    for cid in manufacturer_data:
        if names.company(cid):
            info.vendor = names.company(cid)
            break
    if not info.vendor:
        info.vendor = next((names.service_owner(u) for u in [*service_uuids, *service_data] if names.service_owner(u)),
                           None)
    return info
