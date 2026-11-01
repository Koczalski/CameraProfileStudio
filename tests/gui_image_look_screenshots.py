"""Native Windows validation driver for the Image Look Creator workspace.

One process per scale (QT_SCALE_FACTOR must be set before Qt starts):

    .venv\\Scripts\\python.exe tests\\gui_image_look_screenshots.py --scale 100
    .venv\\Scripts\\python.exe tests\\gui_image_look_screenshots.py --scale 150
    .venv\\Scripts\\python.exe tests\\gui_image_look_screenshots.py --scale 200

Drives the real widgets with the real ``image_look`` engine and the shared
read-only fixtures in ``output/image-look-fixtures`` (one CC0 harbor scene and
its synthetic derivatives plus procedural charts - UI plumbing evidence, not
cross-scene or OEM validation).  Everything is written to
``output/image-look-gui/scale<N>/`` (screens, exports, manifest.json); no
existing output is touched.
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
FIX = ROOT / "output" / "image-look-fixtures"
NAT = FIX / "natural"
PROC = FIX / "procedural"
OUT = ROOT / "output" / "image-look-gui"


def host_scale() -> float:
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
    if not (NAT / "harbor_warm_1200.png").is_file():
        print("fixtures missing:", FIX)
        return 2
    base = host_scale()
    os.environ["QT_SCALE_FACTOR"] = f"{args.scale / 100.0 / base:.6f}"

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from camera_profile_studio.gui.app import apply_theme, create_application, current_color_scheme
    from camera_profile_studio.gui.i18n import T

    app = create_application([])
    from camera_profile_studio.gui.main_window import MainWindow

    tag = f"scale{args.scale}"
    run = OUT / tag
    shutil.rmtree(run, ignore_errors=True)
    shots = run / "screens"
    exports = run / "exports"
    shots.mkdir(parents=True)
    exports.mkdir(parents=True)
    settings = QSettings(str(run / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(settings=settings)
    win.resize(args.width, args.height)
    win.show()
    manifest: dict = {
        "scale_requested": args.scale, "host_dpr": base, "qt_scale_factor": os.environ["QT_SCALE_FACTOR"],
        "effective_dpr": win.devicePixelRatioF(), "style": app.style().name(), "platform": QApplication.platformName(),
        "window_logical": [win.width(), win.height()],
        "evidence_note": "Source-tree GUI validation only (not packaged acceptance). Fixtures: one CC0 harbor scene "
                         "and synthetic derivatives/charts; no independent-scene, OEM or accuracy claim.",
        "actions": [], "screens": [],
    }
    shown_errors: list[str] = []
    win.ctx.show_error = lambda err, title_key="dialog.error": shown_errors.append(f"{err.summary()} | {err.detail}")
    win.ctx.confirm = lambda *a, **k: True

    def pump(seconds: float = 0.2) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    ws = win.workspaces["imagelook"]

    def wait_idle(timeout: float = 120.0) -> bool:
        end = time.monotonic() + timeout
        pump(0.4)
        while time.monotonic() < end:
            app.processEvents()
            if not win.ctx.jobs.active() and not ws._debounce.isActive():
                pump(0.2)
                if not win.ctx.jobs.active():
                    return True
            time.sleep(0.02)
        return False

    def act(name: str, **result) -> None:
        manifest["actions"].append({"action": name, **result})
        print(name, json.dumps(result, ensure_ascii=False, default=str)[:400], flush=True)

    def state() -> dict:
        a = ws.analysis
        return {
            "refs": [(it.name, it.included, it.weight, it.roi, it.state) for it in ws.model.items],
            "analysis_refs": [(Path(r.path).name, r.weight, r.roi, r.sha256[:12]) for r in a.references] if a else None,
            "warnings": a.warnings if a else None, "conflicts": a.conflicts if a else None,
            "ack_visible": ws.ack.isVisible(), "ack": ws.ack.isChecked(),
            "lut": (ws.lut.size, ws.lut.strength) if ws.lut else None,
            "export_enabled": ws.export_button.isEnabled(), "export_reason": ws.export_reason.text(),
            "analyze_enabled": ws.analyze_button.isEnabled(), "analyze_reason": ws.analyze_reason_label.text(),
            "status": ws.status_label.text(), "preview_note": ws.preview.note.text(),
            "has_after": ws.preview.viewer._after is not None,
            "error": (ws._analysis_error.key, ws._analysis_error.detail) if ws._analysis_error else None,
        }

    def grab(name: str, combos=(("light", "en"), ("light", "ja"), ("dark", "en"), ("dark", "ja"))) -> None:
        for theme, lang in combos:
            apply_theme(theme)
            T.set_language(lang)
            pump(0.5)
            path = shots / f"{lang}_{theme}_{name}.png"
            win.grab().save(str(path))
            manifest["screens"].append({"file": path.name, "state": name, "lang": lang, "theme": theme,
                                        "scheme": current_color_scheme(), "logical": [win.width(), win.height()]})
        apply_theme("light")
        T.set_language("en")
        pump(0.3)

    T.set_language("en")
    apply_theme("light")
    win.show_workspace("imagelook")
    pump(0.5)
    # A) empty state: reasons instead of phantom buttons
    act("empty", **state())
    grab("01_empty")

    # B) one warm reference, baseline photo as the separate preview target
    ws.add_references([str(NAT / "harbor_warm_1200.png")])
    ws.preview.load(str(NAT / "harbor_baseline_1200.png"))
    wait_idle()
    act("single_ref_loaded", **state())
    ws.analyze()
    wait_idle()
    act("single_ref_analysed", **state())
    ws.export_to(str(exports / "warm_ref_33.cube"), False)
    wait_idle()
    act("single_ref_export", files=sorted(p.name for p in exports.iterdir()), status=ws.status_label.text())
    grab("02_single_warm_ref_baseline_target")

    # C) multiple variants: weight, crop, exclusion; strength baked once
    ws.add_references([str(NAT / "harbor_softcontrast_1200.png"), str(NAT / "harbor_highcontrast_1200.png"),
                       str(NAT / "harbor_cool_1200.png")])
    wait_idle()
    act("multi_added_invalidates", **state())
    ws.set_weight(0, 2.0)
    ws.set_crop(1, (0.1, 0.15, 0.85, 0.9))
    ws.set_included(3, False)
    ws.set_amount("saturation_amount", 70)
    ws.strength_spin.setValue(80)
    ws.select_row(1)
    ws.analyze()
    wait_idle()
    act("multi_analysed", **state())
    ws.export_to(str(exports / "multi_variants_33_s80.cube"), False)
    wait_idle()
    meta = json.loads((exports / "multi_variants_33_s80.json").read_text(encoding="utf-8"))
    act("multi_export", references=json.loads(meta["provenance"]["references"]),
        settings=json.loads(meta["provenance"]["settings"]), strength=meta.get("strength"))
    grab("03_multi_weight_crop_exclusion")

    # D) conflicting references: explicit acknowledgement required
    ws.remove_rows(list(range(len(ws.model.items))))
    ws.add_references([str(NAT / "harbor_baseline_1200.png"), str(PROC / "colorchart.png")])
    wait_idle()
    ws.analyze()
    wait_idle()
    act("conflict_unacknowledged", **state())
    grab("04_conflict_needs_ack")
    ws.ack.setChecked(True)
    wait_idle()
    act("conflict_acknowledged", **state(), model_warnings=ws._model_warnings)
    grab("05_conflict_acknowledged", combos=(("light", "en"), ("dark", "ja")))

    # E) unusable reference -> analysis error shown with reason
    ws.remove_rows(list(range(len(ws.model.items))))
    ws.add_references([str(PROC / "black.png")])
    wait_idle()
    ws.analyze()
    wait_idle()
    act("unusable_reference_error", **state())
    grab("06_error_unusable_reference", combos=(("light", "en"), ("dark", "ja")))

    # F) narrow window, ready state
    ws.remove_rows(list(range(len(ws.model.items))))
    ws.add_references([str(NAT / "harbor_warm_1200.png"), str(NAT / "harbor_softcontrast_1200.png")])
    ws.set_crop(0, (0.05, 0.05, 0.95, 0.8))
    ws.select_row(0)
    wait_idle()
    ws.analyze()
    wait_idle()
    win.resize(760, 700)
    pump(0.6)
    act("narrow_ready", size=[win.width(), win.height()], **state())
    grab("07_narrow_ready")
    win.resize(args.width, args.height)
    pump(0.3)

    manifest["shown_errors"] = shown_errors
    manifest["export_files"] = sorted(p.name for p in exports.iterdir())
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    win._force_close = True
    win.close()
    pump(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
