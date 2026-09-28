"""Where you were: positions from your phone or a GPS track, for the
packet log. No Qt.

A Mac has no GPS, and a phone doesn't lend it its own. A free app can
send it, though: OwnTracks (owntracks.org, iPhone and Android) in its
HTTP mode posts each position it takes as JSON to an address you give it,
here http://<this Mac>:PORT/<a word of its own>, over the phone's
Personal Hotspot. PhoneReceiver takes them in: it is the "server"
OwnTracks' welcome asks you to set up. Opened in the phone's browser,
the same address offers a link that sets OwnTracks up for it (setup_link).
A GPS track saved by any logging app (GPX) does the same afterwards.

The phone takes a position only now and then (every SETUP_METRES or
SETUP_SECONDS, whichever comes first, once set up by the link), so a
packet's position is interpolated between the positions
before and after it, as if you'd moved at an even pace in a straight
line. Near enough on foot and along a road; around a corner it cuts the
corner, and setting off after a long stop smears the stop along the
first stretch. How far in time the nearest real position was goes with
it, so a guess can be told from a measurement."""
import base64
import bisect
import html
import json
import math
import queue
import re
import secrets
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

PORT = 8765
SPAN = 10 * 60  # a packet between two positions this close in time is placed on the line between them...
NEAR = 60  # ... otherwise, the nearest position is used if it's this close
MAX_BODY = 256 * 1024  # a post bigger than this is no position (OwnTracks sends a few hundred bytes)
SETUP_METRES, SETUP_SECONDS = 10, 30  # the phone set up by the link: a position this often (its own defaults
# are 100 m and 5 minutes: a corner is cut by a lot, walking)
_WORD = "abcdefghjkmnpqrstuvwxyz23456789"  # nothing to mistake for something else when typing it on a phone


@dataclass(frozen=True, slots=True)
class Fix:
    """A position: when, where, and how sure (metres, when known)."""
    t: float
    lat: float
    lon: float
    acc: float | None = None


class Track:
    """Positions in time order, and where you were at any moment between them."""

    def __init__(self, fixes=()):
        self.fixes: list[Fix] = []
        self._times: list[float] = []
        self.add(fixes)

    def __len__(self) -> int:
        return len(self.fixes)

    def add(self, fixes) -> int:
        """Returns how many were new (one per moment: a second one at the same time is left out)."""
        added = 0
        for f in fixes:
            i = bisect.bisect_left(self._times, f.t)
            if i < len(self._times) and self._times[i] == f.t:
                continue
            self._times.insert(i, f.t)
            self.fixes.insert(i, f)
            added += 1
        return added

    def clear(self) -> None:
        self.fixes.clear()
        self._times.clear()

    def at(self, t: float) -> tuple[float, float, float | None, float] | None:
        """(latitude, longitude, accuracy in metres, seconds to the nearest
        real position) at time t, or None when no position is near enough."""
        i = bisect.bisect_left(self._times, t)
        before = self.fixes[i - 1] if i > 0 else None
        after = self.fixes[i] if i < len(self.fixes) else None
        if after is not None and after.t == t:
            return after.lat, after.lon, after.acc, 0.0
        if before is not None and after is not None and after.t - before.t <= SPAN:
            f = (t - before.t) / (after.t - before.t)
            accs = [a for a in (before.acc, after.acc) if a is not None]
            return (before.lat + f * (after.lat - before.lat), before.lon + f * (after.lon - before.lon),
                    max(accs) if accs else None, min(t - before.t, after.t - t))
        near = min((x for x in (before, after) if x is not None), key=lambda x: abs(x.t - t), default=None)
        if near is not None and abs(near.t - t) <= NEAR:
            return near.lat, near.lon, near.acc, abs(near.t - t)
        return None


