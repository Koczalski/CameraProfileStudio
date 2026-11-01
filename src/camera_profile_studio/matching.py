"""Supervised, experimental sRGB camera matching with group-held-out evaluation.

Geometry is evidence of alignment, not proof of identical scene illumination or
camera accuracy. Image decoding and all deliverable LUT I/O belong to core.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np
from scipy import optimize, sparse
from scipy.sparse.linalg import cg

from .color import delta_e00, srgb_decode, srgb_to_lab
from .errors import CancelledError, ImageError, ValidationError, checkpoint as _core_checkpoint
from .types import (
    AlignmentResult, ErrorStats, ImageData, JobContext, Mask, MatchModel,
    MatchReport, MatchSettings, RGB, RGBTransform, SamplePairs, SpaceSpec,
)

_MAX_SAMPLES = 20000
_CHUNK = 8192
_REGISTRATION_LOCK = threading.Lock()
_LIMITATION = (
    'Experimental developed-sRGB match, not a raw sensor/profile-space transform. '
    'Group-held-out patches/spatial blocks are internal validation, not independent '
    'cross-scene camera accuracy or guaranteed OEM JPEG reproduction.'
)


def _checkpoint(context: JobContext | None, *, stage: str, fraction: float, message: str = '') -> None:
    _core_checkpoint(context, stage=stage, fraction=fraction, message=message)
    # A UI callback may cancel synchronously at the final progress publication.
    if context is not None and context.cancel is not None and context.cancel.is_cancelled():
        raise CancelledError('Operation cancelled.', code='cancelled')


def _fail(message: str, code: str = 'invalid_match') -> None:
    raise ValidationError(message, code=code)


def _rgb(value: RGB, *, image: bool = False) -> np.ndarray:
    a = np.asarray(value)
    if (a.ndim < 1 or a.shape[-1] != 3 or a.size == 0
            or (image and a.ndim != 3) or a.dtype.kind not in 'fiu'):
        _fail('Expected nonempty numeric RGB data with last dimension 3.')
    if not np.isfinite(a).all() or np.any(a < 0) or np.any(a > 1):
        _fail('Matching requires finite normalized encoded sRGB in [0,1].', 'invalid_rgb')
    return a


def _readonly(value: np.ndarray, dtype=np.float32) -> np.ndarray:
    a = np.array(value, dtype=dtype, order='C', copy=True)
    a.setflags(write=False)
    return a


def _confirmed(value: bool) -> None:
    if value is not True:
        _fail('Pixel correspondence requires explicit same-scene confirmation; '
              'use measured corresponding patches for unrelated references.', 'missing_correspondence')


def align_pair(source: ImageData, target: ImageData, *,
               same_scene_confirmed: bool = False,
               context: JobContext | None = None) -> AlignmentResult:
    """Register source to target coordinates using bounded ORB and RANSAC.

    Reject weak, localized, degenerate and excessive-perspective fits. A single
    homography cannot solve parallax, subject motion or non-planar geometry.
    """
    _checkpoint(context, stage='align', fraction=0)
    _confirmed(same_scene_confirmed)
    src, dst = _rgb(source.rgb, image=True), _rgb(target.rgb, image=True)
    import cv2

    def features(image):
        scale = min(1.0, 1500.0 / max(image.shape[:2]))
        small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        gray = np.round(np.clip(small @ np.array([0.2126, 0.7152, 0.0722]), 0, 1) * 255).astype('uint8')
        keypoints, desc = cv2.ORB_create(nfeatures=4000, fastThreshold=12).detectAndCompute(gray, None)
        return keypoints, desc, scale, gray.shape

    try:
        kp1, d1, s1, shape1 = features(src)
        _checkpoint(context, stage='align', fraction=0.2)
        kp2, d2, s2, shape2 = features(dst)
        _checkpoint(context, stage='align', fraction=0.4)
        if d1 is None or d2 is None or min(len(d1), len(d2)) < 16:
            _fail('Registration failed: insufficient textured features.', 'alignment_failed')
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        forward = matcher.knnMatch(d1, d2, k=2)
        backward = matcher.knnMatch(d2, d1, k=2)
        reverse = {p[0].queryIdx: p[0].trainIdx for p in backward
                   if len(p) == 2 and p[0].distance < 0.75 * p[1].distance}
        matches = [p[0] for p in forward if len(p) == 2
                   and p[0].distance < 0.75 * p[1].distance
                   and reverse.get(p[0].trainIdx) == p[0].queryIdx]
        if len(matches) < 16:
            _fail('Registration failed: too few mutually consistent features.', 'alignment_failed')
        p = np.float32([kp1[m.queryIdx].pt for m in matches])
        q = np.float32([kp2[m.trainIdx].pt for m in matches])
        # OpenCV exposes a process/thread RNG rather than a per-call RNG.
        with _REGISTRATION_LOCK:
            cv2.setRNGSeed(42)
            h, inliers = cv2.findHomography(p, q, cv2.RANSAC, 3.0,
                                           maxIters=3000, confidence=0.995)
        _checkpoint(context, stage='align', fraction=0.65)
        if h is None or inliers is None or not np.isfinite(h).all():
            _fail('Registration failed: no finite homography.', 'alignment_failed')
        keep = inliers.ravel().astype(bool)
        fraction = float(keep.mean())
        if keep.sum() < 12 or fraction < 0.5:
            _fail('Registration failed: low RANSAC inlier confidence.', 'alignment_failed')
        for points, shape in ((p[keep], shape1), (q[keep], shape2)):
            coverage = cv2.contourArea(cv2.convexHull(points)) / np.prod(shape)
            if coverage < 0.08:
                _fail('Registration failed: inliers are too spatially concentrated.', 'alignment_failed')
        projected = cv2.perspectiveTransform(p[keep, None, :], h)[:, 0, :]
        if np.median(np.linalg.norm(projected - q[keep], axis=1)) > 2.5:
            _fail('Registration failed: excessive reprojection error.', 'alignment_failed')
        h = np.diag([1 / s2, 1 / s2, 1]) @ h @ np.diag([s1, s1, 1])
        if abs(h[2, 2]) < 1e-10:
            _fail('Registration failed: singular homography.', 'alignment_failed')
        h /= h[2, 2]
        hs, ws = src.shape[:2]
        ht, wt = dst.shape[:2]
        normalized = np.diag([1 / wt, 1 / ht, 1]) @ h @ np.diag([ws, hs, 1])
        corners = np.float32([[0, 0], [ws - 1, 0], [ws - 1, hs - 1], [0, hs - 1]])
        denominators = np.c_[corners, np.ones(4)] @ h[2]
        mapped = cv2.perspectiveTransform(corners[None], h)[0]
        area_ratio = abs(cv2.contourArea(mapped)) / (wt * ht)
        if (np.linalg.cond(normalized) > 1e4 or np.min(denominators) <= 0
                or not np.isfinite(mapped).all() or not cv2.isContourConvex(mapped)
                or not 0.2 <= area_ratio <= 5):
            _fail('Registration failed: implausible or degenerate geometry.', 'alignment_failed')
        warped = cv2.warpPerspective(src.astype('float32'), h, (wt, ht), flags=cv2.INTER_LINEAR)
        mask = cv2.warpPerspective(np.ones((hs, ws), np.uint8), h, (wt, ht),
                                   flags=cv2.INTER_NEAREST).astype(bool)
        mask = cv2.erode(mask.astype('uint8'), np.ones((5, 5), np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
        if mask.mean() < 0.2 or mask.sum() < 64:
            _fail('Registration failed: insufficient valid overlap.', 'alignment_failed')
    except cv2.error as exc:
        raise ImageError('OpenCV registration failed: ' + str(exc), code='alignment_failed') from exc
    _checkpoint(context, stage='align', fraction=1,
               message='Inspect alignment and mask moving/occluded objects before fitting.')
    return AlignmentResult(_readonly(warped), _readonly(dst), _readonly(mask, bool),
                           _readonly(h, np.float64), fraction)


def sample_image_pair(source: ImageData, target: ImageData, *,
                      same_scene_confirmed: bool = False, valid_mask: Mask | None = None,
                      max_samples: int = 20000, seed: int = 42,
                      context: JobContext | None = None) -> SamplePairs:
    """Sample asserted equal-coordinate correspondences in 8x8 spatial groups.

    This function does not infer alignment. Pass align_pair's warped source and
    valid_mask when geometry differs; add a manual occlusion/motion mask as needed.
    """
    _checkpoint(context, stage='sample', fraction=0)
    _confirmed(same_scene_confirmed)
    src, dst = _rgb(source.rgb, image=True), _rgb(target.rgb, image=True)
    if src.shape != dst.shape:
        _fail('Image geometry differs; register first and pass the valid overlap mask.', 'unaligned_pair')
    if isinstance(max_samples, bool) or not isinstance(max_samples, (int, np.integer)) or not 12 <= max_samples <= _MAX_SAMPLES:
        _fail('max_samples must be an integer from 12 to 20000.')
    _seed(seed)
    if valid_mask is not None:
        mask = np.asarray(valid_mask)
        if mask.dtype != np.bool_ or mask.shape != src.shape[:2]:
            _fail('valid_mask must be a boolean HxW mask matching both images.')
    else:
        mask = np.ones(src.shape[:2], dtype=bool)
    # Chunked reservoir priorities avoid an image-sized list of valid indices.
    rng = np.random.default_rng(seed)
    selected = np.empty(0, dtype=np.int64)
    priorities = np.empty(0)
    flat = mask.ravel()
    for start in range(0, flat.size, 65536):
        idx = np.flatnonzero(flat[start:start + 65536]) + start
        selected = np.concatenate((selected, idx))
        priorities = np.concatenate((priorities, rng.random(len(idx))))
        if len(selected) > max_samples:
            take = np.argpartition(priorities, max_samples - 1)[:max_samples]
            selected, priorities = selected[take], priorities[take]
        _checkpoint(context, stage='sample', fraction=min(0.95, (start + 65536) / flat.size * 0.95))
    selected.sort()
    if len(selected) < 12:
        _fail('At least 12 valid corresponding pixels are required.', 'insufficient_samples')
    height, width = src.shape[:2]
    ys, xs = np.divmod(selected, width)
    groups = tuple(f'block:{int(y * 8 // height)}:{int(x * 8 // width)}' for y, x in zip(ys, xs))
    evidence = json.dumps({
        'pairing': 'user-confirmed same scene and equal-coordinate correspondences',
        'mask': 'explicit valid overlap/selection mask' if valid_mask is not None else 'all pixels; alignment asserted by user',
        'source_path': str(source.source_path), 'target_path': str(target.source_path),
        'source_metadata': list(source.metadata), 'target_metadata': list(target.metadata),
        'source_warnings': list(source.warnings), 'target_warnings': list(target.warnings),
        'working_space': 'encoded sRGB, not raw camera-linear', 'sampling_seed': int(seed),
    }, ensure_ascii=True)
    _checkpoint(context, stage='sample', fraction=1)
    return SamplePairs(src.reshape(-1, 3)[selected], dst.reshape(-1, 3)[selected], groups, evidence)


def _seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0 or seed >= 2**64:
        _fail('seed must be an integer in [0, 2**64).')


def _settings(settings: MatchSettings) -> None:
    if settings.fit not in ('ccm', 'ccm_tone', 'ccm_tone_lut'):
        _fail('Unknown matching fit mode.')
    if (isinstance(settings.grid_size, bool) or not isinstance(settings.grid_size, (int, np.integer))
            or settings.grid_size not in (5, 9, 17)):
        _fail('Residual grid_size must be 5, 9 or 17.')
    if not np.isfinite(settings.regularization) or settings.regularization <= 0:
        _fail('regularization must be finite and positive.')
    if not np.isfinite(settings.holdout_fraction) or not 0 < settings.holdout_fraction < 1:
        _fail('holdout_fraction must be strictly between zero and one.')
    _seed(settings.seed)


def _split(samples: SamplePairs, settings: MatchSettings) -> tuple[np.ndarray, np.ndarray, bool]:
    n = len(samples.source_rgb)
    if len(samples.groups) != n or any(not isinstance(g, str) or not g.strip() for g in samples.groups):
        _fail('Every sample requires a nonempty string group identifier.')
    groups = sorted(set(samples.groups))
    if len(groups) < 2:
        _fail('At least two independent group identifiers are required.', 'insufficient_groups')
    rng = np.random.default_rng(settings.seed)
    order = rng.permutation(len(groups))
    counts = Counter(samples.groups)
    cumulative = np.cumsum([counts[groups[i]] for i in order])[:-1]
    feasible = np.flatnonzero((cumulative >= 6) & (n - cumulative >= 6))
    if not len(feasible):
        _fail('The seeded group ordering cannot yield six train and six holdout samples; '
              'add independent groups or change the seed.', 'insufficient_samples')
    # Choose the closest feasible fraction using group cardinalities only, never RGB.
    # In particular, a 24-patch chart needs six rather than five held-out patches.
    count = int(feasible[np.argmin(np.abs(cumulative[feasible] / n - settings.holdout_fraction))]) + 1
    held_groups = {groups[i] for i in order[:count]}
    held = np.fromiter((g in held_groups for g in samples.groups), dtype=bool, count=n)
    train, holdout = np.flatnonzero(~held), np.flatnonzero(held)
    capped = n > _MAX_SAMPLES
    if capped:
        hold_limit = max(6, min(_MAX_SAMPLES - 6, int(_MAX_SAMPLES * settings.holdout_fraction)))
        train_limit = _MAX_SAMPLES - hold_limit
        if len(train) > train_limit:
            train = np.sort(rng.choice(train, train_limit, replace=False))
        if len(holdout) > hold_limit:
            holdout = np.sort(rng.choice(holdout, hold_limit, replace=False))
    if min(len(train), len(holdout)) < 6:
        _fail('The fixed grouped split needs at least six train and six holdout samples; '
              'add groups or change the split settings.', 'insufficient_samples')
    return train, holdout, capped


def _check_rank(rgb: np.ndarray, label: str) -> None:
    if len(np.unique(rgb, axis=0)) < 6 or np.linalg.matrix_rank(rgb) < 3 or np.linalg.cond(rgb) > 1e6:
        _fail(f'{label} training samples lack six distinct colors or stable rank three.', 'rank_deficient')


def _encode(linear: np.ndarray) -> np.ndarray:
    # Keep solver finite differences in float64; the public transfer API is float32.
    value = np.clip(linear, 0, 1)
    return np.where(value <= 0.0031308, 12.92 * value,
                    1.055 * value ** (1 / 2.4) - 0.055)


def _tone(rgb: np.ndarray, curve: np.ndarray) -> np.ndarray:
    return np.column_stack([np.interp(rgb[:, c], curve[:, 0], curve[:, c + 1]) for c in range(3)])


def _weights(rgb: np.ndarray, size: int) -> sparse.csr_matrix:
    pos = np.clip(rgb, 0, 1) * (size - 1)
    base = np.minimum(np.floor(pos).astype(np.int64), size - 2)
    frac = pos - base
    columns, values = [], []
    for r in (0, 1):
        for g in (0, 1):
            for b in (0, 1):
                bit = np.array([r, g, b])
                at = base + bit
                columns.append((at[:, 0] * size + at[:, 1]) * size + at[:, 2])
                values.append(np.prod(np.where(bit, frac, 1 - frac), axis=1))
    return sparse.csr_matrix((np.column_stack(values).ravel(),
                              np.column_stack(columns).ravel(),
                              np.arange(0, len(rgb) * 8 + 1, 8)), shape=(len(rgb), size**3))


def _smoothness(size: int) -> sparse.csr_matrix:
    nodes = np.arange(size**3).reshape(size, size, size)
    left, right = [], []
    for axis in range(3):
        a, b = [slice(None)] * 3, [slice(None)] * 3
        a[axis], b[axis] = slice(None, -1), slice(1, None)
        left.append(nodes[tuple(a)].ravel())
        right.append(nodes[tuple(b)].ravel())
    left, right = np.concatenate(left), np.concatenate(right)
    rows = np.repeat(np.arange(len(left)), 2)
    return sparse.coo_matrix((np.tile([1., -1.], len(left)),
                              (rows, np.column_stack([left, right]).ravel())),
                             shape=(len(left), size**3)).tocsr()


def _fit_base(x: np.ndarray, y: np.ndarray, settings: MatchSettings,
              context: JobContext | None) -> tuple[np.ndarray, np.ndarray]:
    linear = np.asarray(srgb_decode(x), dtype=np.float64)
    target = np.asarray(srgb_decode(y), dtype=np.float64)
    _check_rank(linear, 'Source')
    _check_rank(target, 'Target')
    ridge = settings.regularization * len(x) * 0.001
    ccm = np.linalg.solve(linear.T @ linear + ridge * np.eye(3),
                          linear.T @ target + ridge * np.eye(3)).T
    knots = np.linspace(0, 1, 65)
    curve = np.column_stack([knots] * 4)
    if settings.fit != 'ccm':
        # A train-only bounded nonlinear fit separates channel mixing from tone.
        subset = np.linspace(0, len(x) - 1, min(len(x), 4096)).astype(int)
        xx, yy = linear[subset], y[subset]
        calls = 0

        def objective(parameters):
            nonlocal calls
            calls += 1
            _checkpoint(context, stage='fit_base', fraction=min(0.8, calls / 2000))
            matrix = parameters[:9].reshape(3, 3)
            gamma = parameters[9:]
            predicted = np.maximum(_encode(xx @ matrix.T), 1e-10) ** gamma
            prior = np.r_[matrix.ravel() - np.eye(3).ravel(), gamma - 1]
            return np.r_[(predicted - yy).ravel(),
                         np.sqrt(settings.regularization * len(xx) * 0.001) * prior]

        initial = np.r_[np.clip(ccm.ravel(), -3.9, 3.9), np.ones(3)]
        result = optimize.least_squares(objective, initial,
                                        bounds=(np.r_[np.full(9, -4.), np.full(3, 0.35)],
                                                np.r_[np.full(9, 4.), np.full(3, 3.)]),
                                        max_nfev=80, ftol=1e-7, xtol=1e-7, gtol=1e-7)
        if not result.success or not np.isfinite(result.x).all():
            _fail('CCM/tone optimization did not converge; simplify fit or improve references.', 'fit_failed')
        ccm = result.x[:9].reshape(3, 3)
        encoded = _encode(linear @ ccm.T)
        # Smooth train-only spline refinement, followed by isotonic projection.
        for channel in range(3):
            pos = encoded[:, channel] * (len(knots) - 1)
            lower = np.minimum(pos.astype(int), len(knots) - 2)
            frac = pos - lower
            design = sparse.coo_matrix((np.column_stack([1 - frac, frac]).ravel(),
                                        (np.repeat(np.arange(len(x)), 2),
                                         np.column_stack([lower, lower + 1]).ravel())),
                                       shape=(len(x), len(knots))).toarray()
            prior = knots ** result.x[9 + channel]
            weight = settings.regularization * len(x) / len(knots)
            differences = np.diff(np.eye(len(knots)), n=2, axis=0)
            lhs = design.T @ design + weight * (np.eye(len(knots)) + 4 * differences.T @ differences)
            rhs = design.T @ y[:, channel] + weight * prior
            fitted = np.linalg.solve(lhs, rhs)
            curve[:, channel + 1] = np.clip(optimize.isotonic_regression(fitted).x, 0, 1)
    if not np.isfinite(ccm).all() or np.linalg.cond(ccm) > 1e6:
        _fail('Fitted CCM is singular or numerically unstable.', 'singular_ccm')
    _checkpoint(context, stage='fit_base', fraction=1)
    return ccm, curve


def _fit_residual(x: np.ndarray, y: np.ndarray, settings: MatchSettings,
                  context: JobContext | None) -> np.ndarray:
    size = settings.grid_size
    design = _weights(x, size)
    smooth = _smoothness(size)
    penalty = settings.regularization * len(x) / size**3
    normal = (design.T @ design + penalty * (sparse.eye(size**3) + smooth.T @ smooth)).tocsr()
    rhs = design.T @ (y - x)
    preconditioner = sparse.diags(1 / normal.diagonal())
    result = np.empty((size**3, 3))
    for channel in range(3):
        iterations = 0

        def iteration(_):
            nonlocal iterations
            iterations += 1
            _checkpoint(context, stage='fit_residual', fraction=(channel + min(iterations / 400, 0.99)) / 3)

        result[:, channel], info = cg(normal, rhs[:, channel], M=preconditioner,
                                      rtol=1e-7, atol=1e-10, maxiter=400, callback=iteration)
        if info != 0 or not np.isfinite(result[:, channel]).all():
            _fail('Regularized residual solver did not converge.', 'fit_failed')
        _checkpoint(context, stage='fit_residual', fraction=(channel + 1) / 3)
    return result.reshape(size, size, size, 3).astype('float32')


def _predict(rgb: np.ndarray, ccm: np.ndarray, curve: np.ndarray, residual: np.ndarray) -> np.ndarray:
    linear = np.asarray(srgb_decode(rgb), dtype=np.float64)
    base = _tone(_encode(linear @ ccm.T), curve)
    correction = _weights(base, residual.shape[0]) @ residual.reshape(-1, 3)
    return np.clip(base + correction, 0, 1).astype('float32')


def _stats(first: np.ndarray, second: np.ndarray) -> ErrorStats:
    errors = np.asarray(delta_e00(srgb_to_lab(first), srgb_to_lab(second)))
    if not np.isfinite(errors).all():
        _fail('Nonfinite held-out color differences.', 'fit_failed')
    return ErrorStats(len(errors), float(errors.mean()), float(np.median(errors)),
                      float(np.percentile(errors, 95)), float(errors.max()))


def fit_match(samples: SamplePairs, *, settings: MatchSettings = MatchSettings(),
              context: JobContext | None = None) -> MatchModel:
    _checkpoint(context, stage='fit', fraction=0)
    _settings(settings)
    source, target = _rgb(samples.source_rgb), _rgb(samples.target_rgb)
    if source.ndim != 2 or source.shape != target.shape:
        _fail('Paired matching samples must have identical Nx3 shapes.')
    if not isinstance(samples.evidence, str) or not samples.evidence.strip():
        _fail('Document corresponding patch/image evidence before fitting.', 'missing_reference')
    train, holdout, capped = _split(samples, settings)
    _checkpoint(context, stage='fit', fraction=0.1, message='Groups split before all fitting.')
    x, y = source[train].astype('float64'), target[train].astype('float64')
    ccm, curve = _fit_base(x, y, settings, context)
    _checkpoint(context, stage='fit', fraction=0.5)
    residual = np.zeros((settings.grid_size,) * 3 + (3,), dtype='float32')
    if settings.fit == 'ccm_tone_lut':
        toned = _tone(_encode(np.asarray(srgb_decode(x), dtype=np.float64) @ ccm.T), curve)
        residual = _fit_residual(toned, y, settings, context)
    _checkpoint(context, stage='fit', fraction=0.8)
    predicted = _predict(source[holdout], ccm, curve, residual)
    before, after = _stats(source[holdout], target[holdout]), _stats(predicted, target[holdout])
    warnings = [_LIMITATION,
                f'Train samples: {len(train)}; holdout samples: {len(holdout)}. '
                'Whole-group cardinalities can change the requested holdout fraction. '
                'Holdout never selects parameters or fitting mode. Outputs clip to [0,1].']
    if settings.fit != 'ccm_tone_lut':
        warnings.append('Residual LUT disabled by requested fit mode (zero additive correction).')
    if settings.fit == 'ccm':
        warnings.append('Tone disabled by requested fit mode (identity curve).')
    if capped:
        warnings.append('Deterministic within-split subsampling caps total fitting/evaluation samples at 20000.')
    if len(train) < settings.grid_size**3:
        warnings.append('Sparse gamut coverage: residual grid has more nodes than training samples; '
                        'regularization does not establish accuracy for unobserved colors.')
    if after.mean >= before.mean:
        warnings.append('Held-out mean DeltaE00 did not improve over identity; do not assume this match is useful.')
    warnings.append(f'fit={settings.fit}; regularization={settings.regularization}; '
                    f'grid_size={settings.grid_size}; holdout_fraction={settings.holdout_fraction}; seed={settings.seed}')
    report = MatchReport(_readonly(train, np.int64), _readonly(holdout, np.int64), before, after, tuple(warnings))
    _checkpoint(context, stage='fit', fraction=1)
    return MatchModel(_readonly(ccm, np.float64), _readonly(curve, np.float64),
                      _readonly(residual), report, samples.evidence)


def _validate_model(model: MatchModel) -> None:
    ccm, curve, residual = np.asarray(model.ccm), np.asarray(model.tone_curve), np.asarray(model.residual)
    if ccm.shape != (3, 3) or not np.isfinite(ccm).all() or np.linalg.cond(ccm) > 1e6:
        _fail('Match CCM must be a finite nonsingular 3x3 matrix.', 'singular_ccm')
    if (curve.ndim != 2 or curve.shape[1] != 4 or len(curve) < 2 or not np.isfinite(curve).all()
            or curve[0, 0] != 0 or curve[-1, 0] != 1 or np.any(np.diff(curve[:, 0]) <= 0)
            or np.any(curve[:, 1:] < 0) or np.any(curve[:, 1:] > 1)
            or np.any(np.diff(curve[:, 1:], axis=0) < 0)):
        _fail('Tone curve needs increasing unit-domain x and three monotone bounded channels.')
    if (residual.ndim != 4 or residual.shape[0] not in (5, 9, 17)
            or residual.shape != (residual.shape[0],) * 3 + (3,) or not np.isfinite(residual).all()):
        _fail('Residual must be a finite 5/9/17 cubic RGB additive grid.')


def apply_match(rgb: RGB, model: MatchModel, *, context: JobContext | None = None) -> RGB:
    _checkpoint(context, stage='apply_match', fraction=0)
    data = _rgb(rgb)
    _validate_model(model)
    flat = data.reshape(-1, 3)
    output = np.empty(flat.shape, dtype='float32')
    for start in range(0, len(flat), _CHUNK):
        output[start:start + _CHUNK] = _predict(flat[start:start + _CHUNK], model.ccm,
                                               model.tone_curve, model.residual)
        _checkpoint(context, stage='apply_match', fraction=min(1, (start + _CHUNK) / len(flat)))
    return _readonly(output.reshape(data.shape))


@dataclass(frozen=True, slots=True)
class _MatchTransform:
    model: MatchModel
    input_space: SpaceSpec = SpaceSpec('srgb')
    output_space: SpaceSpec = SpaceSpec('srgb')

    @property
    def provenance(self) -> tuple[tuple[str, str], ...]:
        digest = hashlib.sha256()
        for array in (self.model.ccm, self.model.tone_curve, self.model.residual):
            digest.update(array.tobytes())
        diagnostics = {
            'train_count': len(self.model.report.train_indices),
            'holdout_count': len(self.model.report.holdout_indices),
            'baseline_delta_e00': asdict(self.model.report.before),
            'matched_delta_e00': asdict(self.model.report.after),
            'warnings_and_settings': self.model.report.warnings,
        }
        return (
            ('engine', 'group-held-out regularized camera matching'),
            ('experimental_status', 'experimental; internal validation only'),
            ('jpeg_accuracy', 'unverified'),
            ('evidence', self.model.evidence),
            ('model_sha256', digest.hexdigest()),
            ('processing_conditions', 'sRGB decode -> linear @ CCM.T -> sRGB encode/clip -> '
             'monotone tone -> trilinear additive residual -> clip'),
            ('diagnostics', json.dumps(diagnostics, sort_keys=True)),
        )

    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB:
        return apply_match(rgb, self.model, context=context)

    def close(self) -> None:
        """No native handle is retained by a match transform."""


def create_match_transform(model: MatchModel) -> RGBTransform:
    _validate_model(model)
    return _MatchTransform(model)
