"""Batch Processing: queued, independent engine calls per profile.

Each item runs inspect -> request -> convert -> export as its own job with
its own cancel token and terminal state (contract: "GUI implements batch by
queued independent service calls with per-item terminal status").  Existing
output files are detected before the run and are only replaced after an
explicit confirmation; the alternative is to skip them.

Each added file is inspected in the background as soon as it is added.  Items
that cannot be processed with the current settings (for example an ICC
profile in look mode, a DCP without data for the chosen look mode, an ICC
intent the profile does not support, or missing DCP white balance) are shown
as "Not applicable" / "Needs input" with the reason *before* the run, are
skipped with that reason when the batch runs, and the Run command is
disabled when no item could be processed (0.2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QStyledItemDelegate,
    QTableView,
    QWidget,
)

from .. import icons
from ..backend import FORMAT_FILES, GAMUTS, INTENTS, LOOK_MODES, LUT_SIZES, Unsupported
from ..context import PROFILE_SUFFIXES
from ..i18n import T, tr
from ..jobs import ErrorInfo, JobState
from ..widgets.common import (
    AdvancedSection,
    DisabledReason,
    DropArea,
    PathLabel,
    WrapLabel,
    bind_combo,
    fill_combo,
    make_button,
    set_enabled_with_reason,
)
from .base import Workspace
from .lut_base import format_hint, make_format_combo
from ..context import setting_int

MAX_CONCURRENT = 2


@dataclass
class BatchItem:
    path: str
    state: str = "pending"  # pending | queued | running | succeeded | failed | cancelled | skipped
    output: str = ""
    detail: str = ""
    error: ErrorInfo | None = None
    job_id: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    # Background pre-check (0.2.2): inspected profile view or its error.
    info: Any = None
    check_error: ErrorInfo | None = None
    check_job: int = 0
    # Why the item cannot run with the current settings: (kind, key, kwargs);
    # kind is "blocked" (file/mode mismatch) or "input" (a setting is missing).
    blocker: tuple[str, str, dict] | None = None
    skip_reason: tuple[str, dict] | None = None

    @property
    def checking(self) -> bool:
        return self.info is None and self.check_error is None

    def display_state(self) -> str:
        """State shown in the list: run states, or the pre-run verdict."""
        if self.state != "pending":
            return self.state
        if self.check_error is not None:
            return "unreadable"
        if self.checking:
            return "checking"
        if self.blocker is not None:
            return "blocked" if self.blocker[0] == "blocked" else "needs_input"
        return "pending"


class BatchModel(QAbstractTableModel):
    COLUMNS = ("batch.col.file", "batch.col.state", "batch.col.output", "batch.col.detail")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[BatchItem] = []
        T.watch(self, self._retranslate)

    def _retranslate(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 3)
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, 3))

    def add(self, paths: list[str]) -> int:
        known = {str(Path(i.path)) for i in self.items}
        new = [str(Path(p)) for p in paths if str(Path(p)) not in known]
        if new:
            self.beginInsertRows(QModelIndex(), len(self.items), len(self.items) + len(new) - 1)
            self.items.extend(BatchItem(p) for p in new)
            self.endInsertRows()
        return len(new)

    def remove_rows(self, rows: list[int]) -> None:
        for r in sorted(set(rows), reverse=True):
            self.beginRemoveRows(QModelIndex(), r, r)
            del self.items[r]
            self.endRemoveRows()

    def clear(self) -> None:
        self.beginResetModel()
        self.items.clear()
        self.endResetModel()

    def changed(self, item: BatchItem) -> None:
        try:
            r = self.items.index(item)
        except ValueError:
            return
        self.dataChanged.emit(self.index(r, 0), self.index(r, 3))

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(self.COLUMNS[section])
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        c = index.column()
        state = it.display_state()
        if role == Qt.ItemDataRole.DecorationRole and c == 1:
            name = {"succeeded": "checkmark_circle", "failed": "error_circle", "cancelled": "dismiss_circle",
                    "skipped": "warning", "blocked": "warning", "needs_input": "warning",
                    "unreadable": "error_circle"}.get(state)
            return icons.icon(name) if name else None
        if role == Qt.ItemDataRole.ToolTipRole:
            return {0: it.path, 2: it.output, 3: self._detail(it)}.get(c, tr(f"batch.state.{state}"))
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            if c == 0:
                return Path(it.path).name
            if c == 1:
                return tr(f"batch.state.{state}")
            if c == 2:
                return it.output
            if c == 3:
                return self._detail(it)
        return None

    @staticmethod
    def _detail(it: BatchItem) -> str:
        if it.error is not None:
            return f"{it.error.summary()} {it.error.detail}".strip()
        if it.state == "skipped" and it.skip_reason is not None:
            key, kwargs = it.skip_reason
            return tr("batch.skipped_blocked", reason=tr(key, **kwargs))
        if it.state == "pending":
            if it.check_error is not None:
                return f"{it.check_error.summary()} {it.check_error.detail}".strip()
            if it.blocker is not None:
                return tr(it.blocker[1], **it.blocker[2])
        if it.detail.startswith("key:"):
            return tr(it.detail[4:])
        return it.detail


class _ElideRight(QStyledItemDelegate):
    """Reasons read from the start; file paths keep the table's middle elision."""

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideRight


