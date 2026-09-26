"""The device list: every device in range, a live signal bar each,
details on hover, a click to track one. The order holds still while the
pointer is over the list, so a row never jumps out from under a click."""
import time
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLineEdit, QStackedLayout, QStyle,
    QStyledItemDelegate, QTableView, QVBoxLayout, QWidget,
)

from blue_station.core.baseline import Baseline
from blue_station.core.decode import short_company
from blue_station.core.devices import Device, DeviceStore, ago_text, distance_text
from blue_station.core.links import lead
from blue_station.ui import icons
from blue_station.ui.theme import colors
from blue_station.ui.widgets import (
    ElidedLabel, HoverCard, RangeSlider, bold, card, dbm, label, link_button, paint_signal, refresh_tool_icons,
    subtitle, tool_button,
)

PIN, DEVICE, SIGNAL, DISTANCE, SEEN = range(5)
HEADERS = ["", "DEVICE", "SIGNAL", "DISTANCE", "LAST SEEN"]
DEVICE_ROLE = Qt.ItemDataRole.UserRole + 1
RESORT_EVERY = 2.0  # seconds; a list re-sorted on every packet would never sit still
NEW_FOR = 10.0  # seconds a new device wears its NEW badge


def text_matches(haystack: str, needle: str) -> bool:
    """Every word must be there, except a word with a minus in front
    (-apple), which must not."""
    haystack = haystack.lower()
    for word in needle.lower().split():
        if word.startswith("-") and len(word) > 1:
            if word[1:] in haystack:
                return False
        elif word not in haystack:
            return False
    return True


def matches(device: Device, needle: str) -> bool:
    haystack = " ".join(filter(None, [device.title, device.name, device.info.kind, device.info.vendor,
                                      short_company(device.info.vendor), device.address, device.mac]))
    return text_matches(haystack, needle)


NEAR = -80  # dBm: by default, Nearby only shows devices at least this strong...
NEAR_SLACK = 4  # ... and keeps them until this much weaker, so a device on the edge doesn't flicker
NEAR_RANGE = (-100, -30)  # the high handle at the top: no upper limit


