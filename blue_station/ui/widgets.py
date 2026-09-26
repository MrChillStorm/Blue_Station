"""Small widgets and painting shared by both pages: signal bars, the
signal history chart, the hover card, stat tiles, the scan indicator."""
import html
import math
import time
from datetime import datetime

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from blue_station.core.decode import hex_bytes, short_company
from blue_station.core.devices import (
    RSSI_MAX, RSSI_MIN, SMOOTHING, Device, ago_text, distance_text, fraction, quality,
)
from blue_station.core import names
from blue_station.ui import icons
from blue_station.ui.theme import colors, signal_color

MINUS = "−"  # a real minus sign lines up with the digits


def dbm(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{MINUS}{abs(round(value))}" if round(value) < 0 else f"{round(value)}"


def esc(text) -> str:
    return html.escape(str(text))


def when(t: float) -> str:
    """'25.9. 14:31', the Finnish way."""
    d = datetime.fromtimestamp(t)
    return f"{d.day}.{d.month}. {d:%H:%M}"


def ramp(value: float, stops: list[tuple[float, str]]) -> QColor:
    """A color between the stops' colors, for maps and legends."""
    if value <= stops[0][0]:
        return QColor(stops[0][1])
    for (v0, c0), (v1, c1) in zip(stops, stops[1:]):
        if value <= v1:
            f = (value - v0) / (v1 - v0)
            a, b = QColor(c0), QColor(c1)
            return QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * f, a.greenF() + (b.greenF() - a.greenF()) * f,
                                   a.blueF() + (b.blueF() - a.blueF()) * f)
    return QColor(stops[-1][1])


def signal_stops(c: dict) -> list[tuple[float, str]]:
    return [(-95, c["danger"]), (-82, c["warning"]), (-70, c["accent"]), (-58, c["success"])]


def link_button(text: str, tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("link")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setToolTip(tooltip)
    return btn


def tool_button(icon_name: str, tooltip: str, size: int = 18) -> QToolButton:
    btn = QToolButton()
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setIconSize(QSize(size, size))
    btn.setProperty("icon_name", icon_name)
    return btn


def refresh_tool_icons(*buttons: QToolButton, color_key: str = "muted") -> None:
    c = colors()
    for b in buttons:
        b.setIcon(icons.icon(b.property("icon_name"), c[color_key], b.iconSize().width()))


class ChecklistMenu(QMenu):
    """A menu whose checkboxes don't close it, so you can tick several.
    Clicking outside or Esc closes it; other items work as usual."""

    def _tick(self, action) -> bool:
        if action is None or not action.isCheckable():
            return False
        if action.isEnabled():
            action.trigger()
        return True

    def mouseReleaseEvent(self, event) -> None:
        if not self._tick(self.actionAt(event.position().toPoint())):
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        keys = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
        if not (event.key() in keys and self._tick(self.activeAction())):
            super().keyPressEvent(event)


def card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


class ElidedLabel(QLabel):
    """One line that gives way: when the row runs short of room, it ends
    in … (the whole text is in its tooltip) instead of squeezing its
    neighbours."""

    def __init__(self, text: str = "", name: str | None = None):
        super().__init__(text)
        if name:
            self.setObjectName(name)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(policy.Policy.Ignored)
        self.setSizePolicy(policy)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setToolTip(text if self.fontMetrics().horizontalAdvance(text) > self.width() else "")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setToolTip(self.text() if self.fontMetrics().horizontalAdvance(self.text()) > self.width() else "")

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.drawText(self.contentsRect(), int(self.alignment()),
                   self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width()))
        p.end()


def label(text: str = "", name: str | None = None) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    return lab


def subtitle(device: Device) -> str:
    """'AirPods Pro · Apple': what it is and who made it, whatever the title
    already says."""
    parts = []
    kind = device.info.kind
    if kind and kind != device.title:
        parts.append(kind)
    if device.info.detail:
        parts.append(device.info.detail)
    vendor = short_company(device.info.vendor)
    if vendor and vendor not in device.title and vendor not in (kind or ""):
        parts.append(vendor)
    if parts:
        return "  ·  ".join(parts)
    if device.nickname or device.name:
        return "Kind and maker not advertised"
    return "No name advertised" if kind or vendor else "No name or maker advertised"


