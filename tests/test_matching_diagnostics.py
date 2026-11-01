"""Quality budgets, publication cancellation, and exported matching provenance."""
import json
from dataclasses import replace

import numpy as np
import pytest

from camera_profile_studio.errors import CancelledError, ValidationError
from camera_profile_studio.matching import apply_match, create_match_transform, fit_match, sample_image_pair
from camera_profile_studio.types import ImageData, JobContext, MatchSettings, SamplePairs


def fixture(n=900):
    x = np.random.default_rng(390).uniform(0.04, 0.95, (n, 3))
    y = np.clip(x ** [1.2, 0.85, 1.12] + 0.04 * x[:, [1, 2, 0]] * (1 - x), 0, 1)
    return SamplePairs(x, y, tuple(f'patch:{i // 9}' for i in range(n)), 'Synthetic channel-power and cross-channel target')


@pytest.mark.parametrize('grid_size', [5, 9, 17])
def test_all_exposed_residual_grid_settings_work(grid_size):
    model = fit_match(fixture(), settings=MatchSettings(grid_size=grid_size))
    assert model.residual.shape == (grid_size,) * 3 + (3,)
    assert np.isfinite(model.residual).all()
    assert model.report.after.mean < model.report.before.mean * 0.15
    assert np.max(np.abs(model.residual)) > 0.0001


def test_standard_24_patch_chart_has_six_held_out_patches():
    samples = fixture(24)
    samples = replace(samples, groups=tuple(f'patch:{i}' for i in range(24)))
    model = fit_match(samples, settings=MatchSettings(grid_size=5))
    assert model.report.after.count == 6
    assert len(model.report.train_indices) == 18
    assert np.isfinite(model.residual).all()
    assert 'Sparse gamut coverage' in ' '.join(model.report.warnings)


def test_final_progress_cancellation_never_returns_success():
    class Token:
        cancelled = False

        def is_cancelled(self):
            return self.cancelled

    token = Token()

    def cancel_at_publication(p):
        if p.stage == 'fit' and p.fraction == 1:
            token.cancelled = True

    with pytest.raises(CancelledError):
        fit_match(fixture(90), settings=MatchSettings(fit='ccm'),
                  context=JobContext(progress=cancel_at_publication, cancel=token))


def test_sample_and_apply_cancellation_on_progress():
    class Token:
        cancelled = False

        def is_cancelled(self):
            return self.cancelled

    token = Token()

    def cancel(_):
        token.cancelled = True

    image = ImageData(np.random.default_rng(5).random((128, 128, 3)), None)
    with pytest.raises(CancelledError):
        sample_image_pair(image, image, same_scene_confirmed=True,
                          context=JobContext(progress=cancel, cancel=token))
    token.cancelled = False
    model = fit_match(fixture(90), settings=MatchSettings(fit='ccm'))
    with pytest.raises(CancelledError):
        apply_match(image.rgb, model, context=JobContext(progress=cancel, cancel=token))


def test_export_carries_evidence_settings_counts_and_limitations(tmp_path):
    from camera_profile_studio.lut import export_cube, generate_lut
    model = fit_match(fixture(180), settings=MatchSettings(grid_size=5, seed=76))
    transform = create_match_transform(model)
    lut = generate_lut(transform, size=17)
    provenance = dict(lut.provenance)
    assert provenance['evidence'] == model.evidence
    assert provenance['jpeg_accuracy'] == 'unverified'
    assert len(provenance['model_sha256']) == 64
    diagnostics = json.loads(provenance['diagnostics'])
    assert diagnostics['train_count'] == len(model.report.train_indices)
    assert diagnostics['holdout_count'] == model.report.after.count
    assert 'seed=76' in ' '.join(diagnostics['warnings_and_settings'])
    result = export_cube(lut, tmp_path / 'experimental_match.cube')
    metadata = json.loads(result.metadata_path.read_text(encoding='utf-8'))
    assert 'Synthetic channel-power' in json.dumps(metadata)
    assert 'independent' in json.dumps(metadata)


def test_corrupted_monotone_curve_is_not_silently_applied():
    model = fit_match(fixture(90), settings=MatchSettings(fit='ccm'))
    curve = model.tone_curve.copy()
    curve[20, 1] = 0.99
    with pytest.raises(ValidationError, match='monotone'):
        apply_match(np.array([[0.1, 0.2, 0.3]]), replace(model, tone_curve=curve))


def test_independent_delta_e00_metrics():
    from colour import XYZ_to_Lab
    from colour.difference import delta_E_CIE2000
    samples = fixture(180)
    model = fit_match(samples, settings=MatchSettings(grid_size=5))
    holdout = model.report.holdout_indices
    prediction = apply_match(samples.source_rgb[holdout], model)
    target = samples.target_rgb[holdout]
    # Independent reference uses colour's Lab and CIEDE2000, not engine helpers.
    def lab(rgb):
        rgb = rgb.astype('float64')
        linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        matrix = np.array([[0.4124564, 0.3575761, 0.1804375],
                           [0.2126729, 0.7151522, 0.0721750],
                           [0.0193339, 0.1191920, 0.9503041]])
        white = matrix.sum(axis=1)
        xy = white[:2] / white.sum()
        return XYZ_to_Lab(linear @ matrix.T, illuminant=xy)
    delta = delta_E_CIE2000(lab(prediction), lab(target))
    assert model.report.after.mean == pytest.approx(float(delta.mean()), abs=2e-5)
    assert model.report.after.p95 == pytest.approx(float(np.percentile(delta, 95)), abs=2e-5)
    assert model.report.after.maximum == pytest.approx(float(delta.max()), abs=2e-5)
