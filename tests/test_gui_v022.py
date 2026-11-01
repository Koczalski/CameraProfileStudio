"""0.2.2 GUI behaviour.

B11  Batch: items that cannot be processed with the current settings are
     flagged with the reason before the run, skipped with that reason, and Run
     is disabled when nothing could be processed (an ICC in look mode used to
     fail only after the batch had started).
E01  Every LUT can be exported as .cube, Hald CLUT PNG or strip PNG
     (single-LUT pages and Batch).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from test_gui_regressions import PROFILES, idle, win  # noqa: E402,F401 - shared fixture


def _checked(qtbot, b):
    qtbot.waitUntil(lambda: all(not i.checking for i in b.model.items), timeout=30000)
    b._update()


def _row(b, name):
    m = b.model
    r = next(i for i, it in enumerate(m.items) if Path(it.path).name == name)
    return m.data(m.index(r, 1)), m.data(m.index(r, 3))


def e_level(entry):
    return entry.level


def _setup(b, tmp_path, mode, look_mode=None):
    b.set_output_dir(str(tmp_path))
    b.mode.setCurrentIndex(b.mode.findData(mode))
    if look_mode:
        b.look_mode.setCurrentIndex(b.look_mode.findData(look_mode))
    b.size.setCurrentIndex(b.size.findData(17))


# ---------------------------------------------------------------------- B11
def test_batch_icc_in_look_mode_is_flagged_before_run(qtbot, win, tmp_path):
    b = win.workspaces["batch"]
    _setup(b, tmp_path, "look", "combined")
    b.add_files([str(PROFILES / "Sony_DSC-F828.icc")])
    _checked(qtbot, b)
    state, detail = _row(b, "Sony_DSC-F828.icc")
    assert state == "Not applicable" and "Technical conversion" in detail
    assert not b.run_button.isEnabled()
    assert "No item in the list can be processed" in b.run_reason.text()
    b.mode.setCurrentIndex(b.mode.findData("technical"))
    assert _row(b, "Sony_DSC-F828.icc")[0] == "Pending" and b.run_button.isEnabled()


def test_batch_mixed_look_run_skips_icc_with_reason(qtbot, win, tmp_path):
    b = win.workspaces["batch"]
    _setup(b, tmp_path, "look", "look_only")
    b.add_files([str(PROFILES / "Sony_DSC-F828.icc"), str(PROFILES / "Nikon_D70s.dcp")])
    _checked(qtbot, b)
    assert b.run_button.isEnabled() and "1 item(s) cannot be processed" in b.blocked_note.text()
    b.run()
    idle(qtbot, win)
    states = {Path(i.path).name: i.state for i in b.model.items}
    assert states == {"Sony_DSC-F828.icc": "skipped", "Nikon_D70s.dcp": "succeeded"}
    assert _row(b, "Sony_DSC-F828.icc")[1].startswith("Not processed: ICC profiles cannot be used for look extraction")
    assert [p.name for p in tmp_path.glob("Sony*")] == []
    skipped = [e for e in win.ctx.log.model.entries if e.key == "log.batch.item_not_applicable"]
    assert len(skipped) == 1 and e_level(skipped[0]) == "warning" and "Sony_DSC-F828.icc" in skipped[0].detail


def test_batch_reasons_follow_the_settings(qtbot, win, tmp_path):
    b = win.workspaces["batch"]
    _setup(b, tmp_path, "look", "tone_only")
    b.add_files([str(PROFILES / "Olympus_E1.dcp")])
    _checked(qtbot, b)
    state, detail = _row(b, "Olympus_E1.dcp")
    assert state == "Not applicable" and "Tone curve only" in detail and "HueSatMap" in detail
    b.look_mode.setCurrentIndex(b.look_mode.findData("colors_only"))
    assert _row(b, "Olympus_E1.dcp")[0] == "Pending"
    b.mode.setCurrentIndex(b.mode.findData("technical"))
    state, detail = _row(b, "Olympus_E1.dcp")
    assert state == "Needs input" and "camera neutral" in detail
    for spin, v in zip(b.neutral, (0.45, 1.0, 0.62)):
        spin.setValue(v)
    assert _row(b, "Olympus_E1.dcp")[0] == "Pending"


def test_batch_flags_unsupported_icc_intent(qtbot, win, tmp_path):
    b = win.workspaces["batch"]
    _setup(b, tmp_path, "technical")
    b.intent.setCurrentIndex(b.intent.findData("relative"))
    b.add_files([str(PROFILES / "Nikon_D200.icc")])  # LUT-based: perceptual only
    _checked(qtbot, b)
    state, detail = _row(b, "Nikon_D200.icc")
    assert state == "Needs input" and "Relative colorimetric" in detail and "Perceptual" in detail
    b.intent.setCurrentIndex(b.intent.findData("perceptual"))
    assert _row(b, "Nikon_D200.icc")[0] == "Pending"
    b.run()
    idle(qtbot, win)
    assert b.model.items[0].state == "succeeded"


def test_batch_unreadable_file_is_shown_before_the_run(qtbot, win, tmp_path):
    bad = tmp_path / "broken.icc"
    bad.write_bytes(b"not a profile")
    out = tmp_path / "out"
    out.mkdir()
    b = win.workspaces["batch"]
    _setup(b, out, "technical")
    b.add_files([str(bad)])
    _checked(qtbot, b)
    assert _row(b, "broken.icc")[0] == "Unreadable"
    assert not b.run_button.isEnabled()


def test_look_dcp_only_message_says_what_to_do():
    from camera_profile_studio.gui.strings import STRINGS

    assert "Technical conversion" in STRINGS["en"]["reason.look_dcp_only"]
    assert "技術変換" in STRINGS["ja"]["reason.look_dcp_only"]


# ---------------------------------------------------------------------- E01
def test_converter_exports_every_format(qtbot, win, tmp_path):
    from camera_profile_studio.lut import decode_png, png_layout

    win.ctx.open_profiles([str(PROFILES / "Sony_DSC-F828.icc")])
    idle(qtbot, win)
    conv = win.workspaces["converter"]
    conv.size_combo.setCurrentIndex(conv.size_combo.findData(33))
    idle(qtbot, win)
    assert conv.lut is not None
    for fmt, ext in (("cube", ".cube"), ("hald_png", ".png"), ("strip_png", ".png"), ("strip_png_flipped", ".png")):
        conv.format_combo.setCurrentIndex(conv.format_combo.findData(fmt))
        assert conv.lut is not None  # changing the file format never rebuilds the LUT
        assert conv.suggested_file().endswith(ext)
        out = tmp_path / f"conv_{fmt}{ext}"
        conv.export_to(str(out), False)
        idle(qtbot, win)
        assert out.exists() and out.with_suffix(".json").exists()
        if ext == ".png":
            layout = png_layout(fmt, 33)
            assert f"{layout['width']} × {layout['height']}" in conv.format_hint.text()
            assert decode_png(out.read_bytes()).shape == (layout["height"], layout["width"], 3)
    assert win.errors == []


def test_batch_png_output_names(qtbot, win, tmp_path):
    from camera_profile_studio.lut import decode_png

    b = win.workspaces["batch"]
    _setup(b, tmp_path, "technical")
    b.format.setCurrentIndex(b.format.findData("hald_png"))
    b.add_files([str(PROFILES / "Sony_DSC-F828.icc")])
    _checked(qtbot, b)
    b.run()
    idle(qtbot, win)
    item = b.model.items[0]
    assert item.state == "succeeded" and Path(item.output).name == "Sony_DSC-F828_to_srgb_17_hald.png"
    assert decode_png(Path(item.output).read_bytes()).shape == (64, 64, 3)
    assert Path(item.output).with_suffix(".json").exists()


def test_save_dialog_lists_every_format_and_uses_the_chosen_type(qtbot, win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from camera_profile_studio.lut import decode_png

    win.ctx.open_profiles([str(PROFILES / "Sony_DSC-F828.icc")])
    idle(qtbot, win)
    conv = win.workspaces["converter"]
    conv.format_combo.setCurrentIndex(conv.format_combo.findData("cube"))
    idle(qtbot, win)
    win.ctx.remember_dir("export", str(tmp_path / "x.cube"))
    seen = {}

    def fake(parent, caption, directory, filters, selected="", *a, **k):
        seen.update(directory=directory, filters=filters.split(";;"), selected=selected)
        # the user keeps the suggested name and picks the strip PNG type in the list
        return directory, seen["filters"][2]

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake))
    conv.export()
    idle(qtbot, win)
    assert len(seen["filters"]) == 4 and seen["selected"] == seen["filters"][0]
    assert "*.cube" in seen["filters"][0] and all("*.png" in f for f in seen["filters"][1:])
    suggested = Path(seen["directory"])
    assert suggested.suffix == ".cube"
    out = suggested.with_name(suggested.stem + "_strip.png")
    assert out.exists() and decode_png(out.read_bytes()).shape == (32, 1024, 3)
    assert conv.format_combo.currentData() == "strip_png"
    assert win.errors == []


@pytest.mark.parametrize("path, fmt, previous, expected", [
    ("d/a_33.cube", "hald_png", "cube", "d/a_33_hald.png"),          # suggested name kept: tag + extension follow
    ("d/a_33_hald.png", "strip_png_flipped", "hald_png", "d/a_33_strip_flipped.png"),
    ("d/a_33_strip_flipped.png", "cube", "strip_png_flipped", "d/a_33.cube"),
    ("d/mine.png", "cube", "hald_png", "d/mine.cube"),                # user's own name: only the extension
    ("d/mine", "strip_png", "strip_png", "d/mine.png"),
])
def test_export_path_follows_the_chosen_type(path, fmt, previous, expected):
    from camera_profile_studio.gui.workspaces.lut_base import LutWorkspace

    suggested = {"cube": "a_33.cube", "hald_png": "a_33_hald.png", "strip_png": "a_33_strip.png",
                 "strip_png_flipped": "a_33_strip_flipped.png"}[previous]
    assert Path(LutWorkspace.resolve_export_path(path, fmt, previous, suggested)) == Path(expected)


# ---------------------------------------------------------------------- 0.2.4
@pytest.mark.qt_no_exception_capture
def test_gui_thread_exceptions_are_logged_and_shown_once(qtbot, win):
    import sys

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    win._install_exception_hook()      # pytest-qt swaps the hook per test; the app installs it once at start-up
    assert sys.excepthook is not sys.__excepthook__
    before = len(win.ctx.log.model.entries)
    for _ in range(3):
        QTimer.singleShot(0, lambda: [].pop())
        qtbot.wait(30)
    errors = [e for e in win.ctx.log.model.entries[before:] if e.key == "log.gui.unexpected"]
    assert len(errors) == 3 and "IndexError" in errors[0].detail
    boxes = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]
    assert len(boxes) == 1           # repeats of the same failure do not stack dialogs
    for b in boxes:
        b.close()


def test_batch_holds_back_duplicate_output_names(qtbot, win, tmp_path):
    import shutil

    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(PROFILES / "Sony_DSC-F828.icc", other / "Sony_DSC-F828.icc")
    b = win.workspaces["batch"]
    out = tmp_path / "out"
    out.mkdir()
    _setup(b, out, "technical")
    b.add_files([str(PROFILES / "Sony_DSC-F828.icc"), str(other / "Sony_DSC-F828.icc")])
    _checked(qtbot, b)
    states = [_row_at(b, r) for r in range(2)]
    assert states[0][0] == "Pending"
    assert states[1][0] == "Not applicable" and "same output file" in states[1][1]
    b.run()
    idle(qtbot, win)
    assert [i.state for i in b.model.items] == ["succeeded", "skipped"]
    assert len(list(out.glob("*.cube"))) == 1


def _row_at(b, r):
    m = b.model
    return m.data(m.index(r, 1)), m.data(m.index(r, 3))


def test_damaged_settings_file_does_not_prevent_start(qtbot, tmp_path):
    """0.2.4 (E9): int('abc') on lut/default_size raised during MainWindow
    construction, so a hand-edited or damaged settings file stopped the app."""
    from PySide6.QtCore import QSettings

    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.main_window import MainWindow

    create_application([])
    ini = tmp_path / "s.ini"
    ini.write_text("[lut]\ndefault_size=abc\nexport_format=zzz\n[ui]\ntheme=neon\nlanguage=xx\n"
                   "[window]\ngeometry=@ByteArray(garbage)\nsplit_h=@Invalid()\nsplit_v=12\n")
    w = MainWindow(settings=QSettings(str(ini), QSettings.Format.IniFormat))
    qtbot.addWidget(w)
    w.show()
    assert w.width() >= 320 and w.height() >= 240
    assert w.workspaces["converter"].size_combo.currentData() == 33
    assert w.workspaces["converter"].format_combo.currentData() == "cube"
    assert w.workspaces["batch"].size.currentData() == 33
    w._force_close = True
    w.close()
    w.ctx.jobs.shutdown(5000)
