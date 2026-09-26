"""Connecting to a device. read() is the quick look: connect, read the
standard public characteristics (name, model, firmware, battery...),
which rarely need pairing, and leave. Link is the developer's lasting
connection: every service, a read of any characteristic on request, and
notifications as they arrive. Nothing is ever written."""
import asyncio
import struct
import time
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue

from blue_station.core import names
from blue_station.core.decode import hex_bytes

# characteristic -> the label it gets in the summary
SUMMARY = {
    0x2A00: "Device name", 0x2A01: "Appearance", 0x2A29: "Manufacturer", 0x2A24: "Model",
    0x2A25: "Serial number", 0x2A27: "Hardware", 0x2A26: "Firmware", 0x2A28: "Software",
    0x2A19: "Battery", 0x2A50: "PnP ID", 0x2A07: "TX power", 0x2A23: "System ID",
}


@dataclass
class Characteristic:
    uuid: str
    name: str
    properties: list[str]
    value: str | None = None
    handle: int = 0

    @property
    def readable(self) -> bool:
        return "read" in self.properties

    @property
    def notifies(self) -> bool:
        return "notify" in self.properties or "indicate" in self.properties


@dataclass
class Service:
    uuid: str
    name: str
    characteristics: list[Characteristic] = field(default_factory=list)
    handle: int = 0


@dataclass
class GattResult:
    services: list[Service]
    summary: list[tuple[str, str]]


_BODY_LOCATIONS = ("Other", "Chest", "Wrist", "Finger", "Hand", "Ear lobe", "Foot")


def format_value(short: int | None, raw: bytes) -> str:
    if short == 0x2A19 and raw:
        return f"{raw[0]} %"
    if short == 0x2A37 and len(raw) >= 2:
        bpm = int.from_bytes(raw[1:3], "little") if raw[0] & 1 and len(raw) >= 3 else raw[1]
        return f"{bpm} bpm"
    if short == 0x2A38 and raw:
        return _BODY_LOCATIONS[raw[0]] if raw[0] < len(_BODY_LOCATIONS) else f"Location {raw[0]}"
    if short == 0x2A6E and len(raw) >= 2:
        return f"{int.from_bytes(raw[:2], 'little', signed=True) / 100:.2f} °C"
    if short == 0x2A6F and len(raw) >= 2:
        return f"{int.from_bytes(raw[:2], 'little') / 100:.2f} %"
    if short == 0x2A6D and len(raw) >= 4:
        return f"{int.from_bytes(raw[:4], 'little') / 1000:.1f} hPa"
    if short == 0x2A01 and len(raw) >= 2:
        value = int.from_bytes(raw[:2], "little")
        return f"{names.appearance(value)} (0x{value:04X})"
    if short == 0x2A07 and raw:
        return f"{raw[0] - 256 if raw[0] > 127 else raw[0]} dBm"
    if short == 0x2A50 and len(raw) >= 7:
        source, vendor, product, version = struct.unpack("<BHHH", raw[:7])
        who = names.company(vendor) if source == 1 else None
        who = who or f"{'Bluetooth' if source == 1 else 'USB'} vendor 0x{vendor:04X}"
        return f"{who} · product 0x{product:04X} · version 0x{version:04X}"
    text = raw.rstrip(b"\x00")
    try:
        decoded = text.decode("utf-8")
        if decoded and decoded.isprintable():
            return decoded
    except UnicodeDecodeError:
        pass
    return hex_bytes(raw, 32)


async def read(target, timeout: float = 20) -> GattResult:
    """target: a bleak BLEDevice (or an address). Raises on failure."""
    from bleak import BleakClient

    summary = {}
    async with BleakClient(target, timeout=timeout) as client:
        services, chars = service_list(client)
        for char in (c for s in services for c in s.characteristics):
            short = names.short_uuid(char.uuid)
            if char.readable and short in SUMMARY:
                try:
                    raw = bytes(await asyncio.wait_for(client.read_gatt_char(chars[char.handle]), 6))
                    char.value = format_value(short, raw)
                    summary.setdefault(short, char.value)
                except Exception:  # needs pairing, or the device went away mid-read
                    char.value = "(couldn't read)"
    ordered = [(label, summary[short]) for short, label in SUMMARY.items() if short in summary]
    return GattResult(services, ordered)