class BatchWorkspace(Workspace):
    key = "batch"
    icon_name = "layer"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.model = BatchModel(self)
        self._queue: list[BatchItem] = []
        self._running: set[int] = set()
        self._overwrite = False
        self._run_id = 0
        # commands
        self.run_button = make_button("batch.run", "play", primary=True)
        self.cancel_button = make_button("batch.cancel", "dismiss_circle")
        self.add_command(self.cancel_button)
        self.add_command(self.run_button)
        # centre
        self.drop = DropArea(PROFILE_SUFFIXES, "batch.drop_hint", self.center)
        self.center_layout.addWidget(self.drop)
        self.table = QTableView(self.center)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().resizeSection(0, 200)
        self.table.horizontalHeader().resizeSection(2, 220)
        self.table.horizontalHeader().setStretchLastSection(True)
        T.bind(self.table, "setAccessibleName", "batch.table")
        self.table.setItemDelegateForColumn(3, _ElideRight(self.table))
        self.center_layout.addWidget(self.table, 1)
        # Full text of the selected item's status (the Details column is elided).
        self.selected_detail = WrapLabel("", parent=self.center)
        self.selected_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.selected_detail.setVisible(False)
        self.center_layout.addWidget(self.selected_detail)
        row = QHBoxLayout()
        self.remove_button = make_button("batch.remove", "delete")
        self.clear_button = make_button("batch.clear", None)
        self.overall = QProgressBar(self.center)
        self.overall.setRange(0, 100)
        T.bind(self.overall, "setAccessibleName", "batch.progress")
        row.addWidget(self.remove_button)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        self.center_layout.addLayout(row)
        self.overall.setMinimumWidth(80)
        self.center_layout.addWidget(self.overall)
        self.summary = WrapLabel("", parent=self.center)
        self.center_layout.addWidget(self.summary)
        self.blocked_note = WrapLabel("", secondary=True, parent=self.center)
        self.center_layout.addWidget(self.blocked_note)

        # right
        out = QGroupBox()
        T.bind(out, "setTitle", "batch.output")
        of = QFormLayout(out)
        of.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.out_path = PathLabel(out)
        self.out_button = make_button("batch.choose_folder", "folder_open")
        of.addRow(self.out_path)
        of.addRow(self.out_button)
        self.side.add(out)
        mode = QGroupBox()
        T.bind(mode, "setTitle", "batch.mode")
        mf = QFormLayout(mode)
        mf.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.mode = QComboBox(mode)
        fill_combo(self.mode, [("batch.mode.technical", "technical"), ("batch.mode.look", "look")])
        bind_combo(self.mode)
        T.bind(self.mode, "setAccessibleName", "batch.mode")
        mf.addRow(self.mode)
        self.look_mode = QComboBox(mode)
        fill_combo(self.look_mode, [(f"look.mode.{m}", m) for m in LOOK_MODES])
        self.look_mode.setCurrentIndex(self.look_mode.findData("combined"))
        bind_combo(self.look_mode)
        lm = QLabel(mode)
        T.bind(lm, "setText", "batch.look_mode")
        T.bind(self.look_mode, "setAccessibleName", "batch.look_mode")
        lm.setBuddy(self.look_mode)
        mf.addRow(lm, self.look_mode)
        self.direction = QComboBox(mode)
        fill_combo(self.direction, [("conv.dir.to_srgb", "to_srgb"), ("conv.dir.to_linear_srgb", "to_linear_srgb"),
                                    ("conv.dir.from_srgb", "from_srgb")])
        bind_combo(self.direction)
        dl = QLabel(mode)
        T.bind(dl, "setText", "batch.icc_direction")
        T.bind(self.direction, "setAccessibleName", "batch.icc_direction")
        dl.setBuddy(self.direction)
        mf.addRow(dl, self.direction)
        self.direction_hint = WrapLabel("batch.icc_direction_hint", secondary=True, parent=mode)
        mf.addRow(self.direction_hint)
        self.size = QComboBox(mode)
        for n in LUT_SIZES:
            self.size.addItem(f"{n} \u00d7 {n} \u00d7 {n}", n)
        self.size.setCurrentIndex(max(0, self.size.findData(setting_int(ctx.settings, "lut/default_size", 33, LUT_SIZES))))
        sl = QLabel(mode)
        T.bind(sl, "setText", "lut.size")
        T.bind(self.size, "setAccessibleName", "lut.size")
        sl.setBuddy(self.size)
        mf.addRow(sl, self.size)
        self.format = make_format_combo(ctx, mode)
        fl = QLabel(mode)
        T.bind(fl, "setText", "lut.format")
        fl.setBuddy(self.format)
        mf.addRow(fl, self.format)
        self.format_hint = WrapLabel("", secondary=True, parent=mode)
        mf.addRow(self.format_hint)
        for combo in (self.format, self.size):
            combo.currentIndexChanged.connect(lambda _i: self._render_format_hint())
        T.watch(self.format_hint, self._render_format_hint)
        self._render_format_hint()
        self.side.add(mode)
        # DCP parameters
        dcp = QGroupBox()
        T.bind(dcp, "setTitle", "batch.dcp_group")
        df = QFormLayout(dcp)
        df.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        df.addRow(WrapLabel("batch.dcp_hint", secondary=True, parent=dcp))
        nrow = QWidget(dcp)
        nl = QHBoxLayout(nrow)
        nl.setContentsMargins(0, 0, 0, 0)
        self.neutral = []
        for ch in ("r", "g", "b"):
            s = QDoubleSpinBox(nrow)
            s.setDecimals(4)
            s.setRange(0, 10)
            s.setSingleStep(0.01)
            s.setSpecialValueText(tr("conv.wb.unset"))
            T.watch(s, lambda s=s: s.setSpecialValueText(tr("conv.wb.unset")))
            T.bind(s, "setAccessibleName", f"conv.wb.neutral_{ch}")
            nl.addWidget(s)
            self.neutral.append(s)
        nlab = QLabel(dcp)
        T.bind(nlab, "setText", "conv.wb.neutral")
        nlab.setBuddy(self.neutral[0])
        df.addRow(nlab, nrow)
        self.kelvin = QDoubleSpinBox(dcp)
        self.kelvin.setRange(0, 50000)
        self.kelvin.setDecimals(0)
        self.kelvin.setSingleStep(100)
        self.kelvin.setSuffix(" K")
        self.kelvin.setSpecialValueText(tr("conv.wb.unset"))
        T.watch(self.kelvin, lambda: self.kelvin.setSpecialValueText(tr("conv.wb.unset")))
        kl = QLabel(dcp)
        T.bind(kl, "setText", "conv.wb.kelvin")
        T.bind(self.kelvin, "setAccessibleName", "conv.wb.kelvin")
        kl.setBuddy(self.kelvin)
        df.addRow(kl, self.kelvin)
        self.side.add(dcp)
        self.advanced = AdvancedSection("advanced.title")
        adv = QWidget()
        af = QFormLayout(adv)
        af.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        af.setContentsMargins(0, 0, 0, 0)
        self.intent = QComboBox(adv)
        fill_combo(self.intent, [(f"intent.{i}", i) for i in INTENTS])
        self.intent.setCurrentIndex(self.intent.findData("relative"))
        bind_combo(self.intent)
        il = QLabel(adv)
        T.bind(il, "setText", "conv.intent")
        T.bind(self.intent, "setAccessibleName", "conv.intent")
        il.setBuddy(self.intent)
        af.addRow(il, self.intent)
        self.bpc = QCheckBox(adv)
        T.bind(self.bpc, "setText", "conv.bpc")
        af.addRow(self.bpc)
        self.gamut = QComboBox(adv)
        fill_combo(self.gamut, [(f"gamut.{g}", g) for g in GAMUTS])
        bind_combo(self.gamut)
        gl = QLabel(adv)
        T.bind(gl, "setText", "conv.gamut")
        T.bind(self.gamut, "setAccessibleName", "conv.gamut")
        gl.setBuddy(self.gamut)
        af.addRow(gl, self.gamut)
        self.advanced.add_widget(adv)
        self.side.add(self.advanced)
        self.run_reason = DisabledReason()
        self.side.add(self.run_reason)
        self.side.add_stretch()

        # wiring
        self.drop.files_dropped.connect(self.add_files)
        self.drop.browse_requested.connect(self._browse)
        self.out_button.clicked.connect(self._choose_out)
        self.run_button.clicked.connect(self.run)
        self.cancel_button.clicked.connect(self.cancel)
        self.remove_button.clicked.connect(self._remove)
        self.clear_button.clicked.connect(self._clear)
        self.mode.currentIndexChanged.connect(lambda _i: self._update())
        for combo in (self.look_mode, self.direction, self.intent, self.gamut):
            combo.currentIndexChanged.connect(lambda _i: self._update())
        for spin in (*self.neutral, self.kelvin):
            spin.valueChanged.connect(lambda _v: self._update())
        self.model.rowsInserted.connect(lambda *_: self._update())
        self.model.rowsRemoved.connect(lambda *_: self._update())
        self.model.modelReset.connect(self._update)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update())
        self.table.selectionModel().currentRowChanged.connect(lambda *_: self._render_selected())
        self.model.dataChanged.connect(lambda *_: self._render_selected())
        self.model.modelReset.connect(self._render_selected)
        self.model.rowsRemoved.connect(lambda *_: self._render_selected())
        saved = str(ctx.settings.value("batch/output", ""))
        self.output_dir = saved if saved and Path(saved).is_dir() else ""
        self.out_path.set_path(self.output_dir or None, "batch.no_folder")
        T.watch(self, self._update)
        self.setAcceptDrops(True)
        self._update()

    def primary_focus(self):
        return self.drop.browse

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        self.drop.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.drop.dropEvent(event)

    # ------------------------------------------------------------ inputs
    def _browse(self) -> None:
        files = self.ctx.ask_profiles()
        if files:
            self.add_files(files)

    def add_files(self, paths: list[str]) -> None:
        ok = [p for p in paths if Path(p).suffix.lower() in PROFILE_SUFFIXES]
        before = len(self.model.items)
        n = self.model.add(ok)
        self.ctx.log.info("log.batch.added", n=n)
        for item in self.model.items[before:]:
            self._check(item)

    def _check(self, item: BatchItem) -> None:
        """Inspect a newly added file in the background so that problems are
        visible in the list before the batch is started."""
        if not self.ctx.engine.available:
            return
        engine = self.ctx.engine
        path = item.path

        def work(token, progress):
            return engine.inspect(path, token)

        def done(info):
            item.info, item.check_error = info, None
            self._evaluate(item)
            self.model.changed(item)
            self._update()

        def failed(err: ErrorInfo):
            item.check_error = err
            self.model.changed(item)
            self._update()

        def cancelled():
            failed(ErrorInfo("error.cancelled"))

        item.check_job = self.ctx.jobs.submit(
            f"batch-check:{path}", "job.batch_check", work, on_result=done, on_error=failed, on_cancel=cancelled,
            title_kwargs={"name": Path(path).name},
        )

    @staticmethod
    def _parts(info) -> str:
        names = [("hue_sat_map", "HueSatMap"), ("look_table", "LookTable"), ("tone_curve", "dcp.part.tone_curve")]
        found = [tr(label) if label.startswith("dcp.") else label
                 for key, label in names if getattr(info.look_components.get(key), "status", "") == "available"]
        return BatchWorkspace._sep().join(found) if found else tr("dcp.part.none")

    @staticmethod
    def _sep() -> str:
        return tr("list.separator")

    def blocker_for(self, item: BatchItem, s: dict | None = None) -> tuple[str, str, dict] | None:
        """Why ``item`` cannot be processed with the current settings, or None.

        Mirrors what the run would do (same request builders), so a reason is
        shown before the run instead of a failure after it."""
        info = item.info
        if info is None:
            return None
        s = s or self._settings()
        engine = self.ctx.engine
        if s["mode"] == "look":
            if info.kind != "dcp":
                return ("blocked", "batch.blocked.icc_look", {})
            cap = info.look_modes.get(s["look_mode"])
            if cap is None or cap.status not in ("supported", "partial", "available"):
                return ("blocked", "batch.blocked.look_mode",
                        {"mode": tr(f"look.mode.{s['look_mode']}"), "parts": self._parts(info)})
        elif info.kind != "dcp":
            if getattr(info.technical, "status", "") == "unsupported":
                return ("blocked", "batch.blocked.technical", {"reason": info.technical.reason or ""})
            direction = self.direction_for(item.path, s["direction"])
            side = "technical.output" if direction == "from_srgb" else "technical.input"
            caps = {str(c.feature): str(c.status) for c in getattr(info.raw, "capabilities", ())}
            if caps.get(side) == "unsupported":
                return ("blocked", "batch.blocked.direction", {"direction": tr(f"conv.dir.{direction}")})
            if caps.get(f"{side}.{s['intent']}") == "unsupported":
                usable = [tr(f"intent.{i}") for i in INTENTS if caps.get(f"{side}.{i}") in ("supported", "partial")]
                return ("input", "batch.blocked.intent",
                        {"intent": tr(f"intent.{s['intent']}"),
                         "usable": self._sep().join(usable) or tr("dcp.part.none")})
        try:
            if s["mode"] == "look":
                engine.look_request(info, s["look_mode"], s)
            else:
                engine.technical_request(info, dict(s, direction=self.direction_for(item.path, s["direction"])))
        except Unsupported as exc:
            return ("input", exc.key, dict(exc.kwargs))
        except Exception:  # noqa: BLE001 - the run reports anything else as before
            return None
        return None

    def _evaluate(self, item: BatchItem, s: dict | None = None) -> None:
        item.blocker = self.blocker_for(item, s)
        if item.blocker is None and self.output_dir:
            # Two inputs with the same file name in different folders would write
            # the same output: the later one is held back instead of overwriting.
            mine = self.output_for(item)
            for other in self.model.items:
                if other is item:
                    break
                if other.info is not None and other.blocker is None and self.output_for(other) == mine:
                    item.blocker = ("blocked", "batch.blocked.duplicate_output",
                                    {"name": mine.name, "other": other.path})
                    break

    def set_output_dir(self, folder: str) -> None:
        self.output_dir = folder
        self.ctx.settings.setValue("batch/output", folder)
        self.out_path.set_path(folder or None, "batch.no_folder")
        self._update()

    def _choose_out(self) -> None:
        folder = self.ctx.ask_directory("batch.choose_folder", "batch")
        if folder:
            self.set_output_dir(folder)

    def _remove(self) -> None:
        rows = [i.row() for i in self.table.selectionModel().selectedRows()]
        self.model.remove_rows([r for r in rows if self.model.items[r].state not in ("queued", "running")])

    def _clear(self) -> None:
        if not self.running():
            self.model.clear()

    # ------------------------------------------------------------- run
    def running(self) -> bool:
        return bool(self._queue or self._running)

    def run_reason_key(self) -> str | None:
        if not self.ctx.engine.available:
            return "reason.engine_unavailable"
        if self.running():
            return "reason.batch_running"
        if not self.model.items:
            return "reason.batch_empty"
        if not self.output_dir:
            return "reason.batch_no_output"
        if not Path(self.output_dir).is_dir():
            return "reason.batch_output_missing"
        items = self.model.items
        if items and all(i.check_error is not None or i.blocker is not None for i in items):
            return "reason.batch_all_blocked"
        return None

    @staticmethod
    def direction_for(path: str, direction: str) -> str:
        """Technical direction actually used for ``path``: the ICC direction
        applies to ICC/ICM files; DCP technical conversion only produces sRGB."""
        return "to_srgb" if Path(path).suffix.lower() == ".dcp" else direction

    def _render_selected(self) -> None:
        index = self.table.selectionModel().currentIndex()
        row = index.row() if index.isValid() else -1
        if not 0 <= row < len(self.model.items):
            self.selected_detail.setVisible(False)
            return
        item = self.model.items[row]
        detail = self.model._detail(item)
        self.selected_detail.setText(tr("batch.selected_detail", name=Path(item.path).name,
                                        detail=detail or tr(f"batch.state.{item.display_state()}")))
        self.selected_detail.setVisible(True)

    def _render_format_hint(self) -> None:
        self.format_hint.setText(format_hint(self.ctx.engine, self.format.currentData(), int(self.size.currentData())))

    def output_for(self, item: BatchItem) -> Path:
        stem = Path(item.path).stem
        size = self.size.currentData()
        tag, ext = FORMAT_FILES[self.format.currentData()]
        if self.mode.currentData() == "look":
            name = f"{stem}_{self.look_mode.currentData()}_EXPERIMENTAL_{size}{tag}{ext}"
        else:
            name = f"{stem}_{self.direction_for(item.path, self.direction.currentData())}_{size}{tag}{ext}"
        return Path(self.output_dir) / name

    def run(self) -> None:
        if self.run_reason_key() is not None:
            return
        s = self._settings()
        for it in self.model.items:
            if it.info is not None:
                self._evaluate(it, s)
        blocked = [it for it in self.model.items if it.blocker is not None]
        items = [it for it in self.model.items if it.blocker is None]
        conflicts = []
        for it in items:
            out = self.output_for(it)
            if out.exists() or out.with_suffix(".json").exists():
                conflicts.append(out)
        self._overwrite = False
        skip: set[Path] = set()
        if conflicts:
            choice = self.ask_conflicts(conflicts)
            if choice == "cancel":
                self.ctx.log.info("log.batch.not_started")
                return
            if choice == "overwrite":
                self._overwrite = True
            else:
                skip = set(conflicts)
        self._run_id += 1
        settings = self._settings()
        self._queue = []
        for it in blocked:
            it.error, it.output = None, ""
            it.state, it.detail = "skipped", ""
            it.skip_reason = (it.blocker[1], it.blocker[2])
            self.ctx.log.warning("log.batch.item_not_applicable",
                                 detail=f"{it.path}: {tr(it.blocker[1], **it.blocker[2])}")
            self.model.changed(it)
        for it in items:
            it.skip_reason = None
            it.error = None
            it.output = str(self.output_for(it))
            if Path(it.output) in skip:
                it.state, it.detail = "skipped", "key:batch.skipped_existing"
            else:
                it.state, it.detail = "queued", ""
                it.extra = {"settings": settings, "run": self._run_id}
                self._queue.append(it)
            self.model.changed(it)
        self.ctx.log.info("log.batch.started", n=len(self._queue))
        self._pump()
        self._update()

    def ask_conflicts(self, conflicts: list[Path]) -> str:
        """Ask before replacing existing outputs: overwrite | skip | cancel."""
        box = QMessageBox(QMessageBox.Icon.Warning, tr("dialog.overwrite_title"),
                          tr("batch.conflicts", n=len(conflicts)), parent=self.window())
        box.setDetailedText("\n".join(str(c) for c in conflicts))
        overwrite = box.addButton(tr("batch.overwrite"), QMessageBox.ButtonRole.DestructiveRole)
        skip_btn = box.addButton(tr("batch.skip_existing"), QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(tr("action.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(skip_btn)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite:
            return "overwrite"
        if clicked is skip_btn:
            return "skip"
        return "cancel"

    def _settings(self) -> dict:
        neutral = tuple(s.value() for s in self.neutral)
        return {
            "mode": self.mode.currentData(),
            "look_mode": self.look_mode.currentData(),
            "direction": self.direction.currentData(),
            "size": int(self.size.currentData()),
            "format": self.format.currentData(),
            "intent": self.intent.currentData(),
            "bpc": self.bpc.isChecked(),
            "gamut": self.gamut.currentData(),
            "neutral": neutral if all(v > 0 for v in neutral) else None,
            "kelvin": self.kelvin.value() or None,
            "overwrite": self._overwrite,
        }

    def _pump(self) -> None:
        while self._queue and len(self._running) < MAX_CONCURRENT:
            item = self._queue.pop(0)
            self._start(item)
        self._update()

    def _start(self, item: BatchItem) -> None:
        engine = self.ctx.engine
        s = item.extra["settings"]
        path, out = item.path, item.output

        def work(token, progress):
            progress(0.05, "batch.step.inspect")
            info = engine.inspect(path, token)
            if s["mode"] == "look":
                request = engine.look_request(info, s["look_mode"], s)
            else:
                request = engine.technical_request(info, dict(s, direction=self.direction_for(path, s["direction"])))
            progress(0.2, "batch.step.convert")
            lut = engine.convert(request, s["size"], 1.0, token,
                                 lambda f, *_a, **_k: progress(0.2 + 0.7 * (f or 0), "batch.step.convert"))
            token.raise_if_cancelled()
            progress(0.92, "batch.step.export")
            return engine.export(lut, out, s["overwrite"], token, s["format"])

        def done(info):
            item.state, item.detail = "succeeded", f"sha256 {info.sha256[:12]}\u2026"
            self.ctx.log.success("log.batch.item_done", detail=info.cube_path)

        def failed(err: ErrorInfo):
            item.state, item.error = "failed", err
            self.ctx.log.error("log.batch.item_failed", detail=f"{path}: {err.summary()} {err.detail}")

        def cancelled():
            item.state, item.detail = "cancelled", ""
            self.ctx.log.warning("log.cancelled", detail=path)

        def progress(fraction, key, kwargs):
            item.detail = f"key:{key}" if key else item.detail
            self.model.changed(item)
            self._update_progress()

        def finished(_state: JobState):
            self._running.discard(item.job_id)
            self.model.changed(item)
            self._pump()
            if not self.running():
                self._finished_run()

        item.state = "running"
        item.job_id = self.ctx.jobs.submit(
            f"batch:{self._run_id}:{path}", "job.batch_item", work, on_result=done, on_error=failed,
            on_cancel=cancelled, on_progress=progress, on_done=finished, side_effects=True, title_kwargs={"name": Path(path).name},
        )
        self._running.add(item.job_id)
        self.model.changed(item)

    def cancel(self) -> None:
        for it in self._queue:
            it.state = "cancelled"
            self.model.changed(it)
        self._queue.clear()
        for jid in list(self._running):
            self.ctx.jobs.cancel(jid)
        self._update()

    def _finished_run(self) -> None:
        counts: dict[str, int] = {}
        for it in self.model.items:
            counts[it.state] = counts.get(it.state, 0) + 1
        self.ctx.log.info("log.batch.finished", ok=counts.get("succeeded", 0), failed=counts.get("failed", 0),
                          cancelled=counts.get("cancelled", 0), skipped=counts.get("skipped", 0))
        self._update()

    # ----------------------------------------------------------- state
    def _update_progress(self) -> None:
        items = self.model.items
        if not items:
            self.overall.setValue(0)
            return
        done = sum(1 for i in items if i.state in ("succeeded", "failed", "cancelled", "skipped"))
        self.overall.setValue(int(done * 100 / len(items)))

    def _update(self) -> None:
        if not self.running():
            s = self._settings()
            for it in self.model.items:
                if it.info is not None:
                    old = it.blocker
                    self._evaluate(it, s)
                    if it.blocker != old:
                        self.model.changed(it)
        reason = self.run_reason_key()
        set_enabled_with_reason(self.run_button, self.run_reason, reason)
        self.cancel_button.setEnabled(self.running())
        running = self.running()
        self.clear_button.setEnabled(bool(self.model.items) and not running)
        self.remove_button.setEnabled(bool(self.table.selectionModel().selectedRows()) and not running)
        is_look = self.mode.currentData() == "look"
        self.look_mode.setEnabled(is_look)
        self.direction.setEnabled(not is_look)
        self._update_progress()
        counts: dict[str, int] = {}
        for it in self.model.items:
            counts[it.state] = counts.get(it.state, 0) + 1
        self.summary.setText(tr("batch.summary", total=len(self.model.items), ok=counts.get("succeeded", 0),
                                failed=counts.get("failed", 0), cancelled=counts.get("cancelled", 0),
                                skipped=counts.get("skipped", 0), running=counts.get("running", 0) + counts.get("queued", 0)))
        pending_blocked = sum(1 for it in self.model.items if it.state == "pending" and it.blocker is not None)
        checking = sum(1 for it in self.model.items if it.state == "pending" and it.checking)
        notes = []
        if pending_blocked and not running and reason is None:
            notes.append(tr("batch.blocked_note", n=pending_blocked))
        if checking:
            notes.append(tr("batch.checking_note", n=checking))
        self.blocked_note.setText("\n".join(notes))
        self.blocked_note.setVisible(bool(notes))






