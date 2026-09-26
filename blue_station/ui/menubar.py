"""Blue Station in the menu bar. Closing the window leaves the tracker
watch and your alerts running behind a small icon there, which shows how
many devices may be following you. Its menu opens the window again, and
quits."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from blue_station.core import login
from blue_station.ui import icons

SIZE = 18  # points: the height of a menu bar icon


def menu_bar_icon(following: int, paused: bool, dark: bool = False) -> QIcon:
    """The app icon's shape: a rounded square with the waves cut out, faint
    while paused, as a template that takes the menu bar's own color. With
    something following you, a red badge with the count; then the icon
    keeps its colors, so the square is drawn for a light or dark menu bar."""
    dpr = 2.0
    image = QPixmap(round(SIZE * dpr), round(SIZE * dpr))
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff" if following and dark else "#000000"))
    p.drawRoundedRect(QRectF(1, 1, SIZE - 2, SIZE - 2), 4.5, 4.5)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    p.drawPixmap(QRectF(2, 2, SIZE - 4, SIZE - 4), icons.pixmap("generic", "#000000", SIZE - 4, dpr),
                 QRectF(0, 0, (SIZE - 4) * dpr, (SIZE - 4) * dpr))
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    if following:
        badge = QRectF(SIZE - 10, 0, 10, 10)
        p.setBrush(QColor("#ff453a"))
        p.drawEllipse(badge)
        font = QFont()
        font.setPixelSize(7 if following < 10 else 5)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        p.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(following) if following < 10 else "9+")
    p.end()
    if paused:  # the whole icon faint
        faint = QPixmap(image.size())
        faint.setDevicePixelRatio(dpr)
        faint.fill(Qt.GlobalColor.transparent)
        q = QPainter(faint)
        q.setOpacity(0.45)
        q.drawPixmap(0, 0, image)
        q.end()
        image = faint
    icon = QIcon(image)
    icon.setIsMask(not following)
    return icon


class MenuBar(QSystemTrayIcon):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        menu = QMenu()
        self.status = menu.addAction("Nothing is following you")
        self.status.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Open Blue Station", window.bring_back)
        self.scan_action = menu.addAction("Pause scanning", window.toggle_scan)
        if login.supported():
            menu.addSeparator()
            self.login_action = menu.addAction("Start at login")
            self.login_action.setCheckable(True)
            self.login_action.toggled.connect(window.set_start_at_login)
        else:
            self.login_action = None
        menu.addSeparator()
        menu.addAction("Quit Blue Station", window.quit_app)
        self.menu = menu  # the menu bar doesn't keep it alive
        self.setContextMenu(menu)
        self.activated.connect(self._clicked)
        self._key = None
        self.show_state(0, "idle")

    def show_state(self, following: int, scanning: str) -> None:
        paused = scanning not in ("scanning", "starting")
        dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        key = (following, paused, dark)
        if key == self._key:
            return
        self._key = key
        self.setIcon(menu_bar_icon(following, paused, dark))
        text = (f"{following} device{'s' if following != 1 else ''} may be following you" if following
                else "Nothing is following you")
        self.status.setText(text + ("  (paused)" if paused else ""))
        self.setToolTip(f"Blue Station: {text[0].lower()}{text[1:]}" + (", paused" if paused else ""))
        self.scan_action.setText("Scan" if paused else "Pause scanning")

    def set_login(self, on: bool) -> None:
        if self.login_action is not None:
            self.login_action.blockSignals(True)
            self.login_action.setChecked(on)
            self.login_action.blockSignals(False)

    def _clicked(self, reason) -> None:
        # on macOS a click opens the menu by itself; elsewhere a double-click opens the window
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.window.bring_back()
