"""Line icons as inline SVG, drawn in whatever color the theme asks for."""
from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


def _stroke(width: float = 2) -> str:
    return f'fill="none" stroke="{{c}}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"'


_S = _stroke(1.8)
_STAR = "M12 3.8l2.5 5.1 5.6.8-4 3.9 1 5.6-5.1-2.7-5 2.7.9-5.6-4-3.9 5.6-.8z"
_WAVES = "M8.6 8.6a4.8 4.8 0 0 0 0 6.8M15.4 8.6a4.8 4.8 0 0 1 0 6.8M5.7 5.7a8.9 8.9 0 0 0 0 12.6M18.3 5.7a8.9 8.9 0 0 1 0 12.6"

_SVGS = {
    "chevron_left": f'<g {_stroke(2)}><path d="M14.5 6.5L9 12l5.5 5.5"/></g>',
    "check": f'<g {_stroke(3)}><path d="M5.5 12.5l4.2 4.2L18.5 7.5"/></g>',
    "star": f'<g {_S}><path d="{_STAR}"/></g>',
    "star_filled": f'<path d="{_STAR}" fill="{{c}}" stroke="{{c}}" stroke-width="1.8" stroke-linejoin="round"/>',
    "pencil": f'<g {_S}><path d="M4.5 19.5h4l10.5-10.5-4-4L4.5 15.5z"/><path d="M13.2 6.8l4 4"/></g>',
    "search": f'<g {_S}><circle cx="11" cy="11" r="6.3"/><path d="M15.7 15.7l4.3 4.3"/></g>',
    "export": f'<g {_S}><path d="M12 4v11M7.5 8.5L12 4l4.5 4.5"/><path d="M5 14v5h14v-5"/></g>',
    "link": f'<g {_S}><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/>'
            '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></g>',
    "target": f'<g {_S}><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.2"/></g>',
    "gear": f'<g {_stroke(1.8)}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 '
            '2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 '
            '0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 '
            '1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 '
            '1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 '
            '0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></g>',
    # what a device is
    "generic": f'<g {_S}><circle cx="12" cy="12" r="1.5" fill="{{c}}"/><path d="{_WAVES}"/></g>',
    "headphones": f'<g {_S}><path d="M4 17v-4a8 8 0 0 1 16 0v4"/><rect x="3.5" y="14" width="4" height="6.5" rx="1.5"/>'
                  '<rect x="16.5" y="14" width="4" height="6.5" rx="1.5"/></g>',
    "speaker": f'<g {_S}><rect x="6" y="3" width="12" height="18" rx="2.5"/><circle cx="12" cy="14" r="3.2"/>'
               '<path d="M12 7.2h.01"/></g>',
    "tag": f'<g {_S}><path d="M12 21s-6.5-6-6.5-11a6.5 6.5 0 0 1 13 0c0 5-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/></g>',
    "beacon": f'<g {_S}><path d="M12 17.2h.01" stroke-width="3"/><path d="M8.3 13.4a5.3 5.3 0 0 1 7.4 0M5.4 10.5a9.4 9.4 0 0 '
              '1 13.2 0M2.7 7.6a13.3 13.3 0 0 1 18.6 0"/></g>',
    "laptop": f'<g {_S}><rect x="4.5" y="5" width="15" height="10.5" rx="1.5"/><path d="M2.5 19h19"/></g>',
    "phone": f'<g {_S}><rect x="7" y="2.8" width="10" height="18.4" rx="2.4"/><path d="M11 18h2"/></g>',
    "watch": f'<g {_S}><rect x="6.5" y="7" width="11" height="10" rx="3"/><path d="M9 7l.7-3.5h4.6L15 7M9 17l.7 3.5h4.6L15 17"/></g>',
    "heart": f'<g {_S}><path d="M12 19.5s-7.5-4.6-7.5-10A4.2 4.2 0 0 1 12 7a4.2 4.2 0 0 1 7.5 2.5c0 5.4-7.5 10-7.5 10z"/></g>',
    "keyboard": f'<g {_S}><rect x="2.5" y="6.5" width="19" height="11" rx="2"/>'
                '<path d="M6.5 10h.01M9.8 10h.01M13.1 10h.01M16.5 10h.01M7.5 14h9"/></g>',
    "tv": f'<g {_S}><rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8.5 20.5h7"/></g>',
    "sensor": f'<g {_S}><rect x="7" y="7" width="10" height="10" rx="2"/>'
              '<path d="M10 3.5v3M14 3.5v3M10 17.5v3M14 17.5v3M3.5 10h3M3.5 14h3M17.5 10h3M17.5 14h3"/></g>',
    "logo": '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6f8dff"/>'
            '<stop offset="1" stop-color="#4338ca"/></linearGradient></defs>'
            '<rect width="24" height="24" rx="6" fill="url(#g)"/>'
            f'<circle cx="12" cy="12" r="1.9" fill="#fff"/><path d="{_WAVES}" fill="none" stroke="#fff" '
            'stroke-width="1.7" stroke-linecap="round"/>',
}


def svg(name: str, color: str = "#000000") -> str:
    body = _SVGS[name].replace("{c}", color)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">{body}</svg>'


@lru_cache(maxsize=512)
def pixmap(name: str, color: str, size: int, dpr: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg(name, color).encode()))
    px = round(size * dpr)
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pm = QPixmap.fromImage(image)
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str, size: int = 16) -> QIcon:
    ic = QIcon()
    for dpr in (1.0, 2.0):
        ic.addPixmap(pixmap(name, color, size, dpr))
    return ic
