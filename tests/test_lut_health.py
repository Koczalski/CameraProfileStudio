"""LUT health check engine: loaders for every format, the eight findings, the input-kind estimate,
repair, resampling to the engine LUT, determinism, cancellation and the JSON sidecar hook."""
import dataclasses
import io
import json
import subprocess
import sys

import numpy as np
import pytest

from camera_profile_studio import lut_health as lh
from camera_profile_studio.errors import CancelledError, ExportError, ValidationError
from camera_profile_studio.image_look import oklab_to_srgb, srgb_to_oklab
from camera_profile_studio.lut import export_cube, export_lut, png_lut_pixels, resample_lut, read_cube
from camera_profile_studio.types import JobContext, Lut3D, SpaceSpec

SRGB = SpaceSpec('srgb')


# ---------------------------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------------------------
def grid(n):
    axis = np.linspace(0.0, 1.0, n)
    return np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)


def separable(f, n):
    t = f(np.linspace(0.0, 1.0, n))
    return np.stack(np.meshgrid(t, t, t, indexing='ij'), axis=-1)


def loaded(values, name='synthetic', fmt='cube'):
    values = np.asarray(values, dtype=np.float64)
    return lh.LoadedLut(values, values.shape[0], name, fmt, f'{name}.cube')


def lut3d(values, title='health test'):
    return Lut3D(np.ascontiguousarray(values, np.float32), SRGB, SRGB, title, 1.0, (('evidence', 'synthetic'),))


def smooth_values(n, tint=0.0):
    """A gentle creative grade made in Oklab: mild S-curve, -15 % chroma, 4 degree hue turn (+ optional warm tint)."""
    lab = srgb_to_oklab(grid(n))
    light = lab[..., 0] + 0.04 * np.sin(np.pi * lab[..., 0])
    angle = np.radians(4.0)
    warm = tint * (1 - np.abs(2 * lab[..., 0] - 1))
    a = 0.85 * (lab[..., 1] * np.cos(angle) - lab[..., 2] * np.sin(angle)) + warm
    b = 0.85 * (lab[..., 1] * np.sin(angle) + lab[..., 2] * np.cos(angle)) + warm
    return np.clip(oklab_to_srgb(np.stack([light, a, b], axis=-1)), 0, 1)


def non_ok(report):
    return {f.code: f.severity for f in report.findings if f.severity != 'ok'}


def signature(report):
    return (report.size, report.overall, report.kind, report.provenance, report.gray_axis.tobytes(),
            report.hue_samples.tobytes(),
            tuple((f.code, f.severity, f.value, f.threshold, f.count, f.locations.tobytes(), f.details)
                  for f in report.findings))


def cube_bytes(values, *, header=(), newline='\n', bom=False, size=True):
    n = values.shape[0]
    rows = values.transpose(2, 1, 0, 3).reshape(-1, 3)        # red fastest
    buffer = io.StringIO()
    np.savetxt(buffer, rows, fmt='%.9g')
    lines = list(header) + ([f'LUT_3D_SIZE {n}'] if size else []) + buffer.getvalue().splitlines()
    data = newline.join(lines) + newline
    return (b'\xef\xbb\xbf' if bom else b'') + data.encode('utf-8')


def write_cube(path, values, **kw):
    path.write_bytes(cube_bytes(values, **kw))
    return path


class CancelAfter:
    def __init__(self, calls):
        self.calls, self.seen = calls, 0

    def is_cancelled(self):
        self.seen += 1
        return self.seen > self.calls


# ---------------------------------------------------------------------------------------------
# log curve implementations (published anchor values)
# ---------------------------------------------------------------------------------------------
CODE = 0.5 / 1023 + 1e-6      # published 10-bit integer code values are rounded: half a code of tolerance


@pytest.mark.parametrize('curve,black,grey,tolerance', [
    ('S-Log3', 95 / 1023, 420 / 1023, CODE),          # Sony: 0 % -> 95, 18 % -> 420, 90 % -> 598 (10 bit)
    ('F-Log', 95 / 1023, 470 / 1023, CODE),           # Fujifilm data sheet: 95, 470, 705
    ('F-Log2', 0.092864, 0.391007, 2e-5),
    ('V-Log', 128 / 1023, 433 / 1023, CODE),          # Panasonic: 0 % -> 128, 18 % -> 433
    ('N-Log', 0.124373, 372 / 1023, CODE),            # Nikon: 18 % -> 372
    ('C-Log3', 0.125122, 0.343389, 2e-5),             # Canon: 18 % grey = 34.3 %
    ('ARRI LogC3', 0.092809, 0.391007, 2e-5),         # ARRI: EI 800, 18 % grey = 0.391
    ('Apple Log', 0.150476, 0.488272, 2e-5),
])
def test_log_curve_anchors_and_round_trip(curve, black, grey, tolerance):
    assert float(lh.log_encode(curve, 0.0)) == pytest.approx(black, abs=tolerance)
    assert float(lh.log_encode(curve, 0.18)) == pytest.approx(grey, abs=tolerance)
    x = np.concatenate([np.linspace(0, 1, 101), [2.0, 8.0, 16.0]])
    assert np.allclose(lh.log_decode(curve, lh.log_encode(curve, x)), x, atol=1e-6)
    code = np.linspace(0.2, 1, 50)
    assert np.all(np.diff(lh.log_encode(curve, lh.log_decode(curve, code))) > 0)


def test_rec709_oetf_round_trip_and_value():
    x = np.linspace(0, 1, 101)
    assert np.allclose(lh.rec709_inverse_oetf(lh.rec709_oetf(x)), x, atol=1e-12)
    assert float(lh.rec709_oetf(0.18)) == pytest.approx(0.40905, abs=1e-4)