def advertisement_rows(device: Device) -> list[tuple[str, str]]:
    """Everything the advertisement says, as (label, value) pairs of plain text."""
    rows = [("Advertised name", device.name or "—"), ("Kind", device.info.kind or "Unknown"),
            ("Maker", device.info.vendor or "—"), ("Address", device.address)]
    if device.mac:
        rows.append(("Bluetooth address", device.mac))
    if device.earlier:  # the same device under earlier addresses (links.py)
        n, least = len(device.earlier), round(100 * min(p for _, p in device.earlier))
        since = datetime.fromtimestamp(device.first_seen).strftime("%H:%M")
        rows.append(("Address changes", f"Once since {since}, {least}\u00a0% sure" if n == 1  # 91 % kept together
                     else f"{n} times since {since}, each at least {least}\u00a0% sure"))
    if device.fingerprint_bytes:
        rows.append(("Recognized by", f"{device.fingerprint_bytes} bytes it keeps when it changes address"
                     + ("" if device.fingerprint_confirmed else " (seen through one change so far)")))
    if device.partner is not None:
        rows.append(("Also sends", f"{device.partner.info.kind or 'another advertisement'}, changing address with it"))
    rows.append(("Connectable", {True: "Yes", False: "No", None: "—"}[device.connectable]))
    if device.tx_power is not None:
        rows.append(("TX power", f"{device.tx_power} dBm"))
    for uuid in device.service_uuids:
        rows.append(("Service", names.service_label(uuid)))
    for cid, data in device.manufacturer_data.items():
        who = short_company(names.company(cid)) or "Unknown"
        rows.append(("Maker data", f"{who} (0x{cid:04X}): {hex_bytes(data) or '(empty)'}"))
    for uuid, data in device.service_data.items():
        who = names.service(uuid) or "Unknown"
        rows.append(("Service data", f"{who} ({names.pretty_uuid(uuid)}): {hex_bytes(data) or '(empty)'}"))
    return rows


def kv_html(rows: list[tuple[str, str]], c: dict, key_width: int = 130) -> str:
    """A two-column table of muted keys and selectable values, for QLabel.
    Its full width, so a long value wraps instead of running off the edge."""
    cells = "".join(
        f"<tr><td width='{key_width}' style='color:{c['muted']}; padding:2px 10px 2px 0'>{esc(k)}</td>"
        f"<td style='padding:2px 0'>{esc(v)}</td></tr>" for k, v in rows)
    return f"<table width='100%' cellspacing='0' cellpadding='0'>{cells}</table>"


# ---- painting -------------------------------------------------------------------

def paint_signal(p: QPainter, rect: QRectF, rssi: float | None, c: dict, peak: float | None = None) -> None:
    """A rounded track, filled as far as the signal reaches, in its quality color."""
    radius = rect.height() / 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(c["track"]))
    p.drawRoundedRect(rect, radius, radius)
    if rssi is not None:
        width = max(rect.height(), rect.width() * fraction(rssi))
        p.setBrush(QColor(signal_color(rssi, c)))
        p.drawRoundedRect(QRectF(rect.left(), rect.top(), width, rect.height()), radius, radius)
    if peak is not None:
        x = rect.left() + rect.width() * fraction(peak)
        p.setBrush(QColor(c["ink"]))
        p.drawRoundedRect(QRectF(min(x, rect.right() - 2) - 1, rect.top() - 3, 2, rect.height() + 6), 1, 1)


def smoothed_series(points: list[tuple[float, int]]) -> list[tuple[float, float]]:
    """The same smoothing as the live number, run over a stretch of history."""
    out, value, last = [], None, None
    for t, r in points:
        if value is None or not 0 <= t - last <= 10:  # a gap, or a clock that stepped back
            value = float(r)
        else:
            value += (1 - math.exp(-(t - last) / SMOOTHING)) * (r - value)
        last = t
        out.append((t, value))
    return out


