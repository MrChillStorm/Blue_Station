"""The radio side. Scanner runs bleak on its own asyncio loop in a
background thread and queues every advertisement; the window drains the
queue on its own timer, so a burst of packets never floods the interface.
DemoScanner makes up a room full of devices for trying the app without
Bluetooth (blue-station --demo)."""
import asyncio
import math
import random
import sys
import threading
import time
from concurrent.futures import Future
from queue import Empty, SimpleQueue

from blue_station.core import gatt
from blue_station.core.devices import Sighting
from blue_station.core.names import short_uuid

IDLE, STARTING, SCANNING, ERROR = "idle", "starting", "scanning", "error"


def _friendly(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if "turned off" in lowered or "powered off" in lowered:
        return "Bluetooth is turned off. Turn it on, then press Scan."
    if "not authorized" in lowered or "denied" in lowered or "unauthorized" in lowered:
        return ("Blue Station isn't allowed to use Bluetooth. Allow it in System Settings → Privacy & Security → "
                "Bluetooth (for the app, or for Terminal if you started it there), then press Scan.")
    if "no bluetooth" in lowered or "unsupported" in lowered or "adapter" in lowered:
        return "No Bluetooth adapter was found."
    return text


class Scanner:
    def __init__(self):
        self.state = IDLE
        self.error: str | None = None
        self._queue: SimpleQueue[Sighting] = SimpleQueue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock: asyncio.Lock | None = None
        self._scanner = None
        self._want = False  # what the window asked for last; the loop thread catches up to it
        self._running = False
        self._ble: dict = {}  # address -> BLEDevice, for connecting later (loop thread only)
        self._macs: dict[str, str | None] = {}

    # ---- the interface the window uses -----------------------------------------------

    def start(self) -> None:
        self._want = True
        if self.state != SCANNING:
            self.state, self.error = STARTING, None
        self._submit(self._sync())

    def stop(self) -> None:
        self._want = False
        self.state = IDLE
        self._submit(self._sync())

    def drain(self) -> list[Sighting]:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except Empty:
                return out

    def read_gatt(self, address: str) -> Future:
        async def run():
            return await gatt.read(self._ble.get(address, address))
        return self._submit(run())

    def connect(self, address: str) -> gatt.Link:
        return gatt.Link(self._submit, lambda: self._ble.get(address, address))

    def close(self) -> None:
        if self._loop is None:
            return
        self._want = False
        try:
            self._submit(self._sync()).result(timeout=3)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)
        if not self._thread.is_alive():
            self._loop.close()
        self._loop = self._thread = self._scanner = None

    # ---- the loop thread -----------------------------------------------------------

    def _submit(self, coro) -> Future:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, name="bleak", daemon=True)
            self._thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _sync(self) -> None:
        """Starts or stops the scan to match the latest wish. Runs one at a
        time, and each run ends with the state that wish implies, so rapid
        Scan/Pause presses always settle right."""
        from bleak import BleakScanner

        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            try:
                if self._want and not self._running:
                    if self._scanner is None:
                        # made here: on macOS its CoreBluetooth manager binds to the running loop
                        self._scanner = BleakScanner(detection_callback=self._detected)
                    await self._scanner.start()
                    self._running = True
                elif not self._want and self._running:
                    self._running = False
                    await self._scanner.stop()
            except Exception as exc:
                self._running = False
                if self._want:
                    self.state, self.error = ERROR, _friendly(exc)
                    return
            self.state = SCANNING if self._want else IDLE

    def _mac(self, device) -> str | None:
        """macOS hides Bluetooth addresses behind per-Mac UUIDs; an
        undocumented call (the one bleak's use_bdaddr option uses) still
        reveals them. Asked once per device."""
        if sys.platform != "darwin":
            return None
        if device.address not in self._macs:
            try:
                peripheral, manager = device.details
                raw = manager.central_manager.retrieveAddressForPeripheral_(peripheral)
                self._macs[device.address] = bytes(raw).hex(":").upper() if raw else None
            except Exception:
                self._macs[device.address] = None
        return self._macs[device.address]

    def _detected(self, device, adv) -> None:
        self._ble[device.address] = device
        connectable = None
        try:
            if sys.platform == "darwin":
                flag = adv.platform_data[1].get("kCBAdvDataIsConnectable")
                connectable = None if flag is None else bool(flag)
        except Exception:
            pass
        self._queue.put(Sighting(
            address=device.address, rssi=adv.rssi, t=time.time(), name=adv.local_name or device.name,
            tx_power=adv.tx_power, manufacturer_data=dict(adv.manufacturer_data),
            service_uuids=tuple(adv.service_uuids), service_data=dict(adv.service_data),
            connectable=connectable, mac=self._mac(device),
        ))