def test_delta_e2000_matches_sharma_test_pairs():
    pairs = [((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
             ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
             ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
             ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
             ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644)]
    for a, b, expected in pairs:
        assert float(lh.delta_e2000(np.array(a), np.array(b))) == pytest.approx(expected, abs=5e-4)


# ---------------------------------------------------------------------------------------------
# loading: .cube
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('n', [2, 17, 32, 33, 64, 65])
def test_cube_any_size_red_fastest(tmp_path, n):
    values = np.random.default_rng(n).random((n, n, n, 3))
    path = write_cube(tmp_path / f'lut{n}.cube', values, header=['# comment', 'TITLE "Sized"'])
    result = lh.load_lut_file(path)
    assert (result.size, result.source_format, result.title) == (n, 'cube', 'Sized')
    assert result.values.shape == (n, n, n, 3) and result.values.dtype == np.float64
    assert np.allclose(result.values, values, atol=1e-8, rtol=0)
    assert not result.values.flags.writeable and result.warnings == ()
    assert result.domain_min == (0, 0, 0) and result.domain_max == (1, 1, 1) and result.source_path == path


def test_cube_domain_not_unit_bom_crlf_and_comments(tmp_path):
    values = np.random.default_rng(1).random((5, 5, 5, 3))
    path = write_cube(tmp_path / 'dom.cube', values, bom=True, newline='\r\n',
                      header=['# made by hand', 'TITLE "Domain test"  ', 'DOMAIN_MIN -0.1 -0.1 -0.1',
                              'DOMAIN_MAX 1.5 1.5 1.5   # wide'])
    result = lh.load_lut_file(path)
    assert result.title == 'Domain test'
    assert 'domain_not_unit' in result.warnings
    assert result.domain_min == (-0.1, -0.1, -0.1) and result.domain_max == (1.5, 1.5, 1.5)
    assert np.allclose(result.values, values, atol=1e-8)            # values are kept, not rescaled


def test_cube_values_outside_unit_kept_with_warning(tmp_path):
    values = np.random.default_rng(2).random((3, 3, 3, 3))
    values[1, 1, 1] = (1.4, -0.2, 0.5)
    result = lh.load_lut_file(write_cube(tmp_path / 'out.cube', values))
    assert 'values_outside_unit' in result.warnings
    assert result.values[1, 1, 1].tolist() == pytest.approx([1.4, -0.2, 0.5])


def test_cube_nonfinite_values_load_and_warn(tmp_path):
    values = np.random.default_rng(2).random((3, 3, 3, 3))
    values[2, 1, 0, 1] = np.nan
    result = lh.load_lut_file(write_cube(tmp_path / 'nan.cube', values))
    assert 'values_nonfinite' in result.warnings and np.isnan(result.values[2, 1, 0, 1])
    assert lh.diagnose_lut(result).finding('range').severity == 'error'


def test_cube_1d_only_is_an_error(tmp_path):
    path = tmp_path / 'shaper.cube'
    path.write_text('TITLE "1D"\nLUT_1D_SIZE 4\n0 0 0\n0.3 0.3 0.3\n0.6 0.6 0.6\n1 1 1\n')
    with pytest.raises(ValidationError) as caught:
        lh.load_lut_file(path)
    assert caught.value.code == 'lut_1d_only'


def test_cube_1d_shaper_before_3d_block_is_ignored(tmp_path):
    values = np.random.default_rng(3).random((2, 2, 2, 3))
    shaper = '\n'.join(f'{v} {v} {v}' for v in (0.0, 0.25, 0.7, 1.0))
    body = cube_bytes(values).decode().splitlines()
    path = tmp_path / 'both.cube'
    path.write_text('LUT_1D_SIZE 4\n' + body[0] + '\n' + shaper + '\n' + '\n'.join(body[1:]) + '\n')
    result = lh.load_lut_file(path)
    assert 'shaper_1d_ignored' in result.warnings
    assert np.allclose(result.values, values, atol=1e-8)


@pytest.mark.parametrize('text,code', [
    ('LUT_3D_SIZE 130\n', 'lut_size_unsupported'),
    ('LUT_3D_SIZE 1\n0 0 0\n', 'lut_size_unsupported'),
    ('TITLE "no size"\n0 0 0\n', 'lut_unreadable'),
    ('LUT_3D_SIZE 2\n0 0 0\n1 1 1\n', 'lut_unreadable'),                       # too few rows
    ('LUT_3D_SIZE 2\n' + '0 0 0\n' * 8 + '0 0 0\n', 'lut_unreadable'),           # too many rows
    ('LUT_3D_SIZE 2\n' + '0 0 x\n' * 8, 'lut_unreadable'),
    ('LUT_3D_SIZE two\n', 'lut_unreadable'),
    ('DOMAIN_MIN 0 0\nLUT_3D_SIZE 2\n', 'lut_unreadable'),
])
def test_cube_malformed_files_raise_structured_errors(tmp_path, text, code):
    path = tmp_path / 'bad.cube'
    path.write_text(text)
    with pytest.raises(ValidationError) as caught:
        lh.load_lut_file(path)
    assert caught.value.code == code


def test_unreadable_files(tmp_path):
    for target, code in ((tmp_path / 'missing.cube', 'lut_unreadable'), (tmp_path, 'lut_unreadable'),
                         (tmp_path / 'file.xyz', 'lut_unreadable')):
        if target.suffix == '.xyz':
            target.write_text('x')
        with pytest.raises(ValidationError) as caught:
            lh.load_lut_file(target)
        assert caught.value.code == code
    with pytest.raises(ValidationError):
        lh.load_lut_file('')


def test_cps_cube_is_recognised_and_equals_read_cube(tmp_path):
    lut = lut3d(smooth_values(17), title='CPS LUT')
    export_cube(lut, tmp_path / 'cps.cube')
    result = lh.load_lut_file(tmp_path / 'cps.cube')
    assert result.source_format == 'cube_cps' and result.title == 'CPS LUT'
    reference = read_cube(tmp_path / 'cps.cube', input_space=SRGB, output_space=SRGB)
    assert np.array_equal(result.values.astype(np.float32), reference.values)


# ---------------------------------------------------------------------------------------------
# loading: .3dl
# ---------------------------------------------------------------------------------------------
def write_3dl(path, values, depth, *, header=(), float_values=False):
    n = values.shape[0]
    breakpoints = np.round(np.linspace(0, 1023, n)).astype(int)
    lines = list(header) + [' '.join(map(str, breakpoints))]
    rows = values.reshape(-1, 3)                                    # blue fastest
    if float_values:
        lines += [' '.join(f'{v:.6f}' for v in row) for row in rows]
    else:
        scaled = np.round(rows * depth).astype(int)
        lines += [' '.join(map(str, row)) for row in scaled]
    path.write_text('\n'.join(lines) + '\n')


@pytest.mark.parametrize('depth', [1023, 4095, 65535])
def test_3dl_integer_bit_depths_blue_fastest(tmp_path, depth):
    values = np.random.default_rng(depth).random((5, 5, 5, 3))
    values[-1, -1, -1] = 1.0                                         # reaches full scale: depth is inferred from the maximum
    write_3dl(tmp_path / 'lut.3dl', values, depth, header=['3DMESH', '# comment'])
    result = lh.load_lut_file(tmp_path / 'lut.3dl')
    assert (result.size, result.source_format) == (5, '3dl')
    assert np.abs(result.values - values).max() <= 0.5 / depth + 1e-12
    assert result.title == 'lut'


def test_3dl_float_values_and_mesh_header(tmp_path):
    values = np.random.default_rng(4).random((4, 4, 4, 3))
    write_3dl(tmp_path / 'float.3dl', values, 1, float_values=True)
    assert np.abs(lh.load_lut_file(tmp_path / 'float.3dl').values - values).max() < 1e-6
    write_3dl(tmp_path / 'mesh.3dl', values, 4095, header=['Mesh 10 12'])
    assert np.abs(lh.load_lut_file(tmp_path / 'mesh.3dl').values - values).max() <= 0.5 / 4095 + 1e-12


def test_3dl_errors(tmp_path):
    path = tmp_path / 'bad.3dl'
    path.write_text('0 512 1023\n0 0 0\n1 1 1\n')
    with pytest.raises(ValidationError) as caught:
        lh.load_lut_file(path)
    assert caught.value.code == 'lut_unreadable'
    path.write_text('0 1023 512\n' + '0 0 0\n' * 8)
    with pytest.raises(ValidationError):
        lh.load_lut_file(path)


# ---------------------------------------------------------------------------------------------
# loading: Hald and strip images
# ---------------------------------------------------------------------------------------------
def pil_save(path, array):
    from PIL import Image
    Image.fromarray(array).save(path)


def hald_pixels(values):
    n = values.shape[0]
    side = round(n ** 1.5)
    return values.transpose(2, 1, 0, 3).reshape(side, side, 3)


def strip_pixels(values):
    n = values.shape[0]
    return values.transpose(1, 2, 0, 3).reshape(n, n * n, 3)


@pytest.mark.parametrize('size,level', [(17, 4), (65, 8)])
def test_engine_hald_png_levels_4_and_8(tmp_path, size, level):
    lut = lut3d(smooth_values(size), title='Hald fixture')
    export_lut(lut, tmp_path / 'hald.png', format='hald_png')
    result = lh.load_lut_file(tmp_path / 'hald.png')
    assert result.size == level ** 2 and result.source_format == 'hald_png' and result.title == 'Hald fixture'
    assert np.abs(result.values - resample_lut(lut, level ** 2)).max() <= 0.5 / 65535 + 1e-7


def test_strip_png_flipped_and_unflipped_engine_exports_agree(tmp_path):
    lut = lut3d(smooth_values(33))
    export_lut(lut, tmp_path / 'a.png', format='strip_png')
    export_lut(lut, tmp_path / 'b.png', format='strip_png_flipped')
    plain, flipped = lh.load_lut_file(tmp_path / 'a.png'), lh.load_lut_file(tmp_path / 'b.png')
    assert plain.size == flipped.size == 32 and plain.source_format == flipped.source_format == 'strip_png'
    assert 'strip_green_flipped' in flipped.warnings and 'strip_green_flipped' not in plain.warnings
    assert np.array_equal(plain.values, flipped.values)
    assert np.abs(plain.values - resample_lut(lut, 32)).max() <= 0.5 / 255 + 1e-7


def test_foreign_strip_33_8bit_and_orientation_warning(tmp_path):
    values = smooth_values(33)
    pil_save(tmp_path / 'strip33.png', np.round(strip_pixels(values) * 255).astype(np.uint8))
    result = lh.load_lut_file(tmp_path / 'strip33.png')
    assert result.size == 33 and 'strip_orientation_assumed' in result.warnings
    assert np.abs(result.values - values).max() <= 0.5 / 255 + 1e-12


def test_foreign_hald_8bit_pillow_and_16bit_filtered_opencv(tmp_path):
    import cv2
    values = smooth_values(16)
    pil_save(tmp_path / 'h8.png', np.round(hald_pixels(values) * 255).astype(np.uint8))
    assert np.abs(lh.load_lut_file(tmp_path / 'h8.png').values - values).max() <= 0.5 / 255 + 1e-12
    sixteen = np.round(hald_pixels(values) * 65535).astype(np.uint16)
    assert cv2.imwrite(str(tmp_path / 'h16.png'), sixteen[..., ::-1])       # OpenCV writes filtered rows
    result = lh.load_lut_file(tmp_path / 'h16.png')
    assert result.size == 16 and np.abs(result.values - values).max() <= 0.5 / 65535 + 1e-12


def test_tiff_16bit_strip_and_float_hald(tmp_path):
    import tifffile
    values = smooth_values(17)
    tifffile.imwrite(tmp_path / 's.tif', np.round(strip_pixels(values) * 65535).astype(np.uint16), photometric='rgb')
    assert np.abs(lh.load_lut_file(tmp_path / 's.tif').values - values).max() <= 0.5 / 65535 + 1e-12
    small = smooth_values(9)
    tifffile.imwrite(tmp_path / 'h.tiff', hald_pixels(small).astype(np.float32), photometric='rgb')
    result = lh.load_lut_file(tmp_path / 'h.tiff')
    assert result.size == 9 and result.source_format == 'hald_png'
    assert np.abs(result.values - small).max() < 1e-6


def test_png_bad_geometry_and_unreadable_png(tmp_path):
    pil_save(tmp_path / 'ten.png', np.zeros((10, 10, 3), np.uint8))
    pil_save(tmp_path / 'wide.png', np.zeros((33, 1000, 3), np.uint8))
    for name in ('ten.png', 'wide.png'):
        with pytest.raises(ValidationError) as caught:
            lh.load_lut_file(tmp_path / name)
        assert caught.value.code == 'lut_png_layout'
    (tmp_path / 'junk.png').write_bytes(b'not a png at all')
    with pytest.raises(ValidationError) as caught:
        lh.load_lut_file(tmp_path / 'junk.png')
    assert caught.value.code == 'lut_unreadable'
    pil_save(tmp_path / 'big.png', np.zeros((1728, 1728, 3), np.uint8))
    with pytest.raises(ValidationError) as caught:                    # Hald level 12 = grid 144 > 129
        lh.load_lut_file(tmp_path / 'big.png')
    assert caught.value.code == 'lut_size_unsupported'


def test_loaded_lut_validation():
    good = np.zeros((2, 2, 2, 3))
    assert lh.LoadedLut(good, 2, 't', 'cube', 'x.cube').source_path.name == 'x.cube'
    with pytest.raises(ValidationError) as caught:
        lh.LoadedLut(np.zeros((1, 1, 1, 3)), 1, 't', 'cube', 'x.cube')
    assert caught.value.code == 'lut_size_unsupported'
    with pytest.raises(ValidationError):
        lh.LoadedLut(np.zeros((2, 2, 3, 3)), 2, 't', 'cube', 'x.cube')
    with pytest.raises(ValidationError):
        lh.LoadedLut(good, 3, 't', 'cube', 'x.cube')
    with pytest.raises(ValidationError):
        lh.LoadedLut(good, 2, 't', 'bogus', 'x.cube')
    with pytest.raises(ValidationError):
        lh.LoadedLut(good, 2, 't', 'cube', 'x.cube', domain_min=(0, 0, 0), domain_max=(0, 1, 1))
    item = lh.LoadedLut(good, 2, 't', 'cube', 'x.cube')
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.title = 'other'
    with pytest.raises(ValueError):
        item.values[0, 0, 0, 0] = 1.0



# ---------------------------------------------------------------------------------------------
# diagnosis: clean LUTs
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('n', [2, 3, 4, 5, 9, 16, 17, 33, 36, 64, 65])
def test_identity_is_healthy_at_every_size(n):
    report = lh.diagnose_lut(loaded(grid(n)))
    assert report.overall == 'ok' and non_ok(report) == {}
    assert [f.code for f in report.findings] == list(lh.FINDING_CODES)
    assert report.kind.kind == 'identity' and report.kind.confidence > 0.9
    assert all(f.locations.shape == (0, 3) and f.count == 0 for f in report.findings)
    assert report.size == n


def test_gently_graded_lut_and_engine_lut_input_have_no_findings():
    values = smooth_values(33)
    assert lh.diagnose_lut(loaded(values)).overall == 'ok'
    from_engine = lh.diagnose_lut(lut3d(values))                       # a Lut3D is accepted directly
    assert from_engine.overall == 'ok' and from_engine.size == 33


def test_report_structure_arrays_and_provenance():
    report = lh.diagnose_lut(loaded(smooth_values(17)))
    assert report.gray_axis.shape == (257, 7) and report.hue_samples.shape[1] == 6
    assert np.allclose(report.gray_axis[:, 0], np.linspace(0, 1, 257))
    assert np.all(np.isfinite(report.gray_axis)) and np.all(np.isfinite(report.hue_samples))
    assert report.gray_axis[0, 1:4].tolist() == [0, 0, 0] and np.allclose(report.gray_axis[-1, 1:4], 1)
    assert report.hue_samples.shape[0] <= 13 ** 3 + lh.MAX_FLAGGED_SAMPLES
    provenance = dict(report.provenance)
    thresholds = json.loads(provenance['thresholds'])
    assert thresholds['banding_gain'] == lh.BANDING_GAIN == 4.0 and thresholds['tone_jump_ratio'] == 12.0
    assert thresholds['hue_fold_degrees'] == 60.0 and thresholds['clip_fraction'] == 0.05
    assert provenance['schema'] == lh.SCHEMA and provenance['lut_size'] == '17'
    for array in (report.gray_axis, report.hue_samples, report.findings[0].locations):
        assert not array.flags.writeable
        with pytest.raises(ValueError):
            array[...] = 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.overall = 'error'
    assert report.finding('range') is report.findings[0] and report.kind.note_key.startswith('health.kind_note.')


def test_grey_axis_follows_the_diagonal_exactly():
    values = smooth_values(17)
    report = lh.diagnose_lut(loaded(values))
    idx = np.arange(17)
    nodes = report.gray_axis[::16]                                      # 257 samples include the 17 nodes
    assert np.allclose(nodes[:, 1:4], values[idx, idx, idx], atol=1e-12)
    lab = srgb_to_oklab(values[idx, idx, idx])
    assert np.allclose(nodes[:, 4:], lab, atol=1e-9)


def test_65_cubed_diagnosis_is_fast_and_clean():
    import time
    values = smooth_values(65)
    started = time.perf_counter()
    report = lh.diagnose_lut(loaded(values))
    elapsed = time.perf_counter() - started
    assert report.overall == 'ok'
    assert elapsed < 2.0, f'65^3 diagnosis took {elapsed:.2f} s'


@pytest.mark.parametrize('n', [2, 3, 4, 7])
def test_random_garbage_and_nonfinite_never_crash(n):
    rng = np.random.default_rng(n)
    values = rng.uniform(-0.5, 1.5, (n, n, n, 3))
    values[0, 0, 0, 1] = np.inf
    if n > 2:
        values[1, 1, 1, 0] = np.nan
    report = lh.diagnose_lut(loaded(values))
    assert report.overall == 'error' and report.finding('range').severity == 'error'
    assert report.kind.kind == 'unknown'
    assert all(np.isfinite(f.value) for f in report.findings)


# ---------------------------------------------------------------------------------------------
# diagnosis: each synthetic defect triggers its finding
# ---------------------------------------------------------------------------------------------
def test_defect_tone_reversal():
    def dip(t):
        x = np.clip((t - 0.3) / 0.4, 0, 1)
        return t - 0.25 * np.sin(np.pi * x) ** 2
    report = lh.diagnose_lut(loaded(separable(dip, 33)))
    assert non_ok(report) == {'tone_reversal': 'warning'}
    finding = report.finding('tone_reversal')
    assert finding.value > finding.threshold == lh.REVERSAL_EDGE_FRACTION and finding.count > 0
    assert dict(finding.details)['grey_axis_reversals'] != '0'
    assert len(finding.locations) <= 64 and finding.locations.shape[1] == 3
    centre = grid(33)[tuple(finding.locations[0])]
    assert 0.3 <= centre.max() <= 0.7 or 0.3 <= centre.min() <= 0.7          # inside the reversed band


def test_defect_one_reversal_only_on_the_grey_axis_is_a_warning():
    values = grid(17).copy()
    values[8, 8, 8] = values[7, 7, 7] - 0.03                                    # one grey node dips below its neighbour
    report = lh.diagnose_lut(loaded(values))
    finding = report.finding('tone_reversal')
    assert finding.severity == 'warning' and finding.value < lh.REVERSAL_EDGE_FRACTION
    assert dict(finding.details)['grey_axis_reversals'] == '1'
    assert tuple(finding.locations[0]) == (7, 7, 7) or tuple(finding.locations[0]) == (8, 8, 8)


def test_defect_grey_tint_is_info_only():
    g = grid(33)
    tint = np.clip(g + np.array([0.02, 0.0, -0.02]) * (1 - np.abs(2 * g.mean(-1, keepdims=True) - 1)), 0, 1)
    report = lh.diagnose_lut(loaded(tint))
    assert non_ok(report) == {'gray_tint': 'info'} and report.overall == 'info'
    finding = report.finding('gray_tint')
    assert 0.005 < finding.value < 0.02 and finding.count > 0
    assert dict(finding.details)['threshold_applies_to'] == 'second_difference'


def test_defect_non_smooth_grey_tint_is_a_warning():
    values = grid(33).copy()
    idx = np.arange(33)
    sign = np.where(idx % 2 == 0, 1.0, -1.0)
    values[idx, idx, idx, 0] += 0.008 * sign                                    # tint flips sign from node to node
    values[idx, idx, idx, 2] -= 0.008 * sign
    report = lh.diagnose_lut(loaded(np.clip(values, 0, 1)))
    assert non_ok(report) == {'gray_tint': 'warning'}
    finding = report.finding('gray_tint')
    assert float(dict(finding.details)['max_second_difference']) > lh.GRAY_TINT_D2 and finding.count >= 1
    assert all(loc[0] == loc[1] == loc[2] for loc in finding.locations)          # grey-axis nodes


def test_defect_clipping_and_its_inherent_flat_plateau():
    report = lh.diagnose_lut(loaded(separable(lambda t: np.minimum(t * 1.1, 1.0), 33)))
    finding = report.finding('clipping')
    assert finding.severity == 'warning' and 0.05 < finding.value < 0.3 and finding.count > 0
    assert set(non_ok(report)) <= {'clipping', 'flatten', 'tone_jump'}          # a plateau is also flat
    assert (grid(33)[tuple(finding.locations[0])] < 1).all()
    low = lh.diagnose_lut(loaded(separable(lambda t: np.maximum(t * 1.2 - 0.2, 0.0), 33)))
    assert low.finding('clipping').severity == 'warning' and dict(low.finding('clipping').details)['low_nodes'] != '0'


def test_clipping_at_the_cube_corners_only_is_not_a_warning():
    assert lh.diagnose_lut(loaded(separable(lambda t: np.minimum(t * 1.001, 1.0), 33))).finding('clipping').severity == 'ok'


def test_defect_tone_jump_single_node_spike():
    values = grid(17).copy()
    values[8, 5, 9] += (0.05, 0.0, 0.0)
    report = lh.diagnose_lut(loaded(np.clip(values, 0, 1)))
    assert non_ok(report) == {'tone_jump': 'warning'}
    finding = report.finding('tone_jump')
    # value is the Oklab step (unit L) of the worst bend: an R spike of 0.05 on the grey ramp is ~0.1 L
    assert finding.value > 0.03 == finding.threshold and finding.value < 0.5 and finding.count >= 1
    assert float(dict(finding.details)['robust_ratio']) > 12 and dict(finding.details)['step_unit'] == 'Oklab L'
    assert 0 < float(dict(finding.details)['step_L_component']) <= finding.value + 1e-12
    assert tuple(finding.locations[0]) == (8, 5, 9)


def test_tone_jump_value_is_an_oklab_lightness_step_and_matches_the_warning_rule():
    # a pure lightness (grey) bump: the reported step equals the Oklab L of the second difference
    values = grid(17).copy()
    values[8, 8, 8] += 0.06                                                   # all three channels of one grey node
    report = lh.diagnose_lut(loaded(np.clip(values, 0, 1)))
    finding = report.finding('tone_jump')
    lab = srgb_to_oklab(np.clip(values, 0, 1))
    expected_l = abs(lab[8, 8, 8, 0] - lab[7, 7, 7, 0])
    assert finding.severity == 'warning' and finding.value > finding.threshold == lh.TONE_JUMP_ABS
    assert 0.5 * expected_l < finding.value < 4 * expected_l                 # same order as the L change of the node
    assert float(dict(finding.details)['step_L_component']) == pytest.approx(finding.value, rel=0.2)
    # value > threshold  <=>  warning, also for quiet LUTs (no unusual node: value 0)
    quiet = lh.diagnose_lut(loaded(smooth_values(17))).finding('tone_jump')
    assert quiet.severity == 'ok' and quiet.value <= quiet.threshold and quiet.count == 0
    tiny = grid(17).copy()
    tiny[8, 5, 9, 0] += 0.004                                                 # unusual for the LUT but invisible
    small = lh.diagnose_lut(loaded(tiny)).finding('tone_jump')
    assert small.severity == 'ok' and 0 < small.value <= small.threshold
    assert float(dict(small.details)['robust_ratio']) > 12


def test_defect_tone_jump_step_across_a_plane():
    values = grid(17).copy()
    values[8:, :, :, 1] = np.clip(values[8:, :, :, 1] + 0.1, 0, 1)
    report = lh.diagnose_lut(loaded(values))
    assert report.finding('tone_jump').severity == 'warning' and report.finding('tone_jump').count > 100


def test_defect_hue_fold_single_node_rotated():
    values = grid(17).copy()
    base = values[10, 4, 4].copy()
    values[10, 4, 4] = (base[1], base[2], base[0])                              # red-ish node becomes a different hue
    report = lh.diagnose_lut(loaded(values))
    finding = report.finding('hue_fold')
    assert finding.severity == 'warning' and finding.count >= 1 and finding.threshold == 0
    assert tuple(finding.locations[0]) in {(10, 4, 4), (9, 4, 4), (10, 3, 4), (10, 4, 3)}
    assert float(dict(finding.details)['max_rotation_degrees']) > 60
    assert report.finding('range').severity == 'ok' and report.finding('clipping').severity == 'ok'


def test_hue_rotation_that_follows_the_input_hue_is_not_a_fold():
    # a smooth 25 degree global hue rotation never exceeds 60 degrees of relative rotation across an edge
    lab = srgb_to_oklab(grid(33))
    angle = np.radians(25.0)
    a2 = lab[..., 1] * np.cos(angle) - lab[..., 2] * np.sin(angle)
    b2 = lab[..., 1] * np.sin(angle) + lab[..., 2] * np.cos(angle)
    rotated = np.clip(oklab_to_srgb(np.stack([lab[..., 0], a2, b2], -1)), 0, 1)
    assert lh.diagnose_lut(loaded(rotated)).finding('hue_fold').severity == 'ok'


def test_defect_banding_risk_steep_local_gain():
    def steep(t):
        tt = np.linspace(0, 1, 4001)
        width = 0.2
        window = np.where(np.abs(tt - 0.5) < width / 2, 0.5 * (1 + np.cos(2 * np.pi * (tt - 0.5) / width)), 0.0)
        slope = 0.5 + window / width
        slope /= np.trapezoid(slope, tt)
        curve = np.concatenate([[0], np.cumsum((slope[1:] + slope[:-1]) / 2 * np.diff(tt))])
        return np.interp(t, tt, curve)
    report = lh.diagnose_lut(loaded(separable(steep, 65)))
    assert non_ok(report) == {'banding_risk': 'warning'}
    finding = report.finding('banding_risk')
    assert finding.value > 4 == finding.threshold and finding.count > 0


def test_defect_flatten_collapsed_region():
    def shelf(t):
        tt = np.linspace(0, 1, 4001)
        slope = np.ones_like(tt)
        slope = np.where((tt > 0.42) & (tt < 0.58), 0.0, slope)
        ramp_up, ramp_down = (tt >= 0.32) & (tt <= 0.42), (tt >= 0.58) & (tt <= 0.68)
        slope = np.where(ramp_up, 0.5 * (1 + np.cos(np.pi * (tt - 0.32) / 0.1)), slope)
        slope = np.where(ramp_down, 0.5 * (1 - np.cos(np.pi * (tt - 0.58) / 0.1)), slope)
        curve = np.concatenate([[0], np.cumsum((slope[1:] + slope[:-1]) / 2 * np.diff(tt))])
        return np.interp(t, tt, curve / curve[-1])
    report = lh.diagnose_lut(loaded(separable(shelf, 33)))
    assert non_ok(report) == {'flatten': 'warning'}
    finding = report.finding('flatten')
    assert finding.value > 0.02 == finding.threshold and finding.count > 100


def test_defect_nan_is_an_error_with_location():
    values = grid(17).copy()
    values[3, 4, 5, 1] = np.nan
    report = lh.diagnose_lut(loaded(values))
    finding = report.finding('range')
    assert finding.severity == 'error' and finding.count == 1 and tuple(finding.locations[0]) == (3, 4, 5)
    assert report.overall == 'error'
    assert {c for c, v in non_ok(report).items()} == {'range'}                    # the NaN does not trip other checks
    assert report.kind.kind == 'unknown'


def test_defect_values_outside_unit_is_a_warning():
    values = grid(17).copy()
    values[16, 16, 16] = (1.2, 1.0, 1.0)
    values[0, 0, 0] = (-0.1, 0.0, 0.0)
    finding = lh.diagnose_lut(loaded(values)).finding('range')
    assert finding.severity == 'warning' and finding.count == 2 and finding.value == pytest.approx(2 / 17 ** 3)


def test_location_lists_are_capped_and_distinct():
    noisy = np.clip(grid(17) + np.random.default_rng(0).normal(0, 0.05, (17, 17, 17, 3)), 0, 1)
    report = lh.diagnose_lut(loaded(noisy))
    for finding in report.findings:
        assert len(finding.locations) <= 64
        assert len({tuple(row) for row in finding.locations}) == len(finding.locations)
        assert finding.locations.size == 0 or (finding.locations.min() >= 0 and finding.locations.max() < 17)


def test_hue_sample_flags_mark_flagged_nodes():
    values = grid(17).copy()
    values[8, 5, 9] += (0.05, 0.0, 0.0)
    report = lh.diagnose_lut(loaded(np.clip(values, 0, 1)))
    flags = report.hue_samples[:, 4].astype(int)
    assert (flags & lh.FLAG_BITS['tone_jump']).any()
    assert flags.max() < 128 and np.all(report.hue_samples[:, 5] >= 0)



# ---------------------------------------------------------------------------------------------
# input-kind estimate
# ---------------------------------------------------------------------------------------------
LOG_NAMES = tuple(lh.LOG_CURVES)


def srgb_oetf(linear):
    linear = np.clip(linear, 0, None)
    return np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * linear ** (1 / 2.4) - 0.055)


