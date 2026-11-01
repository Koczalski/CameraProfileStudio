from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

from camera_profile_studio.errors import CancelledError, StudioError, ValidationError
from camera_profile_studio.image_look import (
    BASELINE_VERSION, ImageLookAnalysis, ImageLookModel, ImageLookReference,
    ImageLookSettings, analyze_image_look, apply_image_look,
    create_image_look_transform, fit_image_look, pool_statistics,
)
from camera_profile_studio.images import render_preview
from camera_profile_studio.lut import export_cube, generate_lut, read_cube, sample_lut
from camera_profile_studio.types import ImageData, JobContext, SpaceSpec

ZERO = dict(tone_amount=0, color_amount=0, saturation_amount=0)


def fixture_rgb(cast=0., tone=0., size=128):
    rng = np.random.default_rng(91)
    rgb = rng.uniform(.05, .95, (size, size, 3))
    # A reference includes both broad object colors and near-neutral materials.
    rgb[:size // 2] = rng.uniform(.08, .92, (size // 2, size, 1))
    rgb += tone * rgb * (1 - rgb)
    rgb += np.array([cast, 0, -cast])
    return np.clip(rgb, 0, 1).astype(np.float32)


def save_reference(tmp_path, rgb=None, name='reference.png', **kwargs):
    path = tmp_path / name
    Image.fromarray(np.round((fixture_rgb() if rgb is None else rgb) * 255).astype(np.uint8)).save(path)
    return ImageLookReference(path, **kwargs)


def fit_reference(tmp_path, rgb=None, settings=ImageLookSettings(), **kwargs):
    reference = save_reference(tmp_path, rgb, **kwargs)
    analysis = analyze_image_look((reference,), settings=settings)
    return fit_image_look(analysis), analysis


def fit_with_base(tmp_path, reference_rgb, base_rgb, settings=ImageLookSettings()):
    """Fit the look of ``reference_rgb`` relative to the user's own photo ``base_rgb``."""
    refs = (save_reference(tmp_path, reference_rgb, 'ref.png'),
            save_reference(tmp_path, base_rgb, 'base.png', role='base'))
    analysis = analyze_image_look(refs, settings=settings)
    return fit_image_look(analysis), analysis


def grid(size=17):
    axis = np.linspace(0, 1, size)
    return np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1).reshape(-1, 3)


def deviation(model):
    return model.tone_y - model.tone_x


LUMA = np.array([.2126, .7152, .0722])


@pytest.mark.parametrize('cast', [-.035, .035])
def test_actual_warm_cool_nonidentity_and_safety(tmp_path, cast):
    model, analysis = fit_reference(tmp_path, fixture_rgb(cast=cast))
    assert analysis.references[0].color_support > .5
    # warm = +yellow (Oklab b), cool = -b; the shift is measured against neutral grey
    assert np.sign(model.cast[:, 1].mean()) == np.sign(cast)
    assert 'typical_photo_baseline' in model.warnings
    rgb = np.random.default_rng(4).uniform(0, 1, (10000, 3)).astype(np.float32)
    result = apply_image_look(rgb, model)
    assert np.isfinite(result).all() and result.min() >= 0 and result.max() <= 1
    # a typical-photo look is a damped, bounded change of saturated random colours
    assert .001 < np.mean(np.abs(result - rgb)) < .15
    assert not result.flags.writeable
    grey = np.repeat(np.linspace(0, 1, 4097)[:, None], 3, axis=1)
    out = apply_image_look(grey, model)
    luma = out @ LUMA
    assert np.all(np.diff(luma) >= -1e-7)
    assert np.all(np.diff(luma[64:-64]) > 0)
    # the cast moves neutrals toward the reference's cast but never flips its direction
    mid = out[1024:3072]
    assert np.sign(np.mean(mid[:, 0] - mid[:, 2])) == np.sign(cast)
    assert np.all(apply_image_look(np.array([[0, 0, 0], [1, 1, 1]]), model)[1] > .95)


@pytest.mark.parametrize('tone', [-.6, .6])
def test_tone_is_fitted_not_identity(tmp_path, tone):
    model, _ = fit_with_base(tmp_path, fixture_rgb(tone=tone), fixture_rgb())
    assert np.sign(deviation(model)[1:-1].mean()) == np.sign(tone)
    assert np.max(np.abs(deviation(model))) > .05
    grey = np.repeat(np.linspace(.1, .9, 33)[:, None], 3, axis=1)
    assert np.sign(np.mean(apply_image_look(grey, model) - grey)) == np.sign(tone)
    # a colour-cast-only difference leaves the tone curve (almost) on the identity
    same, _ = fit_with_base(tmp_path, fixture_rgb(cast=.03), fixture_rgb())
    assert np.max(np.abs(deviation(same)[1:-1])) < .01 < np.max(np.abs(deviation(model)))


