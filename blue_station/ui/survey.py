"""The Survey job: checking beacons and mapping coverage. The list keeps
every beacon heard this session, so a silent one shows up as missing.
The map is a floor plan (or a blank grid): click where you stand, hold
still for five seconds, move on. It can show one device's signal, the
strongest of them, or how many are usable at each spot."""
import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QMenu, QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from blue_station.core.devices import Device, DeviceStore
from blue_station.core.survey import COUNT, DEVICE, HOLD, STRONGEST, USABLE, Survey
from blue_station.ui.devices import DeviceModel, DeviceTable, HoverCards
from blue_station.ui.theme import colors
from blue_station.ui.widgets import (
    MINUS, Segmented, card, dbm, label, link_button, ramp, refresh_tool_icons, signal_stops, tool_button,
)

MODES = [DEVICE, STRONGEST, COUNT]
GRID_COLUMNS = 72
BLANK_ASPECT = 0.7  # height over width of the grid used when there's no floor plan


def count_stops(c: dict) -> list[tuple[float, str]]:
    return [(0, c["danger"]), (1, c["warning"]), (2, c["accent"]), (3, c["success"])]


class SurveyCanvas(QWidget):
    """The plan, the colored estimate over it, and the points."""
    clicked = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plan: QImage | None = None
        self.values: list[tuple[float, float, float | None]] = []
        self.mode = DEVICE
        self.heat: QImage | None = None
        self.pending: tuple[float, float, float] | None = None  # x, y, seconds left
        self.setMinimumSize(QSize(420, 300))
        self.setCursor(Qt.CursorShape.CrossCursor)

    @property
    def aspect(self) -> float:
        if self.plan is None or self.plan.isNull():
            return BLANK_ASPECT
        return self.plan.height() / self.plan.width()

    def plan_rect(self) -> QRectF:
        area = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        width = min(area.width(), area.height() / self.aspect)
        height = width * self.aspect
        return QRectF(area.center().x() - width / 2, area.center().y() - height / 2, width, height)

    def mousePressEvent(self, event) -> None:
        rect = self.plan_rect()
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton and rect.contains(pos):
            self.clicked.emit((pos.x() - rect.left()) / rect.width(), (pos.y() - rect.top()) / rect.height())

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        self.paint_map(p, self.plan_rect(), live=True)
        p.end()

    def paint_map(self, p: QPainter, rect: QRectF, live: bool = False) -> None:
        c = colors()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.plan is not None and not self.plan.isNull():
            p.fillRect(rect, QColor("#ffffff"))
            p.drawImage(rect, self.plan)
        else:
            p.fillRect(rect, QColor(c["surface"]))
            p.setPen(QPen(QColor(c["border"]), 1))
            step = rect.width() / 20
            x = rect.left()
            while x <= rect.right() + 0.5:
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                x += step
            y = rect.top()
            while y <= rect.bottom() + 0.5:
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                y += step
        if self.heat is not None:
            p.drawImage(rect, self.heat)
        p.setPen(QPen(QColor(c["border_strong"]), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

        stops = count_stops(c) if self.mode == COUNT else signal_stops(c)
        font = p.font()
        font.setPixelSize(max(10, round(rect.width() / 90)))
        font.setBold(True)
        p.setFont(font)
        radius = max(5.0, rect.width() / 150)
        for x, y, v in self.values:
            at = QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())
            fill = QColor(c["faint"]) if v is None else ramp(v, stops)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(fill)
            p.drawEllipse(at, radius, radius)
            text = "—" if v is None else f"{v:.0f}" if self.mode == COUNT else dbm(v)
            box = QRectF(at.x() + radius + 3, at.y() - 9, 60, 18)
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(box.translated(1, 1), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if live and self.pending is not None:
            x, y, left = self.pending
            at = QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())
            phase = (time.monotonic() % 1.0)
            ring = QColor(c["accent"])
            ring.setAlphaF(0.8 * (1 - phase))
            p.setPen(QPen(ring, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(at, radius + 4 + 12 * phase, radius + 4 + 12 * phase)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c["accent"]))
            p.drawEllipse(at, radius, radius)
            text = f"Hold still… {max(1, round(left))}"
            width = p.fontMetrics().horizontalAdvance(text) + 16
            box = QRectF(at.x() - width / 2, at.y() - radius - 30, width, 22)
            p.setBrush(QColor(c["raised"]))
            p.setPen(QColor(c["border_strong"]))
            p.drawRoundedRect(box, 6, 6)
            p.setPen(QColor(c["ink"]))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def render(self) -> QImage:
        """The map at the plan's own size (or 1600 px wide on the grid), for export."""
        if self.plan is not None and not self.plan.isNull():
            size = self.plan.size()
            if size.width() < 1200:
                size = QSize(1200, round(1200 * self.aspect))
        else:
            size = QSize(1600, round(1600 * BLANK_ASPECT))
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(colors()["bg"]))
        p = QPainter(image)
        self.paint_map(p, QRectF(0, 0, size.width(), size.height()))
        p.end()
        return image