def log_to_display(curve, n=33, display=lh.rec709_oetf):
    """Decode the log code to scene linear, then encode for a display (white clips at 1)."""
    return separable(lambda t: np.minimum(display(np.maximum(lh.log_decode(curve, t), 0.0)), 1.0), n)


def display_to_log(curve, n=33):
    return separable(lambda t: np.clip(lh.log_encode(curve, lh.rec709_inverse_oetf(t)), 0, 1), n)


def test_kind_identity_including_8bit_quantised_identity():
    assert lh.diagnose_lut(loaded(grid(17))).kind.kind == 'identity'
    quantised = np.round(grid(33) * 255) / 255
    quantised[3, 4, 5] += 0.0015
    kind = lh.diagnose_lut(loaded(quantised)).kind
    assert kind.kind == 'identity' and kind.note_key == 'health.kind_note.identity' and kind.log_candidates == ()


def test_kind_display_creative_lut_gets_the_709_srgb_note():
    for values in (smooth_values(33), smooth_values(33, tint=0.02),
                   separable(lambda t: 0.08 + 0.85 * t, 33),                  # matte, lifted blacks
                   separable(lambda t: t ** 0.8, 17)):
        kind = lh.diagnose_lut(loaded(values)).kind
        assert kind.kind == 'display' and kind.note_key == 'health.kind_note.display_709_srgb'
        assert kind.log_candidates == () and 0.5 <= kind.confidence <= 1.0