@pytest.mark.parametrize('base', [False, True])
def test_zero_amounts_are_identity_within_1e6(tmp_path, base):
    settings = ImageLookSettings(**ZERO)
    reference, other = fixture_rgb(cast=.04, tone=.4), fixture_rgb(cast=-.02, tone=-.3)
    model, _ = fit_with_base(tmp_path, reference, other, settings) if base else fit_reference(tmp_path, reference, settings)
    assert model.baseline == ('base_images' if base else 'typical_photo')
    np.testing.assert_array_equal(model.tone_y, model.tone_x)
    assert not model.cast.any() and (model.band_gain == 1).all() and (model.sector_gain == 1).all()
    points = grid(17)
    np.testing.assert_allclose(apply_image_look(points, model), points, atol=1e-6)
    rgb = fixture_rgb().reshape(-1, 3)
    np.testing.assert_allclose(apply_image_look(rgb, model), rgb, atol=1e-6)
    # a model with active amounts on the same input is clearly not the identity
    active, _ = fit_with_base(tmp_path, reference, other) if base else fit_reference(tmp_path, reference)
    assert np.max(np.abs(apply_image_look(points, active) - points)) > .01


def test_independent_controls(tmp_path):
    reference, base = fixture_rgb(cast=.04, tone=.4), fixture_rgb()
    probe = np.array([[.3, .4, .5], [.5, .5, .5], [.7, .4, .3]], np.float32)
    outputs = []
    for control in ('tone_amount', 'color_amount', 'saturation_amount'):
        values = dict(ZERO)
        values[control] = 1
        model, _ = fit_with_base(tmp_path, reference, base, ImageLookSettings(**values))
        out = apply_image_look(probe, model)
        assert np.max(np.abs(out - probe)) > 1e-4, control
        outputs.append(out)
        # the other two components really are off in the fitted model
        if control != 'tone_amount':
            np.testing.assert_array_equal(model.tone_y, model.tone_x)
        if control != 'color_amount':
            assert not model.cast.any()
        if control != 'saturation_amount':
            assert (model.band_gain == 1).all() and (model.sector_gain == 1).all()
    assert not np.allclose(outputs[0], outputs[1]) and not np.allclose(outputs[1], outputs[2])


@pytest.mark.parametrize('base', [False, True])
def test_tone_amount_scales_the_tone_deviation_linearly(tmp_path, base):
    reference, other = fixture_rgb(cast=.01, tone=.3), fixture_rgb()
    deviations = {}
    for amount in (0., .25, .5, 1.):
        settings = ImageLookSettings(tone_amount=amount)
        model, _ = fit_with_base(tmp_path, reference, other, settings) if base else fit_reference(tmp_path, reference, settings)
        deviations[amount] = deviation(model)
    assert np.max(np.abs(deviations[1.])) > .01
    assert not deviations[0.].any()
    for amount in (.25, .5):
        np.testing.assert_allclose(deviations[amount], amount * deviations[1.], atol=1e-9)
    knots = fit_with_base(tmp_path, reference, other)[0].tone_x if base else fit_reference(tmp_path, reference)[0].tone_x
    assert np.all(np.diff(knots) > 0) and knots[0] == 0 and knots[-1] == 1


def test_typical_photo_baseline_is_damped_and_recorded(tmp_path):
    import camera_profile_studio.image_look as module
    reference = fixture_rgb(cast=.04, tone=.4)
    damping = module._TYPICAL_DAMPING
    assert set(damping) == {'tone', 'cast', 'saturation'} and all(0 < value <= 1 for value in damping.values())
    typical, _ = fit_reference(tmp_path, reference)
    algorithm = json.loads(dict(typical.provenance)['algorithm'])
    assert algorithm['typical_photo_damping'] == damping
    assert json.loads(dict(typical.provenance)['baseline'])['kind'] == 'typical_photo'
    # the same statistics against an explicit base are applied undamped
    stats = analyze_image_look((save_reference(tmp_path, reference),)).references[0].stats
    full = module._fit(stats, module._typical(), ImageLookSettings(), 'base_images')
    damped = module._fit(stats, module._typical(), ImageLookSettings(), 'typical_photo')
    np.testing.assert_allclose(damped[1] - damped[0], damping['tone'] * (full[1] - full[0]), atol=5e-3)
    np.testing.assert_allclose(damped[2], damping['cast'] * full[2], atol=1e-12)
    np.testing.assert_allclose(damped[3] - 1, damping['saturation'] * (full[3] - 1), atol=1e-12)
    assert not np.array_equal(damped[1], full[1])
    explicit, _ = fit_with_base(tmp_path, reference, fixture_rgb())
    assert json.loads(dict(explicit.provenance)['algorithm'])['typical_photo_damping'] is None
    assert explicit.baseline == 'base_images' and 'typical_photo_baseline' not in explicit.warnings


