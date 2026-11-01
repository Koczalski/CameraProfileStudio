"""GUI infrastructure tests: i18n coverage, style fallback, job queue
semantics (generations, independent jobs, side-effect publication, cancel),
viewer keyboard handling.  Headless (offscreen) by default."""

from __future__ import annotations

import os
import re
import string
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

from camera_profile_studio.gui import strings  # noqa: E402
from camera_profile_studio.gui.app import preferred_style  # noqa: E402
from camera_profile_studio.gui.i18n import T, strip_mnemonic  # noqa: E402
from camera_profile_studio.gui.jobs import JobCancelled, JobManager, JobState  # noqa: E402

GUI_SRC = Path(__file__).resolve().parents[1] / "src" / "camera_profile_studio" / "gui"

# Dynamic key families built with f-strings in the GUI code.
DYNAMIC = {
    "cap.{}": ["supported", "partial", "unsupported", "available", "missing", "verified", "unverified", "unknown", "none"],
    "job.state.{}": [s.value for s in JobState],
    "profile.state.{}": ["loading", "ready", "failed"],
    "batch.state.{}": ["pending", "queued", "running", "succeeded", "failed", "cancelled", "skipped"],
    "space.{}": ["srgb", "linear_srgb", "icc_rgb", "camera_linear"],
    "intent.{}": ["perceptual", "relative", "saturation", "absolute"],
    "gamut.{}": ["clip", "error"],
    "look.mode.{}": ["colors_only", "tone_only", "look_only", "combined"],
    "match.fit.{}": ["ccm", "ccm_tone", "ccm_tone_lut"],
    "settings.theme.{}": ["system", "light", "dark"],
    "nav.{}": ["inspector", "converter", "look", "match", "batch", "settings", "imagelook"],
    "{}.subtitle": ["inspector", "converter", "look", "match", "batch", "settings", "imagelook"],
    "severity.{}": ["warning", "info", "error"],
    "log.level.{}": ["info", "success", "warning", "error"],
    "look.component.{}": ["matrices", "hue_sat_map", "look_table", "tone_curve"],
    "conv.wb.neutral_{}": ["r", "g", "b"],
    # Image Look Creator families (workspaces/image_look.py, widgets/roi_editor.py)
    "imagelook.warning.{}": ["approximate_unverified", "limited_luminance_coverage", "limited_color_coverage",
                             "insufficient_samples", "reference_clipping", "output_clipping", "conflicting_tone",
                             "conflicting_color", "conflicting_saturation", "conflicts_acknowledged",
                             "typical_photo_baseline"],
    "imagelook.conflict.{}": ["conflicting_tone", "conflicting_color", "conflicting_saturation"],
    "imagelook.ref_state.{}": ["queued", "loading", "ready", "failed", "cancelled"],
    "imagelook.amount.{}": ["tone_amount", "color_amount", "saturation_amount"],
    "imagelook.amount_tip.{}": ["tone_amount", "color_amount", "saturation_amount"],
    "imagelook.role.{}": ["reference", "base"],
    "imagelook.roi.{}": ["x0", "y0", "x1", "y1"],
    "imagelook.sel.band.{}": ["shadows", "midtones", "highlights"],
    "error.code.{}": ["image_look_invalid", "image_look_baseline", "image_look_unusable", "image_look_conflict",
                      "image_look_source_changed", "image_look_source_io"],
}


def _fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def test_languages_have_same_keys_and_placeholders():
    assert set(strings.EN) == set(strings.JA)
    for key in strings.EN:
        assert _fields(strings.EN[key]) == _fields(strings.JA[key]), key


def test_every_referenced_key_is_translated():
    literal = re.compile(r"[\"']((?:[a-z_]+)(?:\.[a-z0-9_]+)+)[\"']")
    prefixes = ("action.", "advanced.", "app.", "batch.", "cap.", "conv.", "dialog.", "error.", "filter.", "inspector.",
                "job.", "jobs.", "log.", "look.", "lut.", "match.", "menu.", "meta.", "nav.", "panel.", "path.",
                "picker.", "preview.", "profiles.", "progress.", "reason.", "settings.", "status.", "usage.", "viewer.",
                "about.", "imagelook.")
    missing = set()
    for path in GUI_SRC.rglob("*.py"):
        if path.name == "strings.py":
            continue
        for key in literal.findall(path.read_text(encoding="utf-8")):
            if key.startswith(prefixes) and key not in strings.EN and not key.endswith(".cube") and key != "look.component":
                missing.add(f"{path.name}:{key}")
    for template, values in DYNAMIC.items():
        for v in values:
            if template.format(v) not in strings.EN:
                missing.add(template.format(v))
    assert not missing, sorted(missing)


