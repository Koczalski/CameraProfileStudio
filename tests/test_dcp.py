"""Independent binary fixtures and numerical references; not photographic validation."""
from __future__ import annotations

import colorsys
from dataclasses import replace
from pathlib import Path
import struct

import colour
import numpy as np
import pytest

from camera_profile_studio.dcp import (read_dcp, render_dcp, create_dcp_transform,
                                      _apply_table, _apply_tone, _table, _tone, _weight)
from camera_profile_studio.errors import (CancelledError, InvalidProfileError,
                                         UnsupportedError, ValidationError)
from camera_profile_studio.types import DcpSettings, JobContext

D50 = np.array([.3457 / .3585, 1., (1 - .3457 - .3585) / .3585])
D65 = np.array([.3127 / .3290, 1., (1 - .3127 - .3290) / .3290])
CM = np.array([[1.2, .1, 0], [.1, .9, .1], [0, .2, .7]])


def write_fixture(path, entries=None, *, endian='<', magic=0x4352):
    """Write TIFF entries without calling any production parser/serializer."""
    if entries is None:
        entries = [(50721, 10, np.eye(3).ravel()), (50778, 3, [23])]
    start = 8 + 2 + 12 * len(entries) + 4
    directory = bytearray(struct.pack(endian + 'H', len(entries)))
    payload = bytearray()
    for tag, kind, values in entries:
        if kind == 2:
            raw = values.encode('utf-8') + b'\0'
            count = len(raw)
        elif kind in (5, 10):
            count = len(values)
            raw = b''.join(struct.pack(endian + ('ii' if kind == 10 else 'II'), round(float(v) * 1000000), 1000000) for v in values)
        else:
            count = len(values)
            raw = struct.pack(endian + {1:'B', 3:'H', 4:'I', 11:'f', 12:'d'}[kind] * count, *values)
        field = raw.ljust(4, b'\0') if len(raw) <= 4 else struct.pack(endian + 'I', start + len(payload))
        directory.extend(struct.pack(endian + 'HHI', tag, kind, count) + field)
        if len(raw) > 4:
            payload.extend(raw)
    directory.extend(b'\0' * 4)
    header = (b'II' if endian == '<' else b'MM') + struct.pack(endian + 'HI', magic, 8)
    path.write_bytes(header + directory + payload)
    return path


def profile(tmp_path, entries=None):
    return read_dcp(write_fixture(tmp_path / 'fixture.dcp', entries))


def reference_srgb(xyz_d50):
    # Independently installed Colour's Bradford and standardized sRGB matrix.
    xyz_d65 = colour.adaptation.chromatic_adaptation_VonKries(xyz_d50, D50, D65, transform='Bradford')
    linear = colour.XYZ_to_RGB(xyz_d65, 'sRGB', illuminant=np.array([.3127, .3290]), chromatic_adaptation_transform=None)
    return colour.cctf_encoding(np.clip(linear, 0, 1), function='sRGB')


@pytest.mark.parametrize('endian,magic', [('<', 42), ('<', 0x4352), ('>', 42), ('>', 0x4352)])
def test_tiff_headers_and_rational_directions(tmp_path, endian, magic):
    p = read_dcp(write_fixture(tmp_path / 'p.dcp', [(50721, 10, CM.ravel()), (50936, 2, 'Independent')], endian=endian, magic=magic))
    assert p.info.description == 'Independent'
    np.testing.assert_allclose(dict(p.tags)[50721], CM.ravel())
    assert len(p.info.sha256) == 64
    assert 'matrix-only' in ' '.join(p.info.warnings).lower()


def test_color_matrix_nonidentity_direction_bradford_and_neutral_normalization(tmp_path):
    p = profile(tmp_path, [(50721, 10, CM.ravel())])
    neutral = CM @ D65
    xyz = np.array([[.15, .2, .1], [.2, .15, .12]])
    camera = xyz @ CM.T / max(neutral)
    settings = DcpSettings(camera_neutral=tuple(neutral))
    result = render_dcp(camera.astype(np.float32), p, settings=settings)
    d50 = colour.adaptation.chromatic_adaptation_VonKries(xyz, D65, D50, transform='Bradford')
    np.testing.assert_allclose(result, reference_srgb(d50), atol=1e-4)
    assert result.dtype == np.float32


