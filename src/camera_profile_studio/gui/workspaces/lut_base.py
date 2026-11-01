"""Shared machinery for workspaces that produce one LUT (Converter, Look,
Match): size/strength controls, debounced background builds with stale-result
protection, preview of the *same* Lut3D and export of that Lut3D.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSlider,
    QSpinBox,
    QWidget,
)

from ..backend import EXPORT_FORMATS, FORMAT_FILES, LUT_SIZES, LutData
from ..context import setting_choice, setting_int
from ..i18n import T, tr
from ..jobs import ErrorInfo, JobState
from ..widgets.common import (
    SPACE_S,
    DisabledReason,
    WrapLabel,
    bind_combo,
    fill_combo,
    make_button,
    set_enabled_with_reason,
)
from ..widgets.preview import PreviewPanel, debounce
from .base import Workspace


def format_hint(engine, fmt: str, size: int) -> str:
    """One line describing what an export format produces for ``size``."""
    if fmt == "cube":
        return tr("lut.format_hint.cube", size=size)
    if not engine.available:
        return ""
    layout = engine.png_layout(fmt, size)
    return tr(f"lut.format_hint.{fmt}", w=layout["width"], h=layout["height"], n=layout["grid"],
              level=layout["level"], size=size)


def make_format_combo(ctx, parent) -> QComboBox:
    combo = QComboBox(parent)
    fill_combo(combo, [(f"lut.format.{f}", f) for f in EXPORT_FORMATS])
    bind_combo(combo)
    saved = setting_choice(ctx.settings, "lut/export_format", "cube", EXPORT_FORMATS)
    combo.setCurrentIndex(max(0, combo.findData(saved)))
    T.bind(combo, "setAccessibleName", "lut.format")
    combo.currentIndexChanged.connect(lambda _i: ctx.settings.setValue("lut/export_format", combo.currentData()))
    return combo


class LutWorkspace(Workspace):
    build_title_key = "job.build_lut"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.lut: LutData | None = None
        self._build_error: ErrorInfo | None = None
        self._build_job = -1
        self._export_job = -1
        # header commands
        self.export_button = make_button("action.export_lut", "arrow_export_ltr", primary=True)
        self.export_button.setShortcut(QKeySequence("Ctrl+E"))
        self.cancel_button = make_button("action.cancel", "dismiss_circle")
        self.cancel_button.setShortcut(QKeySequence("Esc"))
        self.add_command(self.cancel_button)
        self.add_command(self.export_button)
        # preview in centre
        self.preview = PreviewPanel(ctx, self.key, self.center)
        self.center_layout.addWidget(self.preview, 1)
        self.domain_label = WrapLabel("", parent=self.center)
        self.domain_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.center_layout.addWidget(self.domain_label)
        self.preview.image_loaded.connect(lambda _d: self._refresh_preview())
        self._debounce = debounce(self, 300, self.rebuild)
        self.export_button.clicked.connect(self.export)
        self.cancel_button.clicked.connect(self.cancel)
        ctx.jobs.job_finished.connect(self._job_finished)

    # ------------------------------------------------------- shared widgets
    def make_output_group(self) -> QGroupBox:
        box = QGroupBox()
        T.bind(box, "setTitle", "lut.group")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.size_combo = QComboBox(box)
        for n in LUT_SIZES:
            self.size_combo.addItem(f"{n} \u00d7 {n} \u00d7 {n}", n)
        default = setting_int(self.ctx.settings, "lut/default_size", 33, LUT_SIZES)
        self.size_combo.setCurrentIndex(max(0, self.size_combo.findData(default)))
        size_label = QLabel(box)
        T.bind(size_label, "setText", "lut.size")
        T.bind(self.size_combo, "setAccessibleName", "lut.size")
        size_label.setBuddy(self.size_combo)
        form.addRow(size_label, self.size_combo)
        # export file format (does not change the LUT itself, only how it is written)
        self.format_combo = make_format_combo(self.ctx, box)
        format_label = QLabel(box)
        T.bind(format_label, "setText", "lut.format")
        format_label.setBuddy(self.format_combo)
        form.addRow(format_label, self.format_combo)
        self.format_hint = WrapLabel("", secondary=True, parent=box)
        form.addRow(self.format_hint)
        self.format_combo.currentIndexChanged.connect(lambda _i: self._render_format_hint())
        self.size_combo.currentIndexChanged.connect(lambda _i: self._render_format_hint())
        T.watch(self.format_hint, self._render_format_hint)
        self._render_format_hint()
        # strength
        strength_row = QWidget(box)
        sl = QHBoxLayout(strength_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(SPACE_S)
        self.strength_slider = QSlider(Qt.Orientation.Horizontal, strength_row)
        self.strength_slider.setRange(0, 100)
        self.strength_slider.setValue(100)
        self.strength_slider.setPageStep(10)
        self.strength_spin = QSpinBox(strength_row)
        self.strength_spin.setRange(0, 100)
        self.strength_spin.setValue(100)
        self.strength_spin.setSuffix(" %")
        sl.addWidget(self.strength_slider, 1)
        sl.addWidget(self.strength_spin)
        strength_label = QLabel(box)
        T.bind(strength_label, "setText", "lut.strength")
        T.bind(self.strength_slider, "setAccessibleName", "lut.strength")
        T.bind(self.strength_spin, "setAccessibleName", "lut.strength_value")
        strength_label.setBuddy(self.strength_slider)
        form.addRow(strength_label, strength_row)
        self.strength_reason = DisabledReason(box)
        form.addRow(self.strength_reason)
        self.strength_slider.valueChanged.connect(self.strength_spin.setValue)
        self.strength_spin.valueChanged.connect(self.strength_slider.setValue)
        self.strength_spin.valueChanged.connect(lambda _v: self.settings_changed())
        self.size_combo.currentIndexChanged.connect(lambda _i: self.settings_changed())
        return box

    def make_status_group(self) -> QGroupBox:
        box = QGroupBox()
        T.bind(box, "setTitle", "lut.status_group")
        lay = QFormLayout(box)
        lay.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.status_label = WrapLabel("", parent=box)
        self.progress = QProgressBar(box)
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        T.bind(self.progress, "setAccessibleName", "lut.progress")
        self.export_reason = DisabledReason(box)
        self.export_button2 = make_button("action.export_lut", "arrow_export_ltr")
        self.export_button2.clicked.connect(self.export)
        lay.addRow(self.status_label)
        lay.addRow(self.progress)
        lay.addRow(self.export_button2)
        lay.addRow(self.export_reason)
        self._status_key, self._status_kwargs = "lut.status.idle", {}
        T.watch(self, self._render_status)
        return box

    # --------------------------------------------------------------- hooks
    def build_precondition(self) -> tuple[str, dict] | None:
        """Return (reason_key, kwargs) if a LUT cannot be built now."""
        return None

    def build_job(self, size: int, strength: float):
        """Return fn(token, progress) -> LutData for the current settings."""
        raise NotImplementedError

    def strength_precondition(self) -> str | None:
        """Reason key if strength must stay 100 % (unequal spaces)."""
        return None

    def suggested_name(self) -> str:
        return "camera_profile_studio.cube"

    def export_format(self) -> str:
        combo = getattr(self, "format_combo", None)
        return combo.currentData() if combo is not None else "cube"

    def _render_format_hint(self) -> None:
        hint = getattr(self, "format_hint", None)
        if hint is not None:
            hint.setText(format_hint(self.ctx.engine, self.export_format(), self.size()))

    def suggested_file(self) -> str:
        """Suggested export file name for the selected format."""
        tag, ext = FORMAT_FILES[self.export_format()]
        return Path(self.suggested_name()).stem + tag + ext

    def domain_text(self) -> str:
        return ""

    # ------------------------------------------------------------- build
    def size(self) -> int:
        return int(self.size_combo.currentData())

    def strength(self) -> float:
        return self.strength_spin.value() / 100.0

    def settings_changed(self) -> None:
        """Any setting changed: invalidate the LUT and schedule a rebuild."""
        self.lut = None
        self._build_error = None
        reason = self.strength_precondition()
        self.strength_slider.setEnabled(reason is None)
        self.strength_spin.setEnabled(reason is None)
        self.strength_reason.set_reason(reason)
        if reason is not None and self.strength_spin.value() != 100:
            self.strength_spin.blockSignals(True)
            self.strength_spin.setValue(100)
            self.strength_slider.setValue(100)
            self.strength_spin.blockSignals(False)
        # Invalidate (not just cancel): a success already queued from the old
        # request must not restore the old LUT or re-enable export.
        self.ctx.jobs.invalidate(f"{self.key}:lut")
        self.ctx.jobs.invalidate(f"{self.key}:preview")
        self._debounce.start()
        self._update_state()

    def rebuild(self) -> None:
        pre = self.build_precondition()
        if pre is not None:
            self.ctx.jobs.invalidate(f"{self.key}:lut")
            self._set_status(pre[0], **pre[1])
            self._update_state()
            self._refresh_preview()
            return
        try:
            fn = self.build_job(self.size(), self.strength())
        except Exception as exc:  # noqa: BLE001 - precondition from engine adapter
            self._build_error = self.ctx.engine.map_error(exc)
            self._set_status("lut.status.failed", reason=self._build_error.summary(), detail=self._build_error.detail)
            self._update_state()
            return
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_status("lut.status.building", size=self.size())
        self._build_job = self.ctx.jobs.submit(
            f"{self.key}:lut", self.build_title_key, fn,
            on_result=self._built, on_error=self._build_failed, on_cancel=self._build_cancelled,
            on_progress=lambda f, _k, _kw: self.progress.setValue(int((f or 0) * 100)),
            title_kwargs={"size": self.size()},
        )
        self._update_state()

    def _built(self, lut: LutData) -> None:
        self.lut = lut
        self.progress.setVisible(False)
        self._set_status("lut.status.ready", size=lut.size, strength=round(lut.strength * 100))
        self.ctx.log.success("log.lut.built", detail=lut.title, size=lut.size, inp=lut.input_kind, out=lut.output_kind)
        self._refresh_preview()
        self._update_state()

    def _build_failed(self, err: ErrorInfo) -> None:
        self.lut = None
        self._build_error = err
        self.progress.setVisible(False)
        self._set_status("lut.status.failed", reason=err.summary(), detail=err.detail)
        self.ctx.log.error("log.lut.failed", detail=f"{err.summary()} {err.detail}".strip())
        self._refresh_preview()
        self._update_state()

    def _build_cancelled(self) -> None:
        self.progress.setVisible(False)
        self._set_status("lut.status.cancelled")
        self.ctx.log.warning("log.cancelled", detail=tr(self.build_title_key, size=self.size()))
        self._update_state()

    def cancel(self) -> None:
        self._debounce.stop()
        self.ctx.jobs.invalidate(f"{self.key}:lut")
        self.ctx.jobs.invalidate(f"{self.key}:preview")
        self.ctx.jobs.invalidate(f"{self.key}:image")
        # export publishes files: cancel cooperatively, report the real outcome
        self.ctx.jobs.cancel_channel(f"{self.key}:export")
        if self.lut is None:
            self._set_status("lut.status.cancelled")
        self.progress.setVisible(False)
        self.preview.viewer.set_busy(False)
        self._update_state()

    def _job_finished(self, _job_id: int, _state: str) -> None:
        self._update_state()

    # ------------------------------------------------------------ preview
    def _refresh_preview(self) -> None:
        lut = self.lut
        self.domain_label.setText(self.domain_text())
        if self.preview.source is None:
            self.preview.set_note("preview.no_image")
            return
        if lut is None:
            self.preview.set_note("preview.no_lut")
            self.preview.render(None)
            return
        reason = lut.preview_reason()
        if reason is not None:
            key, kw = reason
            self.preview.set_note(key, kind=tr(f"space.{kw['kind']}"))
            self.preview.render(None)
            return
        warnings = self.preview.source.warnings
        if warnings:
            self.preview.set_note("preview.same_lut_warn", warnings="; ".join(warnings))
        else:
            self.preview.set_note("preview.same_lut", size=lut.size, strength=round(lut.strength * 100))
        self.preview.render(lut)

    # ------------------------------------------------------------- export
    def export(self) -> None:
        lut = self.lut
        if lut is None or self.ctx.jobs.is_busy(f"{self.key}:lut") or self.ctx.jobs.is_busy(f"{self.key}:export"):
            return
        current = self.export_format()
        suggested = self.suggested_file()
        # Every format is offered in the dialog's file-type list; the type
        # chosen there decides the format (and updates the selector).
        path, fmt = self.ctx.ask_save_typed("dialog.export_lut", suggested,
                                            [(f, f"filter.fmt.{f}") for f in EXPORT_FORMATS], current, "export")
        if not path:
            return
        path = self.resolve_export_path(path, fmt, current, suggested)
        if fmt != current:
            self.format_combo.setCurrentIndex(self.format_combo.findData(fmt))
        target = Path(path)
        companion = target.with_suffix(".json")
        existing = [p for p in (target, companion) if p.exists()]
        overwrite = False
        if existing:
            # The native save dialog only confirms the LUT file; also confirm the
            # metadata companion explicitly.
            if not self.ctx.confirm("dialog.overwrite_title", "dialog.overwrite_text",
                                    files="\n".join(str(p) for p in existing)):
                self.ctx.log.info("log.export.skipped", detail=path)
                return
            overwrite = True
        self.export_to(path, overwrite, fmt)

    @staticmethod
    def resolve_export_path(path: str, fmt: str, previous: str, suggested: str) -> str:
        """Make ``path`` match the chosen format: fix a .cube/.png extension
        that belongs to another format and, if the user kept the suggested
        name, swap its format tag (``_hald`` / ``_strip`` ...) too."""
        tag, ext = FORMAT_FILES[fmt]
        p = Path(path)
        suffix = p.suffix.lower()
        stem = p.stem if suffix in (".cube", ".png") else p.name
        old_tag = FORMAT_FILES[previous][0]
        if fmt != previous and stem == Path(suggested).stem:
            stem = (stem[: -len(old_tag)] if old_tag and stem.endswith(old_tag) else stem) + tag
        return str(p.with_name(stem + ext))

    def export_to(self, path: str, overwrite: bool, fmt: str | None = None) -> int:
        """Export the current LUT (exactly the previewed Lut3D) to ``path``.

        ``fmt`` defaults to .cube for a .cube path, else the selected format."""
        lut = self.lut
        if lut is None:
            return -1
        target = Path(path)
        engine = self.ctx.engine
        if fmt is None:
            fmt = "cube" if target.suffix.lower() == ".cube" else self.export_format()

        def work(token, progress):
            return engine.export(lut, path, overwrite, token, fmt)

        def done(info):
            self.ctx.log.success("log.export.done", detail=f"{info.cube_path} | {info.metadata_path} | sha256 {info.sha256}",
                                 n=info.samples)
            for w in info.warnings:
                self.ctx.log.warning("log.export.warning", detail=w)
            self._set_status("lut.status.exported", path=info.cube_path)
            self.ctx.status_message.emit(tr("lut.status.exported", path=info.cube_path))

        def failed(err: ErrorInfo):
            self.ctx.log.error("log.export.failed", detail=f"{path}: {err.summary()} {err.detail}")
            self.ctx.show_error(err, "dialog.export_failed")

        self._export_job = self.ctx.jobs.submit(f"{self.key}:export", "job.export", work, on_result=done,
                                                on_error=failed, side_effects=True,
                                                title_kwargs={"name": target.name},
                                                on_done=lambda _s: self._update_state())
        self._update_state()
        return self._export_job

    # ------------------------------------------------------------- state
    def _set_status(self, key: str, **kwargs) -> None:
        self._status_key, self._status_kwargs = key, kwargs
        self._render_status()

    def _render_status(self) -> None:
        text = tr(self._status_key, **self._status_kwargs)
        self.status_label.setText(text)
        self.status_label.setAccessibleName(text)

    def export_reason_key(self) -> tuple[str, dict] | None:
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        pre = self.build_precondition()
        if pre is not None:
            return pre
        if self.ctx.jobs.is_busy(f"{self.key}:lut") or self._debounce.isActive():
            return ("reason.lut_building", {})
        if self.ctx.jobs.is_busy(f"{self.key}:export"):
            return ("reason.exporting", {})
        if self._build_error is not None:
            return ("reason.lut_failed", {"reason": self._build_error.summary()})
        if self.lut is None:
            return ("reason.no_lut", {})
        return None

    def _update_state(self) -> None:
        reason = self.export_reason_key()
        key, kwargs = reason if reason else (None, {})
        set_enabled_with_reason(self.export_button, self.export_reason, key, **kwargs)
        self.export_button2.setEnabled(reason is None)
        self.export_button2.setToolTip(self.export_button.toolTip())
        busy = any(self.ctx.jobs.is_busy(f"{self.key}:{c}") for c in ("lut", "preview", "export", "image"))
        self.cancel_button.setEnabled(busy)
        self.domain_label.setText(self.domain_text())