def test_kind_strong_contrast_look_is_not_mistaken_for_log():
    def smoothstep(t):
        return t * t * (3 - 2 * t)
    kind = lh.diagnose_lut(loaded(separable(smoothstep, 33))).kind
    assert kind.kind in ('display', 'unknown')


@pytest.mark.parametrize('curve', LOG_NAMES)
def test_kind_log_to_display_for_each_published_curve(curve):
    kind = lh.diagnose_lut(loaded(log_to_display(curve))).kind
    assert kind.kind == 'log_to_display' and kind.note_key == 'health.kind_note.log_to_display'
    names = [name for name, _ in kind.log_candidates]
    scores = [score for _, score in kind.log_candidates]
    assert len(names) == 3 and curve in names                                      # always among the top three
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 1 for s in scores)
    assert 0.5 < kind.confidence <= 1.0


def test_kind_log_curve_identification_top1_summary():
    ranks = {}
    for curve in LOG_NAMES:
        kind = lh.diagnose_lut(loaded(log_to_display(curve))).kind
        ranks[curve] = [name for name, _ in kind.log_candidates].index(curve)
    top1 = [curve for curve, rank in ranks.items() if rank == 0]
    assert len(top1) >= 6, f'only {top1} are ranked first; ranks {ranks}'
    # F-Log2 and ARRI LogC3 share black and 18 % grey codes to four digits: they may only be mistaken for each other
    for curve, rank in ranks.items():
        if rank:
            assert curve in ('F-Log2', 'ARRI LogC3', 'S-Log3')


