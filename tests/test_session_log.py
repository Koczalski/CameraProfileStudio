"""Opt-in session diagnostics for hands-on testing (TestRun.bat sets CPS_SESSION_LOG).

The detector and the file writer need no Qt; the attachment to a real window is a gui test.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from camera_profile_studio import __version__
from camera_profile_studio.gui import session_log


def test_stall_is_reported_once_and_recovery_gives_its_length():
    d = session_log.StallDetector(threshold=2.0)
    d.tick(0.0)
    assert d.check(1.9) is None
    assert d.check(2.5) == pytest.approx(2.5)
    assert d.check(3.0) is None                       # one report per stall, not one per check
    assert d.tick(4.0) == pytest.approx(4.0)          # the GUI thread is back: how long it was away
    assert d.tick(4.2) is None
    assert d.check(4.5) is None
    assert d.check(7.0) == pytest.approx(2.8)         # a later stall is reported again


def test_lines_are_on_disk_as_soon_as_they_are_written(tmp_path):
    session = session_log.SessionLog(tmp_path / "logs", stamp="t")
    session.write_header()
    session.write("hello from the test", "WARN")
    text = session.path.read_text(encoding="utf-8")   # read while the file is still open: nothing waits for close()
    assert f"Camera Profile Studio {__version__}" in text
    assert "WARN" in text and "hello from the test" in text
    session.close()
    assert "session end" in session.path.read_text(encoding="utf-8")
    session.close()                                    # closing twice is harmless


def test_nothing_starts_without_the_environment_variable(monkeypatch):
    monkeypatch.delenv(session_log.ENV, raising=False)
    assert session_log.start_from_env(None) is None


@pytest.mark.gui
def test_log_panel_entries_and_finished_jobs_reach_the_file(qtbot, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from camera_profile_studio.gui.app import create_application
    from camera_profile_studio.gui.i18n import T
    from camera_profile_studio.gui.main_window import MainWindow

    create_application([])
    T.set_language("en")
    win = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    qtbot.addWidget(win)
    win.show()
    session = session_log.start(win, tmp_path / "logs", crash_dump=False, tee=False)
    try:
        win.ctx.log.warning("log.cancelled", detail="marker-from-log-panel")
        win.ctx.jobs.submit("diag:test", "job.preview", lambda token, progress: 1)

        def failing(token, progress):
            raise ValueError("boom-from-job")

        win.ctx.jobs.submit("diag:fail", "job.preview", failing, on_error=lambda err: None)
        qtbot.waitUntil(lambda: not win.ctx.jobs.active(), timeout=10000)
        qtbot.wait(100)
        text = session.path.read_text(encoding="utf-8")
        assert "marker-from-log-panel" in text
        assert "diag:test" in text and "succeeded" in text.lower()
        assert "diag:fail" in text and "failed" in text.lower()
        assert "boom-from-job" in text and "Traceback" in text    # the failed job's cause and where it happened
    finally:
        session.close()
        win._force_close = True
        win.close()
        win.ctx.jobs.shutdown(10000)
