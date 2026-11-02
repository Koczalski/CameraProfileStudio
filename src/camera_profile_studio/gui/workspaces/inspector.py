"""Profile Inspector: drop/select ICC/ICM/DCP, inspect on a worker, show
format, structure, metadata, issues and capabilities immediately."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QSplitter,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import PROFILE_SUFFIXES, ProfileEntry
from ..i18n import T, bind_text, tr
from ..widgets.capabilities import CapabilityPanel, ComponentList
from ..widgets.common import (
    SPACE_S,
    DisabledReason,
    DropArea,
    HeadingLabel,
    PathLabel,
    WrapLabel,
    make_button,
    set_enabled_with_reason,
)
from .base import Workspace


def _table(parent: QWidget, accessible_key: str) -> tuple[QTableView, QStandardItemModel]:
    model = QStandardItemModel(parent)
    view = QTableView(parent)
    view.setModel(model)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.verticalHeader().setVisible(False)
    view.setWordWrap(True)
    view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    view.horizontalHeader().setStretchLastSection(True)
    view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
    view.horizontalHeader().resizeSection(0, 200)
    T.bind(view, "setAccessibleName", accessible_key)
    return view, model


class InspectorWorkspace(Workspace):
    key = "inspector"
    icon_name = "document_search"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self._removing_failed = False
        # Ctrl+O belongs to File > Open profiles (window-wide).  Giving this
        # button the same key made Ctrl+O ambiguous on this page.
        self.open_button = make_button("action.open_profiles", "folder_open", primary=True)
        self.remove_button = make_button("action.remove_profile", "delete")
        self.remove_failed_button = make_button("action.remove_failed_profiles", "delete")
        self.add_command(self.open_button)
        self.add_command(self.remove_button)
        self.add_command(self.remove_failed_button)

        self.drop = DropArea(PROFILE_SUFFIXES, "inspector.drop_hint", self.center)
        self.center_layout.addWidget(self.drop)
        vsplit = QSplitter(Qt.Orientation.Vertical, self.center)
        vsplit.setChildrenCollapsible(False)
        self.center_layout.addWidget(vsplit, 1)

        self.table = QTableView(vsplit)
        self.table.setModel(ctx.profiles)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().resizeSection(0, 220)
        self.table.setMinimumHeight(90)
        T.bind(self.table, "setAccessibleName", "inspector.profiles_table")

        details = QWidget(vsplit)
        dl = QVBoxLayout(details)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(SPACE_S)
        self.detail_title = HeadingLabel("", 2, details)
        self.detail_path = PathLabel(details)
        dl.addWidget(self.detail_title)
        dl.addWidget(self.detail_path)
        self.tabs = QTabWidget(details)
        self.tabs.setDocumentMode(True)
        self.meta_view, self.meta_model = _table(self.tabs, "inspector.tab.metadata")
        self.struct_view, self.struct_model = _table(self.tabs, "inspector.tab.structure")
        self.issue_view, self.issue_model = _table(self.tabs, "inspector.tab.issues")
        self.tabs.addTab(self.meta_view, "")
        self.tabs.addTab(self.struct_view, "")
        self.tabs.addTab(self.issue_view, "")
        dl.addWidget(self.tabs, 1)
        self.state_label = WrapLabel("", parent=details)
        dl.addWidget(self.state_label)
        vsplit.addWidget(self.table)
        vsplit.addWidget(details)
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 2)

        # right column
        self.caps = CapabilityPanel()
        self.side.add(self.caps)
        self.components = ComponentList("inspector.look_components")
        self.side.add(self.components)
        self.to_converter = make_button("inspector.go_convert", "arrow_swap")
        self.to_look = make_button("inspector.go_look", "color")
        self.next_reason = DisabledReason()
        self.side.add(self.to_converter)
        self.side.add(self.to_look)
        self.side.add(self.next_reason)
        self.side.add_stretch()

        # wiring
        self.open_button.clicked.connect(self.browse)
        self.drop.browse_requested.connect(self.browse)
        self.drop.files_dropped.connect(self.open_paths)
        self.remove_button.clicked.connect(self.remove_selected)
        self.remove_failed_button.clicked.connect(self.remove_failed)
        ctx.profiles.dataChanged.connect(lambda *_: self._update_failed_button())
        ctx.profiles.rowsInserted.connect(lambda *_: self._update_failed_button())
        ctx.profiles.rowsRemoved.connect(lambda *_: self._update_failed_button())
        self._update_failed_button()
        self.table.selectionModel().currentRowChanged.connect(self._row_changed)
        ctx.current_profile_changed.connect(self._show)
        ctx.profiles.dataChanged.connect(self._model_changed)
        self.to_converter.clicked.connect(lambda: ctx.navigate.emit("converter"))
        self.to_look.clicked.connect(lambda: ctx.navigate.emit("look"))
        T.watch(self, self._retranslate)
        self._retranslate()
        self._show(None)
        self.setAcceptDrops(True)

    # ----------------------------------------------------------------- drops
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        self.drop.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.drop.dropEvent(event)

    def primary_focus(self):
        return self.open_button

    # --------------------------------------------------------------- actions
    def browse(self) -> None:
        files = self.ctx.ask_profiles()
        if files:
            self.open_paths(files)

    def open_paths(self, paths: list[str]) -> None:
        started = self.ctx.open_profiles(paths)
        if started:
            row = self.ctx.profiles.row_of(started[-1])
            self.table.selectionModel().setCurrentIndex(
                self.ctx.profiles.index(row, 0),
                QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
            )

    def remove_selected(self) -> None:
        row = self.table.currentIndex().row()
        entry = self.ctx.profiles.entry(row)
        if entry is None:
            return
        if entry.state == "loading":
            self.ctx.jobs.cancel(entry.job_id)
        self.ctx.profiles.remove(row)
        if self.ctx.current_profile is entry:
            nxt = self.ctx.profiles.entry(min(row, self.ctx.profiles.rowCount() - 1))
            self.ctx.set_current_profile(nxt)

    def _failed_rows(self) -> list[int]:
        return [row for row, entry in enumerate(self.ctx.profiles.entries) if entry.state == "failed"]

    def remove_failed(self) -> None:
        """Take every profile that could not be read off the list (the files are left alone)."""
        profiles, rows = self.ctx.profiles, self._failed_rows()
        if not rows:
            return
        names = [profiles.entries[row].name for row in rows]
        current = self.ctx.current_profile
        # If the current profile leaves the list, the row that followed it takes over (as with the Remove button).
        anchor = self._row_of_entry(current)
        if anchor < 0:
            anchor = self.table.currentIndex().row()
        next_row = sum(1 for row in range(anchor) if row not in rows)
        # Rows vanishing move the table's current row. That must not reach the other pages: they
        # refill their settings when the current profile changes, even if it changes back.
        self._removing_failed = True
        try:
            for row in reversed(rows):
                profiles.remove(row)
            target = current if self._row_of_entry(current) >= 0 else profiles.entry(min(next_row, profiles.rowCount() - 1))
            selection = self.table.selectionModel()
            if target is None:
                selection.clear()
            else:
                selection.setCurrentIndex(profiles.index(self._row_of_entry(target), 0),
                                          QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        finally:
            self._removing_failed = False
        self.ctx.set_current_profile(target)
        self.ctx.log.info("log.profile.removed_failed", detail=", ".join(names), n=len(rows))

    def _row_of_entry(self, entry: ProfileEntry | None) -> int:
        """The row holding this very entry; opening a path again replaces the entry on its row."""
        return next((row for row, e in enumerate(self.ctx.profiles.entries) if e is entry), -1)

    def _update_failed_button(self) -> None:
        failed = len(self._failed_rows())
        self.remove_failed_button.setVisible(failed > 0)
        if failed:
            bind_text(self.remove_failed_button, "action.remove_failed_profiles", n=failed)

    def _row_changed(self, current, _previous) -> None:
        if self._removing_failed:
            return
        self.ctx.set_current_profile(self.ctx.profiles.entry(current.row()))

    def _model_changed(self, top_left, _bottom_right, _roles=()) -> None:
        entry = self.ctx.profiles.entry(top_left.row())
        if entry is not None and entry is self.ctx.current_profile:
            self._show(entry)

    # --------------------------------------------------------------- display
    def _retranslate(self) -> None:
        self.tabs.setTabText(0, tr("inspector.tab.metadata"))
        self.tabs.setTabText(1, tr("inspector.tab.structure"))
        self.tabs.setTabText(2, tr("inspector.tab.issues"))
        self.meta_model.setHorizontalHeaderLabels([tr("inspector.col.field"), tr("inspector.col.value")])
        self.struct_model.setHorizontalHeaderLabels(
            [tr("inspector.col.element"), tr("inspector.col.detail"), tr("inspector.col.usage")]
        )
        self.issue_model.setHorizontalHeaderLabels([tr("inspector.col.severity"), tr("inspector.col.message")])
        self._show(self.ctx.current_profile)

    def _show(self, entry: ProfileEntry | None) -> None:
        self.remove_button.setEnabled(entry is not None)
        for model in (self.meta_model, self.struct_model, self.issue_model):
            model.removeRows(0, model.rowCount())
        info = entry.info if entry is not None else None
        if entry is None:
            self.detail_title.setText(tr("inspector.no_profile"))
            self.detail_path.set_path(None)
            self.state_label.setText(tr("inspector.no_profile_hint"))
        else:
            self.detail_title.setText(info.title if info and info.title else entry.name)
            self.detail_path.set_path(entry.path)
            if entry.state == "loading":
                self.state_label.setText(tr("inspector.loading"))
            elif entry.state == "failed" and entry.error is not None:
                self.state_label.setText(tr("inspector.failed", reason=entry.error.summary()) + (f"\n{entry.error.detail}" if entry.error.detail else ""))
            else:
                self.state_label.setText("")
        self.state_label.setVisible(bool(self.state_label.text()))
        if info is not None:
            for label, value in info.metadata:
                self.meta_model.appendRow([QStandardItem(tr(label)), QStandardItem(value)])
            for element, detail, usage in info.structure:
                self.struct_model.appendRow([QStandardItem(element), QStandardItem(detail), QStandardItem(tr(usage))])
            for severity, text in info.issues:
                self.issue_model.appendRow([QStandardItem(tr(f"severity.{severity}")), QStandardItem(text)])
            for model in (self.meta_model, self.struct_model, self.issue_model):
                for r in range(model.rowCount()):
                    for c in range(model.columnCount()):
                        item = model.item(r, c)
                        if item is not None:
                            item.setToolTip(item.text())
        self.tabs.setTabText(2, tr("inspector.tab.issues_n", n=len(info.issues)) if info else tr("inspector.tab.issues"))
        self.caps.set_info(info)
        self.components.set_items(info.look_components if info else {}, "look.component")
        # next steps
        if not self.ctx.engine.available:
            reason = "reason.engine_unavailable"
        elif entry is None:
            reason = "reason.no_profile"
        elif entry.state == "loading":
            reason = "reason.profile_loading"
        elif entry.state == "failed":
            reason = "reason.profile_failed"
        else:
            reason = None
        set_enabled_with_reason(self.to_converter, self.next_reason, reason)
        self.to_look.setEnabled(reason is None)
        if reason is None and info is not None:
            if info.technical.status not in ("supported", "partial"):
                self.to_converter.setEnabled(False)
                self.to_converter.setToolTip(info.technical.reason or tr("reason.technical_unsupported"))
            if info.look.status in ("none", "unsupported"):
                self.to_look.setEnabled(False)
                self.to_look.setToolTip(info.look.reason or tr("reason.look_none"))