@pytest.mark.parametrize('curve', LOG_NAMES)
def test_kind_display_to_log_for_each_published_curve(curve):
    kind = lh.diagnose_lut(loaded(display_to_log(curve))).kind
    assert kind.kind == 'display_to_log' and kind.note_key == 'health.kind_note.display_to_log'
    assert curve in [name for name, _ in kind.log_candidates]


def test_kind_display_to_log_top1_summary():
    top1 = 0
    for curve in LOG_NAMES:
        kind = lh.diagnose_lut(loaded(display_to_log(curve))).kind
        top1 += kind.log_candidates[0][0] == curve
    assert top1 >= 6


def test_kind_log_estimate_survives_a_different_display_transform():
    """With sRGB instead of Rec.709 the nominal display model is off: the kind stays log_to_display and
    the true curve is still among the three candidates for most curves."""
    found = 0
    for curve in LOG_NAMES:
        kind = lh.diagnose_lut(loaded(log_to_display(curve, display=srgb_oetf))).kind
        assert kind.kind == 'log_to_display'
        found += curve in [name for name, _ in kind.log_candidates]
    assert found >= 5


def test_kind_unknown_for_a_decoding_lut_and_for_nonfinite_data():
    decode = separable(lambda t: np.where(t <= 0.04045, t / 12.92, ((t + 0.055) / 1.055) ** 2.4), 17)
    assert lh.diagnose_lut(loaded(decode)).kind.kind == 'unknown'
    values = grid(5).copy()
    values[1, 1, 1] = np.nan
    assert lh.diagnose_lut(loaded(values)).kind.kind == 'unknown'