class SignalMeter(QWidget):
    """The big bar on the tracking page, with its scale and a tick at the
    strongest signal of the last minute."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(40)
        self.rssi: float | None = None
        self.peak: float | None = None

    def set_values(self, rssi: float | None, peak: float | None) -> None:
        self.rssi, self.peak = rssi, peak
        self.update()

    def paintEvent(self, _event) -> None:
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(0, 4, self.width(), 14)
        paint_signal(p, bar, self.rssi, c, self.peak)
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        p.setPen(QColor(c["faint"]))
        for value in range(RSSI_MIN, RSSI_MAX + 1, 10):
            x = bar.width() * fraction(value)
            text = dbm(value)
            flags = Qt.AlignmentFlag.AlignLeft if value == RSSI_MIN else (
                Qt.AlignmentFlag.AlignRight if value == RSSI_MAX else Qt.AlignmentFlag.AlignHCenter)
            box = QRectF(x - (0 if value == RSSI_MIN else 40 if value == RSSI_MAX else 20), bar.bottom() + 5, 40, 14)
            p.drawText(box, flags | Qt.AlignmentFlag.AlignTop, text)
        p.end()


class RssiChart(QWidget):
    """Signal over the last minutes: each packet a dot in its quality
    color, the smoothed signal a line. Compact, it's the sparkline in the
    hover card; full size it has a scale and shows the packet under the
    pointer."""

    def __init__(self, compact: bool = False, parent=None):
        super().__init__(parent)
        self.compact = compact
        self.device: Device | None = None
        self.seconds = 60
        self.now = time.time()
        self._mouse: QPointF | None = None
        self.setMouseTracking(not compact)
        self.setMinimumHeight(60 if compact else 180)

    def show_device(self, device: Device | None, now: float) -> None:
        self.device, self.now = device, now
        self.update()

    def set_window(self, seconds: int) -> None:
        self.seconds = seconds
        self.update()

    def mouseMoveEvent(self, event) -> None:
        self._mouse = event.position()
        self.update()

    def leaveEvent(self, _event) -> None:
        self._mouse = None
        self.update()

    def paintEvent(self, _event) -> None:
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        left = 0 if self.compact else 42
        bottom = 0 if self.compact else 20
        plot = QRectF(left, 4, self.width() - left - 2, self.height() - bottom - 8)
        points = self.device.recent(self.now, self.seconds) if self.device else []
        low, high = RSSI_MIN, RSSI_MAX
        if points:
            low = min(low, min(r for _, r in points) - 2)
            high = max(high, max(r for _, r in points) + 2)

        def xy(t: float, r: float) -> QPointF:
            return QPointF(plot.left() + plot.width() * (1 - (self.now - t) / self.seconds),
                           plot.top() + plot.height() * (high - r) / (high - low))

        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        for value in range(-100, -20, 20 if self.compact else 10):
            if not low <= value <= high:
                continue
            y = xy(self.now, value).y()
            p.setPen(QPen(QColor(c["border"]), 1, Qt.PenStyle.SolidLine if value % 20 == 0 else Qt.PenStyle.DotLine))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            if not self.compact and value % 20 == 0:
                p.setPen(QColor(c["faint"]))
                p.drawText(QRectF(0, y - 7, left - 8, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           dbm(value))
        if not self.compact:
            p.setPen(QColor(c["faint"]))
            span = self.seconds // 60
            for i in range(5):
                x = plot.left() + plot.width() * i / 4
                ago = self.seconds * (4 - i) / 4
                text = "now" if ago == 0 else (f"{MINUS}{ago / 60:g} min" if span > 1 else f"{MINUS}{ago:.0f} s")
                flags = Qt.AlignmentFlag.AlignRight if i == 4 else (Qt.AlignmentFlag.AlignLeft if i == 0
                                                                     else Qt.AlignmentFlag.AlignHCenter)
                box = QRectF(x - (0 if i == 0 else 60 if i == 4 else 30), plot.bottom() + 5, 60, 14)
                p.drawText(box, flags, text)

        if not points:
            p.setPen(QColor(c["faint"]))
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, "No packets in this window")
            p.end()
            return
        p.setClipRect(plot.adjusted(-4, -4, 4, 4))
        dot = 1.6 if self.compact or len(points) > 600 else 2.3
        p.setPen(Qt.PenStyle.NoPen)
        for t, r in points:
            color = QColor(signal_color(r, c))
            color.setAlphaF(0.55)
            p.setBrush(color)
            p.drawEllipse(xy(t, r), dot, dot)
        path, previous = QPainterPath(), None
        for t, value in smoothed_series(points):
            if previous is None or not 0 <= t - previous <= 10:
                path.moveTo(xy(t, value))
            else:
                path.lineTo(xy(t, value))
            previous = t
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(c["ink"] if self.compact else c["accent"]), 1.6 if self.compact else 2))
        p.drawPath(path)

        if self._mouse is not None and plot.contains(self._mouse):
            t, r = min(points, key=lambda pt: abs(xy(*pt).x() - self._mouse.x()))
            at = xy(t, r)
            p.setClipping(False)
            p.setPen(QPen(QColor(c["muted"]), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(at.x(), plot.top()), QPointF(at.x(), plot.bottom()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(signal_color(r, c)))
            p.drawEllipse(at, 4.5, 4.5)
            text = f"{dbm(r)} dBm  ·  {ago_text(self.now - t)}"
            width = p.fontMetrics().horizontalAdvance(text) + 16
            box = QRectF(min(max(at.x() - width / 2, plot.left()), plot.right() - width), plot.top() + 2, width, 20)
            p.setBrush(QColor(c["raised"]))
            p.setPen(QColor(c["border_strong"]))
            p.drawRoundedRect(box, 5, 5)
            p.setPen(QColor(c["ink"]))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


class HoverCard(QFrame):
    """A tooltip-like card that follows the pointer over the device list:
    the details and a live sparkline of the last minute."""

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.box = QFrame()
        self.box.setObjectName("hovercard")
        outer.addWidget(self.box)
        layout = QVBoxLayout(self.box)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon = QLabel()
        heads = QVBoxLayout()
        heads.setSpacing(1)
        self.title = label("", "heading")
        self.sub = label("", "muted")
        self.sub.setWordWrap(True)
        heads.addWidget(self.title)
        heads.addWidget(self.sub)
        top.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(heads, 1)
        self.number = label("", "big")
        top.addWidget(self.number, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)
        self.chart = RssiChart(compact=True)
        self.chart.setFixedHeight(64)
        layout.addWidget(self.chart)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setWordWrap(True)  # the card is only so wide: long values take a second line
        layout.addWidget(self.details)
        self.hint = label("Click to track this device", "faint")
        layout.addWidget(self.hint)
        self.setFixedWidth(380)
        self.device: Device | None = None

    def show_device(self, device: Device, now: float, extra: list[tuple[str, str]] | None = None) -> None:
        """extra: a page's own rows, shown first."""
        c = colors()
        self.device = device
        self.icon.setPixmap(icons.pixmap(device.info.icon, c["accent"], 26))
        self.title.setText(device.title)
        self.sub.setText(subtitle(device))
        self.number.setText(f"<span style='color:{signal_color(device.smoothed, c)}'>{dbm(device.smoothed)}</span>"
                            f"<span style='color:{c['faint']}; font-size:12px'> dBm</span>")
        self.chart.show_device(device, now)
        stats = device.stats(now, 60)
        rows = list(extra or []) + [("Signal", f"{quality(device.smoothed)}" + (
                    f"  ·  {dbm(stats.low)} to {dbm(stats.high)} over 1 min" if stats else "")),
                ("Distance", f"{distance_text(device.distance())}  (rough)"),
                ("Last seen", ago_text(device.age(now))),
                ("Packets", f"{device.packets}  ·  {device.rate(now):.1f} per second")]
        address = device.mac or device.address
        rows.append(("Address", address if len(address) <= 20 else f"{address[:8]}…{address[-6:]}"))
        rows += [r for r in advertisement_rows(device)
                 if r[0] in ("Address changes", "Recognized by", "Also sends", "Connectable", "TX power",
                             "Service")][:6]
        rows += device.info.fields[:6]
        self.details.setText(kv_html(rows, c, 118))  # room for "Address changes" on one line
        # wrapped lines need height the card wouldn't reserve by itself: as tall as they are at its width
        inner = self.width() - 2 * 14 - 2
        self.details.ensurePolished()  # measured in the style's font, not the default one it has before showing
        self.details.setMinimumHeight(self.details.heightForWidth(inner))
        self.adjustSize()

    def place(self, pos: QPoint) -> None:
        """Beside the pointer, kept on the screen."""
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x, y = pos.x() + 18, pos.y() + 14
        if x + self.width() > area.right():
            x = pos.x() - self.width() - 12
        if y + self.height() > area.bottom():
            y = area.bottom() - self.height()
        self.move(max(area.left(), x), max(area.top(), y))


