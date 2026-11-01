"""Image Look Creator: an approximate global sRGB -> sRGB look estimated from
one or more *finished* reference photographs (docs/IMAGE_LOOK_API.md v1).

This pathway is deliberately separate from the Camera Look Extractor (DCP
tables) and Camera Match Studio (paired correspondences): there is no
"before" image, no sample pairing and no accuracy score.

Workflow
--------
1. References (analysed): add JPEG/PNG/TIFF files, include/exclude them, set
   positive weights (equal by default) and optionally crop each one.  Every
   change invalidates the analysis, the fitted look and the LUT, including
   in-flight jobs, so an old LUT can never be exported.
2. Look amounts are part of the engine's analysis settings; editing them
   requires analysing again.
3. Analysis runs on a worker; per-image and set diagnostics, warnings and
   conflicts are shown.  Conflicting references must be explicitly
   acknowledged (or the selection changed) before a LUT is generated.
4. The LUT (17/33/65, overall strength baked once by ``generate_lut``) is
   previewed on a separate *target* image and exported with the existing
   pipeline (same Lut3D, provenance from the engine).
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QSlider,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..backend import (
    IMAGE_LOOK_MAX_REFERENCES,
    IMAGE_LOOK_SUFFIXES,
    ImageLookAnalysisView,
    ImageLookBuild,
    ImageLookRefStats,
    LutData,
    to_qimage,
)
from ..i18n import T, tr
from ..jobs import ErrorInfo
from ..widgets.common import (
    SPACE_S,
    SPACE_XS,
    AdvancedSection,
    DisabledReason,
    HeadingLabel,
    WrapChoice,
    WrapLabel,
    make_button,
    set_enabled_with_reason,
)
from ..widgets.roi_editor import FULL_ROI, MIN_ROI_SPAN, RoiEditor, clamp_roi, is_full, is_valid_roi
from .lut_base import LutWorkspace

REF_PREVIEW_MAX = 640  # display decode size for crop editing / thumbnails (engine resizes)
THUMB_SIZE = 40
WEIGHT_MIN, WEIGHT_MAX = 0.01, 100.0
AMOUNT_KEYS = ("tone_amount", "color_amount", "saturation_amount")
ROLES = ("reference", "base")
KNOWN_WARNINGS = ("approximate_unverified", "limited_luminance_coverage", "limited_color_coverage",
                  "insufficient_samples", "reference_clipping", "output_clipping", "conflicting_tone",
                  "conflicting_color", "conflicting_saturation", "conflicts_acknowledged", "typical_photo_baseline")
KNOWN_CONFLICTS = ("conflicting_tone", "conflicting_color", "conflicting_saturation")
REF_STATES = ("loading", "ready", "failed", "cancelled", "queued")
BANDS = ("shadows", "midtones", "highlights")
# The engine reports the near-neutral cast for 5 lightness bands (centres 0.15/0.35/0.55/0.75/0.92);
# the diagnostics show the three middle ones (the extreme bands are too thin to be informative).
CAST_BANDS = (1, 2, 3)


def warning_text(code: str) -> str:
    """Translated text for engine warning codes; loader warnings verbatim."""
    return tr(f"imagelook.warning.{code}") if code in KNOWN_WARNINGS else code


def conflict_text(code: str) -> str:
    return tr(f"imagelook.conflict.{code}") if code in KNOWN_CONFLICTS else code


def _norm_path(path: str) -> str:
    return str(Path(path))


# ---------------------------------------------------------------------------
# references model / view
# ---------------------------------------------------------------------------


@dataclass
class ReferenceItem:
    uid: int
    path: str
    included: bool = True
    weight: float = 1.0
    roi: tuple[float, float, float, float] = FULL_ROI
    role: str = "reference"  # "reference" defines the look; "base" is what it is measured against
    state: str = "queued"  # queued | loading | ready | failed | cancelled
    error: ErrorInfo | None = None
    image: QImage | None = None  # orientation-corrected display decode
    thumb: QImage | None = None
    width: int = 0
    height: int = 0
    load_warnings: tuple[str, ...] = ()
    stats: ImageLookRefStats | None = None
    _pixmap: QPixmap | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name


class ReferencesModel(QAbstractTableModel):
    """Reference images (Model/View): inclusion, thumbnail, weight, share,
    crop and load/analysis state."""

    COLUMNS = ("imagelook.col.image", "imagelook.col.role", "imagelook.col.weight", "imagelook.col.share",
               "imagelook.col.crop", "imagelook.col.status")
    COL_IMAGE, COL_ROLE, COL_WEIGHT, COL_SHARE, COL_CROP, COL_STATUS = range(6)
    edited = Signal()  # user changed inclusion, role or weight

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[ReferenceItem] = []
        T.watch(self, self.refresh_all)

    # ------------------------------------------------------------ access
    def row_of(self, uid: int) -> int:
        for i, it in enumerate(self.items):
            if it.uid == uid:
                return i
        return -1

    def item(self, row: int) -> ReferenceItem | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def by_uid(self, uid: int) -> ReferenceItem | None:
        row = self.row_of(uid)
        return self.items[row] if row >= 0 else None

    def included(self, role: str | None = None) -> list[ReferenceItem]:
        return [it for it in self.items if it.included and (role is None or it.role == role)]

    def has_path(self, path: str) -> bool:
        norm = _norm_path(path).lower()
        return any(_norm_path(it.path).lower() == norm for it in self.items)

    def append(self, item: ReferenceItem) -> int:
        row = len(self.items)
        self.beginInsertRows(QModelIndex(), row, row)
        self.items.append(item)
        self.endInsertRows()
        self.refresh_shares()
        return row

    def remove_rows(self, rows: list[int]) -> list[ReferenceItem]:
        removed = []
        for row in sorted(set(rows), reverse=True):
            if 0 <= row < len(self.items):
                self.beginRemoveRows(QModelIndex(), row, row)
                removed.append(self.items.pop(row))
                self.endRemoveRows()
        self.refresh_shares()
        return removed

    def changed(self, uid: int) -> None:
        row = self.row_of(uid)
        if row >= 0:
            self.items[row]._pixmap = None
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.COLUMNS) - 1))

    def refresh_shares(self) -> None:
        if self.items:
            self.dataChanged.emit(self.index(0, self.COL_SHARE), self.index(len(self.items) - 1, self.COL_SHARE))

    def refresh_all(self) -> None:
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self.COLUMNS) - 1)
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(self.COLUMNS) - 1))

    def set_weight(self, row: int, weight: float) -> bool:
        return self.setData(self.index(row, self.COL_WEIGHT), weight, Qt.ItemDataRole.EditRole)

    def set_included(self, row: int, on: bool) -> bool:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        return self.setData(self.index(row, self.COL_IMAGE), state, Qt.ItemDataRole.CheckStateRole)

    def set_role(self, row: int, role: str) -> bool:
        return self.setData(self.index(row, self.COL_ROLE), role, Qt.ItemDataRole.EditRole)

    def share(self, it: ReferenceItem) -> float | None:
        """Weight share within the item's own role (references and bases are pooled separately)."""
        total = sum(i.weight for i in self.items if i.included and i.role == it.role)
        return it.weight / total if it.included and total > 0 else None

    # --------------------------------------------------------- Qt model
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return tr(self.COLUMNS[section])
        return None

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == self.COL_IMAGE:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        if index.column() in (self.COL_WEIGHT, self.COL_ROLE):
            base |= Qt.ItemFlag.ItemIsEditable
        return base

    def _status_text(self, it: ReferenceItem) -> str:
        if it.stats is not None:
            n = len([w for w in it.stats.warnings if w != "approximate_unverified"])
            return tr("imagelook.ref_analysed", n=n)
        if it.state == "failed" and it.error is not None:
            return f"{tr('imagelook.ref_state.failed')}: {it.error.summary()}"
        return tr(f"imagelook.ref_state.{it.state}")

    def _crop_text(self, it: ReferenceItem) -> str:
        if is_full(it.roi):
            return tr("imagelook.crop.full")
        x0, y0, x1, y1 = it.roi
        return tr("imagelook.crop.short", x0=f"{x0 * 100:.0f}", y0=f"{y0 * 100:.0f}", x1=f"{x1 * 100:.0f}",
                  y1=f"{y1 * 100:.0f}")

    def _thumb(self, it: ReferenceItem):
        if it.thumb is None:
            name = {"failed": "error_circle", "cancelled": "dismiss_circle"}.get(it.state, "image")
            return icons.icon(name)
        if it._pixmap is None:
            it._pixmap = QPixmap.fromImage(it.thumb)  # GUI thread only
        return it._pixmap

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.UserRole:
            return it.uid
        if col == self.COL_IMAGE:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if it.included else Qt.CheckState.Unchecked
            if role == Qt.ItemDataRole.DecorationRole:
                return self._thumb(it)
            if role == Qt.ItemDataRole.ToolTipRole:
                lines = [it.path]
                if it.width:
                    lines.append(tr("imagelook.ref_size", w=it.width, h=it.height))
                if it.error is not None:
                    lines += [it.error.summary(), it.error.detail]
                return "\n".join(x for x in lines if x)
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
                if role == Qt.ItemDataRole.AccessibleTextRole:
                    state = tr("imagelook.included" if it.included else "imagelook.excluded")
                    return f"{it.name}, {state}"
                return it.name
        if col == self.COL_ROLE:
            if role == Qt.ItemDataRole.EditRole:
                return it.role
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
                return tr(f"imagelook.role.{it.role}")
            if role == Qt.ItemDataRole.ToolTipRole:
                return tr("imagelook.role_tip")
        if col == self.COL_WEIGHT:
            if role == Qt.ItemDataRole.EditRole:
                return float(it.weight)
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
                return f"{it.weight:g}"
            if role == Qt.ItemDataRole.ToolTipRole:
                return tr("imagelook.weight_tip")
        if col == self.COL_SHARE and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole,
                                               Qt.ItemDataRole.ToolTipRole):
            share = self.share(it)
            return tr("imagelook.share.excluded") if share is None else f"{share * 100:.0f}%"
        if col == self.COL_CROP and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole,
                                              Qt.ItemDataRole.ToolTipRole):
            return self._crop_text(it)
        if col == self.COL_STATUS:
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
                return self._status_text(it)
            if role == Qt.ItemDataRole.ToolTipRole:
                lines = [self._status_text(it)]
                if it.stats is not None:
                    lines += [warning_text(w) for w in it.stats.warnings]
                lines += [str(w) for w in it.load_warnings]
                if it.error is not None and it.error.detail:
                    lines.append(it.error.detail)
                return "\n".join(lines)
            if role == Qt.ItemDataRole.DecorationRole:
                if it.state == "failed":
                    return icons.icon("error_circle")
                if it.stats is not None:
                    real = [w for w in it.stats.warnings if w != "approximate_unverified"]
                    return icons.icon("warning" if real else "checkmark_circle")
        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if not index.isValid():
            return False
        it = self.items[index.row()]
        if index.column() == self.COL_IMAGE and role == Qt.ItemDataRole.CheckStateRole:
            on = Qt.CheckState(value) == Qt.CheckState.Checked if not isinstance(value, bool) else value
            if on == it.included:
                return True
            it.included = on
            self.dataChanged.emit(index, self.index(index.row(), len(self.COLUMNS) - 1))
            self.refresh_shares()
            self.edited.emit()
            return True
        if index.column() == self.COL_ROLE and role == Qt.ItemDataRole.EditRole:
            if value not in ROLES:
                return False
            if value == it.role:
                return True
            it.role = value
            self.dataChanged.emit(index, self.index(index.row(), len(self.COLUMNS) - 1))
            self.refresh_shares()
            self.edited.emit()
            return True
        if index.column() == self.COL_WEIGHT and role == Qt.ItemDataRole.EditRole:
            try:
                w = float(value)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(w) or w < WEIGHT_MIN or w > WEIGHT_MAX:
                return False  # weights are strictly positive and finite
            w = round(w, 4)
            if w == it.weight:
                return True
            it.weight = w
            self.dataChanged.emit(index, index)
            self.refresh_shares()
            self.edited.emit()
            return True
        return False


