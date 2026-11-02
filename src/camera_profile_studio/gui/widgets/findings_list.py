"""Findings list and explanation panel of the LUT Check workspace.

``FindingsView`` is a Model/View list (``FindingsModel`` + ``FindingDelegate``):
one row per finding with a severity icon, a plain sentence, the measured value
and, for findings that need attention, the name and severity in words.  The
severity colour bar is an extra cue only.  ``ExplanationPanel`` shows what the
selected finding means, why it matters and what to do.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QListView,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..backend import HealthFindingView
from ..i18n import T, tr
from .chart_style import severity_color
from .common import SPACE_S, SPACE_XS, HeadingLabel, WrapLabel

SEVERITY_ICON = {"ok": "checkmark_circle", "info": "info", "warning": "warning", "error": "error_circle"}
PERCENT_CODES = ("range", "tone_reversal", "clipping", "flatten")


# ----------------------------------------------------------------------------
# text helpers (language independent number formatting, translated sentences)
# ----------------------------------------------------------------------------


def format_percent(fraction: float) -> str:
    if fraction <= 0:
        return "0 %"
    if fraction < 0.0005:
        return "<0.1 %"
    return f"{fraction * 100:.1f} %"


def format_value(code: str, value: float) -> str:
    """Measured value of a finding, short enough for a list row."""
    if code in PERCENT_CODES:
        return format_percent(value)
    if code == "gray_tint":
        return f"{value:.3f}"
    if code == "tone_jump":
        return f"{value:.3f} L" if value else "0"  # largest Oklab lightness step (unit L); the ratio is in the explanation
    if code == "hue_fold":
        return f"{int(round(value))}"
    if code == "banding_risk":
        return f"{value:.1f}×"
    return f"{value:g}"


def _ratio_text(raw: str, digits: int | None = None) -> str:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if digits is not None:
        return f"{value:.{digits}f}"
    return f"{value:.0f}" if value >= 100 else f"{value:.1f}"


def format_limit(code: str, threshold: float) -> str:
    if code in PERCENT_CODES:
        return f"{threshold * 100:g} %"
    if code == "tone_jump":
        return f"{threshold:g} L"
    return f"{threshold:g}"


def sentence_key(code: str, severity: str) -> str:
    """Translation key of the one-sentence summary for ``code`` at ``severity``."""
    for sev in (severity, "warning"):
        key = f"health.finding.{code}.{sev}"
        if tr(key) != key:
            return key
    return f"health.finding.{code}.ok"


def sentence(code: str, severity: str) -> str:
    return tr(sentence_key(code, severity))


@dataclass
class FindingRow:
    code: str
    severity: str
    sentence: str
    value: str
    caption: str = ""
    tooltip: str = ""
    accessible: str = ""


class FindingsModel(QAbstractListModel):
    CodeRole = Qt.ItemDataRole.UserRole + 1
    SeverityRole = Qt.ItemDataRole.UserRole + 2
    CaptionRole = Qt.ItemDataRole.UserRole + 3
    ValueRole = Qt.ItemDataRole.UserRole + 4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[FindingRow] = []

    def set_rows(self, rows: list[FindingRow]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def row_of(self, code: str) -> int:
        return next((i for i, r in enumerate(self.rows) if r.code == code), -1)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.sentence
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return row.accessible
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.tooltip
        if role == Qt.ItemDataRole.DecorationRole:
            return icons.icon(SEVERITY_ICON.get(row.severity, "info"))
        if role == self.CodeRole:
            return row.code
        if role == self.SeverityRole:
            return row.severity
        if role == self.CaptionRole:
            return row.caption
        if role == self.ValueRole:
            return row.value
        return None


class FindingDelegate(QStyledItemDelegate):
    PAD_X, PAD_Y, ICON, GAP, BAR = 8, 4, 18, 8, 3

    def __init__(self, view: "FindingsView") -> None:
        super().__init__(view)
        self._view = view

    def _fonts(self, option: QStyleOptionViewItem, severity: str) -> tuple[QFont, QFont]:
        main = QFont(option.font)
        if severity in ("warning", "error"):
            main.setWeight(QFont.Weight.DemiBold)
        small = QFont(option.font)
        if small.pointSizeF() > 0:
            small.setPointSizeF(max(7.0, small.pointSizeF() - 1.0))
        return main, small

    def _geometry(self, option: QStyleOptionViewItem, index: QModelIndex, width: int) -> tuple[QRect, QRect, QRect, int, int]:
        severity = index.data(FindingsModel.SeverityRole)
        main, small = self._fonts(option, severity)
        fm, fms = QFontMetrics(main), QFontMetrics(small)
        value = index.data(FindingsModel.ValueRole) or ""
        value_w = fm.horizontalAdvance(value) + 2
        left = self.PAD_X + self.BAR + self.GAP + self.ICON + self.GAP
        text_w = max(40, width - left - self.GAP - value_w - self.PAD_X)
        sentence_h = fm.boundingRect(QRect(0, 0, text_w, 100000), int(Qt.TextFlag.TextWordWrap), index.data()).height()
        caption = index.data(FindingsModel.CaptionRole) or ""
        caption_h = fms.height() if caption else 0
        total = self.PAD_Y * 2 + sentence_h + (2 + caption_h if caption else 0)
        text_rect = QRect(left, self.PAD_Y, text_w, sentence_h)
        value_rect = QRect(width - self.PAD_X - value_w, self.PAD_Y, value_w, fm.height())
        caption_rect = QRect(left, self.PAD_Y + sentence_h + 2, width - left - self.PAD_X, caption_h)
        return text_rect, value_rect, caption_rect, total, fm.height()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        width = max(120, self._view.viewport().width())
        _t, _v, _c, total, _h = self._geometry(option, index, width)
        return QSize(width, max(30, total))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        severity = index.data(FindingsModel.SeverityRole)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.save()
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)
        rect = option.rect
        painter.translate(rect.topLeft())
        width = rect.width()
        text_rect, value_rect, caption_rect, total, line_h = self._geometry(option, index, width)
        main, small = self._fonts(option, severity)
        # text colour: windows11 keeps the normal text colour on selection (see icons.py)
        keep = style is not None and style.name().lower() == "windows11"
        text_color = option.palette.highlightedText().color() if (selected and not keep) else option.palette.text().color()
        muted = text_color if selected else option.palette.placeholderText().color()
        if severity in ("warning", "error", "info"):
            painter.fillRect(QRect(self.PAD_X // 2, 4, self.BAR, max(8, total - 8)), severity_color(option.palette, severity))
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None:
            mode = QIcon.Mode.Selected if (selected and not keep) else QIcon.Mode.Normal
            icon.paint(painter, QRect(self.PAD_X + self.BAR + self.GAP, self.PAD_Y + (line_h - self.ICON) // 2, self.ICON, self.ICON),
                       Qt.AlignmentFlag.AlignCenter, mode)
        painter.setFont(main)
        painter.setPen(text_color)
        painter.drawText(text_rect, int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop), index.data())
        painter.drawText(value_rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop), index.data(FindingsModel.ValueRole) or "")
        caption = index.data(FindingsModel.CaptionRole)
        if caption:
            painter.setFont(small)
            painter.setPen(muted)
            painter.drawText(caption_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             QFontMetrics(small).elidedText(caption, Qt.TextElideMode.ElideRight, caption_rect.width()))
        painter.restore()


class FindingsView(QListView):
    finding_selected = Signal(str)  # finding code

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.findings_model = FindingsModel(self)
        self.setModel(self.findings_model)
        self.setItemDelegate(FindingDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(170)
        self._laid_out_width = -1
        self.selectionModel().currentChanged.connect(self._current_changed)
        self.verticalScrollBar().rangeChanged.connect(lambda *_: self._relayout_if_needed())
        T.bind(self, "setAccessibleName", "health.findings.accessible")

    def set_rows(self, rows: list[FindingRow], select: str | None = None) -> None:
        self.findings_model.set_rows(rows)
        self._laid_out_width = self.viewport().width()
        target = self.findings_model.row_of(select) if select else -1
        if target < 0:
            target = next((i for i, r in enumerate(rows) if r.severity in ("warning", "error")), 0 if rows else -1)
        if target >= 0:
            self.setCurrentIndex(self.findings_model.index(target))
        else:
            self.clearSelection()
            self.finding_selected.emit("")

    def current_code(self) -> str:
        idx = self.currentIndex()
        return str(idx.data(FindingsModel.CodeRole)) if idx.isValid() else ""

    def _current_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        self.finding_selected.emit(str(current.data(FindingsModel.CodeRole)) if current.isValid() else "")

    def _relayout_if_needed(self) -> None:
        if self.viewport().width() != self._laid_out_width:
            self._laid_out_width = self.viewport().width()
            self.doItemsLayout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout_if_needed()


# ----------------------------------------------------------------------------
# explanation
# ----------------------------------------------------------------------------


class ExplanationPanel(QScrollArea):
    """What the selected finding means, why it matters, what to do, and the measured numbers."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(90)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
        lay.setSpacing(SPACE_XS)
        self.title = HeadingLabel("", 3, inner)
        self.summary = WrapLabel("", secondary=True, parent=inner)
        self.heads: dict[str, QLabel] = {}
        self.bodies: dict[str, WrapLabel] = {}
        lay.addWidget(self.title)
        lay.addWidget(self.summary)
        for part in ("what", "why", "todo", "measured"):
            head = QLabel(inner)
            font = head.font()
            font.setWeight(QFont.Weight.DemiBold)
            head.setFont(font)
            body = WrapLabel("", parent=inner)
            self.heads[part], self.bodies[part] = head, body
            lay.addSpacing(SPACE_XS)
            lay.addWidget(head)
            lay.addWidget(body)
        self.empty = WrapLabel("health.explain.empty", secondary=True, parent=inner)
        lay.addWidget(self.empty)
        lay.addStretch(1)
        self.setWidget(inner)
        self._finding: HealthFindingView | None = None
        self._before: HealthFindingView | None = None
        T.bind(self, "setAccessibleName", "health.explain.accessible")
        T.watch(self, self._render)
        self._render()

    def show_finding(self, finding: HealthFindingView | None, before: HealthFindingView | None = None) -> None:
        self._finding, self._before = finding, before
        self._render()

    def current_code(self) -> str:
        return self._finding.code if self._finding else ""

    def _render(self) -> None:
        f = self._finding
        parts = ("what", "why", "todo", "measured")
        self.empty.setVisible(f is None)
        self.title.setVisible(f is not None)
        self.summary.setVisible(f is not None)
        for part in parts:
            self.heads[part].setVisible(f is not None)
            self.bodies[part].setVisible(f is not None)
        if f is None:
            return
        code = f.code
        self.title.setText(tr(f"health.name.{code}"))
        self.summary.setText(f"{tr('health.severity.' + f.severity)} — {sentence(code, f.severity)}")
        self.heads["what"].setText(tr("health.explain.what"))
        self.heads["why"].setText(tr("health.explain.why"))
        self.heads["todo"].setText(tr("health.explain.todo"))
        self.heads["measured"].setText(tr("health.explain.measured"))
        self.bodies["what"].setText(tr(f"health.explain.{code}.what"))
        self.bodies["why"].setText(tr(f"health.explain.{code}.why"))
        self.bodies["todo"].setText(tr(f"health.explain.{code}.todo") if f.severity != "ok" else tr("health.explain.nothing"))
        lines = [f"{tr('health.measure.' + code)}: {format_value(code, f.value)}",
                 tr(f"health.limit.{code}", limit=format_limit(code, f.threshold))]
        if f.count:
            lines.append(tr("health.explain.affected", count=f.count))
        if self._before is not None and self._before.value != f.value:
            lines.append(tr("health.row.tooltip_before", value=format_value(code, self._before.value)))
        detail_map = dict(f.details)
        if code == "tone_jump" and detail_map.get("robust_ratio"):
            lines.append(f"{tr('health.measure.tone_jump_ratio')}: ×{_ratio_text(detail_map['robust_ratio'])}")
        if code == "gray_tint" and detail_map.get("max_second_difference"):
            lines.append(f"{tr('health.measure.gray_tint_change')}: {_ratio_text(detail_map['max_second_difference'], 3)}")
        hidden = {"tone_jump": ("robust_ratio", "ratio_limit", "step_unit"), "gray_tint": ("max_second_difference",)}.get(code, ())
        shown = [(k, v) for k, v in f.details if k not in hidden]
        if shown:
            lines.append(" · ".join(f"{k} {v}" for k, v in shown))
        self.bodies["measured"].setText("\n".join(lines))
