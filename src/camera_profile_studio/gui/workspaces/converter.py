"""Technical Converter: profile -> direction/output space -> LUT size ->
(advanced) -> export.  The order of the right-hand groups follows the order
of the task (spec 04 ﾂｧ6 "Converter")."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..backend import GAMUTS, INTENTS
from ..context import ProfileEntry
from ..i18n import T, tr
from ..widgets.common import (
    AdvancedSection,
    DisabledReason,
    PathLabel,
    WrapLabel,
    bind_combo,
    fill_combo,
    make_button,
)
from .lut_base import LutWorkspace

ICC_DIRECTIONS = (
    ("conv.dir.to_srgb", "to_srgb"),
    ("conv.dir.to_linear_srgb", "to_linear_srgb"),
    ("conv.dir.from_srgb", "from_srgb"),
    ("conv.dir.to_icc", "to_icc"),
)
DCP_DIRECTIONS = (("conv.dir.dcp_to_srgb", "to_srgb"),)


class ProfilePicker(QGroupBox):
    """Combo over the shared ProfilesModel + path + open button."""

    def __init__(self, ctx, title_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        T.bind(self, "setTitle", title_key)
        lay = QVBoxLayout(self)
        self.combo = QComboBox(self)
        self.combo.setModel(ctx.profiles)
        self.combo.setModelColumn(0)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(12)
        T.bind(self.combo, "setAccessibleName", title_key)
        T.bind(self.combo, "setPlaceholderText", "picker.placeholder")
        self.path = PathLabel(self)
        self.open = make_button("action.open_profiles", "folder_open")
        lay.addWidget(self.combo)
        lay.addWidget(self.path)
        lay.addWidget(self.open)
        self.open.clicked.connect(self._browse)
        self.combo.currentIndexChanged.connect(self._index_changed)
        ctx.current_profile_changed.connect(self.sync)
        self.sync(ctx.current_profile)

    def _browse(self) -> None:
        files = self.ctx.ask_profiles()
        if files:
            started = self.ctx.open_profiles(files)
            if started:
                row = self.ctx.profiles.row_of(started[-1])
                self.ctx.set_current_profile(self.ctx.profiles.entry(row))

    def _index_changed(self, row: int) -> None:
        entry = self.ctx.profiles.entry(row)
        if entry is not self.ctx.current_profile:
            self.ctx.set_current_profile(entry)

    def sync(self, entry: ProfileEntry | None) -> None:
        row = self.ctx.profiles.row_of(entry.path) if entry else -1
        if self.combo.currentIndex() != row:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(row)
            self.combo.blockSignals(False)
        self.path.set_path(entry.path if entry else None)

    def entry(self) -> ProfileEntry | None:
        return self.ctx.profiles.entry(self.combo.currentIndex())


class ConverterWorkspace(LutWorkspace):
    key = "converter"
    icon_name = "arrow_swap"
    build_title_key = "job.build_technical"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        # 1 profile
        self.picker = ProfilePicker(ctx, "conv.step.profile")
        self.side.add(self.picker)
        # 2 conversion / spaces
        conv = QGroupBox()
        T.bind(conv, "setTitle", "conv.step.conversion")
        form = QFormLayout(conv)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.direction = QComboBox(conv)
        bind_combo(self.direction)
        dl = QLabel(conv)
        T.bind(dl, "setText", "conv.direction")
        T.bind(self.direction, "setAccessibleName", "conv.direction")
        dl.setBuddy(self.direction)
        form.addRow(dl, self.direction)
        self.spaces_label = WrapLabel("", parent=conv)
        form.addRow(self.spaces_label)
        self.side.add(conv)
        # output ICC profile (ICC -> ICC); never changes the input selection
        self.target_group = QGroupBox()
        T.bind(self.target_group, "setTitle", "conv.target.group")
        tl = QVBoxLayout(self.target_group)
        self.target_combo = QComboBox(self.target_group)
        self.target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target_combo.setMinimumContentsLength(12)
        T.bind(self.target_combo, "setAccessibleName", "conv.target.group")
        T.bind(self.target_combo, "setPlaceholderText", "conv.target.placeholder")
        self.target_path = PathLabel(self.target_group)
        self.target_open = make_button("conv.target.open", "folder_open")
        self.target_reason_label = DisabledReason(self.target_group)
        tl.addWidget(self.target_combo)
        tl.addWidget(self.target_path)
        tl.addWidget(self.target_open)
        tl.addWidget(self.target_reason_label)
        self.target_group.setVisible(False)
        self.side.add(self.target_group)
        self._pending_target = ""
        self.target_open.clicked.connect(self._open_target)
        self.target_combo.currentIndexChanged.connect(lambda _i: self._target_changed())
        ctx.profiles.rowsInserted.connect(lambda *_: self._refresh_targets())
        ctx.profiles.rowsRemoved.connect(lambda *_: self._refresh_targets())
        ctx.profile_ready.connect(self._target_ready)
        # DCP white balance (required for camera-linear input)
        self.wb_group = QGroupBox()
        T.bind(self.wb_group, "setTitle", "conv.wb.group")
        wform = QFormLayout(self.wb_group)
        wform.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.wb_hint = WrapLabel("conv.wb.hint", secondary=True, parent=self.wb_group)
        wform.addRow(self.wb_hint)
        nrow = QWidget(self.wb_group)
        nl = QHBoxLayout(nrow)
        nl.setContentsMargins(0, 0, 0, 0)
        self.neutral = []
        for ch in ("R", "G", "B"):
            spin = QDoubleSpinBox(nrow)
            spin.setDecimals(4)
            spin.setRange(0.0, 10.0)
            spin.setSingleStep(0.01)
            spin.setSpecialValueText(tr("conv.wb.unset"))
            spin.setValue(0.0)
            T.bind(spin, "setAccessibleName", f"conv.wb.neutral_{ch.lower()}")
            T.watch(spin, lambda s=spin: s.setSpecialValueText(tr("conv.wb.unset")))
            nl.addWidget(spin)
            self.neutral.append(spin)
            spin.valueChanged.connect(lambda _v: self.settings_changed())
        nlab = QLabel(self.wb_group)
        T.bind(nlab, "setText", "conv.wb.neutral")
        nlab.setBuddy(self.neutral[0])
        wform.addRow(nlab, nrow)
        self.kelvin = QDoubleSpinBox(self.wb_group)
        self.kelvin.setRange(0.0, 50000.0)
        self.kelvin.setDecimals(0)
        self.kelvin.setSingleStep(100)
        self.kelvin.setSuffix(" K")
        self.kelvin.setSpecialValueText(tr("conv.wb.unset"))
        T.watch(self.kelvin, lambda: self.kelvin.setSpecialValueText(tr("conv.wb.unset")))
        klab = QLabel(self.wb_group)
        T.bind(klab, "setText", "conv.wb.kelvin")
        T.bind(self.kelvin, "setAccessibleName", "conv.wb.kelvin")
        klab.setBuddy(self.kelvin)
        wform.addRow(klab, self.kelvin)
        self.kelvin.valueChanged.connect(lambda _v: self.settings_changed())
        self.side.add(self.wb_group)
        # 3 LUT
        self.side.add(self.make_output_group())
        # Advanced
        self.advanced = AdvancedSection("advanced.title")
        adv = QWidget()
        aform = QFormLayout(adv)
        aform.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        aform.setContentsMargins(0, 0, 0, 0)
        self.intent = QComboBox(adv)
        fill_combo(self.intent, [(f"intent.{i}", i) for i in INTENTS])
        self.intent.setCurrentIndex(self.intent.findData("relative"))
        bind_combo(self.intent)
        il = QLabel(adv)
        T.bind(il, "setText", "conv.intent")
        T.bind(self.intent, "setAccessibleName", "conv.intent")
        il.setBuddy(self.intent)
        aform.addRow(il, self.intent)
        self.bpc = QCheckBox(adv)
        T.bind(self.bpc, "setText", "conv.bpc")
        aform.addRow(self.bpc)
        self.gamut = QComboBox(adv)
        fill_combo(self.gamut, [(f"gamut.{g}", g) for g in GAMUTS])
        bind_combo(self.gamut)
        gl = QLabel(adv)
        T.bind(gl, "setText", "conv.gamut")
        T.bind(self.gamut, "setAccessibleName", "conv.gamut")
        gl.setBuddy(self.gamut)
        aform.addRow(gl, self.gamut)
        self.icc_only = WrapLabel("conv.icc_only_hint", secondary=True, parent=adv)
        aform.addRow(self.icc_only)
        self.hsm = QCheckBox(adv)
        self.lt = QCheckBox(adv)
        self.tc = QCheckBox(adv)
        for box, key in ((self.hsm, "conv.dcp.hsm"), (self.lt, "conv.dcp.lt"), (self.tc, "conv.dcp.tc")):
            box.setChecked(True)
            T.bind(box, "setText", key)
            aform.addRow(box)
            box.toggled.connect(lambda _b: self.settings_changed())
        self.advanced.add_widget(adv)
        self.side.add(self.advanced)
        self.side.add(self.make_status_group())
        self.side.add_stretch()

        self.direction.currentIndexChanged.connect(lambda _i: self.settings_changed())
        self.intent.currentIndexChanged.connect(lambda _i: self.settings_changed())
        self.gamut.currentIndexChanged.connect(lambda _i: self.settings_changed())
        self.bpc.toggled.connect(lambda _b: self.settings_changed())
        ctx.current_profile_changed.connect(self._profile_changed)
        T.watch(self, self._render_spaces)
        self._profile_changed(ctx.current_profile)

    def primary_focus(self):
        return self.picker.combo

    # ----------------------------------------------------------- profile
    def _entry(self) -> ProfileEntry | None:
        return self.ctx.current_profile

    def _profile_changed(self, entry: ProfileEntry | None) -> None:
        info = entry.info if entry and entry.state == "ready" else None
        is_dcp = bool(info and info.kind == "dcp")
        self.direction.blockSignals(True)
        fill_combo(self.direction, DCP_DIRECTIONS if is_dcp else ICC_DIRECTIONS)
        self._apply_direction_caps(info)
        self.direction.blockSignals(False)
        self._refresh_targets()
        self.wb_group.setVisible(is_dcp)
        for w in (self.hsm, self.lt, self.tc):
            w.setVisible(is_dcp)
        self.icc_only.setVisible(is_dcp)
        self.intent.setEnabled(not is_dcp)
        self.bpc.setEnabled(not is_dcp)
        if info is not None and is_dcp:
            comps = info.look_components
            for box, name in ((self.hsm, "hue_sat_map"), (self.lt, "look_table"), (self.tc, "tone_curve")):
                present = comps.get(name) is not None and comps[name].status == "available"
                box.setEnabled(present)
                box.setToolTip("" if present else tr("conv.dcp.absent"))
        self.settings_changed()

    def _direction_reason(self, info, direction: str) -> str:
        """Reason text if ``direction`` is impossible for this ICC, else ''."""
        if info is None or info.kind != "icc":
            return ""
        needs_in = direction in ("to_srgb", "to_linear_srgb", "to_icc")
        cap = info.technical_input if needs_in else info.technical_output
        if cap is not None and cap.status == "unsupported":
            return cap.reason or tr("cap.unsupported")
        return ""

    def _apply_direction_caps(self, info) -> None:
        model = self.direction.model()
        first_ok = -1
        for i in range(self.direction.count()):
            reason = self._direction_reason(info, self.direction.itemData(i))
            item = model.item(i) if hasattr(model, "item") else None
            if item is not None:
                item.setEnabled(not reason)
            self.direction.setItemData(i, reason or None, Qt.ItemDataRole.ToolTipRole)
            if not reason and first_ok < 0:
                first_ok = i
        if self._direction_reason(info, self.direction.currentData() or "") and first_ok >= 0:
            self.direction.setCurrentIndex(first_ok)

    # ------------------------------------------------------------ target
    def _target_entry(self) -> ProfileEntry | None:
        path = self.target_combo.currentData()
        row = self.ctx.profiles.row_of(path) if path else -1
        return self.ctx.profiles.entry(row)

    def _refresh_targets(self) -> None:
        current_in = self._entry()
        keep = self._pending_target or self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for e in self.ctx.profiles.entries:
            if e is current_in or Path(e.path).suffix.lower() not in (".icc", ".icm"):
                continue
            self.target_combo.addItem(e.name, e.path)
            self.target_combo.setItemData(self.target_combo.count() - 1, e.path, Qt.ItemDataRole.ToolTipRole)
        idx = self.target_combo.findData(keep) if keep else -1
        self.target_combo.setCurrentIndex(idx)
        self.target_combo.blockSignals(False)
        self._target_changed()

    def _open_target(self) -> None:
        files = self.ctx.ask_profiles()
        files = [f for f in files if Path(f).suffix.lower() in (".icc", ".icm")]
        if files:
            self._pending_target = str(Path(files[0]))
            self.ctx.open_profiles(files[:1], make_current=False)
            self._refresh_targets()

    def _target_ready(self, entry: ProfileEntry) -> None:
        if self._pending_target and str(Path(entry.path)) == self._pending_target:
            self._pending_target = ""
        self._refresh_targets()

    def target_reason(self) -> str | None:
        e = self._target_entry()
        if e is None:
            return "reason.need_target_profile"
        if e.state == "loading":
            return "reason.target_loading"
        if e.state == "failed" or e.info is None:
            return "reason.target_failed"
        out = e.info.technical_output
        if out is not None and out.status == "unsupported":
            return "reason.target_not_output"
        return None

    def _target_changed(self) -> None:
        e = self._target_entry()
        self.target_path.set_path(e.path if e else None)
        reason = self.target_reason()
        if reason == "reason.target_not_output" and e and e.info and e.info.technical_output:
            self.target_reason_label.set_reason("reason.target_not_output_detail", reason=e.info.technical_output.reason)
        else:
            self.target_reason_label.set_reason(reason)
        if hasattr(self, "status_label") and self.direction.currentData() == "to_icc":
            self.settings_changed()

    def _spaces(self) -> tuple[tuple[str, str], tuple[str, str]]:
        """Full (kind, profile path) for input and output, as SpaceSpec compares."""
        entry = self._entry()
        info = entry.info if entry and entry.state == "ready" else None
        src = str(Path(entry.path)) if entry else ""
        d = self.direction.currentData() or "to_srgb"
        if info is not None and info.kind == "dcp":
            return ("camera_linear", src), ("srgb", "")
        if d == "from_srgb":
            return ("srgb", ""), ("icc_rgb", src)
        if d == "to_icc":
            target = self._target_entry()
            return ("icc_rgb", src), ("icc_rgb", str(Path(target.path)) if target else "")
        return ("icc_rgb", src), ("linear_srgb" if d == "to_linear_srgb" else "srgb", "")

    @staticmethod
    def _space_text(space: tuple[str, str]) -> str:
        kind, path = space
        text = tr(f"space.{kind}")
        return f"{text} \u2014 {Path(path).name}" if path else text

    def _render_spaces(self) -> None:
        inp, out = self._spaces()
        self.spaces_label.setText(tr("conv.spaces", inp=self._space_text(inp), out=self._space_text(out)))
        self.target_group.setVisible(self.direction.currentData() == "to_icc")
    # ------------------------------------------------------------ hooks
    def settings(self) -> dict:
        neutral = tuple(s.value() for s in self.neutral)
        return {
            "direction": self.direction.currentData() or "to_srgb",
            "target": self.target_combo.currentData() if self.direction.currentData() == "to_icc" else None,
            "intent": self.intent.currentData(),
            "bpc": self.bpc.isChecked(),
            "gamut": self.gamut.currentData(),
            "neutral": neutral if all(v > 0 for v in neutral) else None,
            "kelvin": self.kelvin.value() or None,
            "hsm": self.hsm.isChecked() and self.hsm.isEnabled(),
            "lt": self.lt.isChecked() and self.lt.isEnabled(),
            "tc": self.tc.isChecked() and self.tc.isEnabled(),
        }

    def settings_changed(self) -> None:
        if hasattr(self, "spaces_label"):
            self._render_spaces()
        if hasattr(self, "status_label"):
            super().settings_changed()

    def build_precondition(self):
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        entry = self._entry()
        if entry is None:
            return ("reason.no_profile", {})
        if entry.state == "loading":
            return ("reason.profile_loading", {})
        if entry.state == "failed" or entry.info is None:
            return ("reason.profile_failed", {})
        info = entry.info
        if info.technical.status == "unsupported":
            return ("reason.technical_unsupported_detail", {"reason": info.technical.reason or tr("cap.unsupported")})
        direction = self.direction.currentData() or "to_srgb"
        dreason = self._direction_reason(info, direction)
        if dreason:
            return ("reason.direction_unsupported", {"reason": dreason})
        if info.kind == "icc" and direction == "to_icc":
            treason = self.target_reason()
            if treason is not None:
                e = self._target_entry()
                if treason == "reason.target_not_output" and e and e.info and e.info.technical_output:
                    return ("reason.target_not_output_detail", {"reason": e.info.technical_output.reason})
                return (treason, {})
        if info.kind == "dcp":
            s = self.settings()
            if s["neutral"] is None:
                return ("reason.dcp_needs_neutral", {})
            if info.dual_illuminant and not s["kelvin"]:
                return ("reason.dcp_needs_kelvin", {})
        return None

    def strength_precondition(self):
        inp, out = self._spaces()  # kind AND profile path, like SpaceSpec equality
        return None if inp == out else "reason.strength_needs_equal_spaces"

    def build_job(self, size: int, strength: float):
        entry = self._entry()
        engine = self.ctx.engine
        request = engine.technical_request(entry.info, self.settings())

        def work(token, progress):
            return engine.convert(request, size, strength, token, progress)

        return work

    def suggested_name(self) -> str:
        entry = self._entry()
        stem = Path(entry.path).stem if entry else "profile"
        inp, out = self._spaces()
        out_name = Path(out[1]).stem if out[1] else out[0]
        return f"{stem}_{inp[0]}_to_{out_name}_{self.size()}.cube"

    def domain_text(self) -> str:
        inp, out = self._spaces()
        gamut = self.gamut.currentData() or "clip"
        text = tr("conv.domain", inp=self._space_text(inp), out=self._space_text(out), gamut=tr(f"gamut.{gamut}"), size=self.size())
        if self.lut is not None:
            text += "\n" + tr("conv.domain_lut", inp=tr(f"space.{self.lut.input_kind}"), out=tr(f"space.{self.lut.output_kind}"),
                             strength=round(self.lut.strength * 100))
        return text


