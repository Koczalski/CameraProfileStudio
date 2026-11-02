"""LUT Compare: one table of photos (rows) and LUTs (columns), exported as
contact sheets or as split before/after images.

Only reduced pictures stay in memory; split images read the photo again when
they are written.  LUTs are kept as they were added, so rebuilding one on the
page that made it does not change the comparison.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QImage, QKeySequence, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
)

from ... import lut_compare as lc
from ...lut_compare import write_jpeg_exclusive
from .. import icons
from ..backend import ImageData, LutData, to_qimage
from ..compare_render import add_warning_band, render_sheet
from ..i18n import T, tr
from ..jobs import ErrorInfo, JobState, is_cancellation
from ..widgets.common import SPACE_S, DisabledReason, WrapLabel, make_button, set_enabled_with_reason
from ..widgets.preview import PREVIEW_MAX_SIDE
from .base import Workspace

BEFORE = 0  # column id of the Before pictures; LUT ids start at 1
LOG_KIND = "log_to_display"
_CELL_PADDING = 8
_LABEL_MAX_WIDTH = 220

ImageRole = Qt.ItemDataRole.UserRole + 20
SelectedRole = Qt.ItemDataRole.UserRole + 21
FailedRole = Qt.ItemDataRole.UserRole + 22


@dataclass(eq=False)
class ComparePhoto:
    id: int
    order: int
    path: str
    key: str
    image: ImageData  # long side at most max(CELL_SIDES)

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def stem(self) -> str:
        return Path(self.path).stem


@dataclass(eq=False)
class CompareLut:
    id: int
    order: int
    name: str
    path: str  # "" for a LUT received from another page
    key: str
    lut: LutData
    is_log: bool


class CompareModel(QAbstractTableModel):
    """Rows are photos; column 0 is Before and the others are LUTs."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.photos: list[ComparePhoto] = []
        self.luts: list[CompareLut] = []
        self.cells: dict[tuple[int, int], QImage] = {}
        self.failed: set[tuple[int, int]] = set()
        self.selected: set[tuple[int, int]] = set()
        T.watch(self, self.retranslate)

    def retranslate(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.luts))

    def _set_rows(self, photos: list[ComparePhoto], luts: list[CompareLut]) -> None:
        """New rows and columns; what belonged to the removed ones is dropped."""
        self.beginResetModel()
        self.photos, self.luts = photos, luts
        photos, luts = {p.id for p in photos}, {lut.id for lut in luts} | {BEFORE}
        self.cells = {k: v for k, v in self.cells.items() if k[0] in photos and k[1] in luts}
        self.failed = {k for k in self.failed if k[0] in photos and k[1] in luts}
        self.selected = {k for k in self.selected if k[0] in photos and k[1] in luts}
        self.endResetModel()

    def add_photo(self, photo: ComparePhoto) -> None:
        self._set_rows(sorted([*self.photos, photo], key=lambda p: p.order), self.luts)

    def add_lut(self, lut: CompareLut) -> None:
        self._set_rows(self.photos, sorted([*self.luts, lut], key=lambda item: item.order))

    def remove_photos(self, rows: Iterable[int]) -> None:
        gone = set(rows)
        self._set_rows([p for i, p in enumerate(self.photos) if i not in gone], self.luts)

    def remove_luts(self, rows: Iterable[int]) -> None:
        gone = set(rows)
        self._set_rows(self.photos, [lut for i, lut in enumerate(self.luts) if i not in gone])

    def clear_cells(self) -> None:
        self.beginResetModel()
        self.cells.clear()
        self.failed.clear()
        self.endResetModel()

    def key_at(self, row: int, column: int) -> tuple[int, int]:
        return (self.photos[row].id, BEFORE if column == 0 else self.luts[column - 1].id)

    def cell_image(self, row: int, column: int) -> QImage | None:
        return self.cells.get(self.key_at(row, column))

    def missing(self) -> list[tuple[ComparePhoto, CompareLut | None]]:
        pairs: list[tuple[ComparePhoto, CompareLut | None]] = []
        for photo in self.photos:
            for lut in (None, *self.luts):
                key = (photo.id, BEFORE if lut is None else lut.id)
                if key not in self.cells and key not in self.failed:
                    pairs.append((photo, lut))
        return pairs

    def _changed(self, key: tuple[int, int]) -> None:
        for row, photo in enumerate(self.photos):
            if photo.id == key[0]:
                for column in range(self.columnCount()):
                    if self.key_at(row, column) == key:
                        index = self.index(row, column)
                        self.dataChanged.emit(index, index)

    def set_cell(self, key: tuple[int, int], image: QImage | None) -> None:
        """Store a finished picture (None: it failed); late results for removed rows are dropped."""
        if key[0] not in {p.id for p in self.photos} or key[1] not in {lut.id for lut in self.luts} | {BEFORE}:
            return
        if image is None:
            self.failed.add(key)
        else:
            self.cells[key] = image
        self._changed(key)

    def toggle(self, row: int, column: int) -> None:
        if column == 0:
            return  # Before has nothing to compare against
        key = self.key_at(row, column)
        self.selected.symmetric_difference_update({key})
        self._changed(key)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.photos)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.luts) + 1

    def flags(self, index: QModelIndex):
        return Qt.ItemFlag.ItemIsEnabled

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        tip = role == Qt.ItemDataRole.ToolTipRole
        if orientation == Qt.Orientation.Vertical:
            if not 0 <= section < len(self.photos):
                return None
            return self.photos[section].path if tip else self.photos[section].name
        if section == 0:
            return tr("compare.col.before")
        if not 0 < section <= len(self.luts):
            return None
        lut = self.luts[section - 1]
        lines = [lut.path if tip and lut.path else lut.name]
        if lut.is_log:
            lines.append(tr("compare.warning.log"))
        return "\n".join(lines)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key = self.key_at(index.row(), index.column())
        if role == ImageRole:
            return self.cells.get(key)
        if role == SelectedRole:
            return key in self.selected
        if role == FailedRole:
            return key in self.failed
        if role == Qt.ItemDataRole.AccessibleTextRole:
            lut = tr("compare.col.before") if index.column() == 0 else self.luts[index.column() - 1].name
            state = "compare.cell.accessible_selected" if key in self.selected else "compare.cell.accessible"
            return tr(state, photo=self.photos[index.row()].name, lut=lut)
        return None


