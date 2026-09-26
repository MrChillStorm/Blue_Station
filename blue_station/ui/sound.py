"""A Geiger counter for finding a device: short beeps that come faster,
and a little higher, as the signal gets stronger. Walk towards the faster
beeps while looking at the room instead of the screen."""
import math
import struct
import wave
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QGuiApplication

WEAK, STRONG = -95, -45  # dBm: the slowest and the fastest beeping
SLOWEST, FASTEST = 1.6, 0.12  # seconds between beeps
TONES = (900, 1100, 1350, 1650, 2000)  # Hz, weak to strong


def strength(rssi: float) -> float:
    """0 at WEAK or below, 1 at STRONG or above."""
    return min(1.0, max(0.0, (rssi - WEAK) / (STRONG - WEAK)))


def interval(rssi: float) -> float:
    """Seconds to the next beep. Evenly spaced on a log scale, so each few
    dB closer sounds about as much faster."""
    return SLOWEST * (FASTEST / SLOWEST) ** strength(rssi)


def _tone_file(freq: int) -> Path:
    """A 60 ms beep as a WAV file, made once in the app's cache folder."""
    from platformdirs import user_cache_dir
    path = Path(user_cache_dir("Blue Station", appauthor=False)) / f"beep-{freq}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        rate, n, edge = 44100, 2646, 200  # 60 ms, faded in and out so it doesn't click
        frames = b"".join(struct.pack("<h", int(11000 * math.sin(2 * math.pi * freq * i / rate)
                                                * min(1.0, i / edge, (n - i) / edge))) for i in range(n))
        temp = path.with_suffix(".tmp")
        with wave.open(str(temp), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(rate)
            f.writeframes(frames)
        temp.replace(path)
    return path


class Beeper(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.on = False
        self.rssi: float | None = None  # None: not heard, so silent
        self.beeps = 0
        self._sounds = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._beep)

    def set_on(self, on: bool) -> None:
        self.on = on
        self._timer.stop()
        if on:
            self._load()
            self.update(self.rssi)

    def update(self, rssi: float | None) -> None:
        self.rssi = rssi
        if not self.on or rssi is None:
            self._timer.stop()
            return
        wait = int(interval(rssi) * 1000)
        if not self._timer.isActive() or self._timer.remainingTime() > wait:
            self._timer.start(wait)  # getting closer shouldn't wait out a slow beat

    def _beep(self) -> None:
        if not self.on or self.rssi is None:
            return
        self.beeps += 1
        if self._sounds:
            sound = self._sounds[min(len(self._sounds) - 1, int(strength(self.rssi) * len(self._sounds)))]
            sound.play()
        self._timer.start(int(interval(self.rssi) * 1000))

    def _load(self) -> None:
        if self._sounds is not None or QGuiApplication.platformName() == "offscreen":
            return  # tests count beeps without making any
        try:
            from PySide6.QtMultimedia import QSoundEffect
            sounds = []
            for freq in TONES:
                sound = QSoundEffect(self)
                sound.setSource(QUrl.fromLocalFile(str(_tone_file(freq))))
                sound.setVolume(0.6)
                sounds.append(sound)
            self._sounds = sounds
        except (ImportError, OSError):
            self._sounds = []
