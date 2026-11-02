"""LUT Check: diagnose any 3D LUT file (or the LUT built on another page),
explain each finding in plain words and, only on request, repair it.

Layout (docs/LUT_HEALTH_API.md section 5):

* header: Open LUT, Cancel, Export
* centre: source bar (or drop area), input-kind card, then
  [findings list + explanation | tabs: grey axis / hue map / image preview]
* side column: Repair, LUT output (grid size, file format), Status and export

The workspace reuses :class:`LutWorkspace` for the preview, the output group,
the export path and the background-job plumbing.  The LUT it "builds" is the
checked file (or its repaired version) resampled to 17/33/65, so preview and
export use exactly the same ``Lut3D``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..backend import HEALTH_ATTENTION, LUT_FILE_SUFFIXES, LUT_SIZES, HealthView, LoadedLutView, LutData, RepairView, to_qimage
from ..i18n import T, tr
from ..jobs import ErrorInfo
from ..widgets.common import (
    SPACE_S,
    SPACE_XS,
    DisabledReason,
    DropArea,
    PathLabel,
    WrapChoice,
    WrapLabel,
    make_button,
    set_enabled_with_reason,
)
from ..widgets.findings_list import (
    ExplanationPanel,
    FindingRow,
    FindingsView,
    format_value,
    sentence,
)
from ..widgets.gray_axis_chart import GrayAxisChart
from ..widgets.hue_map import HueMap
from .lut_base import LutWorkspace


def nearest_grid(n: int) -> int:
    """Supported export grid (17/33/65) closest to a file's own grid; ties go to the larger one."""
    return min(LUT_SIZES, key=lambda s: (abs(s - n), -s))


def sub_progress(progress, lo: float, hi: float):
    """Map an engine call's 0..1 progress into the [lo, hi] part of a job's bar."""

    def inner(fraction=None, key: str = "", **kwargs):
        progress(None if fraction is None else lo + (hi - lo) * float(fraction), key, **kwargs)

    return inner


class SourceBar(QFrame):
    """Which LUT is being checked: path or origin, format/grid/title, and the file's own warnings."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SPACE_S, SPACE_XS, SPACE_S, SPACE_XS)
        lay.setSpacing(0)
        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(SPACE_S)
        self.path = PathLabel(row)          # elided in the middle; the full path is the tooltip
        self.info = QLabel(row)
        self.info.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.info.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        rl.addWidget(self.path, 1)
        rl.addWidget(self.info, 0)
        self.origin = WrapLabel("", parent=self)
        self.warnings = WrapLabel("", secondary=True, parent=self)
        for w in (row, self.origin, self.warnings):
            lay.addWidget(w)
        T.bind(self, "setAccessibleName", "health.source.accessible")


class MessageBar(QFrame):
    """An error that does not replace the LUT already being checked."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACE_S, SPACE_XS, SPACE_S, SPACE_XS)
        self.icon_label = QLabel(self)
        self.text = WrapLabel("", parent=self)
        lay.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.text, 1)
        self._set_icon()
        self.setVisible(False)

    def _set_icon(self) -> None:
        from PySide6.QtCore import QSize

        self.icon_label.setPixmap(icons.icon("error_circle").pixmap(QSize(20, 20), self.devicePixelRatioF()))

    def changeEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.PaletteChange:
            self._set_icon()
        super().changeEvent(event)

    def show_text(self, text: str) -> None:
        self.text.setText(text)
        self.setAccessibleName(text)
        self.setVisible(bool(text))


