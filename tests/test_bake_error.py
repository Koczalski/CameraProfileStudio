"""``lut.bake_error``: how far the exported grid LUT is from the transform it was baked from (dE00)."""
import numpy as np
import pytest

from camera_profile_studio.errors import ValidationError
from camera_profile_studio.lut import bake_error, generate_lut
from camera_profile_studio.types import BakeError, ErrorStats, SpaceSpec

SRGB = SpaceSpec('srgb')


class _Transform:
    def __init__(self, fn, output=SRGB):
        self.fn, self.input_space, self.output_space = fn, SRGB, output

    def apply(self, rgb, *, context=None):
        return np.clip(self.fn(np.asarray(rgb, dtype=np.float32)), 0, 1).astype(np.float32)

    def close(self):
        pass


IDENTITY = _Transform(lambda rgb: rgb)
CURVE = _Transform(lambda rgb: rgb ** 1.8 * [1.0, 0.92, 0.85] + 0.05 * np.sin(np.pi * rgb[..., ::-1]))


def test_identity_bakes_without_error_at_any_size():
    for size in (17, 33, 65):
        result = bake_error(IDENTITY, generate_lut(IDENTITY, size=size))
        assert isinstance(result, BakeError) and isinstance(result.uniform, ErrorStats)
        assert result.size == size and result.strength == 1.0 and result.uniform.count == 30000
        assert result.uniform.maximum < 1e-3
        assert result.points is None


def test_curve_error_falls_with_grid_size_and_is_zero_on_the_nodes():
    results = {size: bake_error(CURVE, generate_lut(CURVE, size=size)) for size in (17, 33, 65)}
    means = [results[s].uniform.mean for s in (17, 33, 65)]
    maxima = [results[s].uniform.maximum for s in (17, 33, 65)]
    assert means[0] > means[1] > means[2] > 0
    assert maxima[0] > maxima[1] > maxima[2]
    for result in results.values():
        assert result.uniform.maximum >= result.uniform.p95 >= result.uniform.median
    axis = np.linspace(0, 1, 17, dtype=np.float32)
    nodes = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1).reshape(-1, 3)
    on_nodes = bake_error(CURVE, generate_lut(CURVE, size=17), points=nodes)
    assert on_nodes.points is not None and on_nodes.points.count == len(nodes)
    assert on_nodes.points.maximum < 1e-3          # grid nodes are reproduced exactly
    assert on_nodes.uniform.maximum > 0.01          # between the nodes they are not


def test_points_are_evaluated_as_given_and_the_uniform_sample_is_deterministic():
    lut = generate_lut(CURVE, size=17)
    points = np.random.default_rng(3).uniform(0, 1, (500, 3)).astype(np.float32)
    first, second = bake_error(CURVE, lut, points=points), bake_error(CURVE, lut, points=points)
    assert first == second
    assert first.points.count == 500
    assert bake_error(CURVE, lut, seed=1).uniform.mean != first.uniform.mean
    assert bake_error(CURVE, lut, count=2000).uniform.count == 2000


def test_strength_of_the_lut_is_honoured():
    # a LUT baked at strength 0 is the identity; the reference must be blended the same way
    assert bake_error(CURVE, generate_lut(CURVE, size=17, strength=0.0)).uniform.maximum < 1e-3
    half = bake_error(CURVE, generate_lut(CURVE, size=17, strength=0.5))
    assert half.strength == 0.5
    assert 0 < half.uniform.mean < bake_error(CURVE, generate_lut(CURVE, size=17)).uniform.mean


LINEAR_OUT = _Transform(lambda rgb: rgb, output=SpaceSpec('linear_srgb'))


@pytest.mark.parametrize('transform, lut_of', [(LINEAR_OUT, LINEAR_OUT),   # dE00 needs encoded sRGB output
                                               (IDENTITY, LINEAR_OUT)])    # LUT of another transform
def test_only_srgb_output_luts_of_the_same_transform_are_measured(transform, lut_of):
    with pytest.raises(ValidationError) as info:
        bake_error(transform, generate_lut(lut_of, size=17))
    assert info.value.code == 'bake_error_unsupported'


@pytest.mark.parametrize('kwargs', [{'count': 0}, {'count': 10 ** 7}, {'seed': -1}, {'points': np.zeros((4, 2))},
                                    {'points': np.full((4, 3), 2.0)}])
def test_invalid_arguments_are_rejected(kwargs):
    with pytest.raises(ValidationError):
        bake_error(IDENTITY, generate_lut(IDENTITY, size=17), **kwargs)
