from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Barrier

import numpy as np
import pytest

from camera_profile_studio.errors import CancelledError, ExportError, UnsupportedError, ValidationError
from camera_profile_studio.lut import export_cube, generate_lut, read_cube, sample_lut, validate_lut
from camera_profile_studio.types import JobContext, Lut3D, SpaceSpec

SRGB = SpaceSpec('srgb')


def tetra_reference(values, rgb):
    """Independent per-point tetrahedral interpolation (six explicit cases)."""
    n = values.shape[0]
    out = np.empty((len(rgb), 3), np.float64)
    for q, x in enumerate(np.clip(np.asarray(rgb, np.float64), 0, 1)):
        p = x * (n - 1)
        i, j, k = (min(int(c), n - 2) for c in p)
        fr, fg, fb = p[0] - i, p[1] - j, p[2] - k
        v = lambda a, b, c: values[i + a, j + b, k + c].astype(np.float64)
        c000, c111 = v(0, 0, 0), v(1, 1, 1)
        if fr >= fg >= fb:
            out[q] = (1 - fr) * c000 + (fr - fg) * v(1, 0, 0) + (fg - fb) * v(1, 1, 0) + fb * c111
        elif fr >= fb > fg:
            out[q] = (1 - fr) * c000 + (fr - fb) * v(1, 0, 0) + (fb - fg) * v(1, 0, 1) + fg * c111
        elif fb > fr >= fg:
            out[q] = (1 - fb) * c000 + (fb - fr) * v(0, 0, 1) + (fr - fg) * v(1, 0, 1) + fg * c111
        elif fg > fr >= fb:
            out[q] = (1 - fg) * c000 + (fg - fr) * v(0, 1, 0) + (fr - fb) * v(1, 1, 0) + fb * c111
        elif fg >= fb > fr:
            out[q] = (1 - fg) * c000 + (fg - fb) * v(0, 1, 0) + (fb - fr) * v(0, 1, 1) + fr * c111
        else:  # fb > fg > fr
            out[q] = (1 - fb) * c000 + (fb - fg) * v(0, 0, 1) + (fg - fr) * v(0, 1, 1) + fr * c111
    return out


class Asymmetric:
    input_space = output_space = SRGB
    provenance = (('evidence', 'synthetic asymmetric multilinear transform'), ('custom', 'preserved'))
    closed = False

    def apply(self, rgb, *, context=None):
        r, g, b = rgb.T
        return np.stack((.1 + .3*r + .2*g*b, .05 + .6*g + .1*r*b, .2 + .4*b + .1*r*g), -1).astype(np.float32)

    def close(self):
        self.closed = True


def test_asymmetric_tetrahedral_independent_reference():
    transform = Asymmetric()
    lut = generate_lut(transform, size=17)
    samples = np.vstack((np.random.default_rng(12).random((123, 3)), np.eye(3), np.zeros(3), np.ones(3))).astype(np.float32)
    # the transform has bilinear cross terms, which tetrahedral (unlike trilinear) does not
    # reproduce between nodes: it must match the independent tetrahedral reference tightly
    # and the analytic transform only to the O(1/16^2) interpolation error ...
    np.testing.assert_allclose(sample_lut(lut, samples), tetra_reference(lut.values, samples), atol=2e-6)
    np.testing.assert_allclose(sample_lut(lut, samples), transform.apply(samples), atol=4e-4)
    # ... while the grid nodes themselves are exact
    axis = np.linspace(0, 1, 17, dtype=np.float32)
    nodes = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), -1).reshape(-1, 3)
    np.testing.assert_allclose(sample_lut(lut, nodes), transform.apply(nodes), atol=1.5e-7)
    assert not transform.closed
    assert validate_lut(lut).valid
    assert not lut.values.flags.writeable


def test_red_fastest_cube_and_roundtrip(tmp_path):
    lut = generate_lut(Asymmetric(), size=17, title='quotes " slash \\ newline\n# injected')
    result = export_cube(lut, tmp_path / 'order.cube')
    lines = result.cube_path.read_text().splitlines()
    data = np.array([list(map(float, line.split())) for line in lines if line and line[0].isdigit()])
    np.testing.assert_allclose(data[1], Asymmetric().apply(np.array([[1/16, 0, 0]], np.float32))[0])
    np.testing.assert_allclose(data[17], Asymmetric().apply(np.array([[0, 1/16, 0]], np.float32))[0])
    np.testing.assert_allclose(data[17**2], Asymmetric().apply(np.array([[0, 0, 1/16]], np.float32))[0])
    restored = read_cube(result.cube_path, input_space=SRGB, output_space=SRGB)
    np.testing.assert_array_equal(restored.values, lut.values)
    assert restored.title == lut.title
    metadata = json.loads(result.metadata_path.read_text())
    assert metadata['cube_sha256'] == hashlib.sha256(result.cube_path.read_bytes()).hexdigest()
    assert metadata['provenance']['custom'] == 'preserved'
    assert metadata['software']['numpy'] == np.__version__


