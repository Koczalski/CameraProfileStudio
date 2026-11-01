"""Selected DCP photo components against hand-calculated working-space fixtures."""
from dataclasses import replace
from pathlib import Path

import colour
import numpy as np
import pytest

from camera_profile_studio.dcp import read_dcp, render_dcp, _components
from camera_profile_studio.look import apply_look, create_look_transform, look_capabilities
from camera_profile_studio.errors import InvalidProfileError, UnsupportedError, ValidationError
from camera_profile_studio.types import DcpSettings
from test_dcp import write_fixture, profile, D50, CM, reference_srgb

# Independent Colour primary-matrix derivation, not an application helper.
PROPHOTO = colour.normalised_primary_matrix(np.array([[.7347, .2653], [.1596, .8404], [.0366, .0001]]), np.array([.3457, .3585]))


def photo_for_work(work):
    return reference_srgb(np.asarray(work) @ PROPHOTO.T).astype(np.float32)


def basic_entries():
    return [(50721, 10, CM.ravel()),
            (50937, 4, [1, 2, 1]), (50938, 11, [0, .5, 1, 0, .5, 1]),
            (50981, 4, [1, 2, 1]), (50982, 11, [0, 1, 1, 0, 1, .5]),
            (50940, 11, [0, 0, .3046875, .1, .4875, .8, 1, 1]), (51110, 4, [1])]


def test_combined_order_huesat_then_look_then_hue_preserving_tone(tmp_path):
    p = profile(tmp_path, basic_entries())
    rgb = photo_for_work([[.6, .3, .15]])
    # HSM halves saturation: [.6,.45,.375]. Look's interpolated V multiplier
    # is 1 - .5*.375 = .8125, giving [.4875,.365625,.3046875].
    # Tone min=.1, max=.8; middle lies 1/3 between, giving 1/3 exactly.
    expected = photo_for_work([[.8, 1 / 3, .1]])
    actual = apply_look(rgb, p)
    np.testing.assert_allclose(actual, expected, atol=2e-4)
    assert not np.allclose(actual, rgb)


def test_colors_only_does_not_apply_tone(tmp_path):
    p = profile(tmp_path, basic_entries())
    rgb = photo_for_work([[.6, .3, .15]])
    expected = photo_for_work([[.4875, .365625, .3046875]])
    np.testing.assert_allclose(apply_look(rgb, p, mode='colors_only'), expected, atol=2e-4)


def test_look_only_does_not_apply_hsm_or_tone(tmp_path):
    p = profile(tmp_path, basic_entries())
    rgb = photo_for_work([[.6, .3, .15]])
    # Original S=.75, value scale=1-.5*.75=.625.
    expected = photo_for_work([[.375, .1875, .09375]])
    np.testing.assert_allclose(apply_look(rgb, p, mode='look_only'), expected, atol=2e-4)


def test_tone_only_is_hue_preserving_in_linear_working_space(tmp_path):
    p = profile(tmp_path, [(50940, 11, [0, 0, .2, .1, .4, .3, .6, .8, 1, 1])])
    rgb = photo_for_work([[.6, .4, .2]])
    expected = photo_for_work([[.8, .45, .1]])
    np.testing.assert_allclose(apply_look(rgb, p, mode='tone_only'), expected, atol=2e-4)


def test_srgb_look_has_no_camera_inverse_or_neutral_requirement(tmp_path):
    entries = [(50940, 11, [0, 0, .2, .1, .4, .3, .6, .8, 1, 1])]
    p = profile(tmp_path, [(50721, 10, (CM * 3).ravel()), *entries])
    rgb = photo_for_work([[.6, .4, .2]])
    expected = photo_for_work([[.8, .45, .1]])
    np.testing.assert_allclose(apply_look(rgb, p, mode='tone_only'), expected, atol=2e-4)


@pytest.mark.parametrize('mode', ['colors_only', 'look_only', 'tone_only', 'combined'])
def test_matrix_only_does_not_invent_a_look(tmp_path, mode):
    p = profile(tmp_path)
    with pytest.raises(UnsupportedError, match='No enabled'):
        apply_look(np.ones((1, 3), np.float32), p, mode=mode)
    caps = {c.feature: c for c in look_capabilities(p)}
    assert caps['look'].status == 'unsupported'
    assert caps['look.' + mode].status == 'unsupported'


