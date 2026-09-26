"""The Trackers job: item trackers around you (AirTags and other Find My
devices away from their owner, Tile, SmartTag, Chipolo, Google's tags)
and whether one is following you. Watch for adds other kinds of device.
The watching itself runs whichever job is on screen; this page shows it."""
import time
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QMessageBox, QPushButton,
    QStackedLayout,
    QStyledItemDelegate, QTableView, QVBoxLayout, QWidget,
)

from blue_station.core.decode import short_company
from blue_station.core.devices import DeviceStore, ago_text, span_text
from blue_station.core.watch import FOLLOWING, GROUPS, HEARD_WITHIN, STAYING, TrackerRecord, Watcher
from blue_station.ui import icons
from blue_station.ui.theme import colors
from blue_station.ui.widgets import ChecklistMenu, bold, card, dbm, label, link_button, paint_signal, subtitle, when

TRACKER, SIGNAL, WITH_YOU, PLACES, VERDICT = range(5)
HEADERS = ["TRACKER", "SIGNAL", "WITH YOU", "PLACES", "VERDICT"]
RECORD_ROLE = Qt.ItemDataRole.UserRole + 1
VERDICTS = {FOLLOWING: ("Following you", "danger"), STAYING: ("Staying near you", "warning"),
            "passing": ("Passing by", "muted")}
_ORDER = {FOLLOWING: 0, STAYING: 1, "passing": 2}

GROUP_ICONS = {"trackers": "tag", "headphones": "headphones", "wearables": "watch", "phones": "phone",
               "other": "generic"}

EXPLAIN = ("A {noun} counts as following you once it has been with you in two different places. Places are "
           "recognized from named devices that stay put (TVs, printers, speakers), which takes a few minutes after "
           "you arrive. You can also say so yourself with I've moved. If it's one of yours, open it and choose "
           "This is mine.")
MORE = (" Phones, AirPods and most watches change their Bluetooth address every 15 minutes or so, so they can't be "
        "followed from place to place. Devices with a fixed address can.")


def only_trackers(watcher: Watcher) -> bool:
    return watcher.groups == {"trackers"}


def _clock(t: float, now: float) -> str:
    d = datetime.fromtimestamp(t)
    return f"{d:%H:%M}" if d.date() == datetime.fromtimestamp(now).date() else when(t)


def timeline(watcher: Watcher, record: TrackerRecord, now: float, last: int = 6) -> str:
    """Where and when it was with you, newest last: 'Home 8:05–8:40  ·
    unknown place 8:40–9:05  ·  Office 9:10–now'."""
    parts = []
    for place, start, end in record.visits[-last:]:
        a = _clock(start, now)
        if now - end <= HEARD_WITHIN:
            b = "now"
        elif datetime.fromtimestamp(end).date() == datetime.fromtimestamp(start).date():
            b = f"{datetime.fromtimestamp(end):%H:%M}"
        else:
            b = when(end)
        parts.append(f"{watcher.place_name(place)} {a if b == a else f'{a}–{b}'}")
    return "  ·  ".join(parts)


class TrackerModel(QAbstractTableModel):
    def __init__(self, watcher: Watcher, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.watcher, self.store = watcher, store
        self.rows: list[TrackerRecord] = []
        self.now = time.time()
        self.needle = ""
        self.show_gone = False

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if section == TRACKER and not only_trackers(self.watcher):
                return "DEVICE"
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignRight if section in (WITH_YOU, PLACES) else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return [None, None, "Time it has been heard with you, across sessions",
                    "Different places it has been with you", None][section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self.rows[index.row()]
        if role == RECORD_ROLE:
            return record
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == PLACES and record.visits:
            return timeline(self.watcher, record, self.now).replace("  ·  ", "\n")
        if role == Qt.ItemDataRole.DisplayRole:
            return [record.kind, None, span_text(record.seen_seconds), str(len(record.places)),
                    VERDICTS[record.verdict][0]][index.column()]
        return None

    def around(self, record: TrackerRecord) -> bool:
        d = self.store.devices.get(record.address)
        return d is not None and not d.gone(self.now)

    def matches(self, record: TrackerRecord) -> bool:
        """Like the Scan page's filter, plus the verdict and where it was with you."""
        d = self.store.devices.get(record.address)
        words = [record.kind, record.address, VERDICTS[record.verdict][0], GROUPS.get(record.group)]
        if d is not None:
            words += [d.title, d.name, d.info.vendor, short_company(d.info.vendor), d.mac]
        words += [self.watcher.place_name(v[0]) for v in record.visits]
        haystack = " ".join(filter(None, words)).lower()
        return all(word in haystack for word in self.needle.lower().split())

    def refresh(self, now: float) -> None:
        self.now = now
        order = sorted(self.watcher.watched(), key=lambda r: (_ORDER[r.verdict], -r.last_seen))
        if not self.show_gone:  # ones that may be following you stay, like pinned devices on Scan
            order = [r for r in order if r.verdict == FOLLOWING or self.around(r)]
        if self.needle:
            order = [r for r in order if self.matches(r)]
        if [r.address for r in order] != [r.address for r in self.rows]:
            self.beginResetModel()
            self.rows = order
            self.endResetModel()
        elif self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(HEADERS) - 1))


