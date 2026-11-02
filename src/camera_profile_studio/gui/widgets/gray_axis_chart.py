"""Grey-axis chart: output R, G, B and Oklab L against the grey input level.

Painted with QPainter from the engine's ``HealthReport.gray_axis`` rows
``(t, R, G, B, L, a, b)``.  No colour maths happens here: the widget only
draws, reads values out and marks the grid nodes the engine flagged.

Accessibility: the widget takes keyboard focus; Left/Right move a read-out
cursor (Shift = 8 steps, Home/End = ends; Esc stays the workspace's Cancel), the same values are
shown for the mouse pointer, and ``accessibleDescription`` summarises the
curve in words.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QKeyEvent, QMouseEvent, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..i18n import T, tr
from .chart_style import chart_colors

SERIES = (("r", 1, "R"), ("g", 2, "G"), ("b", 3, "B"))


def _fmt(v: float) -> str:
    return f"{v:.3f}"


class GrayAxisChart(QWidget):
    cursor_changed = Signal(object)  # input level 0..1, or None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(200, 170)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._data: np.ndarray | None = None
        self._ref: np.ndarray | None = None
        self._flag_sel: list[float] = []
        self._flag_other: list[float] = []
        self._cursor: float | None = None
        T.bind(self, "setAccessibleName", "health.gray.accessible")
        T.watch(self, self._retranslate)
        self._retranslate()

    # ------------------------------------------------------------------ data
    def set_data(self, gray_axis: np.ndarray | None, *, reference: np.ndarray | None = None,
                 flagged_selected: Sequence[float] = (), flagged_other: Sequence[float] = ()) -> None:
        """``gray_axis``: (m, 7) rows t, R, G, B, L, a, b.  ``reference``: the same for the LUT before a repair."""
        self._data = None if gray_axis is None or len(gray_axis) < 2 else np.asarray(gray_axis, dtype=np.float64)
        self._ref = None if reference is None or len(reference) < 2 else np.asarray(reference, dtype=np.float64)
        self._flag_sel = [float(t) for t in flagged_selected]
        self._flag_other = [float(t) for t in flagged_other if float(t) not in self._flag_sel]
        if self._data is None:
            self._cursor = None
        self._retranslate()
        self.update()

    def has_data(self) -> bool:
        return self._data is not None

    def flagged_count(self) -> int:
        return len(set(self._flag_sel) | set(self._flag_other))

    # --------------------------------------------------------------- read-out
    def cursor(self) -> float | None:
        return self._cursor

    def set_cursor(self, t: float | None) -> None:
        if t is not None:
            t = min(1.0, max(0.0, float(t)))
            if self._data is not None:
                steps = len(self._data) - 1
                t = round(t * steps) / steps
        if t != self._cursor:
            self._cursor = t
            self.cursor_changed.emit(t)
            self.update()

    def values_at(self, t: float) -> tuple[float, float, float, float, float] | None:
        """(input, R, G, B, L) at the sample nearest to input level ``t``."""
        if self._data is None:
            return None
        row = self._data[int(round(min(1.0, max(0.0, t)) * (len(self._data) - 1)))]
        return float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])

    def readout_text(self) -> str:
        if self._data is None:
            return ""
        if self._cursor is None:
            return tr("health.gray.hint")
        t, r, g, b, lightness = self.values_at(self._cursor)  # type: ignore[misc]
        return tr("health.gray.readout", t=_fmt(t), r=_fmt(r), g=_fmt(g), b=_fmt(b), l=_fmt(lightness))

    def description_text(self) -> str:
        if self._data is None:
            return tr("health.gray.empty")
        v0, v5, v1 = self.values_at(0.0), self.values_at(0.5), self.values_at(1.0)
        return tr("health.gray.description", r0=_fmt(v0[1]), g0=_fmt(v0[2]), b0=_fmt(v0[3]), r5=_fmt(v5[1]),
                  g5=_fmt(v5[2]), b5=_fmt(v5[3]), r1=_fmt(v1[1]), g1=_fmt(v1[2]), b1=_fmt(v1[3]), n=self.flagged_count())

    def _retranslate(self) -> None:
        self.setAccessibleDescription(self.description_text())
        self.update()

    # ----------------------------------------------------------------- layout
    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(520, 380)

    def _small_font(self):
        font = self.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        return font

    def _y_range(self) -> tuple[float, float]:
        lo, hi = 0.0, 1.0
        for arr in (self._data, self._ref):
            if arr is not None:
                finite = arr[:, 1:5][np.isfinite(arr[:, 1:5])]
                if finite.size:
                    lo, hi = min(lo, float(finite.min())), max(hi, float(finite.max()))
        pad = 0.02 * (hi - lo)
        return lo - pad, hi + pad

    def _legend_items(self) -> list[tuple[str, str]]:
        items = [("r", "R"), ("g", "G"), ("b", "B"), ("lum", tr("health.gray.legend.lightness")),
                 ("id", tr("health.gray.legend.identity"))]
        if self._ref is not None:
            items.append(("ref", tr("health.gray.legend.before")))
        if self._flag_sel or self._flag_other:
            items.append(("flag", tr("health.gray.legend.flagged")))
        return items

    def _layout(self) -> tuple[QRectF, list[tuple[str, str, QRectF]], QRectF, float]:
        """plot rect, legend placements (kind, text, rect), read-out rect, legend bottom."""
        fm = QFontMetricsF(self._small_font())
        line = fm.height()
        w = float(self.width())
        pad = 8.0
        # legend (wraps)
        x, y = pad, pad
        placed: list[tuple[str, str, QRectF]] = []
        sample = 24.0
        for kind, text in self._legend_items():
            iw = sample + 6 + fm.horizontalAdvance(text) + 2 + 14
            if x + iw > w - pad and x > pad:
                x, y = pad, y + line + 4
            placed.append((kind, text, QRectF(x, y, iw - 14, line)))
            x += iw
        legend_bottom = y + line
        # the read-out wraps; reserve room for the longest text it can show so the plot does not jump
        box = QRectF(0, 0, max(20.0, w - 2 * pad), 10000.0)
        read_h = max(fm.boundingRect(box, int(Qt.TextFlag.TextWordWrap), tr("health.gray.hint")).height(),
                     fm.boundingRect(box, int(Qt.TextFlag.TextWordWrap), self.readout_text()).height(), line)
        readout = QRectF(pad, legend_bottom + 4, w - 2 * pad, read_h)
        left = pad + line + 6 + fm.horizontalAdvance("0.00") + 6   # axis title + tick labels
        top = readout.bottom() + 8
        bottom = float(self.height()) - pad - line - 4 - line - 4  # tick labels + axis title
        plot = QRectF(left, top, max(10.0, w - left - pad - 4), max(10.0, bottom - top))
        return plot, placed, readout, legend_bottom

    def _to_screen(self, plot: QRectF, ylo: float, yhi: float, t: float, v: float) -> QPointF:
        return QPointF(plot.left() + t * plot.width(), plot.bottom() - (v - ylo) / (yhi - ylo) * plot.height())

    def _polyline(self, plot: QRectF, ylo: float, yhi: float, arr: np.ndarray, column: int) -> QPolygonF:
        poly = QPolygonF()
        for row in arr:
            v = row[column]
            if np.isfinite(v):
                poly.append(self._to_screen(plot, ylo, yhi, float(row[0]), float(v)))
        return poly

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
        fm = QFontMetricsF(small)
        if self._data is None:
            p.setPen(col["muted"])
            p.drawText(QRectF(self.rect()).adjusted(12, 12, -12, -12), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       tr("health.gray.empty"))
            self._paint_focus(p, col)
            p.end()
            return
        plot, legend, readout, _ = self._layout()
        ylo, yhi = self._y_range()
        # legend
        for kind, text, rect in legend:
            self._paint_legend_sample(p, col, kind, rect, fm)
            p.setPen(col["text"])
            p.drawText(QRectF(rect.left() + 30, rect.top(), rect.width() - 30, rect.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        # read-out line
        p.setPen(col["text"] if self._cursor is not None else col["muted"])
        p.drawText(readout, int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap), self.readout_text())
        # grid and ticks
        ticks = [0.0, 0.25, 0.5, 0.75, 1.0]
        p.setPen(QPen(col["grid"], 1))
        for v in ticks:
            y = self._to_screen(plot, ylo, yhi, 0, v).y()
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            x = plot.left() + v * plot.width()
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        if ylo < 0 or yhi > 1:  # shade the part outside the valid 0..1 output range
            p.setPen(Qt.PenStyle.NoPen)
            shade = QColor(col["flag"])
            shade.setAlpha(28)
            p.setBrush(shade)
            y1 = self._to_screen(plot, ylo, yhi, 0, 1.0).y()
            y0 = self._to_screen(plot, ylo, yhi, 0, 0.0).y()
            if yhi > 1:
                p.drawRect(QRectF(plot.left(), plot.top(), plot.width(), y1 - plot.top()))
            if ylo < 0:
                p.drawRect(QRectF(plot.left(), y0, plot.width(), plot.bottom() - y0))
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(col["axis"], 1))
        p.drawRect(plot)
        p.setPen(col["muted"])
        for v in ticks:
            y = self._to_screen(plot, ylo, yhi, 0, v).y()
            p.drawText(QRectF(plot.left() - fm.horizontalAdvance("0.00") - 6, y - fm.height() / 2,
                              fm.horizontalAdvance("0.00") + 2, fm.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{v:g}")
            x = plot.left() + v * plot.width()
            p.drawText(QRectF(x - 20, plot.bottom() + 3, 40, fm.height()), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       f"{v:g}")
        # axis titles
        p.setPen(col["text"])
        p.drawText(QRectF(plot.left(), self.height() - 8 - fm.height(), plot.width(), fm.height()),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, tr("health.gray.axis_x"))
        p.save()
        p.translate(10 + fm.height() / 2, plot.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-plot.height() / 2, -fm.height() / 2, plot.height(), fm.height()),
                   Qt.AlignmentFlag.AlignCenter, tr("health.gray.axis_y"))
        p.restore()
        # curves (clipped to the plot)
        p.save()
        p.setClipRect(plot.adjusted(-1, -1, 1, 1))
        a, b = self._to_screen(plot, ylo, yhi, 0, 0), self._to_screen(plot, ylo, yhi, 1, 1)
        pen = QPen(col["muted"], 1.2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(a, b)
        if self._ref is not None:
            for key, column, _name in SERIES:
                ref_pen = QPen(col[key], 1.2, Qt.PenStyle.DotLine)
                ref_pen.setCosmetic(True)
                p.setPen(ref_pen)
                p.drawPolyline(self._polyline(plot, ylo, yhi, self._ref, column))
            ref_l = QPen(col["text"], 1.2, Qt.PenStyle.DotLine)
            p.setPen(ref_l)
            p.drawPolyline(self._polyline(plot, ylo, yhi, self._ref, 4))
        for key, column, _name in reversed(SERIES):
            p.setPen(QPen(col[key], 1.9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPolyline(self._polyline(plot, ylo, yhi, self._data, column))
        p.setPen(QPen(col["text"], 1.9, Qt.PenStyle.DashLine))
        p.drawPolyline(self._polyline(plot, ylo, yhi, self._data, 4))
        p.restore()
        # flagged nodes: ring on the lightness curve
        for positions, selected in ((self._flag_other, False), (self._flag_sel, True)):
            for t in positions:
                v = self.values_at(t)
                if v is None:
                    continue
                c = self._to_screen(plot, ylo, yhi, v[0], v[4])
                ring = QColor(col["flag"])
                if selected:
                    fill = QColor(ring)
                    fill.setAlpha(70)
                    p.setBrush(fill)
                    p.setPen(QPen(ring, 2.4))
                    p.drawEllipse(c, 7.0, 7.0)
                else:
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(ring, 1.4, Qt.PenStyle.DotLine))
                    p.drawEllipse(c, 5.5, 5.5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # cursor
        if self._cursor is not None:
            vals = self.values_at(self._cursor)
            x = plot.left() + vals[0] * plot.width()
            p.setPen(QPen(col["text"], 1, Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            for key, idx in (("r", 1), ("g", 2), ("b", 3)):
                c = self._to_screen(plot, ylo, yhi, vals[0], vals[idx])
                p.setBrush(col["bg"])
                p.setPen(QPen(col[key], 2))
                p.drawEllipse(c, 3.5, 3.5)
            c = self._to_screen(plot, ylo, yhi, vals[0], vals[4])
            p.setBrush(col["text"])
            p.setPen(QPen(col["bg"], 1))
            p.drawRect(QRectF(c.x() - 3, c.y() - 3, 6, 6))
        self._paint_focus(p, col)
        p.end()

    def _paint_legend_sample(self, p: QPainter, col: dict, kind: str, rect: QRectF, fm: QFontMetricsF) -> None:
        y = rect.center().y()
        a, b = QPointF(rect.left(), y), QPointF(rect.left() + 24, y)
        if kind in ("r", "g", "b"):
            p.setPen(QPen(col[kind], 2.2))
            p.drawLine(a, b)
        elif kind == "lum":
            p.setPen(QPen(col["text"], 2.2, Qt.PenStyle.DashLine))
            p.drawLine(a, b)
        elif kind == "id":
            p.setPen(QPen(col["muted"], 1.4, Qt.PenStyle.DashLine))
            p.drawLine(a, b)
        elif kind == "ref":
            p.setPen(QPen(col["text"], 1.4, Qt.PenStyle.DotLine))
            p.drawLine(a, b)
        elif kind == "flag":
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col["flag"], 2.2))
            p.drawEllipse(QPointF(rect.left() + 12, y), 5.0, 5.0)

    def _paint_focus(self, p: QPainter, col: dict) -> None:
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(col["focus"], 2))
            p.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))

    # ------------------------------------------------------------ interaction
    def _t_at(self, x: float) -> float:
        plot = self._layout()[0]
        return min(1.0, max(0.0, (x - plot.left()) / plot.width()))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._data is not None:
            self.set_cursor(self._t_at(event.position().x()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._data is not None:
            self.set_cursor(self._t_at(event.position().x()))
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self.hasFocus():
            self.set_cursor(None)
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._data is None:
            return super().keyPressEvent(event)
        steps = len(self._data) - 1
        big = 8 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        cur = self._cursor if self._cursor is not None else 0.5
        key = event.key()
        if key == Qt.Key.Key_Left:
            self.set_cursor(cur - big / steps)
        elif key == Qt.Key.Key_Right:
            self.set_cursor(cur + big / steps)
        elif key == Qt.Key.Key_Home:
            self.set_cursor(0.0)
        elif key == Qt.Key.Key_End:
            self.set_cursor(1.0)
        else:
            return super().keyPressEvent(event)
        event.accept()