@pytest.mark.parametrize('strength', [0, .37, 1])
def test_strength_baked_exactly_once(tmp_path, strength):
    lut = generate_lut(Asymmetric(), size=17, strength=strength)
    path = export_cube(lut, tmp_path / 'strength.cube').cube_path
    restored = read_cube(path, input_space=SRGB, output_space=SRGB)
    rgb = np.random.default_rng(16).random((32, 3)).astype(np.float32)
    axis = np.linspace(0, 1, 17, dtype=np.float32)
    nodes = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), -1).reshape(17, 17, 17, 3)
    baked = ((1-strength)*nodes + strength*Asymmetric().apply(nodes.reshape(-1, 3)).reshape(nodes.shape)).astype(np.float32)
    # blending once at the nodes, then sampling, equals the independent tetrahedral reference of the blended nodes
    np.testing.assert_allclose(sample_lut(restored, rgb), tetra_reference(baked, rgb), atol=2e-6)
    # and stays within the interpolation error of the direct (continuous) blend; a second blend would be far off
    np.testing.assert_allclose(sample_lut(restored, rgb), (1-strength)*rgb + strength*Asymmetric().apply(rgb), atol=4e-4)
    assert restored.strength == strength


def test_zero_strength_does_not_mask_unsupported():
    class Unsupported(Asymmetric):
        def apply(self, rgb, *, context=None):
            raise UnsupportedError('Not implemented for this profile')
    with pytest.raises(UnsupportedError):
        generate_lut(Unsupported(), size=17, strength=0)
    transform = Asymmetric()
    transform.output_space = SpaceSpec('linear_srgb')
    with pytest.raises(ValidationError):
        generate_lut(transform, strength=.5)


@pytest.mark.parametrize('mutation', ['short', 'long', 'nan', 'inf', 'range', 'domain', 'shaper', 'duplicate', 'late', 'channels', 'unknown'])
def test_reject_malformed_cube(tmp_path, mutation):
    path = export_cube(generate_lut(Asymmetric(), size=17), tmp_path / 'bad.cube').cube_path
    text = path.read_text()
    lines = text.splitlines()
    if mutation == 'short': lines.pop()
    if mutation == 'long': lines.append('0 0 0')
    if mutation in ('nan', 'inf'): lines[-1] = f'{mutation} 0 0'
    if mutation == 'range': lines[-1] = '1.01 0 0'
    if mutation == 'domain': lines[5] = 'DOMAIN_MIN -1 0 0'
    if mutation == 'shaper': lines.insert(0, 'LUT_1D_SIZE 17')
    if mutation == 'duplicate': lines.insert(0, 'LUT_3D_SIZE 17')
    if mutation == 'late': lines.append('DOMAIN_MIN 0 0 0')
    if mutation == 'channels': lines[-1] = '0 0 0 1'
    if mutation == 'unknown': lines.insert(0, 'LUT_3D_INPUT_RANGE 0 1')
    path.write_text('\n'.join(lines))
    with pytest.raises(ValidationError):
        read_cube(path, input_space=SRGB, output_space=SRGB)


def test_sampling_bounds_and_no_mutation():
    lut = generate_lut(Asymmetric(), size=17)
    rgb = np.array([[-.2, .4, 1.4]], np.float32)
    old = rgb.copy()
    np.testing.assert_allclose(sample_lut(lut, rgb), Asymmetric().apply(np.clip(rgb, 0, 1)), atol=1e-7)
    np.testing.assert_array_equal(rgb, old)
    with pytest.raises(ValidationError): sample_lut(lut, rgb, out_of_domain='error')
    with pytest.raises(ValidationError): sample_lut(lut, np.array([[np.nan, 0, 0]]))


def test_two_simultaneous_exports_only_one_succeeds(tmp_path):
    barrier = Barrier(2)
    lut = generate_lut(Asymmetric(), size=17)
    path = tmp_path / 'race.cube'
    def run():
        barrier.wait()
        try:
            return export_cube(lut, path)
        except ExportError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert path.exists() and path.with_suffix('.json').exists()
    assert not list(tmp_path.glob('*.lock'))


