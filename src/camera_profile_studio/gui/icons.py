"""Microsoft Fluent UI System Icons (MIT) recoloured from the active palette.

The SVGs are vendored unmodified from
https://github.com/microsoft/fluentui-system-icons (commit
83b26348c811f072429427ff48362238656524fe, see ``icons/LICENSE``).  The
original artwork uses ``fill="#212121"``; at runtime that fill is replaced by
the palette's ``WindowText`` (normal) / ``PlaceholderText`` (disabled) colour
so the icons follow Light/Dark/high-contrast palettes without a custom theme.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QIconEngine, QImage, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).resolve().parent / "icons"
_FALLBACK_DIR = Path(__file__).resolve().parents[3] / "assets" / "icons" / "fluent"


def _svg_path(name: str) -> Path | None:
    filename = f"ic_fluent_{name}_24_regular.svg"
    for folder in (ICON_DIR, _FALLBACK_DIR):
        candidate = folder / filename
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=None)
def _svg_source(name: str) -> str | None:
    path = _svg_path(name)
    return path.read_text(encoding="utf-8") if path else None


def _render(svg: str, color: str, size: int, dpr: float) -> QPixmap:
    data = QByteArray(svg.replace("#212121", color).encode("utf-8"))
    renderer = QSvgRenderer(data)
    px = max(1, round(size * dpr))
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


class _FluentIconEngine(QIconEngine):
    """Renders the SVG at paint time with the *current* palette colour, so
    icons follow live Light/Dark switches without being recreated."""

    def __init__(self, name: str, svg: str) -> None:
        super().__init__()
        self._name = name
        self._svg = svg
        self._cache: dict[tuple[str, int, int], QPixmap] = {}

    @staticmethod
    def _color(mode: QIcon.Mode) -> str:
        app = QGuiApplication.instance()
        palette = app.palette() if app else QPalette()
        group = QPalette.ColorGroup.Disabled if mode == QIcon.Mode.Disabled else QPalette.ColorGroup.Active
        role = QPalette.ColorRole.HighlightedText if mode == QIcon.Mode.Selected else QPalette.ColorRole.WindowText
        if mode == QIcon.Mode.Selected and app is not None and app.style().name().lower() == "windows11":
            role = QPalette.ColorRole.WindowText  # windows11 keeps text colour on selection
        return palette.color(group, role).name()

    def _pixmap(self, size: QSize, mode: QIcon.Mode, scale: float) -> QPixmap:
        side = max(1, min(size.width(), size.height()))
        color = self._color(mode)
        key = (color, side, round(scale * 100))
        pm = self._cache.get(key)
        if pm is None:
            pm = _render(self._svg, color, side, scale)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = pm
        return pm

    def paint(self, painter: QPainter, rect, mode: QIcon.Mode, state: QIcon.State) -> None:
        scale = painter.device().devicePixelRatioF() if painter.device() else 1.0
        pm = self._pixmap(rect.size(), mode, scale)
        painter.drawPixmap(rect, pm)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self._pixmap(size, mode, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:  # noqa: N802
        return self._pixmap(size, mode, max(1.0, scale))

    def clone(self) -> "QIconEngine":
        return _FluentIconEngine(self._name, self._svg)

    def key(self) -> str:
        return f"fluent:{self._name}"


def icon(name: str) -> QIcon:
    """Return a palette-tinted Fluent icon, or an empty QIcon if unavailable."""
    svg = _svg_source(name)
    if svg is None:
        return QIcon()
    return QIcon(_FluentIconEngine(name, svg))

def available() -> bool:
    return _svg_path("settings") is not None


ICON_SIZE = QSize(20, 20)


