"""Independent synthetic references; these are not camera-accuracy evidence."""
from dataclasses import replace

import cv2
import numpy as np
import pytest

from camera_profile_studio.errors import CancelledError, ValidationError
from camera_profile_studio.matching import (
    align_pair, apply_match, create_match_transform, fit_match, sample_image_pair,
)
from camera_profile_studio.types import ImageData, JobContext, MatchSettings, SamplePairs, SpaceSpec


def decode(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def encode(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)


CCM = np.array([[0.86, 0.08, 0.03], [0.035, 0.90, 0.025], [0.02, 0.08, 0.84]])


def references(n=1200, nonlinear=False):
    x = np.random.default_rng(124).uniform(0.025, 0.96, (n, 3))
    y = encode(decode(x) @ CCM.T)
    if nonlinear:
        y = y ** np.array([1.18, 0.88, 1.08])
        y = np.clip(y + 0.07 * np.column_stack([
            np.sin(np.pi * y[:, 1]) * y[:, 0] * (1 - y[:, 0]),
            y[:, 0] * y[:, 2] * (1 - y[:, 1]),
            -np.sin(np.pi * y[:, 0]) * y[:, 2] * (1 - y[:, 2]),
        ]), 0, 1)
    return SamplePairs(x, y, tuple(f'chart:{i // 12}' for i in range(n)), 'Independent synthetic corresponding RGB patches')


def test_known_linear_ccm_direction_and_holdout():
    samples = references()
    model = fit_match(samples, settings=MatchSettings(fit='ccm', regularization=0.001))
    np.testing.assert_allclose(model.ccm, CCM, atol=0.0001)
    assert model.ccm.dtype == np.float64
    assert model.tone_curve.shape[1] == 4
    assert model.residual.dtype == np.float32
    assert model.report.after.mean < 0.005
    assert model.report.after.mean < model.report.before.mean / 100
    np.testing.assert_allclose(apply_match(samples.source_rgb, model), samples.target_rgb, atol=0.0001)
    assert model.report.before.count == len(model.report.holdout_indices)
    assert model.report.after.maximum >= model.report.after.p95 >= model.report.after.median
    assert not model.ccm.flags.writeable
    assert not model.report.train_indices.flags.writeable
    assert not model.residual.flags.writeable
    assert 'independent' in ' '.join(model.report.warnings)


def test_nonlinear_tone_and_residual_fit_are_real():
    samples = references(1800, nonlinear=True)
    base = fit_match(samples, settings=MatchSettings(fit='ccm_tone', grid_size=5))
    model = fit_match(samples, settings=MatchSettings(grid_size=5))
    assert np.max(np.abs(model.residual)) > 0.001
    assert np.max(np.abs(model.tone_curve[:, 1] - model.tone_curve[:, 0])) > 0.02
    assert np.all(np.diff(model.tone_curve[:, 1:], axis=0) >= 0)
    assert model.report.after.mean < model.report.before.mean / 10
    assert model.report.after.mean < base.report.after.mean
    independent = np.random.default_rng(74).uniform(0.025, 0.95, (300, 3))
    prediction = apply_match(independent, model)
    assert prediction.dtype == np.float32 and not prediction.flags.writeable
    assert np.isfinite(prediction).all()


def test_group_split_reproducibility_and_no_validation_leakage():
    samples = references(600, nonlinear=True)
    settings = MatchSettings(grid_size=5)
    first = fit_match(samples, settings=settings)
    second = fit_match(samples, settings=settings)
    np.testing.assert_array_equal(first.ccm, second.ccm)
    np.testing.assert_array_equal(first.residual, second.residual)
    train, held = first.report.train_indices, first.report.holdout_indices
    assert not set(samples.groups[i] for i in train) & set(samples.groups[i] for i in held)
    changed = samples.target_rgb.copy()
    changed[held] = 1 - changed[held]
    altered = fit_match(replace(samples, target_rgb=changed), settings=settings)
    np.testing.assert_array_equal(first.ccm, altered.ccm)
    np.testing.assert_array_equal(first.tone_curve, altered.tone_curve)
    np.testing.assert_array_equal(first.residual, altered.residual)
    assert altered.report.after.mean > first.report.after.mean + 10
    different = fit_match(samples, settings=replace(settings, seed=43))
    assert not np.array_equal(train, different.report.train_indices)


def test_split_happens_before_rank_check():
    samples = references(60)
    initial = fit_match(samples, settings=MatchSettings(fit='ccm'))
    altered = samples.source_rgb.copy()
    altered[initial.report.holdout_indices] = 0.5
    # A rank-deficient HOLDOUT is allowed, because only training estimates the CCM.
    result = fit_match(replace(samples, source_rgb=altered), settings=MatchSettings(fit='ccm'))
    np.testing.assert_array_equal(initial.ccm, result.ccm)


@pytest.mark.parametrize('kind', ['one_group', 'small_holdout', 'rank_source', 'rank_target', 'duplicate_colors'])
def test_invalid_training_rejected(kind):
    samples = references(24)
    if kind == 'one_group':
        samples = replace(samples, groups=('single',) * 24)
    elif kind == 'small_holdout':
        samples = replace(samples, groups=('a',) * 23 + ('b',))
    elif kind == 'rank_source':
        source = np.tile(np.linspace(0.1, 0.9, 24)[:, None], (1, 3))
        samples = replace(samples, source_rgb=source)
    elif kind == 'rank_target':
        samples = replace(samples, target_rgb=np.full((24, 3), 0.4))
    else:
        samples = replace(samples, source_rgb=np.tile(np.eye(3), (8, 1)))
    with pytest.raises(ValidationError):
        fit_match(samples)


@pytest.mark.parametrize('value', [np.nan, np.inf, -0.1, 1.1])
def test_nonfinite_and_out_of_range_rejected(value):
    x = np.zeros((24, 3))
    x[0, 0] = value
    with pytest.raises(ValidationError):
        fit_match(SamplePairs(x, np.zeros_like(x), tuple(str(i) for i in range(24)), 'patches'))


@pytest.mark.parametrize('kwargs', [dict(grid_size=7), dict(regularization=0), dict(regularization=np.nan),
                                    dict(holdout_fraction=0), dict(holdout_fraction=1), dict(seed=-1)])
def test_invalid_settings(kwargs):
    with pytest.raises(ValidationError):
        fit_match(references(60), settings=MatchSettings(**kwargs))


def textured_image(seed=73):
    rng = np.random.default_rng(seed)
    image = np.full((320, 400, 3), 0.12, dtype='float32')
    for _ in range(180):
        center = tuple(rng.integers([8, 8], [392, 312]).tolist())
        color = tuple(rng.uniform(0.15, 0.95, 3).tolist())
        cv2.circle(image, center, int(rng.integers(3, 13)), color, -1)
    return image


def test_actual_registration_warp_mask_and_corresponding_samples():
    source = textured_image()
    homography = np.array([[1, 0.015, 16], [-0.01, 1, 10], [0.000015, 0.00001, 1.]])
    target = cv2.warpPerspective(source, homography, (400, 320))
    target = np.clip(target * np.array([0.95, 0.92, 0.97]) + 0.01, 0, 1)
    result = align_pair(ImageData(source, None), ImageData(target, None), same_scene_confirmed=True)
    assert result.inlier_fraction > 0.6
    assert 0.6 < result.valid_mask.mean() < 0.99
    assert result.homography.dtype == np.float64 and not result.homography.flags.writeable
    points = np.float32([[[50, 50], [350, 50], [350, 270], [50, 270]]])
    np.testing.assert_allclose(cv2.perspectiveTransform(points, result.homography),
                               cv2.perspectiveTransform(points, homography), atol=1.5)
    expected = np.clip(result.source_aligned * np.array([0.95, 0.92, 0.97]) + 0.01, 0, 1)
    assert np.mean(np.abs(expected[result.valid_mask] - target[result.valid_mask])) < 0.015
    samples = sample_image_pair(ImageData(result.source_aligned, None, (('raw_recipe', 'fixed camera WB'),)),
                                ImageData(result.target_rgb, None), same_scene_confirmed=True,
                                valid_mask=result.valid_mask, max_samples=1000)
    assert len(samples.groups) == 1000
    assert len(set(samples.groups)) > 20
    assert 'raw_recipe' in samples.evidence
    assert 'fixed camera WB' in samples.evidence
    model = fit_match(samples, settings=MatchSettings(grid_size=5))
    assert model.report.after.mean < model.report.before.mean


def test_unrelated_pairs_and_failed_registration_reject():
    first, second = ImageData(textured_image(1), None), ImageData(textured_image(2), None)
    for operation in (align_pair, sample_image_pair):
        with pytest.raises(ValidationError, match='confirmation'):
            operation(first, second)
    with pytest.raises(ValidationError) as error:
        align_pair(first, second, same_scene_confirmed=True)
    assert error.value.code == 'alignment_failed'
    blank = ImageData(np.full((100, 100, 3), 0.5), None)
    with pytest.raises(ValidationError, match='features'):
        align_pair(blank, blank, same_scene_confirmed=True)


def test_sampling_validates_geometry_mask_and_cap():
    first = ImageData(textured_image(), None)
    with pytest.raises(ValidationError, match='geometry'):
        sample_image_pair(first, ImageData(first.rgb[:100], None), same_scene_confirmed=True)
    with pytest.raises(ValidationError, match='boolean'):
        sample_image_pair(first, first, same_scene_confirmed=True, valid_mask=np.ones((320, 400)))
    with pytest.raises(ValidationError):
        sample_image_pair(first, first, same_scene_confirmed=True, max_samples=20001)
    mask = np.zeros((320, 400), bool)
    mask[40:250, 60:280] = True
    one = sample_image_pair(first, first, same_scene_confirmed=True, valid_mask=mask, max_samples=500)
    two = sample_image_pair(first, first, same_scene_confirmed=True, valid_mask=mask, max_samples=500)
    np.testing.assert_array_equal(one.source_rgb, two.source_rgb)
    assert one.groups == two.groups
    assert all(not group.startswith('block:0:') for group in one.groups)


def test_fit_large_input_is_bounded_and_report_indices_remain_original():
    samples = references(24000)
    model = fit_match(samples, settings=MatchSettings(fit='ccm'))
    assert len(model.report.train_indices) + len(model.report.holdout_indices) <= 20000
    assert model.report.holdout_indices.max() < 24000
    assert '20000' in ' '.join(model.report.warnings)
    np.testing.assert_allclose(model.ccm, CCM, atol=0.001)


class Token:
    cancelled = False

    def is_cancelled(self):
        return self.cancelled


def test_cancellation_at_entry_and_during_fit_no_model_return():
    token = Token()
    token.cancelled = True
    with pytest.raises(CancelledError):
        fit_match(references(), context=JobContext(cancel=token))
    token.cancelled = False
    progress = []

    def on_progress(p):
        progress.append(p)
        if p.stage == 'fit_residual':
            token.cancelled = True

    with pytest.raises(CancelledError):
        fit_match(references(600, True), settings=MatchSettings(grid_size=5),
                  context=JobContext(progress=on_progress, cancel=token))
    assert any(p.stage == 'fit_residual' for p in progress)
    assert not any(p.stage == 'fit' and p.fraction == 1 for p in progress)


def test_progress_is_monotone_per_stage_and_input_is_unchanged():
    samples = references(120, True)
    before = samples.source_rgb.copy()
    progress = []
    model = fit_match(samples, settings=MatchSettings(grid_size=5), context=JobContext(progress=progress.append))
    stages = {}
    for p in progress:
        assert 0 <= p.fraction <= 1
        assert p.fraction >= stages.get(p.stage, 0)
        stages[p.stage] = p.fraction
    assert stages['fit'] == 1
    np.testing.assert_array_equal(samples.source_rgb, before)
    assert apply_match(np.array([0.2, 0.3, 0.4]), model).shape == (3,)


def test_model_validation_rejects_singular_matrix():
    model = fit_match(references(60), settings=MatchSettings(fit='ccm'))
    with pytest.raises(ValidationError, match='nonsingular'):
        apply_match(np.ones((1, 3)), replace(model, ccm=np.zeros((3, 3))))


@pytest.mark.parametrize('size', [17, 33, 65])
def test_matching_shared_lut_export_and_preview_contract(size, tmp_path):
    from camera_profile_studio.lut import export_cube, generate_lut, read_cube, sample_lut
    samples = references(600, nonlinear=True)
    model = fit_match(samples, settings=MatchSettings(grid_size=5))
    transform = create_match_transform(model)
    assert transform.input_space == transform.output_space == SpaceSpec('srgb')
    try:
        lut = generate_lut(transform, size=size)
        assert lut.values.shape == (size, size, size, 3)
        assert np.isfinite(lut.values).all()
        result = export_cube(lut, tmp_path / f'match_{size}.cube')
        assert result.validation.valid and result.validation.sample_count == size**3
        imported = read_cube(result.cube_path, input_space=SpaceSpec('srgb'), output_space=SpaceSpec('srgb'))
        rendered = sample_lut(lut, samples.source_rgb)
        np.testing.assert_allclose(sample_lut(imported, samples.source_rgb), rendered, atol=2e-6)
        assert np.mean(np.abs(rendered - transform.apply(samples.source_rgb))) < 0.003
    finally:
        transform.close()