class Legend(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = DEVICE
        self.setFixedSize(260, 30)

    def paintEvent(self, _event) -> None:
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(0, 4, self.width(), 8)
        if self.mode == COUNT:
            stops, low, high, labels = count_stops(c), 0, 3, ["none", "1", "2", "3 or more"]
        else:
            stops, low, high = signal_stops(c), -95, -58
            labels = [f"{MINUS}95", f"{MINUS}82", f"{MINUS}70", f"{MINUS}58 dBm"]
        steps = 60
        for i in range(steps):
            v = low + (high - low) * i / (steps - 1)
            p.fillRect(QRectF(bar.left() + bar.width() * i / steps, bar.top(), bar.width() / steps + 1, bar.height()),
                       ramp(v, stops))
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        p.setPen(QColor(c["faint"]))
        for i, text in enumerate(labels):
            x = bar.width() * i / (len(labels) - 1)
            flags = Qt.AlignmentFlag.AlignLeft if i == 0 else (
                Qt.AlignmentFlag.AlignRight if i == len(labels) - 1 else Qt.AlignmentFlag.AlignHCenter)
            box = QRectF(x - (0 if i == 0 else 70 if i == len(labels) - 1 else 35), bar.bottom() + 2, 70, 14)
            p.drawText(box, flags, text)
        p.end()


class SurveyPage(QWidget):
    track = Signal(object)  # Device
    message = Signal(str)

    def __init__(self, store: DeviceStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.survey = Survey()
        self.mode = DEVICE
        self.target: Device | None = None
        self._heat_key = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_list())
        layout.addWidget(self._build_map(), 1)

    def _build_list(self):
        frame = card()
        frame.setFixedWidth(380)
        box = QVBoxLayout(frame)
        box.setContentsMargins(1, 12, 1, 1)
        box.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(15, 0, 14, 0)
        self.list_title = label("BEACONS", "caps")
        head.addWidget(self.list_title)
        head.addStretch(1)
        self.beacons_only = QCheckBox("Beacons only")
        self.beacons_only.setChecked(True)
        self.beacons_only.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.beacons_only.setToolTip("List only iBeacons and Eddystone beacons. Off: every device, for surveying "
                                     "sensors and other things too.")
        self.beacons_only.toggled.connect(self._scope)
        head.addWidget(self.beacons_only)
        box.addLayout(head)
        self.model = DeviceModel(self.store, self)
        self.model.show_gone = True  # a beacon that went quiet is exactly what a survey wants to see
        self.model.predicate = lambda d: d.info.beacon
        self.table = DeviceTable(self.model, picker=True)
        self.table.picked.connect(self._picked)
        self.table.opened.connect(self.track.emit)
        self.hover = HoverCards(self.table, "Click to map it  ·  double-click to track it")
        box.addWidget(self.table, 1)
        self.list_note = label("", "faint")
        self.list_note.setWordWrap(True)
        self.list_note.setContentsMargins(15, 4, 14, 10)
        box.addWidget(self.list_note)
        return frame

    def _build_map(self):
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.modes = Segmented(["Picked device", "Strongest", "How many"], [
            "The signal of the device picked in the list",
            "The strongest signal of the listed devices at each spot",
            f"How many of the listed devices are heard at {MINUS}{abs(USABLE)} dBm or better. Three or more "
            "is what indoor positioning usually needs."])
        self.modes.changed.connect(self._mode)
        bar.addWidget(self.modes)
        bar.addStretch(1)
        self.plan_btn = QPushButton("Load floor plan…")
        self.plan_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.plan_btn.setToolTip("An image of the space: a floor plan, a sketch, a photo of the fire escape map")
        self.plan_btn.clicked.connect(self.load_plan_dialog)
        self.undo_btn = link_button("Undo", "Take back the last point (⌘Z)")
        self.undo_btn.clicked.connect(self.undo)
        self.clear_btn = link_button("Clear", "Remove every point")
        self.clear_btn.clicked.connect(self.clear)
        self.export_btn = tool_button("export", "Export the map or the points (⌘E)", 18)
        self.export_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        menu.addAction("Map as PNG…", self.export_png_dialog)
        menu.addAction("Points as CSV…", self.export_csv_dialog)
        self.export_btn.setMenu(menu)
        for w in (self.plan_btn, self.undo_btn, self.clear_btn, self.export_btn):
            bar.addWidget(w)
        box.addLayout(bar)
        self.canvas = SurveyCanvas()
        self.canvas.clicked.connect(self.add_point)
        box.addWidget(self.canvas, 1)
        foot = QHBoxLayout()
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        foot.addWidget(self.status, 1)
        self.legend = Legend()
        foot.addWidget(self.legend, 0, Qt.AlignmentFlag.AlignBottom)
        box.addLayout(foot)
        return frame

    # ---- actions -----------------------------------------------------------------------

    def _scope(self, beacons: bool) -> None:
        self.model.predicate = (lambda d: d.info.beacon) if beacons else None
        self.list_title.setText("BEACONS" if beacons else "DEVICES")
        self.model.refresh(time.time(), force=True)
        self._heat_key = None

    def _picked(self, device: Device | None) -> None:
        self.target = device
        self.modes.set_current(0)
        self.mode = DEVICE
        self._heat_key = None
        self.tick(time.time())

    def _mode(self, index: int) -> None:
        self.mode = MODES[index]
        self._heat_key = None
        self.tick(time.time())

    def add_point(self, x: float, y: float) -> None:
        self.survey.start_point(x, y, time.time())
        self.tick(time.time())

    def undo(self) -> None:
        if self.survey.undo():
            self._heat_key = None
            self.tick(time.time())

    def clear(self) -> None:
        if self.survey.points and QMessageBox.question(
                self, "Clear", f"Remove all {len(self.survey.points)} points?") != QMessageBox.StandardButton.Yes:
            return
        self.survey.clear()
        self._heat_key = None
        self.tick(time.time())

    def load_plan_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load floor plan", str(Path.home()),
                                              "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        if path:
            self.load_plan(Path(path))

    def load_plan(self, path: Path) -> bool:
        image = QImage(str(path))
        if image.isNull():
            self.message.emit(f"Couldn't open {path.name} as an image.")
            return False
        self.canvas.plan = image
        self._heat_key = None
        self.message.emit(f"Loaded {path.name}. Click where you're standing to add a point.")
        self.tick(time.time())
        return True

    def export_png_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export map", str(Path.home() / "Coverage map.png"), "PNG (*.png)")
        if path:
            self.canvas.render().save(path)
            self.message.emit(f"Saved the map to {Path(path).name}.")

    def export_csv_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export points", str(Path.home() / "Survey points.csv"),
                                              "CSV (*.csv)")
        if path:
            self.export_csv(Path(path))

    def export_csv(self, path: Path) -> None:
        count = self.survey.export(path, {a: d.title for a, d in self.store.devices.items()})
        self.message.emit(f"Exported {count} points to {path.name}.")

    def export_dialog(self) -> None:
        self.export_csv_dialog()

    def refresh_icons(self) -> None:
        refresh_tool_icons(self.export_btn)
        self._heat_key = None

    # ---- every tick -------------------------------------------------------------------

    def listed(self) -> set[str]:
        return {a for a, d in self.store.devices.items() if self.model.predicate is None or self.model.predicate(d)}

    def _heat(self, values) -> QImage | None:
        if len(values) < 2:
            return None
        c = colors()
        stops = count_stops(c) if self.mode == COUNT else signal_stops(c)
        cols = GRID_COLUMNS
        rows = max(2, round(cols * self.canvas.aspect))
        grid = Survey.grid(values, cols, rows, self.canvas.aspect, reach=0.16)
        image = QImage(cols, rows, QImage.Format.Format_ARGB32)
        for r, row in enumerate(grid):
            for col, (value, nearest) in enumerate(row):
                color = ramp(value, stops)
                fade = min(1.0, max(0.0, 1 - (nearest - 0.14) / 0.2))  # fade out away from what was measured
                color.setAlphaF(0.68 * fade)
                image.setPixelColor(col, r, color)
        return image

    def tick(self, now: float) -> None:
        self.model.refresh(now)
        self.hover.tick(now)
        listed = self.listed()
        target = self.target.address if self.target else None
        values = self.survey.values(self.mode, target, listed)
        key = (len(self.survey.points), self.mode, target, len(listed), self.canvas.aspect, colors()["bg"])
        if key != self._heat_key:
            self._heat_key = key
            self.canvas.heat = self._heat(values)
        self.canvas.values = values
        self.canvas.mode = self.legend.mode = self.mode
        p = self.survey.pending
        self.canvas.pending = (p.x, p.y, HOLD - (now - p.t)) if p else None
        self.canvas.update()
        self.legend.update()

        n = len(self.survey.points)
        if self.mode == DEVICE and self.target is None:
            text = "Pick a device in the list to map its signal, or switch to Strongest or How many."
        elif n == 0:
            text = "Click where you're standing, then hold still for five seconds. Repeat around the space."
        else:
            text = f"{n} point{'s' if n != 1 else ''}"
            if self.mode == DEVICE:
                heard = sum(1 for *_, v in values if v is not None)
                text += f"  ·  {self.target.title} heard at {heard}"
            text += ".  Click to add another."
        if p:
            text = "Measuring… hold still."
        self.status.setText(text)
        beacons = [self.store.devices[a] for a in listed]
        missing = [d for d in beacons if d.gone(now)]
        noun = "beacon" if self.beacons_only.isChecked() else "device"
        note = f"{len(beacons)} {noun}{'s' if len(beacons) != 1 else ''} heard this session"
        if missing:
            note += f", {len(missing)} gone quiet"
        self.list_note.setText(note + ".  Click one to map it, double-click to track it.")
        self.undo_btn.setEnabled(bool(n or p))
        self.clear_btn.setEnabled(bool(n))