def test_forward_matrix_is_not_inverted_and_wb_is_applied_once(tmp_path):
    fm = np.diag(D50)
    p = profile(tmp_path, [(50721, 10, CM.ravel()), (50964, 10, fm.ravel())])
    rgb = np.array([[.1, .3, .15]], np.float32)
    actual = render_dcp(rgb, p, settings=DcpSettings(camera_neutral=(.5, 1., .75)))
    expected_xyz = np.array([[.2 * D50[0], .3, .2 * D50[2]]])
    np.testing.assert_allclose(actual, reference_srgb(expected_xyz), atol=1e-4)
    # Neutral scaling is normalized at the first saturated camera channel.
    white = render_dcp(np.array([[.5, 1., .75]], np.float32), p, settings=DcpSettings(camera_neutral=(5., 10., 7.5)))
    np.testing.assert_allclose(white, 1., atol=2e-6)


def test_dual_unequal_matrices_reciprocal_temperature(tmp_path):
    cm2 = np.array([[.9, .2, .1], [.2, 1.1, .1], [.1, .1, .8]])
    p = profile(tmp_path, [(50721, 10, CM.ravel()), (50722, 10, cm2.ravel()), (50778, 3, [17]), (50779, 3, [21])])
    midpoint = 2 / (1 / 2856 + 1 / 6504)
    cm = (CM + cm2) / 2
    neutral = cm @ D65
    xyz = np.array([[.15, .2, .1]])
    camera = xyz @ cm.T / max(neutral)
    result = render_dcp(camera.astype(np.float32), p, settings=DcpSettings(camera_neutral=tuple(neutral), illuminant_kelvin=midpoint))
    expected = colour.adaptation.chromatic_adaptation_VonKries(xyz, D65, D50, transform='Bradford')
    np.testing.assert_allclose(result, reference_srgb(expected), atol=1e-4)
    assert _weight(dict(p.tags), 1000., dual=True) == 1
    assert _weight(dict(p.tags), 20000., dual=True) == 0
    with pytest.raises(ValidationError, match='explicit illuminant'):
        render_dcp(camera, p, settings=DcpSettings(camera_neutral=tuple(neutral)))


def test_dual_forward_matrix_interpolates_before_use(tmp_path):
    fm1 = np.diag(D50)
    fm2 = fm1 @ np.array([[.8, .2, 0], [0, .8, .2], [.2, 0, .8]])
    p = profile(tmp_path, [(50721, 10, CM.ravel()), (50722, 10, CM.ravel()),
                           (50964, 10, fm1.ravel()), (50965, 10, fm2.ravel()),
                           (50778, 3, [17]), (50779, 3, [21])])
    kelvin = 1 / (.25 / 2856 + .75 / 6504)
    rgb = np.array([[.1, .3, .15]], np.float32)
    expected_xyz = np.array([[.2, .3, .2]]) @ (.25 * fm1 + .75 * fm2).T
    actual = render_dcp(rgb, p, settings=DcpSettings(camera_neutral=(.5, 1, .75), illuminant_kelvin=kelvin))
    np.testing.assert_allclose(actual, reference_srgb(expected_xyz), atol=1e-4)


def test_interpolated_matrix_singular(tmp_path):
    p = profile(tmp_path, [(50721, 10, np.eye(3).ravel()), (50722, 10, -np.eye(3).ravel()), (50778, 3, [17]), (50779, 3, [21])])
    with pytest.raises(InvalidProfileError, match='Interpolated ColorMatrix'):
        create_dcp_transform(p, settings=DcpSettings(camera_neutral=(1, 1, 1), illuminant_kelvin=2 / (1 / 2856 + 1 / 6504)))


