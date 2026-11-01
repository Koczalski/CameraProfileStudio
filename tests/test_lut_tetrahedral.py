"""Tetrahedral LUT sampling: grid nodes, grey axis, affine exactness, an
independent per-point reference, and continuity across tetrahedron boundaries."""
import numpy as np
import pytest

from camera_profile_studio.lut import _tetrahedral, sample_lut
from camera_profile_studio.types import Lut3D, SpaceSpec

SRGB = SpaceSpec('srgb')


def make_lut(values):
    return Lut3D(np.ascontiguousarray(values, np.float32), SRGB, SRGB, 'tetra test', 1.0, (('evidence', 'synthetic'),))


def random_lut(n, seed):
    return make_lut(np.random.default_rng(seed).random((n, n, n, 3)))


def node_grid(n):
    axis = np.linspace(0, 1, n, dtype=np.float32)
    return np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)


def tetra_reference(values, rgb):
    """Textbook per-point tetrahedral interpolation, written as the six explicit
    cases c000 + sum(f * edge difference) along the path c000 -> c111."""
    values = np.asarray(values, np.float64)
    n = values.shape[0]
    out = np.empty((len(rgb), 3), np.float64)
    for q, x in enumerate(np.clip(np.asarray(rgb, np.float64), 0, 1)):
        p = x * (n - 1)
        i, j, k = (min(int(c), n - 2) for c in p)
        fr, fg, fb = p[0] - i, p[1] - j, p[2] - k
        c000, c100, c010, c001 = values[i, j, k], values[i + 1, j, k], values[i, j + 1, k], values[i, j, k + 1]
        c110, c101, c011, c111 = values[i + 1, j + 1, k], values[i + 1, j, k + 1], values[i, j + 1, k + 1], values[i + 1, j + 1, k + 1]
        if fr >= fg and fg >= fb:
            out[q] = c000 + fr * (c100 - c000) + fg * (c110 - c100) + fb * (c111 - c110)
        elif fr >= fb and fb > fg:
            out[q] = c000 + fr * (c100 - c000) + fb * (c101 - c100) + fg * (c111 - c101)
        elif fb > fr and fr >= fg:
            out[q] = c000 + fb * (c001 - c000) + fr * (c101 - c001) + fg * (c111 - c101)
        elif fg > fr and fr >= fb:
            out[q] = c000 + fg * (c010 - c000) + fr * (c110 - c010) + fb * (c111 - c110)
        elif fg >= fb and fb > fr:
            out[q] = c000 + fg * (c010 - c000) + fb * (c011 - c010) + fr * (c111 - c011)
        else:  # fb > fg > fr
            out[q] = c000 + fb * (c001 - c000) + fg * (c011 - c001) + fr * (c111 - c011)
    return out


def trilinear_reference(values, rgb):
    values = np.asarray(values, np.float64)
    n = values.shape[0]
    p = np.clip(np.asarray(rgb, np.float64), 0, 1) * (n - 1)
    lo = np.minimum(p.astype(int), n - 2)
    f = p - lo
    out = np.zeros_like(p)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = (f[:, 0] if dr else 1 - f[:, 0]) * (f[:, 1] if dg else 1 - f[:, 1]) * (f[:, 2] if db else 1 - f[:, 2])
                out += values[lo[:, 0] + dr, lo[:, 1] + dg, lo[:, 2] + db] * w[:, None]
    return out


@pytest.mark.parametrize('size', [17, 33, 65])
def test_grid_nodes_are_reproduced_exactly(size):
    lut = random_lut(size, 1)
    nodes = node_grid(size)
    result = sample_lut(lut, nodes)
    assert result.dtype == np.float32 and result.shape == nodes.shape
    np.testing.assert_array_equal(result, lut.values)
    # float64 inputs on the same nodes (the grid is dyadic, so the coordinates are exact)
    np.testing.assert_array_equal(sample_lut(lut, nodes.astype(np.float64)), lut.values)


@pytest.mark.parametrize('size', [17, 33])
def test_grey_axis_of_neutral_diagonal_stays_neutral(size):
    rng = np.random.default_rng(2)
    values = rng.random((size, size, size, 3))               # strongly tinted off-diagonal nodes
    grey = np.linspace(0, 1, size) ** 2.2
    for k in range(size):
        values[k, k, k] = grey[k]                            # neutral diagonal
    lut = make_lut(values)
    t = np.concatenate((np.linspace(0, 1, 2001), rng.random(4000))).astype(np.float32)
    rgb = np.repeat(t[:, None], 3, axis=1)
    out = sample_lut(lut, rgb)
    assert np.abs(out[:, 0] - out[:, 1]).max() <= 1e-6 and np.abs(out[:, 1] - out[:, 2]).max() <= 1e-6
    # it is the piecewise-linear grey ramp along the diagonal
    np.testing.assert_allclose(out[:, 0], np.interp(t, np.linspace(0, 1, size), grey.astype(np.float32)), atol=2e-6)
    # sanity: trilinear interpolation of the very same LUT does tint the greys, so this test discriminates
    tri = trilinear_reference(lut.values, rgb)
    assert np.abs(tri[:, 0] - tri[:, 1]).max() > 1e-2


