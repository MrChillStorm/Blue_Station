"""The tracking page: one device, everything about it. A big live signal
with its trend (for walking toward something you've lost), rough
distance, statistics, the signal's history, the decoded advertisement,
and what the device says about itself when asked (GATT)."""
import csv
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from blue_station.core import names
from blue_station.core.devices import Device, ago_text, distance_text, duration_text, quality
from blue_station.ui import icons
from blue_station.ui.theme import colors, signal_color
from blue_station.ui.widgets import (
    RssiChart, Segmented, SignalMeter, StatTile, advertisement_rows, card, dbm, esc, kv_html, label, link_button,
    refresh_tool_icons, subtitle, tool_button,
)

WINDOWS = [60, 300, 900]
TREND_STEP = 0.4  # dB per second before a change counts as a trend


def trend_text(slope: float | None) -> tuple[str, str]:
    """('▲ Getting closer', color key)."""
    if slope is None:
        return "Listening…", "faint"
    if slope > TREND_STEP:
        return "▲  Getting closer", "success"
    if slope < -TREND_STEP:
        return "▼  Moving away", "danger"
    return "●  Holding steady", "muted"


class TrackPage(QWidget):
    back = Signal()
    changed = Signal(object)  # Device: nickname, pin or calibration changed
    mineToggled = Signal(object)  # Device: yours, or watched again
    message = Signal(str)

    def __init__(self, read_gatt, parent=None):
        super().__init__(parent)
        self.read_gatt = read_gatt  # address -> concurrent.futures.Future of a GattResult
        self.watch_text = None  # device -> a sentence about it from the tracker watch, or None
        self.mine_state = None  # device -> True (yours), False (watched), None (not watched)
        self.device: Device | None = None
        self._future = None
        self._ad_key = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_top())
        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_gauge())
        left.addLayout(self._build_tiles())
        left.addWidget(self._build_chart(), 1)
        body.addLayout(left, 3)
        body.addWidget(self._build_side(), 2)
        layout.addLayout(body, 1)

    # ---- building ------------------------------------------------------------------

    def _build_top(self):
        frame = card()
        row = QHBoxLayout(frame)
        row.setContentsMargins(10, 10, 14, 10)
        row.setSpacing(10)
        self.back_btn = link_button("‹  All devices", "Back to the list (Esc)")
        self.back_btn.clicked.connect(self.back.emit)
        self.icon = QLabel()
        heads = QVBoxLayout()
        heads.setSpacing(1)
        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title = label("", "big")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.rename = QLineEdit()
        self.rename.setObjectName("rename")
        self.rename.setPlaceholderText("A name for this device")
        self.rename.hide()
        self.rename.returnPressed.connect(self._commit_rename)
        self.rename.editingFinished.connect(self._commit_rename)
        self.pencil = tool_button("pencil", "Give it a name of your own (remembered)", 16)
        self.pencil.clicked.connect(self._start_rename)
        title_row.addWidget(self.title)
        title_row.addWidget(self.rename)
        title_row.addWidget(self.pencil)
        title_row.addStretch(1)
        self.sub = label("", "muted")
        heads.addLayout(title_row)
        heads.addWidget(self.sub)
        self.star = tool_button("star", "Pin: keep it on top of the list and remember it", 20)
        self.star.clicked.connect(self._toggle_pin)
        self.export_btn = tool_button("export", "Export this device's signal log as CSV (⌘E)", 18)
        self.export_btn.clicked.connect(self.export_dialog)
        row.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignTop)
        row.addSpacing(6)
        row.addWidget(self.icon)
        row.addLayout(heads, 1)
        row.addWidget(self.star)
        row.addWidget(self.export_btn)
        return frame

    def _build_gauge(self):
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(20, 14, 20, 16)
        box.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.number = label("—", "huge")
        words = QVBoxLayout()
        words.setSpacing(2)
        self.quality = label("", "heading")
        self.trend = label("", "muted")
        words.addStretch(1)
        words.addWidget(self.quality)
        words.addWidget(self.trend)
        words.addStretch(1)
        far = QVBoxLayout()
        far.setSpacing(0)
        self.distance = label("—", "big")
        self.distance.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.distance_note = label("", "faint")
        self.distance_note.setAlignment(Qt.AlignmentFlag.AlignRight)
        cal_row = QHBoxLayout()
        cal_row.addStretch(1)
        self.calibrate = link_button("Calibrate at 1 m", "Hold the device 1 m from this computer for a few seconds, "
                                                         "then click: its distance is measured against this from then on")
        self.calibrate.clicked.connect(self._calibrate)
        self.uncalibrate = link_button("Reset", "Forget the calibration")
        self.uncalibrate.clicked.connect(self._uncalibrate)
        cal_row.addWidget(self.calibrate)
        cal_row.addWidget(self.uncalibrate)
        far.addStretch(1)
        far.addWidget(self.distance)
        far.addWidget(self.distance_note)
        far.addLayout(cal_row)
        row.addWidget(self.number)
        row.addLayout(words)
        row.addStretch(1)
        row.addLayout(far)
        box.addLayout(row)
        self.meter = SignalMeter()
        box.addWidget(self.meter)
        return frame

    def _build_tiles(self):
        grid = QGridLayout()
        grid.setSpacing(10)
        specs = [
            ("last", "Last packet", "The signal of the very last packet, unsmoothed"),
            ("range", "Range · 1 min", "Weakest and strongest packet in the last minute"),
            ("mean", "Average · 1 min", "The mean signal over the last minute"),
            ("spread", "Jitter · 1 min", "How much the signal wobbles (standard deviation). Under 3 dB is calm."),
            ("packets", "Packets", "Packets heard since the device was first seen"),
            ("rate", "Rate", "Packets heard per second over the last 10 seconds"),
            ("first", "First seen", "When this session first heard it"),
            ("seen", "Last seen", "How long since the last packet"),
        ]
        self.tiles: dict[str, StatTile] = {}
        for i, (key, caption, tip) in enumerate(specs):
            tile = self.tiles[key] = StatTile(caption, tip)
            grid.addWidget(tile, i // 4, i % 4)
        return grid

    def _build_chart(self):
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label("SIGNAL HISTORY", "caps"))
        head.addSpacing(10)
        head.addWidget(label("dots are packets, the line is the smoothed signal", "faint"))
        head.addStretch(1)
        self.windows = Segmented(["1 min", "5 min", "15 min"])
        self.windows.changed.connect(lambda i: self.chart.set_window(WINDOWS[i]))
        head.addWidget(self.windows)
        box.addLayout(head)
        self.chart = RssiChart()
        box.addWidget(self.chart, 1)
        return frame

    def _build_side(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 4, 0)
        box.setSpacing(12)
        self.note = label("", "note")
        self.note.hide()
        self.note.setWordWrap(True)
        box.addWidget(self.note)
        self.mine_btn = link_button("This is mine")
        self.mine_btn.hide()
        self.mine_btn.clicked.connect(lambda: self.device and self.mineToggled.emit(self.device))
        mine_row = QHBoxLayout()
        mine_row.setContentsMargins(0, 0, 0, 0)
        mine_row.addWidget(self.mine_btn)
        mine_row.addStretch(1)
        box.addLayout(mine_row)

        ad = card()
        ad_box = QVBoxLayout(ad)
        ad_box.setContentsMargins(16, 12, 16, 14)
        ad_box.setSpacing(8)
        ad_box.addWidget(label("ADVERTISEMENT", "caps"))
        self.ad = self._rich_label()
        ad_box.addWidget(self.ad)
        self.decoded_head = label("DECODED", "caps")
        ad_box.addSpacing(4)
        ad_box.addWidget(self.decoded_head)
        self.decoded = self._rich_label()
        ad_box.addWidget(self.decoded)
        box.addWidget(ad)

        gatt = card()
        gatt_box = QVBoxLayout(gatt)
        gatt_box.setContentsMargins(16, 12, 16, 14)
        gatt_box.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(label("DEVICE INFO (GATT)", "caps"))
        head.addStretch(1)
        self.read_btn = QPushButton("Connect and read")
        self.read_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.read_btn.setToolTip("Connects briefly and reads the standard information: name, maker, model, "
                                 "firmware, battery. Nothing is written, and it reads only public "
                                 "characteristics, which rarely need pairing.")
        self.read_btn.clicked.connect(self.read)
        head.addWidget(self.read_btn)
        gatt_box.addLayout(head)
        self.gatt_status = label("", "muted")
        self.gatt_status.setWordWrap(True)
        gatt_box.addWidget(self.gatt_status)
        self.gatt = self._rich_label()
        gatt_box.addWidget(self.gatt)
        box.addWidget(gatt)
        box.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    @staticmethod
    def _rich_label() -> QLabel:
        lab = QLabel()
        lab.setTextFormat(Qt.TextFormat.RichText)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lab

    # ---- showing a device -----------------------------------------------------------

    def set_origin(self, job: str) -> None:
        self.back_btn.setText(f"‹  {job}")
        self.back_btn.setToolTip(f"Back to {job} (Esc)")

    def show_device(self, device: Device) -> None:
        if device is not self.device:
            self.device = device
            self._future = None
            self._ad_key = None
            self.gatt.clear()
            self.gatt_status.setText("Connect to read what the device says about itself.")
            self.rename.hide()
            self.title.show()
        self.tick(time.time())

    def refresh_icons(self) -> None:
        refresh_tool_icons(self.pencil, self.export_btn)
        if self.device:
            self._ad_key = None  # the HTML carries colors
            self.tick(time.time())

    def tick(self, now: float) -> None:
        d = self.device
        if d is None:
            return
        c = colors()
        self.icon.setPixmap(icons.pixmap(d.info.icon, c["warning"] if d.info.tracker else c["accent"], 34))
        self.title.setText(d.title)
        self.sub.setText(f"{subtitle(d)}  ·  {d.mac or d.address}")
        self.star.setProperty("icon_name", "star_filled" if d.pinned else "star")
        refresh_tool_icons(self.star, color_key="accent" if d.pinned else "muted")

        gone = d.gone(now)
        color = c["faint"] if gone else signal_color(d.smoothed, c)
        self.number.setText(f"<span style='color:{color}'>{dbm(d.smoothed)}</span>"
                            f"<span style='color:{c['faint']}; font-size:18px'> dBm</span>")
        if gone:
            self.quality.setText("Out of range")
            self.trend.setText(f"<span style='color:{c['faint']}'>Last heard {ago_text(d.age(now))}</span>")
        else:
            self.quality.setText(quality(d.smoothed))
            text, key = trend_text(d.trend(now))
            self.trend.setText(f"<span style='color:{c[key]}'>{text}</span>")
        stats = d.stats(now, 60)
        self.meter.set_values(None if gone else d.smoothed, stats.high if stats else None)
        self.distance.setText(distance_text(d.distance()))
        _, source = d.reference
        self.distance_note.setText(f"rough, measured against {source}")
        self.uncalibrate.setVisible(d.calibration is not None)

        t = self.tiles
        t["last"].set(f"{dbm(d.rssi)} dBm")
        t["range"].set(f"{dbm(stats.low)} to {dbm(stats.high)}" if stats else "—")
        t["mean"].set(f"{dbm(stats.mean)} dBm" if stats else "—")
        t["spread"].set(f"± {stats.spread:.1f} dB" if stats else "—")
        t["packets"].set(f"{d.packets:,}".replace(",", " "))
        t["rate"].set(f"{d.rate(now):.1f} / s")
        t["first"].set(datetime.fromtimestamp(d.first_seen).strftime("%H:%M:%S"))
        t["seen"].set(ago_text(d.age(now)))
        t["first"].setToolTip(f"First heard {duration_text(now - d.first_seen)} ago")
        self.chart.show_device(d, now)

        key = (d._payload, d.mac, d.connectable, d.tx_power)
        if key != self._ad_key:
            self._ad_key = key
            self.ad.setText(kv_html(advertisement_rows(d), c))
            self.decoded.setText(kv_html(d.info.fields, c))
            self.decoded.setVisible(bool(d.info.fields))
            self.decoded_head.setVisible(bool(d.info.fields))
        note = "\n\n".join(filter(None, [d.info.note, self.watch_text(d) if self.watch_text else None]))
        if note != self.note.text():
            self.note.setText(note)
            self.note.setVisible(bool(note))
        mine = self.mine_state(d) if self.mine_state else None
        self.mine_btn.setVisible(mine is not None)
        self.mine_btn.setText("Watch it again" if mine else "This is mine")
        self.mine_btn.setToolTip("You said it's yours. The tracker watch will look at it again." if mine else
                                 "Your own devices go everywhere you go. Marked as yours, the tracker watch "
                                 "leaves it alone.")
        self.read_btn.setEnabled(d.connectable is not False and self._future is None)
        if d.connectable is False and self._future is None and not self.gatt.text():
            self.gatt_status.setText("This device doesn't accept connections: it only broadcasts.")
        self._check_gatt()

    # ---- actions --------------------------------------------------------------------

    def _toggle_pin(self) -> None:
        if self.device:
            self.device.pinned = not self.device.pinned
            self.changed.emit(self.device)
            self.tick(time.time())

    def _start_rename(self) -> None:
        if not self.device:
            return
        self.rename.setText(self.device.nickname or self.device.name or "")
        self.title.hide()
        self.rename.show()
        self.rename.setFocus()
        self.rename.selectAll()

    def _commit_rename(self) -> None:
        if self.rename.isHidden() or not self.device:
            return
        text = self.rename.text().strip()
        self.rename.hide()
        self.title.show()
        nickname = text if text and text != self.device.name else None
        if nickname != self.device.nickname:
            self.device.nickname = nickname
            self.changed.emit(self.device)
            self.message.emit(f"Named it {nickname}." if nickname else "Back to the advertised name.")
        self.tick(time.time())

    def cancel_rename(self) -> bool:
        if not self.rename.isHidden():
            self.rename.hide()
            self.title.show()
            return True
        return False

    def _calibrate(self) -> None:
        d = self.device
        stats = d.stats(time.time(), 5) if d else None
        if not stats or stats.count < 3:
            self.message.emit("Not enough packets in the last 5 seconds to calibrate. Wait a moment and try again.")
            return
        d.calibration = round(stats.mean)
        self.changed.emit(d)
        self.message.emit(f"Calibrated: {dbm(d.calibration)} dBm at 1 m, from {stats.count} packets.")
        self.tick(time.time())

    def _uncalibrate(self) -> None:
        if self.device:
            self.device.calibration = None
            self.changed.emit(self.device)
            self.tick(time.time())

    def read(self) -> None:
        if not self.device or self._future is not None:
            return
        self.gatt.clear()
        self.gatt_status.setText("Connecting…  (up to 20 seconds)")
        self.read_btn.setEnabled(False)
        self._future = self.read_gatt(self.device.address)

    def _check_gatt(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        self._future = None
        self.read_btn.setEnabled(self.device.connectable is not False)
        try:
            result = future.result()
        except Exception as exc:
            text = str(exc) or type(exc).__name__
            self.gatt_status.setText(f"Couldn't read it: {text}")
            return
        c = colors()
        count = sum(len(s.characteristics) for s in result.services)
        self.gatt_status.setText(f"Read at {datetime.now():%H:%M:%S}: {len(result.services)} services, "
                                 f"{count} characteristics.")
        parts = [kv_html(result.summary, c)] if result.summary else []
        for service in result.services:
            parts.append(f"<p style='margin:10px 0 2px 0'><b>{esc(service.name)}</b> "
                         f"<span style='color:{c['faint']}'>{esc(names.pretty_uuid(service.uuid))}</span></p>")
            for char in service.characteristics:
                value = f" &nbsp;<span>{esc(char.value)}</span>" if char.value else ""
                parts.append(f"<div style='margin-left:12px'>{esc(char.name)} <span style='color:{c['faint']}'>"
                             f"{esc(', '.join(char.properties))}</span>{value}</div>")
        self.gatt.setText("".join(parts))

    def export_dialog(self) -> None:
        if not self.device:
            return
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in self.device.title).strip() or "device"
        suggested = str(Path.home() / f"{safe} signal {datetime.now():%Y-%m-%d %H%M}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export signal log", suggested, "CSV (*.csv)")
        if path:
            self.export(Path(path))

    def export(self, path: Path) -> None:
        d = self.device
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "unix_time", "rssi_dbm"])
            for t, rssi in d.history:
                writer.writerow([datetime.fromtimestamp(t).isoformat(timespec="milliseconds"), f"{t:.3f}", rssi])
        self.message.emit(f"Exported {len(d.history)} packets to {path.name}.")
