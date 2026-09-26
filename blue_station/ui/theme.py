"""Color tokens and the stylesheet: Timeless's palette. Dark is the
default; the gear menu can pick Light or follow the OS. Painted widgets
read colors() as they paint, so a switch repaints correctly once apply()
has refreshed everything else."""
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

from blue_station.ui import icons

DARK = {
    "bg": "#0f1115", "panel": "#161920", "surface": "#14171d", "raised": "#1c2029",
    "border": "#252a34", "border_strong": "#343a46", "input_border": "#3a404c",
    "ink": "#e7e9ee", "muted": "#9aa1ad", "faint": "#5f6672",
    "accent": "#8f97ff", "accent_strong": "#6d74f7", "on_accent": "#ffffff",
    "hover": "#1d212b", "tint": "#1d2140", "track": "#262b36",
    "warning": "#f2b04a", "danger": "#ff6b6b", "success": "#4cc38a",
    "check_bg": "#6d74f7", "check_mark": "#ffffff", "check_border": "#5f6672",
}
LIGHT = {
    "bg": "#f3f4f7", "panel": "#ffffff", "surface": "#ffffff", "raised": "#f7f8fa",
    "border": "#e4e7ec", "border_strong": "#cfd4dc", "input_border": "#c9ced8",
    "ink": "#1b1e24", "muted": "#5f6672", "faint": "#a6acb6",
    "accent": "#4f46e5", "accent_strong": "#4f46e5", "on_accent": "#ffffff",
    "hover": "#f1f2f6", "tint": "#eef0ff", "track": "#e6e8ee",
    "warning": "#b36b00", "danger": "#c62828", "success": "#1f8a4c",
    "check_bg": "#4f46e5", "check_mark": "#ffffff", "check_border": "#a6acb6",
}

APPEARANCES = {"dark": "Dark", "light": "Light", "system": "Follow System"}
DEFAULT_APPEARANCE = "dark"
_forced: str | None = DEFAULT_APPEARANCE


def set_appearance(appearance: str) -> None:
    """'dark', 'light', or 'system' to follow the OS; anything else is dark."""
    global _forced
    _forced = None if appearance == "system" else appearance if appearance in APPEARANCES else DEFAULT_APPEARANCE


def appearance() -> str:
    return _forced or "system"


def _os_is_dark() -> bool:
    scheme = QGuiApplication.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:
        return scheme == Qt.ColorScheme.Dark
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightnessF() < 0.5


def colors() -> dict:
    if _forced:
        return DARK if _forced == "dark" else LIGHT
    return DARK if _os_is_dark() else LIGHT


def signal_color(rssi: float | None, c: dict | None = None) -> str:
    """Green, blue, amber, red: excellent, good, fair, weak."""
    c = c or colors()
    if rssi is None:
        return c["faint"]
    return c["success"] if rssi >= -55 else c["accent"] if rssi >= -67 else c["warning"] if rssi >= -80 else c["danger"]


# stylesheet sub-controls (checkbox ticks) only take image files
_GLYPH_DIR = Path(tempfile.gettempdir()) / "blue-station-glyphs"


def _glyph(name: str, color: str) -> str:
    path = _GLYPH_DIR / f"{name}-{color.lstrip('#')}.svg"
    if not path.exists():
        _GLYPH_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(icons.svg(name, color))
    return path.as_posix()


def _palette(c: dict) -> QPalette:
    p = QPalette()
    for role, key in {
        QPalette.ColorRole.Window: "bg", QPalette.ColorRole.WindowText: "ink", QPalette.ColorRole.Base: "surface",
        QPalette.ColorRole.AlternateBase: "panel", QPalette.ColorRole.Text: "ink", QPalette.ColorRole.Button: "panel",
        QPalette.ColorRole.ButtonText: "ink", QPalette.ColorRole.BrightText: "on_accent",
        QPalette.ColorRole.Highlight: "accent_strong", QPalette.ColorRole.HighlightedText: "on_accent",
        QPalette.ColorRole.ToolTipBase: "raised", QPalette.ColorRole.ToolTipText: "ink",
        QPalette.ColorRole.PlaceholderText: "faint", QPalette.ColorRole.Link: "accent",
        QPalette.ColorRole.Light: "hover", QPalette.ColorRole.Midlight: "border", QPalette.ColorRole.Mid: "border_strong",
        QPalette.ColorRole.Dark: "border_strong", QPalette.ColorRole.Shadow: "bg",
    }.items():
        p.setColor(role, QColor(c[key]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["faint"]))
    return p


