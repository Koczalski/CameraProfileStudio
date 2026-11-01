"""Before/After image viewport with zoom, pan and split comparison.

The widget only *paints* QImages that were decoded and transformed on worker
threads; it never reads files or applies LUTs itself.

Keyboard: ``+``/``-`` zoom, ``0`` fit, ``1`` 100 %, arrow keys pan,
``[``/``]`` move the split divider, ``B``/``A``/``S``/``D`` choose
Before / After / Split / Side-by-side.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..i18n import T, tr


class CompareMode(str, Enum):
    BEFORE = "before"
    AFTER = "after"
    SPLIT = "split"
    SIDE_BY_SIDE = "side"


class ImageViewer(QWidget):
    zoom_changed = Signal(float)
    mode_changed = Signal(str)

    MIN_ZOOM = 0.02
    MAX_ZOOM = 32.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 120)
        self.setMouseTracking(True)
        self._before: QImage | None = None
        self._after: QImage | None = None
        self._mode = CompareMode.SPLIT
        self._split = 0.5
        self._zoom = 1.0
        self._fit = True
        self._center = QPointF(0, 0)  # image coordinates at viewport centre
        self._drag_origin: QPointF | None = None
        self._drag_center = QPointF()
        self._dragging_split = False
        self._placeholder_key = "viewer.empty"
        self._placeholder_kwargs: dict = {}
        self._busy = False
        T.watch(self, lambda: self.update())
        T.bind(self, "setAccessibleName", "viewer.accessible")
        T.bind(self, "setToolTip", "viewer.tooltip")

    # ------------------------------------------------------------------ API
    def set_images(self, before: QImage | None, after: QImage | None, *, keep_view: bool = True) -> None:
        old_size = self._image_size()
        self._before = before if before is not None and not before.isNull() else None
        self._after = after if after is not None and not after.isNull() else None
        if not keep_view or self._image_size() != old_size:
            self._fit = True
            self._center = QPointF(self._image_size().width() / 2, self._image_size().height() / 2)
        self._busy = False
        self.update()

    def clear(self, placeholder_key: str = "viewer.empty", **kwargs) -> None:
        self._before = self._after = None
        self._placeholder_key = placeholder_key
        self._placeholder_kwargs = kwargs
        self._busy = False
        self.update()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.update()

    def has_image(self) -> bool:
        return self._before is not None or self._after is not None

    def mode(self) -> CompareMode:
        return self._mode

    def set_mode(self, mode: CompareMode | str) -> None:
        mode = CompareMode(mode)
        if mode != self._mode:
            self._mode = mode
            self.mode_changed.emit(mode.value)
            self.update()

    def split_position(self) -> float:
        return self._split

    def set_split_position(self, value: float) -> None:
        self._split = min(1.0, max(0.0, value))
        self.update()

    def zoom(self) -> float:
        return self._effective_zoom()

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        if not self.has_image():
            return
        zoom = min(self.MAX_ZOOM, max(self.MIN_ZOOM, zoom))
        if anchor is not None:
            # keep the image point under the anchor stationary
            before = self._to_image(anchor)
            self._fit = False
            self._zoom = zoom
            after = self._to_image(anchor)
            self._center += before - after
        else:
            self._fit = False
            self._zoom = zoom
        self._clamp_center()
        self.zoom_changed.emit(self._effective_zoom())
        self.update()

    def zoom_in(self) -> None:
        self.set_zoom(self._effective_zoom() * 1.25)

    def zoom_out(self) -> None:
        self.set_zoom(self._effective_zoom() / 1.25)

    def fit(self) -> None:
        self._fit = True
        size = self._image_size()
        self._center = QPointF(size.width() / 2, size.height() / 2)
        self.zoom_changed.emit(self._effective_zoom())
        self.update()

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def pan_by(self, dx: float, dy: float) -> None:
        if not self.has_image():
            return
        if self._fit:
            self._zoom = self._effective_zoom()
            self._fit = False
        z = self._effective_zoom()
        self._center -= QPointF(dx / z, dy / z)
        self._clamp_center()
        self.update()

    # ------------------------------------------------------------ geometry
    def _image_size(self) -> QSize:
        img = self._after or self._before
        return img.size() if img is not None else QSize(0, 0)

    def _pane_rects(self) -> list[QRectF]:
        r = QRectF(self.rect())
        if self._mode is CompareMode.SIDE_BY_SIDE:
            half = r.width() / 2
            return [QRectF(r.left(), r.top(), half - 2, r.height()), QRectF(r.left() + half + 2, r.top(), half - 2, r.height())]
        return [r]

    def _effective_zoom(self) -> float:
        size = self._image_size()
        if size.isEmpty():
            return 1.0
        if not self._fit:
            return self._zoom
        pane = self._pane_rects()[0]
        # widget coordinates are device-independent; images are shown 1:1 in
        # logical pixels when zoom == 1 (DPI handled by Qt's painter scaling)
        return max(self.MIN_ZOOM, min(pane.width() / size.width(), pane.height() / size.height()) * 0.98)

    def _image_rect(self, pane: QRectF) -> QRectF:
        z = self._effective_zoom()
        size = self._image_size()
        center = QPointF(size.width() / 2, size.height() / 2) if self._fit else self._center
        top_left = pane.center() - QPointF(center.x() * z, center.y() * z)
        return QRectF(top_left, QSize(round(size.width() * z), round(size.height() * z)).toSizeF())

    def _to_image(self, pos: QPointF) -> QPointF:
        pane = next((p for p in self._pane_rects() if p.contains(pos)), self._pane_rects()[0])
        rect = self._image_rect(pane)
        z = self._effective_zoom()
        return QPointF((pos.x() - rect.left()) / z, (pos.y() - rect.top()) / z)

    def _clamp_center(self) -> None:
        size = self._image_size()
        self._center.setX(min(max(self._center.x(), 0.0), float(size.width())))
        self._center.setY(min(max(self._center.y(), 0.0), float(size.height())))

    def _split_x(self) -> float:
        rect = self._image_rect(self._pane_rects()[0])
        return rect.left() + rect.width() * self._split

    # ------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        pal = self.palette()
        painter.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        if not self.has_image():
            painter.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            key = "viewer.loading" if self._busy else self._placeholder_key
            painter.drawText(
                self.rect().adjusted(16, 16, -16, -16),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                tr(key, **self._placeholder_kwargs),
            )
            painter.end()
            return
        smooth = self._effective_zoom() < 1.0
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
        label_pen = pal.color(QPalette.ColorRole.WindowText)
        if self._before is None or self._after is None:
            # Only one image exists: never fake a comparison by showing the
            # same pixels as both Before and After.
            pane = QRectF(self.rect())
            only_before = self._after is None
            img = self._before if only_before else self._after
            painter.drawImage(self._image_rect_single(pane), img)
            self._draw_tag(painter, pane, tr("viewer.before" if only_before else "viewer.after"), label_pen)
            self._draw_tag(painter, pane, tr("viewer.after_unavailable" if only_before else "viewer.before_unavailable"),
                           pal.color(QPalette.ColorRole.PlaceholderText), right=True)
            self._paint_focus(painter, pal)
            painter.end()
            return
        before, after = self._before, self._after
        panes = self._pane_rects()
        if self._mode is CompareMode.SIDE_BY_SIDE:
            for pane, img, key in ((panes[0], before, "viewer.before"), (panes[1], after, "viewer.after")):
                painter.save()
                painter.setClipRect(pane)
                painter.drawImage(self._image_rect(pane), img)
                painter.restore()
                self._draw_tag(painter, pane, tr(key), label_pen)
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Mid), 1))
            mid = self.rect().width() / 2
            painter.drawLine(QPointF(mid, 0), QPointF(mid, self.height()))
        else:
            pane = panes[0]
            target = self._image_rect(pane)
            if self._mode is CompareMode.BEFORE:
                painter.drawImage(target, before)
                self._draw_tag(painter, pane, tr("viewer.before"), label_pen)
            elif self._mode is CompareMode.AFTER:
                painter.drawImage(target, after)
                self._draw_tag(painter, pane, tr("viewer.after"), label_pen)
            else:
                x = self._split_x()
                painter.save()
                painter.setClipRect(QRectF(pane.left(), pane.top(), x - pane.left(), pane.height()))
                painter.drawImage(target, before)
                painter.restore()
                painter.save()
                painter.setClipRect(QRectF(x, pane.top(), pane.right() - x, pane.height()))
                painter.drawImage(target, after)
                painter.restore()
                accent = pal.color(QPalette.ColorRole.Highlight)
                painter.setPen(QPen(accent, 2))
                painter.drawLine(QPointF(x, pane.top()), QPointF(x, pane.bottom()))
                self._draw_tag(painter, pane, tr("viewer.before"), label_pen)
                self._draw_tag(painter, pane, tr("viewer.after"), label_pen, right=True)
        self._paint_focus(painter, pal)
        painter.end()

    def _paint_focus(self, painter: QPainter, pal: QPalette) -> None:
        if self.hasFocus():
            painter.setPen(QPen(pal.color(QPalette.ColorRole.Highlight), 2))
            painter.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))

    def _image_rect_single(self, pane: QRectF) -> QRectF:
        """Image rect for single-image display (full widget, any mode)."""
        saved = self._mode
        self._mode = CompareMode.BEFORE
        try:
            return self._image_rect(pane)
        finally:
            self._mode = saved

    def is_comparing(self) -> bool:
        """True only when distinct Before and After images are shown."""
        return self._before is not None and self._after is not None

    def _draw_tag(self, painter: QPainter, pane: QRectF, text: str, color: QColor, right: bool = False) -> None:
        fm = painter.fontMetrics()
        w = fm.horizontalAdvance(text) + 12
        h = fm.height() + 6
        x = pane.right() - w - 8 if right else pane.left() + 8
        box = QRectF(x, pane.top() + 8, w, h)
        bg = self.palette().color(QPalette.ColorRole.Window)
        bg.setAlpha(210)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(color)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    # --------------------------------------------------------------- input
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if not self.has_image():
            return
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.set_zoom(self._effective_zoom() * (1.25**steps), anchor=event.position())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() != Qt.MouseButton.LeftButton or not self.has_image():
            return
        if self._mode is CompareMode.SPLIT and abs(event.position().x() - self._split_x()) < 8:
            self._dragging_split = True
        else:
            self._drag_origin = event.position()
            if self._fit:
                self._zoom = self._effective_zoom()
                size = self._image_size()
                self._center = QPointF(size.width() / 2, size.height() / 2)
                self._fit = False
            self._drag_center = QPointF(self._center)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._dragging_split:
            rect = self._image_rect(self._pane_rects()[0])
            if rect.width() > 0:
                self.set_split_position((event.position().x() - rect.left()) / rect.width())
            return
        if self._drag_origin is not None:
            delta = event.position() - self._drag_origin
            z = self._effective_zoom()
            self._center = self._drag_center - QPointF(delta.x() / z, delta.y() / z)
            self._clamp_center()
            self.update()
            return
        if self._mode is CompareMode.SPLIT and self.has_image() and abs(event.position().x() - self._split_x()) < 8:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.has_image() else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        self._drag_origin = None
        self._dragging_split = False
        self.unsetCursor()

    def mouseDoubleClickEvent(self, _event: QMouseEvent) -> None:  # noqa: N802
        if self._fit:
            self.actual_size()
        else:
            self.fit()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        step = 48
        mapping = {
            Qt.Key.Key_Plus: self.zoom_in,
            Qt.Key.Key_Equal: self.zoom_in,
            Qt.Key.Key_Minus: self.zoom_out,
            Qt.Key.Key_0: self.fit,
            Qt.Key.Key_1: self.actual_size,
            Qt.Key.Key_Left: lambda: self.pan_by(step, 0),
            Qt.Key.Key_Right: lambda: self.pan_by(-step, 0),
            Qt.Key.Key_Up: lambda: self.pan_by(0, step),
            Qt.Key.Key_Down: lambda: self.pan_by(0, -step),
            Qt.Key.Key_BracketLeft: lambda: self.set_split_position(self._split - 0.05),
            Qt.Key.Key_BracketRight: lambda: self.set_split_position(self._split + 0.05),
            Qt.Key.Key_B: lambda: self.set_mode(CompareMode.BEFORE),
            Qt.Key.Key_A: lambda: self.set_mode(CompareMode.AFTER),
            Qt.Key.Key_S: lambda: self.set_mode(CompareMode.SPLIT),
            Qt.Key.Key_D: lambda: self.set_mode(CompareMode.SIDE_BY_SIDE),
        }
        action = mapping.get(Qt.Key(key))
        if action is not None:
            action()
            event.accept()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: D401
        return QSize(640, 420)