def test_order_weights_duplicates_and_resolution_independent(tmp_path):
    rgb = fixture_rgb(cast=.025, size=64)
    small = save_reference(tmp_path, rgb, 'small.png')
    large = save_reference(tmp_path, np.repeat(np.repeat(rgb, 3, 0), 3, 1), 'large.png')
    other = save_reference(tmp_path, fixture_rgb(cast=.015, tone=.15), 'other.png', weight=2)
    a = fit_image_look(analyze_image_look((small, other)))
    b = fit_image_look(analyze_image_look((other, small)))
    c = fit_image_look(analyze_image_look((large, other)))
    for field in ('tone_x', 'tone_y', 'cast', 'band_gain', 'sector_gain'):
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
        np.testing.assert_allclose(getattr(a, field), getattr(c, field), atol=1e-12)
    assert a.provenance == b.provenance
    # pooling is a weighted mean of the lightness quantile functions
    one = analyze_image_look((small,)).references[0].stats
    two = analyze_image_look((other,)).references[0].stats
    pooled = pool_statistics([one, two], [1, 2])
    np.testing.assert_allclose(pooled.l_quantiles, (one.l_quantiles + 2 * two.l_quantiles) / 3)
    from_fit = json.loads(dict(a.provenance)['pooled_reference'])
    np.testing.assert_allclose(from_fit['l_quantiles'], pooled.l_quantiles, atol=1e-5)
    # Global rescaling of positive weights cannot change the fit or overflow.
    huge = fit_image_look(analyze_image_look((replace(small, weight=5e307), replace(other, weight=1e308))))
    for field in ('tone_y', 'cast', 'band_gain', 'sector_gain'):
        np.testing.assert_allclose(getattr(a, field), getattr(huge, field))
    # Selecting the same photo twice is the same as selecting it once.
    copy = save_reference(tmp_path, rgb, 'copy.png')
    single = fit_image_look(analyze_image_look((small,)))
    twice = fit_image_look(analyze_image_look((small, copy)))
    for field in ('tone_y', 'cast', 'band_gain', 'sector_gain'):
        np.testing.assert_allclose(getattr(single, field), getattr(twice, field), atol=1e-12)


def test_roi_selects_normalized_region(tmp_path):
    warm, cool = fixture_rgb(cast=.04), fixture_rgb(cast=-.04)
    joined = np.concatenate((warm, cool), axis=1)
    left, _ = fit_reference(tmp_path, joined, name='joined.png', roi=(0, 0, .5, 1))
    right = fit_image_look(analyze_image_look((ImageLookReference(tmp_path / 'joined.png', roi=(.5, 0, 1, 1)),)))
    assert left.cast[:, 1].mean() > 0 > right.cast[:, 1].mean()
    exact, _ = fit_reference(tmp_path, warm, name='warm.png')
    for field in ('tone_y', 'cast', 'band_gain', 'sector_gain'):
        np.testing.assert_array_equal(getattr(left, field), getattr(exact, field))
    roi = json.loads(dict(left.provenance)['references'])[0]['roi']
    assert roi == [0, 0, .5, 1]


@pytest.mark.parametrize('kind', ['grey', 'green'])
def test_limited_color_coverage_is_reported_without_hue_edits(tmp_path, kind):
    value = np.linspace(.1, .9, 128 * 128).reshape(128, 128, 1)
    rgb = np.repeat(value, 3, axis=2)
    if kind == 'green':
        rgb *= [.4, 1, .4]
    model, analysis = fit_reference(tmp_path, rgb)
    assert 'limited_color_coverage' in analysis.warnings
    # a grey crop has no colourful pixels; the green one covers a single hue sector
    assert analysis.references[0].color_support <= (0 if kind == 'grey' else 1 / 12 + 1e-9)
    # Hue sectors without colourful pixels in the reference keep a neutral gain.
    if kind == 'grey':
        np.testing.assert_array_equal(model.sector_gain, np.ones(12))
        np.testing.assert_allclose(model.cast, 0, atol=1e-6)
        # a monochrome reference is read as a desaturating look, never as a boost
        assert np.all(model.band_gain < 1)
    else:
        np.testing.assert_array_equal(model.sector_gain[:9], np.ones(9))
        assert np.all(model.sector_gain[9:] < 1)


def test_insufficiency_coverage_clipping(tmp_path):
    rgb = np.linspace(.48, .52, 48).reshape(4, 4, 3)
    model, analysis = fit_reference(tmp_path, rgb)
    assert {'insufficient_samples', 'limited_luminance_coverage', 'limited_color_coverage'} <= set(analysis.warnings)
    assert isinstance(model, ImageLookModel) and np.all(np.diff(model.tone_y) >= 0)
    _, analysis = fit_reference(tmp_path, np.clip(fixture_rgb() * 1.8, 0, 1), name='clipped.png')
    assert 'reference_clipping' in analysis.warnings


@pytest.mark.parametrize('kind', ['color', 'tone', 'saturation'])
def test_conflicts_refuse_unless_explicitly_acknowledged(tmp_path, kind):
    if kind == 'color':
        a, b = fixture_rgb(cast=.05), fixture_rgb(cast=-.05)
    elif kind == 'tone':
        a, b = fixture_rgb() * .5, .5 + fixture_rgb() * .5
    else:
        base = fixture_rgb()
        grey = base.mean(axis=2, keepdims=True)
        a, b = grey + .4 * (base - grey), np.clip(grey + 2 * (base - grey), 0, 1)
    refs = (save_reference(tmp_path, a, 'a.png'), save_reference(tmp_path, b, 'b.png'))
    analysis = analyze_image_look(refs)
    assert f'conflicting_{kind}' in analysis.conflicts
    with pytest.raises(ValidationError, match='conflicting') as exc:
        fit_image_look(analysis)
    assert exc.value.code == 'image_look_conflict'
    model = fit_image_look(analysis, acknowledge_conflicts=True)
    assert 'conflicts_acknowledged' in model.warnings
    assert json.loads(dict(model.provenance)['settings'])['acknowledge_conflicts'] is True
    assert f'conflicting_{kind}' in json.loads(dict(model.provenance)['conflicts'])
    # Conflict checking cannot be bypassed by clearing the public diagnostic list.
    with pytest.raises(ValidationError):
        fit_image_look(replace(analysis, conflicts=()))