class RoleDelegate(QStyledItemDelegate):
    """Combo editor for the reference / base role column."""

    def createEditor(self, parent, option, index):  # noqa: N802
        combo = QComboBox(parent)
        for role in ROLES:
            combo.addItem(tr(f"imagelook.role.{role}"), role)
        combo.setAccessibleName(tr("imagelook.col.role"))
        # picking an entry commits at once; no extra click or focus change is needed
        combo.activated.connect(lambda _i, c=combo: self._commit(c))
        return combo

    def _commit(self, editor) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor, index):  # noqa: N802
        i = editor.findData(index.data(Qt.ItemDataRole.EditRole))
        editor.setCurrentIndex(max(0, i))

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, editor.currentData(), Qt.ItemDataRole.EditRole)


class WeightDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):  # noqa: N802
        spin = QDoubleSpinBox(parent)
        spin.setRange(WEIGHT_MIN, WEIGHT_MAX)
        spin.setDecimals(2)
        spin.setSingleStep(0.1)
        spin.setAccessibleName(tr("imagelook.col.weight"))
        return spin


class ReferencesView(QTableView):
    """Reference table; accepts dropped JPEG/PNG/TIFF files; Delete removes."""

    files_dropped = Signal(list)
    remove_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
                             | QAbstractItemView.EditTrigger.SelectedClicked)
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(THUMB_SIZE + 8)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setHighlightSections(False)
        self.setMinimumHeight(110)
        self._placeholder_key = "imagelook.refs.empty"
        T.bind(self, "setAccessibleName", "imagelook.refs.heading")
        T.watch(self, lambda: self.viewport().update())

    @staticmethod
    def accepts(paths: list[str]) -> list[str]:
        return [p for p in paths if Path(p).suffix.lower() in IMAGE_LOOK_SUFFIXES]

    def _local(self, event) -> list[str]:
        md = event.mimeData()
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._local(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._local(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._local(event)
        if paths:
            event.acceptProposedAction()
            # unsupported files are reported by the workspace, never ignored silently
            self.files_dropped.emit(paths)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.state() != QAbstractItemView.State.EditingState:
            self.remove_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        model = self.model()
        if model is not None and model.rowCount() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(self.viewport().rect().adjusted(12, 8, -12, -8),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, tr(self._placeholder_key))
            painter.end()


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


class ImageLookWorkspace(LutWorkspace):
    key = "imagelook"
    icon_name = "image"
    build_title_key = "imagelook.job.build"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.analysis: ImageLookAnalysisView | None = None
        self._analysis_sig: tuple | None = None
        self._analysis_error: ErrorInfo | None = None
        self._had_analysis = False
        self._model_warnings: list[str] = []
        self._uids = itertools.count(1)
        self._load_queue: deque[int] = deque()
        self._loading_uid: int | None = None
        self._loading_job = -1
        # Superseded decode jobs whose worker has not returned yet: the next
        # decode waits for them so full-resolution decodes never overlap.
        self._draining: set[int] = set()
        self._module_reason = ctx.engine.image_look_status() if ctx.engine.available else ""

        # ---------------------------------------------------------- centre
        self.approx_banner = WrapLabel("imagelook.approx_banner", parent=self.center)
        self.approx_banner.setObjectName("imagelook_banner")
        self.center_layout.insertWidget(0, self.approx_banner)
        self.center_layout.removeWidget(self.preview)
        self.vsplit = QSplitter(Qt.Orientation.Vertical, self.center)
        self.vsplit.setObjectName("imagelook_vsplit")
        self.vsplit.setChildrenCollapsible(False)
        self.center_layout.insertWidget(1, self.vsplit, 1)

        refs_pane = QWidget(self.vsplit)
        rl = QVBoxLayout(refs_pane)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(SPACE_XS)
        self.refs_heading = HeadingLabel("imagelook.refs.heading", 3, refs_pane)
        rl.addWidget(self.refs_heading)
        self.refs_hint = WrapLabel("imagelook.refs.hint", secondary=True, parent=refs_pane)
        rl.addWidget(self.refs_hint)
        bar = QHBoxLayout()
        bar.setSpacing(SPACE_S)
        self.add_button = make_button("imagelook.add", "add", tooltip_key="imagelook.add_tip")
        self.add_base_button = make_button("imagelook.add_base", "add", tooltip_key="imagelook.add_base_tip")
        self.remove_button = make_button("imagelook.remove", "delete")
        bar.addWidget(self.add_button)
        bar.addWidget(self.add_base_button)
        bar.addWidget(self.remove_button)
        bar.addStretch(1)
        rl.addLayout(bar)
        # own row: three commands plus the longer count text do not fit side by side in the narrow centre column
        self.count_label = WrapLabel("", parent=refs_pane)
        rl.addWidget(self.count_label)
        # shown while references exist but no base image does: the typical-photo baseline is much less accurate
        self.base_hint = WrapLabel("imagelook.base_recommend", secondary=True, parent=refs_pane)
        rl.addWidget(self.base_hint)
        self.hsplit = QSplitter(Qt.Orientation.Horizontal, refs_pane)
        self.hsplit.setObjectName("imagelook_hsplit")
        self.hsplit.setChildrenCollapsible(False)
        self.model = ReferencesModel(self)
        self.table = ReferencesView(self.hsplit)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(ReferencesModel.COL_WEIGHT, WeightDelegate(self.table))
        self.table.setItemDelegateForColumn(ReferencesModel.COL_ROLE, RoleDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(ReferencesModel.COL_IMAGE, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(ReferencesModel.COL_IMAGE, 180)
        for col, w in ((ReferencesModel.COL_ROLE, 90), (ReferencesModel.COL_WEIGHT, 64), (ReferencesModel.COL_SHARE, 56),
                       (ReferencesModel.COL_CROP, 110)):
            self.table.setColumnWidth(col, w)
        # crop editor
        crop = QWidget(self.hsplit)
        cl = QVBoxLayout(crop)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(SPACE_XS)
        self.crop_title = QLabel(crop)
        cl.addWidget(self.crop_title)
        self.roi = RoiEditor(crop)
        cl.addWidget(self.roi, 1)
        crop_bar = QHBoxLayout()
        crop_bar.setSpacing(SPACE_S)
        self.reset_roi_button = make_button("imagelook.roi.reset", "zoom_fit")
        self.exact_button = QToolButton(crop)
        self.exact_button.setCheckable(True)
        self.exact_button.setAutoRaise(True)
        self.exact_button.setArrowType(Qt.ArrowType.RightArrow)
        self.exact_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        T.bind(self.exact_button, "setText", "imagelook.roi.exact")
        T.bind(self.exact_button, "setAccessibleName", "imagelook.roi.exact")
        crop_bar.addWidget(self.reset_roi_button)
        crop_bar.addWidget(self.exact_button)
        crop_bar.addStretch(1)
        cl.addLayout(crop_bar)
        # exact numeric crop (collapsed by default; the visual editor is keyboard-operable)
        self.roi_form = QWidget(crop)
        grid = QFormLayout(self.roi_form)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(SPACE_XS)
        grid.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.roi_form.setVisible(False)
        self.exact_button.toggled.connect(self._toggle_exact)
        self.roi_spins: dict[str, QDoubleSpinBox] = {}
        for name in ("x0", "y0", "x1", "y1"):
            spin = QDoubleSpinBox(self.roi_form)
            spin.setDecimals(1)
            spin.setRange(0.0, 100.0)
            spin.setSingleStep(1.0)
            spin.setSuffix(" %")
            spin.setKeyboardTracking(False)
            lab = QLabel(self.roi_form)
            T.bind(lab, "setText", f"imagelook.roi.{name}")
            T.bind(spin, "setAccessibleName", f"imagelook.roi.{name}")
            lab.setBuddy(spin)
            grid.addRow(lab, spin)
            self.roi_spins[name] = spin
            spin.valueChanged.connect(lambda v, n=name: self._roi_spin_changed(n, v))
        cl.addWidget(self.roi_form)
        self.hsplit.addWidget(self.table)
        self.hsplit.addWidget(crop)
        self.hsplit.setStretchFactor(0, 3)
        self.hsplit.setStretchFactor(1, 2)
        rl.addWidget(self.hsplit, 1)

        target_pane = QWidget(self.vsplit)
        tl = QVBoxLayout(target_pane)
        tl.setContentsMargins(0, SPACE_S, 0, 0)
        tl.setSpacing(SPACE_XS)
        self.target_heading = HeadingLabel("imagelook.target.heading", 3, target_pane)
        target_bar = QHBoxLayout()
        target_bar.setSpacing(SPACE_S)
        target_bar.addWidget(self.target_heading, 1)
        self.use_target_as_base_button = make_button("imagelook.use_target_as_base", "add",
                                                     tooltip_key="imagelook.use_target_tip")
        self.use_target_as_base_button.setEnabled(False)  # needs a loaded preview image
        target_bar.addWidget(self.use_target_as_base_button)
        tl.addLayout(target_bar)
        self.target_hint = WrapLabel("imagelook.target.hint", secondary=True, parent=target_pane)
        tl.addWidget(self.target_hint)
        tl.addWidget(self.preview, 1)
        self.vsplit.addWidget(refs_pane)
        self.vsplit.addWidget(target_pane)
        self.vsplit.setStretchFactor(0, 2)
        self.vsplit.setStretchFactor(1, 3)
        self.vsplit.setSizes([240, 360])
        self._compact = False
        self._base_hint_compact = False
        self.center.installEventFilter(self)
        self.preview.load_button.setVisible(True)
        self.preview.set_load_text_key("imagelook.preview_image")

        # ------------------------------------------------------------ side
        amounts = QGroupBox()
        T.bind(amounts, "setTitle", "imagelook.amounts.group")
        af = QFormLayout(amounts)
        af.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        af.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.amount_spins: dict[str, QSpinBox] = {}
        self.amount_sliders: dict[str, QSlider] = {}
        for key in AMOUNT_KEYS:
            row = QWidget(amounts)
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(SPACE_S)
            slider = QSlider(Qt.Orientation.Horizontal, row)
            slider.setRange(0, 100)
            slider.setValue(100)
            slider.setPageStep(10)
            spin = QSpinBox(row)
            spin.setRange(0, 100)
            spin.setValue(100)
            spin.setSuffix(" %")
            hl.addWidget(slider, 1)
            hl.addWidget(spin)
            lab = QLabel(amounts)
            T.bind(lab, "setText", f"imagelook.amount.{key}")
            T.bind(slider, "setAccessibleName", f"imagelook.amount.{key}")
            T.bind(spin, "setAccessibleName", f"imagelook.amount.{key}")
            T.bind(slider, "setToolTip", f"imagelook.amount_tip.{key}")
            lab.setBuddy(slider)
            af.addRow(lab, row)
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            spin.valueChanged.connect(lambda _v: self._analysis_settings_changed())
            self.amount_spins[key] = spin
            self.amount_sliders[key] = slider
        af.addRow(WrapLabel("imagelook.amounts.hint", secondary=True, parent=amounts))
        self.side.add(amounts)

        analyze_box = QGroupBox()
        T.bind(analyze_box, "setTitle", "imagelook.analyze.group")
        al = QVBoxLayout(analyze_box)
        self.analyze_button = make_button("imagelook.analyze", "play")
        self.analyze_button.setShortcut(QKeySequence("F5"))
        al.addWidget(self.analyze_button)
        self.analyze_progress = QProgressBar(analyze_box)
        self.analyze_progress.setRange(0, 100)
        self.analyze_progress.setVisible(False)
        T.bind(self.analyze_progress, "setAccessibleName", "imagelook.analyze.progress")
        al.addWidget(self.analyze_progress)
        self.analyze_reason_label = DisabledReason(analyze_box)
        al.addWidget(self.analyze_reason_label)
        # conflicts are decided right where the analysis is run, not buried below
        self.conflict_label = WrapLabel("", parent=analyze_box)
        self.conflict_label.setVisible(False)
        al.addWidget(self.conflict_label)
        self.ack = WrapChoice("imagelook.ack", parent=analyze_box)
        self.ack.setVisible(False)
        al.addWidget(self.ack)
        self.side.add(analyze_box)

        diag = QGroupBox()
        T.bind(diag, "setTitle", "imagelook.diag.group")
        dl = QVBoxLayout(diag)
        self.diag_label = WrapLabel("", parent=diag)
        dl.addWidget(self.diag_label)
        self.selected_label = WrapLabel("", secondary=True, parent=diag)
        dl.addWidget(self.selected_label)
        self.side.add(diag)

        self.side.add(self.make_output_group())
        self.advanced = AdvancedSection("advanced.title")
        adv = QWidget()
        advf = QFormLayout(adv)
        advf.setContentsMargins(0, 0, 0, 0)
        advf.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.seed = QSpinBox(adv)
        self.seed.setRange(0, 2**31 - 1)
        self.seed.setValue(42)
        self.analysis_size = QSpinBox(adv)
        self.analysis_size.setRange(16, 256)
        self.analysis_size.setSingleStep(16)
        self.analysis_size.setValue(128)
        for w, key in ((self.seed, "imagelook.adv.seed"), (self.analysis_size, "imagelook.adv.analysis_size")):
            lab = QLabel(adv)
            T.bind(lab, "setText", key)
            T.bind(w, "setAccessibleName", key)
            lab.setBuddy(w)
            advf.addRow(lab, w)
            w.valueChanged.connect(lambda _v: self._analysis_settings_changed())
        advf.addRow(WrapLabel("imagelook.adv.hint", secondary=True, parent=adv))
        self.advanced.add_widget(adv)
        self.side.add(self.advanced)
        self.side.add(self.make_status_group())
        self.side.add_stretch()

        # ---------------------------------------------------------- wiring
        # clicked(bool) must not leak into the role argument: bind it explicitly
        self.add_button.clicked.connect(lambda _checked=False: self.browse_references("reference"))
        self.add_base_button.clicked.connect(lambda _checked=False: self.browse_references("base"))
        self.use_target_as_base_button.clicked.connect(lambda _checked=False: self.add_preview_as_base())
        self.preview.image_loaded.connect(lambda _d: self._update_state())
        self.remove_button.clicked.connect(self.remove_selected)
        self.table.files_dropped.connect(self.add_references)
        self.table.remove_requested.connect(self.remove_selected)
        self.table.selectionModel().currentRowChanged.connect(lambda _c, _p: self._selection_changed())
        self.table.selectionModel().selectionChanged.connect(lambda _s, _d: self._update_state())
        self.model.edited.connect(self._references_changed)
        self.roi.roi_changed.connect(self._roi_committed)
        self.roi.roi_editing.connect(self._sync_roi_spins)
        self.reset_roi_button.clicked.connect(self.reset_crop)
        self.analyze_button.clicked.connect(self.analyze)
        self.ack.toggled.connect(lambda _b: self._ack_changed())
        ctx.jobs.job_finished.connect(self._load_job_finished)
        T.watch(self, self._render_all)
        self._selection_changed()
        self._render_all()
        self.settings_changed()

    # ----------------------------------------------------------- layout
    COMPACT_BELOW = 560  # logical px of the centre column

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.center and event.type() == QEvent.Type.Resize:
            self._apply_compact(self.center.width() < self.COMPACT_BELOW)
        return super().eventFilter(obj, event)

    def _apply_compact(self, compact: bool) -> None:
        """Narrow centre: hints move to tooltips, secondary columns hide
        (crop stays visible in the editor), so the preview keeps its room."""
        if compact == self._compact:
            return
        self._compact = compact
        for hint, heading, key in ((self.refs_hint, self.refs_heading, "imagelook.refs.hint"),
                                   (self.target_hint, self.target_heading, "imagelook.target.hint")):
            hint.setVisible(not compact)
            if compact:
                T.bind(heading, "setToolTip", key)
            else:
                T.unbind(heading)
                T.bind(heading, "setText", "imagelook.refs.heading" if heading is self.refs_heading
                       else "imagelook.target.heading")
                heading.setToolTip("")
        for col in (ReferencesModel.COL_SHARE, ReferencesModel.COL_CROP):
            self.table.setColumnHidden(col, compact)
        # Vertical budget: the target path stays in the path label's tooltip
        # source (heading tooltip) and the domain line in the status group.
        self.preview.path_label.setVisible(not compact)
        self.domain_label.setVisible(not compact)
        self._base_hint_compact = compact
        self._update_base_hint()
        self.table.setMinimumHeight(72 if compact else 110)
        self.roi.setMinimumHeight(72 if compact else 100)
        self._sync_target_tooltip()

    def _sync_target_tooltip(self) -> None:
        if self._compact:
            path = self.preview.path_label.path()
            hint = tr("imagelook.target.hint")
            self.target_heading.setToolTip(f"{hint}\n{path}" if path else hint)

    def _toggle_exact(self, on: bool) -> None:
        self.exact_button.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.roi_form.setVisible(on)

    # ----------------------------------------------------------- helpers
    def primary_focus(self):
        return self.add_button

    def activated(self) -> None:
        if self.ctx.engine.available and self._module_reason:
            # The module may have become importable (e.g. source checkout updated).
            self._module_reason = self.ctx.engine.image_look_status()
            self._update_state()

    def settings_dict(self) -> dict[str, Any]:
        s: dict[str, Any] = {k: self.amount_spins[k].value() / 100.0 for k in AMOUNT_KEYS}
        s["seed"] = int(self.seed.value())
        s["analysis_size"] = int(self.analysis_size.value())
        return s

    def request_signature(self) -> tuple:
        refs = tuple((_norm_path(it.path), it.weight, tuple(it.roi), it.role) for it in self.model.included())
        return refs, tuple(sorted(self.settings_dict().items()))

    def current_item(self) -> ReferenceItem | None:
        idx = self.table.currentIndex()
        return self.model.item(idx.row()) if idx.isValid() else None

    def select_row(self, row: int) -> None:
        if 0 <= row < self.model.rowCount():
            self.table.setCurrentIndex(self.model.index(row, 0))
            self.table.selectRow(row)

    def set_amount(self, key: str, percent: int) -> None:
        self.amount_spins[key].setValue(percent)

    # -------------------------------------------------------- references
    def browse_references(self, role: str = "reference") -> None:
        title = tr("imagelook.dialog.add_base" if role == "base" else "imagelook.dialog.add")
        files, _ = QFileDialog.getOpenFileNames(self.ctx.window, title, self.ctx.last_dir("images"),
                                                tr("imagelook.filter"))
        if files:
            self.ctx.remember_dir("images", files[0])
            self.add_references(files, role=role)

    def add_preview_as_base(self) -> list[int]:
        """Register the opened preview image as a base image (paths are de-duplicated as usual)."""
        source = self.preview.source
        if source is None:
            return []
        return self.add_references([source.path], role="base")

    def add_references(self, paths: list[str], role: str = "reference") -> list[int]:
        """Queue image files for display decoding with the given role
        (``reference`` defines the look, ``base`` is what it is measured
        against); returns new uids."""
        if role not in ROLES:
            raise ValueError(f"unknown image look role: {role!r}")
        added: list[int] = []
        for raw in paths:
            path = _norm_path(raw)
            if Path(path).suffix.lower() not in IMAGE_LOOK_SUFFIXES:
                self.ctx.log.warning("imagelook.log.unsupported", detail=path)
                continue
            if self.model.has_path(path):
                self.ctx.log.info("imagelook.log.duplicate", detail=path)
                continue
            if sum(1 for it in self.model.items if it.role == role) >= IMAGE_LOOK_MAX_REFERENCES:
                self.ctx.log.warning("imagelook.log.too_many", detail=path, n=IMAGE_LOOK_MAX_REFERENCES)
                continue
            item = ReferenceItem(next(self._uids), path, role=role)
            self.model.append(item)
            self._load_queue.append(item.uid)
            added.append(item.uid)
        if added:
            self.ctx.log.info("imagelook.log.added_base" if role == "base" else "imagelook.log.added", n=len(added))
            if self.current_item() is None:
                self.select_row(self.model.row_of(added[0]))
            self._pump_loads()
            self._references_changed()
        return added

    def _pump_loads(self) -> None:
        """Decode one reference at a time: the shared loader may decode a full
        resolution image before resizing, so display decodes never overlap -
        not even with a removed/cancelled decode whose worker is still
        running (it is drained first)."""
        if self._loading_uid is not None or self._draining:
            return
        while self._load_queue:
            uid = self._load_queue.popleft()
            item = self.model.by_uid(uid)
            if item is None:
                continue
            self._start_load(item)
            return

    def _start_load(self, item: ReferenceItem) -> None:
        engine = self.ctx.engine
        uid, path = item.uid, item.path
        item.state = "loading"
        self._loading_uid = uid
        self.model.changed(uid)

        def work(token, progress):
            data = engine.load_image(path, REF_PREVIEW_MAX, token, progress)
            image = to_qimage(data.array)
            thumb = image.scaled(THUMB_SIZE, THUMB_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            return data.width, data.height, image, thumb, tuple(data.warnings)

        def done(result):
            it = self.model.by_uid(uid)
            if it is None:
                return
            it.width, it.height, it.image, it.thumb, it.load_warnings = result
            it.state, it.error = "ready", None
            self.model.changed(uid)
            self.ctx.log.success("imagelook.log.ref_loaded", detail=path, w=it.width, h=it.height)
            for w in it.load_warnings:
                self.ctx.log.warning("log.image.warning", detail=f"{path}: {w}")
            if self.current_item() is it:
                self._selection_changed()

        def failed(err: ErrorInfo):
            it = self.model.by_uid(uid)
            if it is None:
                return
            it.state, it.error = "failed", err
            self.model.changed(uid)
            self.ctx.log.error("imagelook.log.ref_failed", detail=f"{path}: {err.summary()} {err.detail}".strip())
            if self.current_item() is it:
                self._selection_changed()

        def cancelled():
            it = self.model.by_uid(uid)
            if it is not None and it.state == "loading":
                it.state = "cancelled"
                self.model.changed(uid)

        def finished(_state):
            if self._loading_uid == uid:
                self._loading_uid = None
            self._pump_loads()
            self._update_state()

        self._loading_job = self.ctx.jobs.submit(
            f"imagelook:ref:{uid}", "imagelook.job.load_ref", work, on_result=done, on_error=failed,
            on_cancel=cancelled, on_done=finished, title_kwargs={"name": item.name})

    def _supersede_current_load(self) -> None:
        """Invalidate the in-flight decode; keep the slot until its worker returns."""
        if self._loading_uid is None:
            return
        self.ctx.jobs.invalidate(f"imagelook:ref:{self._loading_uid}")
        if self._loading_job > 0:
            self._draining.add(self._loading_job)
        self._loading_uid = None
        self._loading_job = -1

    def _load_job_finished(self, job_id: int, _state: str) -> None:
        # job_finished is emitted only when the worker itself has reported
        if job_id in self._draining:
            self._draining.discard(job_id)
            self._pump_loads()
            self._update_state()

    def _drop_load(self, uid: int) -> None:
        """Forget a pending/in-flight decode (result can never be applied)."""
        try:
            self._load_queue.remove(uid)
        except ValueError:
            pass
        if self._loading_uid == uid:
            self._supersede_current_load()
        else:
            self.ctx.jobs.invalidate(f"imagelook:ref:{uid}")

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentIndex().isValid():
            rows = [self.table.currentIndex().row()]
        if not rows:
            return
        self.remove_rows(rows)

    def remove_rows(self, rows: list[int]) -> None:
        removed = self.model.remove_rows(rows)
        for it in removed:
            self._drop_load(it.uid)
        if removed:
            self.ctx.log.info("imagelook.log.removed", n=len(removed))
            self._pump_loads()
            if self.model.rowCount():
                self.select_row(min(min(rows), self.model.rowCount() - 1))
            self._selection_changed()
            self._references_changed()

    def set_weight(self, row: int, weight: float) -> bool:
        return self.model.set_weight(row, weight)

    def set_included(self, row: int, on: bool) -> bool:
        return self.model.set_included(row, on)

    def set_role(self, row: int, role: str) -> bool:
        return self.model.set_role(row, role)

    # -------------------------------------------------------------- crop
    def _selection_changed(self) -> None:
        if not hasattr(self, "roi_spins"):
            return
        it = self.current_item()
        enabled = it is not None
        self.roi.setEnabled(enabled)
        for spin in self.roi_spins.values():
            spin.setEnabled(enabled)
        if it is None:
            self.roi.set_image(None, "imagelook.roi.empty")
            self.roi.set_roi(FULL_ROI)
        else:
            placeholder = {"failed": "imagelook.roi.failed", "cancelled": "imagelook.roi.cancelled"}.get(
                it.state, "viewer.loading")
            self.roi.set_image(it.image, placeholder)
            self.roi.set_roi(it.roi)
        self._sync_roi_spins(self.roi.roi())
        self._render_crop_title()
        self._render_selected()
        self._update_state()

    def _sync_roi_spins(self, roi) -> None:
        x0, y0, x1, y1 = roi
        values = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
        # Limits are rounded *inward* to the spin boxes' 0.1 % step so every
        # offered value is accepted (crops carry 4 decimals).
        def lo(v: float) -> float:
            return math.ceil(round(v * 1000, 6)) / 10

        def hi(v: float) -> float:
            return math.floor(round(v * 1000, 6)) / 10

        limits = {"x0": (0.0, hi(x1 - MIN_ROI_SPAN)), "x1": (lo(x0 + MIN_ROI_SPAN), 100.0),
                  "y0": (0.0, hi(y1 - MIN_ROI_SPAN)), "y1": (lo(y0 + MIN_ROI_SPAN), 100.0)}
        for name, spin in self.roi_spins.items():
            spin.blockSignals(True)
            spin.setRange(*limits[name])
            spin.setValue(values[name] * 100)
            spin.blockSignals(False)

    def _roi_spin_changed(self, name: str, value: float) -> None:
        it = self.current_item()
        if it is None:
            return
        roi = list(it.roi)
        roi[("x0", "y0", "x1", "y1").index(name)] = value / 100.0
        self.set_crop(self.model.row_of(it.uid), tuple(roi))

    def _roi_committed(self, roi) -> None:
        it = self.current_item()
        if it is not None:
            self.set_crop(self.model.row_of(it.uid), roi)

    def set_crop(self, row: int, roi) -> bool:
        """Set a reference crop; invalid (e.g. zero-area) rectangles are rejected."""
        it = self.model.item(row)
        if it is None or not is_valid_roi(roi):
            if it is not None and self.current_item() is it:
                self._sync_roi_spins(it.roi)
                self.roi.set_roi(it.roi)
            return False
        new = clamp_roi(*roi)
        if new == it.roi:
            return True
        it.roi = new
        self.model.changed(it.uid)
        if self.current_item() is it:
            self.roi.set_roi(new)
            self._sync_roi_spins(new)
            self._render_crop_title()
        self._references_changed()
        return True

    def reset_crop(self) -> None:
        it = self.current_item()
        if it is not None:
            self.set_crop(self.model.row_of(it.uid), FULL_ROI)

    def _render_crop_title(self) -> None:
        it = self.current_item()
        self.crop_title.setText(tr("imagelook.roi.title", name=it.name) if it else tr("imagelook.roi.title_none"))

    # --------------------------------------------------------- invalidation
    def _references_changed(self) -> None:
        self._invalidate_analysis()

    def _analysis_settings_changed(self) -> None:
        self._invalidate_analysis()

    def _invalidate_analysis(self) -> None:
        """Any reference, crop, weight, inclusion or analysis-setting change:
        drop the analysis (and any in-flight one), the model and the LUT."""
        if not hasattr(self, "status_label"):
            return
        self.ctx.jobs.invalidate("imagelook:analyze")
        if self.analysis is not None:
            self._had_analysis = True
        self.analysis = None
        self._analysis_sig = None
        self._analysis_error = None
        self._model_warnings = []
        for it in self.model.items:
            if it.stats is not None:
                it.stats = None
                self.model.changed(it.uid)
        self.analyze_progress.setVisible(False)
        self.progress.setVisible(False)
        self.ack.blockSignals(True)
        self.ack.setChecked(False)
        self.ack.blockSignals(False)
        self._render_all()
        self.settings_changed()

    def _ack_changed(self) -> None:
        if self.ack.isChecked() and self.analysis is not None:
            self.ctx.log.warning("imagelook.log.acknowledged",
                                 detail=", ".join(conflict_text(c) for c in self.analysis.conflicts))
        self.settings_changed()  # drops the LUT and its model notes, then re-renders

    def settings_changed(self) -> None:
        if hasattr(self, "status_label"):
            # model notes describe the built LUT, which is dropped here
            self._model_warnings = []
            super().settings_changed()
            if hasattr(self, "diag_label"):
                self._render_diagnostics()

    # ------------------------------------------------------------ analysis
    def _selection_reason(self) -> tuple[str, dict] | None:
        """The look needs at least one included reference-role image; base images are optional
        (without them a typical photo is the baseline).  At most 64 included images per role."""
        if not any(it.role == "reference" for it in self.model.items):
            return ("imagelook.reason.no_refs", {})
        if not self.model.included("reference"):
            return ("imagelook.reason.none_included", {})
        if any(len(self.model.included(role)) > IMAGE_LOOK_MAX_REFERENCES for role in ROLES):
            return ("imagelook.reason.too_many", {"n": IMAGE_LOOK_MAX_REFERENCES})
        return None

    def analyze_reason(self) -> tuple[str, dict] | None:
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        if self._module_reason:
            return ("imagelook.reason.module_unavailable", {"detail": self._module_reason})
        reason = self._selection_reason()
        if reason is not None:
            return reason
        included = self.model.included()
        failed = [it for it in included if it.state == "failed"]
        if failed:
            return ("imagelook.reason.ref_failed", {"name": failed[0].name})
        bad = [it for it in included if not is_valid_roi(it.roi) or not (WEIGHT_MIN <= it.weight <= WEIGHT_MAX)]
        if bad:
            return ("imagelook.reason.ref_invalid", {"name": bad[0].name})
        if self.ctx.jobs.is_busy("imagelook:analyze"):
            return ("imagelook.reason.analyzing", {})
        return None

    def analyze(self) -> int:
        reason = self.analyze_reason()
        if reason is not None:
            self._update_state()
            return -1
        engine = self.ctx.engine
        included = self.model.included()
        refs = [(it.path, it.weight, tuple(it.roi), it.role) for it in included]
        settings = self.settings_dict()
        sig = self.request_signature()
        self._analysis_error = None
        self.analysis = None
        self._model_warnings = []
        for it in self.model.items:  # a failed/cancelled re-analysis must not show old stats
            if it.stats is not None:
                it.stats = None
                self.model.changed(it.uid)
        self.analyze_progress.setValue(0)
        self.analyze_progress.setVisible(True)

        def work(token, progress):
            return engine.analyze_image_look(refs, settings, token, progress)

        def done(view: ImageLookAnalysisView):
            if sig != self.request_signature():  # defensive: never apply to a changed selection
                return
            self.analysis = view
            self._analysis_sig = sig
            self._had_analysis = True
            self._attach_stats(view)
            self.ctx.log.success("imagelook.log.analyzed", n=len(view.references), c=len(view.conflicts))
            for w in view.warnings:
                if w != "approximate_unverified":
                    self.ctx.log.warning("imagelook.log.warning", detail=warning_text(w))
            for c in view.conflicts:
                self.ctx.log.warning("imagelook.log.conflict", detail=conflict_text(c))
            self.ack.blockSignals(True)
            self.ack.setChecked(False)
            self.ack.blockSignals(False)
            self._render_all()
            self.settings_changed()  # build the LUT (or show the conflict reason)

        def failed(err: ErrorInfo):
            self._analysis_error = err
            self.ctx.log.error("imagelook.log.analysis_failed", detail=f"{err.summary()} {err.detail}".strip())
            self._render_all()
            self.settings_changed()

        def cancelled():
            self.ctx.log.warning("log.cancelled", detail=tr("imagelook.job.analyze", n=len(refs)))
            self._render_all()

        def finished(_state):
            self.analyze_progress.setVisible(False)
            self._update_state()

        job = self.ctx.jobs.submit("imagelook:analyze", "imagelook.job.analyze", work, on_result=done, on_error=failed,
                                   on_cancel=cancelled, on_done=finished,
                                   on_progress=lambda f, _k, _kw: self.analyze_progress.setValue(int((f or 0) * 100)),
                                   title_kwargs={"n": len(refs)})
        # the previous LUT belongs to the previous analysis
        self.settings_changed()
        self._render_all()
        return job

    def _attach_stats(self, view: ImageLookAnalysisView) -> None:
        def key(path, weight, roi, role):
            return (_norm_path(path).lower(), round(float(weight), 4), tuple(round(float(v), 4) for v in roi), role)

        stats = {key(s.path, s.weight, s.roi, s.role): s for s in view.references}
        for it in self.model.items:
            it.stats = stats.get(key(it.path, it.weight, it.roi, it.role)) if it.included else None
            self.model.changed(it.uid)

    # ------------------------------------------------------------- LUT hooks
    def build_precondition(self):
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        if self._module_reason:
            return ("imagelook.reason.module_unavailable", {"detail": self._module_reason})
        reason = self._selection_reason()
        if reason is not None:
            return reason
        if self.ctx.jobs.is_busy("imagelook:analyze"):
            return ("imagelook.reason.analyzing", {})
        if self._analysis_error is not None:
            return ("imagelook.reason.analysis_failed", {"reason": self._analysis_error.summary()})
        if self.analysis is None or self._analysis_sig != self.request_signature():
            return ("imagelook.reason.reanalyze" if self._had_analysis else "imagelook.reason.needs_analysis", {})
        if self.analysis.conflicts and not self.ack.isChecked():
            return ("imagelook.reason.conflicts", {"conflicts": ", ".join(conflict_text(c) for c in self.analysis.conflicts)})
        return None

    def build_job(self, size: int, strength: float):
        engine, analysis = self.ctx.engine, self.analysis
        acknowledge = bool(analysis.conflicts) and self.ack.isChecked()

        def work(token, progress):
            return engine.build_image_look_lut(analysis, acknowledge, size, strength, token, progress)

        return work

    def _built(self, result) -> None:
        if isinstance(result, ImageLookBuild):
            if result.analysis is not self.analysis:
                return  # fitted from an analysis that is no longer current
            self._model_warnings = list(result.warnings)
            for w in result.warnings:
                if w not in ("approximate_unverified",):
                    self.ctx.log.warning("imagelook.log.model_warning", detail=warning_text(w))
            lut: LutData = result.lut
        else:
            lut = result
        super()._built(lut)
        self._render_diagnostics()

    def _refresh_preview(self) -> None:
        if hasattr(self, "target_heading"):
            self._sync_target_tooltip()
        if self.preview.source is None:
            self.domain_label.setText(self.domain_text())
            self.preview.set_note("imagelook.preview.no_target")
            return
        super()._refresh_preview()

    def _build_cancelled(self) -> None:
        super()._build_cancelled()
        if self.lut is None:
            self._refresh_preview()  # no stale After without a LUT

    def cancel(self) -> None:
        analyzing = self.ctx.jobs.is_busy("imagelook:analyze")
        rendering = self.ctx.jobs.is_busy("imagelook:preview")
        self.ctx.jobs.invalidate("imagelook:analyze")
        self.analyze_progress.setVisible(False)
        pending = list(self._load_queue)
        self._load_queue.clear()
        if self._loading_uid is not None:
            pending.append(self._loading_uid)
            self._supersede_current_load()
        for uid in pending:
            it = self.model.by_uid(uid)
            if it is not None and it.state in ("queued", "loading"):
                it.state = "cancelled"
                self.model.changed(uid)
        if analyzing:
            self.ctx.log.warning("log.cancelled", detail=tr("imagelook.job.analyze", n=len(self.model.included())))
        super().cancel()
        # The base cancel stops the debounced rebuild that would otherwise
        # refresh the viewer: never leave a previous LUT's After on screen.
        if self.lut is None:
            self._refresh_preview()
        elif rendering and self.preview.source is not None:
            self.preview.render(None)  # Before only; the in-flight render of this LUT was cancelled
            self.preview.set_note("imagelook.preview.cancelled")
        self._selection_changed()
        self._render_all()

    def shutdown(self) -> None:
        self._load_queue.clear()
        self._debounce.stop()

    def suggested_name(self) -> str:
        return f"image_look_{len(self.model.included('reference'))}refs_APPROX_{self.size()}.cube"

    def domain_text(self) -> str:
        if not hasattr(self, "model"):
            return ""
        bases = len(self.model.included("base"))
        baseline = tr("imagelook.domain.baseline_base", m=bases) if bases else tr("imagelook.domain.baseline_typical")
        return tr("imagelook.domain", n=len(self.model.included("reference")), baseline=baseline, size=self.size(),
                  strength=self.strength_spin.value() if hasattr(self, "strength_spin") else 100)

    # ------------------------------------------------------------- state
    def _busy_channels(self) -> bool:
        return any(r.channel.startswith("imagelook:") for r in self.ctx.jobs.active())

    def _update_state(self) -> None:
        if not hasattr(self, "analyze_button"):
            return
        super()._update_state()
        self.cancel_button.setEnabled(self._busy_channels() or bool(self._load_queue))
        reason = self.analyze_reason()
        key, kwargs = reason if reason else (None, {})
        set_enabled_with_reason(self.analyze_button, self.analyze_reason_label, key, **kwargs)
        has_sel = bool(self.table.selectionModel().selectedRows()) or self.table.currentIndex().isValid()
        self.remove_button.setEnabled(has_sel and bool(self.model.items))
        it = self.current_item()
        self.reset_roi_button.setEnabled(it is not None and not is_full(it.roi))
        self.use_target_as_base_button.setEnabled(self.preview.source is not None)
        self._render_count()
        self._update_base_hint()

    def _update_base_hint(self) -> None:
        if not hasattr(self, "base_hint"):
            return
        needed = bool(self.model.included("reference")) and not self.model.included("base")
        self.base_hint.setVisible(needed and not self._base_hint_compact)

    def _render_count(self) -> None:
        refs = [it for it in self.model.items if it.role == "reference"]
        self.count_label.setText(tr("imagelook.count", n=len(refs), k=len(self.model.included("reference")),
                                    m=len(self.model.included("base"))))

    def _render_all(self) -> None:
        if not hasattr(self, "diag_label"):
            return
        self._sync_target_tooltip()
        self._render_crop_title()
        self._render_diagnostics()
        self._render_selected()
        self._update_state()

    def _render_diagnostics(self) -> None:
        a = self.analysis
        if a is None:
            self.conflict_label.setVisible(False)
            self.ack.setVisible(False)
            if self.ctx.jobs.is_busy("imagelook:analyze"):
                text = tr("imagelook.diag.running", n=len(self.model.included()))
            elif self._analysis_error is not None:
                err = self._analysis_error
                text = tr("imagelook.diag.failed", reason=err.summary(), detail=err.detail)
            elif self._had_analysis:
                text = tr("imagelook.diag.stale")
            else:
                text = tr("imagelook.diag.none")
            self.diag_label.setText(text)
            return
        refs, bases = a.by_role("reference"), a.by_role("base")
        lines = [tr("imagelook.diag.set", n=len(refs), samples=sum(r.sample_count for r in refs))]
        # the baseline decides how trustworthy the estimate is, so it comes first
        if a.baseline == "base_images":
            lines.append(tr("imagelook.diag.baseline_base", m=len(bases)))
        else:
            lines.append(tr("imagelook.diag.baseline_typical"))
        spans = [r.quantiles for r in refs if r.quantiles]
        if spans:
            lo = min(q[0] for q in spans)
            hi = max(q[-1] for q in spans)
            lines.append(tr("imagelook.diag.set_tone", lo=f"{lo:.2f}", hi=f"{hi:.2f}"))
        if refs:
            clipped = max(r.clipped_fraction for r in refs)
            lines.append(tr("imagelook.diag.set_clip", p=f"{clipped * 100:.1f}"))
        for r in refs + bases:
            q = r.quantiles
            lines.append(tr("imagelook.diag.ref_line", name=Path(r.path).name, role=tr(f"imagelook.role.{r.role}"),
                            lo=f"{q[0]:.2f}" if q else "?", mid=f"{q[len(q) // 2]:.2f}" if q else "?",
                            hi=f"{q[-1]:.2f}" if q else "?", chroma=f"{r.saturation:.3f}",
                            support=f"{r.color_support * 100:.0f}", clip=f"{r.clipped_fraction * 100:.1f}"))
        # conflict codes are listed in their own section (with the acknowledgement)
        warnings = [w for w in a.warnings if w not in a.conflicts]
        if warnings:
            lines.append(tr("imagelook.diag.warnings"))
            lines += [f"\u26a0 {warning_text(w)}" for w in warnings]
        extra = [w for w in self._model_warnings if w not in warnings and w not in a.conflicts]
        if extra:
            lines.append(tr("imagelook.diag.model_warnings"))
            lines += [f"\u26a0 {warning_text(w)}" for w in extra]
        lines.append(tr("imagelook.diag.no_score"))
        self.diag_label.setText("\n".join(lines))
        if a.conflicts:
            text = [tr("imagelook.diag.conflicts")] + [f"\u26a0 {conflict_text(c)}" for c in a.conflicts]
            text.append(tr("imagelook.diag.conflict_help"))
            self.conflict_label.setText("\n".join(text))
            self.conflict_label.setVisible(True)
            self.ack.setVisible(True)
        else:
            self.conflict_label.setVisible(False)
            self.ack.setVisible(False)

    def _render_selected(self) -> None:
        if not hasattr(self, "selected_label"):
            return
        it = self.current_item()
        if it is None:
            self.selected_label.setText("")
            self.selected_label.setVisible(False)
            return
        self.selected_label.setVisible(True)
        s = it.stats
        if s is None:
            state = tr(f"imagelook.ref_state.{it.state}")
            if not it.included:
                state = tr("imagelook.excluded")
            self.selected_label.setText(tr("imagelook.sel.pending", name=it.name, state=state))
            return
        q = s.quantiles
        lines = [tr("imagelook.sel.title", name=it.name),
                 tr("imagelook.sel.role", role=tr(f"imagelook.role.{s.role}")),
                 tr("imagelook.sel.samples", n=s.sample_count),
                 tr("imagelook.sel.tone", lo=f"{q[0]:.2f}", mid=f"{q[len(q) // 2]:.2f}", hi=f"{q[-1]:.2f}"),
                 tr("imagelook.sel.chroma", v=f"{s.saturation:.3f}"),
                 tr("imagelook.sel.support", p=f"{s.color_support * 100:.0f}"),
                 tr("imagelook.sel.clip", p=f"{s.clipped_fraction * 100:.1f}")]
        for band, index in zip(BANDS, CAST_BANDS):
            if index < len(s.cast):
                a_, b_ = s.cast[index]
                lines.append(tr(f"imagelook.sel.band.{band}", a=f"{a_:+.3f}", b=f"{b_:+.3f}"))
        lines.append(tr("imagelook.sel.sha", sha=s.sha256[:16]))
        lines += [f"\u26a0 {warning_text(w)}" for w in s.warnings if w != "approximate_unverified"]
        self.selected_label.setText("\n".join(lines))
