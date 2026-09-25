"""The Trackers job: item trackers around you (AirTags and other Find My
devices away from their owner, Tile, SmartTag, Chipolo, Google's tags)
and whether one is following you. The watching itself runs whichever job
is on screen; this page shows it."""
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QMessageBox, QPushButton, QStackedLayout, QStyledItemDelegate,
    QTableView, QVBoxLayout, QWidget,
)

from blue_station.core.devices import DeviceStore, ago_text, span_text
from blue_station.core.watch import FOLLOWING, STAYING, TrackerRecord, Watcher
from blue_station.ui import icons
from blue_station.ui.theme import colors
from blue_station.ui.widgets import bold, card, dbm, label, link_button, paint_signal, subtitle, when

TRACKER, SIGNAL, WITH_YOU, PLACES, VERDICT = range(5)
HEADERS = ["TRACKER", "SIGNAL", "WITH YOU", "PLACES", "VERDICT"]
RECORD_ROLE = Qt.ItemDataRole.UserRole + 1
VERDICTS = {FOLLOWING: ("Following you", "danger"), STAYING: ("Staying near you", "warning"),
            "passing": ("Passing by", "muted")}
_ORDER = {FOLLOWING: 0, STAYING: 1, "passing": 2}

EXPLAIN = ("A tracker counts as following you once it has been with you in two different places. Places are "
           "recognized from named devices that stay put (TVs, printers, speakers), which takes a few minutes after "
           "you arrive. You can also say so yourself with I've moved.")


class TrackerModel(QAbstractTableModel):
    def __init__(self, watcher: Watcher, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.watcher, self.store = watcher, store
        self.rows: list[TrackerRecord] = []
        self.now = time.time()

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
        if role == Qt.ItemDataRole.DisplayRole:
            return [record.kind, None, span_text(record.seen_seconds), str(len(record.places)),
                    VERDICTS[record.verdict][0]][index.column()]
        return None

    def refresh(self, now: float) -> None:
        self.now = now
        order = sorted(self.watcher.trackers.values(), key=lambda r: (_ORDER[r.verdict], -r.last_seen))
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
            p.drawPixmap(int(rect.left() + 12), int(rect.center().y() - 11),
                         icons.pixmap("tag", c[key] if key != "muted" else c["accent"], 22))
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
        self.forget_btn = link_button("Forget history", "Forget every tracker and place seen so far")
        self.forget_btn.clicked.connect(self.forget)
        row.addWidget(self.moved_btn)
        row.addWidget(self.forget_btn)
        layout.addWidget(status)

        table_card = card()
        box = QVBoxLayout(table_card)
        box.setContentsMargins(1, 1, 1, 1)
        box.setSpacing(0)
        self.stack = QStackedLayout()
        self.model = TrackerModel(watcher, store, self)
        self.table = TrackerTable(self.model)
        self.table.opened.connect(self._open)
        self.empty = label("No item trackers heard yet. Blue Station keeps watching in the background, whichever "
                           "job you're in.", "empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        box.addLayout(self.stack, 1)
        explain = label(EXPLAIN, "faint")
        explain.setWordWrap(True)
        explain.setContentsMargins(16, 8, 16, 10)
        box.addWidget(explain)
        layout.addWidget(table_card, 1)

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
        following = self.watcher.following()
        if following:
            n = len(following)
            self.headline.setText(f"<span style='color:{c['danger']}'>{n} tracker{'s' if n > 1 else ''} may be "
                                  f"following you</span>")
        else:
            self.headline.setText(f"<span style='color:{c['success']}'>Nothing is following you</span>")
        heard = sum(1 for r in self.watcher.trackers.values()
                    if (d := self.store.devices.get(r.address)) is not None and not d.gone(now))
        parts = [f"{len(self.watcher.trackers)} tracker{'s' if len(self.watcher.trackers) != 1 else ''} seen, "
                 f"{heard} around you now"]
        settled, current = self.watcher.settled, self.watcher.current
        if settled is not None:
            marks = len(settled.landmarks)
            parts.append(f"at place {settled.id} since {when(settled.first_seen)}"
                         + (f", known by {marks} landmark{'s' if marks != 1 else ''}" if marks else ", learning it"))
        elif current is not None:
            parts.append("checking whether you've moved…")
        else:
            parts.append("learning where you are (it needs two named devices that stay put)")
        self.detail.setText("  ·  ".join(parts))
        self.stack.setCurrentWidget(self.table if self.model.rows else self.empty)