# ---- the lasting connection ------------------------------------------------------------

CONNECTING, CONNECTED, CLOSED, FAILED = "connecting", "connected", "closed", "failed"


@dataclass(frozen=True)
class Event:
    """A value read or notified, or a read that failed."""
    handle: int
    t: float
    kind: str  # "read", "notify" or "error"
    text: str
    data: bytes = b""


def service_list(client) -> tuple[list[Service], dict[int, object]]:
    """Our Services from bleak's, and bleak's characteristics by handle."""
    services, chars = [], {}
    for s in client.services:
        service = Service(s.uuid, names.service(s.uuid) or s.description or "Unknown service", handle=s.handle)
        for c in s.characteristics:
            service.characteristics.append(Characteristic(
                c.uuid, names.characteristic(c.uuid) or c.description or "Unknown", list(c.properties),
                handle=c.handle))
            chars[c.handle] = c
        services.append(service)
    return services, chars


def _why(exc: Exception) -> str:
    return "the device didn't answer" if isinstance(exc, asyncio.TimeoutError) else str(exc) or type(exc).__name__


class Link:
    """Its methods are called from the window's thread; the work runs on
    the scanner's asyncio loop, and results come back through drain()."""

    def __init__(self, submit, target):
        self._submit = submit  # coroutine -> concurrent.futures.Future on the bleak loop
        self._target = target  # () -> a BLEDevice or an address, looked up on that loop
        self.state = CONNECTING
        self.error: str | None = None
        self.services: list[Service] = []
        self.subscribed: set[int] = set()
        self._events: SimpleQueue[Event] = SimpleQueue()
        self._client = None
        self._chars: dict[int, object] = {}
        self._uuids: dict[int, str] = {}
        submit(self._connect())

    def drain(self) -> list[Event]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except Empty:
                return out

    def read(self, handle: int) -> None:
        if self.state == CONNECTED:
            self._submit(self._read(handle))

    def notify(self, handle: int, on: bool) -> None:
        if self.state == CONNECTED:
            self._submit(self._notify(handle, on))

    def close(self) -> None:
        if self.state in (CONNECTING, CONNECTED):
            self.state = CLOSED
            self._submit(self._close())

    def _put(self, handle: int, kind: str, raw: bytes | None = None, text: str | None = None) -> None:
        if text is None:
            text = format_value(names.short_uuid(self._uuids.get(handle, "")), raw or b"")
        self._events.put(Event(handle, time.time(), kind, text, raw or b""))

    async def _connect(self) -> None:
        from bleak import BleakClient

        try:
            self._client = BleakClient(self._target(), disconnected_callback=self._lost, timeout=20)
            await self._client.connect()
            self.services, self._chars = service_list(self._client)
            self._uuids = {h: c.uuid for h, c in self._chars.items()}
            if self.state == CONNECTING:
                self.state = CONNECTED
            else:  # closed while connecting
                await self._client.disconnect()
        except Exception as exc:
            if self.state != CLOSED:
                self.state, self.error = FAILED, str(exc) or type(exc).__name__

    def _lost(self, _client) -> None:
        if self.state == CONNECTED:
            self.state, self.error = CLOSED, "The device disconnected."
            self.subscribed.clear()

    async def _read(self, handle: int) -> None:
        try:
            raw = bytes(await asyncio.wait_for(self._client.read_gatt_char(self._chars[handle]), 10))
            self._put(handle, "read", raw)
        except Exception as exc:
            self._put(handle, "error", text=f"Couldn't read: {_why(exc)}")

    async def _notify(self, handle: int, on: bool) -> None:
        try:
            if on:
                await asyncio.wait_for(self._client.start_notify(
                    self._chars[handle], lambda _c, data: self._put(handle, "notify", bytes(data))), 20)
                self.subscribed.add(handle)
            else:
                self.subscribed.discard(handle)
                await self._client.stop_notify(self._chars[handle])
        except Exception as exc:
            self.subscribed.discard(handle)
            self._put(handle, "error", text=f"Couldn't {'start' if on else 'stop'} notifications: {_why(exc)}")

    async def _close(self) -> None:
        try:
            if self._client is not None:
                await self._client.disconnect()
        except Exception:
            pass
        self.subscribed.clear()
