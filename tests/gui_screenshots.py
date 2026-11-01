"""Native Windows GUI validation driver: real user actions + screenshots.

Run one process per scale (QT_SCALE_FACTOR must be set before Qt starts):

    .venv\\Scripts\\python.exe tests\\gui_screenshots.py --scale 100
    .venv\\Scripts\\python.exe tests\\gui_screenshots.py --scale 150
    .venv\\Scripts\\python.exe tests\\gui_screenshots.py --scale 200

The host monitor scale is detected and compensated so the *effective*
device pixel ratio equals the requested scale.  The script drives the real
widgets (open/drop profiles, choose settings, load images, build LUTs, export,
fit match, batch, settings, language and theme switches) through the same
slots users trigger, waits for the background jobs and grabs every workspace
in EN/JA x Light/Dark into ``output/gui-validation/screens``.  A JSON
manifest with the observed results is written next to the images.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
OUT = ROOT / "output" / "gui-validation"


def host_scale() -> float:
    """System DPR measured in a throw-away process (before our Qt starts)."""
    import subprocess

    code = "from PySide6.QtWidgets import QApplication; a=QApplication([]); print(a.primaryScreen().devicePixelRatio())"
    env = {k: v for k, v in os.environ.items() if k != "QT_SCALE_FACTOR"}
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    return float(res.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=100)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=820)
    args = ap.parse_args()
    base = host_scale()
    os.environ["QT_SCALE_FACTOR"] = f"{args.scale / 100.0 / base:.6f}"

    import gui_fixtures

    fx = gui_fixtures.build()
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtWidgets import QApplication

    from camera_profile_studio.gui.app import apply_theme, create_application, current_color_scheme
    from camera_profile_studio.gui.i18n import T

    app = create_application([])
    from camera_profile_studio.gui.main_window import MainWindow

    tag = f"scale{args.scale}"
    work = OUT / "runs" / tag
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    settings = QSettings(str(work / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings=settings)
    win.resize(args.width, args.height)
    win.show()
    manifest: dict = {
        "scale_requested": args.scale,
        "host_dpr": base,
        "qt_scale_factor": os.environ["QT_SCALE_FACTOR"],
        "effective_dpr": win.devicePixelRatioF(),
        "style": app.style().name(),
        "platform": QApplication.platformName(),
        "window_logical": [win.width(), win.height()],
        "actions": [],
    }

    def pump(seconds: float = 0.2) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def wait_idle(timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        pump(0.35)  # let debounced rebuilds start
        while time.monotonic() < end:
            app.processEvents()
            if not win.ctx.jobs.active() and not any(
                getattr(ws, "_debounce", None) is not None and ws._debounce.isActive() for ws in win.workspaces.values()
            ):
                pump(0.15)
                if not win.ctx.jobs.active():
                    return True
            time.sleep(0.02)
        return False

    def act(name: str, **result) -> None:
        manifest["actions"].append({"action": name, **result})
        print(name, result, flush=True)

    # Never block on modal dialogs during automation; record them instead.
    shown_errors: list[str] = []
    win.ctx.show_error = lambda err, title_key="dialog.error": shown_errors.append(f"{err.summary()} | {err.detail}")
    win.ctx.confirm = lambda *a, **k: True

    ws = win.workspaces
    # 1) Inspector: open (same slot as drag & drop) four supplied profiles + long path + invalid file
    profiles = [gui_fixtures.PROFILES / n for n in ("Nikon_D70s.dcp", "Olympus_E1.dcp", "Nikon_D200.icc", "Sony_DSC-F828.icc")]
    ws["inspector"].open_paths([str(p) for p in profiles] + [str(fx["long_profile"]), str(fx["invalid"])])
    wait_idle()
    act("inspect_profiles", states={e.name: e.state for e in win.ctx.profiles.entries},
        errors={e.name: (e.error.summary() + " " + e.error.detail) for e in win.ctx.profiles.entries if e.error})
    # select D70s to show DCP inspection
    win.ctx.set_current_profile(win.ctx.profiles.entries[0])
    ws["inspector"].table.selectRow(0)
    pump(0.3)
    act("inspector_d70s", technical=ws["inspector"].caps.rows["technical"].status.text(),
        look=ws["inspector"].caps.rows["look"].status.text(), meta_rows=ws["inspector"].meta_model.rowCount(),
        tag_rows=ws["inspector"].struct_model.rowCount())

    # 2) Converter: sRGB -> Sony F828 ICC (previewable), then ICC -> ICC
    conv = ws["converter"]
    win.show_workspace("converter")
    win.ctx.set_current_profile(win.ctx.profiles.entries[3])
    conv.direction.setCurrentIndex(conv.direction.findData("to_srgb"))
    conv.preview.load(str(fx["chart"]))
    wait_idle()
    act("converter_icc_to_srgb", status=conv.status_label.text(), preview_note=conv.preview.note.text(),
        lut=(conv.lut.input_kind, conv.lut.output_kind, conv.lut.size) if conv.lut else None,
        strength_enabled=conv.strength_slider.isEnabled(), strength_reason=conv.strength_reason.text())
    conv.direction.setCurrentIndex(conv.direction.findData("to_icc"))
    idx = conv.target_combo.findData(str(fx["long_profile"]))
    conv.target_combo.setCurrentIndex(idx)
    conv.size_combo.setCurrentIndex(conv.size_combo.findData(17))
    wait_idle()
    act("converter_icc_to_icc", status=conv.status_label.text(), spaces=conv.spaces_label.text(),
        export_enabled=conv.export_button.isEnabled())
    out_dir = OUT / "exports" / tag
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    conv.export_to(str(out_dir / "F828_to_F828copy_17.cube"), False)
    wait_idle()
    act("converter_export", files=sorted(p.name for p in out_dir.iterdir()), status=conv.status_label.text())
    conv.direction.setCurrentIndex(conv.direction.findData("from_srgb"))
    conv.size_combo.setCurrentIndex(conv.size_combo.findData(33))
    wait_idle()
    act("converter_srgb_to_icc_preview", status=conv.status_label.text(), preview_note=conv.preview.note.text(),
        has_after=conv.preview.viewer._after is not None)
    # D200 ICC lacks B2A: sRGB->profile must be disabled with a reason
    win.ctx.set_current_profile(win.ctx.profiles.entries[2])
    pump(0.3)
    item = conv.direction.model().item(conv.direction.findData("from_srgb"))
    act("converter_d200_output_direction", enabled=item.isEnabled(),
        tooltip=conv.direction.itemData(conv.direction.findData("from_srgb"), 3))
    # DCP technical: requires neutral + kelvin (user-entered test values)
    win.ctx.set_current_profile(win.ctx.profiles.entries[0])
    wait_idle()
    act("converter_dcp_without_wb", status=conv.status_label.text(), export_enabled=conv.export_button.isEnabled(),
        reason=conv.export_reason.text())
    for spin, v in zip(conv.neutral, (0.45, 1.0, 0.62)):
        spin.setValue(v)
    conv.kelvin.setValue(5000)
    wait_idle()
    act("converter_dcp_with_operator_wb", status=conv.status_label.text(), preview_note=conv.preview.note.text())

    # 3) Look extractor: D70s look-only @ 60 %
    look = ws["look"]
    win.show_workspace("look")
    look.mode_buttons["look_only"].setChecked(True)
    look.preview.load(str(fx["chart"]))
    look.strength_spin.setValue(60)
    wait_idle()
    act("look_d70s_look_only_60", status=look.status_label.text(), preview_note=look.preview.note.text(),
        has_after=look.preview.viewer._after is not None,
        lut_strength=look.lut.strength if look.lut else None)
    look.export_to(str(out_dir / "D70s_look_only_60.cube"), False)
    wait_idle()
    act("look_export", files=sorted(p.name for p in out_dir.iterdir()))
    look.mode_buttons["combined"].setChecked(True)
    wait_idle()
    act("look_combined_without_kelvin", status=look.status_label.text(), export_enabled=look.export_button.isEnabled())

    # 4) Match: CSV patches first, then the pixel-aligned same-scene pair
    match = ws["match"]
    win.show_workspace("match")
    match.evidence.setText("Synthetic GUI fixture: chart -> D70s LookTable (engine); not camera evidence")
    match.set_csv(str(fx["csv"]))
    pump(0.2)
    act("match_csv_ready", fit_enabled=match.fit_button.isEnabled(), reason=match.fit_reason_label.text())
    match._fit()
    wait_idle(120)
    act("match_csv_fit", report=match.report_label.text())
    match.rb_pair.setChecked(True)
    match.load_pair_image("source", str(fx["pair_source"]))
    match.load_pair_image("target", str(fx["pair_target"]))
    wait_idle()
    act("match_pair_unconfirmed", fit_enabled=match.fit_button.isEnabled(), reason=match.fit_reason_label.text(),
        register_enabled=match.register_button.isEnabled(), register_reason=match.pair_reason.text())
    match.same_scene.setChecked(True)
    pump(0.2)
    match._register()
    wait_idle(120)
    act("match_pair_register", result=match.align_label.text(), fit_enabled=match.fit_button.isEnabled(),
        reason=match.fit_reason_label.text())
    match.prealigned.setChecked(True)
    pump(0.2)
    match._fit()
    wait_idle(120)
    act("match_pair_fit", report=match.report_label.text(), status=match.status_label.text(),
        has_after=match.preview.viewer._after is not None)
    match.export_to(str(out_dir / "match_pair_EXPERIMENTAL_33.cube"), False)
    wait_idle()
    act("match_export", files=sorted(p.name for p in out_dir.iterdir()))
    # 5) Batch: four profiles, technical, operator WB for DCPs
    batch = ws["batch"]
    win.show_workspace("batch")
    bdir = OUT / "batch" / tag
    shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True)
    batch.add_files([str(p) for p in profiles] + [str(fx["invalid"])])
    batch.set_output_dir(str(bdir))
    for spin, v in zip(batch.neutral, (0.45, 1.0, 0.62)):
        spin.setValue(v)
    batch.kelvin.setValue(5000)
    batch.run()
    wait_idle(120)
    act("batch_run", items={Path(i.path).name: (i.state, batch.model._detail(i)) for i in batch.model.items},
        files=sorted(p.name for p in bdir.iterdir()))
    # second run: existing outputs -> user chooses "skip existing" (nothing replaced)
    mtimes = {p.name: p.stat().st_mtime_ns for p in bdir.iterdir()}
    batch.ask_conflicts = lambda conflicts: (act("batch_conflict_prompt", n=len(conflicts)), "skip")[1]
    batch.run()
    wait_idle(120)
    act("batch_rerun_skip_existing", items={Path(i.path).name: i.state for i in batch.model.items},
        unchanged=all(p.stat().st_mtime_ns == mtimes[p.name] for p in bdir.iterdir() if p.name in mtimes))
    # third run: user explicitly confirms replacement
    batch.ask_conflicts = lambda conflicts: "overwrite"
    batch.run()
    wait_idle(120)
    act("batch_rerun_overwrite_confirmed", items={Path(i.path).name: i.state for i in batch.model.items})

    # 6) Settings diagnostics
    win.show_workspace("settings")
    ws["settings"].run_diagnostics()
    wait_idle()
    act("settings_diagnostics", text=ws["settings"].diag_text.toPlainText().splitlines())

    # Screenshots: language x theme x workspace (live switching, no restart)
    shots = OUT / "screens" / tag
    shutil.rmtree(shots, ignore_errors=True)
    shots.mkdir(parents=True)
    keys = ["inspector", "converter", "look", "match", "batch", "settings"]
    look.mode_buttons["look_only"].setChecked(True)
    win.ctx.set_current_profile(win.ctx.profiles.entries[0])
    wait_idle()
    for theme in ("light", "dark"):
        apply_theme(theme)
        pump(0.6)
        for lang in ("en", "ja"):
            T.set_language(lang)
            pump(0.3)
            for key in keys:
                win.show_workspace(key)
                if key == "converter":
                    win.ctx.set_current_profile(win.ctx.profiles.entries[3])
                    conv.direction.setCurrentIndex(conv.direction.findData("from_srgb"))
                    wait_idle()
                elif key in ("inspector", "look"):
                    win.ctx.set_current_profile(win.ctx.profiles.entries[0])
                    wait_idle()
                pump(0.25)
                pix = win.grab()
                path = shots / f"{lang}_{theme}_{key}.png"
                pix.save(str(path))
            act("screens", theme=theme, lang=lang, scheme=current_color_scheme())
    # narrow window (resize) check
    win.resize(760, 700)
    pump(0.5)
    for key in ("inspector", "converter", "look", "match", "batch"):
        win.show_workspace(key)
        pump(0.3)
        win.grab().save(str(shots / f"narrow_{T.language}_{key}.png"))
    act("narrow_resize", size=[win.width(), win.height()])
    manifest["shown_errors"] = shown_errors
    (OUT / f"manifest_{tag}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    win._force_close = True
    win.close()
    pump(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