def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Metres between two positions (near enough for the distances here)."""
    mid = math.radians((lat1 + lat2) / 2)
    return math.hypot(math.radians(lon2 - lon1) * math.cos(mid), math.radians(lat2 - lat1)) * 6_371_000


def read_gpx(path: Path) -> list[Fix]:
    """The timed track points of a GPX file, in time order."""
    out = []
    for el in ET.parse(path).iter():
        if el.tag.rsplit("}", 1)[-1] != "trkpt":
            continue
        when = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "time"), None)
        if when:
            t = datetime.fromisoformat(when.strip().replace("Z", "+00:00")).timestamp()
            out.append(Fix(t, float(el.get("lat")), float(el.get("lon"))))
    return sorted(out, key=lambda f: f.t)


def owntracks_fixes(body: bytes) -> list[Fix]:
    """The positions in an OwnTracks post: one message, or several queued
    while the Mac couldn't be reached. Anything else in it is left alone."""
    try:
        data = json.loads(body)
    except ValueError:
        return []
    out = []
    for m in data if isinstance(data, list) else [data]:
        if not isinstance(m, dict) or m.get("_type") != "location":
            continue
        try:
            lat, lon, t = float(m["lat"]), float(m["lon"]), float(m["tst"])
            acc = float(m["acc"]) if m.get("acc") is not None else None
        except (KeyError, TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            out.append(Fix(t, lat, lon, acc))
    return out


def setup_link(url: str) -> str:
    """The link that sets OwnTracks up to send its positions to url: HTTP
    mode, Move mode, a position every SETUP_METRES or SETUP_SECONDS.
    OwnTracks asks before it takes it."""
    config = {"_type": "configuration", "mode": 3, "url": url, "auth": False, "tid": "bs", "monitoring": 2,
              "locatorDisplacement": SETUP_METRES, "locatorInterval": SETUP_SECONDS}
    return "owntracks:///config?inline=" + quote(base64.b64encode(json.dumps(config).encode()).decode(), safe="")


def setup_page(url: str) -> bytes:
    """What the phone's browser shows at the address: that it got through, and the link."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blue Station</title>
<style>
body {{ font: 17px -apple-system, system-ui, sans-serif; margin: 28px 22px; max-width: 34em; line-height: 1.45 }}
a.setup {{ display: block; margin: 24px 0; padding: 15px; border-radius: 12px; background: #5b5bd6; color: #fff;
          text-align: center; text-decoration: none; font-weight: 600 }}
.faint {{ color: #777; font-size: 15px }}
</style></head><body>
<h2>Blue Station hears you.</h2>
<p>Blue Station is the server OwnTracks asks for. Tap below to set OwnTracks up on this phone: it then sends its
position here, to {html.escape(url)}, every {SETUP_METRES} m or {SETUP_SECONDS} seconds while it moves.</p>
<a class="setup" href="{html.escape(setup_link(url))}">Set up OwnTracks</a>
<p>If OwnTracks says <i>URI or file configuration not allowed</i>: in OwnTracks, tap ⓘ on the map, then
Settings, then the ⓘ at the end of Remote Control, and turn on <b>Allow external configuration</b>. Come back
and tap the button again. Afterwards you can turn it off again.</p>
<p>In the phone's Settings → OwnTracks → Location, choose <b>Always</b>, or OwnTracks stops sending once the
phone is locked.</p>
<p class="faint">OwnTracks asks before it changes its settings. When it first sends a position, the phone may ask
whether it may connect to devices on your local network: allow it. Sending a position this often uses the
battery, as navigation does.</p>
</body></html>
""".encode()


def new_word() -> str:
    return "".join(secrets.choice(_WORD) for _ in range(6))


def this_mac() -> str | None:
    """This Mac's address on the network it would reach the internet
    through (the phone's hotspot, say). Nothing is sent to find out."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # a documentation address: only picks the way out
            return s.getsockname()[0]
    except OSError:
        return None


class PhoneReceiver:
    """Takes the positions your phone posts (OwnTracks' HTTP mode), on a
    thread of its own, while it's on. Only posts to its own word are
    taken, so a stranger on the same network can't slip one in by chance."""

    def __init__(self, word: str, port: int = PORT):
        self.word, self.port = word, port
        self.heard: float | None = None  # when the phone last posted (this Mac's clock)
        self.latest: Fix | None = None
        self._fixes: queue.SimpleQueue = queue.SimpleQueue()
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> str | None:
        """None when listening, or why it can't."""
        if self._server is not None:
            return None
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if self.path.split("?")[0] != f"/{receiver.word}" or not 0 < length <= MAX_BODY:
                    self.send_error(404)
                    return
                receiver._took(owntracks_fixes(self.rfile.read(length)))
                self._reply(b"[]", "application/json")  # nothing for the phone in return

            def do_GET(self):  # the address opened in the phone's browser: it gets through, and the setup link
                if self.path.split("?")[0] != f"/{receiver.word}":
                    self.send_error(404)
                    return
                host = self.headers.get("Host", "")  # as the phone reached this Mac
                if not re.fullmatch(r"[\w.\-]+(:\d+)?|\[[0-9a-fA-F:.]+\](:\d+)?", host):
                    host = f"{this_mac() or 'localhost'}:{receiver.port}"
                self._reply(setup_page(f"http://{host}/{receiver.word}"), "text/html; charset=utf-8")

            def _reply(self, body: bytes, kind: str):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        try:
            self._server = ThreadingHTTPServer(("", self.port), Handler)
        except OSError as exc:
            return f"can't listen on port {self.port}: {exc.strerror or exc}"
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="phone positions", daemon=True).start()
        return None

    def _took(self, fixes: list[Fix]) -> None:
        self.heard = time.time()
        for f in fixes:
            self._fixes.put(f)
            if self.latest is None or f.t >= self.latest.t:
                self.latest = f

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    def url(self) -> str | None:
        """The address to give the phone, when this Mac is on a network."""
        mac = this_mac()
        return f"http://{mac}:{self.port}/{self.word}" if mac else None

    def drain(self) -> list[Fix]:
        out = []
        while True:
            try:
                out.append(self._fixes.get_nowait())
            except queue.Empty:
                return out