class KindCard(QFrame):
    """Estimate of what the LUT expects as input, with log-curve candidates and the Rec.709/sRGB note."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        T.bind(self, "setAccessibleName", "health.kind.accessible")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SPACE_S, SPACE_XS, SPACE_S, SPACE_XS)
        lay.setSpacing(0)
        self.name = WrapLabel("", parent=self)
        font = self.name.font()
        font.setBold(True)
        self.name.setFont(font)
        self.candidates = WrapLabel("", parent=self)
        self.note = WrapLabel("", secondary=True, parent=self)
        for w in (self.name, self.candidates, self.note):
            lay.addWidget(w)

    def show_report(self, report: HealthView | None) -> None:
        self.setVisible(report is not None)
        if report is None:
            return
        k = report.kind
        text = f"{tr('health.kind.title')}: {tr(f'health.kind.name.{k.kind}')}"
        if k.kind != "unknown":
            text += f" — {tr('health.kind.confidence', value=f'{round(k.confidence * 100)} %')}"
        self.name.setText(text)
        show_candidates = bool(k.log_candidates) and k.kind in ("log_to_display", "display_to_log")
        if show_candidates:
            names = []
            for curve, score in k.log_candidates:
                label = tr(f"health.curve.{curve}")
                names.append(f"{curve if label == f'health.curve.{curve}' else label} {round(score * 100)} %")
            self.candidates.setText(tr("health.kind.candidates", list=", ".join(names)))
        self.candidates.setVisible(show_candidates)
        note = tr(k.note_key) if k.note_key else ""
        self.note.setText(note)
        self.note.setVisible(bool(note))


class DiagnoseWorkspace(LutWorkspace):
    key = "diagnose"
    icon_name = "checkmark_circle"
    build_title_key = "health.job.prepare"
    has_strength = False      # a checked LUT is exported as it is; there is no strength
    show_check_action = False  # this page is the check

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.loaded: LoadedLutView | None = None   # the LUT as read (never modified)
        self.report: HealthView | None = None      # diagnosis of ``loaded``
        self.repair: RepairView | None = None      # None: the original is used
        self.page_key = ""                         # nav key of the page a LUT was received from
        self.load_error: ErrorInfo | None = None
        self.repair_error: ErrorInfo | None = None

        self.open_button = make_button("health.action.open", "folder_open", primary=True)
        self.export_button.setDefault(False)
        self.export_button.setAutoDefault(False)
        self.commands.insertWidget(0, self.open_button)

        # --- centre -------------------------------------------------------
        self.center_layout.removeWidget(self.preview)
        self.center_layout.removeWidget(self.domain_label)
        self.drop = DropArea(LUT_FILE_SUFFIXES, "health.drop_hint", self.center)
        self.source_bar = SourceBar(self.center)
        self.source_bar.setVisible(False)
        self.message_bar = MessageBar(self.center)
        self.kind_card = KindCard(self.center)
        self.kind_card.setVisible(False)
        self.center_layout.addWidget(self.drop)
        self.center_layout.addWidget(self.source_bar)
        self.center_layout.addWidget(self.message_bar)
        self.center_layout.addWidget(self.kind_card)

        self.work = QSplitter(Qt.Orientation.Horizontal, self.center)
        self.work.setObjectName("diagnose_work_split")
        self.work.setChildrenCollapsible(False)
        self.center_layout.addWidget(self.work, 1)

        # left: overall line, findings list, explanation
        left = QSplitter(Qt.Orientation.Vertical, self.work)
        left.setObjectName("diagnose_left_split")
        left.setChildrenCollapsible(False)
        findings_box = QWidget(left)
        fl = QVBoxLayout(findings_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(SPACE_XS)
        self.overall = WrapLabel("", parent=findings_box)
        font = self.overall.font()
        font.setBold(True)
        self.overall.setFont(font)
        self.findings = FindingsView(findings_box)
        fl.addWidget(self.overall)
        fl.addWidget(self.findings, 1)
        self.explanation = ExplanationPanel(left)
        left.addWidget(findings_box)
        left.addWidget(self.explanation)
        left.setStretchFactor(0, 1)
        left.setStretchFactor(1, 0)
        left.setSizes([360, 120])
        self.left_split = left
        self._splits_set = False

        # right: tabs
        self.tabs = QTabWidget(self.work)
        self.tabs.setDocumentMode(True)
        self.gray_chart = GrayAxisChart(self.tabs)
        self.hue_map = HueMap(self.tabs)
        preview_page = QWidget(self.tabs)
        pl = QVBoxLayout(preview_page)
        pl.setContentsMargins(0, SPACE_S, 0, 0)
        pl.setSpacing(SPACE_S)
        pl.addWidget(self.preview, 1)
        pl.addWidget(self.domain_label)
        self.tabs.addTab(self.gray_chart, "")
        self.tabs.addTab(self.hue_map, "")
        self.tabs.addTab(preview_page, "")
        self.work.addWidget(left)
        self.work.addWidget(self.tabs)
        self.work.setStretchFactor(0, 0)
        self.work.setStretchFactor(1, 1)
        self.work.setSizes([380, 420])
        left.setMinimumWidth(220)
        self.tabs.setMinimumWidth(200)

        # --- side ----------------------------------------------------------
        self.side.add(self._make_repair_group())
        self.side.add(self.make_output_group())
        self.side.add(self.make_status_group())
        self.side.add_stretch()

        # --- wiring ---------------------------------------------------------
        self.open_button.clicked.connect(self.browse)
        self.drop.browse_requested.connect(self.browse)
        self.drop.files_dropped.connect(self.open_dropped)
        self.findings.finding_selected.connect(self._finding_selected)
        self.apply_button.clicked.connect(self.apply_repair)
        self.discard_button.clicked.connect(self.discard_repair)
        self.smooth_slider.valueChanged.connect(self.smooth_spin.setValue)
        self.smooth_spin.valueChanged.connect(self.smooth_slider.setValue)
        for control in (self.smooth_spin,):
            control.valueChanged.connect(lambda _v: self._update_repair_controls())
        for choice in (self.opt_jumps, self.opt_round, self.opt_gray):
            choice.toggled.connect(lambda _b: self._update_repair_controls())
        ctx.lut_for_diagnosis.connect(self.receive_lut)
        ctx.jobs.job_finished.connect(lambda *_: self._update_repair_controls())
        T.watch(self, self._retranslate)
        self.setAcceptDrops(True)
        self._retranslate()
        self._set_status("health.reason.no_lut")
        self._update_state()

    # ------------------------------------------------------------ construction
    def _make_repair_group(self) -> QGroupBox:
        box = QGroupBox()
        T.bind(box, "setTitle", "health.repair.group")
        lay = QVBoxLayout(box)
        lay.setSpacing(SPACE_S)
        lay.addWidget(WrapLabel("health.repair.intro", secondary=True, parent=box))
        label = QLabel(box)
        T.bind(label, "setText", "health.repair.smooth")
        row = QWidget(box)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(SPACE_S)
        self.smooth_slider = QSlider(Qt.Orientation.Horizontal, row)
        self.smooth_slider.setRange(0, 100)
        self.smooth_slider.setPageStep(10)
        self.smooth_spin = QSpinBox(row)
        self.smooth_spin.setRange(0, 100)
        self.smooth_spin.setSuffix(" %")
        rl.addWidget(self.smooth_slider, 1)
        rl.addWidget(self.smooth_spin)
        label.setBuddy(self.smooth_slider)
        T.bind(self.smooth_slider, "setAccessibleName", "health.repair.smooth")
        T.bind(self.smooth_spin, "setAccessibleName", "health.repair.smooth_value")
        lay.addWidget(label)
        lay.addWidget(row)
        self.opt_jumps = WrapChoice("health.repair.fix_jumps", parent=box)
        self.opt_round = WrapChoice("health.repair.round_clip", parent=box)
        self.opt_gray = WrapChoice("health.repair.lock_gray", parent=box)
        for choice in (self.opt_jumps, self.opt_round, self.opt_gray):
            lay.addWidget(choice)
        self.apply_button = make_button("health.action.apply", "play")
        self.discard_button = make_button("health.action.discard", "dismiss_circle")
        lay.addWidget(self.apply_button)
        lay.addWidget(self.discard_button)
        self.repair_reason = DisabledReason(box)
        lay.addWidget(self.repair_reason)
        self.repair_progress = QProgressBar(box)
        self.repair_progress.setRange(0, 100)
        self.repair_progress.setVisible(False)
        T.bind(self.repair_progress, "setAccessibleName", "health.repair.progress")
        lay.addWidget(self.repair_progress)
        self.repair_result = WrapLabel("", parent=box)
        self.repair_hint = WrapLabel("health.repair.result_hint", secondary=True, parent=box)
        lay.addWidget(self.repair_result)
        lay.addWidget(self.repair_hint)
        return box

    def primary_focus(self):
        return self.open_button

    EXPLANATION_HEIGHT = 112  # default height of the explanation; the list gets the rest (drag the handle to change)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._splits_set:
            QTimer.singleShot(0, self._default_splits)

    def _default_splits(self) -> None:
        total = self.left_split.height()
        if self._splits_set or total <= self.EXPLANATION_HEIGHT * 2:
            return
        self._splits_set = True
        self.left_split.setSizes([total - self.EXPLANATION_HEIGHT, self.EXPLANATION_HEIGHT])

    # ----------------------------------------------------------- opening a LUT
    def browse(self) -> None:
        path = self.ctx.ask_lut()
        if path:
            self.open_path(path)

    def open_dropped(self, paths: list[str]) -> None:
        if paths:
            self.open_path(paths[0])

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        self.drop.dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.drop.dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self.drop.dropEvent(event)

    def open_path(self, path: str) -> int:
        """Read and diagnose a LUT file in the background."""
        path = str(Path(path))
        if Path(path).suffix.lower() not in LUT_FILE_SUFFIXES:
            self.ctx.log.warning("log.profile.unsupported_suffix", detail=path)
            return -1
        engine = self.ctx.engine

        def work(token, progress):
            loaded = engine.load_lut_file(path, token, sub_progress(progress, 0.0, 0.3))
            return loaded, engine.diagnose(loaded, token, sub_progress(progress, 0.3, 1.0))

        return self._start_load(work, Path(path).name, page_key="")

    def receive_lut(self, lut: LutData, page_key: str = "") -> int:
        """Check the LUT another page has built (sent by its "Check this LUT" button)."""
        engine = self.ctx.engine

        def work(token, progress):
            loaded = engine.lut_to_loaded(lut)
            return loaded, engine.diagnose(loaded, token, sub_progress(progress, 0.1, 1.0))

        return self._start_load(work, lut.title, page_key=page_key)

    def _start_load(self, work, name: str, *, page_key: str) -> int:
        if self.ctx.jobs.is_busy("diagnose:repair"):
            self.ctx.jobs.cancel_channel("diagnose:repair")
        self.message_bar.show_text("")
        self._set_status("health.status.loading", name=name)
        self.progress.setValue(0)
        self.progress.setVisible(True)

        def done(result):
            self.progress.setVisible(False)
            loaded, report = result
            self._loaded(loaded, report, page_key)

        def failed(err: ErrorInfo):
            self.progress.setVisible(False)
            self.load_error = err
            text = err.summary() + (f"\n{err.detail}" if err.detail else "")
            self.message_bar.show_text(text)
            self._set_status("health.status.load_failed", reason=err.summary(), detail=err.detail)
            self.ctx.log.error("health.log.failed", detail=f"{name}: {err.summary()} {err.detail}".strip())
            self._update_state()

        def cancelled():
            self.progress.setVisible(False)
            self.ctx.log.warning("log.cancelled", detail=tr("health.job.check", name=name))
            self._set_status("lut.status.cancelled" if self.loaded is None else self._ready_key(), size=self.size())
            self._update_state()

        job = self.ctx.jobs.submit(
            "diagnose:load", "health.job.check", work, on_result=done, on_error=failed, on_cancel=cancelled,
            on_progress=lambda f, _k, _kw: self.progress.setValue(int((f or 0) * 100)), title_kwargs={"name": name})
        self._update_state()
        return job

    def _loaded(self, loaded: LoadedLutView, report: HealthView, page_key: str) -> None:
        self.ctx.jobs.invalidate("diagnose:repair")
        self.loaded, self.report, self.repair = loaded, report, None
        self.page_key = page_key
        self.load_error = self.repair_error = None
        origin = loaded.path or (f"{tr(f'nav.{page_key}')}: {loaded.title}" if page_key else loaded.title)
        self.ctx.log.info("health.log.opened", detail=origin)
        # Export at the supported grid closest to the file's own, so nothing is resampled needlessly.
        self.size_combo.blockSignals(True)
        self.size_combo.setCurrentIndex(max(0, self.size_combo.findData(nearest_grid(loaded.size))))
        self.size_combo.blockSignals(False)
        self._render_all()
        self.settings_changed()

    # -------------------------------------------------------------- repair
    def repair_settings(self) -> dict:
        return {"smooth": self.smooth_spin.value() / 100.0, "fix_jumps": self.opt_jumps.isChecked(),
                "round_clip": self.opt_round.isChecked(), "lock_gray": self.opt_gray.isChecked()}

    def repair_reason_key(self) -> tuple[str, dict] | None:
        if not self.ctx.engine.available or self.ctx.engine.health_status():
            return ("health.reason.unavailable", {})
        if self.ctx.jobs.is_busy("diagnose:load"):
            return ("health.reason.checking", {})
        if self.loaded is None:
            return ("health.reason.no_lut", {})
        if self.ctx.jobs.is_busy("diagnose:repair"):
            return ("health.reason.repairing", {})
        s = self.repair_settings()
        if s["smooth"] == 0.0 and not (s["fix_jumps"] or s["round_clip"] or s["lock_gray"]):
            return ("health.reason.no_option", {})
        return None

    def apply_repair(self) -> int:
        if self.repair_reason_key() is not None or self.loaded is None:
            return -1
        loaded, settings, engine = self.loaded, self.repair_settings(), self.ctx.engine
        name = self.source_name()

        def work(token, progress):
            return engine.repair(loaded, settings, token, progress)

        def done(result: RepairView):
            self.repair_progress.setVisible(False)
            if loaded is not self.loaded:
                return  # a different LUT was opened meanwhile
            self.repair, self.repair_error = result, None
            mean = result.change[0]
            self.ctx.log.success("health.log.repaired", detail=name, mean=f"{mean:.2f}")
            self._render_all()
            self.settings_changed()

        def failed(err: ErrorInfo):
            self.repair_progress.setVisible(False)
            self.repair_error = err
            self.ctx.log.error("health.log.repair_failed", detail=f"{name}: {err.summary()} {err.detail}".strip())
            self._render_repair()
            self._update_state()

        def cancelled():
            self.repair_progress.setVisible(False)
            self.ctx.log.warning("log.cancelled", detail=tr("health.job.repair", name=name))
            self._render_repair()
            self._update_state()

        self.repair_progress.setValue(0)
        self.repair_progress.setVisible(True)
        job = self.ctx.jobs.submit(
            "diagnose:repair", "health.job.repair", work, on_result=done, on_error=failed, on_cancel=cancelled,
            on_progress=lambda f, _k, _kw: self.repair_progress.setValue(int((f or 0) * 100)), title_kwargs={"name": name})
        self.repair_error = None
        self._render_repair()
        self._update_state()
        return job

    def discard_repair(self) -> None:
        if self.repair is None or self.ctx.jobs.is_busy("diagnose:repair"):
            return
        self.repair = None
        self.ctx.log.info("health.log.discarded", detail=self.source_name())
        self._render_all()
        self.settings_changed()

    def active_loaded(self) -> LoadedLutView | None:
        return self.repair.loaded if self.repair is not None else self.loaded

    def active_report(self) -> HealthView | None:
        return self.repair.after if self.repair is not None else self.report

    def source_name(self) -> str:
        if self.loaded is None:
            return ""
        return Path(self.loaded.path).name if self.loaded.path else self.loaded.title

    # ------------------------------------------------- LutWorkspace hooks
    def build_precondition(self):
        if not self.ctx.engine.available or self.ctx.engine.health_status():
            return ("health.reason.unavailable", {})
        if self.ctx.jobs.is_busy("diagnose:load"):
            return ("health.reason.checking", {})
        if self.loaded is None:
            return ("health.reason.no_lut", {})
        if self.ctx.jobs.is_busy("diagnose:repair"):
            return ("health.reason.repairing", {})
        return None

    def build_job(self, size: int, strength: float):
        loaded, engine = self.active_loaded(), self.ctx.engine

        def work(token, progress):
            return engine.loaded_to_lutdata(loaded, size, token, progress)

        return work

    def _ready_key(self) -> str:
        return "health.status.ready_repaired" if self.repair is not None else "health.status.ready_original"

    def _built(self, lut: LutData) -> None:
        self.lut = lut
        self.progress.setVisible(False)
        self._set_status(self._ready_key(), size=lut.size)
        self._refresh_preview()
        self._update_state()

    def suggested_name(self) -> str:
        stem = Path(self.loaded.path).stem if self.loaded and self.loaded.path else (self.loaded.title if self.loaded else "lut")
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem).strip("._") or "lut"
        return f"{safe}_{'repaired' if self.repair is not None else 'checked'}_{self.size()}.cube"

    def export_extra(self) -> dict:
        loaded = self.loaded
        if loaded is None:
            return {}
        extra: dict = {"source": {"file": Path(loaded.path).name if loaded.path else "", "origin": "file" if loaded.path else "page",
                                  "format": loaded.source_format, "grid": loaded.size, "title": loaded.title}}
        if self.repair is not None:
            mean, p99, peak = self.repair.change
            extra["repair"] = {"settings": dict(self.repair.settings),
                               "delta_e00": {"mean": mean, "p99": p99, "max": peak}}
        return extra

    def domain_text(self) -> str:
        if self.loaded is None:
            return ""
        return tr("health.domain", size=self.loaded.size, format=tr(f"health.format.{self.loaded.source_format}"),
                  target=self.size())

    # --------------------------------------------------------------- preview
    def _refresh_preview(self) -> None:
        self.domain_label.setText(self.domain_text())
        if self.preview.source is None:
            self.preview.set_note("preview.no_image")
            return
        lut = self.lut
        if lut is None:
            self.preview.set_note("preview.no_lut")
            self.preview.render(None)
            return
        if self.report is not None and self.report.kind.kind == "log_to_display":
            self.preview.set_note("health.preview.log")
        else:
            self.preview.set_note("health.preview.repaired" if self.repair is not None else "health.preview.original",
                                  size=lut.size)
        if self.repair is None or self.loaded is None:
            self.preview.render(lut)
        else:
            self._render_comparison(lut)

    def _render_comparison(self, repaired: LutData) -> None:
        """Before = original LUT on the image, after = repaired LUT on the image."""
        engine, source, original, size = self.ctx.engine, self.preview.source, self.loaded, repaired.size
        viewer = self.preview.viewer
        viewer.set_busy(True)

        def work(token, progress):
            before = engine.loaded_to_lutdata(original, size, token)
            return (to_qimage(engine.render_preview(source, before, token)),
                    to_qimage(engine.render_preview(source, repaired, token)))

        def done(images):
            viewer.set_images(images[0], images[1])

        def failed(err: ErrorInfo):
            viewer.set_images(self.preview._before_qimage, None)
            self.preview.set_note("preview.failed_note", reason=err.summary(), detail=err.detail)
            self.ctx.log.error("log.preview.failed", detail=f"{err.summary()} {err.detail}".strip())

        self.ctx.jobs.submit(f"{self.key}:preview", "job.preview", work, on_result=done, on_error=failed,
                             on_cancel=lambda: viewer.set_busy(False))

    # ----------------------------------------------------------------- cancel
    def cancel(self) -> None:
        self.ctx.jobs.cancel_channel("diagnose:load")
        self.ctx.jobs.cancel_channel("diagnose:repair")
        super().cancel()

    def shutdown(self) -> None:
        self._debounce.stop()

    # ------------------------------------------------------------- rendering
    def _retranslate(self) -> None:
        for i, key in enumerate(("health.tab.gray", "health.tab.hue", "health.tab.preview")):
            self.tabs.setTabText(i, tr(key))
        self._render_all()

    def _finding_selected(self, code: str) -> None:
        self._render_detail()

    def _rows(self, report: HealthView, before: HealthView | None) -> list[FindingRow]:
        rows = []
        for f in report.findings:
            name = tr(f"health.name.{f.code}")
            sev = tr(f"health.severity.{f.severity}")
            text = sentence(f.code, f.severity)
            value = format_value(f.code, f.value)
            old = before.finding(f.code) if before is not None else None
            if old is not None and (old.severity != "ok" or f.severity != "ok"):
                caption = tr("health.caption.repaired", name=name, before=tr(f"health.severity.{old.severity}"), after=sev)
                tooltip = f"{text}\n{tr('health.row.tooltip_before', value=format_value(f.code, old.value))}"
            elif f.severity != "ok":
                caption, tooltip = tr("health.caption", name=name, severity=sev), text
            else:
                caption, tooltip = "", f"{name}: {text}"
            rows.append(FindingRow(f.code, f.severity, text, value, caption, tooltip,
                                   tr("health.row.accessible", severity=sev, sentence=f"{name}. {text}", value=value)))
        return rows

    def _overall_text(self) -> str:
        report = self.active_report()
        if report is None:
            return tr("health.overall.checking") if self.ctx.jobs.is_busy("diagnose:load") else tr("health.overall.none")
        n = len(report.attention)
        if self.repair is not None:
            return tr("health.overall.repaired", after=n, before=len(self.report.attention))
        if report.overall == "ok":
            return tr("health.overall.ok")
        if report.overall == "info":
            return tr("health.overall.info", n=sum(1 for f in report.findings if f.severity == "info"))
        return tr(f"health.overall.{report.overall}", n=n)

    def _render_all(self) -> None:
        loaded, report = self.loaded, self.active_report()
        has = loaded is not None
        self.drop.setVisible(not has)
        self.source_bar.setVisible(has)
        if has:
            bar = self.source_bar
            fmt = tr(f"health.format.{loaded.source_format}")
            info = tr("health.source.file", format=fmt, size=loaded.size, title=loaded.title)
            if loaded.path:
                bar.path.set_path(loaded.path)
                bar.path.setVisible(True)
                bar.origin.setVisible(False)
                bar.info.setText(info)
            else:
                bar.path.setVisible(False)
                bar.origin.setText(tr("health.source.page", page=tr(f"nav.{self.page_key}") if self.page_key else "",
                                      size=loaded.size, title=loaded.title))
                bar.origin.setVisible(True)
                bar.info.setText("")
            bar.info.setVisible(bool(bar.info.text()))
            warnings = []
            for w in loaded.warnings:
                text = tr(f"health.warning.{w}")
                warnings.append(w if text == f"health.warning.{w}" else text)
            bar.warnings.setText("\n".join(warnings))
            bar.warnings.setVisible(bool(warnings))
        self.kind_card.show_report(self.report if self.repair is None else self.repair.after)
        self.overall.setText(self._overall_text())
        if report is None:
            self.findings.set_rows([])
        else:
            self.findings.set_rows(self._rows(report, self.report if self.repair is not None else None),
                                   select=self.findings.current_code() or None)
        self._render_detail()
        self._render_repair()
        self.domain_label.setText(self.domain_text())

    def _render_detail(self) -> None:
        """Explanation, charts and highlights for the selected finding."""
        report = self.active_report()
        if report is None:
            self.explanation.show_finding(None)
            self.gray_chart.set_data(None)
            self.hue_map.set_data(None)
            return
        code = self.findings.current_code()
        selected = report.finding(code) if code else None
        before = self.report.finding(code) if (code and self.repair is not None) else None
        self.explanation.show_finding(selected, before)
        grey_sel, grey_other = [], []
        for f in report.findings:
            if f.severity not in HEALTH_ATTENTION and not (f is selected and f.severity == "info"):
                continue
            (grey_sel if f is selected else grey_other).extend(f.grey_positions(report.size))
        reference = self.report.gray_axis if self.repair is not None else None
        self.gray_chart.set_data(report.gray_axis, reference=reference, flagged_selected=grey_sel, flagged_other=grey_other)
        base, marked = self.ctx.engine.hue_nodes(self.active_loaded(), selected.locations if selected is not None else ())
        self.hue_map.set_data(base, marked)

    def _render_repair(self) -> None:
        if self.repair_error is not None:
            err = self.repair_error
            self.repair_result.setText(tr("health.repair.failed", reason=err.summary(), detail=err.detail))
            self.repair_hint.setVisible(False)
        elif self.repair is not None:
            mean, p99, peak = self.repair.change
            self.repair_result.setText(tr("health.repair.result", mean=f"{mean:.2f}", p99=f"{p99:.2f}", max=f"{peak:.2f}"))
            self.repair_hint.setVisible(True)
        elif self.ctx.jobs.is_busy("diagnose:repair"):
            self.repair_result.setText(tr("health.repair.running"))
            self.repair_hint.setVisible(False)
        else:
            self.repair_result.setText(tr("health.repair.none"))
            self.repair_hint.setVisible(False)
        self._update_repair_controls()

    def _update_repair_controls(self) -> None:
        if not hasattr(self, "apply_button"):
            return
        reason = self.repair_reason_key()
        key, kwargs = reason if reason else (None, {})
        set_enabled_with_reason(self.apply_button, self.repair_reason, key, **kwargs)
        can_discard = self.repair is not None and not self.ctx.jobs.is_busy("diagnose:repair")
        self.discard_button.setEnabled(can_discard)
        self.discard_button.setToolTip("" if can_discard else tr("health.reason.no_repair"))
        busy = self.ctx.jobs.is_busy("diagnose:repair")
        for w in (self.smooth_slider, self.smooth_spin, self.opt_jumps, self.opt_round, self.opt_gray):
            w.setEnabled(not busy)

    def _update_state(self) -> None:
        super()._update_state()
        if hasattr(self, "apply_button"):
            if any(self.ctx.jobs.is_busy(f"{self.key}:{c}") for c in ("load", "repair")):
                self.cancel_button.setEnabled(True)
            self._update_repair_controls()
