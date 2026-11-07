"""Camera Match: the error of every patch, one stage at a time, sortable and saveable.

The numbers come from the engine's ``PatchErrors`` record; nothing is computed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..i18n import T, tr
from ..jobs import ErrorInfo
from .common import SPACE_M, SPACE_S, WrapLabel, make_button

SortRole = Qt.ItemDataRole.UserRole + 1
STAGES = ("before", "ccm", "tone", "residual", "baked")


class PatchTableModel(QAbstractTableModel):
    """Rows are samples, columns are fixed; ``stage`` selects which layer of the record is shown."""

    # header: a literal symbol, or a translation key
    COLUMNS = ("#", "match.patches.col.group", "match.patches.col.set", "match.patches.col.source",
               "match.patches.col.target", "match.patches.col.result", "ΔE00", "ΔL*", "ΔC*", "ΔH*")
    COL_INDEX, COL_GROUP, COL_SET, COL_SOURCE, COL_TARGET, COL_RESULT, COL_DE, COL_DL, COL_DC, COL_DH = range(10)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.errors: Any = None
        self.stage = 0
        T.watch(self, self._retranslate)

    def _retranslate(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.COLUMNS) - 1)
        if self.errors is not None:
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, len(self.COLUMNS) - 1))

    def set_errors(self, errors: Any, stage: str | None = None) -> None:
        self.beginResetModel()
        self.errors = errors
        if errors is not None:
            stages = list(errors.stages)
            self.stage = stages.index(stage) if stage in stages else len(stages) - 1
        self.endResetModel()

    def set_stage(self, stage: str) -> None:
        if self.errors is None or stage not in self.errors.stages:
            return
        self.stage = list(self.errors.stages).index(stage)
        if self.rowCount():
            self.dataChanged.emit(self.index(0, self.COL_RESULT), self.index(self.rowCount() - 1, self.COL_DH))

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() or self.errors is None else int(len(self.errors.indices))

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            name = self.COLUMNS[section]
            return tr(name) if name.startswith("match.") else name
        return None

    def _set_key(self, row: int) -> str:
        e = self.errors
        return "holdout" if e.holdout[row] else ("train" if e.train[row] else "unused")

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or self.errors is None:
            return None
        e, row, col, s = self.errors, index.row(), index.column(), self.stage
        if role == Qt.ItemDataRole.DecorationRole:
            rgb = {self.COL_SOURCE: e.source_rgb[row], self.COL_TARGET: e.target_rgb[row],
                   self.COL_RESULT: e.predicted_rgb[s, row]}.get(col)
            return None if rgb is None else QColor.fromRgbF(float(rgb[0]), float(rgb[1]), float(rgb[2]))
        if role == Qt.ItemDataRole.TextAlignmentRole and col >= self.COL_DE:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == SortRole:
            if col == self.COL_INDEX:
                return int(e.indices[row])
            if col == self.COL_GROUP:
                return str(e.groups[row])
            if col == self.COL_SET:
                return self._set_key(row)
            if col in (self.COL_SOURCE, self.COL_TARGET, self.COL_RESULT):
                return float(e.delta_e00[s, row])
            return float({self.COL_DE: e.delta_e00, self.COL_DL: e.delta_l, self.COL_DC: e.delta_c,
                          self.COL_DH: e.delta_h}[col][s, row])
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if col == self.COL_INDEX:
            return str(int(e.indices[row]))
        if col == self.COL_GROUP:
            return str(e.groups[row])
        if col == self.COL_SET:
            return tr(f"match.patches.set.{self._set_key(row)}")
        if col in (self.COL_SOURCE, self.COL_TARGET, self.COL_RESULT):
            rgb = {self.COL_SOURCE: e.source_rgb[row], self.COL_TARGET: e.target_rgb[row],
                   self.COL_RESULT: e.predicted_rgb[s, row]}[col]
            text = " ".join(f"{float(v):.3f}" for v in rgb)
            return text if role != Qt.ItemDataRole.DisplayRole else ""
        if col == self.COL_DE:
            return f"{float(e.delta_e00[s, row]):.2f}"
        value = {self.COL_DL: e.delta_l, self.COL_DC: e.delta_c, self.COL_DH: e.delta_h}[col][s, row]
        return f"{float(value):+.2f}"


class PatchTableDialog(QDialog):
    """Non-modal window with the patch table of the current match; the page refreshes it after a fit."""

    def __init__(self, ctx, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("patch_table_dialog")
        self.setModal(False)
        T.bind(self, "setWindowTitle", "match.patches.title")
        self.resize(980, 620)
        lay = QVBoxLayout(self)
        lay.setSpacing(SPACE_M)
        top = QHBoxLayout()
        top.setSpacing(SPACE_S)
        stage_label = QLabel(self)
        T.bind(stage_label, "setText", "match.patches.stage")
        self.stage_combo = QComboBox(self)
        for stage in STAGES:
            self.stage_combo.addItem(tr(f"match.patches.stage.{stage}"), stage)
        T.watch(self.stage_combo, self._retranslate_stages)
        stage_label.setBuddy(self.stage_combo)
        T.bind(self.stage_combo, "setAccessibleName", "match.patches.stage")
        top.addWidget(stage_label)
        top.addWidget(self.stage_combo)
        top.addStretch(1)
        self.count_label = QLabel("", self)
        top.addWidget(self.count_label)
        lay.addLayout(top)
        lay.addWidget(WrapLabel("match.patches.note", secondary=True, parent=self))
        self.model = PatchTableModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SortRole)
        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(PatchTableModel.COL_DE, Qt.SortOrder.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(False)   # keep the number columns side by side
        T.bind(self.table, "setAccessibleName", "match.patches.accessible")
        lay.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        bottom.setSpacing(SPACE_S)
        self.save_button = make_button("match.patches.save", "arrow_export_ltr", parent=self)
        self.close_button = make_button("match.patches.close", parent=self)
        bottom.addWidget(self.save_button)
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        lay.addLayout(bottom)
        self.stage_combo.currentIndexChanged.connect(lambda _i: self.model.set_stage(self.stage_combo.currentData()))
        self.save_button.clicked.connect(self.save)
        self.close_button.clicked.connect(self.close)
        T.watch(self, self._render_count)
        self.set_errors(None)

    def _retranslate_stages(self) -> None:
        for i in range(self.stage_combo.count()):
            self.stage_combo.setItemText(i, tr(f"match.patches.stage.{self.stage_combo.itemData(i)}"))

    def set_errors(self, errors: Any) -> None:
        """Show ``errors`` (None: empty). Stages the record lacks are hidden; the last one is selected."""
        self.model.set_errors(errors)
        combo = self.stage_combo
        combo.blockSignals(True)
        for i in range(combo.count()):
            present = errors is not None and combo.itemData(i) in errors.stages
            combo.view().setRowHidden(i, not present)
        if errors is not None:
            combo.setCurrentIndex(combo.findData(errors.stages[-1]))
        combo.blockSignals(False)
        self.table.sortByColumn(PatchTableModel.COL_DE, Qt.SortOrder.DescendingOrder)
        self.save_button.setEnabled(errors is not None)
        self._render_count()

    def _render_count(self) -> None:
        e = self.model.errors
        self.count_label.setText("" if e is None else tr("match.patches.count", n=len(e.indices), h=int(e.holdout.sum())))

    def save(self) -> None:
        errors = self.model.errors
        if errors is None:
            return
        path, kind = self.ctx.ask_save_typed("match.patches.save_title", "match_patches.csv",
                                             [("csv", "filter.csv"), ("json", "filter.json")], "csv", "match")
        if not path:
            return
        try:
            text = self.ctx.engine.patch_errors_text(errors, kind)
            Path(path).write_text(text, encoding="utf-8", newline="")
        except OSError as exc:
            info = ErrorInfo("error.file", detail=str(exc))
            self.ctx.log.error("log.match.patches_failed", detail=f"{path}: {exc}")
            self.ctx.show_error(info, "dialog.export_failed")
            return
        self.ctx.log.success("log.match.patches_saved", detail=path)
