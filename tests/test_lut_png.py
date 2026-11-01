"""PNG LUT export (0.2.2): Hald CLUT and horizontal strip images.

The expected layouts are built here from first principles, independently of
the exporter: Hald CLUT in ImageMagick ``hald:`` order (red fastest, then
green, then blue, rows top to bottom) and the Unreal Engine / ReShade strip
(tile = blue, x inside a tile = red, y = green downwards).
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from camera_profile_studio.cli import main
from camera_profile_studio.errors import ExportError
from camera_profile_studio.lut import decode_png, export_lut, generate_lut, png_layout, sample_lut
from camera_profile_studio.types import SpaceSpec


class Identity:
    input_space = output_space = SpaceSpec('srgb')

    def apply(self, rgb, context=None):
        return np.asarray(rgb, np.float32)


class Warm:
    input_space = output_space = SpaceSpec('srgb')

    def apply(self, rgb, context=None):
        rgb = np.asarray(rgb, np.float32)
        return np.clip(np.stack([0.9 * rgb[:, 0] + 0.1 * rgb[:, 1], rgb[:, 1] ** 1.1,
                                 0.8 * rgb[:, 2] + 0.2 * rgb[:, 0]], -1), 0, 1).astype(np.float32)


def _reference(layout):
    n = layout['grid']
    if layout['layout'] == 'hald':
        index = np.arange(layout['width'] * layout['height'])
        rgb = np.stack([index % n, (index // n) % n, index // (n * n)], -1)
        return rgb.reshape(layout['height'], layout['width'], 3)
    y, x = np.mgrid[0:n, 0:n * n]
    rgb = np.stack([x % n, y, x // n], -1)
    return rgb[::-1] if layout['flipped'] else rgb


@pytest.mark.parametrize('size, grid_hald, grid_strip', [(17, 16, 16), (33, 36, 32), (65, 64, 64)])
def test_layout_geometry(size, grid_hald, grid_strip):
    hald = png_layout('hald_png', size)
    assert (hald['grid'], hald['level'], hald['width'], hald['height'], hald['bit_depth']) == \
        (grid_hald, int(grid_hald ** .5), int(grid_hald ** .5) ** 3, int(grid_hald ** .5) ** 3, 16)
    strip = png_layout('strip_png', size)
    assert (strip['grid'], strip['width'], strip['height'], strip['bit_depth'], strip['flipped']) == \
        (grid_strip, grid_strip ** 2, grid_strip, 8, False)
    assert png_layout('strip_png_flipped', size)['flipped']


@pytest.mark.parametrize('fmt', ['hald_png', 'strip_png', 'strip_png_flipped'])
@pytest.mark.parametrize('size', [17, 33, 65])
def test_identity_lut_matches_independent_layout(tmp_path, fmt, size):
    result = export_lut(generate_lut(Identity(), size=size), tmp_path / 'id.png', format=fmt)
    layout = png_layout(fmt, size)
    image = decode_png(result.cube_path.read_bytes())
    scale = 65535 if layout['bit_depth'] == 16 else 255
    assert image.shape == (layout['height'], layout['width'], 3)
    assert image.dtype == (np.uint16 if scale == 65535 else np.uint8)
    expected = _reference(layout) / (layout['grid'] - 1)
    # resampling an identity LUT is exact up to float32; only quantization remains
    assert np.abs(image / scale - expected).max() <= 0.5 / scale + 1e-6


def test_png_values_are_the_tetrahedral_lut_at_the_png_grid(tmp_path):
    lut = generate_lut(Warm(), size=33)
    for fmt in ('hald_png', 'strip_png'):
        result = export_lut(lut, tmp_path / f'{fmt}.png', format=fmt)
        layout = png_layout(fmt, 33)
        n, scale = layout['grid'], (65535 if layout['bit_depth'] == 16 else 255)
        image = decode_png(result.cube_path.read_bytes()).astype(np.float64) / scale
        coords = _reference(layout).reshape(-1, 3) / (n - 1)
        expected = sample_lut(lut, coords.astype(np.float32)).reshape(image.shape)
        assert np.abs(image - expected).max() <= 0.5 / scale + 1e-5


def test_png_file_is_standard_and_sidecar_describes_it(tmp_path):
    from PIL import Image
    import cv2

    lut = generate_lut(Warm(), size=17)
    strip = export_lut(lut, tmp_path / 'strip.png', format='strip_png')
    hald = export_lut(lut, tmp_path / 'hald.png', format='hald_png')
    # independent decoders read the same values (Pillow: 8-bit; OpenCV: 16-bit)
    assert np.array_equal(np.asarray(Image.open(strip.cube_path).convert('RGB')), decode_png(strip.cube_path.read_bytes()))
    assert np.array_equal(cv2.imread(str(hald.cube_path), cv2.IMREAD_UNCHANGED)[:, :, ::-1], decode_png(hald.cube_path.read_bytes()))
    for result, fmt in ((strip, 'strip_png'), (hald, 'hald_png')):
        blob = result.cube_path.read_bytes()
        assert all(chunk not in blob for chunk in (b'sRGB', b'gAMA', b'iCCP', b'cHRM'))  # values are data, not colour
        meta = json.loads(result.metadata_path.read_text(encoding='utf-8'))
        assert meta['format'] == fmt and meta['png_sha256'] == hashlib.sha256(blob).hexdigest() == result.sha256
        assert meta['size'] == 17 and meta['png']['grid'] == 16 and meta['png']['resampling'] == 'tetrahedral from 17^3'
        assert meta['validation']['valid'] and meta['provenance']


def test_png_export_refuses_wrong_suffix_and_existing_files(tmp_path):
    lut = generate_lut(Identity(), size=17)
    with pytest.raises(ExportError) as wrong:
        export_lut(lut, tmp_path / 'x.cube', format='hald_png')
    assert wrong.value.code == 'invalid_export_path' and not list(tmp_path.iterdir())
    with pytest.raises(ExportError):
        export_lut(lut, tmp_path / 'x.png', format='cube')
    path = tmp_path / 'x.png'
    export_lut(lut, path, format='strip_png')
    before = path.read_bytes()
    with pytest.raises(ExportError):
        export_lut(lut, path, format='strip_png')
    assert path.read_bytes() == before
    export_lut(generate_lut(Warm(), size=17), path, format='strip_png', overwrite=True)
    assert path.read_bytes() != before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['x.json', 'x.png']


def test_cli_png_formats(tmp_path, capsys):
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'schema_version': 1, 'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'linear_srgb'}}))
    out = tmp_path / 'technical_hald.png'
    assert main(['convert', '--request', str(request), '--output', str(out), '--size', '17', '--format', 'hald-png']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['export']['validation']['valid'] and decode_png(out.read_bytes()).shape == (64, 64, 3)
    assert json.loads(out.with_suffix('.json').read_text())['format'] == 'hald_png'
    bad = tmp_path / 'strip.cube'
    assert main(['convert', '--request', str(request), '--output', str(bad), '--format', 'strip-png']) == 3
    assert json.loads(capsys.readouterr().err)['code'] == 'invalid_export_path' and not bad.exists()