def test_strip_mnemonic():
    assert strip_mnemonic("&File") == "File"
    assert strip_mnemonic("\u30d5\u30a1\u30a4\u30eb(&F)") == "\u30d5\u30a1\u30a4\u30eb"
    assert strip_mnemonic("A && B") == "A & B"


def test_style_preference_and_fallback():
    assert preferred_style(["windows11", "windowsvista", "Fusion"]) == "windows11"
    assert preferred_style(["windowsvista", "Windows", "Fusion"]) == "windowsvista"
    assert preferred_style(["Windows", "Fusion"]) == "Fusion"
    assert preferred_style([]) == "Fusion"


def test_live_language_switch_rebinds(qtbot):
    from PySide6.QtWidgets import QLabel

    label = QLabel()
    qtbot.addWidget(label)
    T.set_language("en")
    T.bind(label, "setText", "nav.settings")
    assert label.text() == "Settings"
    T.set_language("ja")
    assert label.text() == strings.JA["nav.settings"]
    T.set_language("en")
    assert label.text() == "Settings"


# ----------------------------------------------------------------- jobs


def _wait(qtbot, jm: JobManager, timeout=5000):
    qtbot.waitUntil(lambda: not jm.active(), timeout=timeout)


def test_superseded_job_result_is_dropped(qtbot):
    jm = JobManager()
    gate = threading.Event()
    got = []

    def slow(token, progress):
        gate.wait(2)
        return "old"

    first = jm.submit("preview", "job.preview", slow, on_result=got.append)
    second = jm.submit("preview", "job.preview", lambda t, p: "new", on_result=got.append)
    gate.set()
    _wait(qtbot, jm)
    assert got == ["new"]
    assert jm.records[first].state is JobState.SUPERSEDED
    assert jm.records[second].state is JobState.SUCCEEDED
    jm.shutdown()


def test_independent_jobs_on_same_channel_both_deliver(qtbot):
    jm = JobManager()
    got = []
    gate = threading.Event()

    def work(name):
        def fn(token, progress):
            gate.wait(2)
            return name
        return fn

    a = jm.submit("export", "job.export", work("a"), on_result=got.append, supersede=False)
    b = jm.submit("export", "job.export", work("b"), on_result=got.append, supersede=False)
    gate.set()
    _wait(qtbot, jm)
    assert sorted(got) == ["a", "b"]
    assert jm.records[a].state is JobState.SUCCEEDED and jm.records[b].state is JobState.SUCCEEDED
    # a later superseding job must not hide independent ones either
    c = jm.submit("export", "job.export", lambda t, p: "c", on_result=got.append)
    _wait(qtbot, jm)
    assert jm.records[c].state is JobState.SUCCEEDED
    jm.shutdown()


def test_side_effect_job_reports_commit_after_late_cancel(qtbot):
    """Export committed files, then the user cancelled: report success with paths."""
    jm = JobManager()
    started, release = threading.Event(), threading.Event()
    got, cancelled = [], []

    def export(token, progress):
        started.set()
        release.wait(2)
        return {"cube": "x.cube"}  # engine already published

    jid = jm.submit("look:export", "job.export", export, on_result=got.append,
                    on_cancel=lambda: cancelled.append(True), side_effects=True)
    assert started.wait(2)
    jm.cancel(jid)
    release.set()
    _wait(qtbot, jm)
    assert got == [{"cube": "x.cube"}] and not cancelled
    assert jm.records[jid].state is JobState.SUCCEEDED
    jm.shutdown()


def test_side_effect_job_reports_real_error_despite_cancel(qtbot):
    jm = JobManager()
    started, release = threading.Event(), threading.Event()
    errors = []

    class PartialPublication(Exception):
        code = "partial_publication"
        message = "cube written, json failed"
        details = (("cube", "x.cube"),)

    def export(token, progress):
        started.set()
        release.wait(2)
        raise PartialPublication("cube written, json failed")

    jm.error_mapper = __import__("camera_profile_studio.gui.backend", fromlist=["Engine"]).Engine().map_error
    jid = jm.submit("look:export", "job.export", export, on_error=errors.append, side_effects=True)
    assert started.wait(2)
    jm.cancel(jid)
    release.set()
    _wait(qtbot, jm)
    assert jm.records[jid].state is JobState.FAILED
    assert errors and "json failed" in errors[0].detail
    jm.shutdown()