def test_input_kind_estimate_validation():
    with pytest.raises(ValidationError):
        lh.InputKindEstimate('bogus', 0.5)
    with pytest.raises(ValidationError):
        lh.InputKindEstimate('display', 1.5)
    with pytest.raises(ValidationError):
        lh.InputKindEstimate('display', 0.5, (('Not a curve', 0.5),))
    assert lh.InputKindEstimate('display', 0.5).note_key == 'health.kind_note.display_709_srgb'


# ---------------------------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------------------------
def spike_lut(n=17):
    values = grid(n).copy()
    values[n // 2, 5, 9] += (0.05, 0.0, 0.0)
    return np.clip(values, 0, 1)


def chebyshev_distance_to(mask):
    from scipy import ndimage
    return ndimage.distance_transform_cdt(~mask, metric='chessboard')


def test_repair_defaults_are_a_bit_identical_copy():
    for values in (spike_lut(), smooth_values(17)):
        source = loaded(values)
        result = lh.repair_lut(source, lh.RepairSettings())
        assert result.lut.values.tobytes() == source.values.tobytes()
        assert result.lut is not source and result.change == (0.0, 0.0, 0.0)
        assert [f.severity for f in result.after.findings] == [f.severity for f in result.before.findings]
        assert json.loads(dict(result.after.provenance)['repair'])['settings']['smooth'] == 0.0
    assert lh.repair_lut(loaded(spike_lut())).lut.values.tobytes() == loaded(spike_lut()).values.tobytes()


def test_repair_default_copy_keeps_nonfinite_values_bit_for_bit():
    values = grid(5).copy()
    values[2, 2, 2, 0] = np.nan
    assert lh.repair_lut(loaded(values)).lut.values.tobytes() == loaded(values).values.tobytes()


def test_repair_smoothing_reduces_the_tone_jump_metric():
    source = loaded(spike_lut())
    result = lh.repair_lut(source, lh.RepairSettings(smooth=0.3))
    before, after = result.before.finding('tone_jump'), result.after.finding('tone_jump')
    assert before.severity == 'warning' and before.value > 0.03
    assert float(dict(before.details)['robust_ratio']) > 12
    assert after.severity == 'ok' and after.value < before.value
    assert float(dict(after.details)['robust_ratio']) < float(dict(before.details)['robust_ratio'])
    assert result.after.overall == 'ok' and result.before.overall == 'warning'
    assert result.lut.size == 17 and result.lut.values.min() >= 0 and result.lut.values.max() <= 1
    assert source.values.tobytes() == loaded(spike_lut()).values.tobytes()           # the input is untouched


def test_repair_smoothing_acts_on_the_residual_so_identity_does_not_shrink():
    result = lh.repair_lut(loaded(grid(17)), lh.RepairSettings(smooth=1.0, iterations=64))
    assert np.abs(result.lut.values - grid(17)).max() < 1e-12
    assert result.change[2] < 1e-9
    # whereas a uniform shift of the whole cube is smoothed only as a residual (stays a uniform shift)
    shifted = np.clip(grid(17) * 0.9 + 0.02, 0, 1)
    again = lh.repair_lut(loaded(shifted), lh.RepairSettings(smooth=1.0, iterations=64)).lut.values
    assert np.abs(again[1:-1, 1:-1, 1:-1] - shifted[1:-1, 1:-1, 1:-1]).max() < 1e-9


def test_repair_cube_faces_keep_their_position():
    n = 17
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 0.02, (n, n, n, 3))
    for channel in range(3):                                           # no noise along the face normal on a face
        for face in (0, n - 1):
            index = [slice(None)] * 3
            index[channel] = face
            noise[tuple(index) + (channel,)] = 0.0
    values = np.clip(grid(n) + noise, 0, 1)
    for settings in (lh.RepairSettings(smooth=1.0, iterations=64), lh.RepairSettings(smooth=0.5, fix_jumps=True, round_clip=True)):
        out = lh.repair_lut(loaded(values), settings).lut.values
        for channel in range(3):
            low = np.take(out, 0, axis=channel)[..., channel]
            high = np.take(out, n - 1, axis=channel)[..., channel]
            assert np.all(low == np.take(values, 0, axis=channel)[..., channel]) and np.all(low == 0)
            assert np.all(high == np.take(values, n - 1, axis=channel)[..., channel]) and np.all(high == 1)
        assert not np.array_equal(out[1:-1, 1:-1, 1:-1], values[1:-1, 1:-1, 1:-1])   # the interior did change
    corners = lh.repair_lut(loaded(values), lh.RepairSettings(smooth=1.0)).lut.values
    for corner in ((0, 0, 0), (0, 0, n - 1), (n - 1, n - 1, n - 1), (0, n - 1, 0)):
        assert np.array_equal(corners[corner], values[corner])


def tinted_values(n=33):
    return smooth_values(n, tint=0.015)


def test_repair_lock_gray_makes_the_grey_axis_neutral_and_keeps_lightness():
    source = loaded(tinted_values())
    idx = np.arange(33)
    before_lab = srgb_to_oklab(source.values[idx, idx, idx])
    assert np.hypot(before_lab[:, 1], before_lab[:, 2]).max() > 0.01
    result = lh.repair_lut(source, lh.RepairSettings(lock_gray=True))
    lab = srgb_to_oklab(result.lut.values[idx, idx, idx])
    assert np.hypot(lab[:, 1], lab[:, 2]).max() < 1e-3
    assert np.abs(lab[:, 0] - before_lab[:, 0]).max() < 1e-3
    assert result.before.finding('gray_tint').severity == 'info' and result.after.finding('gray_tint').severity == 'ok'
    grey = result.after.gray_axis
    assert np.abs(grey[:, 1] - grey[:, 2]).max() < 1e-6 and np.abs(grey[:, 2] - grey[:, 3]).max() < 1e-6


def test_repair_lock_gray_correction_fades_with_input_chroma():
    source = loaded(tinted_values())
    result = lh.repair_lut(source, lh.RepairSettings(lock_gray=True))
    change = np.abs(result.lut.values - source.values).max(axis=-1)
    chroma = np.hypot(*srgb_to_oklab(grid(33))[..., 1:].transpose(3, 0, 1, 2))
    assert change[chroma > 0.15].max() < 0.01 and change[chroma > lh.LOCK_GRAY_FADE].max() < 1e-12
    near, middle = change[chroma < 0.03].mean(), change[(chroma > 0.06) & (chroma < 0.09)].mean()
    assert near > middle > 0.0


def test_repair_lock_gray_alone_changes_only_near_the_axis():
    source = loaded(tinted_values())
    result = lh.repair_lut(source, lh.RepairSettings(lock_gray=True))
    changed = np.abs(result.lut.values - source.values).max(axis=-1) > 1e-9
    chroma = np.hypot(*srgb_to_oklab(grid(33))[..., 1:].transpose(3, 0, 1, 2))
    assert changed.any() and chroma[changed].max() < lh.LOCK_GRAY_FADE


def test_repair_round_clip_is_local_to_the_clipped_border():
    from scipy import ndimage
    values = separable(lambda t: np.minimum(t * 1.3, 1.0), 33)
    source = loaded(values)
    plateau = ((values >= 1 - 5e-4) & (grid(33) < 1 - 5e-4)).any(axis=-1)
    border = ndimage.binary_dilation(plateau) & ndimage.binary_dilation(~plateau)
    distance = chebyshev_distance_to(border)
    result = lh.repair_lut(source, lh.RepairSettings(round_clip=True))
    diff = np.abs(result.lut.values - source.values).max(axis=-1)
    assert diff[distance >= 4].max() < 1e-12                           # nothing changes away from the plateau border
    assert diff[border].max() > 0.005                                  # a soft shoulder appears at the border
    assert result.after.finding('clipping').value < result.before.finding('clipping').value
    assert result.lut.values.max() <= 1.0


