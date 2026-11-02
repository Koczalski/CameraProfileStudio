"""Hue map: LUT nodes as arrows from the input colour to the output colour in
the Oklab a/b plane.

Two layers (``Engine.hue_nodes``): a sparse regular sub-grid of the LUT drawn as
thin, translucent arrows (the "cloud", at most about 500 nodes), and the grid
points the *selected finding* names (its ``locations``, at most 64) drawn on top
in the flag colour with a ring at the output end (shape, not colour alone).
Nothing is marked when the selected finding names no grid points.

Keyboard: Left/Right step through the marked points; the pointer reads the
nearest arrow.  Colours come from the palette (``chart_style``), so Light/Dark
apply; the legend and the read-out wrap instead of being clipped.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..i18n import T, tr
from .chart_style import chart_colors

RING_CHROMA = (0.1, 0.2, 0.3, 0.4)
SAMPLE_W = 28.0   # width of a legend sample


def _fmt(v: float) -> str:
    return f"{v:+.3f}"


class HueMap(QWidget):
    sample_changed = Signal(object)  # index into the arrows (cloud first, then marked), or None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._base = np.zeros((0, 4))
        self._marked = np.zeros((0, 4))
        self._current: int | None = None
        T.bind(self, "setAccessibleName", "health.hue.accessible")
        T.watch(self, self._retranslate)
        self._retranslate()

    # ------------------------------------------------------------------ data
    def set_data(self, base: np.ndarray | None, marked: np.ndarray | None = None) -> None:
        """``base``: cloud arrows (k, 4); ``marked``: arrows of the selected finding's grid points (m, 4).
        Columns are in_a, in_b, out_a, out_b (Oklab)."""
        def clean(arr):
            if arr is None or len(arr) == 0:
                return np.zeros((0, 4))
            arr = np.asarray(arr, dtype=np.float64).reshape(-1, 4)
            return arr[np.isfinite(arr).all(axis=1)]

        self._base, self._marked = clean(base), clean(marked)
        self._current = None
        self._retranslate()
        self.update()

    def _all(self) -> np.ndarray:
        return np.concatenate([self._base, self._marked]) if len(self._marked) else self._base

    def has_data(self) -> bool:
        return len(self._base) + len(self._marked) > 0

    def sample_count(self) -> int:
        return len(self._base)

    def marked_count(self) -> int:
        return len(self._marked)

    def marked_indices(self) -> list[int]:
        return list(range(len(self._base), len(self._base) + len(self._marked)))

    def current(self) -> int | None:
        return self._current

    def set_current(self, index: int | None) -> None:
        if index is not None and not 0 <= index < len(self._base) + len(self._marked):
            index = None
        if index != self._current:
            self._current = index
            self.sample_changed.emit(index)
            self.update()

    def _hint(self) -> str:
        return tr("health.hue.hint" if len(self._marked) else "health.hue.hint_plain")

    def readout_text(self) -> str:
        if not self.has_data():
            return ""
        if self._current is None:
            return self._hint()
        ia, ib, oa, ob = self._all()[self._current]
        return tr("health.hue.readout", ia=_fmt(ia), ib=_fmt(ib), oa=_fmt(oa), ob=_fmt(ob))

    def description_text(self) -> str:
        if not self.has_data():
            return tr("health.hue.empty")
        return tr("health.hue.description", k=len(self._base), n=len(self._marked))

    def _retranslate(self) -> None:
        self.setAccessibleDescription(self.description_text())
        self.update()

    # ----------------------------------------------------------------- layout
    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(480, 420)

    def _small_font(self):
        font = self.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        return font

    def _extent(self) -> float:
        arr = self._all()
        peak = float(np.max(np.abs(arr))) if arr.size else 0.1
        return max(0.1, math.ceil(peak * 20.0) / 20.0 + 0.02)

    def _legend_items(self) -> list[tuple[str, str]]:
        items = [("arrow", tr("health.hue.legend.arrow"))]
        if len(self._marked):
            items.append(("flag", tr("health.hue.legend.flagged")))
        items.append(("rings", tr("health.hue.legend.rings")))
        return items

    def _wrapped_height(self, fm: QFontMetricsF, width: float, text: str) -> float:
        return fm.boundingRect(QRectF(0, 0, max(20.0, width), 10000.0), int(Qt.TextFlag.TextWordWrap), text).height()

    def _layout(self) -> tuple[QRectF, float, list[tuple[str, str, QRectF]], QRectF, QRectF]:
        """plot square, pixels per Oklab unit, legend placements, read-out rect, axis-title rect."""
        fm = QFontMetricsF(self._small_font())
        line = fm.height()
        w, h = float(self.width()), float(self.height())
        pad = 8.0
        x, y = pad, pad
        placed: list[tuple[str, str, QRectF]] = []
        for kind, text in self._legend_items():
            item_w = SAMPLE_W + 4 + fm.horizontalAdvance(text) + 2
            if x + item_w > w - pad and x > pad:
                x, y = pad, y + line + 3
            placed.append((kind, text, QRectF(x, y, item_w, line)))
            x += item_w + 14
        legend_bottom = y + line
        # the read-out wraps; reserve room for the longest text it can show so the plot does not jump
        read_h = max(self._wrapped_height(fm, w - 2 * pad, self._hint()),
                     self._wrapped_height(fm, w - 2 * pad, self.readout_text()), line)
        readout = QRectF(pad, legend_bottom + 4, w - 2 * pad, read_h)
        axes = QRectF(pad, h - pad - line, w - 2 * pad, line)
        top = readout.bottom() + 6
        avail_h = axes.top() - 6 - top
        side = max(40.0, min(w - 2 * pad, avail_h))
        plot = QRectF((w - side) / 2.0, top + (avail_h - side) / 2.0, side, side)
        return plot, side / 2.0 / self._extent(), placed, readout, axes

    def _point(self, plot: QRectF, scale: float, a: float, b: float) -> QPointF:
        return QPointF(plot.center().x() + a * scale, plot.center().y() - b * scale)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        col = chart_colors(self.palette())
        p.fillRect(self.rect(), col["bg"])
        p.setPen(QPen(col["frame"], 1))
        p.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        small = self._small_font()
        p.setFont(small)
        if not self.has_data():
            p.setPen(col["muted"])
            p.drawText(QRectF(self.rect()).adjusted(12, 12, -12, -12), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       tr("health.hue.empty"))
            self._paint_focus(p, col)
            p.end()
            return
        plot, scale, legend, readout, axes = self._layout()
        for kind, text, rect in legend:
            self._paint_legend_sample(p, col, kind, rect)
            p.setPen(col["text"])
            p.drawText(QRectF(rect.left() + SAMPLE_W + 4, rect.top(), rect.width() - SAMPLE_W - 4 + 2, rect.height()),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        p.setPen(col["text"] if self._current is not None else col["muted"])
        p.drawText(readout, int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap), self.readout_text())
        p.setPen(col["text"])
        half = axes.width() / 2.0
        p.drawText(QRectF(axes.left(), axes.top(), half, axes.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   tr("health.hue.axis_a"))
        p.drawText(QRectF(axes.left() + half, axes.top(), half, axes.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                   tr("health.hue.axis_b"))
        # frame, axes and chroma rings
        centre = plot.center()
        p.setPen(QPen(col["frame"], 1))
        p.drawRect(plot)
        p.setPen(QPen(col["axis"], 1))
        p.drawLine(QPointF(plot.left(), centre.y()), QPointF(plot.right(), centre.y()))
        p.drawLine(QPointF(centre.x(), plot.top()), QPointF(centre.x(), plot.bottom()))
        extent = self._extent()
        p.setBrush(Qt.BrushStyle.NoBrush)
        for r in RING_CHROMA:
            if r > extent:
                break
            p.setPen(QPen(col["grid"], 1, Qt.PenStyle.DashLine))
            p.drawEllipse(centre, r * scale, r * scale)
            p.setPen(col["muted"])
            p.drawText(QPointF(centre.x() + r * scale * 0.7071 + 3, centre.y() - r * scale * 0.7071 - 2), f"{r:g}")
        p.save()
        p.setClipRect(plot.adjusted(1, 1, -1, -1))
        # cloud: thin, translucent arrows
        cloud = QColor(col["axis"])
        cloud.setAlpha(150)
        p.setPen(QPen(cloud, 0.9))
        for ia, ib, oa, ob in self._base:
            self._arrow(p, self._point(plot, scale, ia, ib), self._point(plot, scale, oa, ob), 3.0)
        # marked grid points of the selected finding, on top
        flag_pen = QPen(col["flag"], 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        for ia, ib, oa, ob in self._marked:
            a, b = self._point(plot, scale, ia, ib), self._point(plot, scale, oa, ob)
            p.setPen(flag_pen)
            self._arrow(p, a, b, 4.0)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(b, 3.5, 3.5)
        # current sample
        if self._current is not None:
            ia, ib, oa, ob = self._all()[self._current]
            a, b = self._point(plot, scale, ia, ib), self._point(plot, scale, oa, ob)
            p.setPen(QPen(col["text"], 1.8))
            p.setBrush(col["bg"])
            p.drawEllipse(a, 2.5, 2.5)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(b, 6.0, 6.0)
        p.restore()
        self._paint_focus(p, col)
        p.end()

    def _arrow(self, p: QPainter, a: QPointF, b: QPointF, head: float) -> None:
        p.drawLine(a, b)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy)
        if length > 2.0 * head:
            ux, uy = dx / length, dy / length
            for sign in (+1, -1):
                rx = -ux * math.cos(0.5) + sign * uy * math.sin(0.5)
                ry = -uy * math.cos(0.5) - sign * ux * math.sin(0.5)
                p.drawLine(b, QPointF(b.x() + rx * head, b.y() + ry * head))

    def _paint_legend_sample(self, p: QPainter, col: dict, kind: str, rect: QRectF) -> None:
        y = rect.center().y()
        a, b = QPointF(rect.left(), y), QPointF(rect.left() + SAMPLE_W - 4, y)
        if kind == "arrow":
            p.setPen(QPen(col["axis"], 1.2))
            p.drawLine(a, b)
            p.drawLine(b, QPointF(b.x() - 4, b.y() - 3))
            p.drawLine(b, QPointF(b.x() - 4, b.y() + 3))
        elif kind == "flag":
            p.setPen(QPen(col["flag"], 1.8))
            p.drawLine(a, QPointF(b.x() - 6, b.y()))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(b.x() - 6, b.y()), 3.5, 3.5)
        elif kind == "rings":
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col["axis"], 1.2, Qt.PenStyle.DashLine))
            p.drawEllipse(QPointF(rect.left() + 12, y), 5.0, 5.0)

    def _paint_focus(self, p: QPainter, col: dict) -> None:
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col["focus"], 2))
            p.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))

    # ------------------------------------------------------------ interaction
    def nearest(self, pos: QPointF, radius: float = 12.0) -> int | None:
        """Index of the arrow (marked ones win ties) whose start or end is within ``radius`` px of ``pos``."""
        arr = self._all()
        if not len(arr):
            return None
        plot, scale, *_ = self._layout()
        centre = plot.center()
        ends = np.concatenate([arr[:, 0:2], arr[:, 2:4]])
        d = np.hypot(centre.x() + ends[:, 0] * scale - pos.x(), centre.y() - ends[:, 1] * scale - pos.y())
        d = d.copy()
        marked = np.zeros(len(arr), bool)
        marked[len(self._base):] = True
        d[np.concatenate([marked, marked])] -= 3.0   # prefer the finding's own points when overlapping
        i = int(np.argmin(d))
        return i % len(arr) if d[i] <= radius else None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.has_data():
            self.set_current(self.nearest(event.position()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self.hasFocus():
            self.set_current(None)
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def step_marked(self, direction: int) -> None:
        """Move to the next/previous marked grid point of the selected finding."""
        indices = self.marked_indices()
        if not indices:
            return
        if self._current in indices:
            pos = (indices.index(self._current) + direction) % len(indices)
        else:
            pos = 0 if direction > 0 else len(indices) - 1
        self.set_current(indices[pos])

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Right and self._marked.size:
            self.step_marked(+1)
        elif key == Qt.Key.Key_Left and self._marked.size:
            self.step_marked(-1)
        else:
            return super().keyPressEvent(event)
        event.accept()