@pytest.mark.parametrize('field,value', [
    ('weight', 0), ('weight', -1), ('weight', float('nan')), ('weight', True),
    ('roi', (0, 0, 0, 1)), ('roi', (0, 0, 2, 1)), ('roi', (0, 0, 1)),
    ('roi', 'bad'), ('path', None),
    ('role', 'target'), ('role', 'Base'), ('role', ''), ('role', None), ('role', 1),
])
def test_invalid_references(field, value):
    kwargs = {'path': 'x.png', field: value}
    with pytest.raises(ValidationError):
        ImageLookReference(**kwargs)


def test_reference_roles():
    assert ImageLookReference('x.png').role == 'reference'
    assert ImageLookReference('x.png', role='base').role == 'base'
    with pytest.raises(ValidationError, match="'reference' or 'base'"):
        ImageLookReference('x.png', role='source')


@pytest.mark.parametrize('kwargs', [
    {'seed': -1}, {'seed': True}, {'analysis_size': 15}, {'analysis_size': 257},
    {'max_dimension': 0}, {'tone_amount': 1.1}, {'color_amount': float('nan')},
    {'saturation_amount': '1'}, {'tone_amount': -.1}, {'acknowledge_conflicts': 1},
    {'baseline_version': 'unknown'}, {'baseline_version': 'cps-distribution-v1'},
])
def test_invalid_settings(kwargs):
    with pytest.raises(ValidationError):
        ImageLookSettings(**kwargs)


def test_removed_neutral_protection_setting_is_not_accepted():
    with pytest.raises(TypeError):
        ImageLookSettings(neutral_protection=1.0)
    assert 'neutral_protection' not in ImageLookSettings.__slots__


@pytest.mark.parametrize('rgb', [np.zeros((32, 32, 3)), np.ones((1, 32, 3)) * .5])
@pytest.mark.parametrize('role', ['reference', 'base'])
def test_unusable_is_error_not_identity(tmp_path, rgb, role):
    ref = save_reference(tmp_path, rgb, role=role)
    others = (save_reference(tmp_path, name='other.png'),) if role == 'base' else ()
    with pytest.raises(ValidationError) as exc:
        analyze_image_look((ref, *others))
    assert exc.value.code == 'image_look_unusable'


def test_missing_bad_files_and_source_change(tmp_path, monkeypatch):
    with pytest.raises(StudioError) as exc:
        analyze_image_look((ImageLookReference(tmp_path / 'missing.png'),))
    assert exc.value.code == 'image_look_source_io'
    ok = save_reference(tmp_path, name='ok.png')
    with pytest.raises(StudioError) as exc:
        analyze_image_look((ok, ImageLookReference(tmp_path / 'missing.png', role='base')))
    assert exc.value.code == 'image_look_source_io'
    bad = tmp_path / 'bad.png'
    bad.write_bytes(b'not an image')
    with pytest.raises(StudioError):
        analyze_image_look((ImageLookReference(bad),))
    import camera_profile_studio.image_look as module
    ref = save_reference(tmp_path)
    original = module.load_image
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        ref.path.write_bytes(b'changed')
        return result
    monkeypatch.setattr(module, 'load_image', changed)
    with pytest.raises(ValidationError) as exc:
        analyze_image_look((ref,))
    assert exc.value.code == 'image_look_source_changed'


class Token:
    cancelled = False
    def is_cancelled(self):
        return self.cancelled


def test_progress_cancellation_and_no_success(tmp_path):
    refs = (save_reference(tmp_path), save_reference(tmp_path, name='b.png', role='base'))
    token = Token()
    events = []
    def progress(event):
        events.append(event)
        if event.stage == 'image_look.analyze' and event.fraction >= .5:
            token.cancelled = True
    with pytest.raises(CancelledError):
        analyze_image_look(refs, context=JobContext(progress=progress, cancel=token))
    token.cancelled = False
    analysis = analyze_image_look(refs)
    token.cancelled = True
    with pytest.raises(CancelledError):
        fit_image_look(analysis, context=JobContext(cancel=token))
    model = fit_image_look(analysis)
    with pytest.raises(CancelledError):
        apply_image_look(np.zeros((3, 3)), model, context=JobContext(cancel=token))
    events = []
    context = JobContext(progress=events.append)
    analyze_image_look(refs, context=context)
    fit_image_look(analysis, context=context)
    apply_image_look(np.zeros((70000, 3)), model, context=context)
    for stage in {event.stage for event in events}:
        fractions = [event.fraction for event in events if event.stage == stage]
        assert fractions == sorted(fractions)
        assert fractions[0] == 0 and fractions[-1] == 1