def test_repair_fix_jumps_is_local_to_the_jump():
    source = loaded(spike_lut())
    result = lh.repair_lut(source, lh.RepairSettings(fix_jumps=True))
    diff = np.abs(result.lut.values - source.values).max(axis=-1)
    node = np.zeros((17, 17, 17), bool)
    node[8, 5, 9] = True
    distance = chebyshev_distance_to(node)
    assert diff[distance >= 5].max() < 1e-12
    assert diff[8, 5, 9] > 0.02
    assert result.after.finding('tone_jump').severity == 'ok' and result.after.finding('hue_fold').severity == 'ok'


def test_repair_fix_jumps_and_round_clip_leave_a_healthy_lut_alone():
    source = loaded(smooth_values(17))
    result = lh.repair_lut(source, lh.RepairSettings(fix_jumps=True, round_clip=True))
    assert np.array_equal(result.lut.values, source.values) and result.change == (0.0, 0.0, 0.0)


def test_repair_change_metrics_are_plausible_and_match_an_independent_computation():
    rng = np.random.default_rng(3)
    values = np.clip(smooth_values(17) + rng.normal(0, 0.008, (17, 17, 17, 3)), 0, 1)
    source = loaded(values)
    result = lh.repair_lut(source, lh.RepairSettings(smooth=0.5))
    mean, p99, maximum = result.change
    assert 0 < mean <= p99 <= maximum < 6
    old, new = lh._srgb_to_lab(source.values.reshape(-1, 3)), lh._srgb_to_lab(result.lut.values.reshape(-1, 3))
    reference = lh.delta_e2000(old, new)
    assert mean == pytest.approx(reference.mean()) and maximum == pytest.approx(reference.max())
    assert p99 == pytest.approx(np.percentile(reference, 99))
    stronger = lh.repair_lut(source, lh.RepairSettings(smooth=1.0, iterations=32)).change
    assert stronger[0] > mean


def test_repair_provenance_records_settings_and_change():
    settings = lh.RepairSettings(smooth=0.25, fix_jumps=True, lock_gray=True, iterations=5)
    result = lh.repair_lut(loaded(spike_lut()), settings)
    record = json.loads(dict(result.after.provenance)['repair'])
    assert record['settings'] == {'smooth': 0.25, 'fix_jumps': True, 'round_clip': False, 'lock_gray': True, 'iterations': 5}
    assert record['change_de2000']['max'] == pytest.approx(result.change[2])
    assert 'repair' not in dict(result.before.provenance) and dict(result.after.provenance)['schema'] == lh.SCHEMA
    assert 'repaired' in result.lut.warnings


def test_repair_replaces_nonfinite_and_out_of_range_nodes():
    values = grid(9).copy()
    values[4, 4, 4] = (np.nan, 0.5, 0.5)
    values[2, 2, 2] = (1.3, -0.2, 0.5)
    result = lh.repair_lut(loaded(values), lh.RepairSettings(smooth=0.1))
    assert np.all(np.isfinite(result.lut.values)) and result.lut.values.min() >= 0 and result.lut.values.max() <= 1
    assert result.before.finding('range').severity == 'error' and result.after.finding('range').severity == 'ok'
    assert 'values_nonfinite' not in result.lut.warnings


def test_repair_accepts_engine_lut_and_none_settings():
    result = lh.repair_lut(lut3d(spike_lut()), lh.RepairSettings(smooth=0.2))
    assert result.lut.size == 17 and result.lut.source_format == 'cube'
    assert lh.repair_lut(lut3d(spike_lut()), None).change == (0.0, 0.0, 0.0)


@pytest.mark.parametrize('kwargs', [
    {'smooth': -0.1}, {'smooth': 1.5}, {'smooth': float('nan')}, {'smooth': True}, {'smooth': '0.3'},
    {'iterations': 0}, {'iterations': 65}, {'iterations': 2.5}, {'iterations': True},
    {'fix_jumps': 1}, {'round_clip': 'yes'}, {'lock_gray': None},
])
def test_repair_settings_validation(kwargs):
    with pytest.raises(ValidationError) as caught:
        lh.RepairSettings(**kwargs)
    assert caught.value.code == 'invalid_repair_settings'


def test_repair_rejects_wrong_argument_types():
    with pytest.raises(ValidationError) as caught:
        lh.repair_lut(loaded(grid(5)), {'smooth': 0.5})
    assert caught.value.code == 'invalid_repair_settings'
    with pytest.raises(ValidationError):
        lh.repair_lut('not a lut', lh.RepairSettings())
    with pytest.raises(ValidationError):
        lh.diagnose_lut(np.zeros((5, 5, 5, 3)))


# ---------------------------------------------------------------------------------------------
# resampling to / from the engine LUT
# ---------------------------------------------------------------------------------------------
def test_to_lut3d_same_size_is_exact_and_clipped():
    values = smooth_values(33)
    out = lh.to_lut3d(loaded(values))
    assert out.values.shape == (33, 33, 33, 3) and out.values.dtype == np.float32
    assert np.array_equal(out.values, values.astype(np.float32))
    wide = values.copy()
    wide[0, 0, 0] = (-0.3, 0.5, 1.4)
    assert lh.to_lut3d(loaded(wide)).values[0, 0, 0].tolist() == [0.0, 0.5, 1.0]
    assert out.input_space == SRGB and out.output_space == SRGB and out.strength == 1.0


def test_to_lut3d_17_to_65_matches_tetrahedral_sampling_and_keeps_shared_nodes():
    source = lut3d(smooth_values(17))
    out = lh.to_lut3d(lh.from_lut3d(source), 65)
    assert np.array_equal(out.values, resample_lut(source, 65))
    assert np.array_equal(out.values[::4, ::4, ::4], source.values)                 # shared nodes are exact
    back = lh.to_lut3d(lh.from_lut3d(out), 17)
    assert np.array_equal(back.values, source.values)


def test_to_lut3d_nearest_supported_size_and_overrides():
    for n, expected in ((2, 17), (16, 17), (24, 17), (25, 33), (36, 33), (49, 65), (64, 65), (100, 65), (129, 65)):
        values = np.zeros((n, n, n, 3)) if n > 100 else grid(n)
        assert lh.to_lut3d(loaded(values)).values.shape[0] == expected
    assert lh.to_lut3d(loaded(grid(36)), 65).values.shape[0] == 65
    with pytest.raises(ValidationError) as caught:
        lh.to_lut3d(loaded(grid(17)), 20)
    assert caught.value.code == 'invalid_lut_size'


def test_to_lut3d_from_odd_size_and_identity_stays_identity():
    out = lh.to_lut3d(loaded(grid(36)))
    assert out.values.shape[0] == 33 and np.abs(out.values - grid(33)).max() < 1e-6
    out = lh.to_lut3d(loaded(grid(5)), 17)
    assert np.abs(out.values - grid(17)).max() < 1e-6


def test_to_lut3d_replaces_nonfinite_nodes_with_the_identity_and_records_provenance():
    values = grid(17).copy()
    values[3, 3, 3] = np.nan
    out = lh.to_lut3d(loaded(values, name='my look'), title='renamed')
    assert np.allclose(out.values[3, 3, 3], grid(17)[3, 3, 3]) and out.title == 'renamed'
    provenance = dict(out.provenance)
    assert provenance['imported_from'] == 'my look.cube' and provenance['source_size'] == '17'
    assert provenance['resampling'] == 'none'
    assert dict(lh.to_lut3d(loaded(grid(5)), 17).provenance)['resampling'] == 'tetrahedral from 5^3 to 17^3'


def test_to_lut3d_result_exports_and_round_trips(tmp_path):
    out = lh.to_lut3d(loaded(smooth_values(36)))
    export_cube(out, tmp_path / 'resampled.cube')
    again = lh.load_lut_file(tmp_path / 'resampled.cube')
    assert again.size == 33 and np.array_equal(again.values.astype(np.float32), out.values)


def test_from_lut3d_wraps_without_changing_values():
    source = lut3d(smooth_values(17), title='in-app')
    wrapped = lh.from_lut3d(source)
    assert wrapped.size == 17 and wrapped.title == 'in-app' and wrapped.source_format == 'cube'
    assert np.array_equal(wrapped.values, source.values.astype(np.float64)) and not wrapped.values.flags.writeable
    with pytest.raises(ValidationError):
        lh.from_lut3d(np.zeros((17, 17, 17, 3)))