def test_table_storage_trilinear_and_hue_shift_units():
    # At H=.125, S=.25, V=.375: indices H=.5, S=.5, V=.75.
    table = np.empty((3, 4, 3, 3))
    for v in range(3):
        for h in range(4):
            for s in range(3):
                table[v, h, s] = (10 * h + 20 * v + 4 * s, 1 - .1 * s, 1 - .1 * s)
    tags = {50937: (4, 3, 3), 50938: tuple(table.ravel())}
    loaded = _table(tags, 50937, 50938)
    rgb = np.array([colorsys.hsv_to_rgb(.125, .25, .375)])
    expected = colorsys.hsv_to_rgb(.125 + 22 / 360, .25 * .95, .375 * .95)
    np.testing.assert_allclose(_apply_table(rgb, loaded, 0), [expected], atol=1e-12)


def test_hue_wrap_last_to_first_and_one_hue_division():
    table = np.zeros((1, 4, 2, 3))
    table[..., 1:] = 1
    table[:, :, :, 0] = np.array([0, 10, 20, 30])[None, :, None]
    rgb = np.array([colorsys.hsv_to_rgb(.875, .8, .6)])
    # Last hue bin to first: 50% of 30 degrees and 0 degrees, not clamped 30.
    expected = colorsys.hsv_to_rgb((.875 + 15 / 360) % 1, .8, .6)
    np.testing.assert_allclose(_apply_table(rgb, table, 0), [expected], atol=1e-12)
    table = np.array([[[[60, 1, 1], [60, 1, 1]]]], dtype=float)
    np.testing.assert_allclose(_apply_table(np.array([[.5, 0, 0]]), table, 0), [[.5, .5, 0]], atol=1e-12)


@pytest.mark.parametrize('encoding', [0, 1])
def test_value_encoding_addresses_and_scales_in_encoded_domain(encoding):
    table = np.zeros((2, 1, 2, 3))
    table[..., 1:] = 1
    table[0, 0, 1, 2] = .4
    table[1, 0, 1, 2] = .8
    rgb = np.array([[.25, 0, 0]])
    coordinate = .25 if encoding == 0 else 1.055 * .25 ** (1 / 2.4) - .055
    scale = .4 + .4 * coordinate
    product = coordinate * scale
    expected = product if encoding == 0 else ((product + .055) / 1.055) ** 2.4
    np.testing.assert_allclose(_apply_table(rgb, table, encoding), [[expected, 0, 0]], atol=1e-12)


def test_encoded_value_single_slice_compatibility_is_not_guessed():
    table = np.array([[[[0, 1, 1], [0, 1, .5]]]], dtype=float)
    with pytest.raises(UnsupportedError, match='single value slice'):
        _apply_table(np.array([[.25, 0, 0]]), table, 1)


@pytest.mark.parametrize('dims_tag,data_tag', [(50937, 50938), (50937, 50939), (50981, 50982)])
def test_legacy_skip_sat_zero_is_expanded_like_sdk(dims_tag, data_tag):
    tags = {dims_tag: (2, 2, 1), data_tag: (10., .8, .7, 20., .9, .6)}
    table = _table(tags, dims_tag, data_tag)
    np.testing.assert_allclose(table[0, 0], [[10, .8, 1], [10, .8, .7]])
    np.testing.assert_allclose(table[0, 1], [[20, .9, 1], [20, .9, .6]])


def test_tone_is_hue_preserving_and_monotone():
    curve = _tone({50940: (0., 0., .25, .125, .5, .4, .75, .8, 1., 1.)})
    rgb = np.array([[.75, .5, .25], [.25, .75, .5], [.5, .25, .75], [.25, .25, .25]])
    # At exact knots min=.125, max=.8 and middle is halfway, NOT curve(.5)=.4.
    expected = np.array([[.8, .4625, .125], [.125, .8, .4625], [.4625, .125, .8], [.125, .125, .125]])
    np.testing.assert_allclose(_apply_tone(rgb, curve), expected, atol=1e-12)
    ramp = curve(np.linspace(0, 1, 10001))
    assert np.all(np.diff(ramp) >= 0)
    assert ramp.min() == 0 and ramp.max() == 1