# ---- demo --------------------------------------------------------------------------

def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# name, base RSSI, packets/s, payload, (appears, leaves) in seconds since start
_DEMO = [
    ("Fitness Band", -58, 4, dict(service_uuids=(_uuid16(0x180D), _uuid16(0x180F)),
                                  manufacturer_data={0x0059: bytes.fromhex("0201a4")}, connectable=True, tx_power=4),
     (0, None)),
    (None, -46, 6, dict(manufacturer_data={0x004C: bytes.fromhex("071901142055a8160001003c7c8b6a3e21d4c09f91b2d3a14e5f60")},
                        connectable=True), (0, None)),
    (None, -71, 3, dict(manufacturer_data={0x004C: bytes.fromhex("0215" "3b8f1c24a7e54d0b9c3f5e6a7b8c9d0e"
                                                                 "0001002ac5")}), (0, None)),
    (None, -77, 2, dict(service_uuids=(_uuid16(0xFEAA),),
                        service_data={_uuid16(0xFEAA): bytes([0x10, 0xEE, 0x03]) + b"example" + bytes([0x07])}),
     (0, None)),
    (None, -83, 1, dict(manufacturer_data={0x004C: bytes.fromhex("121910" + "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
                                                                 "e5f6" + "0100")}), (0, None)),
    ("Living Room TV", -73, 3, dict(connectable=True, tx_power=8), (0, None)),
    (None, -66, 2, dict(manufacturer_data={0x0006: bytes.fromhex("012f2002" + "a1b2c3d4" + "00" * 16)}), (0, None)),
    ("Desk Keyboard", -52, 5, dict(service_uuids=(_uuid16(0x1812),), connectable=True), (0, None)),
    ("Bike Sensor", -87, 2, dict(service_uuids=(_uuid16(0x1816),), connectable=True), (12, None)),
    (None, -63, 4, dict(manufacturer_data={0x004C: bytes.fromhex("100507180b3a2c")}), (0, None)),
    ("Kitchen Speaker", -69, 2, dict(connectable=True, service_uuids=(_uuid16(0xFE2C),),
                                     service_data={_uuid16(0xFE2C): bytes.fromhex("0a1b2c")}), (0, None)),
    ("Thermo Sensor 3A", -80, 1, dict(service_uuids=(_uuid16(0x181A),),
                                      manufacturer_data={0x0059: bytes.fromhex("0bd4")}), (0, None)),
    (None, -90, 1, dict(service_uuids=(_uuid16(0xFEED),)), (0, 45)),
    # an iPad that changes its Bluetooth address after 100 seconds, like Apple devices do every 15 minutes
    (None, -67, 3, dict(manufacturer_data={0x004C: bytes.fromhex("1006" + "3b1c2d3e4f5a")}, connectable=True,
                        tx_power=12), (0, 100)),
    (None, -67, 3, dict(manufacturer_data={0x004C: bytes.fromhex("1006" + "3b1c2d3e4f5a")}, connectable=True,
                        tx_power=12), (103, None)),
]
_SAME_DEVICE = {14: 13}  # a later address -> the earlier one of the same device: its signal carries on


