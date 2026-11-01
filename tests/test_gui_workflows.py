"""End-to-end GUI workflows against the real engine (offscreen Qt).

These drive the same widgets/slots a user triggers.  Native-window visual
checks live in ``tests/gui_screenshots.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PySide6.QtCore import QMimeData, QPointF, QSettings, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "input" / "ccd_profiles"


@pytest.fixture()
def fx():
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    import gui_fixtures

    return gui_fixtures.build()


@pytest.fixture()
def win(qtbot, tmp_path):
    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.i18n import T

    create_application([])
    T.set_language("en")
    from camera_profile_studio.gui.main_window import MainWindow

    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    if not w.ctx.engine.available:
        pytest.skip(f"engine unavailable: {w.ctx.engine.unavailable_reason}")
    w.errors = []
    w.ctx.show_error = lambda err, title_key="": w.errors.append(err)
    w.ctx.confirm = lambda *a, **k: True
    qtbot.addWidget(w)
    w.resize(1280, 820)
    w.show()
    yield w
    w._force_close = True
    w.close()
    w.ctx.jobs.shutdown(10000)


def idle(qtbot, w, timeout=60000):
    def done():
        if w.ctx.jobs.active():
            return False
        return not any(getattr(ws, "_debounce", None) is not None and ws._debounce.isActive() for ws in w.workspaces.values())

    qtbot.wait(50)
    qtbot.waitUntil(done, timeout=timeout)


def drop(widget, paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    ev = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(ev)


def test_drop_inspect_shows_capabilities_and_errors(qtbot, win, fx):
    insp = win.workspaces["inspector"]
    drop(insp.drop, [PROFILES / "Nikon_D70s.dcp", fx["invalid"], ROOT / "README_does_not_matter.txt"])
    idle(qtbot, win)
    states = {e.name: e.state for e in win.ctx.profiles.entries}
    assert states == {"Nikon_D70s.dcp": "ready", "broken_profile.icc": "failed"}
    win.ctx.set_current_profile(win.ctx.profiles.entries[0])
    assert insp.caps.rows["technical"].status.text() == "Supported"
    assert insp.struct_model.rowCount() >= 10
    failed = win.ctx.profiles.entries[1]
    assert failed.error is not None and failed.error.key == "error.invalid_profile"


def test_converter_icc_to_icc_target_does_not_change_input(qtbot, win, fx):
    win.ctx.open_profiles([str(PROFILES / "Sony_DSC-F828.icc")])
    idle(qtbot, win)
    conv = win.workspaces["converter"]
    src = win.ctx.current_profile
    conv.direction.setCurrentIndex(conv.direction.findData("to_icc"))
    assert "Choose an output ICC profile" in conv.status_label.text() or not conv.export_button.isEnabled()
    win.ctx.open_profiles([str(fx["long_profile"])], make_current=False)
    idle(qtbot, win)
    assert win.ctx.current_profile is src
    conv.target_combo.setCurrentIndex(conv.target_combo.findData(str(Path(fx["long_profile"]))))
    idle(qtbot, win)
    assert conv.lut is not None
    raw = conv.lut.raw
    assert raw.input_space.profile_path != raw.output_space.profile_path
    # same kind, different profile -> strength must stay fixed
    assert not conv.strength_slider.isEnabled()
    assert not conv.lut.strength_supported


def test_converter_rejects_unsupported_direction_and_needs_wb(qtbot, win):
    win.ctx.open_profiles([str(PROFILES / "Nikon_D200.icc"), str(PROFILES / "Nikon_D70s.dcp")])
    idle(qtbot, win)
    conv = win.workspaces["converter"]
    d200 = next(e for e in win.ctx.profiles.entries if e.name == "Nikon_D200.icc")
    win.ctx.set_current_profile(d200)
    idx = conv.direction.findData("from_srgb")
    assert not conv.direction.model().item(idx).isEnabled()
    d70s = next(e for e in win.ctx.profiles.entries if e.name == "Nikon_D70s.dcp")
    win.ctx.set_current_profile(d70s)
    idle(qtbot, win)
    assert conv.lut is None and not conv.export_button.isEnabled()
    assert "camera neutral" in conv.export_reason.text()
    for spin, v in zip(conv.neutral, (0.45, 1.0, 0.62)):
        spin.setValue(v)
    idle(qtbot, win)
    assert "colour temperature" in conv.export_reason.text()  # dual illuminant
    conv.kelvin.setValue(5000)
    idle(qtbot, win)
    assert conv.lut is not None and conv.lut.input_kind == "camera_linear"
    assert not conv.lut.previewable  # camera-linear LUT never previewed on sRGB photos


def test_look_preview_and_export_use_same_baked_lut(qtbot, win, fx, tmp_path):
    from camera_profile_studio import images, lut as lutmod
    from camera_profile_studio.types import SpaceSpec

    win.ctx.open_profiles([str(PROFILES / "Nikon_D70s.dcp")])
    idle(qtbot, win)
    look = win.workspaces["look"]
    look.mode_buttons["look_only"].setChecked(True)
    look.preview.load(str(fx["chart"]))
    look.strength_spin.setValue(60)
    idle(qtbot, win)
    assert look.lut is not None and look.lut.strength == pytest.approx(0.6)
    assert look.preview.viewer._after is not None
    out = tmp_path / "look.cube"
    look.export_to(str(out), False)
    idle(qtbot, win)
    assert out.exists() and out.with_suffix(".json").exists()
    back = lutmod.read_cube(out, input_space=SpaceSpec("srgb"), output_space=SpaceSpec("srgb"))
    np.testing.assert_allclose(back.values, look.lut.raw.values, atol=1e-5)
    img = look.preview.source.raw
    a = np.asarray(images.render_preview(img, look.lut.raw).rgb)
    b = np.asarray(images.render_preview(img, back).rgb)
    np.testing.assert_allclose(a, b, atol=1e-4)
    # combined needs Kelvin for the dual HueSatMap -> disabled with reason
    look.mode_buttons["combined"].setChecked(True)
    idle(qtbot, win)
    assert not look.export_button.isEnabled() and "colour temperature" in look.export_reason.text()
    # explicit exclusion of HueSatMap removes that requirement
    look.hsm.setChecked(False)
    idle(qtbot, win)
    assert look.lut is not None


def test_existing_export_is_not_overwritten_without_confirmation(qtbot, win, fx, tmp_path):
    win.ctx.open_profiles([str(PROFILES / "Nikon_D70s.dcp")])
    idle(qtbot, win)
    look = win.workspaces["look"]
    look.mode_buttons["look_only"].setChecked(True)
    idle(qtbot, win)
    out = tmp_path / "x.cube"
    out.write_text("keep")
    look.export_to(str(out), False)  # overwrite=False -> engine refuses
    idle(qtbot, win)
    assert out.read_text() == "keep"
    assert win.errors, "user must be told why nothing was written"


def test_match_requires_confirmation_and_reports_holdout(qtbot, win, fx):
    match = win.workspaces["match"]
    assert not match.rb_ref.isEnabled()  # unrelated reference photos unsupported
    match.rb_pair.setChecked(True)
    match.load_pair_image("source", str(fx["pair_source"]))
    match.load_pair_image("target", str(fx["pair_target"]))
    idle(qtbot, win)
    match.evidence.setText("synthetic test fixture")
    assert not match.fit_button.isEnabled()
    assert "same scene" in match.fit_reason_label.text()
    match.same_scene.setChecked(True)
    assert not match.fit_button.isEnabled()  # must register or declare alignment
    match.prealigned.setChecked(True)
    assert match.fit_button.isEnabled()
    match._fit()
    idle(qtbot, win, 120000)
    assert match.outcome is not None and match.outcome.holdout_count > 0
    assert "hold-out" in match.report_label.text()
    assert match.lut is not None and match.lut.previewable


def test_batch_per_file_status_and_skip_existing(qtbot, win, fx, tmp_path):
    batch = win.workspaces["batch"]
    batch.add_files([str(PROFILES / "Sony_DSC-F828.icc"), str(fx["invalid"])])
    batch.set_output_dir(str(tmp_path))
    batch.run()
    idle(qtbot, win, 120000)
    states = {Path(i.path).name: i.state for i in batch.model.items}
    assert states == {"Sony_DSC-F828.icc": "succeeded", "broken_profile.icc": "failed"}
    cube = next(tmp_path.glob("*.cube"))
    stamp = cube.stat().st_mtime_ns
    asked = []
    batch.ask_conflicts = lambda conflicts: (asked.append(len(conflicts)), "skip")[1]
    batch.run()
    idle(qtbot, win, 120000)
    assert asked == [1]
    assert cube.stat().st_mtime_ns == stamp
    assert {Path(i.path).name: i.state for i in batch.model.items}["Sony_DSC-F828.icc"] == "skipped"
    batch.ask_conflicts = lambda conflicts: "cancel"
    batch.run()
    assert not batch.running()


def test_keyboard_navigation_and_language_switch(qtbot, win):
    from camera_profile_studio.gui import strings
    from camera_profile_studio.gui.i18n import T

    qtbot.keyClick(win, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
    # shortcuts need an active window; fall back to the action when offscreen
    if win.current_workspace().key != "look":
        win.ws_actions[2].trigger()
    assert win.current_workspace().key == "look"
    win.nav.setFocus()
    qtbot.keyClick(win.nav, Qt.Key.Key_Down)
    assert win.current_workspace().key == "match"
    win.act_lang.trigger()
    assert T.language == "ja"
    assert win.nav.nav_model.item(0).text() == strings.JA["nav.inspector"]
    assert win.workspaces["match"].title.text() == strings.JA["nav.match"]
    win.act_lang.trigger()
    assert T.language == "en"
    # advanced settings toggle via keyboard
    adv = win.workspaces["converter"].advanced
    win.show_workspace("converter")
    adv.button.setFocus()
    qtbot.keyClick(adv.button, Qt.Key.Key_Space)
    assert adv.is_expanded()


def test_close_waits_for_running_job(qtbot, win):
    import threading

    release = threading.Event()
    win.ctx.jobs.submit("native", "job.build_lut", lambda t, p: release.wait(5) and "x")
    win._force_close = True
    qtbot.wait(50)
    win.shutdown_timeout_ms = 50
    win.close()
    assert win.isVisible() and not win.ctx.jobs.is_idle()  # window kept while the worker runs
    release.set()
    qtbot.waitUntil(lambda: not win.isVisible(), timeout=5000)



def _fake_lut(title):
    from camera_profile_studio.gui.backend import LutData

    return LutData(size=17, title=title, input_kind="srgb", output_kind="srgb", strength=1.0, provenance=[])


def test_late_lut_completion_cannot_restore_old_lut(qtbot, win, monkeypatch):
    import threading

    look = win.workspaces["look"]
    gate = threading.Event()
    applied = []
    calls = []

    def build_job(size, strength):
        title = "old" if not calls else "new"
        calls.append(title)

        def work(token, progress):
            if title == "old":
                gate.wait(5)
            return _fake_lut(title)

        return work

    monkeypatch.setattr(look, "build_precondition", lambda: None)
    monkeypatch.setattr(look, "build_job", build_job)
    original = look._built
    monkeypatch.setattr(look, "_built", lambda lut: (applied.append(lut.title), original(lut)))
    monkeypatch.setattr(win.ctx.engine, "render_preview", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    look.rebuild()
    qtbot.wait(50)
    look.settings_changed()  # user changes a setting while "old" is running
    gate.set()
    win.ctx.jobs.pool.waitForDone(5000)
    qtbot.wait(20)
    assert look.lut is None and not look.export_button.isEnabled()  # old result never applied
    idle(qtbot, win)
    assert applied == ["new"] and look.lut.title == "new"


def test_stale_preview_render_cannot_paint_after_invalidation(qtbot, win, fx, monkeypatch):
    import threading

    look = win.workspaces["look"]
    look.preview.load(str(fx["chart"]))
    idle(qtbot, win)
    gate = threading.Event()
    src = look.preview.source

    def slow_render(image, lut, token):
        gate.wait(5)
        return src.array

    monkeypatch.setattr(win.ctx.engine, "render_preview", slow_render)
    look.preview.render(_fake_lut("old"))
    qtbot.wait(50)
    unsupported = _fake_lut("camera")
    unsupported.input_kind = "camera_linear"
    look.preview.render(unsupported)  # switch to an unpreviewable LUT
    gate.set()
    win.ctx.jobs.pool.waitForDone(5000)
    qtbot.wait(100)
    assert look.preview.viewer._after is None and not look.preview.viewer.is_comparing()