@pytest.mark.parametrize('entries,match', [
    ([(50721, 10, np.zeros(9))], 'singular'),
    ([(50721, 11, np.eye(3).ravel())], 'type/count'),
    ([(50721, 10, np.eye(3).ravel()), (50721, 10, np.eye(3).ravel())], 'Duplicate'),
    ([(50964, 10, np.eye(3).ravel())], 'D50'),
    ([(50937, 4, [2, 2, 1]), (50938, 11, [0, 1, 1])], 'count'),
    ([(50937, 4, [0, 2, 1]), (50938, 11, [0, 1, 1])], 'dimensions'),
    ([(50937, 4, [1, 2, 1]), (50938, 11, [0, 1, .5, 0, 1, 1])], 'Zero-saturation'),
    ([(50940, 11, [0, 0, .5, .8, .7, .6, 1, 1])], 'monotone'),
    ([(50940, 11, [0, 0, float('nan'), 1])], 'NaN/Inf'),
])
def test_invalid_numeric_structures(tmp_path, entries, match):
    with pytest.raises(InvalidProfileError, match=match):
        profile(tmp_path, entries)


@pytest.mark.parametrize('mutation', ['header', 'ifd_offset', 'ifd_truncated', 'payload_truncated', 'payload_offset', 'zero_denominator', 'count_overflow', 'bad_type', 'payload_in_ifd'])
def test_corrupt_binary_is_bounded_and_rejected(tmp_path, mutation):
    path = write_fixture(tmp_path / 'bad.dcp')
    data = bytearray(path.read_bytes())
    if mutation == 'header': data[2:4] = b'NO'
    elif mutation == 'ifd_offset': struct.pack_into('<I', data, 4, 0xfffffff0)
    elif mutation == 'ifd_truncated': data = data[:17]
    elif mutation == 'payload_truncated': data = data[:-1]
    elif mutation == 'payload_offset': struct.pack_into('<I', data, 18, 0xfffffff0)
    elif mutation == 'zero_denominator': struct.pack_into('<i', data, 42, 0)
    elif mutation == 'count_overflow': struct.pack_into('<I', data, 14, 0xffffffff)
    elif mutation == 'bad_type': struct.pack_into('<H', data, 12, 99)
    elif mutation == 'payload_in_ifd': struct.pack_into('<I', data, 18, 12)
    path.write_bytes(data)
    with pytest.raises(InvalidProfileError):
        read_dcp(path)


def test_multiple_ifds_rejected(tmp_path):
    path = write_fixture(tmp_path / 'multi.dcp')
    data = bytearray(path.read_bytes())
    struct.pack_into('<I', data, 34, 8)
    path.write_bytes(data)
    with pytest.raises(UnsupportedError, match='Multiple IFDs'):
        read_dcp(path)


def test_unknown_tag_retained_but_not_rendered(tmp_path):
    p = profile(tmp_path, [(50721, 10, np.eye(3).ravel()), (65000, 4, [123])])
    assert dict(p.tags)[65000] == 123
    assert next(c for c in p.info.capabilities if c.feature == 'technical').status == 'unsupported'
    with pytest.raises(UnsupportedError, match='65000'):
        render_dcp(np.zeros((1, 3)), p, settings=DcpSettings(camera_neutral=(1, 1, 1)))


def test_io_error_and_missing_matrix(tmp_path):
    with pytest.raises(InvalidProfileError) as exc:
        read_dcp(tmp_path / 'missing.dcp')
    assert exc.value.code == 'profile_io' and isinstance(exc.value.__cause__, OSError)
    p = profile(tmp_path, [(50940, 11, [0, 0, 1, 1])])
    with pytest.raises(UnsupportedError, match='ColorMatrix1'):
        create_dcp_transform(p, settings=DcpSettings(camera_neutral=(1, 1, 1)))


@pytest.mark.parametrize('neutral', [None, (0, 1, 1), (-1, 1, 1), (1, float('nan'), 1)])
def test_invalid_neutral(tmp_path, neutral):
    p = profile(tmp_path)
    with pytest.raises(ValidationError):
        render_dcp(np.ones((1, 3)), p, settings=DcpSettings(camera_neutral=neutral))