class DemoScanner:
    def __init__(self, seed: int = 7):
        self.state = IDLE
        self.error: str | None = None
        self._rng = random.Random(seed)
        self._t0 = time.time()
        self._last = None
        self._walk = [0.0] * len(_DEMO)
        self._addresses = [f"{self._rng.getrandbits(128):032X}" for _ in _DEMO]
        self._addresses = [f"{a[:8]}-{a[8:12]}-{a[12:16]}-{a[16:20]}-{a[20:]}" for a in self._addresses]

    def start(self) -> None:
        self.state, self._last = SCANNING, time.time()

    def stop(self) -> None:
        self.state = IDLE

    def close(self) -> None:
        self.state = IDLE

    def drain(self, now: float | None = None) -> list[Sighting]:
        now = time.time() if now is None else now
        if self.state != SCANNING or self._last is None:
            return []
        out, since = [], now - self._last
        self._last = now
        for i, (name, base, rate, payload, (appears, leaves)) in enumerate(_DEMO):
            age = now - self._t0
            if age < appears or (leaves is not None and age > leaves):
                continue
            if i in _SAME_DEVICE and self._walk[i] == 0.0:
                self._walk[i] = self._walk[_SAME_DEVICE[i]]
            expected = rate * since
            count = int(expected) + (self._rng.random() < expected % 1)
            for k in range(count):
                self._walk[i] = max(-12.0, min(12.0, self._walk[i] + self._rng.gauss(0, 0.8)))
                drift = 6 * math.sin(age / 9 + i) if i == 0 else 0  # the band wanders about the room
                rssi = round(base + self._walk[i] + drift + self._rng.gauss(0, 2.5))
                out.append(Sighting(address=self._addresses[i], rssi=rssi, t=now - since * (count - k - 1) / count,
                                    name=name, **payload))
        return out

    def watch_history(self, now: float) -> dict:
        """A made-up past for the tracker watch: the demo's Find My tag was
        already with you at home this morning, and on the way here."""
        named = [a for a, spec in zip(self._addresses, _DEMO) if spec[0]]
        return {
            "places": [{"id": 1, "first_seen": now - 4 * 3600, "last_seen": now - 2 * 3600, "name": "Home",
                        "landmarks": ["DEMO-HOME-TV", "DEMO-HOME-PRINTER", "DEMO-HOME-SPEAKER"]},
                       {"id": 2, "first_seen": now - 40 * 60, "last_seen": now, "landmarks": named, "manual": True}],
            "trackers": [{"address": self._addresses[4], "kind": "Find My device", "first_seen": now - 3.5 * 3600,
                          "last_seen": now - 2.62 * 3600, "seen_seconds": 52 * 60, "places": [1],
                          "visits": [[1, now - 3.5 * 3600, now - 2.75 * 3600],  # at home, then on the way
                                     [None, now - 2.75 * 3600, now - 2.62 * 3600]]}],
            "current": 2,
        }

    def connect(self, address: str) -> "DemoLink":
        index = self._addresses.index(address) if address in self._addresses else -1
        return DemoLink(_DEMO[index][0] if index >= 0 else None,
                        index >= 0 and bool(_DEMO[index][3].get("connectable")))

    def read_gatt(self, address: str) -> Future:
        future: Future = Future()
        index = self._addresses.index(address) if address in self._addresses else -1
        name = _DEMO[index][0] if index >= 0 else None

        def finish():
            if index < 0 or not _DEMO[index][3].get("connectable"):
                future.set_exception(TimeoutError("The device didn't answer. It may not accept connections."))
                return
            info = gatt.Service(_uuid16(0x180A), "Device Information", [
                gatt.Characteristic(_uuid16(0x2A29), "Manufacturer Name String", ["read"], "Example Devices"),
                gatt.Characteristic(_uuid16(0x2A24), "Model Number String", ["read"], "EX-100"),
                gatt.Characteristic(_uuid16(0x2A26), "Firmware Revision String", ["read"], "2.4.1"),
            ])
            battery = gatt.Service(_uuid16(0x180F), "Battery", [
                gatt.Characteristic(_uuid16(0x2A19), "Battery Level", ["read", "notify"], "76 %")])
            access = gatt.Service(_uuid16(0x1800), "Generic Access", [
                gatt.Characteristic(_uuid16(0x2A00), "Device Name", ["read"], name or "Demo device"),
                gatt.Characteristic(_uuid16(0x2A01), "Appearance", ["read"], "Watch (0x00C0)")])
            services = [access, info, battery]
            summary = [("Device name", name or "Demo device"), ("Appearance", "Watch (0x00C0)"),
                       ("Manufacturer", "Example Devices"), ("Model", "EX-100"), ("Firmware", "2.4.1"),
                       ("Battery", "76 %")]
            future.set_result(gatt.GattResult(services, summary))

        timer = threading.Timer(1.2, finish)
        timer.daemon = True
        timer.start()
        return future


