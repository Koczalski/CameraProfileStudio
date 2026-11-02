"""Paint LUT Compare sheets and warning bands with QPainter (worker-safe: QImage only).

The engine has no text renderer and no bundled Japanese font; Qt falls back to
the fonts of the system, so names in any language are drawn correctly.
"""

from __future__ import annotations

from typing import Callable, Sequence

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

from ..lut_compare import Page, SheetMetrics, centered_offset

_INK = QColor(0, 0, 0)
_PAPER = QColor(255, 255, 255)
_WARNING_INK = QColor(170, 70, 0)
_BAND = QColor(150, 60, 0)
_LINE_FACTOR = 1.4  # line box of the headers, in font heights


def _rgb8(image: QImage) -> np.ndarray:
    converted = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = converted.width(), converted.height()
    data = np.frombuffer(converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes())
    return data.reshape(height, converted.bytesPerLine())[:, :width * 3].reshape(height, width, 3).copy()


def _font(pixels: int) -> QFont:
    font = QFont()
    font.setPixelSize(pixels)
    return font


def _elided(painter: QPainter, text: str, width: int) -> str:
    return QFontMetrics(painter.font()).elidedText(text, Qt.TextElideMode.ElideMiddle, width)


def render_sheet(page: Page, metrics: SheetMetrics, before_title: str, photo_names: Sequence[str], lut_names: Sequence[str],
                 lut_warnings: Sequence[str], cell_at: Callable[[int, int | None], QImage | None]) -> np.ndarray:
    """One page as 8-bit RGB.  ``cell_at(photo, lut)`` gives a picture (``lut`` None: Before) or None."""
    width, height = metrics.sheet_size(page)
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(_PAPER)
    side, line = metrics.side, int(metrics.font_px * _LINE_FACTOR)
    align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        columns: list[int | None] = [None, *page.luts]
        for column, lut in enumerate(columns):
            x = metrics.cell_origin(0, column)[0]
            painter.setFont(_font(metrics.font_px))
            painter.setPen(_INK)
            title = before_title if lut is None else lut_names[lut]
            painter.drawText(QRect(x, metrics.margin, side, line), align, _elided(painter, title, side))
            if lut is not None and lut_warnings[lut]:
                painter.setFont(_font(max(11, metrics.font_px * 3 // 4)))
                painter.setPen(_WARNING_INK)
                painter.drawText(QRect(x, metrics.margin + line, side, line), align,
                                 _elided(painter, lut_warnings[lut], side))
        label_width = metrics.label_width - metrics.margin - metrics.gap
        for row, photo in enumerate(page.photos):
            y = metrics.cell_origin(row, 0)[1]
            painter.setFont(_font(metrics.font_px))
            painter.setPen(_INK)
            painter.drawText(QRect(metrics.margin, y, label_width, side), align,
                             _elided(painter, photo_names[photo], label_width))
            for column, lut in enumerate(columns):
                picture = cell_at(photo, lut)
                if picture is not None:
                    x, y = metrics.cell_origin(row, column)
                    dx, dy = centered_offset(picture.width(), picture.height(), side)
                    painter.drawImage(x + dx, y + dy, picture)
    finally:
        painter.end()
    return _rgb8(image)


def add_warning_band(rgb8: np.ndarray, text: str) -> np.ndarray:
    """``rgb8`` with a band added below it that carries ``text`` (wrapped to the width)."""
    height, width = rgb8.shape[:2]
    font = _font(max(12, width // 60))
    padding = font.pixelSize() // 2
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap)
    inner = max(1, width - 2 * padding)
    band = QFontMetrics(font).boundingRect(QRect(0, 0, inner, 100000), flags, text).height() + 2 * padding
    image = QImage(width, band, QImage.Format.Format_RGB888)
    image.fill(_BAND)
    painter = QPainter(image)
    try:
        painter.setFont(font)
        painter.setPen(_PAPER)
        painter.drawText(QRect(padding, 0, inner, band), flags, text)
    finally:
        painter.end()
    return np.concatenate((rgb8, _rgb8(image)), axis=0)
