"""Camera Look Extractor: DCP look elements as an sRGB -> sRGB LUT.

Modes come from the engine's per-mode capabilities (``look.<mode>``); modes
without applicable elements are disabled with the engine's reason.  What the
profile can and cannot reproduce is listed explicitly; results are labelled
as profile-derived approximations, never as verified camera-JPEG matches.
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
    QLabel,
    QRadioButton,
    QVBoxLayout,
)

from ..backend import GAMUTS, LOOK_MODES
from ..context import ProfileEntry
from ..i18n import T, tr
from ..widgets.capabilities import ComponentList
from ..widgets.common import AdvancedSection, WrapChoice, WrapLabel, bind_combo, fill_combo
from .converter import ProfilePicker
from .lut_base import LutWorkspace


class LookWorkspace(LutWorkspace):
    key = "look"
    icon_name = "color"
    build_title_key = "job.build_look"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.approx_banner = WrapLabel("look.approx_banner", parent=self.center)
        self.center_layout.insertWidget(0, self.approx_banner)

        self.picker = ProfilePicker(ctx, "look.step.profile")
        self.side.add(self.picker)
        # modes
        mode_box = QGroupBox()
        T.bind(mode_box, "setTitle", "look.step.mode")
        ml = QVBoxLayout(mode_box)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, WrapChoice] = {}
        self.mode_reasons: dict[str, WrapLabel] = {}
        for i, mode in enumerate(LOOK_MODES):
            rb = WrapChoice(f"look.mode.{mode}", radio=True, parent=mode_box)
            self.mode_group.addButton(rb.button, i)
            self.mode_buttons[mode] = rb
            reason = WrapLabel("", secondary=True, parent=mode_box)
            reason.setContentsMargins(24, 0, 0, 0)
            reason.setVisible(False)
            self.mode_reasons[mode] = reason
            ml.addWidget(rb)
            ml.addWidget(reason)
        self.mode_buttons["combined"].setChecked(True)
        self.mode_group.idToggled.connect(lambda _i, on: on and self.settings_changed())
        self.side.add(mode_box)
        # reproducible / not reproducible
        self.components = ComponentList("look.reproducible")
        self.side.add(self.components)
        self.not_repro = QGroupBox()
        T.bind(self.not_repro, "setTitle", "look.not_reproducible")
        nl = QVBoxLayout(self.not_repro)
        nl.addWidget(WrapLabel("look.not_reproducible_text", parent=self.not_repro))
        self.side.add(self.not_repro)
        # illuminant
        ill = QGroupBox()
        T.bind(ill, "setTitle", "look.illuminant")
        iform = QFormLayout(ill)
        iform.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.kelvin = QDoubleSpinBox(ill)
        self.kelvin.setRange(0, 50000)
        self.kelvin.setDecimals(0)
        self.kelvin.setSingleStep(100)
        self.kelvin.setSuffix(" K")
        self.kelvin.setSpecialValueText(tr("conv.wb.unset"))
        T.watch(self.kelvin, lambda: self.kelvin.setSpecialValueText(tr("conv.wb.unset")))
        kl = QLabel(ill)
        T.bind(kl, "setText", "conv.wb.kelvin")
        T.bind(self.kelvin, "setAccessibleName", "conv.wb.kelvin")
        kl.setBuddy(self.kelvin)
        iform.addRow(kl, self.kelvin)
        self.kelvin_hint = WrapLabel("look.kelvin_hint", secondary=True, parent=ill)
        iform.addRow(self.kelvin_hint)
        self.kelvin.valueChanged.connect(lambda _v: self.settings_changed())
        self.side.add(ill)
        self.side.add(self.make_output_group())
        self.advanced = AdvancedSection("advanced.title")
        self.gamut = QComboBox()
        fill_combo(self.gamut, [(f"gamut.{g}", g) for g in GAMUTS])
        bind_combo(self.gamut)
        gl = QLabel()
        T.bind(gl, "setText", "conv.gamut")
        T.bind(self.gamut, "setAccessibleName", "conv.gamut")
        gl.setBuddy(self.gamut)
        self.advanced.add_widget(gl)
        self.advanced.add_widget(self.gamut)
        self.gamut.currentIndexChanged.connect(lambda _i: self.settings_changed())
        # Explicitly disabling a component is allowed only where it is present
        # and used by the chosen mode (engine never invents absent ones).
        self.hsm = QCheckBox()
        self.lt = QCheckBox()
        self.tc = QCheckBox()
        for box, key in ((self.hsm, "conv.dcp.hsm"), (self.lt, "conv.dcp.lt"), (self.tc, "conv.dcp.tc")):
            box.setChecked(True)
            T.bind(box, "setText", key)
            self.advanced.add_widget(box)
            box.toggled.connect(lambda _b: self.settings_changed())
        self.side.add(self.advanced)
        self.side.add(self.make_status_group())
        self.side.add_stretch()
        ctx.current_profile_changed.connect(self._profile_changed)
        self._profile_changed(ctx.current_profile)

    def primary_focus(self):
        return self.picker.combo

    def mode(self) -> str:
        for mode, rb in self.mode_buttons.items():
            if rb.isChecked():
                return mode
        return "combined"

    def _info(self):
        e = self.ctx.current_profile
        return e.info if e and e.state == "ready" else None

    def _profile_changed(self, entry: ProfileEntry | None) -> None:
        info = self._info()
        modes = info.look_modes if info else {}
        for mode, rb in self.mode_buttons.items():
            cap = modes.get(mode)
            ok = cap is not None and cap.status in ("supported", "partial", "available")
            rb.setEnabled(ok)
            label = self.mode_reasons[mode]
            if info is None:
                label.setVisible(False)
            elif not ok:
                text = (cap.reason if cap and cap.reason else tr("look.mode_unavailable"))
                label.setText(text)
                label.setVisible(True)
                rb.setToolTip(text)
            else:
                # Available: engine caveats live in the tooltip to keep the
                # mode list scannable; unavailable modes show their reason.
                label.setVisible(False)
                rb.setToolTip(cap.reason)
        if info is not None and not self.mode_buttons[self.mode()].isEnabled():
            for mode in ("combined", "colors_only", "look_only", "tone_only"):
                if self.mode_buttons[mode].isEnabled():
                    self.mode_buttons[mode].setChecked(True)
                    break
        self.components.set_items(info.look_components if info else {}, "look.component")
        self.settings_changed()

    _MODE_USES = {"colors_only": ("hsm", "lt"), "tone_only": ("tc",), "look_only": ("lt",), "combined": ("hsm", "lt", "tc")}
    _COMPONENT = {"hsm": "hue_sat_map", "lt": "look_table", "tc": "tone_curve"}

    def _sync_components(self) -> None:
        info = self._info()
        uses = self._MODE_USES.get(self.mode(), ())
        for name in ("hsm", "lt", "tc"):
            box = getattr(self, name)
            cap = info.look_components.get(self._COMPONENT[name]) if info else None
            present = cap is not None and cap.status == "available"
            box.setEnabled(present and name in uses)
            box.setToolTip("" if present else tr("conv.dcp.absent"))
        needs = bool(info) and self.ctx.engine.look_needs_kelvin(info, self.mode(), self._include("hsm"))
        self.kelvin_hint.setVisible(needs)

    def _include(self, name: str) -> bool:
        box = getattr(self, name, None)
        return True if box is None else box.isChecked()

    def settings_changed(self) -> None:
        if hasattr(self, "hsm"):
            self._sync_components()
        if hasattr(self, "status_label"):
            super().settings_changed()

    def build_precondition(self):
        if not self.ctx.engine.available:
            return ("reason.engine_unavailable", {})
        e = self.ctx.current_profile
        if e is None:
            return ("reason.no_profile", {})
        if e.state == "loading":
            return ("reason.profile_loading", {})
        if e.state == "failed" or e.info is None:
            return ("reason.profile_failed", {})
        info = e.info
        if info.kind != "dcp":
            return ("reason.look_dcp_only", {})
        cap = info.look_modes.get(self.mode())
        if cap is None or cap.status not in ("supported", "partial", "available"):
            return ("reason.look_mode_unavailable", {"reason": (cap.reason if cap else "") or tr("look.mode_unavailable")})
        if self.ctx.engine.look_needs_kelvin(info, self.mode(), self._include("hsm")) and not self.kelvin.value():
            return ("reason.dcp_needs_kelvin", {})
        return None

    def build_job(self, size: int, strength: float):
        engine = self.ctx.engine
        request = engine.look_request(self._info(), self.mode(), {
            "kelvin": self.kelvin.value() or None, "gamut": self.gamut.currentData(),
            "hsm": self._include("hsm"), "lt": self._include("lt"), "tc": self._include("tc"),
        })

        def work(token, progress):
            return engine.convert(request, size, strength, token, progress)

        return work

    def suggested_name(self) -> str:
        e = self.ctx.current_profile
        stem = Path(e.path).stem if e else "look"
        return f"{stem}_{self.mode()}_EXPERIMENTAL_{self.size()}.cube"

    def domain_text(self) -> str:
        return tr("look.domain", mode=tr(f"look.mode.{self.mode()}"), size=self.size(),
                  strength=self.strength_spin.value() if hasattr(self, "strength_spin") else 100)