class DemoLink:
    """A made-up connection: a heart rate strap with a battery and a
    vendor service whose counter ticks five times a second."""
    CUSTOM = "7e5a0001-8c3b-4d6e-9f10-2a3b4c5d6e7f"

    def __init__(self, name: str | None, connectable: bool):
        self.state = gatt.CONNECTING
        self.error: str | None = None
        self.services: list[gatt.Service] = []
        self.subscribed: set[int] = set()
        self._name, self._ok = name or "Demo device", connectable
        self._t0 = time.time()
        self._queue: list[gatt.Event] = []
        self._last: dict[int, float] = {}
        self._bpm, self._counter = 72.0, 0
        self._rng = random.Random(3)
        c = gatt.Characteristic
        self._values = {3: self._name.encode(), 5: (0x00C1).to_bytes(2, "little"), 8: b"Example Devices",
                        10: b"EX-100", 12: b"2.4.1", 18: b"\x01", 21: b"\x4c", 25: bytes(6)}
        self._all = [
            gatt.Service(_uuid16(0x1800), "Generic Access", [
                c(_uuid16(0x2A00), "Device Name", ["read"], handle=3),
                c(_uuid16(0x2A01), "Appearance", ["read"], handle=5)], handle=1),
            gatt.Service(_uuid16(0x180A), "Device Information", [
                c(_uuid16(0x2A29), "Manufacturer Name String", ["read"], handle=8),
                c(_uuid16(0x2A24), "Model Number String", ["read"], handle=10),
                c(_uuid16(0x2A26), "Firmware Revision String", ["read"], handle=12)], handle=6),
            gatt.Service(_uuid16(0x180D), "Heart Rate", [
                c(_uuid16(0x2A37), "Heart Rate Measurement", ["notify"], handle=15),
                c(_uuid16(0x2A38), "Body Sensor Location", ["read"], handle=18)], handle=13),
            gatt.Service(_uuid16(0x180F), "Battery", [
                c(_uuid16(0x2A19), "Battery Level", ["read", "notify"], handle=21)], handle=19),
            gatt.Service(self.CUSTOM, "Unknown service", [
                c("7e5a0002-8c3b-4d6e-9f10-2a3b4c5d6e7f", "Unknown", ["read", "notify"], handle=25)], handle=23),
        ]
        self._uuids = {ch.handle: ch.uuid for s in self._all for ch in s.characteristics}

    def _advance(self, now: float) -> None:
        if self.state == gatt.CONNECTING and now - self._t0 >= 0.8:
            if self._ok:
                self.state, self.services = gatt.CONNECTED, self._all
            else:
                self.state, self.error = gatt.FAILED, "The device didn't answer. It may not accept connections."

    def _event(self, handle: int, kind: str, raw: bytes, t: float) -> gatt.Event:
        return gatt.Event(handle, t, kind, gatt.format_value(short_uuid(self._uuids[handle]), raw), raw)

    def drain(self) -> list[gatt.Event]:
        now = time.time()
        self._advance(now)
        out, self._queue = self._queue, []
        periods = {15: 1.0, 21: 10.0, 25: 0.2}
        for handle in sorted(self.subscribed):
            last = self._last.get(handle, now)
            while now - last >= periods[handle]:
                last += periods[handle]
                if handle == 15:
                    self._bpm = min(95.0, max(58.0, self._bpm + self._rng.gauss(0, 1.5)))
                    raw = bytes([0x00, round(self._bpm)])
                elif handle == 25:
                    self._counter += 1
                    raw = self._counter.to_bytes(4, "little") + bytes([self._rng.randrange(256), 0x5A])
                else:
                    raw = self._values[21]
                out.append(self._event(handle, "notify", raw, last))
            self._last[handle] = last
        return out

    def read(self, handle: int) -> None:
        self._advance(time.time())
        if self.state == gatt.CONNECTED:
            raw = self._counter.to_bytes(4, "little") + bytes(2) if handle == 25 else self._values[handle]
            self._queue.append(self._event(handle, "read", raw, time.time()))

    def notify(self, handle: int, on: bool) -> None:
        self._advance(time.time())
        if self.state == gatt.CONNECTED:
            if on:
                self.subscribed.add(handle)
                self._last[handle] = time.time()
            else:
                self.subscribed.discard(handle)

    def close(self) -> None:
        self.state = gatt.CLOSED
        self.subscribed.clear()