def test_metadata_determinism_and_owned_records(tmp_path):
    model, analysis = fit_reference(tmp_path, fixture_rgb(cast=.03))
    again = fit_image_look(analyze_image_look((analysis.references[0].reference,)))
    assert model.provenance == again.provenance
    metadata = dict(model.provenance)
    assert metadata['engine'] == 'image-look-v2'
    assert metadata['status'] == 'approximate_unverified'
    assert metadata['jpeg_accuracy'] == 'unverified'
    baseline = json.loads(metadata['baseline'])
    assert baseline['version'] == BASELINE_VERSION and baseline['kind'] == 'typical_photo'
    assert json.loads(metadata['settings'])['seed'] == 42
    reference = json.loads(metadata['references'])[0]
    assert reference['sha256'] == hashlib.sha256(analysis.references[0].reference.path.read_bytes()).hexdigest()
    assert reference['weight'] == 1 and reference['roi'] == [0, 0, 1, 1] and reference['role'] == 'reference'
    assert reference['path'] == 'reference.png'
    assert str(tmp_path) not in json.dumps(metadata)
    assert 'icc_normalization' in reference['normalization']
    assert len(json.dumps(metadata)) < 20000
    assert not any(word in json.dumps(metadata).lower() for word in ('delta_e', 'holdout'))
    recorded = json.loads(metadata['model'])
    np.testing.assert_allclose(recorded['tone_y'], model.tone_y, atol=1e-5)
    np.testing.assert_allclose(recorded['cast'], model.cast, atol=1e-5)
    assert json.loads(metadata['pooled_base'])['l_quantiles'] == pytest.approx(
        json.loads(metadata['baseline'])['typical_photo']['l_quantiles'], abs=1e-4)
    with pytest.raises(FrozenInstanceError):
        model.baseline = 'base_images'
    for arr in (model.tone_x, model.tone_y, model.cast, model.band_gain, model.sector_gain,
                analysis.references[0].luminance_quantiles):
        with pytest.raises(ValueError):
            arr.setflags(write=True)
    original = model.tone_y.copy()
    owned = replace(model, tone_y=original)
    original[:] = .1
    np.testing.assert_array_equal(owned.tone_y, model.tone_y)


@pytest.mark.parametrize('size', [17, 33, 65])
@pytest.mark.parametrize('strength', [0., .4, 1.])
def test_baked_strength_preview_export_sampling_agreement(tmp_path, size, strength):
    model, _ = fit_reference(tmp_path, fixture_rgb(cast=.035, tone=.4))
    transform = create_image_look_transform(model)
    try:
        full = generate_lut(transform, size=size)
        lut = generate_lut(transform, size=size, strength=strength)
    finally:
        transform.close()
    axis = np.linspace(0, 1, size, dtype=np.float32)
    identity = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)
    np.testing.assert_allclose(lut.values, (1 - strength) * identity + strength * full.values, atol=1e-7)
    image = ImageData(fixture_rgb(size=16), None)
    preview = render_preview(image, lut)
    np.testing.assert_array_equal(preview.rgb, sample_lut(lut, image.rgb))
    exported = export_cube(lut, tmp_path / 'look.cube')
    loaded = read_cube(exported.cube_path, input_space=SpaceSpec('srgb'), output_space=SpaceSpec('srgb'))
    np.testing.assert_allclose(sample_lut(loaded, image.rgb), preview.rgb, atol=2e-7)
    direct = (1 - strength) * image.rgb + strength * apply_image_look(image.rgb, model)
    # The look is not piecewise linear (gamut mapping, steep dark tones), so a baked LUT agrees with the
    # direct transform on average and within a bounded local error; exact agreement is checked above.
    assert np.mean(np.abs(preview.rgb - direct)) < 2e-3
    np.testing.assert_allclose(preview.rgb, direct, atol=.08)
    metadata = json.loads(exported.metadata_path.read_text())
    assert metadata['strength'] == strength
    assert metadata['provenance']['status'] == 'approximate_unverified'


@pytest.mark.parametrize('bad', [np.array([1, 2]), np.zeros((0, 3)), [[0, float('nan'), 0]], [[0, 2, 0]], [['a', 'b', 'c']]])
def test_invalid_rgb(tmp_path, bad):
    model, _ = fit_reference(tmp_path)
    with pytest.raises(ValidationError):
        apply_image_look(bad, model)


