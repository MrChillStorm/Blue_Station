# Blue Station: how it works

The [README](README.md) tells how to use Blue Station. This is the rest:
how it works inside, what a recording holds, the tools for checking it
against real trips, and the code.

## Running it from the code

```bash
git clone https://github.com/MrChillStorm/Blue_Station.git
cd Blue_Station
pip install -r requirements.txt
python3 -m blue_station
```

Or double-click **Blue Station.app** in the folder. It uses your
`python3`, so install the packages first. The app bundle has to stay in
this folder, because it starts the code next to it. If you downloaded a
ZIP and macOS refuses to open the app, right-click it and choose Open
once. From the bundle, macOS asks about Bluetooth for *Blue Station*;
from a terminal, for the terminal app.

```bash
pip install -e .                     # the `blue-station` command, running this checkout
python3 -m unittest discover tests   # core and interface tests (offscreen, no Bluetooth needed)
python3 packaging/build_icon.py      # rebuild the app icon after editing packaging/icon.svg
python3 tools/drive_check.py packets.csv track.gpx   # the tracker watch against a trip's GPS track
python3 tools/add_track.py packets.csv track.gpx     # where you were, added to a log exported without it
```

Set `BLUE_STATION_SETTINGS=/some/other.json` to use another settings
file; `trackers.json` then goes next to it.

Blue Station is built and used on macOS. bleak also works on Windows and
Linux, and the app is plain Qt, so it should run there too, but only
macOS has been tried.

## The code

- `blue_station/core/` has no Qt:
  - `scanner.py`: bleak on a background thread, and the demo
  - `devices.py`: each device's history, smoothing, statistics, distance, trend and packet gaps
  - `decode.py`: what an advertisement says (kinds, makers, beacons, Apple, Microsoft...)
  - `links.py`: following a device through its address changes, and what it learns from them
  - `watch.py`: trackers, places and landmarks, and each device's timeline
  - `alerts.py`: out of range and back again, for the devices you asked about
  - `baseline.py`: New only's baseline, and which devices are new
  - `survey.py`: survey points and the map's estimate
  - `packets.py`: the packet log
  - `position.py`: where you were, from your phone (OwnTracks) or a GPS track
  - `gatt.py`: the quick read, and the lasting connection with notifications
  - `names.py`: Bluetooth SIG names for companies, services and characteristics
  - `prefs.py`: the settings files
  - `login.py`: starting at login on macOS
