import numpy as np
import pytest
from camera_profile_studio.color import srgb_decode, srgb_encode, srgb_to_lab, delta_e00
from camera_profile_studio.errors import ValidationError


def test_transfer_known_values_and_roundtrip():
    values = np.array([[0, 0.04045, 1], [0.5, 0.25, 0.75]], dtype=np.float32)
    linear = srgb_decode(values)
    np.testing.assert_allclose(linear[0], [0, 0.003130805, 1], atol=2e-8)
    assert abs(float(linear[1, 0]) - 0.21404114) < 2e-8
    np.testing.assert_allclose(srgb_encode(linear), values, atol=6e-8)


def test_lab_reference_white_black_red():
    lab = srgb_to_lab(np.array([[1, 1, 1], [0, 0, 0], [1, 0, 0]], dtype=np.float32))
    np.testing.assert_allclose(lab[0], [100, 0, 0], atol=1e-10)
    np.testing.assert_allclose(lab[1], [0, 0, 0], atol=1e-10)
    np.testing.assert_allclose(lab[2], [53.2408, 80.0925, 67.2032], atol=0.001)


def test_sharma_delta_e00_reference():
    # Sharma/Wu/Dalal CIEDE2000 supplementary test data, pair 1.
    first = np.array([[50, 2.6772, -79.7751]])
    second = np.array([[50, 0, -82.7485]])
    np.testing.assert_allclose(delta_e00(first, second), [2.0425], atol=0.00005)
    np.testing.assert_allclose(delta_e00(first, first), [0], atol=0)


@pytest.mark.parametrize('bad', [np.array([1, 2]), np.array([1, np.nan, 1])])
def test_bad_rgb(bad):
    with pytest.raises(ValidationError):
        srgb_decode(bad)