class DeviceModel(QAbstractTableModel):
    def __init__(self, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.rows: list[Device] = []
        self.now = time.time()
        self.needle = ""
        self.show_gone = False
        self.nearby_only = False
        self.baseline: Baseline | None = None  # New only: everything heard before counts as known
        self.near_dbm = NEAR  # the weakest signal shown...
        self.near_max = NEAR_RANGE[1]  # ... and the strongest, below the top hiding the very closest ones too
        self._near: set[str] = set()
        self.sort_column, self.sort_order = SIGNAL, Qt.SortOrder.DescendingOrder
        self.frozen = False
        self.predicate = None  # a job's own rule for which devices belong in its list
        self._sorted_at = 0.0

    # ---- Qt's side ----------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignRight if section in (DISTANCE, SEEN) else Qt.AlignmentFlag.AlignLeft
            return int(align | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return ["Pinned devices stay on top", "Sort by name", "Sort by signal strength",
                    "Sort by rough distance", "Sort by when a packet was last heard"][section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        device = self.rows[index.row()]
        if role == DEVICE_ROLE:
            return device
        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == PIN:
                return "★" if device.pinned else ""
            if col == DEVICE:
                return device.title
            if col == SIGNAL:
                return f"{dbm(device.smoothed)} dBm"
            if col == DISTANCE:
                return distance_text(device.distance())
            return ago_text(device.age(self.now))
        return None

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        self.sort_column, self.sort_order = column, order
        self.refresh(self.now, force=True)

    # ---- keeping up with the store ----------------------------------------------------

    def _key(self, device: Device):
        col = self.sort_column
        if col == DEVICE:
            return device.title.lower()
        if col == DISTANCE:
            distance = device.distance()
            return float("inf") if distance is None else distance
        if col == SEEN:
            return device.age(self.now)
        return -999.0 if device.smoothed is None else device.smoothed  # PIN and SIGNAL

    def near(self, device: Device) -> bool:
        s = device.smoothed
        slack = NEAR_SLACK if device.address in self._near else 0  # shown ones stay a little past either end
        near = s is not None and s >= self.near_dbm - slack and (self.near_max >= NEAR_RANGE[1]
                                                                or s <= self.near_max + slack)
        if near:
            self._near.add(device.address)
        else:
            self._near.discard(device.address)
        return near

    def visible(self) -> list[Device]:
        return [d for d in self.store.devices.values()
                if d.superseded_by is None  # an address it has changed from: the device is listed under its new one
                and (d.partner is None or lead(d, d.partner) is d)  # one device sending two kinds: listed once
                and (d.pinned or self.show_gone or not d.gone(self.now)) and (not self.needle or matches(d, self.needle))
                and (d.pinned or not self.nearby_only or self.near(d))
                and (d.pinned or self.baseline is None or self.baseline.is_new(d))
                and (self.predicate is None or self.predicate(d))]

    def refresh(self, now: float, force: bool = False) -> None:
        self.now = now
        wanted = self.visible()
        if self.frozen and not force:
            # keep every row where it is; newcomers wait at the bottom
            kept = {d.address for d in self.rows}
            order = [d for d in self.rows if d.address in self.store.devices]
            order += [d for d in wanted if d.address not in kept]
        elif force or now - self._sorted_at >= RESORT_EVERY:
            self._sorted_at = now
            order = sorted(wanted, key=self._key, reverse=self.sort_order == Qt.SortOrder.DescendingOrder)
            order.sort(key=lambda d: not d.pinned)
        else:
            keep = {d.address for d in wanted}
            order = [d for d in self.rows if d.address in keep]
            known = {d.address for d in order}
            order += [d for d in wanted if d.address not in known]
        if [d.address for d in order] == [d.address for d in self.rows]:
            if self.rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(HEADERS) - 1))
            return
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        addresses = [self.rows[i.row()].address if i.row() < len(self.rows) else None for i in old]
        self.rows = order
        where = {d.address: r for r, d in enumerate(order)}
        self.changePersistentIndexList(old, [self.index(where[a], i.column()) if a in where else QModelIndex()
                                             for a, i in zip(addresses, old)])
        self.layoutChanged.emit()

    def row_of(self, address: str) -> int:
        return next((r for r, d in enumerate(self.rows) if d.address == address), -1)


class DeviceDelegate(QStyledItemDelegate):
    """Paints every cell by hand: the pin star, the device with its icon and
    what it is, the signal bar with its number, distance and last seen."""

    def __init__(self, view: "DeviceTable"):
        super().__init__(view)
        self.view = view

    def paint(self, p: QPainter, option, index) -> None:
        c = colors()
        device: Device = index.data(DEVICE_ROLE)
        model: DeviceModel = index.model()
        now = model.now
        rect = QRectF(option.rect)
        hovered = index.row() == self.view.hover_row
        selected = bool(option.state & QStyle.StateFlag.State_Selected) and self.view.hasFocus()
        marked = device.address == self.view.marked
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if hovered or selected or marked:
            p.fillRect(option.rect, QColor(c["tint"] if selected or marked else c["hover"]))
        if marked and index.column() == 0:
            p.fillRect(QRectF(rect.left(), rect.top(), 3, rect.height()), QColor(c["accent_strong"]))
        p.setPen(QPen(QColor(c["border"]), 1))
        p.drawLine(rect.bottomLeft(), rect.bottomRight())
        p.setOpacity(device.fade(now))
        col = index.column()
        if col == PIN:
            if device.pinned or hovered:
                name, color = ("star_filled", c["accent"]) if device.pinned else ("star", c["faint"])
                pm = icons.pixmap(name, color, 16)
                p.drawPixmap(int(rect.center().x() - 8), int(rect.center().y() - 8), pm)
        elif col == DEVICE:
            self._paint_device(p, rect, device, now, c)
        elif col == SIGNAL and self.view.picker and device.gone(now):
            p.setPen(QColor(c["warning"]))
            p.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"quiet {ago_text(device.age(now)).removesuffix(' ago')}")
        elif col == SIGNAL:
            number_w = 70 if not self.view.picker else 62
            bar = QRectF(rect.left() + 8, rect.center().y() - 4, max(20.0, rect.width() - number_w - 16), 8)
            paint_signal(p, bar, device.smoothed, c)
            text_rect = QRectF(bar.right() + 8, rect.top(), number_w, rect.height())
            font = p.font()
            p.setFont(bold(font))
            p.setPen(QColor(c["ink"]))
            number = dbm(device.smoothed)
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, number)
            width = p.fontMetrics().horizontalAdvance(number)
            p.setFont(font)
            p.setPen(QColor(c["faint"]))
            p.drawText(text_rect.adjusted(width + 4, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       "dBm")
        elif col == DISTANCE:
            p.setPen(QColor(c["muted"]))
            p.drawText(rect.adjusted(0, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       distance_text(device.distance()))
        else:
            age = device.age(now)
            fresh = age < 2
            p.setPen(QColor(c["success"] if fresh else c["muted"] if not device.gone(now) else c["faint"]))
            p.drawText(rect.adjusted(0, 0, -16, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       ago_text(age))
        p.restore()

    def _paint_device(self, p: QPainter, rect: QRectF, device: Device, now: float, c: dict) -> None:
        tracker = device.info.tracker
        icon_color = c["warning"] if tracker else c["accent"] if device.smoothed is not None else c["muted"]
        p.drawPixmap(int(rect.left() + 4), int(rect.center().y() - 11), icons.pixmap(device.info.icon, icon_color, 22))
        left = rect.left() + 38
        width = rect.width() - 42
        font = p.font()
        title_font = bold(font)
        small = QFont(font)
        small.setPixelSize(11)
        p.setFont(title_font)
        metrics = p.fontMetrics()
        badge = "NEW" if now - device.first_seen < NEW_FOR else ""
        title = metrics.elidedText(device.title, Qt.TextElideMode.ElideRight, int(width - (44 if badge else 0)))
        named = bool(device.nickname or device.name)
        p.setPen(QColor(c["ink"] if named else c["muted"]))
        title_rect = QRectF(left, rect.top() + 6, width, rect.height() / 2 - 4)
        p.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, title)
        if badge:
            x = left + metrics.horizontalAdvance(title) + 8
            p.setFont(small)
            pill = QRectF(x, title_rect.bottom() - 15, p.fontMetrics().horizontalAdvance(badge) + 10, 15)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["tint"]))
            p.drawRoundedRect(pill, 7, 7)
            p.setPen(QColor(c["accent"]))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, badge)
        p.setFont(small)
        p.setPen(QColor(c["warning"] if tracker else c["muted"]))
        sub = p.fontMetrics().elidedText(subtitle(device), Qt.TextElideMode.ElideRight, int(width))
        p.drawText(QRectF(left, rect.center().y() + 2, width, rect.height() / 2 - 4),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, sub)
        p.setFont(font)


