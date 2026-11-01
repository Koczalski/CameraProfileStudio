"""Bottom panel: Jobs table (Model/View) and processing log.

Log entries store translation keys + arguments and are re-rendered on a
language switch, so the whole log follows the UI language.  Raw backend
details (paths, exception text) are kept verbatim.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QListView,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..i18n import T, tr
from ..jobs import FINAL_STATES, JobManager
from .common import SPACE_S, SPACE_XS, make_button


@dataclass
class LogEntry:
    level: str  # info | success | warning | error
    key: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    stamp: float = field(default_factory=time.time)

    def render(self) -> str:
        text = f"{time.strftime('%H:%M:%S', time.localtime(self.stamp))}  [{tr('log.level.' + self.level)}]  {tr(self.key, **self.kwargs)}"
        if self.detail:
            text += f" \u2014 {self.detail}"
        return text


class LogModel(QAbstractListModel):
    MAX = 2000

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[LogEntry] = []
        T.watch(self, lambda: self._retranslate(""))

    def _retranslate(self, _l: str) -> None:
        if self.entries:
            self.dataChanged.emit(self.index(0), self.index(len(self.entries) - 1))

    def add(self, entry: LogEntry) -> None:
        if len(self.entries) >= self.MAX:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self.entries.pop(0)
            self.endRemoveRows()
        n = len(self.entries)
        self.beginInsertRows(QModelIndex(), n, n)
        self.entries.append(entry)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.entries.clear()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry = self.entries[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole):
            return entry.render()
        if role == Qt.ItemDataRole.DecorationRole:
            name = {"error": "error_circle", "warning": "warning", "success": "checkmark_circle"}.get(entry.level, "info")
            return icons.icon(name)
        return None


class Logger:
    """Thin facade used by workspaces: ``log.info("key", detail=..., **kw)``."""

    def __init__(self, model: LogModel) -> None:
        self.model = model

    def _add(self, level: str, key: str, detail: str = "", **kwargs: Any) -> None:
        self.model.add(LogEntry(level, key, kwargs, detail))

    def info(self, key: str, detail: str = "", **kwargs: Any) -> None:
        self._add("info", key, detail, **kwargs)

    def success(self, key: str, detail: str = "", **kwargs: Any) -> None:
        self._add("success", key, detail, **kwargs)

    def warning(self, key: str, detail: str = "", **kwargs: Any) -> None:
        self._add("warning", key, detail, **kwargs)

    def error(self, key: str, detail: str = "", **kwargs: Any) -> None:
        self._add("error", key, detail, **kwargs)


class BottomPanel(QTabWidget):
    """Tabs: Jobs (cancellable) and Log (copyable)."""

    cancel_requested = Signal(int)

    def __init__(self, jobs: JobManager, log_model: LogModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.setDocumentMode(True)
        # Jobs tab
        jobs_page = QWidget()
        jl = QVBoxLayout(jobs_page)
        jl.setContentsMargins(SPACE_XS, SPACE_XS, SPACE_XS, SPACE_XS)
        jl.setSpacing(SPACE_S)
        self.jobs_view = QTableView(jobs_page)
        self.jobs_view.setModel(jobs.model)
        self.jobs_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_view.verticalHeader().setVisible(False)
        self.jobs_view.setWordWrap(False)
        self.jobs_view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.jobs_view.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        header.resizeSection(0, 260)
        T.bind(self.jobs_view, "setAccessibleName", "jobs.accessible")
        jl.addWidget(self.jobs_view, 1)
        row = QHBoxLayout()
        row.setSpacing(SPACE_S)
        self.cancel_button = make_button("jobs.cancel_selected", "dismiss_circle")
        self.cancel_all_button = make_button("jobs.cancel_all", "dismiss_circle")
        row.addWidget(self.cancel_button)
        row.addWidget(self.cancel_all_button)
        row.addStretch(1)
        jl.addLayout(row)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.cancel_all_button.clicked.connect(jobs.cancel_all)
        self.jobs_view.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        jobs.job_changed.connect(lambda _id: self._update_buttons())
        jobs.job_finished.connect(lambda *_: self._update_buttons())
        # Log tab
        log_page = QWidget()
        ll = QVBoxLayout(log_page)
        ll.setContentsMargins(SPACE_XS, SPACE_XS, SPACE_XS, SPACE_XS)
        ll.setSpacing(SPACE_S)
        self.log_view = QListView(log_page)
        self.log_view.setModel(log_model)
        self.log_view.setUniformItemSizes(True)
        self.log_view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.log_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        T.bind(self.log_view, "setAccessibleName", "log.accessible")
        ll.addWidget(self.log_view, 1)
        lrow = QHBoxLayout()
        self.copy_button = make_button("log.copy", None)
        self.clear_button = make_button("log.clear", "delete")
        lrow.addWidget(self.copy_button)
        lrow.addWidget(self.clear_button)
        lrow.addStretch(1)
        ll.addLayout(lrow)
        self.copy_button.clicked.connect(self._copy_log)
        self.clear_button.clicked.connect(log_model.clear)
        log_model.rowsInserted.connect(lambda *_: self.log_view.scrollToBottom())
        self.log_model = log_model
        self.addTab(jobs_page, icons.icon("table"), "")
        self.addTab(log_page, icons.icon("document_table"), "")
        T.watch(self, lambda: self._retranslate())
        self._retranslate()
        self._update_buttons()

    def _retranslate(self) -> None:
        self.setTabText(0, tr("panel.jobs"))
        self.setTabText(1, tr("panel.log"))

    def _selected_job(self) -> int | None:
        rows = self.jobs_view.selectionModel().selectedRows()
        if not rows:
            return None
        rec = self.jobs.model.job_at(rows[0].row())
        return rec.job_id if rec else None

    def _update_buttons(self) -> None:
        jid = self._selected_job()
        rec = self.jobs.records.get(jid) if jid else None
        self.cancel_button.setEnabled(bool(rec and rec.state not in FINAL_STATES))
        self.cancel_all_button.setEnabled(bool(self.jobs.active()))

    def _cancel_selected(self) -> None:
        jid = self._selected_job()
        if jid:
            self.jobs.cancel(jid)

    def _copy_log(self) -> None:
        rows = sorted(i.row() for i in self.log_view.selectionModel().selectedIndexes())
        entries = [self.log_model.entries[r] for r in rows] if rows else self.log_model.entries
        QGuiApplication.clipboard().setText("\n".join(e.render() for e in entries))