class StatTile(QFrame):
    def __init__(self, caption: str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setToolTip(tooltip)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(2)
        self.caption = label(caption.upper(), "caps")
        self.value = label("—", "heading")
        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set(self, text: str) -> None:
        self.value.setText(text)


class ScanIndicator(QWidget):
    """A dot that pulses while scanning, with a word beside it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state, self.text = "idle", "Paused"
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)
        self.setMinimumWidth(120)
        self.setFixedHeight(24)

    def set_state(self, state: str, text: str) -> None:
        if (state, text) == (self.state, self.text):
            return
        self.state, self.text = state, text
        self._timer.start() if state in ("scanning", "starting") else self._timer.stop()
        self.setToolTip(text)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(22 + self.fontMetrics().horizontalAdvance(self.text) + 6, 24)

    def paintEvent(self, _event) -> None:
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = {"scanning": c["success"], "starting": c["warning"], "error": c["danger"]}.get(self.state, c["faint"])
        centre = QPointF(8, self.height() / 2)
        if self.state in ("scanning", "starting"):
            phase = (time.monotonic() % 1.6) / 1.6
            ring = QColor(color)
            ring.setAlphaF(0.5 * (1 - phase))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ring)
            p.drawEllipse(centre, 4 + 5 * phase, 4 + 5 * phase)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(centre, 4, 4)
        p.setPen(QColor(c["muted"]))
        text_rect = QRect(22, 0, self.width() - 22, self.height())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   p.fontMetrics().elidedText(self.text, Qt.TextElideMode.ElideRight, text_rect.width()))
        p.end()


class RangeSlider(QWidget):
    """A slider with two handles, for a low and a high end. The handles
    can't cross, and clicking the groove moves the nearer one there."""
    valuesChanged = Signal(int, int)
    HANDLE = 14

    def __init__(self, minimum: int, maximum: int, low: int, high: int, parent=None):
        super().__init__(parent)
        self.minimum, self.maximum = minimum, maximum
        self.low, self.high = low, high
        self._dragging: str | None = None
        self.setFixedHeight(20)
        self.setMinimumWidth(80)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(120, 20)

    def setValues(self, low: int, high: int) -> None:
        low = max(self.minimum, min(self.maximum, int(low)))
        high = max(low, min(self.maximum, int(high)))
        if (low, high) != (self.low, self.high):
            self.low, self.high = low, high
            self.update()
            self.valuesChanged.emit(low, high)

    def values(self) -> tuple[int, int]:
        return self.low, self.high

    def _span(self) -> tuple[float, float]:
        half = self.HANDLE / 2
        return half, self.width() - half

    def _x(self, value: int) -> float:
        left, right = self._span()
        return left + (value - self.minimum) / (self.maximum - self.minimum) * (right - left)

    def _value(self, x: float) -> int:
        left, right = self._span()
        share = min(1.0, max(0.0, (x - left) / max(1.0, right - left)))
        return round(self.minimum + share * (self.maximum - self.minimum))

    def paintEvent(self, event) -> None:
        c = colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid = self.height() / 2
        left, right = self._span()
        on = self.isEnabled()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c["border"]))
        p.drawRoundedRect(QRectF(left, mid - 2, right - left, 4), 2, 2)
        p.setBrush(QColor(c["accent"] if on else c["faint"]))
        p.drawRoundedRect(QRectF(self._x(self.low), mid - 2, self._x(self.high) - self._x(self.low), 4), 2, 2)
        for value in (self.low, self.high):
            p.drawEllipse(QPointF(self._x(value), mid), self.HANDLE / 2, self.HANDLE / 2)
        p.end()

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        # the nearer handle; when they sit together, the side you pressed on decides
        low_gap, high_gap = abs(x - self._x(self.low)), abs(x - self._x(self.high))
        if low_gap == high_gap:
            self._dragging = "low" if x < self._x(self.low) else "high"
        else:
            self._dragging = "low" if low_gap < high_gap else "high"
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging is None:
            return
        value = self._value(event.position().x())
        if self._dragging == "low":
            self.setValues(min(value, self.high), self.high)
        else:
            self.setValues(self.low, max(value, self.low))

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = None


class Segmented(QFrame):
    """A row of mutually exclusive buttons."""
    changed = Signal(int)

    def __init__(self, labels: list[str], tooltips: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("segments")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        for i, text in enumerate(labels):
            btn = QPushButton(text)
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if tooltips:
                btn.setToolTip(tooltips[i])
            self.group.addButton(btn, i)
            layout.addWidget(btn)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(self.changed.emit)

    def set_current(self, i: int) -> None:
        self.group.button(i).setChecked(True)

    def button(self, i: int) -> QPushButton:
        return self.group.button(i)


def bold(font: QFont, weight=QFont.Weight.DemiBold) -> QFont:
    font = QFont(font)
    font.setWeight(weight)
    return font
