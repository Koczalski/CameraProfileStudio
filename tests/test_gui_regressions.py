"""Regression tests for defects found by the 0.2.0 GUI feature scan.

MA08  Camera Match crashed when a tone fit followed a CCM-only fit
M07   Ctrl+O was ambiguous on the Profile Inspector page
M12   language switches dropped button shortcuts (Ctrl+E, Esc, F5)
MB06  'Use camera white balance for RAW' off could only fail
X03   Camera Match's Cancel did not cover image loading / registration / fit
B10   batch 'ICC direction' was applied to DCP files
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "input" / "ccd_profiles"


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
    w.activateWindow()
    yield w
    T.set_language("en")
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


def _png(path: Path, w: int = 64, h: int = 48) -> Path:
    from PIL import Image

    yy, xx = np.mgrid[0:h, 0:w]
    arr = np.stack([xx / (w - 1), yy / (h - 1), 1 - xx / (w - 1)], axis=2)
    Image.fromarray((arr * 255).astype(np.uint8), "RGB").save(path)
    return path


# --------------------------------------------------------------------- MA08
def test_engine_is_preloaded_on_the_gui_thread(qtbot, tmp_path, monkeypatch):
    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.backend import Engine

    create_application([])
    calls = []
    real = Engine.preload
    monkeypatch.setattr(Engine, "preload", lambda self: (calls.append(threading.current_thread() is threading.main_thread()),
                                                         real(self)))
    from camera_profile_studio.gui.main_window import MainWindow

    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(w)
    assert calls == [True]
    assert set(Engine.MODULES) <= set(w.ctx.engine._modules)
    assert "scipy.optimize" in sys.modules


def test_refitting_with_a_tone_model_after_ccm_does_not_crash(tmp_path):
    """The defect killed the process (SIGSEGV), so run it in a child process."""
    rng = np.random.default_rng(3)
    src = rng.uniform(0.05, 0.95, (240, 3))
    tgt = np.clip(src ** 1.1 * [1.04, 1.0, 0.95], 0, 1)
    csv = tmp_path / "patches.csv"
    rows = ["source_r,source_g,source_b,target_r,target_g,target_b,group"]
    rows += [",".join(f"{v:.6f}" for v in (*s, *t)) + f",g{i % 4}" for i, (s, t) in enumerate(zip(src, tgt))]
    csv.write_text("\n".join(rows), encoding="utf-8")
    script = textwrap.dedent(f"""
        import os, sys, time
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QSettings
        from camera_profile_studio.gui.app import create_application
        app = create_application([])
        from camera_profile_studio.gui.main_window import MainWindow
        w = MainWindow(settings=QSettings({str(tmp_path / 'c.ini')!r}, QSettings.Format.IniFormat))
        m = w.workspaces["match"]
        m.set_csv({str(csv)!r}); m.evidence.setText("synthetic")
        for fit in ("ccm", "ccm_tone", "ccm_tone_lut"):
            m.fit_combo.setCurrentIndex(m.fit_combo.findData(fit))
            m._fit()
            end = time.time() + 30
            while w.ctx.jobs.is_busy("match:fit") and time.time() < end:
                app.processEvents(); time.sleep(0.01)
            assert m.outcome is not None, fit
        print("OK")
        w._force_close = True; w.close(); w.ctx.jobs.shutdown(5000)
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert r.returncode == 0 and "OK" in r.stdout, (r.returncode, r.stderr[-2000:])


# ---------------------------------------------------------------- M07 / M12
def _shortcuts(w):
    out = {}
    for key in ("converter", "look", "match", "imagelook"):
        ws = w.workspaces[key]
        out[f"{key}.export"] = ws.export_button.shortcut().toString()
        out[f"{key}.cancel"] = ws.cancel_button.shortcut().toString()
    out["imagelook.analyze"] = w.workspaces["imagelook"].analyze_button.shortcut().toString()
    return out


def test_button_shortcuts_survive_language_switches(qtbot, win):
    before = _shortcuts(win)
    assert all(before.values())
    win.act_lang.trigger()                                   # -> ja
    settings = win.workspaces["settings"]
    settings.language.setCurrentIndex(settings.language.findData("en"))
    win.act_lang.trigger()                                   # -> ja again
    assert _shortcuts(win) == before