@pytest.mark.parametrize('size', [17, 33, 65])
def test_identity_and_affine_luts_are_reproduced_at_random_points(size):
    rng = np.random.default_rng(3)
    rgb = rng.random((5000, 3)).astype(np.float32)
    rgb[:300, 1] = rgb[:300, 0]                              # exercise tie cases as well
    rgb[300:600, 2] = rgb[300:600, 1]
    identity = make_lut(node_grid(size))
    np.testing.assert_allclose(sample_lut(identity, rgb), rgb, atol=1e-7, rtol=0)
    # any affine map is linear on every tetrahedron, so it is reproduced as well
    matrix = np.array([[.5, .2, .1], [.1, .6, .2], [.2, .1, .5]])
    offset = np.array([.02, .03, .04])
    affine = make_lut(node_grid(size).astype(np.float64) @ matrix.T + offset)
    np.testing.assert_allclose(sample_lut(affine, rgb), rgb.astype(np.float64) @ matrix.T + offset, atol=2e-6, rtol=0)


def _reference_points(seed, count=2000):
    rng = np.random.default_rng(seed)
    pts = rng.random((count, 3))
    pts[:150, 1] = pts[:150, 0]                              # fr == fg
    pts[150:300, 2] = pts[150:300, 1]                        # fg == fb
    pts[300:450, 2] = pts[300:450, 0]                        # fr == fb
    pts[450:500] = pts[450:500, :1]                          # fr == fg == fb (grey)
    pts[500:530] = rng.integers(0, 2, (30, 3))               # cube corners
    pts[530:560] = rng.random((30, 3)) * 1.6 - .3            # out of range: clipped
    return pts


@pytest.mark.parametrize('size', [17, 33])
def test_matches_independent_per_point_reference(size):
    lut = random_lut(size, 4)
    pts = _reference_points(5)
    np.testing.assert_allclose(sample_lut(lut, pts), tetra_reference(lut.values, pts), atol=1e-6, rtol=0)
    pts32 = pts.astype(np.float32)
    np.testing.assert_allclose(sample_lut(lut, pts32), tetra_reference(lut.values, pts32), atol=1e-6, rtol=0)


def test_core_matches_reference_on_a_9_cube_table():
    # sample_lut only accepts the LUT sizes 17/33/65, so exercise the interpolation core on 9^3 directly
    n = 9
    values = np.random.default_rng(6).random((n, n, n, 3)).astype(np.float32)
    pts = np.clip(_reference_points(7), 0, 1)
    strides = np.array([n * n, n, 1], dtype=np.intp)
    out = _tetrahedral(values.reshape(-1, 3), strides, pts * (n - 1), n)
    np.testing.assert_allclose(out, tetra_reference(values, pts), atol=1e-6, rtol=0)


def test_chunk_boundaries_and_shapes():
    lut = random_lut(17, 8)
    rng = np.random.default_rng(9)
    pts = rng.random((70001, 3)).astype(np.float32)
    flat = sample_lut(lut, pts)
    probe = np.concatenate((np.arange(32760, 32776), np.arange(65530, 65546), np.arange(69990, 70001)))
    np.testing.assert_allclose(flat[probe], tetra_reference(lut.values, pts[probe]), atol=1e-6, rtol=0)
    assert flat.dtype == np.float32 and flat.min() >= 0 and flat.max() <= 1
    image = pts[:60].reshape(5, 12, 3)
    np.testing.assert_array_equal(sample_lut(lut, image), flat[:60].reshape(5, 12, 3))


def _straddle(cell, t, other, a, b, delta):
    """Grid coordinates inside ``cell`` with f_a = t + delta and f_b = t - delta (the third fraction is free)."""
    p = (cell + other).astype(np.float64)
    p[:, a] = cell[:, a] + t + delta
    p[:, b] = cell[:, b] + t - delta
    return p


@pytest.mark.parametrize('axes', [(0, 1), (1, 2), (0, 2)])
@pytest.mark.parametrize('size', [17, 33])
def test_continuous_across_tetrahedron_boundaries(axes, size):
    """Points a hair on either side of the planes f_a == f_b inside a cell give the same colour."""
    lut = random_lut(size, 10)
    rng = np.random.default_rng(11)
    count = 600
    cell = rng.integers(0, size - 1, (count, 3))
    t = rng.uniform(.05, .95, count)
    other = rng.uniform(.05, .95, (count, 3))
    results = {}
    for name, sign in (('above', +1), ('below', -1), ('on_plane', 0)):
        p = _straddle(cell, t, other, *axes, sign * 1e-8) / (size - 1)
        results[name] = sample_lut(lut, p).astype(np.float64)
        # each side also agrees with the independent reference, which picks its tetrahedron by comparison
        np.testing.assert_allclose(results[name], tetra_reference(lut.values, p), atol=1e-6, rtol=0)
    assert np.abs(results['above'] - results['below']).max() < 1e-5
    assert np.abs(results['above'] - results['on_plane']).max() < 1e-5
    assert np.abs(results['below'] - results['on_plane']).max() < 1e-5


@pytest.mark.parametrize('axis', [0, 1, 2])
def test_continuous_across_cell_faces(axis):
    lut = random_lut(17, 12)
    rng = np.random.default_rng(13)
    p = rng.random((800, 3)) * 16
    node = rng.integers(1, 16, 800)
    lower, upper = p.copy(), p.copy()
    lower[:, axis] = node - 1e-8
    upper[:, axis] = node + 1e-8
    a = sample_lut(lut, lower / 16).astype(np.float64)
    b = sample_lut(lut, upper / 16).astype(np.float64)
    assert np.abs(a - b).max() < 1e-5
