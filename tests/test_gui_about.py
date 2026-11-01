"""Version and licence information in the GUI (Settings > About, Help menu)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PySide6.QtCore import QSettings  # noqa: E402

import camera_profile_studio  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def win(qtbot, tmp_path):
    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.i18n import T

    create_application([])
    T.set_language("en")
    from camera_profile_studio.gui.main_window import MainWindow

    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(w)
    yield w
    T.set_language("en")
    w._force_close = True
    w.close()


def test_version_and_licence_are_shown_in_settings_and_about(win):
    from camera_profile_studio.gui.i18n import T

    v = camera_profile_studio.__version__
    settings = win.workspaces["settings"]
    assert settings.version_label.text() == f"Version {v}"
    assert settings.license_label.text().startswith("Licence: MIT")
    box = win.about_box()
    assert v in box.text() and "Camera Profile Studio" in box.text() and "&" not in box.windowTitle()
    T.set_language("ja")
    assert settings.version_label.text() == f"バージョン {v}"


def test_declared_licence_matches_the_licence_file():
    from camera_profile_studio.gui import about

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == about.APP_LICENSE == "MIT"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert (ROOT / "licenses" / "APP-LICENSE.txt").read_bytes() == (ROOT / "LICENSE").read_bytes()


def test_help_menu_offers_licences_and_about(win):
    actions = [a for a in win.help_menu.actions() if not a.isSeparator()]
    assert actions == [win.act_licenses, win.act_about]


def test_licence_entries_come_from_the_shipped_folder():
    import json

    from camera_profile_studio.gui import about

    base = about.licenses_dir()
    assert base == ROOT / "licenses"
    entries = about.license_entries(base)
    by = {e.name: e for e in entries}
    app = entries[0]
    assert app.name == "Camera Profile Studio" and app.version == camera_profile_studio.__version__
    assert app.license == "MIT" and app.files and "MIT License" in about.read_text(app, base)
    for required in ("Qt", "Python", "LittleCMS", "Fluent UI System Icons", "PySide6", "numpy", "scipy", "rawpy"):
        assert required in by, required
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8-sig"))
    assert {m["name"] for m in manifest} <= set(by)          # every recorded package is listed
    for e in entries:
        if e.files:
            assert about.read_text(e, base).strip(), e.name
    assert not by["ordered-set"].files                         # recorded without a licence file
    assert (by["numpy"].license, by["numpy"].bundled) == ("BSD-3-Clause", True)
    assert by["rawpy"].license == "MIT AND LGPL-2.1 (LibRaw)"
    assert by["Nuitka"].license == "Apache-2.0"


def test_known_licence_names_need_the_pinned_version():
    from camera_profile_studio.gui import about

    assert about.KNOWN_LICENSES.get(("numpy", "9.9.9")) is None
    assert about._short("Apache License, Version 2.0") == "Apache-2.0"
    assert about._short("BSD") == "BSD"                          # no guessing beyond spelling


def test_licence_dialog_shows_the_selected_text(qtbot, win):
    from camera_profile_studio.gui.widgets.license_dialog import LicenseDialog

    dlg = LicenseDialog(win)
    qtbot.addWidget(dlg)
    assert "MIT License" in dlg.current_text()                 # the application's own licence first
    names = [e.name for e in dlg.entries]
    row = names.index("numpy")
    dlg.table.selectRow(row)
    assert "NumPy" in dlg.current_text()
    assert dlg.model.item(row, 2).text() == "BSD-3-Clause + bundled components"
    row = names.index("ordered-set")
    dlg.table.selectRow(row)
    assert dlg.note.isVisibleTo(dlg) and "no licence file" in dlg.current_text()
    assert dlg.open_button.isEnabled()


def test_licence_dialog_without_folder_explains(qtbot, win, monkeypatch):
    from camera_profile_studio.gui import about
    from camera_profile_studio.gui.widgets.license_dialog import LicenseDialog

    monkeypatch.setattr(about, "licenses_dir", lambda: None)
    dlg = LicenseDialog(win)
    qtbot.addWidget(dlg)
    assert len(dlg.entries) == 1 and not dlg.open_button.isEnabled()
    assert dlg.folder.text() and "not found" in dlg.folder.text() and "not found" in dlg.current_text()