# ---------------------------------------------------------------------------------------------
# determinism, progress, cancellation, headless
# ---------------------------------------------------------------------------------------------
def test_diagnosis_and_repair_are_deterministic():
    source = loaded(np.clip(smooth_values(17) + np.random.default_rng(5).normal(0, 0.01, (17, 17, 17, 3)), 0, 1))
    assert signature(lh.diagnose_lut(source)) == signature(lh.diagnose_lut(source))
    settings = lh.RepairSettings(smooth=0.4, fix_jumps=True, round_clip=True, lock_gray=True, iterations=6)
    first, second = lh.repair_lut(source, settings), lh.repair_lut(source, settings)
    assert first.lut.values.tobytes() == second.lut.values.tobytes() and first.change == second.change
    assert signature(first.after) == signature(second.after)


def test_progress_is_monotonic_within_zero_and_one():
    seen = []
    context = JobContext(progress=lambda p: seen.append((p.stage, p.fraction)))
    lh.diagnose_lut(loaded(smooth_values(17)), context=context)
    fractions = [f for _, f in seen]
    assert fractions[0] == 0 and fractions[-1] == 1 and fractions == sorted(fractions)
    assert {stage for stage, _ in seen} == {'health.diagnose'}
    seen.clear()
    lh.repair_lut(loaded(spike_lut()), lh.RepairSettings(smooth=0.3, iterations=4), context=context)
    fractions = [f for _, f in seen]
    assert fractions[0] == 0 and fractions[-1] == 1 and fractions == sorted(fractions)
    assert all(0 <= f <= 1 for f in fractions)


def test_cancellation_stops_every_long_operation(tmp_path):
    source = loaded(smooth_values(17))
    path = write_cube(tmp_path / 'cancel.cube', smooth_values(17))
    for call in (lambda c: lh.diagnose_lut(source, context=c),
                 lambda c: lh.repair_lut(source, lh.RepairSettings(smooth=0.3), context=c),
                 lambda c: lh.load_lut_file(path, context=c),
                 lambda c: lh.to_lut3d(loaded(grid(36)), 65, context=c)):
        for calls in (0, 1, 3):
            with pytest.raises(CancelledError):
                call(JobContext(cancel=CancelAfter(calls)))
    assert lh.diagnose_lut(source, context=JobContext(cancel=CancelAfter(10 ** 6))).overall == 'ok'


def test_module_import_is_headless():
    code = ("import sys; import camera_profile_studio.lut_health as m; "
            "assert not any(name.startswith(('PySide', 'PyQt')) for name in sys.modules), 'Qt was imported'; "
            "print(len(m.LOG_CURVES))")
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '8'


# ---------------------------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------------------------
def test_health_finding_and_report_validation():
    empty = np.zeros((0, 3), np.int64)
    finding = lh.HealthFinding('range', 'ok', 0.0, 0.0, 0, empty)
    assert finding.locations.shape == (0, 3) and finding.details == ()
    for kwargs in ({'code': 'bogus'}, {'severity': 'fatal'}, {'value': float('nan')}, {'count': -1},
                   {'locations': np.zeros((65, 3), np.int64)}, {'locations': np.full((1, 3), -1)},
                   {'details': (('a', 1),)}):
        arguments = dict(code='range', severity='ok', value=0.0, threshold=0.0, count=0, locations=empty)
        arguments.update(kwargs)
        with pytest.raises(ValidationError):
            lh.HealthFinding(**arguments)
    report = lh.diagnose_lut(loaded(grid(5)))
    with pytest.raises(ValidationError):
        dataclasses.replace(report, overall='error')                                  # must be the worst severity
    with pytest.raises(ValidationError):
        dataclasses.replace(report, findings=report.findings[:-1])
    with pytest.raises(ValidationError):
        dataclasses.replace(report, gray_axis=np.zeros((257, 6)))
    with pytest.raises(ValidationError):
        dataclasses.replace(report, provenance=(('a', 'b'), ('a', 'c')))


def test_report_serialisation_helpers_are_json_safe():
    report = lh.diagnose_lut(loaded(spike_lut()))
    summary = lh.health_summary(report)
    assert summary['overall'] == 'warning' and [f['code'] for f in summary['findings']] == list(lh.FINDING_CODES)
    json.dumps(summary, allow_nan=False)
    brief = lh.report_to_dict(report)
    full = lh.report_to_dict(report, include_arrays=True)
    json.dumps(full, allow_nan=False)
    assert all('locations' not in f for f in brief['findings']) and 'gray_axis' not in brief
    assert all('locations' in f for f in full['findings']) and len(full['gray_axis']) == 257
    assert full['kind']['kind'] == report.kind.kind and full['flag_bits']['tone_jump'] == 8


# ---------------------------------------------------------------------------------------------
# export_lut(extra_metadata=...)
# ---------------------------------------------------------------------------------------------
def test_extra_metadata_is_merged_into_cube_and_png_sidecars(tmp_path):
    lut = lut3d(smooth_values(17))
    health = lh.health_summary(lh.diagnose_lut(lut))
    export_lut(lut, tmp_path / 'plain.cube')
    export_lut(lut, tmp_path / 'extra.cube', extra_metadata={'health': health, 'note': 'x'})
    plain = json.loads((tmp_path / 'plain.json').read_text())
    extra = json.loads((tmp_path / 'extra.json').read_text())
    assert extra['health'] == health and extra['note'] == 'x'
    assert {k: v for k, v in extra.items() if k not in ('health', 'note')}.keys() == plain.keys()
    assert extra['cube_sha256'] == plain['cube_sha256']
    for fmt in ('hald_png', 'strip_png', 'strip_png_flipped'):
        export_lut(lut, tmp_path / f'{fmt}.png', format=fmt, extra_metadata={'health': health})
        assert json.loads((tmp_path / f'{fmt}.json').read_text())['health'] == health
    export_cube(lut, tmp_path / 'direct.cube', extra_metadata={'health': health})
    assert json.loads((tmp_path / 'direct.json').read_text())['health'] == health
    read_cube(tmp_path / 'extra.cube', input_space=SRGB, output_space=SRGB)       # still a valid companion


def test_extra_metadata_none_or_empty_changes_nothing(tmp_path):
    lut = lut3d(smooth_values(17))
    export_lut(lut, tmp_path / 'a.cube')
    export_lut(lut, tmp_path / 'b.cube', extra_metadata=None)
    export_lut(lut, tmp_path / 'c.cube', extra_metadata={})
    first = json.loads((tmp_path / 'a.json').read_text())
    assert json.loads((tmp_path / 'b.json').read_text()) == first == json.loads((tmp_path / 'c.json').read_text())
    assert (tmp_path / 'a.cube').read_bytes() == (tmp_path / 'b.cube').read_bytes()


@pytest.mark.parametrize('extra', [
    {'blob': 'x' * 70000},                          # over 64 KiB
    {'bad': float('nan')}, {'bad': float('inf')}, {'bad': object()}, {'bad': {1, 2}},
    {'size': 99}, {'provenance': {}}, {'png_sha256': 'x'},   # would replace the sidecar's own keys
    {1: 'integer key'}, {'': 'empty key'}, ['not', 'a', 'dict'], 'text',
])
def test_extra_metadata_rejections_leave_no_files(tmp_path, extra):
    lut = lut3d(smooth_values(17))
    for fmt, name in (('cube', 'x.cube'), ('hald_png', 'x.png')):
        with pytest.raises(ValidationError) as caught:
            export_lut(lut, tmp_path / name, format=fmt, extra_metadata=extra)
        assert caught.value.code == 'invalid_extra_metadata'
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_extra_metadata_just_under_the_limit_is_accepted(tmp_path):
    lut = lut3d(smooth_values(17))
    export_lut(lut, tmp_path / 'ok.cube', extra_metadata={'blob': 'x' * 60000})
    assert len(json.loads((tmp_path / 'ok.json').read_text())['blob']) == 60000

