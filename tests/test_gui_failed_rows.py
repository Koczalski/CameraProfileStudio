"""Removing the rows that could not be read, all at once, on Profile Inspector and Batch Processing.

Uses damaged files and rows placed directly in the models, so no sample profile is needed.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PySide6.QtCore import QSettings  # noqa: E402


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
    w.ctx.confirm = lambda *a, **k: True
    qtbot.addWidget(w)
    w.resize(1280, 820)
    w.show()
    yield w
    w._force_close = True
    w.close()
    w.ctx.jobs.shutdown(10000)
    T.set_language("en")


def idle(qtbot, w, timeout=60000):
    qtbot.wait(50)
    qtbot.waitUntil(lambda: not w.ctx.jobs.active(), timeout=timeout)


def damaged(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b"not a profile")
    return path


# ---------------------------------------------------------------- Profile Inspector


def test_inspector_removes_unreadable_profiles_and_keeps_the_rest(qtbot, win, tmp_path):
    from camera_profile_studio.gui.context import ProfileEntry

    ws = win.workspaces["inspector"]
    assert ws.remove_failed_button.isHidden()
    bad = [damaged(tmp_path, "bad1.icc"), damaged(tmp_path, "bad2.dcp")]
    ws.open_paths([str(p) for p in bad])
    idle(qtbot, win)
    profiles = win.ctx.profiles
    assert [e.state for e in profiles.entries] == ["failed", "failed"]
    good = ProfileEntry(str(tmp_path / "good.icc"), state="loading")
    profiles.upsert(good)
    assert not ws.remove_failed_button.isHidden() and "2" in ws.remove_failed_button.text()
    ws.remove_failed_button.click()
    assert profiles.entries == [good]
    assert ws.remove_failed_button.isHidden()
    assert all(p.read_bytes() == b"not a profile" for p in bad)    # only the rows go, never the files
    last = win.ctx.log_model.entries[-1]
    assert last.key == "log.profile.removed_failed" and last.kwargs["n"] == 2
    assert "bad1.icc" in last.detail and "bad2.dcp" in last.detail


def test_inspector_current_profile_moves_when_it_is_removed(qtbot, win, tmp_path):
    from camera_profile_studio.gui.context import ProfileEntry

    ws = win.workspaces["inspector"]
    ws.open_paths([str(damaged(tmp_path, "bad.icc"))])
    idle(qtbot, win)
    profiles = win.ctx.profiles
    assert win.ctx.current_profile is profiles.entries[0]
    good = ProfileEntry(str(tmp_path / "good.icc"), state="loading")
    profiles.upsert(good)
    ws.remove_failed_button.click()
    assert win.ctx.current_profile is good


def test_inspector_with_nothing_left_has_no_current_profile(qtbot, win, tmp_path):
    ws = win.workspaces["inspector"]
    ws.open_paths([str(damaged(tmp_path, "bad.icc"))])
    idle(qtbot, win)
    ws.remove_failed_button.click()
    assert win.ctx.profiles.entries == [] and win.ctx.current_profile is None


def opened(qtbot, win, tmp_path, *names):
    """Open real sRGB profiles and, for names starting with "bad", damaged files; returns entries by name."""
    from PIL import ImageCms

    paths = []
    for name in names:
        path = tmp_path / name
        if name.startswith("bad"):
            damaged(tmp_path, name)
        else:
            path.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        paths.append(str(path))
    ws = win.workspaces["inspector"]
    ws.open_paths(paths)
    idle(qtbot, win)
    entries = {e.name: e for e in win.ctx.profiles.entries}
    assert [entries[n].state for n in names] == ["failed" if n.startswith("bad") else "ready" for n in names]
    return ws, entries


def select_row(ws, row):
    ws.table.setCurrentIndex(ws.ctx.profiles.index(row, 0))


def table_entry(win, ws):
    return win.ctx.profiles.entry(ws.table.currentIndex().row())


def test_inspector_moves_to_the_row_after_the_removed_current_and_table_agrees(qtbot, win, tmp_path):
    ws, e = opened(qtbot, win, tmp_path, "bad0.icc", "r1.icc", "bad2.icc", "r3.icc")
    select_row(ws, 2)
    assert win.ctx.current_profile is e["bad2.icc"]
    ws.remove_failed_button.click()
    assert win.ctx.profiles.entries == [e["r1.icc"], e["r3.icc"]]
    assert win.ctx.current_profile is e["r3.icc"]       # the row that followed it, as with the Remove button
    assert table_entry(win, ws) is e["r3.icc"]


def test_inspector_keeps_a_ready_current_profile_without_switching_away(qtbot, win, tmp_path):
    # The Converter refills its settings when the current profile changes, so even a brief switch would lose them.
    ws, e = opened(qtbot, win, tmp_path, "a.icc", "bad.icc", "c.icc")
    select_row(ws, 1)                                   # the table is on the failed row...
    win.ctx.set_current_profile(e["a.icc"])             # ...while another page has picked this profile
    seen = []
    win.ctx.current_profile_changed.connect(seen.append)
    ws.remove_failed_button.click()
    assert seen == [] and win.ctx.current_profile is e["a.icc"]
    assert win.ctx.profiles.entries == [e["a.icc"], e["c.icc"]]
    assert table_entry(win, ws) is e["a.icc"]


def test_inspector_ready_current_row_stays_selected(qtbot, win, tmp_path):
    ws, e = opened(qtbot, win, tmp_path, "bad.icc", "r.icc")
    select_row(ws, 1)
    ws.remove_failed_button.click()
    assert win.ctx.current_profile is e["r.icc"] and table_entry(win, ws) is e["r.icc"]


def test_inspector_does_not_restore_an_entry_that_left_the_list(qtbot, win, tmp_path):
    # Opening a path again replaces its row; the page still holds the old entry as the current profile.
    ws, e = opened(qtbot, win, tmp_path, "x.icc")
    old = e["x.icc"]
    select_row(ws, 0)
    assert win.ctx.current_profile is old
    damaged(tmp_path, "x.icc")
    ws.open_paths([str(tmp_path / "x.icc")])
    idle(qtbot, win)
    assert win.ctx.profiles.entries[0] is not old and win.ctx.profiles.entries[0].state == "failed"
    ws.remove_failed_button.click()
    assert win.ctx.profiles.entries == [] and win.ctx.current_profile is None


# ----------------------------------------------------------------- Batch Processing


@pytest.fixture()
def rows(win, tmp_path, monkeypatch):
    """One row per state, placed directly: the verdict logic is the subject, not the engine."""
    from camera_profile_studio.gui.jobs import ErrorInfo

    ws = win.workspaces["batch"]
    monkeypatch.setattr(ws, "_evaluate", lambda item, s=None: None)     # keep the blockers set below
    names = {"failed": {"state": "failed"}, "skipped": {"state": "skipped"},
             "unreadable": {"check_error": ErrorInfo("error.file")},
             "blocked": {"info": object(), "blocker": ("blocked", "batch.blocked.duplicate_output", {})},
             "needs_input": {"info": object(), "blocker": ("input", "batch.blocked.intent", {})},
             "checking": {}, "ready": {"info": object()},
             "succeeded": {"state": "succeeded", "info": object()}, "cancelled": {"state": "cancelled", "info": object()}}
    ws.model.add([str(tmp_path / f"{name}.icc") for name in names])
    for item in ws.model.items:
        for key, value in names[Path(item.path).stem].items():
            setattr(item, key, value)
    ws._update()
    yield ws
    ws.model.clear()     # the placeholder rows must not outlive the replacement above


def test_batch_shows_no_removal_without_rows(win):
    assert win.workspaces["batch"].remove_failed_button.isHidden()


def test_batch_removes_only_rows_that_could_not_run(win, rows):
    ws = rows
    assert {it.display_state() for it in ws.model.items} == {
        "failed", "skipped", "unreadable", "blocked", "needs_input", "checking", "pending", "succeeded", "cancelled"}
    assert not ws.remove_failed_button.isHidden() and "4" in ws.remove_failed_button.text()
    ws.remove_failed_button.click()
    assert {it.display_state() for it in ws.model.items} == {"needs_input", "checking", "pending", "succeeded", "cancelled"}
    assert ws.remove_failed_button.isHidden()
    last = win.ctx.log_model.entries[-1]
    assert last.key == "log.batch.removed_failed" and last.kwargs["n"] == 4


def test_batch_needs_input_alone_offers_no_removal(rows):
    # every row is in this state until a setting is entered: one click must not empty the list
    ws = rows
    ws.model.remove_rows([r for r, it in enumerate(ws.model.items) if it.display_state() != "needs_input"])
    ws._update()
    assert len(ws.model.items) == 1 and ws.remove_failed_button.isHidden()


def test_batch_removal_is_disabled_while_running(rows, monkeypatch):
    ws = rows
    count = len(ws.model.items)
    monkeypatch.setattr(ws, "running", lambda: True)
    ws._update()
    assert not ws.remove_failed_button.isEnabled()
    ws._remove_failed()
    assert len(ws.model.items) == count


def test_batch_real_unreadable_file_is_flagged_and_removed(qtbot, win, tmp_path):
    ws = win.workspaces["batch"]
    bad = damaged(tmp_path, "bad.icc")
    ws.add_files([str(bad)])
    idle(qtbot, win)
    assert ws.model.items[0].display_state() == "unreadable"
    ws.remove_failed_button.click()
    assert ws.model.items == [] and bad.read_bytes() == b"not a profile"


def test_commands_follow_the_language(qtbot, win, tmp_path):
    from camera_profile_studio.gui.i18n import T, tr

    inspector, batch = win.workspaces["inspector"], win.workspaces["batch"]
    inspector.open_paths([str(damaged(tmp_path, "a.icc"))])
    batch.add_files([str(damaged(tmp_path, "b.icc"))])
    idle(qtbot, win)
    T.set_language("ja")
    assert inspector.remove_failed_button.text() == tr("action.remove_failed_profiles", n=1)
    assert batch.remove_failed_button.text() == tr("batch.remove_failed", n=1)
    assert "{n}" not in inspector.remove_failed_button.text() + batch.remove_failed_button.text()