def test_non_side_effect_late_cancel_is_cancelled(qtbot):
    jm = JobManager()
    started, release = threading.Event(), threading.Event()
    got = []

    def build(token, progress):
        started.set()
        release.wait(2)
        return "lut"

    jid = jm.submit("lut", "job.build_lut", build, on_result=got.append)
    assert started.wait(2)
    jm.cancel(jid)
    release.set()
    _wait(qtbot, jm)
    assert not got and jm.records[jid].state is JobState.CANCELLED
    jm.shutdown()


def test_engine_cancelled_error_maps_to_cancelled(qtbot):
    from camera_profile_studio.errors import CancelledError

    jm = JobManager()

    def fn(token, progress):
        raise CancelledError("stop", code="cancelled")

    jid = jm.submit("x", "job.build_lut", fn)
    _wait(qtbot, jm)
    assert jm.records[jid].state is JobState.CANCELLED
    jm.shutdown()


def test_progress_raises_when_cancelled_for_non_side_effect(qtbot):
    jm = JobManager()
    started = threading.Event()

    def fn(token, progress):
        started.set()
        for _ in range(200):
            progress(0.1, "")
            time.sleep(0.01)
        return "done"

    jid = jm.submit("x", "job.build_lut", fn)
    assert started.wait(2)
    jm.cancel(jid)
    _wait(qtbot, jm)
    assert jm.records[jid].state is JobState.CANCELLED
    jm.shutdown()


def test_shutdown_reports_running_workers(qtbot):
    jm = JobManager()
    release = threading.Event()
    jm.submit("native", "job.build_lut", lambda t, p: release.wait(3) and "x")
    time.sleep(0.1)
    assert jm.shutdown(50) is False  # still running: caller must not destroy
    release.set()
    assert jm.shutdown(3000) is True


# --------------------------------------------------------------- viewer


def test_viewer_keyboard_zoom_pan_split(qtbot):
    from camera_profile_studio.gui.widgets.viewer import CompareMode, ImageViewer

    v = ImageViewer()
    qtbot.addWidget(v)
    v.resize(400, 300)
    v.show()
    img = QImage(800, 600, QImage.Format.Format_RGB888)
    img.fill(Qt.GlobalColor.gray)
    v.set_images(img, img)
    fit = v.zoom()
    qtbot.keyClick(v, Qt.Key.Key_Plus)
    assert v.zoom() > fit
    qtbot.keyClick(v, Qt.Key.Key_1)
    assert v.zoom() == pytest.approx(1.0)
    before = v._center.x()
    qtbot.keyClick(v, Qt.Key.Key_Right)
    assert v._center.x() > before
    qtbot.keyClick(v, Qt.Key.Key_BracketLeft)
    assert v.split_position() == pytest.approx(0.45)
    qtbot.keyClick(v, Qt.Key.Key_D)
    assert v.mode() is CompareMode.SIDE_BY_SIDE
    qtbot.keyClick(v, Qt.Key.Key_S)
    qtbot.keyClick(v, Qt.Key.Key_0)
    assert v._fit and v.zoom() == pytest.approx(fit)




def test_invalidate_drops_already_queued_success(qtbot):
    """A worker finished (signal queued) before invalidate: result must be dropped."""
    jm = JobManager()
    done = threading.Event()
    got = []

    def fn(token, progress):
        return "old"

    jid = jm.submit("look:lut", "job.build_lut", fn, on_result=got.append)
    jm.pool.waitForDone(2000)  # worker returned; its signal is queued, not delivered
    jm.invalidate("look:lut")
    qtbot.wait(100)
    assert got == []
    assert jm.records[jid].state is JobState.SUPERSEDED
    jm.shutdown()


def test_viewer_single_image_is_not_a_comparison(qtbot):
    from camera_profile_studio.gui.widgets.viewer import ImageViewer

    v = ImageViewer()
    qtbot.addWidget(v)
    v.resize(300, 200)
    img = QImage(100, 80, QImage.Format.Format_RGB888)
    img.fill(Qt.GlobalColor.gray)
    v.set_images(img, None)
    assert v.has_image() and not v.is_comparing()
    v.grab()  # paints the Before-only path without error
    v.set_images(img, img)
    assert v.is_comparing()