class TrackerDelegate(QStyledItemDelegate):
    def __init__(self, view: "QTableView"):
        super().__init__(view)
        self.view = view

    def paint(self, p: QPainter, option, index) -> None:
        c = colors()
        record: TrackerRecord = index.data(RECORD_ROLE)
        model: TrackerModel = index.model()
        device = model.store.devices.get(record.address)
        heard = device is not None and not device.gone(model.now)
        text, key = VERDICTS[record.verdict]
        rect = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if index.row() == getattr(self.view, "hover_row", -1):
            p.fillRect(option.rect, QColor(c["hover"]))
        p.setPen(QPen(QColor(c["border"]), 1))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())
        if not heard:
            p.setOpacity(0.55)
        col = index.column()
        font = p.font()
        if col == TRACKER:
            icon = device.info.icon if device else GROUP_ICONS.get(record.group, "generic")
            p.drawPixmap(int(rect.left() + 12), int(rect.center().y() - 11),
                         icons.pixmap(icon, c[key] if key != "muted" else c["accent"], 22))
            left, width = rect.left() + 46, rect.width() - 50
            p.setFont(bold(font))
            p.setPen(QColor(c["ink"]))
            title = device.title if device else record.kind
            p.drawText(QRectF(left, rect.top() + 6, width, rect.height() / 2 - 4),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       p.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, int(width)))
            small = QFont(font)
            small.setPixelSize(11)
            p.setFont(small)
            p.setPen(QColor(c["muted"]))
            sub = subtitle(device) if device else "Not heard in this session"
            sub += f"  ·  first heard {when(record.first_seen)}"
            p.drawText(QRectF(left, rect.center().y() + 2, width, rect.height() / 2 - 4),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       p.fontMetrics().elidedText(sub, Qt.TextElideMode.ElideRight, int(width)))
        elif col == SIGNAL:
            if heard:
                bar = QRectF(rect.left() + 8, rect.center().y() - 4, max(20.0, rect.width() - 86), 8)
                paint_signal(p, bar, device.smoothed, c)
                p.setFont(bold(font))
                p.setPen(QColor(c["ink"]))
                p.drawText(QRectF(bar.right() + 8, rect.top(), 70, rect.height()),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"{dbm(device.smoothed)} dBm")
            else:
                p.setPen(QColor(c["faint"]))
                p.drawText(rect.adjusted(8, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           f"Last heard {ago_text(model.now - record.last_seen)}")
        elif col in (WITH_YOU, PLACES):
            p.setPen(QColor(c["ink"] if col == WITH_YOU else c[key] if record.verdict == FOLLOWING else c["ink"]))
            p.setFont(bold(font))
            value = span_text(record.seen_seconds) if col == WITH_YOU else str(len(record.places) or "—")
            p.drawText(rect.adjusted(0, 0, -14, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, value)
        else:
            small = QFont(font)
            small.setPixelSize(11)
            small.setWeight(QFont.Weight.DemiBold)
            p.setFont(small)
            width = p.fontMetrics().horizontalAdvance(text) + 18
            pill = QRectF(rect.left() + 10, rect.center().y() - 11, width, 22)
            fill = QColor(c[key])
            fill.setAlphaF(0.16)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(pill, 11, 11)
            p.setPen(QColor(c[key]))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()


class TrackerTable(QTableView):
    opened = Signal(object)  # TrackerRecord

    def __init__(self, model: TrackerModel, parent=None):
        super().__init__(parent)
        self.setObjectName("devices")
        self.setModel(model)
        self.setItemDelegate(TrackerDelegate(self))
        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(54)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(TRACKER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SIGNAL, QHeaderView.ResizeMode.Stretch)
        for col, width in ((WITH_YOU, 130), (PLACES, 90), (VERDICT, 170)):
            self.setColumnWidth(col, width)
        self.hover_row = -1

    def mouseMoveEvent(self, event) -> None:
        row = self.indexAt(event.position().toPoint()).row()
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            self.opened.emit(index.data(RECORD_ROLE))


class TrackersPage(QWidget):
    track = Signal(object)  # Device
    message = Signal(str)
    changed = Signal()  # the watch's memory changed by hand
    groupsChanged = Signal(list)  # what to watch for

    def __init__(self, watcher: Watcher, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.watcher, self.store = watcher, store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        status = card()
        row = QHBoxLayout(status)
        row.setContentsMargins(18, 12, 16, 12)
        row.setSpacing(14)
        words = QVBoxLayout()
        words.setSpacing(0)
        self.headline = label("", "big")
        self.detail = label("", "muted")
        words.addWidget(self.headline)
        words.addWidget(self.detail)
        row.addLayout(words, 1)
        self.moved_btn = QPushButton("I've moved")
        self.moved_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.moved_btn.setToolTip("Tell Blue Station you're somewhere new. It also notices by itself when the "
                                  "devices around you change, a few minutes after you arrive.")
        self.moved_btn.clicked.connect(self.moved)
        self.watch_btn = QPushButton("Watch for  ▾")
        self.watch_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.watch_btn.setToolTip("Which kinds of device to watch. Item trackers are what it's for; the others "
                                  "only work for devices that keep their Bluetooth address.")
        self.watch_menu = ChecklistMenu(self)
        self.group_actions = {}
        for key, text in GROUPS.items():
            action = self.watch_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(key in watcher.groups)
            action.toggled.connect(self._groups_picked)
            self.group_actions[key] = action
        self._lock_last()
        self.watch_menu.addSeparator()
        self.unmine_action = self.watch_menu.addAction("Watch my devices again", self.unmine)
        self.watch_menu.addAction("Forget history…", self.forget).setToolTip(
            "Forget every tracker and place seen so far")
        self.watch_menu.aboutToShow.connect(self._menu_shown)
        self.watch_btn.setMenu(self.watch_menu)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(180)
        self.search.setToolTip("Matches names, kinds, makers, addresses, verdicts and places")
        self.search.addAction(icons.icon("search", colors()["faint"], 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._filter)
        self.name_btn = link_button("Name this place", "Call the place you're at something of your own, like Home. "
                                                       "Timelines then say it instead of a number.")
        self.name_btn.clicked.connect(self.name_place)
        self.gone = QCheckBox("Show out of range")
        self.gone.setToolTip("Also list the ones not heard for 30 seconds. Any that may be following you are "
                             "always listed.")
        self.gone.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.gone.toggled.connect(self._toggle_gone)
        row.addWidget(self.search)
        row.addWidget(self.gone)
        row.addWidget(self.name_btn)
        row.addWidget(self.watch_btn)
        row.addWidget(self.moved_btn)
        layout.addWidget(status)

        table_card = card()
        box = QVBoxLayout(table_card)
        box.setContentsMargins(1, 1, 1, 1)
        box.setSpacing(0)
        self.stack = QStackedLayout()
        self.model = TrackerModel(watcher, store, self)
        self.table = TrackerTable(self.model)
        self.table.opened.connect(self._open)
        self.empty = label("", "empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        box.addLayout(self.stack, 1)
        self.explain = label("", "faint")
        self.explain.setWordWrap(True)
        self.explain.setContentsMargins(16, 8, 16, 10)
        box.addWidget(self.explain)
        layout.addWidget(table_card, 1)
        self._words()

    def _words(self) -> None:
        """What the page calls the things it watches: trackers, or devices once
        it watches more than item trackers."""
        self.search.setPlaceholderText(f"Filter {'trackers' if only_trackers(self.watcher) else 'devices'}   ⌘F")
        if only_trackers(self.watcher):
            self.explain.setText(EXPLAIN.format(noun="tracker"))
            self._empty_text = ("No item trackers heard yet. Blue Station keeps watching in the background, "
                                "whichever job you're in.")
        else:
            self.explain.setText(EXPLAIN.format(noun="device") + MORE)
            self._empty_text = ("Nothing you watch for heard yet. Blue Station keeps watching in the background, "
                                "whichever job you're in.")
        self.empty.setText(self._empty_text)
        self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, TRACKER, TRACKER)

    def _menu_shown(self) -> None:
        mine = len(self.watcher.mine)
        self.unmine_action.setText(f"Watch my devices again ({mine})" if mine else "Watch my devices again")
        self.unmine_action.setEnabled(bool(mine))
        self.unmine_action.setToolTip("Forget which devices you said are yours")

    def _lock_last(self) -> list[str]:
        """Something is always watched: the last kind ticked can't be unticked."""
        picked = [key for key, action in self.group_actions.items() if action.isChecked()]
        for action in self.group_actions.values():
            action.setEnabled(len(picked) > 1 or not action.isChecked())
        return picked

    def _groups_picked(self) -> None:
        picked = self._lock_last()
        self.watcher.set_groups(picked)
        self._words()
        self.groupsChanged.emit(picked)
        self.tick(time.time())

    def unmine(self) -> None:
        count = len(self.watcher.mine)
        for address in list(self.watcher.mine):
            self.watcher.set_mine(address, False)
        self.changed.emit()
        self.message.emit(f"Watching {count} device{'s' if count != 1 else ''} you said were yours again.")

    def _toggle_gone(self, on: bool) -> None:
        self.model.show_gone = on
        self.tick(time.time())

    def _filter(self, text: str) -> None:
        self.model.needle = text.strip()
        self.tick(time.time())

    def name_place(self) -> None:
        place = self.watcher.settled
        if place is None:
            return
        name, ok = QInputDialog.getText(self, "Name this place", "What do you call the place you're at?",
                                        text=place.name or "")
        if ok:
            self.watcher.name_place(place, name)
            self.changed.emit()
            self.tick(time.time())

    def moved(self) -> None:
        place = self.watcher.moved(time.time())
        self.changed.emit()
        self.message.emit(f"Noted: you're at a new place (place {place.id}). It learns the landmarks around you "
                          "over the next few minutes.")
        self.tick(time.time())

    def forget(self) -> None:
        answer = QMessageBox.question(self, "Forget history",
                                      "Forget every tracker and place Blue Station has seen so far?")
        if answer == QMessageBox.StandardButton.Yes:
            self.watcher.forget()
            self.changed.emit()
            self.tick(time.time())

    def _open(self, record: TrackerRecord) -> None:
        device = self.store.devices.get(record.address)
        if device is None:
            self.message.emit(f"That {record.kind} hasn't been heard in this session "
                              f"(last {ago_text(time.time() - record.last_seen)}).")
        else:
            self.track.emit(device)

    def tick(self, now: float) -> None:
        c = colors()
        self.model.refresh(now)
        noun = "tracker" if only_trackers(self.watcher) else "device"
        following = self.watcher.following()
        if following:
            n = len(following)
            self.headline.setText(f"<span style='color:{c['danger']}'>{n} {noun}{'s' if n > 1 else ''} may be "
                                  f"following you</span>")
        else:
            self.headline.setText(f"<span style='color:{c['success']}'>Nothing is following you</span>")
        watched = self.watcher.watched()
        heard = sum(1 for r in watched if (d := self.store.devices.get(r.address)) is not None and not d.gone(now))
        parts = [f"{len(watched)} {noun}{'s' if len(watched) != 1 else ''} seen, {heard} around you now"]
        if self.model.needle:
            parts.insert(0, f"{len(self.model.rows)} match the filter")
        settled, current = self.watcher.settled, self.watcher.current
        if settled is not None:
            marks = len(settled.landmarks)
            parts.append(f"at {self.watcher.place_name(settled.id)} since {when(settled.first_seen)}"
                         + (f", known by {marks} landmark{'s' if marks != 1 else ''}" if marks else ", learning it"))
        elif current is not None:
            parts.append("checking whether you've moved…")
        else:
            parts.append("learning where you are (it needs two named devices that stay put)")
        self.detail.setText("  ·  ".join(parts))
        self.name_btn.setVisible(settled is not None)
        if settled is not None:
            self.name_btn.setText("Rename this place" if settled.name else "Name this place")
        away = len(watched) - heard
        if self.model.needle:
            self.empty.setText(f"No {noun} matches the filter.")
        elif away and not self.model.show_gone:
            self.empty.setText(f"No {noun}s around you now. Tick Show out of range to see the {away} heard "
                               "earlier.")
        else:
            self.empty.setText(self._empty_text)
        self.stack.setCurrentWidget(self.table if self.model.rows else self.empty)
