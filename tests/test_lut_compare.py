"""LUT Compare: page splitting, cell placement, output names and JPEG publishing (no Qt)."""
from __future__ import annotations

import itertools
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image, JpegImagePlugin

from camera_profile_studio import images, lut_compare as lc
from camera_profile_studio.types import ImageData


def test_module_does_not_need_qt():
    code = "import sys, camera_profile_studio.lut_compare; sys.exit(any(m.split('.')[0] == 'PySide6' for m in sys.modules))"
    assert subprocess.run([sys.executable, '-c', code], check=False).returncode == 0


# ---------------------------------------------------------------- page splitting


def test_exactly_at_the_limit_is_one_page():
    assert lc.MAX_COLUMNS == 8 and lc.MAX_ROWS == 6   # the Before column counts as one of the eight
    pages = lc.paginate(6, 7)
    assert len(pages) == 1
    assert pages[0].photos == tuple(range(6)) and pages[0].luts == tuple(range(7))


@pytest.mark.parametrize('photos, luts, count', [(7, 7, 2), (6, 8, 2), (7, 8, 4), (1, 1, 1), (13, 15, 9)])
def test_one_over_the_limit_adds_pages(photos, luts, count):
    assert len(lc.paginate(photos, luts)) == count


@pytest.mark.parametrize('photos, luts', [(1, 1), (6, 7), (7, 7), (6, 8), (7, 8), (13, 15), (2, 3), (20, 1), (1, 20)])
def test_every_combination_appears_exactly_once(photos, luts):
    pages = lc.paginate(photos, luts)
    seen = [pair for page in pages for pair in itertools.product(page.photos, page.luts)]
    assert sorted(seen) == sorted(itertools.product(range(photos), range(luts)))
    assert [page.number for page in pages] == list(range(1, len(pages) + 1))
    for page in pages:
        assert 1 <= len(page.photos) <= lc.MAX_ROWS
        assert 1 <= len(page.luts) <= lc.MAX_COLUMNS - 1   # the Before column repeats on every page


def test_rows_stay_in_order_and_luts_go_to_the_next_page():
    pages = lc.paginate(2, 9)
    assert [page.luts for page in pages] == [tuple(range(7)), (7, 8)]
    assert [page.photos for page in pages] == [(0, 1), (0, 1)]


@pytest.mark.parametrize('photos, luts', [(0, 0), (0, 3), (3, 0)])
def test_nothing_to_show_is_no_page(photos, luts):
    assert lc.paginate(photos, luts) == []


# ---------------------------------------------------------------- cell placement


def test_cell_sizes_and_default():
    assert lc.CELL_SIDES == (320, 480, 720) and lc.DEFAULT_CELL_SIDE == 480


@pytest.mark.parametrize('side', lc.CELL_SIDES)
def test_sheet_has_room_for_the_headers_and_every_cell(side):
    page = lc.paginate(2, 3)[0]
    metrics = lc.SheetMetrics.for_side(side)
    width, height = metrics.sheet_size(page)
    assert width >= metrics.label_width + 4 * side and height >= metrics.header_height + 2 * side
    assert metrics.label_width > 0 and metrics.header_height > 0
    origins = [metrics.cell_origin(row, column) for row in range(2) for column in range(4)]
    assert len(set(origins)) == 8
    for x, y in origins:
        assert metrics.label_width <= x and x + side <= width
        assert metrics.header_height <= y and y + side <= height
    assert metrics.cell_origin(0, 1)[0] - metrics.cell_origin(0, 0)[0] >= side
    assert metrics.cell_origin(1, 0)[1] - metrics.cell_origin(0, 0)[1] >= side


@pytest.mark.parametrize('width, height, expected', [(480, 320, (0, 80)), (320, 480, (80, 0)), (480, 480, (0, 0)),
                                                     (100, 50, (190, 215))])
def test_photo_is_centred_in_its_square_cell(width, height, expected):
    assert lc.centered_offset(width, height, 480) == expected


# ---------------------------------------------------------------- output names


def test_contact_file_names():
    assert lc.contact_file_name(1) == 'contact-001.jpg'
    assert lc.contact_file_name(12) == 'contact-012.jpg'


def test_comparison_file_name_keeps_japanese_and_replaces_invalid_characters():
    assert lc.comparison_file_name('夕焼け', '暖色 LUT') == '夕焼け__暖色 LUT.jpg'
    assert lc.comparison_file_name('a<b>:c', 'd"e/f\\g|h?i*j') == 'a_b__c__d_e_f_g_h_i_j.jpg'
    assert lc.comparison_file_name('tab\there', 'line\nbreak') == 'tab_here__line_break.jpg'


@pytest.mark.parametrize('name', ['', '...', '   ', '. .'])
def test_a_name_with_nothing_usable_still_gives_a_valid_file_name(name):
    assert lc.comparison_file_name(name, 'lut') == '___lut.jpg'


