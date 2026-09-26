"""The Develop job: one device, the way its firmware's author sees it.
Packet timing, what changed in the payload, a packet log that records
only when asked, and a GATT explorer that reads any characteristic and
follows notifications live."""
import math
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSizePolicy, QStackedLayout, QTableView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from blue_station.core import gatt, names
from blue_station.core.decode import short_company
from blue_station.core.devices import Device, DeviceStore, gap_stats, payload_key
from blue_station.core.packets import LIMIT, PacketLog, payload_text
from blue_station.ui import icons
from blue_station.ui.devices import DeviceModel, DeviceTable, HoverCards
from blue_station.ui.theme import colors
from blue_station.ui.widgets import StatTile, card, esc, label, link_button, subtitle, tool_button, refresh_tool_icons

SHOWN = 300  # packets the log table shows; the rest wait in the log for export
GAP_BINS = 30  # 10 ms to 10 s, ten bins a decade
QUIET = 10  # followed this long with nothing sent: say so


def mono():
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(11)
    return font


def ms(value: float) -> str:
    return f"{value:.0f} ms" if value < 1000 else f"{value / 1000:.2f} s"


def diff_html(new: bytes, old: bytes | None, c: dict) -> str:
    """Hex bytes, the ones that changed since the last different value highlighted."""
    out = []
    for i, b in enumerate(new):
        text = f"{b:02X}"
        if old is not None and len(old) == len(new) and old[i] != b:
            text = f"<b style='color:{c['accent']}'>{text}</b>"
        out.append(text)
    return " ".join(out) or "(empty)"


