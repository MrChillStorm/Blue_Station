"""Regenerates Blue Station.app's icon from packaging/icon.svg -- run after
editing the SVG. macOS only (uses the built-in `iconutil`)."""
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = Path(__file__).resolve().parent / "icon.svg"
ICNS_DEST = ROOT / "Blue Station.app" / "Contents" / "Resources" / "AppIcon.icns"
SIZES = {  # Apple's iconset naming
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32, "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256, "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}


def main() -> None:
    app = QApplication(sys.argv)  # noqa: F841 -- QPixmap needs an application
    renderer = QSvgRenderer(QByteArray(SVG_PATH.read_bytes()))
    if not renderer.isValid():
        raise SystemExit(f"{SVG_PATH} failed to parse")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for name, size in SIZES.items():
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter)
            painter.end()
            pixmap.save(str(iconset / name))
        ICNS_DEST.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS_DEST)], check=True)
    print(f"Wrote {ICNS_DEST}")


if __name__ == "__main__":
    main()