def test_headless_import_has_no_qt():
    command = 'import sys; import camera_profile_studio.image_look; assert not any(x.startswith(("PySide", "PyQt")) for x in sys.modules)'
    result = subprocess.run([sys.executable, '-c', command], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

@pytest.mark.parametrize('bad', [10**400, complex(1, 0), np.complex128(1), np.float64(np.inf)])
def test_nonnumeric_and_overflowing_weights_are_structured_errors(bad):
    with pytest.raises(ValidationError):
        ImageLookReference('x.png', weight=bad)


def test_images_are_released_sequentially(tmp_path, monkeypatch):
    import weakref
    import camera_profile_studio.image_look as module
    original = module.load_image
    held = []
    def checked(*args, **kwargs):
        assert all(reference() is None for reference in held)
        result = original(*args, **kwargs)
        held.append(weakref.ref(result.rgb))
        return result
    monkeypatch.setattr(module, 'load_image', checked)
    refs = tuple(save_reference(tmp_path, name=f'{i}.png', role='base' if i == 2 else 'reference') for i in range(3))
    analysis = analyze_image_look(refs)
    assert len(analysis.references) == 3
    assert all(reference() is None for reference in held)


def test_resize_and_seed_have_bounded_analysis_variation(tmp_path):
    ref = save_reference(tmp_path, fixture_rgb(cast=.025, size=128))
    with Image.open(ref.path) as original:
        original.resize((431, 377), Image.Resampling.NEAREST).save(tmp_path / 'resized.png')
    first = fit_image_look(analyze_image_look((ref,)))
    second = fit_image_look(analyze_image_look((ImageLookReference(tmp_path / 'resized.png'),)))
    another_seed = fit_image_look(analyze_image_look((ref,), settings=ImageLookSettings(seed=55)))
    for other in (second, another_seed):
        np.testing.assert_allclose(first.tone_y, other.tone_y, atol=.01)
        np.testing.assert_allclose(first.cast, other.cast, atol=.002)
        np.testing.assert_allclose(first.band_gain, other.band_gain, atol=.02)
        np.testing.assert_allclose(first.sector_gain, other.sector_gain, atol=.03)
    assert json.loads(dict(another_seed.provenance)['settings'])['seed'] == 55


def test_cancel_on_final_progress_does_not_publish(tmp_path):
    reference = save_reference(tmp_path)
    token = Token()
    def cancel_at_end(event):
        if event.fraction == 1:
            token.cancelled = True
    context = JobContext(cancel=token, progress=cancel_at_end)
    with pytest.raises(CancelledError):
        analyze_image_look((reference,), context=context)
    analysis = analyze_image_look((reference,))
    token.cancelled = False
    with pytest.raises(CancelledError):
        fit_image_look(analysis, context=context)
    model = fit_image_look(analysis)
    token.cancelled = False
    with pytest.raises(CancelledError):
        apply_image_look(np.ones((4, 3)) * .5, model, context=context)


def valid_fields(model):
    return dict(tone_x=model.tone_x.copy(), tone_y=model.tone_y.copy(), cast=model.cast.copy(),
                band_gain=model.band_gain.copy(), sector_gain=model.sector_gain.copy())


def mutated(model, field, index, value):
    arrays = valid_fields(model)
    arrays[field][index] = value
    return {field: arrays[field]}


def test_model_limits_accept_values_on_the_bounds(tmp_path):
    import camera_profile_studio.image_look as module
    model, _ = fit_reference(tmp_path)
    replace(model, cast=np.full((5, 2), module._CAST_LIMIT), band_gain=np.full(5, module._BAND_GAIN[1]),
            sector_gain=np.full(12, module._SECTOR_GAIN[0]))
    replace(model, cast=np.full((5, 2), -module._CAST_LIMIT), band_gain=np.full(5, module._BAND_GAIN[0]),
            sector_gain=np.full(12, module._SECTOR_GAIN[1]))
    replace(model, tone_y=np.zeros(15))
    replace(model, tone_y=np.ones(15))


@pytest.mark.parametrize('change', [
    ('cast', (0, 0), .0801), ('cast', (4, 1), -.0801), ('band_gain', 0, 2.01), ('band_gain', 4, .49),
    ('sector_gain', 0, 1.61), ('sector_gain', 11, .59),
    ('cast', (0, 0), float('nan')), ('band_gain', 1, float('inf')), ('sector_gain', 3, float('nan')),
    ('tone_y', 5, -.01), ('tone_y', 5, 1.01), ('tone_x', 5, 1.01), ('tone_x', 0, .01), ('tone_x', 14, .99),
    ('tone_y', 5, float('nan')),
])
def test_invalid_model_coefficients_rejected(tmp_path, change):
    model, _ = fit_reference(tmp_path)
    field, index, value = change
    with pytest.raises(ValidationError) as exc:
        replace(model, **mutated(model, field, index, value))
    assert exc.value.code == 'image_look_invalid'


def test_model_tone_must_be_monotone_and_shapes_fixed(tmp_path):
    model, _ = fit_reference(tmp_path)
    decreasing = model.tone_y.copy()
    decreasing[7], decreasing[8] = decreasing[8] + .01, decreasing[7]
    stalled_x = model.tone_x.copy()
    stalled_x[8] = stalled_x[7]
    for kwargs in ({'tone_y': decreasing}, {'tone_x': stalled_x}, {'tone_x': model.tone_x[::-1].copy()},
                   {'tone_y': model.tone_y[:-1]}, {'cast': np.zeros((3, 2))}, {'cast': np.zeros((5, 3))},
                   {'band_gain': np.ones(4)}, {'sector_gain': np.ones(6)}, {'baseline': 'other'},
                   {'settings': 'not settings'}, {'cast': np.array([['a', 'b']] * 5)}):
        with pytest.raises(ValidationError):
            replace(model, **kwargs)
    # a plateau (equal neighbouring y values) is a legal tone curve
    flat = model.tone_y.copy()
    flat[7] = flat[8]
    replace(model, tone_y=flat)


def test_opposing_luminance_bands_are_not_silently_averaged(tmp_path):
    rgb = fixture_rgb()
    luma = rgb @ LUMA
    cast = (.09 * (1 - 2 * luma))[..., None] * np.array([1, 0, -1])
    a = save_reference(tmp_path, np.clip(rgb + cast, 0, 1), 'a.png')
    b = save_reference(tmp_path, np.clip(rgb - cast, 0, 1), 'b.png')
    analysis = analyze_image_look((a, b))
    assert 'conflicting_color' in analysis.conflicts
    with pytest.raises(ValidationError):
        fit_image_look(analysis)


def test_output_clipping_warning_from_safety_grid(tmp_path):
    # A strongly saturated look applied to a flat-colour base pushes colours outside sRGB.
    muted = .5 + .25 * (fixture_rgb() - .5)
    vivid = np.clip(.5 + 2.2 * (fixture_rgb() - .5), 0, 1)
    model, _ = fit_with_base(tmp_path, vivid, muted)
    assert 'output_clipping' in model.warnings
    disabled, _ = fit_with_base(tmp_path, vivid, muted, ImageLookSettings(**ZERO))
    assert 'output_clipping' not in disabled.warnings


def bright_ramp_references(tmp_path):
    ramp = np.broadcast_to(np.linspace(.2, 1., 256)[None, :, None], (64, 256, 3))
    return tuple(save_reference(tmp_path, ramp, f'bright-{i}.png') for i in range(10))


def test_ten_real_grayscale_ramps_pool_like_one(tmp_path):
    references = bright_ramp_references(tmp_path)
    assert len({ref.path for ref in references}) == 10
    analysis = analyze_image_look(references)
    assert not analysis.conflicts
    individual = [fit_image_look(replace(analysis, references=(record,))) for record in analysis.references]
    model = fit_image_look(analysis)
    # Ten identical references are one reference: the pooled fit stays strictly inside the model bounds.
    assert np.max(np.abs(deviation(model))) > .01
    for field in ('tone_x', 'tone_y', 'cast', 'band_gain', 'sector_gain'):
        np.testing.assert_allclose(getattr(model, field), getattr(individual[0], field), atol=1e-15)
    recorded = json.loads(dict(model.provenance)['model'])
    assert recorded['tone_y'] == np.round(model.tone_y, 5).tolist()
    assert len(json.loads(dict(model.provenance)['references'])) == 10


@pytest.mark.parametrize('as_path', [False, True])
def test_nul_reference_direct_engine_is_structured_error(as_path):
    from pathlib import Path
    path = Path('nul\0.png') if as_path else 'nul\0.png'
    with pytest.raises(ValidationError) as exc:
        analyze_image_look((ImageLookReference(path),))
    assert exc.value.code == 'image_look_invalid'


# ---------------------------------------------------------------------------
# Base images (role='base')
# ---------------------------------------------------------------------------
def test_reference_identical_to_the_base_is_identity(tmp_path):
    rgb = fixture_rgb(cast=.02, tone=.2)
    model, analysis = fit_with_base(tmp_path, rgb, rgb)
    assert model.baseline == 'base_images' and analysis.base_count == 1
    assert 'typical_photo_baseline' not in model.warnings and 'typical_photo_baseline' not in analysis.warnings
    points = grid(17)
    np.testing.assert_allclose(apply_image_look(points, model), points, atol=1e-3)
    assert np.max(np.abs(deviation(model))) < 1e-9
    # also for the full set of default amounts and a clipped (piled-up) base
    clipped = np.clip(fixture_rgb() * 1.6 - .1, 0, 1)
    model, _ = fit_with_base(tmp_path, clipped, clipped)
    np.testing.assert_allclose(apply_image_look(points, model), points, atol=1e-3)


def test_brightened_reference_with_the_original_as_base_brightens(tmp_path):
    base = fixture_rgb()
    bright = base ** .6
    model, analysis = fit_with_base(tmp_path, bright, base)
    result = apply_image_look(base, model)
    assert (result @ LUMA).mean() > (base @ LUMA).mean() + .05
    # a monotone point transform is recovered pixel by pixel, not only on average
    assert np.abs(result - bright).mean() < .5 * np.abs(base - bright).mean()
    darkened, _ = fit_with_base(tmp_path, base ** 1.6, base)
    assert (apply_image_look(base, darkened) @ LUMA).mean() < (base @ LUMA).mean() - .05
    # the same reference without a base is judged against typical photographs, not against these ones
    alone, _ = fit_reference(tmp_path, bright, name='alone.png')
    assert alone.baseline == 'typical_photo'
    assert np.max(np.abs(deviation(alone))) < np.max(np.abs(deviation(model)))


def test_base_color_cast_and_saturation_are_measured_against_the_base(tmp_path):
    base = fixture_rgb(cast=.03)
    model, _ = fit_with_base(tmp_path, fixture_rgb(cast=.03), base)
    assert np.max(np.abs(model.cast)) < 1e-9          # same cast on both sides: nothing to add
    warmer, _ = fit_with_base(tmp_path, fixture_rgb(cast=.06), base)
    assert warmer.cast[:, 1].mean() > .005
    grey = base.mean(axis=2, keepdims=True)
    vivid, _ = fit_with_base(tmp_path, np.clip(grey + 1.5 * (base - grey), 0, 1), base)
    assert vivid.band_gain.mean() > 1.1 and vivid.sector_gain.shape == (12,)
    dull, _ = fit_with_base(tmp_path, grey + .5 * (base - grey), base)
    assert dull.band_gain.mean() < .9


def test_mixed_roles_are_pooled_separately(tmp_path):
    ref_a, ref_b = fixture_rgb(tone=.5), fixture_rgb(cast=.03, tone=.3)
    base_c, base_d = fixture_rgb(), fixture_rgb(tone=-.4)
    records = (save_reference(tmp_path, ref_a, 'a.png'), save_reference(tmp_path, ref_b, 'b.png', weight=3),
               save_reference(tmp_path, base_c, 'c.png', role='base'), save_reference(tmp_path, base_d, 'd.png', role='base', weight=2))
    analysis = analyze_image_look(records)
    assert analysis.base_count == 2 and len(analysis.references) == 4
    assert 'typical_photo_baseline' not in analysis.warnings
    model = fit_image_look(analysis)
    shuffled = fit_image_look(analyze_image_look(records[::-1]))
    assert model.provenance == shuffled.provenance
    stats = {r.reference.path.name: r.stats for r in analysis.references}
    metadata = dict(model.provenance)
    pooled_ref = pool_statistics([stats['a.png'], stats['b.png']], [1 / 3, 1])
    pooled_base = pool_statistics([stats['c.png'], stats['d.png']], [.5, 1])
    np.testing.assert_allclose(json.loads(metadata['pooled_reference'])['l_quantiles'], pooled_ref.l_quantiles, atol=1e-5)
    np.testing.assert_allclose(json.loads(metadata['pooled_base'])['l_quantiles'], pooled_base.l_quantiles, atol=1e-5)
    # The tone curve is exactly reference pool vs base pool; base photos never enter the reference pool.
    assert not np.allclose(pooled_ref.l_quantiles, pooled_base.l_quantiles, atol=.02)
    inside = model.tone_x[1:-1]
    np.testing.assert_allclose(inside, np.maximum.accumulate(pooled_base.l_quantiles), atol=0.015)
    assert json.loads(metadata['baseline'])['kind'] == 'base_images'
    assert [r['role'] for r in json.loads(metadata['references'])].count('base') == 2
    # Roles swapped: the opposite look.
    swapped = fit_image_look(analyze_image_look((replace(records[2], role='reference'), replace(records[3], role='reference'),
                                                 replace(records[0], role='base'), replace(records[1], role='base')),
                                                settings=ImageLookSettings(acknowledge_conflicts=True)))
    assert np.sign(deviation(model)[1:-1].mean()) == -np.sign(deviation(swapped)[1:-1].mean())


def test_base_images_never_create_conflicts(tmp_path):
    reference = save_reference(tmp_path, fixture_rgb(tone=.2), 'ref.png')
    dark = save_reference(tmp_path, fixture_rgb() * .4, 'dark.png', role='base')
    light = save_reference(tmp_path, .6 + fixture_rgb() * .4, 'light.png', role='base')
    analysis = analyze_image_look((reference, dark, light))
    assert analysis.conflicts == () and analysis.base_count == 2
    fit_image_look(analysis)
    # the same two photos as references do conflict
    conflicting = analyze_image_look((replace(dark, role='reference'), replace(light, role='reference')))
    assert 'conflicting_tone' in conflicting.conflicts


def test_base_only_selection_is_refused(tmp_path):
    with pytest.raises(ValidationError, match='references'):
        analyze_image_look((save_reference(tmp_path, role='base'),))
    analysis = analyze_image_look((save_reference(tmp_path, name='r.png'), save_reference(tmp_path, name='b.png', role='base')))
    with pytest.raises(ValidationError):
        fit_image_look(replace(analysis, references=tuple(r for r in analysis.references if r.reference.role == 'base')))


def test_at_most_64_images_per_role(tmp_path):
    tiny = np.linspace(.1, .9, 16 * 16 * 3).reshape(16, 16, 3)
    reference = save_reference(tmp_path, tiny, 'tiny-ref.png')
    base = save_reference(tmp_path, tiny[::-1] * .9, 'tiny-base.png', role='base')
    settings = ImageLookSettings(analysis_size=16, acknowledge_conflicts=True)
    analysis = analyze_image_look((reference,) * 64 + (base,) * 64, settings=settings)
    assert len(analysis.references) == 128 and analysis.base_count == 64
    too_many_bases = (reference,) + (base,) * 65
    with pytest.raises(ValidationError, match='at most 64 base'):
        analyze_image_look(too_many_bases, settings=settings)
    with pytest.raises(ValidationError, match='between 1 and 64'):
        analyze_image_look((reference,) * 65 + (base,), settings=settings)
    with pytest.raises(ValidationError):
        analyze_image_look((reference,) * 65 + (base,) * 64, settings=settings)
    record = next(r for r in analysis.references if r.reference.role == 'reference')
    with pytest.raises(ValidationError):
        replace(analysis, references=(record,) * 65)
    with pytest.raises(ValidationError):
        replace(analysis, references=tuple(r for r in analysis.references if r.reference.role == 'base'))


def test_base_count_and_warnings_in_the_analysis(tmp_path):
    only = analyze_image_look((save_reference(tmp_path),))
    assert only.base_count == 0 and 'typical_photo_baseline' in only.warnings
    both = analyze_image_look((save_reference(tmp_path), save_reference(tmp_path, name='b.png', role='base'),
                               save_reference(tmp_path, name='c.png', role='base', weight=2)))
    assert both.base_count == 2 and 'typical_photo_baseline' not in both.warnings
    # warnings of base photos (for example clipping) are not attributed to the look
    clipped = save_reference(tmp_path, np.clip(fixture_rgb() * 1.8, 0, 1), 'clipped.png', role='base')
    mixed = analyze_image_look((save_reference(tmp_path), clipped))
    assert 'reference_clipping' not in mixed.warnings
    assert all(isinstance(r.reference, ImageLookReference) for r in mixed.references)
    assert [r.reference.role for r in mixed.references] == sorted(r.reference.role for r in mixed.references)