def stylesheet(c: dict) -> str:
    check = _glyph("check", c["on_accent"])
    return f"""
    QMainWindow, QDialog {{ background: {c['bg']}; }}
    QWidget {{ color: {c['ink']}; font-size: 13px; }}
    QLabel {{ background: transparent; }}
    QLabel#title {{ font-size: 17px; font-weight: 600; }}
    QLabel#big {{ font-size: 22px; font-weight: 600; }}
    QLabel#huge {{ font-size: 44px; font-weight: 600; }}
    QLabel#muted {{ color: {c['muted']}; }}
    QLabel#faint {{ color: {c['faint']}; }}
    QLabel#heading {{ font-size: 15px; font-weight: 600; }}
    QLabel#caps {{ color: {c['muted']}; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }}
    QLabel#empty {{ color: {c['muted']}; font-size: 14px; padding: 28px; }}
    QLabel#note {{
        background: {c['tint']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 9px 11px;
    }}
    QLabel#warn {{ color: {c['warning']}; }}
    QFrame#card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QFrame#card QLabel, QFrame#card QCheckBox {{ background: transparent; }}
    QFrame#tile {{ background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 8px; }}
    QFrame#header {{ background: {c['panel']}; border-bottom: 1px solid {c['border']}; }}
    QFrame#hovercard {{ background: {c['raised']}; border: 1px solid {c['border_strong']}; border-radius: 10px; }}
    QFrame#rule {{ background: {c['border']}; max-height: 1px; min-height: 1px; border: none; }}

    QPushButton {{
        background: {c['raised']}; color: {c['ink']}; border: 1px solid {c['border_strong']};
        border-radius: 7px; padding: 6px 13px;
    }}
    QPushButton:hover {{ background: {c['hover']}; border-color: {c['accent_strong']}; }}
    QPushButton:pressed {{ background: {c['tint']}; }}
    QPushButton:disabled {{ color: {c['faint']}; border-color: {c['border']}; }}
    QPushButton[primary="true"] {{ background: {c['accent_strong']}; color: {c['on_accent']}; border-color: {c['accent_strong']}; }}
    QPushButton[primary="true"]:hover {{ background: {c['accent']}; }}
    QPushButton#link {{ background: transparent; border: none; color: {c['accent']}; padding: 4px 6px; }}
    QPushButton#link:hover {{ color: {c['ink']}; }}
    QPushButton#link:disabled {{ color: {c['faint']}; }}
    QPushButton#segment {{
        background: transparent; border: none; border-radius: 7px; padding: 6px 14px; color: {c['muted']};
    }}
    QPushButton#segment:hover {{ color: {c['ink']}; background: {c['hover']}; }}
    QPushButton#segment:checked {{ background: {c['raised']}; color: {c['ink']}; font-weight: 600; }}
    QPushButton#segment:disabled {{ color: {c['faint']}; }}
    QPushButton#segment[alert="true"] {{ color: {c['danger']}; font-weight: 600; }}
    QFrame#segments {{ background: {c['bg']}; border: 1px solid {c['border']}; border-radius: 9px; }}
    QToolButton {{ background: transparent; border: none; border-radius: 7px; padding: 5px; }}
    QToolButton:hover {{ background: {c['hover']}; }}
    QToolButton::menu-indicator, QPushButton::menu-indicator {{ image: none; width: 0; }}

    QLineEdit {{
        background: {c['surface']}; border: 1px solid {c['input_border']}; border-radius: 7px; padding: 5px 9px;
        selection-background-color: {c['accent_strong']}; selection-color: {c['on_accent']};
    }}
    QLineEdit:focus {{ border-color: {c['accent_strong']}; }}
    QLineEdit#rename {{ font-size: 22px; font-weight: 600; padding: 2px 6px; }}

    QTableView#devices {{
        background: {c['surface']}; border: none; outline: none;
        selection-background-color: transparent; selection-color: {c['ink']};
    }}
    QTableView#devices QHeaderView::section {{
        background: {c['panel']}; color: {c['muted']}; border: none; border-bottom: 1px solid {c['border']};
        padding: 8px; font-size: 11px; font-weight: 600;
    }}
    QHeaderView {{ background: {c['panel']}; border: none; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 4px; min-width: 30px; }}

    QMenu {{ background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 5px; }}
    QMenu::item {{ padding: 6px 22px; border-radius: 5px; }}
    QMenu::item:selected {{ background: {c['tint']}; }}
    QMenu::item:disabled {{ color: {c['faint']}; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
    QToolTip {{ background: {c['raised']}; color: {c['ink']}; border: 1px solid {c['border']}; padding: 7px; }}
    QStatusBar {{ background: {c['bg']}; color: {c['muted']}; }}
    QStatusBar::item {{ border: none; }}
    QCheckBox {{ color: {c['muted']}; spacing: 7px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px; border: 1px solid {c['check_border']}; border-radius: 4px; background: {c['surface']};
    }}
    QCheckBox::indicator:checked {{ background: {c['check_bg']}; border-color: {c['check_bg']}; image: url("{check}"); }}
    QSlider::groove:horizontal {{ height: 4px; background: {c['border']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: {c['accent']};
    }}
    QSlider::handle:horizontal:disabled {{ background: {c['faint']}; }}
    """


def apply(app) -> None:
    c = colors()
    app.setPalette(_palette(c))
    app.setStyleSheet(stylesheet(c))