class CellDelegate(QStyledItemDelegate):
    """Draws the picture centred at its own size, with a mark on selected cells."""

    def __init__(self, side: int, parent=None) -> None:
        super().__init__(parent)
        self.side = side

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(self.side + _CELL_PADDING, self.side + _CELL_PADDING)

    def paint(self, painter, option, index) -> None:
        painter.save()
        rect = option.rect
        picture: QImage | None = index.data(ImageRole)
        if picture is not None:
            x = rect.x() + (rect.width() - picture.width()) // 2
            y = rect.y() + (rect.height() - picture.height()) // 2
            painter.drawImage(x, y, picture)
        else:
            key = "compare.cell.failed" if index.data(FailedRole) else "compare.cell.pending"
            painter.setPen(option.palette.placeholderText().color())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, tr(key))
        if index.data(SelectedRole):
            painter.setPen(QPen(option.palette.highlight().color(), 3))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
            icons.icon("checkmark_circle").paint(painter, QRect(rect.x() + 8, rect.y() + 8, 24, 24))
        elif option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(option.palette.highlight().color(), 1, Qt.PenStyle.DashLine))
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
        painter.restore()


class CompareGrid(QTableView):
    """Space or Enter toggles the current cell, as a click does."""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.clicked.emit(self.currentIndex())
            return
        super().keyPressEvent(event)


