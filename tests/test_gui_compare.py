"""LUT Compare workspace (offscreen Qt): grid, contact sheets and split images.

Runs the real engine on small generated photos and .cube files, so a missing
engine module is a failure, never a skip.  Failures while writing are provoked
by replacing the JPEG publisher with a test-only wrapper.
"""
from __future__ import annotations

import errno
import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.gui

from PIL import Image  # noqa: E402
from PySide6.QtCore import QSettings, Qt  # noqa: E402

from camera_profile_studio import lut_compare as lc  # noqa: E402
from camera_profile_studio import lut_health as lh  # noqa: E402

HORIZONTAL = Qt.Orientation.Horizontal
VERTICAL = Qt.Orientation.Vertical
DISPLAY = Qt.ItemDataRole.DisplayRole
TIP = Qt.ItemDataRole.ToolTipRole


def gradient(width=300, height=200):
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    return np.dstack((0.2 + 0.6 * x / (width - 1), 0.25 + 0.5 * y / (height - 1), np.full((height, width), 0.5)))


def save_photo(path: Path, width=300, height=200) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((gradient(width, height) * 255 + 0.5).astype(np.uint8)).save(path)
    return path


def write_cube(path: Path, values: np.ndarray) -> Path:
    n = values.shape[0]
    lines = [f"LUT_3D_SIZE {n}"]
    for b in range(n):
        for g in range(n):
            for r in range(n):
                lines.append("{:.6f} {:.6f} {:.6f}".format(*values[r, g, b]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def tinted(gain, n=9) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n)
    return np.stack(np.meshgrid(t, t, t, indexing="ij"), axis=-1) * np.asarray(gain)


def log_values(n=33) -> np.ndarray:
    t = np.minimum(lh.rec709_oetf(np.maximum(lh.log_decode("S-Log3",np.linspace(0.0, 1.0, n)), 0.0)), 1.0)
    return np.stack(np.meshgrid(t, t, t, indexing="ij"), axis=-1)


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
    w.ctx.confirm = lambda *a, **k: True   # closing with a job running must not wait for a dialog
    qtbot.addWidget(w)
    w.resize(1280, 820)
    w.show()
    w.show_workspace("compare")
    yield w
    w._force_close = True
    w.close()
    w.ctx.jobs.shutdown(10000)
    T.set_language("en")


def idle(qtbot, w, timeout=120000):
    qtbot.wait(50)
    qtbot.waitUntil(lambda: not w.ctx.jobs.active(), timeout=timeout)


def log_text(win) -> str:
    return "\n".join(e.render() for e in win.ctx.log_model.entries)


@pytest.fixture()
def ws(win):
    return win.workspaces["compare"]


@pytest.fixture()
def out(tmp_path):
    folder = tmp_path / "out"
    folder.mkdir()
    return folder


def filled(qtbot, win, ws, tmp_path, photos=2, luts=3):
    ws.add_photos([str(save_photo(tmp_path / f"photo{i}.png")) for i in range(photos)])
    gains = [(1.0, 0.85, 0.6), (0.6, 0.85, 1.0), (0.8, 1.0, 0.7), (1.0, 0.6, 0.9), (0.7, 0.7, 1.0), (0.9, 0.9, 0.6),
             (0.5, 1.0, 1.0), (1.0, 1.0, 0.5), (0.6, 0.6, 0.6)]
    ws.add_lut_files([str(write_cube(tmp_path / f"lut{i}.cube", tinted(gains[i]))) for i in range(luts)])
    idle(qtbot, win)


def read_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        assert image.format == "JPEG"
        return np.asarray(image.convert("RGB"), dtype=np.int32)


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# ------------------------------------------------------------------- page and grid


def test_page_is_added_after_lut_check_with_ctrl_9(win):
    from camera_profile_studio.gui.main_window import WORKSPACES

    assert [c.key for c in WORKSPACES][-2:] == ["diagnose", "compare"]
    win.ws_actions[8].trigger()
    assert win.current_workspace().key == "compare"
    assert win.ws_actions[8].shortcut().toString() == "Ctrl+9"
    assert not win.nav.nav_model.item(8).icon().isNull()


def test_two_photos_three_luts_make_two_rows_four_columns(qtbot, win, ws, tmp_path):
    filled(qtbot, win, ws, tmp_path)
    assert (ws.model.rowCount(), ws.model.columnCount()) == (2, 4)
    assert ws.model.headerData(0, VERTICAL, DISPLAY) == "photo0.png"
    assert ws.model.headerData(1, HORIZONTAL, DISPLAY).startswith("lut0")
    for row in range(2):
        for column in range(4):
            assert ws.model.cell_image(row, column) is not None


def test_before_column_shows_the_photo_and_lut_columns_show_the_result(qtbot, win, ws, tmp_path):
    from camera_profile_studio.gui.jobs import CancelToken

    filled(qtbot, win, ws, tmp_path, 1, 1)
    token = CancelToken()
    data = win.ctx.engine.load_image(str(tmp_path / "photo0.png"), 2048, token)
    applied = win.ctx.engine.render_preview(data, ws.model.luts[0].lut, token)
    before, after = ws.model.cell_image(0, 0), ws.model.cell_image(0, 1)
    assert (before.width(), before.height()) == (300, 200)
    pixel = before.pixelColor(150, 100)
    assert abs(pixel.red() - data.array[100, 150, 0] * 255) <= 1
    pixel = after.pixelColor(150, 100)
    assert abs(pixel.blue() - applied[100, 150, 2] * 255) <= 1


def test_nothing_added_shows_the_hint_and_disables_the_sheet(qtbot, win, ws, tmp_path):
    assert not ws.empty_label.isHidden() and ws.grid.isHidden()
    assert not ws.sheet_button.isEnabled() and not ws.split_button.isEnabled()
    ws.add_photos([str(save_photo(tmp_path / "only.png"))])
    idle(qtbot, win)
    assert not ws.empty_label.isHidden() and not ws.sheet_button.isEnabled()
    ws.add_lut_files([str(write_cube(tmp_path / "a.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    assert ws.empty_label.isHidden() and not ws.grid.isHidden() and ws.sheet_button.isEnabled()
    ws.remove_luts([0])
    assert not ws.empty_label.isHidden() and not ws.sheet_button.isEnabled()


def test_unreadable_files_are_not_added_and_the_rest_continue(qtbot, win, ws, tmp_path):
    bad_photo = tmp_path / "bad.png"
    bad_photo.write_bytes(b"not an image")
    bad_lut = tmp_path / "bad.cube"
    bad_lut.write_text("nonsense", encoding="utf-8")
    ws.add_photos([str(bad_photo), str(save_photo(tmp_path / "good.png"))])
    ws.add_lut_files([str(bad_lut), str(write_cube(tmp_path / "good.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    assert (ws.model.rowCount(), ws.model.columnCount()) == (1, 2)
    text = log_text(win)
    assert "bad.png" in text and "bad.cube" in text


def test_adding_the_same_file_again_is_refused_and_logged(qtbot, win, ws, tmp_path):
    photo, lut = save_photo(tmp_path / "p.png"), write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5)))
    ws.add_photos([str(photo)])
    ws.add_lut_files([str(lut)])
    idle(qtbot, win)
    ws.add_photos([str(photo)])
    ws.add_lut_files([str(lut)])
    idle(qtbot, win)
    assert (ws.model.rowCount(), ws.model.columnCount()) == (1, 2)
    text = log_text(win)
    assert "p.png" in text and "l.cube" in text and "already" in text


def test_same_file_twice_in_one_request_is_added_once(qtbot, win, ws, tmp_path):
    photo = str(save_photo(tmp_path / "p.png"))
    ws.add_photos([photo, photo])
    idle(qtbot, win)
    assert ws.model.rowCount() == 1


def test_photos_keep_the_chosen_order_and_can_be_removed(qtbot, win, ws, tmp_path):
    paths = [str(save_photo(tmp_path / f"{name}.png")) for name in "cab"]
    ws.add_photos(paths)
    idle(qtbot, win)
    assert [p.name for p in ws.model.photos] == ["c.png", "a.png", "b.png"]
    ws.remove_photos([1])
    assert [p.name for p in ws.model.photos] == ["c.png", "b.png"]
    assert ws.photo_list.count() == 2


def test_lut_size_outside_17_33_65_is_converted_and_applied(qtbot, win, ws, tmp_path):
    ws.add_photos([str(save_photo(tmp_path / "p.png"))])
    ws.add_lut_files([str(write_cube(tmp_path / "nine.cube", tinted((1.2, 1.0, 0.5), 9)))])  # also leaves 0..1
    idle(qtbot, win)
    assert ws.model.luts[0].lut.size == 17 and ws.model.cell_image(0, 1) is not None
    assert not win.errors and "nine.cube" not in "".join(e.render() for e in win.ctx.log_model.entries if e.level == "error")


@pytest.mark.parametrize("side", lc.CELL_SIDES)
def test_cell_size_changes_the_cells(qtbot, win, ws, tmp_path, side):
    ws.add_photos([str(save_photo(tmp_path / "big.png", 1000, 600))])
    ws.add_lut_files([str(write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    assert ws.size_combo.currentData() == lc.DEFAULT_CELL_SIDE == 480
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(side))
    idle(qtbot, win)
    assert ws.model.cell_image(0, 0).width() == side and ws.model.cell_image(0, 1).width() == side
    assert ws.grid.rowHeight(0) >= side and ws.grid.columnWidth(1) >= side


def test_cell_size_change_updates_the_scroll_range(qtbot, win, ws, tmp_path):
    filled(qtbot, win, ws, tmp_path, 3, 3)
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(320))
    idle(qtbot, win)
    qtbot.wait(50)
    across, down = ws.grid.horizontalHeader(), ws.grid.verticalHeader()
    assert across.length() == 4 * (320 + 8) and down.length() == 3 * (320 + 8)
    viewport = ws.grid.viewport()
    assert ws.grid.horizontalScrollBar().maximum() == max(0, across.length() - viewport.width())
    assert ws.grid.verticalScrollBar().maximum() == max(0, down.length() - viewport.height())


def test_cell_size_change_does_not_read_the_files_again(qtbot, win, ws, tmp_path):
    photo = save_photo(tmp_path / "big.png", 1000, 600)
    ws.add_photos([str(photo)])
    ws.add_lut_files([str(write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    photo.unlink()
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(320))
    idle(qtbot, win)
    assert ws.model.cell_image(0, 0).width() == 320


# ------------------------------------------------------------------- contact sheets


def test_contact_sheet_has_headers_and_every_cell(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path)
    ws.export_sheets(str(out))
    idle(qtbot, win)
    assert names(out) == ["contact-001.jpg"]
    sheet = read_rgb(out / "contact-001.jpg")
    metrics = lc.SheetMetrics.for_side(480)
    assert (sheet.shape[1], sheet.shape[0]) == metrics.sheet_size(lc.paginate(2, 3)[0])
    assert sheet.shape[1] >= 4 * 480 + metrics.label_width and sheet.shape[0] >= 2 * 480 + metrics.header_height
    # text was drawn in the header and in the row labels
    assert (sheet[:metrics.header_height, metrics.label_width:] < 128).any()
    assert (sheet[metrics.header_height:, :metrics.label_width] < 128).any()
    # the picture of each cell is where the layout says, centred in its square
    for row in range(2):
        for column in range(4):
            x, y = metrics.cell_origin(row, column)
            dx, dy = lc.centered_offset(300, 200, 480)
            cell = ws.model.cell_image(row, column).pixelColor(150, 100)
            sampled = sheet[y + dy + 100, x + dx + 150]
            assert np.abs(sampled - (cell.red(), cell.green(), cell.blue())).max() <= 6
    assert "contact-001.jpg" in log_text(win)


def test_more_luts_than_one_page_make_numbered_pages_with_before_repeated(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path, 1, 8)
    ws.export_sheets(str(out))
    idle(qtbot, win)
    assert names(out) == ["contact-001.jpg", "contact-002.jpg"]
    metrics = lc.SheetMetrics.for_side(480)
    second = read_rgb(out / "contact-002.jpg")
    assert (second.shape[1], second.shape[0]) == metrics.sheet_size(lc.paginate(1, 8)[1])
    x, y = metrics.cell_origin(0, 0)
    dx, dy = lc.centered_offset(300, 200, 480)
    before = ws.model.cell_image(0, 0).pixelColor(150, 100)
    assert np.abs(second[y + dy + 100, x + dx + 150] - (before.red(), before.green(), before.blue())).max() <= 6


def test_cell_size_changes_the_sheet(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path, 1, 1)
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(320))
    idle(qtbot, win)
    ws.export_sheets(str(out))
    idle(qtbot, win)
    sheet = read_rgb(out / "contact-001.jpg")
    assert (sheet.shape[1], sheet.shape[0]) == lc.SheetMetrics.for_side(320).sheet_size(lc.paginate(1, 1)[0])


def test_mixed_portrait_and_landscape_are_centred_in_square_cells(qtbot, win, ws, tmp_path, out):
    ws.add_photos([str(save_photo(tmp_path / "wide.png", 1000, 500)), str(save_photo(tmp_path / "tall.png", 500, 1000))])
    ws.add_lut_files([str(write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    ws.export_sheets(str(out))
    idle(qtbot, win)
    sheet = read_rgb(out / "contact-001.jpg")
    metrics = lc.SheetMetrics.for_side(480)
    x, y = metrics.cell_origin(0, 0)      # wide: background above, picture in the middle
    assert (sheet[y + 20, x + 240] > 240).all() and not (sheet[y + 240, x + 240] > 240).all()
    x, y = metrics.cell_origin(1, 0)      # tall: background at the side, picture in the middle
    assert (sheet[y + 240, x + 20] > 240).all() and not (sheet[y + 240, x + 240] > 240).all()


def test_sheet_with_an_existing_file_writes_nothing(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path, 1, 8)
    (out / "contact-002.jpg").write_bytes(b"precious")
    ws.export_sheets(str(out))
    idle(qtbot, win)
    assert names(out) == ["contact-002.jpg"]
    assert (out / "contact-002.jpg").read_bytes() == b"precious"
    assert "contact-002.jpg" in log_text(win)
    assert any("contact-002.jpg" in e.summary() + e.detail for e in win.errors)


def test_japanese_and_very_long_names_export(qtbot, win, ws, tmp_path, out):
    from camera_profile_studio.gui.i18n import T

    T.set_language("ja")
    long_stem = "とても長い名前の写真" * 8
    ws.add_photos([str(save_photo(tmp_path / "夕焼け.png")), str(save_photo(tmp_path / f"{long_stem}.png"))])
    ws.add_lut_files([str(write_cube(tmp_path / "暖色.cube", tinted((1, 0.85, 0.6))))])
    idle(qtbot, win)
    assert ws.model.headerData(0, VERTICAL, DISPLAY) == "夕焼け.png"
    ws.export_sheets(str(out))
    idle(qtbot, win)
    for row in range(2):
        for column in (1,):
            ws.toggle_cell(ws.model.index(row, column))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert not win.errors
    assert "contact-001.jpg" in names(out) and "夕焼け__暖色.jpg" in names(out)
    assert len(names(out)) == 3
    read_rgb(out / "夕焼け__暖色.jpg")


def test_sheet_headers_use_the_original_names_and_shorten_long_ones(qtbot, win, ws, tmp_path, out, monkeypatch):
    from camera_profile_studio.gui import compare_render

    drawn = []
    real = compare_render._elided

    def recording(painter, text, width):
        shown = real(painter, text, width)
        drawn.append((text, shown))
        return shown

    monkeypatch.setattr(compare_render, "_elided", recording)
    long_photo, long_lut = "写真の長い名前" * 12 + ".png", "long_lut_name_" * 12
    ws.add_photos([str(save_photo(tmp_path / long_photo))])
    ws.add_lut_files([str(write_cube(tmp_path / f"{long_lut}.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    ws.receive_lut(replace(ws.model.luts[0].lut, title='a/b:c'), "look")
    idle(qtbot, win)
    ws.export_sheets(str(out))
    idle(qtbot, win)
    assert names(out) == ["contact-001.jpg"]
    shown = dict(drawn)
    assert shown["a/b:c"] == "a/b:c"     # file-name characters are replaced in file names only
    for name in (long_photo, long_lut):
        assert "…" in shown[name] and len(shown[name]) < len(name)


# ------------------------------------------------------------------- split images


def test_split_image_matches_before_and_after(qtbot, win, ws, tmp_path, out):
    from camera_profile_studio.gui.jobs import CancelToken

    filled(qtbot, win, ws, tmp_path, 1, 1)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["photo0__lut0.jpg"]
    image = read_rgb(out / "photo0__lut0.jpg")
    assert image.shape == (200, 300, 3)
    token = CancelToken()
    data = win.ctx.engine.load_image(str(tmp_path / "photo0.png"), 2048, token)
    before = lc.to_rgb8(data.array).astype(np.int32)
    after = lc.to_rgb8(win.ctx.engine.render_preview(data, ws.model.luts[0].lut, token)).astype(np.int32)
    assert np.abs(before - after).mean() > 10   # the two sides can be told apart
    assert np.abs(image[:, :142] - before[:, :142]).max() <= 6
    assert np.abs(image[:, 158:] - after[:, 158:]).max() <= 6
    assert np.abs(image[:, :142] - after[:, :142]).mean() > 10
    assert image[100, 149].min() > 200   # the line
    assert win.ctx.log_model.entries[-1].level in ("success", "info")


def test_split_image_is_limited_to_2048(qtbot, win, ws, tmp_path, out):
    ws.add_photos([str(save_photo(tmp_path / "huge.png", 2400, 1200))])
    ws.add_lut_files([str(write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert read_rgb(out / "huge__l.jpg").shape == (1024, 2048, 3)


def test_only_selected_cells_are_written_and_clicks_toggle(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path)
    assert not ws.split_button.isEnabled()
    ws.grid.clicked.emit(ws.model.index(0, 1))
    ws.grid.clicked.emit(ws.model.index(1, 2))
    ws.grid.clicked.emit(ws.model.index(1, 3))
    assert ws.selected_count() == 3 and ws.split_button.isEnabled()
    ws.grid.clicked.emit(ws.model.index(1, 3))    # clicking again clears it
    assert ws.selected_count() == 2
    ws.grid.clicked.emit(ws.model.index(0, 0))    # the Before column cannot be chosen
    assert ws.selected_count() == 2
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["photo0__lut0.jpg", "photo1__lut1.jpg"]
    ws.grid.clicked.emit(ws.model.index(0, 1))
    ws.grid.clicked.emit(ws.model.index(1, 2))
    assert ws.selected_count() == 0 and not ws.split_button.isEnabled()


def test_real_mouse_click_and_keyboard_toggle_a_cell(qtbot, win, ws, tmp_path):
    filled(qtbot, win, ws, tmp_path, 1, 1)
    index = ws.model.index(0, 1)
    ws.grid.scrollTo(index)
    qtbot.mouseClick(ws.grid.viewport(), Qt.MouseButton.LeftButton, pos=ws.grid.visualRect(index).center())
    assert ws.selected_count() == 1
    ws.grid.setCurrentIndex(index)
    qtbot.keyClick(ws.grid, Qt.Key.Key_Space)
    assert ws.selected_count() == 0


def test_removing_a_photo_or_lut_clears_its_selection(qtbot, win, ws, tmp_path):
    filled(qtbot, win, ws, tmp_path)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.toggle_cell(ws.model.index(1, 2))
    ws.remove_photos([0])
    assert ws.selected_count() == 1
    ws.remove_luts([1])
    assert ws.selected_count() == 0 and not ws.split_button.isEnabled()


def test_split_with_an_existing_file_writes_nothing(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.toggle_cell(ws.model.index(1, 1))
    (out / "photo1__lut0.jpg").write_bytes(b"precious")
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["photo1__lut0.jpg"]
    assert (out / "photo1__lut0.jpg").read_bytes() == b"precious"
    assert any("photo1__lut0.jpg" in e.summary() + e.detail for e in win.errors)


def test_same_output_name_writes_the_first_and_logs_the_other(qtbot, win, ws, tmp_path, out):
    ws.add_photos([str(save_photo(tmp_path / "a" / "same.png")), str(save_photo(tmp_path / "b" / "same.png"))])
    ws.add_lut_files([str(write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.toggle_cell(ws.model.index(1, 1))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["same__l.jpg"]
    assert "same__l.jpg" in log_text(win) and str(tmp_path / "b" / "same.png") in log_text(win)


def test_invalid_characters_are_replaced_in_the_file_but_not_in_the_header(qtbot, win, ws, tmp_path, out):
    ws.add_photos([str(save_photo(tmp_path / "p.png"))])
    ws.add_lut_files([str(write_cube(tmp_path / "base.cube", tinted((1, 1, 0.5))))])
    idle(qtbot, win)
    ws.receive_lut(replace(ws.model.luts[0].lut, title='a/b:c'), "look")
    idle(qtbot, win)
    assert ws.model.headerData(2, HORIZONTAL, DISPLAY).startswith("a/b:c")
    ws.toggle_cell(ws.model.index(0, 2))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["p__a_b_c.jpg"]


def test_missing_photo_fails_only_its_cells(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path, 2, 1)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.toggle_cell(ws.model.index(1, 1))
    (tmp_path / "photo0.png").unlink()
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["photo1__lut0.jpg"]
    assert "photo0" in log_text(win)


def test_cancel_keeps_finished_files_and_reports_the_count(qtbot, win, ws, tmp_path, out, monkeypatch):
    from camera_profile_studio.gui.workspaces import compare

    filled(qtbot, win, ws, tmp_path, 3, 1)
    for row in range(3):
        ws.toggle_cell(ws.model.index(row, 1))
    real = compare.write_jpeg_exclusive

    def write_then_cancel(pixels, destination):
        real(pixels, destination)
        for rec in win.ctx.jobs.active("compare:export"):
            rec.token.cancel()

    monkeypatch.setattr(compare, "write_jpeg_exclusive", write_then_cancel)
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["photo0__lut0.jpg"]
    last = win.ctx.log_model.entries[-1]
    assert last.key == "compare.log.cancelled" and last.level == "warning" and last.kwargs["n"] == 1


def test_write_failure_stops_keeps_finished_files_and_leaves_no_partial(qtbot, win, ws, tmp_path, out, monkeypatch):
    from camera_profile_studio.gui.workspaces import compare

    filled(qtbot, win, ws, tmp_path, 3, 1)
    for row in range(3):
        ws.toggle_cell(ws.model.index(row, 1))
    real, calls = compare.write_jpeg_exclusive, []

    def full_disk(pixels, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(pixels, destination)

    monkeypatch.setattr(compare, "write_jpeg_exclusive", full_disk)
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert len(calls) == 2 and names(out) == ["photo0__lut0.jpg"]
    assert "No space left" in log_text(win)
    last = win.ctx.log_model.entries[-1]
    assert last.key == "compare.log.stopped" and last.kwargs["n"] == 1 and last.kwargs["name"] == "photo1__lut0.jpg"


def test_cancel_during_sheets_keeps_finished_pages(qtbot, win, ws, tmp_path, out, monkeypatch):
    from camera_profile_studio.gui.workspaces import compare

    filled(qtbot, win, ws, tmp_path, 1, 8)
    real = compare.write_jpeg_exclusive

    def write_then_cancel(pixels, destination):
        real(pixels, destination)
        for rec in win.ctx.jobs.active("compare:export"):
            rec.token.cancel()

    monkeypatch.setattr(compare, "write_jpeg_exclusive", write_then_cancel)
    ws.export_sheets(str(out))
    idle(qtbot, win)
    assert names(out) == ["contact-001.jpg"]
    last = win.ctx.log_model.entries[-1]
    assert last.key == "compare.log.cancelled" and last.level == "warning" and last.kwargs["n"] == 1


def test_a_file_that_appears_meanwhile_stops_the_run_without_replacing_it(qtbot, win, ws, tmp_path, out, monkeypatch):
    from camera_profile_studio.gui.workspaces import compare

    filled(qtbot, win, ws, tmp_path, 1, 1)
    ws.toggle_cell(ws.model.index(0, 1))
    real = compare.write_jpeg_exclusive

    def racing(pixels, destination):
        Path(destination).write_bytes(b"precious")
        real(pixels, destination)

    monkeypatch.setattr(compare, "write_jpeg_exclusive", racing)
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    assert names(out) == ["photo0__lut0.jpg"]
    assert (out / "photo0__lut0.jpg").read_bytes() == b"precious"
    assert "photo0__lut0.jpg" in log_text(win)


def test_cancelling_the_grid_does_not_prevent_the_sheet(qtbot, win, ws, tmp_path, out):
    filled(qtbot, win, ws, tmp_path, 1, 1)
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(320))
    ws.cancel()
    idle(qtbot, win)
    assert ws.sheet_button.isEnabled()
    ws.export_sheets(str(out))
    idle(qtbot, win)
    sheet = read_rgb(out / "contact-001.jpg")
    assert (sheet.shape[1], sheet.shape[0]) == lc.SheetMetrics.for_side(320).sheet_size(lc.paginate(1, 1)[0])
    x, y = lc.SheetMetrics.for_side(320).cell_origin(0, 1)
    dx, dy = lc.centered_offset(300, 200, 320)
    assert not (sheet[y + dy + 100, x + dx + 150] > 240).all()   # the unrendered cell was drawn for the sheet


# ------------------------------------------------------------------- Log warning


def test_log_lut_is_applied_and_warned_everywhere(qtbot, win, ws, tmp_path, out):
    from camera_profile_studio.gui.i18n import tr
    from camera_profile_studio.gui.jobs import CancelToken

    warning = tr("compare.warning.log")
    ws.add_photos([str(save_photo(tmp_path / "p.png"))])
    ws.add_lut_files([str(write_cube(tmp_path / "log.cube", log_values())),
                      str(write_cube(tmp_path / "plain.cube", tinted((1, 0.8, 0.6))))])
    idle(qtbot, win)
    assert [lut.is_log for lut in ws.model.luts] == [True, False]
    assert warning in ws.model.headerData(1, HORIZONTAL, DISPLAY) and warning in ws.model.headerData(1, HORIZONTAL, TIP)
    assert warning not in ws.model.headerData(2, HORIZONTAL, DISPLAY)
    assert ws.model.cell_image(0, 1) is not None
    # beside the LUT name on the sheet: the warning line has ink only above the Log LUT
    ws.export_sheets(str(out))
    idle(qtbot, win)
    sheet = read_rgb(out / "contact-001.jpg")
    metrics = lc.SheetMetrics.for_side(480)
    top = metrics.margin + int(metrics.font_px * 1.4)
    log_x = metrics.cell_origin(0, 1)[0]
    plain_x = metrics.cell_origin(0, 2)[0]
    assert (sheet[top:metrics.header_height - 2, log_x:log_x + 480] < 200).any()
    assert not (sheet[top:metrics.header_height - 2, plain_x:plain_x + 480] < 200).any()
    # a band added below the photo of the Log LUT's split image only
    ws.toggle_cell(ws.model.index(0, 1))
    ws.toggle_cell(ws.model.index(0, 2))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    banded, plain = read_rgb(out / "p__log.jpg"), read_rgb(out / "p__plain.jpg")
    assert plain.shape == (200, 300, 3)
    assert banded.shape[1] == 300 and banded.shape[0] > 200
    # the photo above the band is the ordinary split image
    token = CancelToken()
    data = win.ctx.engine.load_image(str(tmp_path / "p.png"), 2048, token)
    expected = lc.split_image(data.array, win.ctx.engine.render_preview(data, ws.model.luts[0].lut, token)).astype(np.int32)
    difference = np.abs(banded[:200] - expected)
    assert difference[:, :142].max() <= 8 and difference[:, 158:].mean() <= 3
    # the band has its colour and the words of the warning
    band = banded[204:]
    assert np.abs(np.median(band, axis=(0, 1)) - (150, 60, 0)).max() <= 12
    assert (band > 220).all(axis=-1).any()


def test_portrait_split_keeps_the_photo_at_2048_and_adds_the_band(qtbot, win, ws, tmp_path, out):
    ws.add_photos([str(save_photo(tmp_path / "tall.png", 1200, 2400))])
    ws.add_lut_files([str(write_cube(tmp_path / "log.cube", log_values()))])
    idle(qtbot, win)
    ws.toggle_cell(ws.model.index(0, 1))
    ws.export_comparisons(str(out))
    idle(qtbot, win)
    image = read_rgb(out / "tall__log.jpg")
    assert image.shape[1] == 1024 and image.shape[0] > 2048


# ------------------------------------------------------------------- "Add to comparison"


def diagnose_with(qtbot, win, cube: Path):
    page = win.workspaces["diagnose"]
    page.open_path(str(cube))
    idle(qtbot, win)
    qtbot.waitUntil(lambda: page.lut is not None and not page._debounce.isActive(), timeout=60000)
    idle(qtbot, win)
    assert page.lut is not None
    return page


@pytest.mark.parametrize("key", ["converter", "look", "match", "imagelook", "diagnose"])
def test_every_lut_page_has_the_button(win, key):
    assert win.workspaces[key].compare_button is not None


def test_button_needs_a_lut(win):
    page = win.workspaces["look"]
    assert not page.compare_button.isEnabled() and page.compare_button.toolTip()


def test_technical_lut_cannot_be_added_and_says_why(qtbot, win, ws, tmp_path):
    from camera_profile_studio.gui.i18n import tr

    page = diagnose_with(qtbot, win, write_cube(tmp_path / "l.cube", tinted((1, 1, 0.5))))
    assert page.compare_button.isEnabled()
    page.lut = replace(page.lut, input_kind="camera_linear")
    page._update_state()
    assert not page.compare_button.isEnabled()
    assert page.compare_button.toolTip() == tr("reason.preview_needs_srgb_input", kind=tr("space.camera_linear"))
    page.send_to_compare()
    idle(qtbot, win)
    assert not ws.model.luts


def test_added_lut_is_kept_as_it_was(qtbot, win, ws, tmp_path):
    page = diagnose_with(qtbot, win, write_cube(tmp_path / "warm.cube", tinted((1.0, 0.8, 0.5))))
    page.compare_button.click()
    assert win.current_workspace().key == "compare"
    ws.add_photos([str(save_photo(tmp_path / "p.png"))])
    idle(qtbot, win)
    added = page.lut
    assert ws.model.luts[0].lut is added
    assert ws.model.luts[0].name == added.title
    kept = ws.model.cell_image(0, 1).copy()
    diagnose_with(qtbot, win, write_cube(tmp_path / "cool.cube", tinted((0.4, 0.8, 1.0))))
    assert page.lut is not added
    # rendered again from what the page holds, not from the page that built it
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(320))
    idle(qtbot, win)
    ws.size_combo.setCurrentIndex(ws.size_combo.findData(480))
    idle(qtbot, win)
    assert ws.model.luts[0].lut is added
    assert ws.model.cell_image(0, 1) == kept


def test_adding_the_same_built_lut_again_is_refused(qtbot, win, ws, tmp_path):
    page = diagnose_with(qtbot, win, write_cube(tmp_path / "warm.cube", tinted((1.0, 0.8, 0.5))))
    page.compare_button.click()
    idle(qtbot, win)
    page.compare_button.click()
    idle(qtbot, win)
    assert len(ws.model.luts) == 1
    assert "already" in log_text(win)


def test_log_lut_from_a_page_is_warned(qtbot, win, ws, tmp_path):
    page = diagnose_with(qtbot, win, write_cube(tmp_path / "log.cube", log_values()))
    page.compare_button.click()
    idle(qtbot, win)
    assert ws.model.luts[0].is_log