def test_a_very_long_name_stays_within_the_file_name_limit():
    file_name = lc.comparison_file_name('写' * 400, 'l' * 400)
    assert len(file_name) <= 255 and file_name.endswith('.jpg')


def test_collision_keeps_the_first_and_reports_the_later_one():
    plan = lc.plan_comparison_files([(('a', 1), 'photo', 'warm'), (('b', 1), 'photo', 'warm'), (('a', 2), 'photo', 'cool')])
    assert plan.files == [(('a', 1), 'photo__warm.jpg'), (('a', 2), 'photo__cool.jpg')]
    assert plan.collisions == [(('b', 1), 'photo__warm.jpg', ('a', 1))]


def test_different_names_that_become_the_same_file_collide():
    plan = lc.plan_comparison_files([(1, 'a?b', 'lut'), (2, 'a*b', 'lut')])
    assert [key for key, _ in plan.files] == [1]
    assert [item[0] for item in plan.collisions] == [2]


def test_names_differing_only_in_case_collide_as_on_windows():
    plan = lc.plan_comparison_files([(1, 'Photo', 'LUT'), (2, 'photo', 'lut')])
    assert [key for key, _ in plan.files] == [1]
    assert [item[0] for item in plan.collisions] == [2]


def test_no_collision_for_distinct_names():
    plan = lc.plan_comparison_files([(1, 'a', 'x'), (2, 'a', 'y'), (3, 'b', 'x')])
    assert len(plan.files) == 3 and plan.collisions == []


def test_existing_files_lists_only_what_is_there(tmp_path):
    (tmp_path / 'contact-002.jpg').write_bytes(b'x')
    assert lc.existing_files(tmp_path, ['contact-001.jpg', 'contact-002.jpg']) == ['contact-002.jpg']
    assert lc.existing_files(tmp_path, ['contact-001.jpg']) == []


# ---------------------------------------------------------------- split image


def test_split_image_left_is_before_right_is_after_with_a_line_in_the_middle():
    before = np.full((40, 100, 3), 0.25, dtype=np.float32)
    after = np.full((40, 100, 3), 0.75, dtype=np.float32)
    out = lc.split_image(before, after)
    assert out.dtype == np.uint8 and out.shape == (40, 100, 3)
    assert (out[:, :48] == 64).all() and (out[:, 52:] == 191).all()
    assert (out[:, 49:51] == 255).all() and (out[:, 48] == 0).all() and (out[:, 51] == 0).all()


def test_split_image_rejects_different_sizes():
    with pytest.raises(ValueError):
        lc.split_image(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)))


@pytest.mark.parametrize('width', [1, 2, 3])
def test_split_image_survives_a_tiny_photo(width):
    assert lc.split_image(np.zeros((2, width, 3)), np.ones((2, width, 3))).shape == (2, width, 3)


# ---------------------------------------------------------------- publishing


def _pixels(height=32, width=48):
    return np.full((height, width, 3), (200, 100, 50), dtype=np.uint8)


def test_jpeg_is_quality_95_444_and_readable(tmp_path):
    assert lc.JPEG_QUALITY == 95
    folder = tmp_path / '書き出し先'
    folder.mkdir()
    target = folder / '日本語の名前.jpg'
    lc.write_jpeg_exclusive(_pixels(), target)
    with Image.open(target) as image:
        assert image.format == 'JPEG' and image.mode == 'RGB' and image.size == (48, 32)
        assert JpegImagePlugin.get_sampling(image) == 0
        assert np.abs(np.asarray(image, dtype=int) - (200, 100, 50)).max() <= 2
    assert [p.name for p in folder.iterdir()] == ['日本語の名前.jpg']


def test_jpeg_never_replaces_an_existing_file_and_leaves_no_temporary(tmp_path):
    target = tmp_path / 'contact-001.jpg'
    target.write_bytes(b'precious')
    with pytest.raises(FileExistsError):
        lc.write_jpeg_exclusive(_pixels(), target)
    assert target.read_bytes() == b'precious'
    assert [p.name for p in tmp_path.iterdir()] == ['contact-001.jpg']


def test_failed_jpeg_leaves_no_partial_file(tmp_path):
    with pytest.raises(Exception):
        lc.write_jpeg_exclusive(np.zeros((0, 0, 3), dtype=np.uint8), tmp_path / 'broken.jpg')
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- shrinking


def _image(height, width):
    rgb = np.linspace(0, 1, height * width * 3, dtype=np.float32).reshape(height, width, 3)
    return ImageData(rgb, None)


@pytest.mark.parametrize('height, width, expected', [(400, 800, (180, 360)), (800, 400, (360, 180))])
def test_shrink_image_limits_the_long_side(height, width, expected):
    out = images.shrink_image(_image(height, width), 360)
    assert out.rgb.shape[:2] == expected
    assert out.rgb.min() >= 0 and out.rgb.max() <= 1


def test_shrink_image_never_enlarges():
    image = _image(40, 80)
    assert images.shrink_image(image, 360) is image
