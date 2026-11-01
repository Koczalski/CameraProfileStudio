"""Crop/ROI editor for one reference image (Image Look Creator).

The ROI is a normalised rectangle ``(x0, y0, x1, y1)`` in the
*orientation-corrected* image (the engine loader applies EXIF orientation
before anything else; the editor only ever paints an image decoded by that
loader on a worker thread).  Coordinates are resolution independent, so a
display-sized decode selects the same region the engine analyses.

Interaction:

* drag outside the selection draws a new rectangle, drag inside moves it,
  drag a corner handle resizes it; nothing changes until the pointer moves
  ``QApplication.startDragDistance()`` (a click/jitter to focus the editor
  never alters the crop or invalidates an analysis);
* keyboard: arrow keys move by 1 % (Ctrl: 10 %), Shift+arrows resize the
  right/bottom edge, Home or R resets to the full image;
* zero/near-zero areas are impossible: every edit is clamped to a minimum
  span of :data:`MIN_ROI_SPAN` per axis.

``roi_changed`` is emitted only for committed edits (mouse release or a key
press), so a drag does not invalidate analysis results on every pixel.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QSizePolicy, QWidget

from ..i18n import T, tr

FULL_ROI: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
MIN_ROI_SPAN = 0.02  # 2 % of width/height; the engine also rejects too-small crops
_HANDLE = 8.0  # logical px hit radius for corner handles
_DIGITS = 4


def _round(v: float) -> float:
    return round(float(v), _DIGITS)


def is_valid_roi(roi, min_span: float = MIN_ROI_SPAN) -> bool:
    """True for a finite, in-bounds rectangle with at least ``min_span``
    extent on both axes (never a zero-area selection)."""
    try:
        x0, y0, x1, y1 = (float(v) for v in roi)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return False
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return False
    eps = 1e-9
    return (x1 - x0) + eps >= min_span and (y1 - y0) + eps >= min_span


def clamp_roi(x0: float, y0: float, x1: float, y1: float, min_span: float = MIN_ROI_SPAN) -> tuple[float, float, float, float]:
    """Order, clip to the unit square and grow to ``min_span`` if needed."""

    def axis(a: float, b: float) -> tuple[float, float]:
        lo, hi = sorted((min(1.0, max(0.0, a)), min(1.0, max(0.0, b))))
        if hi - lo < min_span:
            mid = (lo + hi) / 2
            lo = min(max(0.0, mid - min_span / 2), 1.0 - min_span)
            hi = lo + min_span
        return _round(lo), _round(hi)

    ax0, ax1 = axis(x0, x1)
    ay0, ay1 = axis(y0, y1)
    return (ax0, ay0, ax1, ay1)


def move_roi(roi, dx: float, dy: float) -> tuple[float, float, float, float]:
    """Translate keeping the size, stopping at the image bounds."""
    x0, y0, x1, y1 = roi
    w, h = x1 - x0, y1 - y0
    nx0 = min(max(0.0, x0 + dx), 1.0 - w)
    ny0 = min(max(0.0, y0 + dy), 1.0 - h)
    return (_round(nx0), _round(ny0), _round(nx0 + w), _round(ny0 + h))


def is_full(roi) -> bool:
    return tuple(_round(v) for v in roi) == FULL_ROI


class RoiEditor(QWidget):
    roi_changed = Signal(tuple)  # committed edit
    roi_editing = Signal(tuple)  # live, during a drag

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(140, 100)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._roi = FULL_ROI
        self._placeholder_key = "imagelook.roi.empty"
        self._drag: str | None = None  # "new" | "move" | "corner" (pending until past the threshold)
        self._active = False  # True once the pointer moved startDragDistance()
        self._press_px = QPointF()
        self._origin = QPointF()
        self._start_roi = FULL_ROI
        self._anchor = QPointF()  # fixed corner (normalised) while resizing
        T.bind(self, "setAccessibleName", "imagelook.roi.accessible")
        T.bind(self, "setToolTip", "imagelook.roi.tooltip")
        T.watch(self, self._describe)
        self._describe()

    # ------------------------------------------------------------------ API
    def set_image(self, image: QImage | None, placeholder_key: str = "imagelook.roi.empty") -> None:
        self._image = image if image is not None and not image.isNull() else None
        self._placeholder_key = placeholder_key
        self._drag = None
        self.update()

    def has_image(self) -> bool:
        return self._image is not None

    def roi(self) -> tuple[float, float, float, float]:
        return self._roi

    def set_roi(self, roi, *, emit: bool = False) -> None:
        new = clamp_roi(*roi)
        changed = new != self._roi
        self._roi = new
        self._describe()
        self.update()
        if emit and changed:
            self.roi_changed.emit(new)

    def reset(self) -> None:
        self.set_roi(FULL_ROI, emit=True)

    # ------------------------------------------------------------ geometry
    def image_rect(self) -> QRectF:
        if self._image is None:
            return QRectF()
        area = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0 or area.width() <= 0 or area.height() <= 0:
            return QRectF()
        s = min(area.width() / iw, area.height() / ih)
        w, h = iw * s, ih * s
        return QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)

    def _to_norm(self, pos: QPointF) -> QPointF:
        r = self.image_rect()
        if r.isEmpty():
            return QPointF(0, 0)
        return QPointF(min(1.0, max(0.0, (pos.x() - r.left()) / r.width())),
                       min(1.0, max(0.0, (pos.y() - r.top()) / r.height())))

    def roi_rect(self) -> QRectF:
        r = self.image_rect()
        x0, y0, x1, y1 = self._roi
        return QRectF(r.left() + x0 * r.width(), r.top() + y0 * r.height(), (x1 - x0) * r.width(), (y1 - y0) * r.height())

    def _corners(self) -> list[QPointF]:
        rr = self.roi_rect()
        return [rr.topLeft(), rr.topRight(), rr.bottomLeft(), rr.bottomRight()]

    # ------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        pal = self.palette()
        painter.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        if self._image is None:
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(QRectF(self.rect()).adjusted(12, 12, -12, -12),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, tr(self._placeholder_key))
            self._paint_focus(painter, pal)
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        ir = self.image_rect()
        painter.drawImage(ir, self._image)
        rr = self.roi_rect()
        # dim everything outside the selection (works in light and dark)
        shade = QColor(0, 0, 0, 130)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shade)
        for part in (QRectF(ir.left(), ir.top(), ir.width(), rr.top() - ir.top()),
                     QRectF(ir.left(), rr.bottom(), ir.width(), ir.bottom() - rr.bottom()),
                     QRectF(ir.left(), rr.top(), rr.left() - ir.left(), rr.height()),
                     QRectF(rr.right(), rr.top(), ir.right() - rr.right(), rr.height())):
            if part.width() > 0 and part.height() > 0:
                painter.drawRect(part)
        accent = pal.color(QPalette.ColorRole.Highlight)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 3))
        painter.drawRect(rr)
        painter.setPen(QPen(accent, 2))
        painter.drawRect(rr)
        painter.setBrush(accent)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        for c in self._corners():
            painter.drawRect(QRectF(c.x() - 4, c.y() - 4, 8, 8))
        self._paint_focus(painter, pal)
        painter.end()

    def _paint_focus(self, painter: QPainter, pal: QPalette) -> None:
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Highlight), 2))
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))

    # --------------------------------------------------------------- mouse
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() != Qt.MouseButton.LeftButton or self._image is None or not self.isEnabled():
            return
        pos = event.position()
        if not self.image_rect().adjusted(-_HANDLE, -_HANDLE, _HANDLE, _HANDLE).contains(pos):
            return
        self._start_roi = self._roi
        self._origin = self._to_norm(pos)
        self._press_px = QPointF(pos)
        self._active = False
        corners = self._corners()
        for i, c in enumerate(corners):
            if abs(c.x() - pos.x()) <= _HANDLE and abs(c.y() - pos.y()) <= _HANDLE:
                opposite = corners[3 - i]
                self._anchor = self._to_norm(opposite)
                self._drag = "corner"
                return
        if self.roi_rect().contains(pos):
            self._drag = "move"
        else:
            self._drag = "new"
            self._anchor = self._origin

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._drag is None:
            near_corner = any(abs(c.x() - pos.x()) <= _HANDLE and abs(c.y() - pos.y()) <= _HANDLE for c in self._corners())
            if self._image is None:
                self.unsetCursor()
            elif near_corner:
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif self.roi_rect().contains(pos):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if not self._active:
            if (pos - self._press_px).manhattanLength() < QApplication.startDragDistance():
                return
            self._active = True
        p = self._to_norm(pos)
        if self._drag == "move":
            new = move_roi(self._start_roi, p.x() - self._origin.x(), p.y() - self._origin.y())
        else:
            new = clamp_roi(self._anchor.x(), self._anchor.y(), p.x(), p.y())
        self._roi = new
        self._describe()
        self.roi_editing.emit(new)
        self.update()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is None:
            return
        active = self._active
        self._drag = None
        self._active = False
        if active and self._roi != self._start_roi:
            self.roi_changed.emit(self._roi)

    # ------------------------------------------------------------ keyboard
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._image is None or not self.isEnabled():
            super().keyPressEvent(event)
            return
        key = event.key()
        mods = event.modifiers()
        step = 0.1 if mods & Qt.KeyboardModifier.ControlModifier else 0.01
        dirs = {Qt.Key.Key_Left: (-step, 0.0), Qt.Key.Key_Right: (step, 0.0),
                Qt.Key.Key_Up: (0.0, -step), Qt.Key.Key_Down: (0.0, step)}
        if key in (Qt.Key.Key_Home, Qt.Key.Key_R):
            self.reset()
            event.accept()
            return
        if key in dirs:
            dx, dy = dirs[Qt.Key(key)]
            x0, y0, x1, y1 = self._roi
            if mods & Qt.KeyboardModifier.ShiftModifier:
                new = clamp_roi(x0, y0, max(x0 + MIN_ROI_SPAN, x1 + dx), max(y0 + MIN_ROI_SPAN, y1 + dy))
            else:
                new = move_roi(self._roi, dx, dy)
            self.set_roi(new, emit=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def _describe(self) -> None:
        x0, y0, x1, y1 = self._roi
        self.setAccessibleDescription(tr("imagelook.crop.value", x0=f"{x0 * 100:.1f}", y0=f"{y0 * 100:.1f}",
                                         x1=f"{x1 * 100:.1f}", y1=f"{y1 * 100:.1f}"))

    def sizeHint(self) -> QSize:  # noqa: D401
        return QSize(320, 220)