def test_all_disabled_or_selected_absent_fails(tmp_path):
    p = profile(tmp_path, basic_entries())
    with pytest.raises(UnsupportedError, match='No enabled'):
        create_look_transform(p, settings=DcpSettings(include_hue_sat_map=False, include_look_table=False, include_tone_curve=False))
    p = profile(tmp_path, [(50940, 11, [0, 0, 1, 1])])
    with pytest.raises(UnsupportedError): create_look_transform(p, mode='look_only')
    with pytest.raises(ValidationError): create_look_transform(p, mode='fictional')


def test_unsupported_encoding_fails_only_when_component_active(tmp_path):
    p = profile(tmp_path, [*basic_entries(), (51107, 4, [7]), (51108, 4, [9])])
    rgb = np.array([[.3, .4, .5]], np.float32)
    with pytest.raises(UnsupportedError, match='encoding'): apply_look(rgb, p)
    actual = apply_look(rgb, p, mode='tone_only')
    assert np.isfinite(actual).all()
    actual = apply_look(rgb, p, settings=DcpSettings(include_hue_sat_map=False, include_look_table=False))
    assert np.isfinite(actual).all()
    caps = {c.feature: c.status for c in p.info.capabilities}
    assert caps['look.tone_only'] == 'partial' and caps['look.colors_only'] == 'unsupported'


def test_invalid_encoding_on_absent_component_is_not_active(tmp_path):
    p = profile(tmp_path, [(50721, 10, CM.ravel()), (51107, 4, [7]), (50940, 11, [0, 0, 1, 1])])
    rgb = np.array([[.3, .4, .5]], np.float32)
    np.testing.assert_allclose(apply_look(rgb, p), rgb, atol=2e-7)


def test_dual_hsm_interpolates_actual_table_triples(tmp_path):
    entries = [(50937, 4, [1, 2, 1]), (50938, 11, [0, .4, 1, 0, .4, 1]),
               (50939, 11, [0, .8, 1, 0, .8, 1]), (50778, 3, [17]), (50779, 3, [21])]
    p = profile(tmp_path, entries)
    kelvin = 1 / (.25 / 2856 + .75 / 6504)
    rgb = photo_for_work([[.6, .3, .15]])
    # S=.75 * (.25*.4+.75*.8)=.525; original hue is retained.
    expected = photo_for_work([[.6, .39, .285]])
    actual = apply_look(rgb, p, settings=DcpSettings(illuminant_kelvin=kelvin))
    np.testing.assert_allclose(actual, expected, atol=2e-4)
    with pytest.raises(ValidationError, match='explicit illuminant'):
        apply_look(rgb, p)


def test_camera_only_unsupported_calibration_does_not_block_tone(tmp_path):
    p = profile(tmp_path, [(50721, 10, CM.ravel()), (50727, 5, [2, 1, 1]), (50940, 11, [0, 0, 1, 1])])
    rgb = np.array([[.3, .4, .5]], np.float32)
    np.testing.assert_allclose(apply_look(rgb, p, mode='tone_only'), rgb, atol=2e-7)
    with pytest.raises(UnsupportedError, match='AnalogBalance'):
        render_dcp(rgb, p, settings=DcpSettings(camera_neutral=(1, 1, 1)))


@pytest.mark.parametrize('tag,value', [(51109, 1), (51110, 9)])
def test_unsupported_exposure_or_black_not_silently_used(tmp_path, tag, value):
    entries = [e for e in basic_entries() if e[0] != tag]
    entries.append((tag, 10 if tag == 51109 else 4, [value]))
    p = profile(tmp_path, entries)
    with pytest.raises(UnsupportedError): create_look_transform(p)
    # Neither exposure offset nor a host black stage belongs to isolated HSM.
    transform = create_look_transform(p, mode='colors_only', settings=DcpSettings(include_look_table=False))
    assert np.isfinite(transform.apply(np.array([[.3, .4, .5]], np.float32))).all()
    transform.close()