class GapHistogram(QWidget):
    """How long the gaps between heard packets are, on a log scale."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.gaps: list[float] = []
        self.setMinimumHeight(96)

    def set_gaps(self, gaps: list[float]) -> None:
        self.gaps = gaps
        self.update()

    def paintEvent(self, _event) -> None:
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = QRectF(0, 4, self.width(), self.height() - 22)
        counts = [0] * GAP_BINS
        for g in self.gaps:
            if g > 0:
                counts[min(GAP_BINS - 1, max(0, int((math.log10(g) - 1) * 10)))] += 1
        top = max(counts) or 1
        width = plot.width() / GAP_BINS
        p.setPen(Qt.PenStyle.NoPen)
        for i, n in enumerate(counts):
            bar = QRectF(plot.left() + i * width + 1, plot.bottom(), width - 2, 0)
            p.setBrush(QColor(c["track"]))
            p.drawRoundedRect(QRectF(bar.left(), plot.bottom() - 2, bar.width(), 2), 1, 1)
            if n:
                h = max(3.0, plot.height() * n / top)
                p.setBrush(QColor(c["accent"]))
                p.drawRoundedRect(QRectF(bar.left(), plot.bottom() - h, bar.width(), h), 2, 2)
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        p.setPen(QColor(c["faint"]))
        for i, text in enumerate(("10 ms", "100 ms", "1 s", "10 s")):
            x = plot.left() + plot.width() * i / 3
            flags = Qt.AlignmentFlag.AlignLeft if i == 0 else (Qt.AlignmentFlag.AlignRight if i == 3
                                                                else Qt.AlignmentFlag.AlignHCenter)
            p.drawText(QRectF(x - (0 if i == 0 else 60 if i == 3 else 30), plot.bottom() + 4, 60, 14), flags, text)
        if not self.gaps:
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Not enough packets yet")
        p.end()


class PacketModel(QAbstractTableModel):
    HEADERS = ["TIME", "DEVICE", "RSSI", "PAYLOAD"]

    def __init__(self, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.rows = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        s = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return datetime.fromtimestamp(s.t).strftime("%H:%M:%S.%f")[:-3]
            if col == 1:
                device = self.store.devices.get(s.address)
                return device.title if device else s.address
            if col == 2:
                return str(s.rssi)
            return payload_text(s) or (f"name: {s.name}" if s.name else "")
        if role == Qt.ItemDataRole.FontRole and col in (0, 3):
            return mono()
        if role == Qt.ItemDataRole.ForegroundRole and col == 1:
            return QColor(colors()["muted"])
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def show(self, rows) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class DevelopPage(QWidget):
    track = Signal(object)  # Device
    message = Signal(str)

    def __init__(self, store: DeviceStore, connect, log: PacketLog, parent=None):
        super().__init__(parent)
        self.store = store
        self.connect_to = connect  # address -> a gatt.Link (or the demo's)
        self.log = log
        self.device: Device | None = None
        self.link = None
        self._items: dict[int, QTreeWidgetItem] = {}
        self._buttons: list[QPushButton] = []  # every Read and Follow: only while connected
        self._follows: dict[int, QPushButton] = {}
        self._pending: dict[int, bool] = {}  # handle -> following asked on or off, not yet answered
        self._waiting: dict[int, float] = {}  # handle -> when following started, until its first value
        self._link_state = None
        self._payload_key = None
        self._shown_total = -1
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_picker())
        self.work = QStackedLayout()
        self.pick_hint = label("Pick a device on the left to see its packets, their timing and its GATT services.",
                               "empty")
        self.pick_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pick_hint.setWordWrap(True)
        self.work.addWidget(self.pick_hint)
        self.work.addWidget(self._build_workspace())
        holder = QWidget()
        holder.setLayout(self.work)
        layout.addWidget(holder, 1)

    # ---- building ------------------------------------------------------------------

    def _build_picker(self):
        frame = card()
        frame.setFixedWidth(340)
        box = QVBoxLayout(frame)
        box.setContentsMargins(1, 12, 1, 1)
        box.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter devices")
        self.search.setToolTip("Matches names, kinds, makers and addresses. A minus leaves out what matches: "
                               "-apple -tv")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(12, 0, 12, 0)
        search_row.addWidget(self.search)
        box.addLayout(search_row)
        self.model = DeviceModel(self.store, self)
        self.table = DeviceTable(self.model, picker=True)
        self.table.picked.connect(self.pick)
        self.table.opened.connect(self.track.emit)
        self.hover = HoverCards(self.table, "Click to develop with it  ·  double-click to track it")
        box.addWidget(self.table, 1)
        return frame

    def _build_workspace(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        head = card()
        row = QHBoxLayout(head)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(10)
        self.icon = QLabel()
        words = QVBoxLayout()
        words.setSpacing(0)
        self.title = label("", "heading")
        self.sub = label("", "muted")
        self.sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        words.addWidget(self.title)
        words.addWidget(self.sub)
        self.track_btn = link_button("Track it", "The device's own page: live signal, history, distance")
        self.track_btn.clicked.connect(lambda: self.device and self.track.emit(self.device))
        row.addWidget(self.icon)
        row.addLayout(words, 1)
        row.addWidget(self.track_btn)
        box.addWidget(head)
        box.addWidget(self._build_packets())
        lower = QHBoxLayout()
        lower.setSpacing(12)
        for part in (self._build_log(), self._build_gatt()):
            # half each, whatever the tables inside would like
            part.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lower.addWidget(part, 1)
        box.addLayout(lower, 1)
        return page

    def _build_packets(self):
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(16)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tiles = {}
        for key, caption, tip in (
            ("rate", "Rate", "Packets heard per second over the last 10 seconds"),
            ("typical", "Shortest gap", "The shortest gap between heard packets that happens often (the 10th "
                                        "percentile of the last minute). The device's real advertising interval "
                                        "is this or shorter: macOS passes on only some packets, about one or two "
                                        "a second per device."),
            ("median", "Median gap", "The middle gap between heard packets"),
            ("changes", "Payload changes", "How often the maker or service data changed this session"),
        ):
            tile = self.tiles[key] = StatTile(caption, tip)
            tiles.addWidget(tile)
        top.addLayout(tiles, 3)
        right = QVBoxLayout()
        right.setSpacing(2)
        right.addWidget(label("GAPS BETWEEN HEARD PACKETS · 1 MIN", "caps"))
        self.histogram = GapHistogram()
        right.addWidget(self.histogram)
        top.addLayout(right, 2)
        box.addLayout(top)
        pay_head = QHBoxLayout()
        pay_head.addWidget(label("LATEST PAYLOAD", "caps"))
        pay_head.addSpacing(8)
        pay_head.addWidget(label("bytes that just changed are highlighted", "faint"))
        pay_head.addStretch(1)
        box.addLayout(pay_head)
        self.payload = QLabel()
        self.payload.setTextFormat(Qt.TextFormat.RichText)
        self.payload.setWordWrap(True)
        self.payload.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.payload.setFont(mono())
        box.addWidget(self.payload)
        return frame

    def _build_log(self):
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(8)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addWidget(label("PACKET LOG", "caps"))
        self.record_btn = QPushButton("● Record")
        self.record_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.record_btn.setToolTip(f"Keep every packet as it arrives, up to the last {LIMIT:,}".replace(",", " "))
        self.record_btn.clicked.connect(self.toggle_record)
        self.all_devices = QCheckBox("All devices")
        self.all_devices.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.all_devices.setToolTip("Record every device, not just this one")
        self.log_clear = link_button("Clear", "Throw the recorded packets away")
        self.log_clear.clicked.connect(self.clear_log)
        self.log_export = tool_button("export", "Export the recorded packets as CSV (⌘E)", 18)
        self.log_export.clicked.connect(self.export_dialog)
        bar.addWidget(self.record_btn)
        bar.addWidget(self.all_devices)
        bar.addStretch(1)
        bar.addWidget(self.log_clear)
        bar.addWidget(self.log_export)
        box.addLayout(bar)
        self.log_stack = QStackedLayout()
        self.log_model = PacketModel(self.store, self)
        self.log_view = QTableView()
        self.log_view.setObjectName("devices")
        self.log_view.setModel(self.log_model)
        self.log_view.setShowGrid(False)
        self.log_view.setWordWrap(False)
        self.log_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.log_view.verticalHeader().hide()
        self.log_view.verticalHeader().setDefaultSectionSize(24)
        header = self.log_view.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in ((0, 116), (1, 140), (2, 48)):
            self.log_view.setColumnWidth(col, width)
        self.log_off = label(f"The log is off, so nothing piles up. Press Record to keep every packet of this "
                             f"device (or all of them) as it arrives, up to the last {LIMIT:,}."
                             .replace(",", " "), "empty")
        self.log_off.setWordWrap(True)
        self.log_off.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.log_stack.addWidget(self.log_off)
        self.log_stack.addWidget(self.log_view)
        box.addLayout(self.log_stack, 1)
        self.log_note = label("", "faint")
        box.addWidget(self.log_note)
        return frame

    def _build_gatt(self):
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(label("GATT", "caps"))
        bar.addStretch(1)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.connect_btn.setToolTip("Stay connected to browse every service, read any characteristic and follow "
                                    "notifications. Nothing is written. Reading a protected characteristic may make "
                                    "macOS ask to pair.")
        self.connect_btn.clicked.connect(self.toggle_connection)
        bar.addWidget(self.connect_btn)
        box.addLayout(bar)
        self.gatt_status = label("Not connected.", "muted")
        self.gatt_status.setWordWrap(True)
        box.addWidget(self.gatt_status)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(14)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 120)
        self.tree.setColumnWidth(2, 96)
        box.addWidget(self.tree, 3)
        box.addWidget(label("NOTIFICATIONS", "caps"))
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumBlockCount(400)
        self.notes.setFont(mono())
        self.notes.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.notes.setPlaceholderText("Values that the device notifies show up here as they arrive.")
        box.addWidget(self.notes, 2)
        return frame

    # ---- picking --------------------------------------------------------------------

    def _filter(self, text: str) -> None:
        self.model.needle = text.strip()
        self.model.refresh(time.time(), force=True)

    def pick(self, device: Device | None) -> None:
        if device is self.device:
            return
        self.disconnect()
        self.device = device
        self.table.marked = device.address if device else None
        self.table.viewport().update()
        self._payload_key = None
        self._shown_total = -1
        self.work.setCurrentIndex(1 if device else 0)
        self.tick(time.time())

    # ---- the packet log ------------------------------------------------------------------

    def toggle_record(self) -> None:
        if self.log.recording:
            self.log.stop()
            self.message.emit(f"Stopped recording: {len(self.log.rows)} packets kept.")
        elif self.device or self.all_devices.isChecked():
            self.log.start(None if self.all_devices.isChecked() else self.device.address)
        self.tick(time.time())

    def clear_log(self) -> None:
        self.log.clear()
        self._shown_total = -1
        self.tick(time.time())

    def export_dialog(self) -> None:
        if not self.log.rows:
            self.message.emit("Nothing recorded yet: press Record first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export packets",
                                              str(Path.home() / f"Packets {datetime.now():%Y-%m-%d %H%M}.csv"),
                                              "CSV (*.csv)")
        if path:
            self.export(Path(path))

    def export(self, path: Path) -> None:
        count = self.log.export(path, {a: d.title for a, d in self.store.devices.items()})
        self.message.emit(f"Exported {count} packets to {path.name}.")

    # ---- GATT ------------------------------------------------------------------------------

    def toggle_connection(self) -> None:
        if self.link is not None and self.link.state in (gatt.CONNECTING, gatt.CONNECTED):
            self.disconnect()
        elif self.device is not None:
            self._clear_tree()
            self.link = self.connect_to(self.device.address)
            self._link_state = None
        self.tick(time.time())

    def disconnect(self) -> None:
        if self.link is not None:
            self.link.close()
        self.link = None
        self._clear_tree()

    def _clear_tree(self) -> None:
        self.tree.clear()
        for table in (self._items, self._follows, self._pending, self._waiting):
            table.clear()
        self._buttons.clear()

    def _note(self, t: float, text: str) -> None:
        self.notes.appendPlainText(f"{datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')[:-3]}  {text}")

    def _build_tree(self) -> None:
        c = colors()
        self._clear_tree()
        for service in self.link.services:
            top = QTreeWidgetItem([f"{service.name}   {names.pretty_uuid(service.uuid)}", "", ""])
            top.setForeground(0, QColor(c["ink"]))
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            top.setToolTip(0, service.uuid)
            self.tree.addTopLevelItem(top)
            top.setFirstColumnSpanned(True)
            for char in service.characteristics:
                name = char.name if char.name != "Unknown" else f"Unknown {names.pretty_uuid(char.uuid)[:8]}"
                item = QTreeWidgetItem([name, "", ""])
                item.setToolTip(0, f"{char.uuid}\n{', '.join(char.properties)}\nhandle {char.handle}")
                top.addChild(item)
                self._items[char.handle] = item
                actions = QWidget()
                row = QHBoxLayout(actions)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(0)
                if char.readable:
                    read = link_button("Read", "Read its value now")
                    read.clicked.connect(lambda _=False, h=char.handle: self.link.read(h))
                    row.addWidget(read)
                    self._buttons.append(read)
                if char.notifies:
                    follow = link_button("Follow", "Follow its notifications live")
                    follow.clicked.connect(lambda _=False, h=char.handle: self._follow(h))
                    row.addWidget(follow)
                    self._buttons.append(follow)
                    self._follows[char.handle] = follow
                row.addStretch(1)
                self.tree.setItemWidget(item, 2, actions)
            top.setExpanded(True)

    def _follow(self, handle: int) -> None:
        """Asks to start or stop. The button says what the device answered,
        not what was asked."""
        on = handle not in self.link.subscribed
        self._pending[handle] = on
        self.link.notify(handle, on)
        self._gatt_tick()

    def _gatt_tick(self) -> None:
        link = self.link
        if link is None:
            self.connect_btn.setText("Connect")
            self.connect_btn.setEnabled(self.device is not None and self.device.connectable is not False)
            self.gatt_status.setText("This device doesn't accept connections: it only broadcasts."
                                     if self.device is not None and self.device.connectable is False
                                     else "Not connected.")
            return
        for event in link.drain():
            item = self._items.get(event.handle)
            if item is None:
                continue
            raw = f"  [{event.data.hex(' ').upper()}]" if event.data and event.text != event.data.hex(" ").upper() \
                else ""
            item.setText(1, event.text)
            item.setToolTip(1, event.text + raw)
            if event.kind == "notify":
                self._waiting.pop(event.handle, None)
                self._note(event.t, f"{item.text(0)}  {event.text}{raw}")
            elif event.kind == "error":
                self._pending.pop(event.handle, None)
                self._note(event.t, f"{item.text(0)}  {event.text}")  # it stays where it's looked for
                self.message.emit(event.text)
        state = link.state
        if state == gatt.CONNECTED and not self._items and link.services:
            self._build_tree()
        if state != self._link_state:
            if state == gatt.CLOSED and self._link_state == gatt.CONNECTED and link.error:
                self._note(time.time(), link.error)  # the device hung up, rather than you
            self._link_state = state
        connected = state == gatt.CONNECTED
        for handle, on in list(self._pending.items()):
            if not connected or (handle in link.subscribed) == on:
                del self._pending[handle]
                if connected and on:
                    self._waiting[handle] = time.time()
        if not connected:
            self._waiting.clear()
        for button in self._buttons:
            button.setEnabled(connected)
        for handle, button in self._follows.items():
            button.setText("Stop" if connected and handle in link.subscribed else "Follow")
            if handle in self._pending:
                button.setEnabled(False)  # until the device answers
        text = {gatt.CONNECTING: "Connecting…", gatt.CONNECTED: f"Connected: {len(link.services)} services.",
                gatt.CLOSED: link.error or "Disconnected.", gatt.FAILED: f"Couldn't connect: {link.error}"}[state]
        quiet = [h for h, t in self._waiting.items() if h in link.subscribed and time.time() - t >= QUIET]
        starting = [h for h, on in self._pending.items() if on]
        if connected and starting:
            text += f" Starting to follow {self._items[starting[0]].text(0)}…"
        elif connected and quiet:
            short = next((names.short_uuid(c.uuid) for s in link.services for c in s.characteristics
                          if c.handle == quiet[0]), None)
            text += (" Following Heart Rate Measurement: nothing yet. A watch usually sends its heart rate only "
                     "after you turn on heart rate sharing on the watch." if short == 0x2A37 else
                     f" Following {self._items[quiet[0]].text(0)}: nothing yet. A device sends only when it has "
                     "a new value.")
        self.gatt_status.setText(text)
        live = state in (gatt.CONNECTING, gatt.CONNECTED)
        self.connect_btn.setText("Disconnect" if live else "Connect")
        self.connect_btn.setEnabled(True)

    # ---- every tick ------------------------------------------------------------------------

    def refresh_icons(self) -> None:
        refresh_tool_icons(self.log_export)
        self._payload_key = None

    def tick(self, now: float) -> None:
        self.model.refresh(now)
        self.hover.tick(now)
        self._log_tick()
        d = self.device
        if d is None:
            return
        c = colors()
        self.icon.setPixmap(icons.pixmap(d.info.icon, c["accent"], 28))
        self.title.setText(d.title)
        self.sub.setText(f"{subtitle(d)}  ·  {d.mac or d.address}")
        gaps = d.gaps(now, 60)
        stats = gap_stats(gaps)
        self.tiles["rate"].set(f"{d.rate(now):.1f} / s")
        self.tiles["typical"].set(ms(stats.typical) if stats else "—")
        self.tiles["median"].set(ms(stats.median) if stats else "—")
        self.tiles["changes"].set(str(d.payload_changes))
        self.histogram.set_gaps(gaps)
        key = (d._payload, c["accent"])
        if key != self._payload_key:
            self._payload_key = key
            rows = []
            for cid, data in d.manufacturer_data.items():
                who = short_company(names.company(cid)) or "Unknown maker"
                rows.append((f"{who} 0x{cid:04X}", diff_html(data, d.previous.get(payload_key(cid)), c)))
            for uuid, data in d.service_data.items():
                who = names.service(uuid) or "Service"
                rows.append((f"{who} {names.pretty_uuid(uuid)}", diff_html(data, d.previous.get(payload_key(uuid)), c)))
            self.payload.setText("<br>".join(f"<span style='color:{c['muted']}'>{esc(k)}</span>&nbsp;&nbsp;{v}"
                                             for k, v in rows)
                                 or f"<span style='color:{c['muted']}'>No maker or service data: "
                                    f"{'only a name' if d.name else 'an empty advertisement'}.</span>")
        self._gatt_tick()

    def _log_tick(self) -> None:
        log = self.log
        if self.record_btn.property("primary") != (not log.recording):
            self.record_btn.setText("■ Stop" if log.recording else "● Record")
            self.record_btn.setProperty("primary", not log.recording)
            self.record_btn.style().unpolish(self.record_btn)
            self.record_btn.style().polish(self.record_btn)
        self.record_btn.setEnabled(log.recording or self.device is not None or self.all_devices.isChecked())
        self.all_devices.setEnabled(not log.recording)
        count = len(log.rows)
        self.log_clear.setEnabled(bool(count))
        self.log_stack.setCurrentIndex(1 if count or log.recording else 0)
        at_top = self.log_view.verticalScrollBar().value() == 0
        if log.total != self._shown_total and at_top:
            self._shown_total = log.total
            self.log_model.show(log.latest(SHOWN))
        parts = [f"{count:,} packet{'s' if count != 1 else ''}".replace(",", " ")]
        if log.recording:
            who = "every device" if log.address is None else (
                self.store.devices[log.address].title if log.address in self.store.devices else "one device")
            parts.append(f"recording {who}")
        if log.total != self._shown_total:
            parts.append(f"{log.total - self._shown_total} new: scroll to the top to follow")
        else:
            parts.append("newest first" + (f", the last {SHOWN} shown (export keeps them all)" if count > SHOWN else ""))
        self.log_note.setText("  ·  ".join(parts))

    def hideEvent(self, event) -> None:
        self.hover.hide()
        super().hideEvent(event)