class DeviceTable(QTableView):
    """A click opens a device -- or, in a picker, a click picks it and a
    double-click opens it."""
    opened = Signal(object)  # Device
    picked = Signal(object)
    pinToggled = Signal(object)
    hovered = Signal(object, QPoint)  # Device or None, global position

    def __init__(self, model: DeviceModel, parent=None, picker: bool = False):
        super().__init__(parent)
        self.picker = picker
        self.marked: str | None = None  # the picked device's address
        self.setObjectName("devices")
        self.setModel(model)
        self.setItemDelegate(DeviceDelegate(self))
        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(50)
        header = self.horizontalHeader()
        header.setSortIndicator(model.sort_column, model.sort_order)  # before sorting is on, which sorts by it
        self.setSortingEnabled(True)
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(DEVICE, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(SIGNAL, QHeaderView.ResizeMode.Stretch)
        for col, width in ((PIN, 34), (DISTANCE, 100), (SEEN, 110)):
            self.setColumnWidth(col, width)
        self.hover_row = -1
        if picker:  # narrow: the device and its signal (or how long it's been quiet)
            for col in (PIN, DISTANCE, SEEN):
                self.setColumnHidden(col, True)
            header.setSectionResizeMode(SIGNAL, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(SIGNAL, 128)

    def device_at(self, pos) -> Device | None:
        index = self.indexAt(pos)
        return index.data(DEVICE_ROLE) if index.isValid() else None

    def mouseMoveEvent(self, event) -> None:
        row = self.indexAt(event.position().toPoint()).row()
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        self.hovered.emit(self.device_at(event.position().toPoint()), event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_row = -1
        self.viewport().update()
        self.hovered.emit(None, QPoint())
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton or not index.isValid():
            return
        device = index.data(DEVICE_ROLE)
        if index.column() == PIN:
            self.pinToggled.emit(device)
        elif self.picker:
            self.pick(device)
        else:
            self.opened.emit(device)

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if self.picker and index.isValid():
            self.opened.emit(index.data(DEVICE_ROLE))
            return
        super().mouseDoubleClickEvent(event)

    def pick(self, device: Device | None) -> None:
        self.marked = device.address if device else None
        self.viewport().update()
        self.picked.emit(device)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            device = self.currentIndex().data(DEVICE_ROLE)
            self.pick(device) if self.picker else self.opened.emit(device)
            return
        super().keyPressEvent(event)


class HoverCards(QObject):
    """Shows the hover card for whichever device the pointer rests on in a
    table, and keeps it live."""

    def __init__(self, table, hint: str = "Click to track this device", extra=None):
        super().__init__(table)
        self.table = table
        self.extra = extra  # device -> the page's own rows for the card, or None
        self.card = HoverCard()
        self.card.hint.setText(hint)
        self.device: Device | None = None
        self._pos = QPoint()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._show)
        table.hovered.connect(self._hovered)
        table.opened.connect(lambda _d: self.hide())
        table.destroyed.connect(self.card.deleteLater)

    def _hovered(self, device: Device | None, pos: QPoint) -> None:
        self.device, self._pos = device, pos
        if device is None:
            self.hide()
        elif self.card.isVisible():
            self._show()
        else:
            self._timer.start()

    def _show(self) -> None:
        if self.device is None or not self.table.underMouse():
            return
        self.card.show_device(self.device, time.time(), self.extra(self.device) if self.extra else None)
        self.card.place(self._pos)
        self.card.show()

    def hide(self) -> None:
        self._timer.stop()
        self.card.hide()

    def tick(self, now: float) -> None:
        if self.card.isVisible() and self.device is not None:
            self.card.show_device(self.device, now, self.extra(self.device) if self.extra else None)


class DevicesPage(QWidget):
    track = Signal(object)  # Device
    pinChanged = Signal(object)
    message = Signal(str)

    def __init__(self, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.model = DeviceModel(store, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_summary())

        table_card = card()
        box = QVBoxLayout(table_card)
        box.setContentsMargins(1, 1, 1, 1)
        box.setSpacing(0)
        self.stack = QStackedLayout()
        self.table = DeviceTable(self.model)
        self.empty = label("", "empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        box.addLayout(self.stack, 1)
        foot = QHBoxLayout()
        foot.setContentsMargins(16, 8, 16, 10)
        hint = label("Hover a device for details  ·  click to track it  ·  ☆ pins it to the top  ·  "
                     "the list holds still while the pointer is on it", "faint")
        foot.addWidget(hint, 1)
        box.addLayout(foot)
        layout.addWidget(table_card, 1)

        self.hover = HoverCards(self.table)
        self.table.opened.connect(self.track.emit)
        self.table.pinToggled.connect(self._toggle_pin)

    def _build_summary(self):
        frame = card()
        row = QHBoxLayout(frame)
        row.setContentsMargins(18, 12, 16, 12)
        row.setSpacing(14)
        numbers = QVBoxLayout()
        numbers.setSpacing(0)
        self.count = label("0 devices", "big")
        self.detail = ElidedLabel("", "muted")
        numbers.addWidget(self.count)
        numbers.addWidget(self.detail)
        row.addLayout(numbers, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter devices   ⌘F")
        self.search.setToolTip("Matches names, kinds, makers and addresses. A minus leaves out what matches: "
                               "-apple -tv")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(220)
        self.search.addAction(icons.icon("search", colors()["faint"], 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._filter)
        self.nearby = QCheckBox("Nearby only")
        self.nearby.setToolTip("Show only devices whose signal is within the range set beside it. Pinned devices "
                               "stay.")
        self.nearby.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nearby.toggled.connect(self._toggle_nearby)
        self.near_slider = RangeSlider(*NEAR_RANGE, NEAR, NEAR_RANGE[1])
        self.near_slider.setFixedWidth(110)
        self.near_slider.setToolTip("The left handle is the weakest signal shown, the right one the strongest. "
                                    "Roughly: −60 dBm is close by in the same room, −80 the next room, −90 and weaker "
                                    "far away. Below the top, the right handle also hides devices right next to "
                                    "you, like your own.")
        self.near_slider.valuesChanged.connect(self._set_near)
        self.near_value = label(self.near_text(), "muted")
        self.near_value.setFixedWidth(self.near_value.fontMetrics().horizontalAdvance(
            f"{dbm(-100)} to {dbm(-100)} dBm") + 4)
        self.near_slider.setEnabled(False)
        self.near_value.setEnabled(False)
        self.gone = QCheckBox("Show out of range")
        self.gone.setToolTip("Also list devices that haven't been heard for 30 seconds")
        self.gone.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.gone.toggled.connect(self._toggle_gone)
        self.clear_btn = link_button("Clear", "Forget the devices heard so far (pinned ones stay)")
        self.clear_btn.clicked.connect(self.clear)
        self.new_only = QCheckBox("New only")
        self.new_only.setToolTip("Everything heard so far counts as known, and the list shows only devices that "
                                 "turn up after that. Phones and earbuds change their address every 15 minutes "
                                 "or so: when Blue Station hears the change happen, or they advertise a name "
                                 "already known, they stay known. Otherwise they look new.")
        self.new_only.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.new_only.toggled.connect(self._toggle_new)
        self.new_bell = tool_button("bell", "A notification when a new device turns up (a near one, with Nearby "
                                            "only), also from the menu bar", 18)
        self.new_bell.setCheckable(True)
        self.new_bell.toggled.connect(self._toggle_new_bell)
        self.new_reset = link_button("Reset", "Start over: everything heard so far counts as known")
        self.new_reset.clicked.connect(self.reset_baseline)
        for w in (self.new_bell, self.new_reset):
            w.setEnabled(False)  # until New only is on; shown anyway, so nothing moves when it is

        controls = QVBoxLayout()
        controls.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(self.search, 1)
        top.addWidget(self.clear_btn)
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addStretch(1)
        new_group = QHBoxLayout()
        new_group.setSpacing(2)
        for w in (self.new_only, self.new_bell, self.new_reset):
            new_group.addWidget(w)
        bottom.addLayout(new_group)
        bottom.addWidget(self.nearby)
        bottom.addSpacing(-6)
        bottom.addWidget(self.near_slider)
        bottom.addWidget(self.near_value)
        bottom.addWidget(self.gone)
        controls.addLayout(top)
        controls.addLayout(bottom)
        row.addLayout(controls)
        return frame

    # ---- actions ----------------------------------------------------------------------

    def _filter(self, text: str) -> None:
        self.model.needle = text.strip()
        self.model.refresh(time.time(), force=True)

    def _toggle_new(self, on: bool) -> None:
        self.model.baseline = Baseline(time.time(), self.store.devices.values()) if on else None
        self.new_reset.setEnabled(on)
        self.new_bell.setEnabled(on)
        if not on:
            self.new_bell.setChecked(False)
        self.model.refresh(time.time(), force=True)
        if on:
            self.message.emit(f"New only: the {len(self.store.devices)} devices heard so far count as known.")

    def reset_baseline(self) -> None:
        if self.new_only.isChecked():
            self._toggle_new(True)

    def _toggle_new_bell(self, on: bool) -> None:
        refresh_tool_icons(self.new_bell, color_key="accent" if on else "muted")
        if not self.new_only.isChecked():
            return
        self.message.emit("You'll get a notification when a new device turns up." if on
                          else "No notifications for new devices.")

    def arrivals(self, now: float) -> list[Device]:
        """New devices to announce, each once, when New only's bell is on.
        Checked every tick, so it works from the menu bar too."""
        baseline = self.model.baseline
        if baseline is None:
            return []
        due = baseline.arrivals(now, list(self.store.devices.values()),
                                self.model.near if self.model.nearby_only else None)
        return due if self.new_bell.isChecked() else []  # noticed either way, so turning it on doesn't flood

    def _toggle_gone(self, on: bool) -> None:
        self.model.show_gone = on
        self.model.refresh(time.time(), force=True)

    def _toggle_nearby(self, on: bool) -> None:
        self.model.nearby_only = on
        self.near_slider.setEnabled(on)
        self.near_value.setEnabled(on)
        self.model.refresh(time.time(), force=True)

    def near_text(self) -> str:
        low, high = self.near_slider.values()
        return f"{dbm(low)} dBm and up" if high >= NEAR_RANGE[1] else f"{dbm(low)} to {dbm(high)} dBm"

    def _set_near(self, low: int, high: int) -> None:
        self.model.near_dbm, self.model.near_max = low, high
        self.model._near.clear()  # the slack is for flicker at one setting, not across settings
        self.near_value.setText(self.near_text())
        self.model.refresh(time.time(), force=True)

    def clear(self) -> None:
        self.store.clear()
        self.model.refresh(time.time(), force=True)
        self.message.emit("Cleared. Pinned devices stay on the list.")

    def _toggle_pin(self, device: Device) -> None:
        device.pinned = not device.pinned
        self.pinChanged.emit(device)
        self.model.refresh(time.time(), force=not self.model.frozen)
        self.message.emit(f"Pinned {device.title}: it stays on top, and is remembered next time." if device.pinned
                          else f"Unpinned {device.title}.")

    def hideEvent(self, event) -> None:
        self.hover.hide()
        super().hideEvent(event)

    # ---- every tick --------------------------------------------------------------------

    def tick(self, now: float, state: str, error: str | None) -> None:
        self.model.frozen = self.table.underMouse()
        self.model.refresh(now)
        devices = list(self.store.devices.values())
        in_range = [d for d in devices if not d.gone(now)]
        total = len(in_range)
        self.count.setText(f"{total} device{'s' if total != 1 else ''} in range")
        parts = []
        strongest = max((d for d in in_range if d.smoothed is not None), key=lambda d: d.smoothed, default=None)
        if strongest:
            parts.append(f"strongest: {strongest.title} at {dbm(strongest.smoothed)} dBm")
        gone = len(devices) - total
        if gone:
            parts.append(f"{gone} out of range")
        pinned = sum(1 for d in devices if d.pinned)
        if pinned:
            parts.append(f"{pinned} pinned")
        far = sum(1 for d in in_range if not d.pinned and not self.model.near(d)) if self.model.nearby_only else 0
        if far:
            parts.insert(0, f"{far} hidden by Nearby only")  # before the rest: it explains the list
        if len(self.model.rows) != len(in_range) and self.model.needle:
            parts.insert(0, f"{len(self.model.rows)} match the filter")
        baseline = self.model.baseline
        if baseline is not None:
            since = datetime.fromtimestamp(baseline.since).strftime("%H:%M")
            parts.insert(0, f"{baseline.count_new(devices, now)} new since {since}")
        self.detail.setText("  ·  ".join(parts) or "Nothing heard yet")

        if self.model.rows:
            self.stack.setCurrentWidget(self.table)
        else:
            if state == "error" and error:
                text = error
            elif self.model.needle:
                text = "No device matches the filter."
            elif baseline is not None:
                text = (f"Nothing new since {datetime.fromtimestamp(baseline.since):%H:%M}. Devices that turn up "
                        "from now on show up here.")
            elif far:
                text = (f"Nothing at {self.near_text()}. Widen the range, or turn off Nearby only, to see the "
                        f"{far} outside it.")
            elif state == "starting":
                text = "Starting the scan…  If macOS asks whether Blue Station may use Bluetooth, allow it."
            elif state == "scanning":
                text = "Listening for Bluetooth devices…"
            else:
                text = "Press Scan (or Space) to look for Bluetooth devices nearby."
            self.empty.setText(text)
            self.stack.setCurrentWidget(self.empty)
        self.hover.tick(now)