def test_default_black_auto_is_explicit_partial_not_invented(tmp_path):
    entries = [e for e in basic_entries() if e[0] != 51110] + [(51110, 4, [0])]
    p = profile(tmp_path, entries)
    assert any('automatic black' in reason for reason in p.info.warnings)
    assert next(c for c in p.info.capabilities if c.feature == 'look.combined').status == 'partial'


def test_huesat_only_is_not_reported_as_jpeg_design(tmp_path):
    p = profile(tmp_path, [(50937, 4, [1, 2, 1]), (50938, 11, [10, .8, 1, 10, .8, 1])])
    caps = {c.feature: c for c in p.info.capabilities}
    assert caps['look'].status == 'partial'
    assert any('not evidence' in r for r in caps['look.colors_only'].reasons)
    assert caps['jpeg_accuracy'].status == 'unverified'


def test_shapes_ownership_closed_and_empty(tmp_path):
    p = profile(tmp_path, [(50940, 11, [0, 0, 1, 1])])
    transform = create_look_transform(p)
    rgb = np.random.default_rng(1).random((2, 4, 3)).astype(np.float32)
    old = rgb.copy()
    output = transform.apply(rgb)
    assert output.shape == rgb.shape and output.dtype == np.float32
    np.testing.assert_array_equal(rgb, old)
    np.testing.assert_allclose(output, rgb, atol=2e-7)
    assert transform.apply(np.empty((0, 3))).shape == (0, 3)
    transform.close()
    with pytest.raises(ValidationError): transform.apply(rgb)


@pytest.mark.parametrize('name', ['Nikon_D70s.dcp', 'Nikon_D200.dcp', 'Olympus_E1.dcp'])
def test_real_photo_smoke_and_d70s_look_plus_tone_without_wb(name):
    path = Path(__file__).parents[1] / 'input' / 'ccd_profiles' / name
    if not path.exists(): pytest.skip('External local profile not installed')
    p = read_dcp(path)
    rgb = np.random.default_rng(25).random((31, 3)).astype(np.float32)
    settings = DcpSettings(include_hue_sat_map=False) if name == 'Nikon_D70s.dcp' else DcpSettings()
    out = apply_look(rgb, p, settings=settings)
    assert out.shape == rgb.shape and np.isfinite(out).all()
    assert out.min() >= 0 and out.max() <= 1
    assert not np.allclose(out, rgb)
    if name != 'Nikon_D70s.dcp':
        with pytest.raises(UnsupportedError): apply_look(rgb, p, settings=DcpSettings(include_hue_sat_map=False))


@pytest.mark.parametrize('tag', [52531, 52532, 52537, 53535])
def test_unsupported_third_illuminant_tags_do_not_block_unrelated_tone(tmp_path, tag):
    p = profile(tmp_path, [*basic_entries(), (tag, 11, [1])])
    assert np.isfinite(apply_look(np.array([[.3, .4, .5]], np.float32), p, mode='tone_only')).all()
    if tag == 52537:
        with pytest.raises(UnsupportedError): create_look_transform(p)
    else:
        with pytest.raises(UnsupportedError):
            render_dcp(np.array([[.3, .4, .5]], np.float32), p, settings=DcpSettings(camera_neutral=(1, 1, 1)))


def test_encoding_one_single_value_slice_rejected_only_if_selected(tmp_path):
    p = profile(tmp_path, [*basic_entries(), (51107, 4, [1])])
    with pytest.raises(UnsupportedError, match='single value slice'):
        create_look_transform(p)
    transform = create_look_transform(p, settings=DcpSettings(include_hue_sat_map=False))
    assert np.isfinite(transform.apply(np.array([[.3, .4, .5]], np.float32))).all()


@pytest.mark.parametrize('tag', [52525, 52543, 52544, 52551])
def test_gain_rgb_tables_dynamic_range_not_silently_ignored(tmp_path, tag):
    p = profile(tmp_path, [*basic_entries(), (tag, 11, [1])])
    with pytest.raises(UnsupportedError, match='processing tags'):
        create_look_transform(p, mode='tone_only')