def test_ctrl_o_opens_profiles_on_every_page(qtbot, win):
    asked = []
    win.ctx.ask_profiles = lambda: asked.append(win.stack.currentWidget().key) or []
    assert win.workspaces["inspector"].open_button.shortcut().isEmpty()   # File > Open owns Ctrl+O
    for key in win.workspaces:
        win.show_workspace(key)
        win.activateWindow()
        qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=3000)
        qtbot.keyClick(win, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    assert asked == ["inspector"] * len(win.workspaces)   # File > Open always shows the Inspector


# --------------------------------------------------------------------- MB06
def test_match_raw_uses_explicit_white_balance_when_camera_wb_is_off(qtbot, win, tmp_path, monkeypatch):
    m = win.workspaces["match"]
    engine = win.ctx.engine
    fake_raw = tmp_path / "frame.nef"
    fake_raw.write_bytes(b"not decoded: load_image is replaced below")
    png = _png(tmp_path / "decoded.png")
    recipes = []
    real_load = engine.load_image

    def load_image(path, max_side, token, progress=None, raw_recipe=None):
        recipes.append(raw_recipe)
        data = real_load(str(png), max_side, token, progress)
        data.path = str(path)
        return data

    monkeypatch.setattr(engine, "load_image", load_image)
    m.raw_wb.setChecked(False)
    assert all(s.isEnabled() for s in m.raw_wb_spins) and m.raw_wb_reason.reason_text()
    assert m.load_pair_image("source", str(fake_raw)) == -1          # refused before decoding
    assert win.errors and win.errors[-1].key == "reason.match_raw_wb_missing" and not recipes
    for spin, v in zip(m.raw_wb_spins, (2.0, 1.0, 1.5)):
        spin.setValue(v)
    m.load_pair_image("source", str(fake_raw))
    idle(qtbot, win)
    assert recipes[-1].use_camera_wb is False and recipes[-1].user_wb == (2.0, 1.0, 1.5, 1.0)
    m.raw_wb.setChecked(True)                                         # settings change -> developed again
    idle(qtbot, win)
    assert recipes[-1].use_camera_wb is True and recipes[-1].user_wb is None and len(recipes) == 2
    m.raw_wb.setChecked(False)                                        # same multipliers -> developed again
    qtbot.waitUntil(lambda: len(recipes) == 3, timeout=3000)
    idle(qtbot, win)
    m.raw_wb_spins[0].setValue(2.4)                                   # edited multiplier (debounced) -> again
    qtbot.waitUntil(lambda: len(recipes) == 4, timeout=3000)
    idle(qtbot, win)
    assert recipes[-1].user_wb == (2.4, 1.0, 1.5, 1.0) and m.fit_reason()[0] != "reason.match_raw_wb_missing"


# ---------------------------------------------------------------------- X03
def test_match_cancel_covers_image_loading(qtbot, win, tmp_path, monkeypatch):
    m = win.workspaces["match"]
    win.show_workspace("match")
    gate = threading.Event()
    real_load = win.ctx.engine.load_image

    def slow_load(path, max_side, token, progress=None, raw_recipe=None):
        gate.wait(10)
        return real_load(path, max_side, token, progress, raw_recipe=raw_recipe)

    monkeypatch.setattr(win.ctx.engine, "load_image", slow_load)
    m.evidence.setText("x")
    job = m.load_pair_image("source", str(_png(tmp_path / "a.png")))
    qtbot.waitUntil(lambda: m.cancel_button.isEnabled(), timeout=3000)
    assert m.fit_reason()[0] == "reason.match_images_loading"
    m.cancel_button.click()
    gate.set()
    idle(qtbot, win)
    assert win.ctx.jobs.records[job].state.name == "CANCELLED"
    assert m.source is None and m.src_label.path() == ""
    assert not m.cancel_button.isEnabled()


# ---------------------------------------------------------------------- B10
def test_batch_dcp_items_always_use_srgb(qtbot, win, tmp_path):
    b = win.workspaces["batch"]
    b.add_files([str(PROFILES / "Nikon_D70.icc"), str(PROFILES / "Nikon_D70s.dcp")])
    b.set_output_dir(str(tmp_path))
    b.direction.setCurrentIndex(b.direction.findData("to_linear_srgb"))
    b.size.setCurrentIndex(b.size.findData(17))
    for spin, v in zip(b.neutral, (0.45, 1.0, 0.62)):
        spin.setValue(v)
    b.kelvin.setValue(5000)
    b.run()
    idle(qtbot, win)
    states = {Path(i.path).name: (i.state, Path(i.output).name) for i in b.model.items}
    assert states["Nikon_D70.icc"] == ("succeeded", "Nikon_D70_to_linear_srgb_17.cube")
    assert states["Nikon_D70s.dcp"] == ("succeeded", "Nikon_D70s_to_srgb_17.cube")
