"""Settings: language (live), theme (system/light/dark), defaults, engine
diagnostics, and the About group (version, licences)."""

from __future__ import annotations

import platform

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QLibraryInfo, Qt, qVersion
from PySide6.QtWidgets import QApplication, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout

from ..app import THEMES, apply_theme, current_color_scheme
from ..backend import LUT_SIZES
from ..context import setting_int
from ..i18n import T, tr
from .. import about
from ..widgets.common import SPACE_S, HeadingLabel, WrapChoice, WrapLabel, bind_combo, fill_combo, make_button
from .base import Workspace


class SettingsWorkspace(Workspace):
    key = "settings"
    icon_name = "settings"

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        # centre: appearance & behaviour
        general = QGroupBox(self.center)
        T.bind(general, "setTitle", "settings.general")
        form = QFormLayout(general)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.language = QComboBox(general)
        # Language names are shown in their own language (Windows convention).
        self.language.addItem("English", "en")
        self.language.addItem("\u65e5\u672c\u8a9e", "ja")
        self.language.setCurrentIndex(self.language.findData(T.language))
        ll = QLabel(general)
        T.bind(ll, "setText", "settings.language")
        T.bind(self.language, "setAccessibleName", "settings.language")
        ll.setBuddy(self.language)
        form.addRow(ll, self.language)
        self.theme = QComboBox(general)
        fill_combo(self.theme, [(f"settings.theme.{t}", t) for t in THEMES])
        bind_combo(self.theme)
        self.theme.setCurrentIndex(max(0, self.theme.findData(str(ctx.settings.value("ui/theme", "system")))))
        tl = QLabel(general)
        T.bind(tl, "setText", "settings.theme")
        T.bind(self.theme, "setAccessibleName", "settings.theme")
        tl.setBuddy(self.theme)
        form.addRow(tl, self.theme)
        self.size = QComboBox(general)
        for n in LUT_SIZES:
            self.size.addItem(f"{n} \u00d7 {n} \u00d7 {n}", n)
        self.size.setCurrentIndex(max(0, self.size.findData(setting_int(ctx.settings, "lut/default_size", 33, LUT_SIZES))))
        sl = QLabel(general)
        T.bind(sl, "setText", "settings.default_size")
        T.bind(self.size, "setAccessibleName", "settings.default_size")
        sl.setBuddy(self.size)
        form.addRow(sl, self.size)
        self.theme_note = WrapLabel("settings.theme_note", secondary=True, parent=general)
        form.addRow(self.theme_note)
        self.center_layout.addWidget(general)

        # updates: the same switches as the automatic check at start-up, plus a manual check and the last result
        self.updates_group = QGroupBox(self.center)
        self.updates_group.setObjectName("settings_updates")
        T.bind(self.updates_group, "setTitle", "settings.updates")
        ul = QVBoxLayout(self.updates_group)
        ul.setSpacing(SPACE_S)
        self.update_auto = WrapChoice("settings.update.auto", parent=self.updates_group)
        self.update_prerelease = WrapChoice("settings.update.prerelease", parent=self.updates_group)
        self.update_check = make_button("update.action.check_now", parent=self.updates_group)
        self.update_status = WrapLabel(parent=self.updates_group)
        self.update_mode = WrapLabel(secondary=True, parent=self.updates_group)
        check_row = QHBoxLayout()
        check_row.setSpacing(SPACE_S)
        check_row.addWidget(self.update_check, 0)
        check_row.addWidget(self.update_status, 1)
        ul.addWidget(self.update_auto)
        ul.addWidget(self.update_prerelease)
        ul.addLayout(check_row)
        ul.addWidget(self.update_mode)
        ul.addWidget(WrapLabel("settings.update.privacy", secondary=True, parent=self.updates_group))
        self.center_layout.addWidget(self.updates_group)

        diag = QGroupBox(self.center)
        T.bind(diag, "setTitle", "settings.diagnostics")
        dform = QFormLayout(diag)
        dform.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.diag_text = QPlainTextEdit(diag)
        self.diag_text.setReadOnly(True)
        self.diag_text.setMinimumHeight(120)
        T.bind(self.diag_text, "setAccessibleName", "settings.diagnostics")
        self.diag_button = make_button("settings.run_diagnostics", "play")
        dform.addRow(self.diag_button)
        dform.addRow(self.diag_text)
        self.center_layout.addWidget(diag, 1)

        # right: About - name, version (selectable), main third-party parts, licences
        about_box = QGroupBox()
        about_box.setObjectName("settings_about")
        T.bind(about_box, "setTitle", "about.group")
        al = QFormLayout(about_box)
        al.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        name = HeadingLabel("", 3, about_box)
        name.setText(about.APP_NAME)
        al.addRow(name)
        self.version_label = QLabel(about_box)
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        T.bind(self.version_label, "setText", "about.version", version=about.APP_VERSION)
        T.bind(self.version_label, "setAccessibleName", "about.version", version=about.APP_VERSION)
        al.addRow(self.version_label)
        self.license_label = QLabel(about_box)
        T.bind(self.license_label, "setText", "about.app_license", license=about.APP_LICENSE)
        al.addRow(self.license_label)
        notices = WrapLabel("settings.notices_text", secondary=True, parent=about_box)
        al.addRow(notices)
        self.licenses_button = make_button("about.show_licenses", "document_table", parent=about_box)
        al.addRow(self.licenses_button)
        self.side.add(about_box)
        self.side.add_stretch()
        self.licenses_button.clicked.connect(lambda: ctx.window.show_licenses() if ctx.window else None)

        self.language.currentIndexChanged.connect(self._language)
        self.theme.currentIndexChanged.connect(self._theme)
        self.size.currentIndexChanged.connect(
            lambda _i: ctx.settings.setValue("lut/default_size", int(self.size.currentData()))
        )
        self.diag_button.clicked.connect(self.run_diagnostics)
        T.watch(self, self._sync_language)
        updates = ctx.updates
        if updates is not None:
            self.update_auto.setChecked(updates.auto_check)
            self.update_prerelease.setChecked(updates.include_prereleases)
            self.update_auto.toggled.connect(lambda on: setattr(updates, "auto_check", on))
            self.update_prerelease.toggled.connect(lambda on: setattr(updates, "include_prereleases", on))
            self.update_check.clicked.connect(lambda: updates.check(manual=True))
            updates.state_changed.connect(self._sync_updates)
            T.watch(self, self._sync_updates)
            self._sync_updates()
        else:
            self.updates_group.setEnabled(False)

    def _sync_updates(self) -> None:
        """Last result, install mode and the Check now button (disabled with a reason while a check runs)."""
        updates = self.ctx.updates
        self.update_status.setText(updates.last_result_text())
        self.update_mode.setText(updates.mode_text())
        self.update_check.setEnabled(not updates.checking)
        self.update_check.setToolTip(tr("update.reason.checking") if updates.checking else "")

    def primary_focus(self):
        return self.language

    def _sync_language(self) -> None:
        idx = self.language.findData(T.language)
        if idx != self.language.currentIndex():
            self.language.blockSignals(True)
            self.language.setCurrentIndex(idx)
            self.language.blockSignals(False)

    def _language(self, _i: int) -> None:
        lang = self.language.currentData()
        self.ctx.settings.setValue("ui/language", lang)
        T.set_language(lang)
        self.ctx.log.info("log.settings.language", lang=self.language.currentText())

    def _theme(self, _i: int) -> None:
        theme = self.theme.currentData()
        self.ctx.settings.setValue("ui/theme", theme)
        apply_theme(theme)
        self.ctx.log.info("log.settings.theme", theme=tr(f"settings.theme.{theme}"), scheme=current_color_scheme())

    def run_diagnostics(self) -> None:
        engine = self.ctx.engine
        app = QApplication.instance()
        static = [
            f"Python {platform.python_version()} ({platform.architecture()[0]}) on {platform.platform()}",
            f"Qt {qVersion()} / PySide6 {pyside_version}",
            f"QStyle: {app.style().name() if app else '?'}",
            f"Color scheme: {current_color_scheme()}",
            f"Device pixel ratio: {self.devicePixelRatioF():.2f}",
            f"Qt plugins: {QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)}",
        ]
        self.diag_text.setPlainText("\n".join(static + [tr("settings.diag_running")]))

        def work(token, progress):
            lines = []
            for name, err in engine.module_status().items():
                lines.append(f"engine.{name}: {'OK' if not err else err}")
            try:
                lines.append(f"LittleCMS: {engine.probe_lcms()}")
            except Exception as exc:  # noqa: BLE001
                info = engine.map_error(exc)
                lines.append(f"LittleCMS: {info.summary()} {info.detail}")
            try:
                lines.append(f"rawpy: {engine.raw_support()}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"rawpy: {exc}")
            return lines

        self.ctx.jobs.submit("settings:diag", "job.diagnostics", work,
                             on_result=lambda lines: self.diag_text.setPlainText("\n".join(static + lines)))