@dataclass
class ExportReport:
    existing: list[str] = field(default_factory=list)  # nothing was written
    written: list[str] = field(default_factory=list)
    failures: list[tuple[str, ErrorInfo]] = field(default_factory=list)  # (name, why), the run went on
    cancelled: bool = False
    stopped: tuple[str, ErrorInfo] | None = None  # (name, why), the run ended there


class CompareWorkspace(Workspace):
    key = "compare"
    icon_name = "table"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.model = CompareModel(self)
        self._ids = itertools.count(1)
        self._orders = itertools.count(1)
        self._pending_photos: set[str] = set()
        self._pending_luts: set[str] = set()
        self._pending_built: list[LutData] = []
        self._receive_jobs = itertools.count(1)
        # header commands
        self.cancel_button = make_button("action.cancel", "dismiss_circle")
        self.cancel_button.setShortcut(QKeySequence("Esc"))
        self.sheet_button = make_button("compare.action.sheet", "arrow_export_ltr", primary=True)
        self.split_button = make_button("compare.action.split", "split_vertical")
        for button in (self.cancel_button, self.split_button, self.sheet_button):
            self.add_command(button)
        # table in the centre
        self.empty_label = WrapLabel("compare.empty", secondary=True, parent=self.center)
        self.center_layout.addWidget(self.empty_label)
        self.grid = CompareGrid(self.center)
        self.grid.setModel(self.model)
        self.delegate = CellDelegate(lc.DEFAULT_CELL_SIDE, self.grid)
        self.grid.setItemDelegate(self.delegate)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        for header in (self.grid.horizontalHeader(), self.grid.verticalHeader()):
            header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
            header.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.grid.verticalHeader().setMaximumWidth(_LABEL_MAX_WIDTH)
        T.bind(self.grid, "setAccessibleName", "compare.grid.accessible")
        self.center_layout.addWidget(self.grid, 1)
        # settings on the right
        self.photo_list = self._make_list("compare.photos.accessible")
        self.lut_list = self._make_list("compare.luts.accessible")
        add_photos = make_button("compare.action.add_photos", "add")
        add_luts = make_button("compare.action.add_luts", "add")
        self.remove_photo_button = make_button("compare.action.remove", "delete")
        self.remove_lut_button = make_button("compare.action.remove", "delete")
        self.side.add(self._make_list_group("compare.group.photos", self.photo_list, add_photos, self.remove_photo_button))
        self.side.add(self._make_list_group("compare.group.luts", self.lut_list, add_luts, self.remove_lut_button))
        self.side.add(self._make_output_group())
        self.side.add_stretch()
        add_photos.clicked.connect(self._browse_photos)
        add_luts.clicked.connect(self._browse_luts)
        self.remove_photo_button.clicked.connect(lambda: self.remove_photos(self._rows(self.photo_list)))
        self.remove_lut_button.clicked.connect(lambda: self.remove_luts(self._rows(self.lut_list)))
        self.photo_list.itemSelectionChanged.connect(self._update_state)
        self.lut_list.itemSelectionChanged.connect(self._update_state)
        self.grid.clicked.connect(self.toggle_cell)
        self.cancel_button.clicked.connect(self.cancel)
        self.sheet_button.clicked.connect(self._choose_sheet_folder)
        self.split_button.clicked.connect(self._choose_split_folder)
        self.size_combo.currentIndexChanged.connect(lambda _i: self._side_changed())
        ctx.lut_for_compare.connect(self.receive_lut)
        ctx.jobs.job_finished.connect(lambda *_: self._update_state())
        ctx.jobs.busy_changed.connect(lambda _b: self._update_state())
        T.watch(self, self._retranslate)
        self._apply_side()
        self._after_change()

    # ------------------------------------------------------------ construction
    def _make_list(self, accessible_key: str) -> QListWidget:
        widget = QListWidget()
        widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        widget.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        widget.setMaximumHeight(160)
        T.bind(widget, "setAccessibleName", accessible_key)
        return widget

    def _make_list_group(self, title_key: str, widget: QListWidget, add_button, remove_button) -> QGroupBox:
        box = QGroupBox()
        T.bind(box, "setTitle", title_key)
        layout = QVBoxLayout(box)
        layout.setSpacing(SPACE_S)
        layout.addWidget(widget)
        row = QHBoxLayout()
        row.setSpacing(SPACE_S)
        row.addWidget(add_button, 1)
        row.addWidget(remove_button)
        layout.addLayout(row)
        return box

    def _make_output_group(self) -> QGroupBox:
        box = QGroupBox()
        T.bind(box, "setTitle", "compare.group.output")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.size_combo = QComboBox(box)
        for side in lc.CELL_SIDES:
            self.size_combo.addItem(f"{side} px", side)
        self.size_combo.setCurrentIndex(self.size_combo.findData(lc.DEFAULT_CELL_SIDE))
        size_label = QLabel(box)
        T.bind(size_label, "setText", "compare.cell_size")
        T.bind(self.size_combo, "setAccessibleName", "compare.cell_size")
        size_label.setBuddy(self.size_combo)
        form.addRow(size_label, self.size_combo)
        self.selected_label = WrapLabel("", secondary=True, parent=box)
        form.addRow(self.selected_label)
        self.status_label = WrapLabel("", parent=box)
        form.addRow(self.status_label)
        self.progress = QProgressBar(box)
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        form.addRow(self.progress)
        self.sheet_reason = DisabledReason(box)
        form.addRow(self.sheet_reason)
        self.split_reason = DisabledReason(box)
        form.addRow(self.split_reason)
        return box

    # --------------------------------------------------------------- photos
    @staticmethod
    def _file_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _browse_photos(self) -> None:
        self.add_photos(self.ctx.ask_images("compare.dialog.photos"))

    def add_photos(self, paths: Iterable[str]) -> None:
        engine = self.ctx.engine
        for raw in paths:
            path = str(Path(raw))
            key, name, order = self._file_key(path), Path(path).name, next(self._orders)
            if key in self._pending_photos or any(p.key == key for p in self.model.photos):
                self.ctx.log.info("compare.log.duplicate", detail=path, name=name)
                continue
            self._pending_photos.add(key)

            def work(token, progress, _p=path):
                return engine.shrink_image(engine.load_image(_p, PREVIEW_MAX_SIDE, token), max(lc.CELL_SIDES))

            def done(image, _p=path, _k=key, _o=order):
                self.model.add_photo(ComparePhoto(next(self._ids), _o, _p, _k, image))
                self._after_change()

            def failed(err: ErrorInfo, _p=path, _n=name):
                self.ctx.log.error("compare.log.photo_failed", detail=f"{_p} {err.detail}".strip(), name=_n, reason=err.summary())

            self.ctx.jobs.submit(f"compare:photo:{key}", "compare.job.photo", work, on_result=done, on_error=failed,
                                 on_cancel=lambda _n=name: self.ctx.log.warning("compare.log.cancelled_add", name=_n),
                                 on_done=lambda _s, _k=key: self._pending_photos.discard(_k),
                                 title_kwargs={"name": name})
        self._update_state()

    def remove_photos(self, rows: Iterable[int]) -> None:
        self.model.remove_photos(rows)
        self._after_change()

    # ----------------------------------------------------------------- LUTs
    def _browse_luts(self) -> None:
        self.add_lut_files(self.ctx.ask_luts("compare.dialog.luts"))

    def _is_log(self, lut: Any, token) -> tuple[bool, ErrorInfo | None]:
        """Worker: the Log estimate of LUT Check.  A failed check does not keep the LUT out."""
        engine = self.ctx.engine
        try:
            return engine.diagnose(lut, token).kind.kind == LOG_KIND, None
        except Exception as exc:  # noqa: BLE001 - reported in the log
            if is_cancellation(exc):
                raise
            return False, engine.map_error(exc)

    def add_lut_files(self, paths: Iterable[str]) -> None:
        engine = self.ctx.engine
        for raw in paths:
            path = str(Path(raw))
            key, name, order = self._file_key(path), Path(path).name, next(self._orders)
            if key in self._pending_luts or any(lut.key == key for lut in self.model.luts):
                self.ctx.log.info("compare.log.duplicate", detail=path, name=name)
                continue
            self._pending_luts.add(key)

            def work(token, progress, _p=path):
                loaded = engine.load_lut_file(_p, token)
                return engine.loaded_to_lutdata(loaded, None, token), self._is_log(loaded, token)

            def done(result, _p=path, _k=key, _o=order):
                lut, (is_log, problem) = result
                self._add_lut(_o, Path(_p).stem, _p, _k, lut, is_log, problem)

            def failed(err: ErrorInfo, _p=path, _n=name):
                self.ctx.log.error("compare.log.lut_failed", detail=f"{_p} {err.detail}".strip(), name=_n, reason=err.summary())

            self.ctx.jobs.submit(f"compare:lut:{key}", "compare.job.lut", work, on_result=done, on_error=failed,
                                 on_cancel=lambda _n=name: self.ctx.log.warning("compare.log.cancelled_add", name=_n),
                                 on_done=lambda _s, _k=key: self._pending_luts.discard(_k),
                                 title_kwargs={"name": name})
        self._update_state()

    def receive_lut(self, lut: LutData, page_key: str = "") -> int:
        """Keep the LUT another page has built (sent by its "Add to comparison" button)."""
        name = lut.title.strip() or tr(f"nav.{page_key}")
        if any(item is lut for item in self._pending_built) or any(item.lut is lut for item in self.model.luts):
            self.ctx.log.info("compare.log.duplicate", name=name)
            return -1
        order = next(self._orders)
        self._pending_built.append(lut)

        def work(token, progress):
            return self._is_log(lut, token)

        def done(result):
            is_log, problem = result
            self._add_lut(order, name, "", "", lut, is_log, problem)

        def failed(err: ErrorInfo):
            self.ctx.log.error("compare.log.lut_failed", detail=err.detail, name=name, reason=err.summary())

        def finished(_state: JobState):
            self._pending_built[:] = [item for item in self._pending_built if item is not lut]

        return self.ctx.jobs.submit(f"compare:receive:{next(self._receive_jobs)}", "compare.job.receive", work,
                                    on_result=done, on_error=failed,
                                    on_cancel=lambda: self.ctx.log.warning("compare.log.cancelled_add", name=name),
                                    on_done=finished, title_kwargs={"name": name})

    def _add_lut(self, order: int, name: str, path: str, key: str, lut: LutData, is_log: bool,
                 problem: ErrorInfo | None) -> None:
        if problem is not None:
            self.ctx.log.warning("compare.log.lut_not_checked", detail=problem.detail, name=name, reason=problem.summary())
        self.model.add_lut(CompareLut(next(self._ids), order, name, path, key, lut, is_log))
        self._after_change()

    def remove_luts(self, rows: Iterable[int]) -> None:
        self.model.remove_luts(rows)
        self._after_change()

    # ----------------------------------------------------------------- cells
    def cell_side(self) -> int:
        return int(self.size_combo.currentData())

    def _apply_side(self) -> None:
        side = self.cell_side()
        self.delegate.side = side
        self.grid.horizontalHeader().setDefaultSectionSize(side + _CELL_PADDING)
        self.grid.verticalHeader().setDefaultSectionSize(side + _CELL_PADDING)

    def _side_changed(self) -> None:
        self._apply_side()
        self.model.clear_cells()
        self.grid.updateGeometries()  # the scroll range still reflects the old section size
        self._render_cells()

    def _render_cell(self, photo: ComparePhoto, lut: CompareLut | None, side: int, token) -> QImage:
        """Worker: one picture of the table at ``side``."""
        engine = self.ctx.engine
        small = engine.shrink_image(photo.image, side)
        return to_qimage(small.array if lut is None else engine.render_preview(small, lut.lut, token))

    def _render_cells(self) -> None:
        missing, side = self.model.missing(), self.cell_side()
        before = tr("compare.col.before")
        if not missing:
            self.ctx.jobs.invalidate("compare:grid")
            return

        def work(token, progress):
            for done, (photo, lut) in enumerate(missing, start=1):
                token.raise_if_cancelled()
                key = (photo.id, BEFORE if lut is None else lut.id)
                try:
                    picture, problem = self._render_cell(photo, lut, side, token), None
                except Exception as exc:  # noqa: BLE001 - that cell fails, the others are still drawn
                    if is_cancellation(exc):
                        raise
                    picture, problem = None, self.ctx.engine.map_error(exc)
                progress(done / len(missing), "", cell=key, picture=picture, problem=problem,
                         photo=photo.name, lut=lut.name if lut is not None else before)

        def failed(err: ErrorInfo) -> None:
            self.ctx.log.error("log.preview.failed", detail=f"{err.summary()} {err.detail}".strip())

        self.ctx.jobs.submit("compare:grid", "compare.job.render", work, on_progress=self._cell_ready, on_error=failed)
        self._update_state()

    def _cell_ready(self, _fraction, _key, kwargs: dict) -> None:
        self.model.set_cell(kwargs["cell"], kwargs["picture"])
        problem: ErrorInfo | None = kwargs["problem"]
        if problem is not None:
            self.ctx.log.error("compare.log.cell_failed", detail=problem.detail, photo=kwargs["photo"], lut=kwargs["lut"],
                               reason=problem.summary())

    def toggle_cell(self, index: QModelIndex) -> None:
        if index.isValid():
            self.model.toggle(index.row(), index.column())
            self._update_state()

    def selected_count(self) -> int:
        return len(self.model.selected)

    # ---------------------------------------------------------------- export
    def _exporting(self) -> bool:
        return self.ctx.jobs.is_busy("compare:export")

    def sheet_reason_key(self) -> str | None:
        if not self.ctx.engine.available:
            return "reason.engine_unavailable"
        if not self.model.photos or not self.model.luts:
            return "compare.reason.need_both"
        return "compare.reason.exporting" if self._exporting() else None

    def split_reason_key(self) -> str | None:
        if not self.ctx.engine.available:
            return "reason.engine_unavailable"
        if not self.model.selected:
            return "compare.reason.no_selection"
        return "compare.reason.exporting" if self._exporting() else None

    def _choose_sheet_folder(self) -> None:
        folder = self.ctx.ask_directory("compare.dialog.folder", "compare")
        if folder:
            self.export_sheets(folder)

    def _choose_split_folder(self) -> None:
        folder = self.ctx.ask_directory("compare.dialog.folder", "compare")
        if folder:
            self.export_comparisons(folder)

    def _write(self, report: ExportReport, pixels: Callable[[], Any], folder: str, name: str) -> bool:
        """Worker: publish one file.  False ends the run (cancelled, or the folder cannot take it)."""
        try:
            write_jpeg_exclusive(pixels(), Path(folder) / name)
        except Exception as exc:  # noqa: BLE001 - reported with what was already written
            if is_cancellation(exc):
                report.cancelled = True
            else:
                report.stopped = (name, self.ctx.engine.map_error(exc))
            return False
        report.written.append(name)
        return True

    def export_sheets(self, folder: str) -> int:
        """Write the table as ``contact-NNN.jpg`` pages; nothing at all if one of the names exists."""
        if self.sheet_reason_key() is not None:
            return -1
        side, photos, luts = self.cell_side(), list(self.model.photos), list(self.model.luts)
        cells = dict(self.model.cells)
        metrics = lc.SheetMetrics.for_side(side)
        pages = lc.paginate(len(photos), len(luts))
        before = tr("compare.col.before")
        warning = tr("compare.warning.log")
        photo_names = [p.name for p in photos]
        lut_names = [lut.name for lut in luts]
        lut_warnings = [warning if lut.is_log else "" for lut in luts]

        def work(token, progress):
            report = ExportReport()
            names = [lc.contact_file_name(page.number) for page in pages]
            report.existing = lc.existing_files(folder, names)
            if report.existing:
                return report
            unavailable: dict[tuple[int, int], ErrorInfo] = {}

            def cell_at(photo_index: int, lut_index: int | None) -> QImage | None:
                photo = photos[photo_index]
                lut = None if lut_index is None else luts[lut_index]
                key = (photo.id, BEFORE if lut is None else lut.id)
                if key in cells:
                    return cells[key]
                if key in unavailable:
                    return None
                try:
                    cells[key] = self._render_cell(photo, lut, side, token)  # not on screen yet
                except Exception as exc:  # noqa: BLE001 - that cell stays empty, the sheet goes on
                    if is_cancellation(exc):
                        raise
                    unavailable[key] = self.ctx.engine.map_error(exc)
                    report.failures.append((f"{photo.name} / {lut.name if lut else before}", unavailable[key]))
                    return None
                return cells[key]

            for done, (page, name) in enumerate(zip(pages, names, strict=True)):
                if token.cancelled:
                    report.cancelled = True
                    break
                sheet = partial(render_sheet, page, metrics, before, photo_names, lut_names, lut_warnings, cell_at)
                if not self._write(report, sheet, folder, name):
                    break
                progress((done + 1) / len(pages))
            return report

        return self._submit_export("compare.job.sheet", work, folder, [])

    def export_comparisons(self, folder: str) -> int:
        """Write one split image per selected cell; nothing at all if one of the names exists."""
        if self.split_reason_key() is not None:
            return -1
        engine = self.ctx.engine
        chosen = [(photo, lut) for photo in self.model.photos for lut in self.model.luts
                  if (photo.id, lut.id) in self.model.selected]
        plan = lc.plan_comparison_files([((photo, lut), photo.stem, lut.name) for photo, lut in chosen])
        warning = tr("compare.warning.log")

        def work(token, progress):
            report = ExportReport()
            report.existing = lc.existing_files(folder, [name for _key, name in plan.files])
            if report.existing:
                return report
            current: tuple[ComparePhoto, ImageData] | None = None
            for done, ((photo, lut), name) in enumerate(plan.files, start=1):
                if token.cancelled:
                    report.cancelled = True
                    break
                try:
                    if current is None or current[0] is not photo:
                        current = (photo, engine.load_image(photo.path, PREVIEW_MAX_SIDE, token))
                    data = current[1]
                    after = engine.render_preview(data, lut.lut, token)
                except Exception as exc:  # noqa: BLE001 - that cell fails, the others go on
                    if is_cancellation(exc):
                        report.cancelled = True
                        break
                    report.failures.append((name, engine.map_error(exc)))
                    continue

                def pixels(_data=data, _after=after, _lut=lut):
                    rgb = lc.split_image(_data.array, _after)
                    return add_warning_band(rgb, warning) if _lut.is_log else rgb

                if not self._write(report, pixels, folder, name):
                    break
                progress(done / len(plan.files))
            return report

        notes = [(name, f"{photo.path} / {lut.name}") for (photo, lut), name, _owner in plan.collisions]
        return self._submit_export("compare.job.split", work, folder, notes)

    def _submit_export(self, title_key: str, work, folder: str, collisions: list[tuple[str, str]]) -> int:
        def done(report: ExportReport) -> None:
            for name, detail in collisions:
                self.ctx.log.error("compare.log.collision", detail=detail, name=name)
            self._report(report, folder)

        def failed(err: ErrorInfo) -> None:
            self.ctx.log.error("log.export.failed", detail=f"{folder}: {err.summary()} {err.detail}")
            self.ctx.show_error(err, "dialog.export_failed")

        def cancelled() -> None:
            self.ctx.log.warning("compare.log.cancelled", detail=folder, n=0)

        self.progress.setValue(0)
        self.progress.setVisible(True)
        job = self.ctx.jobs.submit("compare:export", title_key, work, on_result=done, on_error=failed, on_cancel=cancelled,
                                   on_progress=lambda f, _k, _kw: self.progress.setValue(int((f or 0) * 100)),
                                   on_done=lambda _s: self.progress.setVisible(False), side_effects=True)
        self._update_state()
        return job

    def _report(self, report: ExportReport, folder: str) -> None:
        log = self.ctx.log
        if report.existing:
            files = ", ".join(report.existing)
            log.error("compare.log.exists", detail=f"{folder}: {files}")
            self.ctx.show_error(ErrorInfo("compare.error.exists", {"files": files}), "dialog.export_failed")
            return
        for name, err in report.failures:
            log.error("compare.log.item_failed", detail=err.detail, name=name, reason=err.summary())
        n, detail = len(report.written), f"{folder}: {', '.join(report.written)}" if report.written else folder
        if report.stopped is not None:
            name, err = report.stopped
            log.error("compare.log.stopped", detail=f"{detail} | {err.detail}", name=name, n=n, reason=err.summary())
        elif report.cancelled:
            log.warning("compare.log.cancelled", detail=detail, n=n)
        elif report.failures:
            log.warning("compare.log.done_failed", detail=detail, n=n, failed=len(report.failures))
        else:
            log.success("compare.log.done", detail=detail, n=n)

    # ---------------------------------------------------------------- cancel
    def _active_jobs(self) -> list:
        return [rec for rec in self.ctx.jobs.active() if rec.channel.startswith("compare:")]

    def cancel(self) -> None:
        for rec in self._active_jobs():
            self.ctx.jobs.cancel(rec.job_id)
        self._update_state()

    # ----------------------------------------------------------------- state
    @staticmethod
    def _rows(widget: QListWidget) -> list[int]:
        return [index.row() for index in widget.selectedIndexes()]

    def _fill_lists(self) -> None:
        self.photo_list.clear()
        for photo in self.model.photos:
            item = QListWidgetItem(photo.name)
            item.setToolTip(photo.path)
            self.photo_list.addItem(item)
        self.lut_list.clear()
        for lut in self.model.luts:
            item = QListWidgetItem(lut.name)
            warning = tr("compare.warning.log") if lut.is_log else ""
            item.setToolTip("\n".join(text for text in (lut.path, warning) if text) or lut.name)
            if lut.is_log:
                item.setIcon(icons.icon("warning"))
            self.lut_list.addItem(item)

    def _after_change(self) -> None:
        self._fill_lists()
        table = bool(self.model.photos and self.model.luts)
        self.empty_label.setVisible(not table)
        self.grid.setVisible(table)
        self._render_cells()
        self._update_state()

    def _retranslate(self) -> None:
        self._fill_lists()
        self._update_state()

    def activated(self) -> None:
        # Cells that were cancelled earlier are drawn again when the page is shown.
        if not self.ctx.jobs.is_busy("compare:grid"):
            self._render_cells()

    def _update_state(self) -> None:
        set_enabled_with_reason(self.sheet_button, self.sheet_reason, self.sheet_reason_key())
        set_enabled_with_reason(self.split_button, self.split_reason, self.split_reason_key())
        self.remove_photo_button.setEnabled(bool(self._rows(self.photo_list)))
        self.remove_lut_button.setEnabled(bool(self._rows(self.lut_list)))
        self.selected_label.setText(tr("compare.selected", n=self.selected_count()))
        busy = bool(self._active_jobs())
        self.cancel_button.setEnabled(busy)
        self.status_label.setText(tr("compare.status.working" if busy else "compare.status.idle"))
