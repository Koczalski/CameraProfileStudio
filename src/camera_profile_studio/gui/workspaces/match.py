"""Camera Match Studio.

Supported evidence (contract "Matching worker API"):

* measured corresponding patches from a CSV (chart users), and
* a same-scene image pair (two cameras, or RAW + camera JPEG) that the user
  explicitly confirms, either already pixel-aligned or registered by the
  engine and then visually checked by the user.

An arbitrary reference photo without correspondences is shown as an
unavailable mode with its reason; pixels are never paired by position for
unrelated scenes.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..backend import MATCH_FITS, MATCH_GRIDS, AlignmentInfo, ImageData, MatchOutcome, to_qimage
from ..i18n import T, tr
from ..jobs import ErrorInfo
from ..widgets.common import (
    AdvancedSection,
    DisabledReason,
    PathLabel,
    WrapChoice,
    WrapLabel,
    bind_combo,
    fill_combo,
    make_button,
    set_enabled_with_reason,
)
from ..widgets.preview import debounce
from .lut_base import LutWorkspace

MATCH_MAX_SIDE = 2048
RAW_SUFFIXES = {".nef", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".raf", ".rw2", ".pef", ".srw", ".dcr", ".kdc", ".iiq", ".3fr", ".fff"}


class MatchWorkspace(LutWorkspace):
    key = "match"
    icon_name = "image_multiple"
    build_title_key = "job.build_match_lut"
    #: Jobs of this page besides the shared LUT/preview/export ones; the page's
    #: Cancel button (Esc) must cover them too.
    MATCH_CHANNELS = ("match:source", "match:target", "match:align", "match:fit")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.model = None
        self.outcome: MatchOutcome | None = None
        self.source: ImageData | None = None
        self.target: ImageData | None = None
        self.alignment: AlignmentInfo | None = None
        self.csv_path = ""
        self._raw_keys: dict[str, tuple] = {}  # side -> RAW recipe the loaded image was decoded with
        self._report_key = "match.report.none"

        # --- evidence type
        mode_box = QGroupBox()
        T.bind(mode_box, "setTitle", "match.step.evidence")
        ml = QVBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.rb_csv = WrapChoice("match.mode.csv", radio=True, parent=mode_box)
        self.rb_pair = WrapChoice("match.mode.pair", radio=True, parent=mode_box)
        self.rb_ref = WrapChoice("match.mode.reference", radio=True, parent=mode_box)
        for i, rb in enumerate((self.rb_csv, self.rb_pair, self.rb_ref)):
            self.mode_group.addButton(rb.button, i)
            ml.addWidget(rb)
        self.rb_ref.setEnabled(False)
        self.ref_reason = WrapLabel("match.reference_unsupported", secondary=True, parent=mode_box)
        self.ref_reason.setContentsMargins(24, 0, 0, 0)
        T.bind(self.rb_ref, "setToolTip", "match.reference_unsupported")
        ml.addWidget(self.ref_reason)
        self.rb_pair.setChecked(True)
        self.side.add(mode_box)

        # --- CSV evidence
        self.csv_box = QGroupBox()
        T.bind(self.csv_box, "setTitle", "match.csv.group")
        cl = QFormLayout(self.csv_box)
        cl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        cl.addRow(WrapLabel("match.csv.hint", secondary=True, parent=self.csv_box))
        self.csv_label = PathLabel(self.csv_box)
        self.csv_button = make_button("match.csv.open", "document_table")
        cl.addRow(self.csv_label)
        cl.addRow(self.csv_button)
        self.side.add(self.csv_box)

        # --- pair evidence
        self.pair_box = QGroupBox()
        T.bind(self.pair_box, "setTitle", "match.pair.group")
        pl = QFormLayout(self.pair_box)
        pl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.src_label = PathLabel(self.pair_box)
        self.src_button = make_button("match.pair.source", "image")
        self.tgt_label = PathLabel(self.pair_box)
        self.tgt_button = make_button("match.pair.target", "image")
        pl.addRow(self.src_button)
        pl.addRow(self.src_label)
        pl.addRow(self.tgt_button)
        pl.addRow(self.tgt_label)
        self.same_scene = WrapChoice("match.pair.same_scene", parent=self.pair_box)
        pl.addRow(self.same_scene)
        self.prealigned = WrapChoice("match.pair.prealigned", parent=self.pair_box)
        pl.addRow(self.prealigned)
        self.register_button = make_button("match.pair.register", "target")
        pl.addRow(self.register_button)
        self.align_label = WrapLabel("", parent=self.pair_box)
        pl.addRow(self.align_label)
        self.align_ok = WrapChoice("match.pair.alignment_ok", parent=self.pair_box)
        self.align_ok.setEnabled(False)
        pl.addRow(self.align_ok)
        self.pair_reason = DisabledReason(self.pair_box)
        pl.addRow(self.pair_reason)
        self.side.add(self.pair_box)

        # --- evidence note (provenance)
        ev = QGroupBox()
        T.bind(ev, "setTitle", "match.evidence.group")
        el = QFormLayout(ev)
        el.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.evidence = QLineEdit(ev)
        T.bind(self.evidence, "setPlaceholderText", "match.evidence.placeholder")
        T.bind(self.evidence, "setAccessibleName", "match.evidence.group")
        el.addRow(self.evidence)
        self.side.add(ev)

        # --- fit
        fit_box = QGroupBox()
        T.bind(fit_box, "setTitle", "match.fit.group")
        fl = QFormLayout(fit_box)
        fl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.fit_combo = QComboBox(fit_box)
        fill_combo(self.fit_combo, [(f"match.fit.{f}", f) for f in MATCH_FITS])
        self.fit_combo.setCurrentIndex(self.fit_combo.findData("ccm_tone_lut"))
        bind_combo(self.fit_combo)
        fit_label = QLabel(fit_box)
        T.bind(fit_label, "setText", "match.fit.model")
        T.bind(self.fit_combo, "setAccessibleName", "match.fit.model")
        fit_label.setBuddy(self.fit_combo)
        fl.addRow(fit_label, self.fit_combo)
        self.fit_button = make_button("match.fit.run", "play")
        fl.addRow(self.fit_button)
        self.fit_reason_label = DisabledReason(fit_box)
        fl.addRow(self.fit_reason_label)
        self.side.add(fit_box)

        # --- advanced fit settings
        self.advanced = AdvancedSection("advanced.title")
        adv = QWidget()
        af = QFormLayout(adv)
        af.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        af.setContentsMargins(0, 0, 0, 0)
        self.reg = QDoubleSpinBox(adv)
        self.reg.setDecimals(4)
        self.reg.setRange(0.0001, 10.0)
        self.reg.setSingleStep(0.005)
        self.reg.setValue(0.01)
        self.grid = QComboBox(adv)
        for g in MATCH_GRIDS:
            self.grid.addItem(str(g), g)
        self.grid.setCurrentIndex(self.grid.findData(9))
        self.holdout = QDoubleSpinBox(adv)
        self.holdout.setRange(0.05, 0.9)
        self.holdout.setSingleStep(0.05)
        self.holdout.setValue(0.2)
        self.seed = QSpinBox(adv)
        self.seed.setRange(0, 2**31 - 1)
        self.seed.setValue(42)
        for w, key in ((self.reg, "match.adv.reg"), (self.grid, "match.adv.grid"), (self.holdout, "match.adv.holdout"),
                       (self.seed, "match.adv.seed")):
            lab = QLabel(adv)
            T.bind(lab, "setText", key)
            T.bind(w, "setAccessibleName", key)
            lab.setBuddy(w)
            af.addRow(lab, w)
        self.raw_wb = WrapChoice("match.adv.raw_camera_wb", parent=adv)
        self.raw_wb.setChecked(True)
        af.addRow(self.raw_wb)
        # Explicit RAW white balance, used only when camera WB is off.  The
        # engine requires it then; values are never guessed (0 = not set).
        wb_row = QWidget(adv)
        wl = QHBoxLayout(wb_row)
        wl.setContentsMargins(0, 0, 0, 0)
        self.raw_wb_spins: list[QDoubleSpinBox] = []
        for ch in ("r", "g", "b"):
            spin = QDoubleSpinBox(wb_row)
            spin.setDecimals(4)
            spin.setRange(0.0, 16.0)
            spin.setSingleStep(0.01)
            spin.setSpecialValueText(tr("conv.wb.unset"))
            spin.setValue(0.0)
            T.watch(spin, lambda s=spin: s.setSpecialValueText(tr("conv.wb.unset")))
            T.bind(spin, "setAccessibleName", f"match.adv.raw_wb_{ch}")
            wl.addWidget(spin)
            self.raw_wb_spins.append(spin)
        wb_label = QLabel(adv)
        T.bind(wb_label, "setText", "match.adv.raw_wb_multipliers")
        wb_label.setBuddy(self.raw_wb_spins[0])
        af.addRow(wb_label, wb_row)
        af.addRow(WrapLabel("match.adv.raw_wb_hint", secondary=True, parent=adv))
        self.raw_wb_reason = DisabledReason(adv)
        af.addRow(self.raw_wb_reason)
        self._raw_debounce = debounce(self, 500, self._raw_settings_changed)
        self.advanced.add_widget(adv)
        self.side.add(self.advanced)

        # --- report
        self.report_box = QGroupBox()
        T.bind(self.report_box, "setTitle", "match.report.group")
        rl = QVBoxLayout(self.report_box)
        self.report_label = WrapLabel("match.report.none", parent=self.report_box)
        rl.addWidget(self.report_label)
        self.side.add(self.report_box)
        self.side.add(self.make_output_group())
        self.side.add(self.make_status_group())
        self.side.add_stretch()
        self.preview.load_button.setVisible(True)
        self.preview.set_load_text_key("match.preview_image")

        # wiring
        self.mode_group.idToggled.connect(lambda _i, on: on and self._evidence_changed())
        self.csv_button.clicked.connect(self._pick_csv)
        self.src_button.clicked.connect(lambda: self._pick_image("source"))
        self.tgt_button.clicked.connect(lambda: self._pick_image("target"))
        self.same_scene.toggled.connect(lambda _b: self._evidence_changed())
        self.prealigned.toggled.connect(lambda _b: self._evidence_changed())
        self.align_ok.toggled.connect(lambda _b: self._update_fit_state())
        self.register_button.clicked.connect(self._register)
        self.fit_button.clicked.connect(self._fit)
        self.evidence.textChanged.connect(lambda _t: self._update_fit_state())
        self.raw_wb.toggled.connect(lambda _b: self._raw_settings_changed())
        for spin in self.raw_wb_spins:
            spin.valueChanged.connect(lambda _v: (self._sync_raw_wb(), self._raw_debounce.start()))
        for w in (self.fit_combo, self.grid):
            w.currentIndexChanged.connect(lambda _i: self._invalidate_model())
        for w in (self.reg, self.holdout):
            w.valueChanged.connect(lambda _v: self._invalidate_model())
        self.seed.valueChanged.connect(lambda _v: self._invalidate_model())
        T.watch(self, self._render_report)
        self._sync_raw_wb()
        self._evidence_changed()

    def primary_focus(self):
        return self.rb_pair

    # ---------------------------------------------------------- evidence
    def _is_pair(self) -> bool:
        return self.rb_pair.isChecked()

    def _evidence_changed(self) -> None:
        pair = self._is_pair()
        self.csv_box.setVisible(not pair)
        self.pair_box.setVisible(pair)
        self.alignment = None if not self.prealigned.isChecked() or not pair else self.alignment
        self.align_ok.setChecked(False)
        self.align_ok.setEnabled(False)
        self.align_label.setText("")
        self._invalidate_model()

    def _invalidate_model(self) -> None:
        self.model = None
        self.outcome = None
        self._report_key = "match.report.none"
        self._render_report()
        self.settings_changed()
        self._update_fit_state()

    def _pick_csv(self) -> None:
        path, _ = __import__("PySide6.QtWidgets", fromlist=["QFileDialog"]).QFileDialog.getOpenFileName(
            self, tr("match.csv.open"), self.ctx.last_dir("match"), tr("filter.csv"))
        if path:
            self.ctx.remember_dir("match", path)
            self.csv_path = path
            self.csv_label.set_path(path)
            self._invalidate_model()

    # --------------------------------------------------------- RAW develop
    @staticmethod
    def _is_raw(path: str) -> bool:
        return Path(path).suffix.lower() in RAW_SUFFIXES

    def raw_user_wb(self) -> tuple[float, float, float, float] | None:
        """Explicit multipliers as LibRaw's (R, G, B, G2), or None while incomplete."""
        r, g, b = (s.value() for s in self.raw_wb_spins)
        return (r, g, b, g) if r > 0 and g > 0 and b > 0 else None

    def raw_recipe_reason(self) -> str | None:
        if self.raw_wb.isChecked() or self.raw_user_wb() is not None:
            return None
        return "reason.match_raw_wb_missing"

    def _raw_key(self) -> tuple:
        return (True, None) if self.raw_wb.isChecked() else (False, self.raw_user_wb())

    def _raw_stale(self) -> bool:
        """A loaded RAW was developed with settings other than the current ones."""
        if not self._raw_keys:
            return False
        key = self._raw_key()
        return any(k != key for k in self._raw_keys.values())

    def _sync_raw_wb(self) -> None:
        manual = not self.raw_wb.isChecked()
        for spin in self.raw_wb_spins:
            spin.setEnabled(manual)
        self.raw_wb_reason.set_reason(self.raw_recipe_reason())

    def _raw_settings_changed(self) -> None:
        """Re-develop loaded RAW images so they always match the settings."""
        self._raw_debounce.stop()
        self._sync_raw_wb()
        if self.raw_recipe_reason() is None:
            key = self._raw_key()
            for which in ("source", "target"):
                data = self.source if which == "source" else self.target
                if data is not None and which in self._raw_keys and self._raw_keys[which] != key:
                    self.ctx.log.info("log.match.raw_reload", detail=data.path)
                    self.load_pair_image(which, data.path)
        self._update_fit_state()

    def _show_loaded_path(self, which: str) -> None:
        data = self.source if which == "source" else self.target
        (self.src_label if which == "source" else self.tgt_label).set_path(data.path if data is not None else None)

    def _pick_image(self, which: str) -> None:
        path = self.ctx.ask_image("match.pair.source" if which == "source" else "match.pair.target")
        if path:
            self.load_pair_image(which, path)

    def load_pair_image(self, which: str, path: str) -> int:
        engine = self.ctx.engine
        is_raw = self._is_raw(path)
        if is_raw and self.raw_recipe_reason() is not None:
            # Refuse before decoding: the engine would fail without WB values.
            err = ErrorInfo(self.raw_recipe_reason())
            self.ctx.log.error("log.image.failed", detail=f"{path}: {err.summary()}")
            self.ctx.show_error(err, "dialog.image_failed")
            return -1
        raw_key = self._raw_key()
        use_wb, user_wb = raw_key
        (self.src_label if which == "source" else self.tgt_label).set_path(path)

        def work(token, progress):
            recipe = engine.raw_recipe(use_camera_wb=use_wb, user_wb=user_wb) if is_raw else None
            data = engine.load_image(path, MATCH_MAX_SIDE, token, progress, raw_recipe=recipe)
            return data, to_qimage(data.array)

        def done(result):
            data, qimage = result
            if which == "source":
                self.source = data
                self.preview.set_source(data, qimage)
            else:
                self.target = data
                self._target_qimage = qimage
            if is_raw:
                self._raw_keys[which] = raw_key
            else:
                self._raw_keys.pop(which, None)
            self.alignment = None
            self.ctx.log.success("log.image.loaded", detail=path, w=data.width, h=data.height)
            for w in data.warnings:
                self.ctx.log.warning("log.image.warning", detail=w)
            self._evidence_changed()

        def failed(err: ErrorInfo):
            self._show_loaded_path(which)  # the previously loaded image (if any) stays in use
            self.ctx.log.error("log.image.failed", detail=f"{path}: {err.summary()} {err.detail}")
            self.ctx.show_error(err, "dialog.image_failed")

        def cancelled():
            self._show_loaded_path(which)
            self.ctx.log.warning("log.cancelled", detail=path)

        job = self.ctx.jobs.submit(f"match:{which}", "job.load_image", work, on_result=done, on_error=failed,
                                   on_cancel=cancelled, title_kwargs={"name": Path(path).name})
        self._update_state()
        return job

    def set_csv(self, path: str) -> None:
        self.rb_csv.setChecked(True)
        self.csv_path = path
        self.csv_label.set_path(path)
        self._invalidate_model()

    # ----------------------------------------------------- registration
    def register_reason(self) -> str | None:
        if not self.ctx.engine.available:
            return "reason.engine_unavailable"
        if self.ctx.jobs.is_busy("match:source") or self.ctx.jobs.is_busy("match:target"):
            return "reason.match_images_loading"
        if self.source is None or self.target is None:
            return "reason.match_need_both"
        if self._raw_stale():
            return "reason.match_raw_wb_missing"
        if not self.same_scene.isChecked():
            return "reason.match_confirm_scene"
        if self.prealigned.isChecked():
            return "reason.match_prealigned"
        if self.ctx.jobs.is_busy("match:align"):
            return "reason.match_registering"
        return None

    def _register(self) -> None:
        if self.register_reason() is not None:
            return
        engine, src, tgt = self.ctx.engine, self.source, self.target

        def work(token, progress):
            info = engine.align(src, tgt, token, progress)
            return info, to_qimage(info.source_aligned), to_qimage(info.target)

        def done(result):
            info, before, after = result
            self.alignment = info
            self.align_ok.setEnabled(True)
            self.align_ok.setChecked(False)
            self.align_label.setText(tr("match.pair.alignment_result", inliers=round(info.inlier_fraction * 100),
                                        valid=round(info.valid_fraction * 100)))
            # Let the user inspect registration: aligned source vs target.
            self.preview.show_images(before, after)
            self.preview.set_note("match.pair.inspect_note")
            self.ctx.log.success("log.match.registered", inliers=round(info.inlier_fraction * 100))
            self._update_fit_state()

        def failed(err: ErrorInfo):
            self.alignment = None
            self.align_label.setText(tr("match.pair.alignment_failed", reason=err.summary()))
            self.ctx.log.error("log.match.register_failed", detail=f"{err.summary()} {err.detail}")
            self._update_fit_state()

        def cancelled():
            self.alignment = None
            self.align_label.setText(tr("match.pair.alignment_cancelled"))
            self.ctx.log.warning("log.cancelled", detail=tr("job.register"))

        self.ctx.jobs.submit("match:align", "job.register", work, on_result=done, on_error=failed,
                             on_cancel=cancelled, on_done=lambda _s: self._update_state())
        self._update_state()

    # --------------------------------------------------------------- fit
    def fit_reason(self) -> tuple[str, dict] | None:
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        if self.ctx.jobs.is_busy("match:fit"):
            return ("reason.match_fitting", {})
        if not self.evidence.text().strip():
            return ("reason.match_needs_evidence", {})
        if not self._is_pair():
            if not self.csv_path:
                return ("reason.match_need_csv", {})
            return None
        if self.ctx.jobs.is_busy("match:source") or self.ctx.jobs.is_busy("match:target"):
            return ("reason.match_images_loading", {})
        if self.source is None or self.target is None:
            return ("reason.match_need_both", {})
        if self._raw_stale():
            return ("reason.match_raw_wb_missing", {})
        if not self.same_scene.isChecked():
            return ("reason.match_confirm_scene", {})
        if self.prealigned.isChecked():
            if (self.source.width, self.source.height) != (self.target.width, self.target.height):
                return ("reason.match_size_mismatch", {"a": f"{self.source.width}\u00d7{self.source.height}",
                                                        "b": f"{self.target.width}\u00d7{self.target.height}"})
            return None
        if self.alignment is None:
            return ("reason.match_need_registration", {})
        if not self.align_ok.isChecked():
            return ("reason.match_confirm_alignment", {})
        return None

    def _update_fit_state(self) -> None:
        if not hasattr(self, "fit_button"):
            return
        r = self.fit_reason()
        key, kwargs = r if r else (None, {})
        set_enabled_with_reason(self.fit_button, self.fit_reason_label, key, **kwargs)
        rr = self.register_reason()
        set_enabled_with_reason(self.register_button, self.pair_reason, rr)

    def _fit(self) -> None:
        if self.fit_reason() is not None:
            return
        engine = self.ctx.engine
        pair = self._is_pair()
        csv_path, evidence = self.csv_path, self.evidence.text().strip()
        src, tgt = self.source, self.target
        alignment = None if self.prealigned.isChecked() else self.alignment
        params = dict(fit=self.fit_combo.currentData(), regularization=self.reg.value(), grid=self.grid.currentData(),
                      holdout=self.holdout.value(), seed=self.seed.value(), size=self.size(), strength=self.strength())

        def work(token, progress):
            if pair:
                samples = engine.sample_pair(src, tgt, alignment, token, progress)
                # Engine records pairing conditions; append the user's note.
                types = engine._mod("types")
                samples = types.SamplePairs(samples.source_rgb, samples.target_rgb, samples.groups,
                                            f"{samples.evidence} | user: {evidence}")
            else:
                samples = engine.read_patch_csv(csv_path, evidence)
            return engine.fit(samples, token=token, progress=progress, **params)

        def done(outcome: MatchOutcome):
            self.outcome = outcome
            self.model = outcome.model
            self.lut = outcome.lut
            self._build_error = None
            self._set_status("lut.status.ready", size=outcome.lut.size, strength=round(outcome.lut.strength * 100))
            self.ctx.log.success("log.match.fitted", before=f"{outcome.before['mean']:.2f}",
                                 after=f"{outcome.after['mean']:.2f}", n=outcome.holdout_count)
            for w in outcome.warnings:
                self.ctx.log.warning("log.match.warning", detail=w)
            self._render_report()
            self._refresh_preview()
            self._update_state()

        def failed(err: ErrorInfo):
            self.model = None
            self.outcome = None
            self._render_report(err)
            self.ctx.log.error("log.match.fit_failed", detail=f"{err.summary()} {err.detail}")
            self._update_state()

        def cancelled():
            self._report_key = "match.report.cancelled"
            self._render_report()
            self.ctx.log.warning("log.cancelled", detail=tr("job.fit_match"))

        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.ctx.jobs.submit("match:fit", "job.fit_match", work, on_result=done, on_error=failed, on_cancel=cancelled,
                             on_progress=lambda f, _k, _kw: self.progress.setValue(int((f or 0) * 100)),
                             on_done=lambda _s: (self.progress.setVisible(False), self._update_state()))
        self._update_state()

    def _render_report(self, err: ErrorInfo | None = None) -> None:
        if not hasattr(self, "report_label"):
            return
        o = self.outcome
        if err is not None:
            self.report_label.setText(tr("match.report.failed", reason=err.summary(), detail=err.detail))
            return
        if o is None:
            self.report_label.setText(tr(self._report_key))
            return
        b, a = o.before, o.after
        lines = [
            tr("match.report.counts", train=o.train_count, holdout=o.holdout_count),
            tr("match.report.before", mean=f"{b['mean']:.2f}", median=f"{b['median']:.2f}", p95=f"{b['p95']:.2f}", max=f"{b['max']:.2f}"),
            tr("match.report.after", mean=f"{a['mean']:.2f}", median=f"{a['median']:.2f}", p95=f"{a['p95']:.2f}", max=f"{a['max']:.2f}"),
            tr("match.report.caveat"),
        ]
        lines += [f"\u26a0 {w}" for w in o.warnings]
        self.report_label.setText("\n".join(lines))

    # ----------------------------------------------------------- cancel
    def cancel(self) -> None:
        for channel in self.MATCH_CHANNELS:
            self.ctx.jobs.cancel_channel(channel)
        super().cancel()

    def _update_state(self) -> None:
        super()._update_state()
        if not hasattr(self, "fit_button"):
            return  # still constructing
        if any(self.ctx.jobs.is_busy(c) for c in self.MATCH_CHANNELS):
            self.cancel_button.setEnabled(True)
        self._update_fit_state()

    # ------------------------------------------------------- LUT hooks
    def build_precondition(self):
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        if self.model is None:
            return ("reason.match_no_model", {})
        return None

    def build_job(self, size: int, strength: float):
        engine, model = self.ctx.engine, self.model

        def work(token, progress):
            return engine.regenerate_match_lut(model, size, strength, token)

        return work

    def settings_changed(self) -> None:
        if hasattr(self, "status_label"):
            super().settings_changed()

    def suggested_name(self) -> str:
        stem = Path(self.source.path).stem if self.source else "match"
        return f"{stem}_match_EXPERIMENTAL_{self.size()}.cube"

    def domain_text(self) -> str:
        return tr("match.domain", size=self.size())



