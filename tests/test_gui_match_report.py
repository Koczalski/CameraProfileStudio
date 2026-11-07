"""Camera Match page: per-patch error table and the bake error of the exported LUT (offscreen Qt).

Synthetic patch CSVs drive the real engine; no sample profile is needed.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PySide6.QtCore import QSettings, Qt  # noqa: E402

DISPLAY = Qt.ItemDataRole.DisplayRole


@pytest.fixture()
def win(qtbot, tmp_path):
    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.i18n import T

    create_application([])
    T.set_language("en")
    from camera_profile_studio.gui.main_window import MainWindow

    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    if not w.ctx.engine.available:
        pytest.fail(f"engine unavailable: {w.ctx.engine.unavailable_reason}")
    w.errors = []
    w.ctx.show_error = lambda err, title_key="": w.errors.append(err)
    w.ctx.confirm = lambda *a, **k: True
    qtbot.addWidget(w)
    w.resize(1280, 820)
    w.show()
    w.show_workspace("match")
    yield w
    w._force_close = True
    w.close()
    w.ctx.jobs.shutdown(10000)
    T.set_language("en")


def idle(qtbot, w, timeout=120000):
    def done():
        if w.ctx.jobs.active():
            return False
        return not any(getattr(ws, "_debounce", None) is not None and ws._debounce.isActive() for ws in w.workspaces.values())

    qtbot.wait(50)
    qtbot.waitUntil(done, timeout=timeout)


def patch_csv(path: Path, n: int = 96) -> Path:
    rng = np.random.default_rng(11)
    source = rng.uniform(0.05, 0.95, (n, 3))
    target = np.clip(source ** np.array([1.15, 0.9, 1.05]) * [0.95, 1.0, 0.9] + 0.03 * np.sin(np.pi * source[:, ::-1]), 0, 1)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["source_r", "source_g", "source_b", "target_r", "target_g", "target_b", "group"])
        for i in range(n):
            writer.writerow([*(f"{v:.6f}" for v in source[i]), *(f"{v:.6f}" for v in target[i]), f"chart:{i // 12}"])
    return path


def fitted(qtbot, win, tmp_path, n=96):
    ws = win.workspaces["match"]
    ws.set_csv(str(patch_csv(tmp_path / "patches.csv", n)))
    ws.evidence.setText("synthetic chart, GUI test")
    assert ws.fit_button.isEnabled(), ws.fit_reason_label.text()
    ws._fit()
    idle(qtbot, win)
    assert ws.outcome is not None and ws.lut is not None and not win.errors
    return ws


def column(dialog, header: str) -> int:
    model = dialog.table.model()
    names = [str(model.headerData(c, Qt.Orientation.Horizontal, DISPLAY)) for c in range(model.columnCount())]
    assert header in names, names
    return names.index(header)


def values(dialog, header: str) -> list:
    model, col = dialog.table.model(), column(dialog, header)
    return [model.data(model.index(r, col), DISPLAY) for r in range(model.rowCount())]


# ------------------------------------------------------------------ bake error


def test_fit_reports_the_bake_error_for_every_grid_size(qtbot, win, tmp_path):
    ws = fitted(qtbot, win, tmp_path)
    bake = ws.outcome.bake
    assert set(bake) == {17, 33, 65}
    for size, entry in bake.items():
        assert entry["size"] == size and entry["strength"] == 1.0
        assert entry["uniform"]["count"] == 30000 and entry["points"]["count"] == ws.outcome.holdout_count
        assert 0 <= entry["uniform"]["mean"] <= entry["uniform"]["max"]
    assert bake[17]["uniform"]["mean"] > bake[65]["uniform"]["mean"]
    text = ws.report_label.text()
    for size in (17, 33, 65):
        assert f"{size}³" in text
    assert f"{bake[33]['uniform']['max']:.2f}" in text        # the current size is spelled out


def test_size_and_strength_changes_refresh_the_baked_stage_and_the_bake_error(qtbot, win, tmp_path):
    ws = fitted(qtbot, win, tmp_path)
    patches = ws.outcome.patches
    assert patches.stages[-1] == "baked" and patches.delta_e00.shape[1] == 96
    baked_33 = np.array(patches.predicted_rgb[-1])
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(17))
    idle(qtbot, win)
    assert ws.lut.size == 17 and ws.outcome.lut.size == 17
    assert not np.allclose(ws.outcome.patches.predicted_rgb[-1], baked_33, atol=1e-5)   # re-baked on the new grid
    np.testing.assert_array_equal(ws.outcome.patches.predicted_rgb[:4], patches.predicted_rgb[:4])  # model stages kept
    ws.strength_spin.setValue(50)
    idle(qtbot, win)
    assert ws.lut.strength == 0.5
    assert all(entry["strength"] == 0.5 for entry in ws.outcome.bake.values())
    assert "17³" in ws.report_label.text()


# ------------------------------------------------------------------ patch table


def test_patch_table_lists_every_sample_worst_first(qtbot, win, tmp_path):
    from camera_profile_studio.gui.i18n import tr

    ws = fitted(qtbot, win, tmp_path)
    assert ws.patch_table_button.isEnabled()
    dialog = ws.open_patch_table()
    assert dialog is not None and dialog.isVisible()
    assert dialog.stage_combo.currentData() == "baked"
    model = dialog.table.model()
    assert model.rowCount() == 96
    de = [float(v) for v in values(dialog, "ΔE00")]
    assert de == sorted(de, reverse=True)
    sets = values(dialog, tr("match.patches.col.set"))
    assert sets.count(tr("match.patches.set.holdout")) == ws.outcome.holdout_count
    assert sets.count(tr("match.patches.set.train")) == ws.outcome.train_count
    baked = ws.outcome.patches.delta_e00[-1]                            # the table shows the record's baked stage
    assert np.allclose(sorted(baked, reverse=True), de, atol=0.0051)   # values are shown with two decimals
    indices = [int(v) for v in values(dialog, "#")]
    assert sorted(indices) == list(range(96))
    # another stage changes the numbers; the identity stage is the "before" error
    dialog.stage_combo.setCurrentIndex(dialog.stage_combo.findData("before"))
    before = [float(v) for v in values(dialog, "ΔE00")]
    assert before != de and before == sorted(before, reverse=True)
    held_before = [d for d, s in zip(before, values(dialog, tr("match.patches.col.set"))) if s == tr("match.patches.set.holdout")]
    assert abs(sum(held_before) / len(held_before) - ws.outcome.before["mean"]) < 1e-3
    for header in ("ΔL*", "ΔC*", "ΔH*"):
        assert all(isinstance(float(v), float) for v in values(dialog, header))
    dialog.close()


def test_patch_table_saves_csv_and_json_through_the_save_dialog(qtbot, win, tmp_path):
    ws = fitted(qtbot, win, tmp_path)
    dialog = ws.open_patch_table()
    out = tmp_path / "out"
    out.mkdir()
    for kind in ("csv", "json"):
        target = out / f"patches.{kind}"
        win.ctx.ask_save_typed = lambda *a, _t=target, _k=kind, **k: (str(_t), _k)
        dialog.save_button.click()
        idle(qtbot, win)
        assert target.exists(), kind
    with (out / "patches.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 96 and {"index", "group", "set", "baked_de00", "residual_dH"} <= set(rows[0])
    table = json.loads((out / "patches.json").read_text(encoding="utf-8"))
    assert table["stages"] == ["before", "ccm", "tone", "residual", "baked"] and len(table["groups"]) == 96
    assert not win.errors
    assert win.ctx.log_model.entries[-1].level == "success"
    dialog.close()


def test_patch_table_follows_a_refit_and_the_language(qtbot, win, tmp_path):
    from camera_profile_studio.gui.i18n import T, tr

    ws = fitted(qtbot, win, tmp_path)
    dialog = ws.open_patch_table()
    assert dialog.table.model().rowCount() == 96
    ws.set_csv(str(patch_csv(tmp_path / "smaller.csv", 48)))
    assert not ws.patch_table_button.isEnabled()          # the previous fit no longer applies
    ws._fit()
    idle(qtbot, win)
    assert dialog.table.model().rowCount() == 48           # the open table shows the new fit
    T.set_language("ja")
    assert dialog.windowTitle() == tr("match.patches.title")
    assert column(dialog, tr("match.patches.col.set")) >= 0
    dialog.close()


def test_a_lut_build_started_before_a_refit_cannot_overwrite_the_new_fit(qtbot, win, tmp_path, monkeypatch):
    import threading

    ws = fitted(qtbot, win, tmp_path)
    gate = threading.Event()
    original = win.ctx.engine.match_diagnostics

    def slow(*args, **kwargs):                 # only the rebuild of a size change goes through here
        gate.wait(30)
        return original(*args, **kwargs)

    monkeypatch.setattr(win.ctx.engine, "match_diagnostics", slow)
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(17))
    qtbot.waitUntil(lambda: win.ctx.jobs.is_busy("match:lut"), timeout=10000)
    ws._fit()                                  # same inputs, fitted again while the old build is still running
    qtbot.waitUntil(lambda: not win.ctx.jobs.is_busy("match:fit"), timeout=60000)
    second_lut, second_patches = ws.lut, ws.outcome.patches
    assert second_lut is not None and second_patches is not None
    gate.set()
    idle(qtbot, win)
    assert ws.lut is second_lut and ws.outcome.lut is second_lut
    assert ws.outcome.patches is second_patches


def test_export_records_the_bake_error_in_the_sidecar(qtbot, win, tmp_path):
    ws = fitted(qtbot, win, tmp_path)
    out = tmp_path / "match.cube"
    ws.export_to(str(out), False)
    idle(qtbot, win)
    assert out.exists() and not win.errors
    sidecar = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    bake = sidecar["bake_error"]
    assert set(bake) == {"17", "33", "65"}
    for size, entry in bake.items():
        assert entry["size"] == int(size) and entry["strength"] == 1.0
        for part in ("uniform", "points"):
            assert set(entry[part]) == {"count", "mean", "median", "p95", "maximum"}   # the CLI's shape
        assert entry["points"]["count"] == ws.outcome.holdout_count
    assert bake["33"]["uniform"]["maximum"] == pytest.approx(ws.outcome.bake[33]["uniform"]["max"])


# ------------------------------------------------------------------ reset


def test_reset_returns_the_page_to_its_initial_state(qtbot, win, tmp_path):
    from camera_profile_studio.gui.i18n import tr

    ws = fitted(qtbot, win, tmp_path)
    ws.reg.setValue(0.5)
    ws.seed.setValue(7)
    ws.grid.setCurrentIndex(ws.grid.findData(5))
    ws.fit_combo.setCurrentIndex(ws.fit_combo.findData("ccm"))
    asked = []
    win.ctx.confirm = lambda *a, **k: asked.append(a) or True
    ws.reset_button.click()
    idle(qtbot, win)
    assert len(asked) == 1
    assert ws.model is None and ws.outcome is None and ws.lut is None
    assert ws.csv_path == "" and ws.source is None and ws.target is None and ws.evidence.text() == ""
    assert ws.rb_pair.isChecked() and not ws.same_scene.isChecked() and not ws.prealigned.isChecked()
    assert (ws.reg.value(), ws.holdout.value(), ws.seed.value()) == (0.01, 0.2, 42)
    assert ws.grid.currentData() == 9 and ws.fit_combo.currentData() == "ccm_tone_lut"
    assert ws.report_label.text() == tr("match.report.none")
    assert not ws.patch_table_button.isEnabled() and not ws.fit_button.isEnabled()
    assert not win.errors


def test_declining_the_reset_confirmation_keeps_everything(qtbot, win, tmp_path):
    ws = fitted(qtbot, win, tmp_path)
    outcome = ws.outcome
    win.ctx.confirm = lambda *a, **k: False
    ws.reset_button.click()
    idle(qtbot, win)
    assert ws.outcome is outcome and ws.lut is not None and ws.csv_path and ws.evidence.text()


def test_match_page_says_what_it_needs_and_points_to_image_look(win):
    from camera_profile_studio.gui.i18n import tr

    ws = win.workspaces["match"]
    assert ws.intro_label.isVisible()
    for text in (ws.intro_label.text(), ws.ref_reason.text()):
        assert "Image Look Creator" in text
    assert "chart" in ws.intro_label.text() and "same-scene" in ws.intro_label.text()
    assert not ws.rb_ref.isEnabled()                       # the unsupported mode stays, with its reason
    assert tr("nav.imagelook") in ws.intro_label.text()


def test_no_table_without_a_fit(win):
    ws = win.workspaces["match"]
    assert not ws.patch_table_button.isEnabled()
    assert ws.open_patch_table() is None