def test_closed_transform_gamut_nonfinite_and_input_ownership(tmp_path):
    p = profile(tmp_path)
    transform = create_dcp_transform(p, settings=DcpSettings(camera_neutral=tuple(D50), gamut='error'))
    a = np.array([[.1, .1, .1]], np.float32)
    old = a.copy()
    transform.apply(a)
    np.testing.assert_array_equal(a, old)
    with pytest.raises(ValidationError): transform.apply(np.array([[float('nan'), 0, 0]]))
    with pytest.raises(ValidationError): transform.apply(np.zeros((2, 4)))
    with pytest.raises(ValidationError): transform.apply(np.array([[2, 0, 0]]))
    transform.close()
    with pytest.raises(ValidationError, match='closed'): transform.apply(a)


def test_cancellation_at_entry_and_during_chunks(tmp_path):
    p = profile(tmp_path)
    class Token:
        cancelled = False
        def is_cancelled(self): return self.cancelled
    token = Token()
    def progress(p):
        if p.stage == 'dcp.render' and p.fraction > 0:
            token.cancelled = True
    transform = create_dcp_transform(p, settings=DcpSettings(camera_neutral=tuple(D50)))
    with pytest.raises(CancelledError):
        transform.apply(np.zeros((100000, 3), np.float32), context=JobContext(progress=progress, cancel=token))
    with pytest.raises(CancelledError):
        read_dcp(p.info.path, context=JobContext(cancel=token))


@pytest.mark.parametrize('name', ['Nikon_D70s.dcp', 'Nikon_D200.dcp', 'Olympus_E1.dcp'])
def test_supplied_profiles_real_smoke(name):
    path = Path(__file__).parents[1] / 'input' / 'ccd_profiles' / name
    if not path.exists(): pytest.skip('External local profile not installed')
    p = read_dcp(path)
    grid = np.stack(np.meshgrid(*([np.linspace(0, 1, 9)] * 3), indexing='ij'), axis=-1).astype(np.float32)
    out = render_dcp(grid, p, settings=DcpSettings(camera_neutral=(.5, 1, .7), illuminant_kelvin=5000))
    assert out.shape == grid.shape and out.dtype == np.float32
    assert np.isfinite(out).all() and out.min() >= 0 and out.max() <= 1
    assert not np.allclose(out, grid)
    assert next(c for c in p.info.capabilities if c.feature == 'jpeg_accuracy').status == 'unverified'


def test_reversed_illuminants_and_equal_temperature_error():
    assert _weight({50778: 21, 50779: 17}, 1000., dual=True) == 0
    assert _weight({50778: 21, 50779: 17}, 20000., dual=True) == 1
    assert _weight({50778: 21, 50779: 17}, 2 / (1 / 2856 + 1 / 6504), dual=True) == pytest.approx(.5)
    with pytest.raises(InvalidProfileError, match='must differ'):
        _weight({50778: 21, 50779: 21}, 5000., dual=True)
    with pytest.raises(UnsupportedError, match='fixed CCT'):
        _weight({50778: 0, 50779: 21}, 5000., dual=True)


def test_file_limit_before_parse(tmp_path, monkeypatch):
    import camera_profile_studio.dcp as module
    monkeypatch.setattr(module, '_MAX_BYTES', 128)
    path = tmp_path / 'huge.dcp'
    path.write_bytes(b'X' * 129)
    with pytest.raises(InvalidProfileError) as exc:
        read_dcp(path)
    assert exc.value.code == 'profile_too_large'


def test_identity_individual_calibration_is_allowed_but_not_inferred(tmp_path):
    p = profile(tmp_path, [(50721, 10, np.eye(3).ravel()), (50723, 10, np.eye(3).ravel()),
                           (50727, 5, [1, 1, 1])])
    actual = render_dcp(np.array([D50 * .2 / max(D50)], np.float32), p,
                        settings=DcpSettings(camera_neutral=tuple(D50)))
    np.testing.assert_allclose(actual, colour.cctf_encoding(np.full((1, 3), .2)), atol=2e-6)


def test_zero_y_white_rejected(tmp_path):
    cm = np.array([[1, 0, 0], [0, 1, 0], [0, 0, -1]])
    p = profile(tmp_path, [(50721, 10, cm.ravel())])
    with pytest.raises(InvalidProfileError, match='nonpositive XYZ'):
        create_dcp_transform(p, settings=DcpSettings(camera_neutral=(1, 1, 1)))