- `blue_station/ui/` is the PySide6 interface:
  - `devices.py` (Scan), `trackers.py`, `survey.py`, `develop.py` and `track.py` (a device's own page)
  - `window.py`, `menubar.py`, `widgets.py`, `theme.py` and `icons.py`: everything around them
  - `sound.py`: the beep for finding a device
- `tools/`: `drive_check.py` and `add_track.py`, below.

Each module starts with what it does and why; the numbers it works by
are named at the top, each with the reason for its value.

## The signal number

A device advertises on three radio channels, and at any one spot each
arrives several dB stronger or weaker than the others. macOS reports one
reading per packet, from whichever channel it caught, so the readings
hop about even when nothing moves. An average hops between the channels
too; the strongest keeps to one. So the number shown is the strongest
reading of the last 5 seconds, less how far that peak usually stands
above the readings (learned over about a minute). That brings it back to
the device's usual level. A stronger signal shows as soon as it's heard,
and a weaker one within about 6 seconds. Tuned on two devices that
stayed put for 3 hours: it changes half as often as a 1.5-second average
did, and rises as fast.

## Packet timing

macOS passes on only some of a device's packets: most devices' about
every second or two, some (Find My devices) only every 10 seconds or so,
whatever Blue Station is doing and on battery too. So the real
advertising interval is the shortest gap between heard packets or
shorter, and a Mac can't see intervals much under half a second.

## Following a device through its address changes

Phones, AirPods, watches and other Apple devices change their Bluetooth
address every 15 minutes or so. Signal strength can't tell one device
from another: it says how far away a device is, not which one it is.
What gives a change away is the moment itself: the old address goes
quiet, and within seconds a new one starts from the same spot (about the
same signal) with the same kind of advertisement.

- **Scoring.** Each candidate is scored by how likely its timing and
  signal would be for the same device, against a stranger. A stranger is
  likelier where many devices are heard at that strength, so a match
  among the faint ones at −100 dBm counts for little, and one at −69,
  where few devices are, counts for a lot. Rival candidates share the
  confidence, and a link needs 60 % (70–95 % is usual). Two identical
  devices changing at the same moment are left apart rather than
  guessed.
- **Devices heard rarely.** A device heard only every few seconds, like
  a Find My device, leaves longer gaps at its changes, up to about a
  minute. One heard less often than every 40 seconds can't be timed at
  all: its silence looks like a change, and a change could be anywhere
  between its packets.
- **Steady bytes.** From the changes it's sure of, it learns which bytes
  of each kind of advertisement stay the same through a change: an
  AirPlay target, for one, keeps its network address in it. A device
  with enough such bytes is recognized even after a change nobody heard,
  out of range or while the Mac slept. It uses them tentatively after
  the first sure change, and fully after the second: once, a random byte
  may have stayed the same by chance. Kinds that change every byte, like
  Apple's Nearby Info and Find My, get none. The learned bytes are kept
  in `settings.json`.
- **Partners.** Two kinds of advertisement that change address at the
  same moments, from the same spot, are one device sending both (an
  Apple TV sends AirPlay and Nearby Info). They're listed as one, and one
  recognized after a gap brings its partner along.
- **Twins.** A Mac sends each Nearby Info message from two addresses at
  once, word for word. Apple's messages carry a code that changes all
  the time, so two addresses sending the very same bytes within half a
  minute, on two different occasions, are one device. For your own
  Macs, macOS recognizes one of the two through your Apple Account and
  shows it under one address for good. That one then vouches for the
  other through every change, heard or not: the other's new address is
  the one sending what it sends.
- **Off and on together.** A device switched off and on (its Bluetooth,
  or itself) stops everything it sends within a few seconds, and starts
  it all again together, while everything else carries on. That's how a
  Mac's own Find My advertisement, which changes address on its own
  schedule and sends nothing like its others, joins them. Three
  recordings at home, 12 hours of them overnight, showed 14 such shared
  cycles, every one a single device's.
- **Taking back.** A link can still be wrong, and one kind of wrong
  shows: a device never goes back to an address it has left, so an old
  address heard again never changed. Its link is taken back: the old
  address gets back its row, name, history and tracker record, the new
  one starts on its own (and may be linked rightly later), and what the
  link taught about steady bytes is worked out again without it. In
  recordings on the move, about one link in ten was found wrong this
  way; at home, one in forty. Examples: a Mac's steady address taken to
  have changed to its twin's new one after a sleep, and a TV left behind
  taken for a stranger's phone.

A device's card lists everything it sends, and how its advertisements
are known to be one device. A change missed out of earshot, silent for
over a minute around it, or at the same moment as a lookalike's, makes a
phone show up as a new device.

### The answer key

With **Read names** on, the packet log asks each device it records, once
per address, what it says about itself over a connection: maker, model,
serial number and versions (public strings, nothing that needs pairing,
nothing written). Blue Station's own guess never sees the answers, so
they can check it: two addresses that told the same are most likely one
device, two that told different things are two. Apple devices tell just
their maker and model, so two of the same model look alike. After a
connection, macOS also passes on the device's name to the scan, though
from Apple devices it's only *Mac* or *iPhone*. Each read connects for a
few seconds, and some devices go quiet meanwhile. With Nearby only on,
only the devices within its range are asked.

On two home recordings with such an answer key, all 65 heard address
changes of a Mac and all 33 of an iPhone were linked right, and none
wrong.

## The tracker watch

A tracker's verdict comes from how long it has been with you, and where:

- **Following you**: with you for 10 minutes, in two different places.
  Seconds either side of a move don't count, and a silence of over 5
  minutes isn't time with you. A landmark of any of your places (below)
  is never following: it stays put, which is what made it one. On a dog
  walk, a place was made at a spot within earshot of home, and five of
  home's landmarks, heard faintly from there, had gained a second place.
- **With you on the move**: by your phone's position, with you for 15
  minutes while you moved, over at least 1 km, in one stretch (a
  silence of over 5 minutes starts one over). On a 33 km drive and two
  walks with GPS tracks, a stranger's device stayed with you 7 minutes
  at most (4.1 km, beside you in traffic), and your phone all the way.
  On a bus or a train everyone's does, which is why it's less than
  following and sends no notification. The longest stretch is kept, so
  it stays until forgotten.
- **Staying near you**: 20 minutes in one place, like a neighbour's tag.
- **Passing by**: anything less.

### Places without GPS

A laptop has no GPS, so places are recognized by their **landmarks**:
named devices that stay put (TVs, printers, speakers, a fridge), never
the kinds people carry (phones, watches, headphones, laptops). In
traffic or a car park, other people's phones stay near you for minutes,
and would make a new place every few minutes.

- **A landmark** has been heard for 3 minutes without a silence of 90
  seconds. First heard that long ago isn't enough: coming home, the
  street's devices were first heard on the way out.
- **A place it knows** is recognized when at least half the landmarks
  around you are its own.
- **Somewhere new** becomes a place once two of its landmarks have been
  heard for 5 minutes: you've stayed. In the recordings, walking past,
  two landmarks stayed in earshot together for 3.6 minutes at most (on
  a slow dog walk), and driving past, for under 2.
- **A move** needs most landmarks around you (60 %) to be new to the
  place, and the place's own landmarks gone: at most a quarter of those
  heard there in its last 10 minutes still heard in the last 5, on two
  checks in a row. New landmarks turning up while the place's own are
  still heard are more of the same place, not a move. They join it once
  heard alongside it for 3 minutes (so coming home, the café's lingering
  landmarks don't become part of home).
- **Companions.** A named device heard at the new place that was a
  landmark of the last one came along (a speaker you took with you), so
  it stops being a landmark. It's still watched like any other device.
- **Between places**, nothing is credited to a place, so a stranger's
  tag at the café isn't blamed on your home before the café is
  recognized. **I've moved** says so straight away; the place it starts
  learns its landmarks as they turn up, or turns out to be one already
  known.

### With your phone's position

With **Coordinates** on, the phone's position (below) says where you
are, and the landmarks are only learned:

- **Moving** (not within 50 m of your latest position, or its accuracy
  if worse, for 2 minutes): you're between places, however long the
  houses you pass stay in earshot. A dog walk made three places out of
  spots where two landmarks stayed in earshot for 5.5 to 7.8 minutes.
- **Stopped for 2 minutes within 200 m of a place's centre**: you're
  there. A spot 150 m from home is home, not a new place whose nearby
  landmarks (home's own) would then gain a second place.
- **Stopped near a place the landmarks recognize**, one without a
  centre yet (known from before): you're there, and it learns where it
  is.
- **Stopped for 5 minutes anywhere else**: a new place, even with too
  few named devices around to make one (a stop on a drive had one).
- **Came along**: a landmark of the last place, heard at a place over
  500 m from it, came with you. No house was heard over more than 320 m
  of street.
- **The phone quiet for 2 minutes**: the landmarks decide again, from
  where your phone last had you.

A place's centre is kept in memory only: `trackers.json` still holds
nothing about where your places are.

It remembers trackers for 48 hours (AirTags away from their owner keep
their address for a day), other kinds that only passed by for 3 hours,
places for 60 days, and places you've named for good. A device that
sends several advertisements is watched once, as what the Scan page
lists it as: a Mac's Nearby Info is a computer's, and an Apple TV's is a
TV's, which stays put and isn't watched at all.

## Your phone's position

A Mac has no GPS, and a phone doesn't lend it its own. OwnTracks
(owntracks.org, free, iPhone and Android) in its HTTP mode posts each
position it takes as JSON (`_type` location, `lat`, `lon`, `tst`, `acc`)
to the address you give it. With **Coordinates** on, Blue Station
listens at `http://<this Mac>:8765/<word>`: the word is made once and
kept in `settings.json`, and a post to any other address is refused.
The reply is `[]`, which is all OwnTracks expects. Opened in the phone's
browser, the address answers *Blue Station hears you*, with a **Set up
OwnTracks** link: `owntracks:///config?inline=` and the settings in
base64 (HTTP mode, that address, Move mode, a position every 10 m or 30
seconds instead of OwnTracks' 100 m or 5 minutes). OwnTracks asks before
it takes them, and on the iPhone only takes them at all with **Allow
external configuration** on (off by default), which the page explains. Positions OwnTracks queued while it couldn't reach the
Mac arrive later with their own times.

The phone takes a position only now and then (every 10 m or 30 seconds,
whichever comes first, as the link sets it up), so a packet's
position is interpolated: between two positions at most 10 minutes
apart, on the straight line between them, as if you'd moved at an even
pace; otherwise the nearest position within a minute; otherwise none.
Near enough on foot and along a road; around a corner it cuts the
corner, and setting off after a long stop smears the stop along the
first stretch. **Add track…** does the same from a GPX track, whose
points are usually a second or two apart.

## What a recording holds

The packet log keeps the last 350 000 packets (about 45 MB, 12 hours of
a home's packets), sharing the parts that repeat. While it records, the
Mac doesn't go to sleep on its own (macOS's idle-sleep assertion), on
battery too; closing the lid still puts it to sleep. Its CSV export has
one row per packet:

| Column | What |
|---|---|
| `time`, `unix_time` | when it was heard (local time, and seconds since 1970) |
| `address` | macOS's address for the device (a UUID the same only on this Mac) |
| `device` | what Blue Station listed it as at export |
| `rssi_dbm` | the packet's signal (127: macOS had no reading) |
| `name`, `tx_power_dbm`, `connectable` | as advertised (the name may come from macOS) |
| `manufacturer_data`, `service_uuids`, `service_data` | the advertisement, in hex |
| `identity` | what the address told about itself, with Read names |
| `changed_from`, `change_sure` | Blue Station's guess: the address it changed from, and how sure |
| `latitude`, `longitude` | where you were, with Coordinates or a track |
| `position_accuracy_m`, `position_gap_s` | how sure the position is, and how many seconds away the nearest real one was |

An export reads back as heard (`packets.read`), for replaying through
the app's own code.

A recording holds the real names of the devices around you, your
neighbours' too, and with coordinates, where each was heard. Keep them
out of anything you share.

## Checking the tracker watch on a trip

Record the trip, a drive or a walk, in Develop (**Record**, **All
devices**) with **Coordinates** on, or while a GPS logging app on your
phone saves a GPX track. Then run:

```bash
python3 tools/drive_check.py "Packets 2026-09-28 1400.csv"            # recorded with Coordinates
python3 tools/drive_check.py "Packets 2026-09-28 1400.csv" track.gpx  # or with a separate track
```

It replays the packets through the app's own code, starting from the
places your watch already knows, and tells stop by stop whether the
watch recognized where you were, how soon, whether it kept a place while
you drove, whether the same place got the same answer each time, and
what it thought was following you. A stop is where you stood still for 2
minutes: walking up and down a street isn't one. The track is boiled
down to when you stopped and which stops were the same place: nothing
the tool prints holds a coordinate, and `--save-key key.json` keeps just
that, so the GPX can be deleted. `--watch all` watches every kind of
device, and `--fresh` starts knowing no places. `--phone` gives the
watch the track as your phone's position, as OwnTracks would send it
(every 10 m or 30 seconds), to compare with the landmarks alone.

`tools/add_track.py packets.csv track.gpx` adds where you were to a log
exported without it, from a GPX track of the same time, and writes
*packets with track.csv* beside it.