def test_partial_publication_is_error(tmp_path, monkeypatch):
    import camera_profile_studio.lut as module
    original = module.os.link
    def fail_sidecar(source, destination):
        if destination.suffix == '.json':
            raise OSError('injected sidecar failure')
        original(source, destination)
    monkeypatch.setattr(module.os, 'link', fail_sidecar)
    with pytest.raises(ExportError) as exc:
        export_cube(generate_lut(Asymmetric(), size=17), tmp_path / 'partial.cube')
    assert exc.value.code == 'partial_publication'
    assert 'partial.cube' in dict(exc.value.details)['published']


def test_cancel_before_publication_leaves_no_artifacts(tmp_path):
    class Token:
        cancelled = False
        def is_cancelled(self): return self.cancelled
    token = Token()
    def progress(p):
        if p.stage == 'export_cube' and p.fraction >= .8: token.cancelled = True
    with pytest.raises(CancelledError):
        export_cube(generate_lut(Asymmetric(), size=17), tmp_path / 'cancel.cube', context=JobContext(progress, token))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('size', [17, 33, 65])
def test_supported_sizes(size):
    assert generate_lut(Asymmetric(), size=size).values.shape == (size, size, size, 3)


def test_constant_white_interpolation_stays_unit_bounded():
    lut = Lut3D(np.ones((17, 17, 17, 3), np.float32), SRGB, SRGB)
    rgb = np.random.default_rng(123).random((100001, 3)).astype(np.float32)
    result = sample_lut(lut, rgb)
    assert result.max() <= 1 and result.min() >= 0
    np.testing.assert_allclose(result, 1, atol=2e-7)


def test_cancel_arriving_during_commit_returns_committed_result(tmp_path, monkeypatch):
    import camera_profile_studio.lut as module
    class Token:
        cancelled = False
        def is_cancelled(self): return self.cancelled
    token = Token()
    original = module.os.link
    def link_then_cancel(source, destination):
        original(source, destination)
        token.cancelled = True
    monkeypatch.setattr(module.os, 'link', link_then_cancel)
    events = []
    result = export_cube(generate_lut(Asymmetric(), size=17), tmp_path/'committed.cube',
                         context=JobContext(events.append, token))
    assert token.cancelled
    assert result.cube_path.exists() and result.metadata_path.exists()
    assert events[-1].fraction == 1


def test_cancel_at_last_precommit_progress_does_not_publish(tmp_path):
    class Token:
        cancelled = False
        def is_cancelled(self): return self.cancelled
    token = Token()
    def progress(p):
        if p.fraction == .95: token.cancelled = True
    with pytest.raises(CancelledError):
        export_cube(generate_lut(Asymmetric(), size=17), tmp_path/'not-committed.cube',
                    context=JobContext(progress, token))
    assert list(tmp_path.iterdir()) == []


def test_sidecar_preserved_and_tampering_detected(tmp_path):
    result = export_cube(generate_lut(Asymmetric(), size=17), tmp_path/'provenance.cube')
    restored = read_cube(result.cube_path, input_space=SRGB, output_space=SRGB)
    assert dict(restored.provenance)['custom'] == 'preserved'
    metadata = json.loads(result.metadata_path.read_text())
    metadata['cube_sha256'] = '0'*64
    result.metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValidationError, match='Companion'):
        read_cube(result.cube_path, input_space=SRGB, output_space=SRGB)


@pytest.mark.parametrize('value', ['bad', True, float('nan'), -1, 1.1])
def test_invalid_strength_settings_are_structured_errors(value):
    with pytest.raises(ValidationError): generate_lut(Asymmetric(), strength=value)


@pytest.mark.parametrize('value', [np.empty((0, 3)), [['red', 'green', 'blue']], np.ones((2, 3), complex)])
def test_invalid_rgb_arrays_are_structured_errors(value):
    with pytest.raises(ValidationError): sample_lut(generate_lut(Asymmetric(), size=17), value)


def test_generate_and_sample_chunk_progress_monotonic():
    events = []
    lut = generate_lut(Asymmetric(), size=65, context=JobContext(events.append))
    sample_lut(lut, np.random.default_rng(10).random((70000, 3)).astype(np.float32), context=JobContext(events.append))
    for stage in {p.stage for p in events}:
        fractions = [p.fraction for p in events if p.stage == stage]
        assert fractions == sorted(fractions)
        assert fractions[0] == 0 and fractions[-1] == 1
