"""Reference-image looks by distribution matching (image-look-v2).

The look is the difference between the colour statistics of finished
*reference* photos and the statistics of a *base*: either the user's own
photos the look will be applied to (role ``base``) or, when none are given, a
built-in ``typical photo`` baseline.  Statistics are measured in Oklab:

* tone        -- lightness quantiles -> monotone lightness curve
* colour cast -- mean (a, b) of low-chroma pixels per lightness band
* saturation  -- mean chroma per lightness band and per hue sector

Everything is estimated locally from pixel statistics; nothing is learned
from, or sent to, a network.  A statistical look cannot reproduce local edits,
subject content or hue rotations that depend on pixel correspondence (use
camera match for paired photos).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path

import numpy as np

from .errors import StudioError, ValidationError, checkpoint
from .images import load_image
from .types import JobContext, RGB, RGBTransform, SpaceSpec

BASELINE_VERSION = 'cps-distribution-v2'
ROLES = ('reference', 'base')
_CHUNK = 32768
_PROBS = np.array([.01, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .99])
_BAND_CENTERS = np.array([.15, .35, .55, .75, .92])
_BAND_SIGMA = .12
_SECTORS = 12
_NEUTRAL_CHROMA = .05      # Oklab chroma scale of "near neutral" pixels
_MIN_MASS = .02            # minimum band/sector mass before a statistic is trusted
_CAST_LIMIT = .08          # Oklab a/b units
_BAND_GAIN = (.5, 2.)
_SECTOR_GAIN = (.6, 1.6)
_TOTAL_GAIN = (.4, 2.5)
_MIN_SLOPE = .15
_MIN_END_SPAN = .01       # base quantile spacing needed to estimate a tone end slope
# Without base photos the reference statistics also contain the *content* of the references (a dark
# scene, a green forest), so the raw difference to a typical photograph over-corrects.  Against the
# built-in typical-photo baseline (only) the three components are therefore multiplied by these factors.
# Chosen on synthetic known transforms (3 reference sets x 5 transforms x 4 targets, mean CIEDE2000):
# tone .15 / cast .35 / saturation .7 gave the lowest mean error and the smallest error for an identity look.
_TYPICAL_DAMPING = {'tone': .15, 'cast': .35, 'saturation': .7}
_NO_DAMPING = {'tone': 1., 'cast': 1., 'saturation': 1.}

# Pooled Oklab statistics of 45 varied finished photographs (landscape,
# portrait, night, interiors, product; sRGB and Adobe RGB sources), sampled at
# 160x160 per image; used as the base when the user gives no base photos.
_TYPICAL = {
    'l_quantiles': [.2432, .3224, .3760, .4389, .4823, .5247, .5708, .6216, .6714, .7283, .8015, .8438, .8996],
    # Casts are judged against neutral grey, not against the average warmth of photographs.
    'cast': [[0., 0.], [0., 0.], [0., 0.], [0., 0.], [0., 0.]],
    'cast_mass': [.0995, .0981, .0941, .1290, .1094],
    'chroma': [.0213, .0423, .0603, .0569, .0419],
    'chroma_mass': [.1549, .2785, .3884, .4199, .2472],
    'sector_chroma': [.0471, .0598, .0656, .0677, .0682, .1060, .0932, .0747, .0717, .0890, .0898, .0583],
    'sector_mass': [.0092, .0491, .0715, .0267, .0134, .0364, .0288, .0515, .0497, .0663, .0967, .0246],
    'mean_chroma': .0471,
    'clipped_fraction': .1086,
}


def _fail(message, code='image_look_invalid'):
    raise ValidationError(message, code=code)


def _number(value, name, low=0., high=1.):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        _fail(f'{name} must be a real number.')
    try:
        finite = np.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        finite = False
    if not finite or not low <= value <= high:
        _fail(f'{name} must be finite and in [{low}, {high}].')


def _owned(value, shape):
    try:
        original = np.asarray(value)
        if original.dtype.kind not in 'iuf' or original.shape != shape or not np.isfinite(original).all():
            _fail(f'Expected finite numeric array with shape {shape}.')
        data = np.array(original, dtype=np.float64, order='C')
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValidationError('Invalid numeric array.', code='image_look_invalid') from exc
    return np.frombuffer(data.tobytes(), dtype=np.float64).reshape(shape)


def _strings(value):
    if not isinstance(value, (tuple, list)) or any(not isinstance(x, str) for x in value):
        _fail('Warnings and conflicts must be sequences of strings.')
    return tuple(value)


def _pairs(value):
    if not isinstance(value, (tuple, list)):
        _fail('Metadata must be key/value pairs.')
    result = []
    for pair in value:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(not isinstance(x, str) for x in pair):
            _fail('Metadata must contain string key/value pairs.')
        if len(pair[0]) > 256 or len(pair[1]) > 1024 * 1024:
            _fail('Metadata entry exceeds the bounded provenance limit.')
        result.append(tuple(pair))
    if (len(result) > 128 or len(dict(result)) != len(result)
            or sum(len(k) + len(v) for k, v in result) > 1024 * 1024):
        _fail('Metadata keys must be unique and total metadata must not exceed 1 MiB.')
    return tuple(result)


# ---------------------------------------------------------------------------
# Oklab
# ---------------------------------------------------------------------------
_M1 = np.array([[.4122214708, .5363325363, .0514459929],
                [.2119034982, .6806995451, .1073969566],
                [.0883024619, .2817188376, .6299787005]])
_M2 = np.array([[.2104542553, .7936177850, -.0040720468],
                [1.9779984951, -2.4285922050, .4505937099],
                [.0259040371, .7827717662, -.8086757660]])
_M1_INV = np.linalg.inv(_M1)
_M2_INV = np.linalg.inv(_M2)


def _to_linear(rgb):
    rgb = np.clip(rgb, 0, 1)
    return np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)


def _from_linear(lin):
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= .0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - .055)


def srgb_to_oklab(rgb):
    """Encoded sRGB (..., 3) in [0, 1] -> Oklab (..., 3)."""
    lms = _to_linear(np.asarray(rgb, dtype=np.float64)) @ _M1.T
    return np.cbrt(lms) @ _M2.T


def oklab_to_srgb(lab):
    """Oklab (..., 3) -> encoded sRGB (..., 3); out-of-gamut values are NOT clipped here."""
    lms = (np.asarray(lab, dtype=np.float64) @ _M2_INV.T) ** 3
    return _from_linear_unclipped(lms @ _M1_INV.T)


def _from_linear_unclipped(lin):
    sign = np.sign(lin)
    mag = np.abs(lin)
    return sign * np.where(mag <= .0031308, mag * 12.92, 1.055 * np.power(mag, 1 / 2.4) - .055)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ImageLookReference:
    path: Path
    weight: float = 1.0
    roi: tuple[float, float, float, float] = (0., 0., 1., 1.)
    role: str = 'reference'

    def __post_init__(self):
        if not isinstance(self.path, (str, Path)) or not str(self.path):
            _fail('Reference path must be nonempty.')
        if '\0' in str(self.path):
            _fail('Reference path must not contain NUL characters.')
        object.__setattr__(self, 'path', Path(self.path))
        _number(self.weight, 'weight', 0, np.finfo(float).max)
        if self.weight <= 0:
            _fail('Reference weight must be strictly positive.')
        if not isinstance(self.roi, (tuple, list)) or len(self.roi) != 4:
            _fail('ROI requires normalized x0,y0,x1,y1.')
        for x in self.roi:
            _number(x, 'ROI')
        if self.roi[0] >= self.roi[2] or self.roi[1] >= self.roi[3]:
            _fail('ROI must have positive width and height.')
        if self.role not in ROLES:
            _fail("Role must be 'reference' or 'base'.")
        object.__setattr__(self, 'roi', tuple(float(x) for x in self.roi))
        object.__setattr__(self, 'weight', float(self.weight))


@dataclass(frozen=True, slots=True)
class ImageLookSettings:
    baseline_version: str = BASELINE_VERSION
    seed: int = 42
    analysis_size: int = 160
    max_dimension: int = 2048
    tone_amount: float = 1.0
    color_amount: float = 1.0
    saturation_amount: float = 1.0
    acknowledge_conflicts: bool = False

    def __post_init__(self):
        if self.baseline_version != BASELINE_VERSION:
            _fail('Unsupported canonical baseline version.', 'image_look_baseline')
        for name, low, high in (('seed', 0, 2**32 - 1), ('analysis_size', 16, 256), ('max_dimension', 256, 4096)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                _fail(f'{name} must be an integer in [{low}, {high}].')
        for name in ('tone_amount', 'color_amount', 'saturation_amount'):
            _number(getattr(self, name), name)
            object.__setattr__(self, name, float(getattr(self, name)))
        if type(self.acknowledge_conflicts) is not bool:
            _fail('acknowledge_conflicts must be boolean.')


@dataclass(frozen=True, slots=True)
class LookStatistics:
    """Oklab colour statistics of one image (or a weighted pool of images)."""
    l_quantiles: np.ndarray        # (13,) lightness at _PROBS
    cast: np.ndarray               # (5, 2) mean (a, b) of near-neutral pixels per lightness band
    cast_mass: np.ndarray          # (5,) share of samples that formed each cast estimate
    chroma: np.ndarray             # (5,) mean chroma (cast removed) per lightness band
    chroma_mass: np.ndarray        # (5,) share of samples in each band
    sector_chroma: np.ndarray      # (12,) mean chroma of colourful pixels per hue sector
    sector_mass: np.ndarray        # (12,) share of colourful samples per hue sector
    mean_chroma: float
    clipped_fraction: float

    def __post_init__(self):
        q = _owned(self.l_quantiles, (len(_PROBS),))
        if np.any((q < 0) | (q > 1)) or np.any(np.diff(q) < -1e-9):
            _fail('Invalid lightness quantiles.')
        object.__setattr__(self, 'l_quantiles', q)
        for name, shape in (('cast', (5, 2)), ('cast_mass', (5,)), ('chroma', (5,)), ('chroma_mass', (5,)),
                            ('sector_chroma', (_SECTORS,)), ('sector_mass', (_SECTORS,))):
            object.__setattr__(self, name, _owned(getattr(self, name), shape))
        if np.max(np.abs(self.cast)) > .5 or np.any(self.cast_mass < 0) or np.any(self.chroma_mass < 0) \
                or np.any(self.sector_mass < 0) or np.any(self.chroma < 0) or np.any(self.sector_chroma < 0):
            _fail('Invalid colour statistics.')
        _number(self.mean_chroma, 'mean_chroma', 0, 1)
        _number(self.clipped_fraction, 'clipped_fraction')
        object.__setattr__(self, 'mean_chroma', float(self.mean_chroma))
        object.__setattr__(self, 'clipped_fraction', float(self.clipped_fraction))

    def as_json(self):
        return {k: (np.round(v, 5).tolist() if isinstance(v, np.ndarray) else round(float(v), 5))
                for k, v in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class ImageLookReferenceAnalysis:
    reference: ImageLookReference
    source_sha256: str
    sample_count: int
    stats: LookStatistics
    normalization: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.reference, ImageLookReference):
            _fail('Expected ImageLookReference.')
        if (not isinstance(self.source_sha256, str) or len(self.source_sha256) != 64
                or any(c not in '0123456789abcdef' for c in self.source_sha256)):
            _fail('Expected hexadecimal source SHA256.')
        if type(self.sample_count) is not int or not 4 <= self.sample_count <= 65536:
            _fail('Invalid effective sample count.')
        if not isinstance(self.stats, LookStatistics):
            _fail('Expected LookStatistics.')
        object.__setattr__(self, 'normalization', _pairs(self.normalization))
        object.__setattr__(self, 'warnings', _strings(self.warnings))

    # Display conveniences (kept for the GUI/CLI).
    @property
    def luminance_quantiles(self):
        return self.stats.l_quantiles

    @property
    def saturation(self):
        return self.stats.mean_chroma

    @property
    def color_support(self):
        """Share of hue sectors with enough colourful pixels (coverage, not accuracy)."""
        return float(np.mean(self.stats.sector_mass >= .03))

    @property
    def clipped_fraction(self):
        return self.stats.clipped_fraction


@dataclass(frozen=True, slots=True)
class ImageLookAnalysis:
    references: tuple[ImageLookReferenceAnalysis, ...]
    settings: ImageLookSettings
    warnings: tuple[str, ...]
    conflicts: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.references, (tuple, list)) or not 1 <= len(self.references) <= 128:
            _fail('Select between 1 and 64 references.')
        if any(not isinstance(r, ImageLookReferenceAnalysis) for r in self.references):
            _fail('Expected reference analysis records.')
        if not isinstance(self.settings, ImageLookSettings):
            _fail('Expected ImageLookSettings.')
        refs = [r for r in self.references if r.reference.role == 'reference']
        if not 1 <= len(refs) <= 64:
            _fail('Select between 1 and 64 references.')
        object.__setattr__(self, 'references', tuple(sorted(self.references, key=_reference_key)))
        object.__setattr__(self, 'warnings', _strings(self.warnings))
        object.__setattr__(self, 'conflicts', _strings(self.conflicts))

    @property
    def base_count(self):
        return sum(1 for r in self.references if r.reference.role == 'base')


@dataclass(frozen=True, slots=True)
class ImageLookModel:
    tone_x: np.ndarray             # (15,) increasing base lightness knots in [0, 1]
    tone_y: np.ndarray             # (15,) increasing reference lightness knots in [0, 1]
    cast: np.ndarray               # (5, 2) Oklab (a, b) shift per lightness band
    band_gain: np.ndarray          # (5,) chroma gain per lightness band
    sector_gain: np.ndarray        # (12,) relative chroma gain per hue sector
    baseline: str                  # 'base_images' or 'typical_photo'
    settings: ImageLookSettings
    warnings: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self):
        x = _owned(self.tone_x, (len(_PROBS) + 2,))
        y = _owned(self.tone_y, (len(_PROBS) + 2,))
        if (np.any((x < 0) | (x > 1) | (y < 0) | (y > 1)) or np.any(np.diff(x) <= 0) or np.any(np.diff(y) < 0)
                or x[0] != 0 or x[-1] != 1):
            _fail('Tone knots must be increasing in [0, 1] with x from 0 to 1.')
        cast = _owned(self.cast, (5, 2))
        band = _owned(self.band_gain, (5,))
        sector = _owned(self.sector_gain, (_SECTORS,))
        if (np.max(np.abs(cast)) > _CAST_LIMIT + 1e-9 or np.any(band < _BAND_GAIN[0] - 1e-9) or np.any(band > _BAND_GAIN[1] + 1e-9)
                or np.any(sector < _SECTOR_GAIN[0] - 1e-9) or np.any(sector > _SECTOR_GAIN[1] + 1e-9)):
            _fail('Model exceeds coefficient limits.')
        if self.baseline not in ('base_images', 'typical_photo'):
            _fail('Unknown baseline kind.')
        if not isinstance(self.settings, ImageLookSettings):
            _fail('Expected ImageLookSettings.')
        object.__setattr__(self, 'tone_x', x)
        object.__setattr__(self, 'tone_y', y)
        object.__setattr__(self, 'cast', cast)
        object.__setattr__(self, 'band_gain', band)
        object.__setattr__(self, 'sector_gain', sector)
        object.__setattr__(self, 'warnings', _strings(self.warnings))
        object.__setattr__(self, 'provenance', _pairs(self.provenance))


def _reference_key(record):
    return (record.reference.role, record.source_sha256, record.reference.roi, record.reference.weight,
            str(record.reference.path))


def _hash(path, context):
    digest = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                checkpoint(context, stage='image_look.hash', fraction=0)
                digest.update(block)
    except OSError as exc:
        raise StudioError(f'Cannot read reference: {path}', code='image_look_source_io') from exc
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def _band_weights(l):
    w = np.exp(-.5 * ((l[:, None] - _BAND_CENTERS[None, :]) / _BAND_SIGMA) ** 2)
    return w


def look_statistics(rgb_samples) -> LookStatistics:
    """Oklab statistics of an (N, 3) encoded-sRGB sample set."""
    pixels = np.asarray(rgb_samples, dtype=np.float64).reshape(-1, 3)
    lab = srgb_to_oklab(pixels)
    l, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    chroma = np.hypot(a, b)
    hue = np.arctan2(b, a)
    n = len(pixels)
    quantiles = np.quantile(l, _PROBS)
    weights = _band_weights(l)                                   # (n, 5)
    neutral = np.exp(-(chroma / _NEUTRAL_CHROMA) ** 2)           # (n,)
    cast = np.zeros((5, 2))
    cast_mass = np.zeros(5)
    band_chroma = np.zeros(5)
    band_mass = np.zeros(5)
    for k in range(5):
        w = weights[:, k] * neutral
        cast_mass[k] = w.sum() / n
        if w.sum() > 0:
            cast[k] = np.array([np.sum(a * w), np.sum(b * w)]) / w.sum()
        wb = weights[:, k]
        band_mass[k] = wb.sum() / n
        if wb.sum() > 0:
            ca = np.hypot(a - cast[k, 0], b - cast[k, 1])
            band_chroma[k] = np.sum(ca * wb) / wb.sum()
    # Hue sectors over colourful pixels, cast removed per band so a global tint
    # does not masquerade as one strongly saturated hue.
    band_norm = weights / np.maximum(weights.sum(1, keepdims=True), 1e-12)
    a_c = a - band_norm @ cast[:, 0]
    b_c = b - band_norm @ cast[:, 1]
    chroma_c = np.hypot(a_c, b_c)
    hue_c = np.arctan2(b_c, a_c)
    colourful = chroma_c > .03
    bins = np.minimum(((hue_c + np.pi) * _SECTORS / (2 * np.pi)).astype(int), _SECTORS - 1)
    sector_mass = np.bincount(bins[colourful], minlength=_SECTORS) / n
    sector_chroma = np.bincount(bins[colourful], weights=chroma_c[colourful], minlength=_SECTORS)
    with np.errstate(invalid='ignore', divide='ignore'):
        sector_chroma = np.where(sector_mass > 0, sector_chroma / np.maximum(sector_mass * n, 1), 0.)
    clipping = float(np.mean(np.any((pixels <= 1 / 255) | (pixels >= 254 / 255), axis=1)))
    return LookStatistics(quantiles, cast, cast_mass, band_chroma, band_mass, sector_chroma, sector_mass,
                          float(chroma_c.mean()), clipping)


def _typical():
    t = _TYPICAL
    return LookStatistics(np.array(t['l_quantiles']), np.array(t['cast']), np.array(t['cast_mass']),
                          np.array(t['chroma']), np.array(t['chroma_mass']), np.array(t['sector_chroma']),
                          np.array(t['sector_mass']), t['mean_chroma'], t['clipped_fraction'])


def pool_statistics(records, weights) -> LookStatistics:
    """Weighted pool of LookStatistics.  Quantile functions are averaged
    (1-D Wasserstein barycenter); masses weight the per-band/sector means."""
    w = np.asarray(weights, dtype=np.float64)
    w = w / w.sum()
    q = sum(wi * s.l_quantiles for wi, s in zip(w, records))

    def mass_mean(values, masses):
        values = np.stack([np.asarray(v, dtype=np.float64) for v in values])      # (r, k[, 2])
        masses = np.stack([np.asarray(m, dtype=np.float64) for m in masses])      # (r, k)
        wm = w.reshape(-1, *([1] * (masses.ndim - 1))) * masses                   # (r, k)
        den = wm.sum(0)                                                           # (k,)
        wm_b = wm[..., None] if values.ndim > masses.ndim else wm
        den_b = den[..., None] if values.ndim > masses.ndim else den
        num = (wm_b * values).sum(0)
        return np.where(den_b > 0, num / np.maximum(den_b, 1e-12), 0.), den
    cast, cast_mass = mass_mean([s.cast for s in records], [s.cast_mass for s in records])
    chroma, chroma_mass = mass_mean([s.chroma for s in records], [s.chroma_mass for s in records])
    sector_chroma, sector_mass = mass_mean([s.sector_chroma for s in records], [s.sector_mass for s in records])
    mean_chroma = float(sum(wi * s.mean_chroma for wi, s in zip(w, records)))
    clipped = float(sum(wi * s.clipped_fraction for wi, s in zip(w, records)))
    return LookStatistics(np.maximum.accumulate(q), cast, cast_mass, chroma, chroma_mass, sector_chroma,
                          sector_mass, mean_chroma, clipped)


def _sample(rgb, reference, settings):
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = reference.roi
    left, top = int(np.floor(x0 * w)), int(np.floor(y0 * h))
    right, bottom = int(np.ceil(x1 * w)), int(np.ceil(y1 * h))
    crop = rgb[top:bottom, left:right]
    ch, cw = crop.shape[:2]
    if ch * cw < 16 or min(ch, cw) < 2:
        _fail('Reference crop is too small to infer a look.', 'image_look_unusable')
    n = settings.analysis_size
    rng = np.random.default_rng(settings.seed)
    yy, xx = np.meshgrid(np.arange(n), np.arange(n), indexing='ij')
    ys = np.minimum(((yy + rng.random((n, n))) * ch / n).astype(int), ch - 1)
    xs = np.minimum(((xx + rng.random((n, n))) * cw / n).astype(int), cw - 1)
    pixels = crop[ys, xs].reshape(-1, 3).astype(np.float64)
    count = int(len(np.unique(ys * cw + xs)))
    return pixels, count, (left, top, right, bottom)


def _statistics(rgb, reference, digest, settings, normalization, loader_warnings):
    pixels, count, box = _sample(rgb, reference, settings)
    if np.max(np.std(pixels, axis=0)) < 1e-5:
        _fail('Constant reference crop contains no usable distribution.', 'image_look_unusable')
    stats = look_statistics(pixels)
    warnings = list(loader_warnings)
    if count < 256:
        warnings.append('insufficient_samples')
    if stats.l_quantiles[-1] - stats.l_quantiles[0] < .35:
        warnings.append('limited_luminance_coverage')
    if np.count_nonzero(stats.sector_mass >= .03) < 3:
        warnings.append('limited_color_coverage')
    if stats.clipped_fraction > .05:
        warnings.append('reference_clipping')
    norm = dict(normalization)
    norm.update({'image_look_roi_pixels': json.dumps(list(box)),
                 'image_look_normalized_shape': json.dumps(list(rgb.shape)),
                 'image_look_sampling': 'seeded normalized stratified square grid; equal image mass',
                 'image_look_color_domain': 'encoded sRGB -> Oklab (D65)'})
    return ImageLookReferenceAnalysis(reference, digest, count, stats, tuple(sorted(norm.items())), tuple(warnings))


def _conflicts(records):
    conflicts = set()
    refs = [r for r in records if r.reference.role == 'reference']
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            qa, qb = a.stats.l_quantiles, b.stats.l_quantiles
            if abs(qa[6] - qb[6]) > .2 or abs(np.ptp(qa) - np.ptp(qb)) > .3:
                conflicts.add('conflicting_tone')
            for k in range(5):
                if min(a.stats.cast_mass[k], b.stats.cast_mass[k]) >= _MIN_MASS:
                    av, bv = a.stats.cast[k], b.stats.cast[k]
                    if np.linalg.norm(av - bv) > .03 and np.dot(av, bv) < 0:
                        conflicts.add('conflicting_color')
            ca, cb = a.stats.mean_chroma, b.stats.mean_chroma
            if max(ca, cb) > 1.6 * max(min(ca, cb), .01):
                conflicts.add('conflicting_saturation')
    return tuple(sorted(conflicts))


def analyze_image_look(references: tuple[ImageLookReference, ...], *,
                       settings: ImageLookSettings = ImageLookSettings(),
                       context: JobContext | None = None) -> ImageLookAnalysis:
    checkpoint(context, stage='image_look.analyze', fraction=0)
    if not isinstance(settings, ImageLookSettings):
        _fail('Expected ImageLookSettings.')
    if not isinstance(references, (tuple, list)) or not 1 <= len(references) <= 128:
        _fail('Select between 1 and 64 references.')
    if any(not isinstance(ref, ImageLookReference) for ref in references):
        _fail('Expected ImageLookReference records.')
    if not 1 <= sum(1 for r in references if r.role == 'reference') <= 64:
        _fail('Select between 1 and 64 references.')
    if sum(1 for r in references if r.role == 'base') > 64:
        _fail('Select at most 64 base images.')
    child = JobContext(cancel=context.cancel) if context is not None else None
    records = []
    for i, reference in enumerate(references):
        digest = _hash(reference.path, child)
        image = load_image(reference.path, max_dimension=settings.max_dimension, context=child)
        if _hash(reference.path, child) != digest:
            _fail('Reference changed while being analyzed.', 'image_look_source_changed')
        records.append(_statistics(image.rgb, reference, digest, settings, image.metadata, image.warnings))
        del image
        checkpoint(context, stage='image_look.analyze', fraction=(i + 1) / len(references))
    conflicts = _conflicts(records)
    warnings = {'approximate_unverified', *conflicts,
                *(w for record in records for w in record.warnings if record.reference.role == 'reference')}
    if not any(r.reference.role == 'base' for r in records):
        warnings.add('typical_photo_baseline')
    result = ImageLookAnalysis(tuple(records), settings, tuple(sorted(warnings)), conflicts)
    checkpoint(child, stage='image_look.analyze', fraction=1)
    return result


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------
def _fill_invalid(values, valid):
    """Replace invalid band entries by the nearest valid one (0/identity if none)."""
    values = np.array(values, dtype=np.float64)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return None
    for k in np.flatnonzero(~valid):
        values[k] = values[idx[np.argmin(np.abs(idx - k))]]
    return values


def _valid_x(x):
    """Tone knot positions: strictly increasing from 0 to 1, at least `gap` from both ends."""
    n = len(x)
    gap = 1e-3
    x = np.clip(np.array(x, dtype=np.float64), 0, 1)
    x[0], x[-1] = 0., 1.
    for i in range(1, n - 1):
        x[i] = max(x[i], x[i - 1] + gap)
    for i in range(n - 2, 0, -1):
        x[i] = min(x[i], x[i + 1] - gap)
    x[1:-1] = np.clip(x[1:-1], gap, 1 - gap)
    return x


def _valid_y(x, y):
    """Tone knot values: non-decreasing with a floor slope, inside [0, 1]."""
    n = len(x)
    y = np.clip(np.array(y, dtype=np.float64), 0, 1)
    # The slope floor may push the top knots above 1, so compress from the end to the beginning afterwards.
    for i in range(1, n):
        y[i] = max(y[i], y[i - 1] + _MIN_SLOPE * (x[i] - x[i - 1]))
    y = np.clip(y, 0, 1)
    for i in range(n - 2, -1, -1):
        y[i] = min(y[i], y[i + 1])
    return y


def _monotone_knots(x, y):
    """Tone knots: x strictly increasing from 0 to 1, y non-decreasing with a floor slope."""
    x = _valid_x(x)
    return x, _valid_y(x, y)


def _pchip_slopes(x, y):
    h = np.diff(x)
    d = np.diff(y) / h
    m = np.zeros_like(y)
    for i in range(1, len(y) - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    m[0] = d[0]
    m[-1] = d[-1]
    return m


def _pchip(x, y, m, t):
    i = np.clip(np.searchsorted(x, t, side='right') - 1, 0, len(x) - 2)
    h = x[i + 1] - x[i]
    s = (t - x[i]) / h
    h00 = (1 + 2 * s) * (1 - s) ** 2
    h10 = s * (1 - s) ** 2
    h01 = s * s * (3 - 2 * s)
    h11 = s * s * (s - 1)
    return h00 * y[i] + h10 * h * m[i] + h01 * y[i + 1] + h11 * h * m[i + 1]


def _fit(ref: LookStatistics, base: LookStatistics, settings: ImageLookSettings, baseline: str = 'base_images'):
    damping = _TYPICAL_DAMPING if baseline == 'typical_photo' else _NO_DAMPING
    tone_amount = settings.tone_amount * damping['tone']
    color_amount = settings.color_amount * damping['cast']
    saturation_amount = settings.saturation_amount * damping['saturation']
    # --- tone: quantile map base -> reference, extended to the ends -------
    bx, by = base.l_quantiles, ref.l_quantiles
    # End slopes are only estimated where the base quantiles are spread out; a pile-up (clipped
    # shadows/highlights) says nothing about the slope, so the identity slope is used there.
    slope0 = np.clip((by[1] - by[0]) / (bx[1] - bx[0]), .5, 3.) if bx[1] - bx[0] >= _MIN_END_SPAN else 1.
    slope1 = np.clip((by[-1] - by[-2]) / (bx[-1] - bx[-2]), .5, 3.) if bx[-1] - bx[-2] >= _MIN_END_SPAN else 1.
    y0 = np.clip(by[0] - bx[0] * slope0, 0, by[0])
    y1 = np.clip(by[-1] + (1 - bx[-1]) * slope1, by[-1], 1)
    x = np.concatenate(([0.], bx, [1.]))
    y = np.concatenate(([y0], by, [y1]))
    # The deviation from the identity is scaled linearly; the knots are made valid first so that a
    # zero amount gives exactly y == x.
    deviation = y - x
    x = _valid_x(x)
    y = _valid_y(x, x + tone_amount * deviation)
    # --- colour cast per band ------------------------------------------
    valid = (ref.cast_mass >= _MIN_MASS) & (base.cast_mass >= _MIN_MASS)
    delta = _fill_invalid(ref.cast - base.cast, valid)
    cast = np.zeros((5, 2)) if delta is None else np.clip(delta, -_CAST_LIMIT, _CAST_LIMIT) * color_amount
    # --- chroma gain per band -------------------------------------------
    valid = (ref.chroma_mass >= _MIN_MASS) & (base.chroma_mass >= _MIN_MASS) & (base.chroma > 1e-3)
    with np.errstate(invalid='ignore', divide='ignore'):
        ratio = np.where(valid, ref.chroma / np.maximum(base.chroma, 1e-3), 1.)
    gain = _fill_invalid(ratio, valid)
    gain = np.ones(5) if gain is None else np.clip(gain, *_BAND_GAIN)
    gain = np.clip(1 + saturation_amount * (gain - 1), *_BAND_GAIN)
    # --- hue-sector relative gain ----------------------------------------
    valid = (ref.sector_mass >= .03) & (base.sector_mass >= .03) & (base.sector_chroma > 1e-3)
    ref_rel = ref.sector_chroma / max(ref.mean_chroma, 1e-3)
    base_rel = base.sector_chroma / max(base.mean_chroma, 1e-3)
    with np.errstate(invalid='ignore', divide='ignore'):
        rel = np.where(valid, ref_rel / np.maximum(base_rel, 1e-3), 1.)
    rel = np.clip(rel, *_SECTOR_GAIN)
    # circular smoothing so a single sector cannot create a hue seam
    rel = .25 * np.roll(rel, 1) + .5 * rel + .25 * np.roll(rel, -1)
    sector = np.clip(1 + saturation_amount * (rel - 1), *_SECTOR_GAIN)
    return x, y, cast, gain, sector


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fit_image_look(analysis: ImageLookAnalysis, *, acknowledge_conflicts: bool | None = None,
                   context: JobContext | None = None) -> ImageLookModel:
    checkpoint(context, stage='image_look.fit', fraction=0)
    if not isinstance(analysis, ImageLookAnalysis):
        _fail('Expected ImageLookAnalysis.')
    if acknowledge_conflicts is not None and type(acknowledge_conflicts) is not bool:
        _fail('Conflict acknowledgement must be boolean.')
    settings = analysis.settings if acknowledge_conflicts is None else replace(
        analysis.settings, acknowledge_conflicts=acknowledge_conflicts)
    records = analysis.references
    conflicts = tuple(sorted(set(analysis.conflicts) | set(_conflicts(records))))
    if conflicts and not settings.acknowledge_conflicts:
        _fail('References have conflicting statistics; change selection or explicitly acknowledge: '
              + ', '.join(conflicts), 'image_look_conflict')

    def pooled(role):
        group = [r for r in records if r.reference.role == role]
        if not group:
            return None
        weights = np.array([r.reference.weight for r in group])
        weights = weights / weights.max()
        confidence = np.array([min(1., r.sample_count / 256) for r in group])
        return pool_statistics([r.stats for r in group], weights * confidence)
    ref = pooled('reference')
    base = pooled('base')
    baseline = 'base_images' if base is not None else 'typical_photo'
    if base is None:
        base = _typical()
    x, y, cast, gain, sector = _fit(ref, base, settings, baseline)
    warnings = set(analysis.warnings) | {'approximate_unverified'} | set(conflicts)
    if conflicts:
        warnings.add('conflicts_acknowledged')
    if baseline == 'typical_photo':
        warnings.add('typical_photo_baseline')
    checkpoint(context, stage='image_look.fit', fraction=.5)
    provisional = ImageLookModel(x, y, cast, gain, sector, baseline, settings, tuple(sorted(warnings)), ())
    axis = np.linspace(0, 1, 17)
    safety_grid = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1).reshape(-1, 3)
    raw, clipped = _predict(safety_grid, provisional)
    if clipped.mean() > .02:
        warnings.add('output_clipping')
    source_records = []
    for r in records:
        source_records.append({'path': r.reference.path.name, 'sha256': r.source_sha256, 'role': r.reference.role,
                               'weight': r.reference.weight, 'roi': r.reference.roi,
                               'sample_count': r.sample_count, 'normalization': dict(r.normalization),
                               'warnings': r.warnings, 'statistics': r.stats.as_json()})
    provenance = (
        ('engine', 'image-look-v2'), ('status', 'approximate_unverified'),
        ('jpeg_accuracy', 'unverified'),
        ('baseline', _json({'version': BASELINE_VERSION, 'kind': baseline,
                            'typical_photo': _TYPICAL if baseline == 'typical_photo' else None,
                            'typical_photo_source': 'pooled Oklab statistics of 45 varied finished photographs'})),
        ('settings', _json(asdict(settings))), ('references', _json(source_records)),
        ('pooled_reference', _json(ref.as_json())), ('pooled_base', _json(base.as_json())),
        ('algorithm', _json({'color_space': 'Oklab', 'quantile_probabilities': _PROBS.tolist(),
                             'band_centers': _BAND_CENTERS.tolist(), 'band_sigma': _BAND_SIGMA,
                             'neutral_chroma': _NEUTRAL_CHROMA, 'hue_sectors': _SECTORS, 'min_mass': _MIN_MASS,
                             'cast_limit': _CAST_LIMIT, 'band_gain': _BAND_GAIN, 'sector_gain': _SECTOR_GAIN,
                             'total_gain': _TOTAL_GAIN, 'min_tone_slope': _MIN_SLOPE,
                             'typical_photo_damping': dict(_TYPICAL_DAMPING) if baseline == 'typical_photo' else None,
                             'tone': 'quantile map base -> reference, PCHIP, end slopes clipped [0.5, 3]; '
                                     'deviation from identity scaled by tone_amount (x damping for typical_photo)',
                             'pooling': 'weighted mean of quantile functions; mass-weighted band/sector means',
                             'gamut': 'chroma reduced toward neutral (bisection) until inside sRGB, then clip'})),
        ('model', _json({'tone_x': np.round(x, 5).tolist(), 'tone_y': np.round(y, 5).tolist(),
                         'cast': np.round(cast, 5).tolist(), 'band_gain': np.round(gain, 5).tolist(),
                         'sector_gain': np.round(sector, 5).tolist()})),
        ('warnings', _json(sorted(warnings))), ('conflicts', _json(conflicts)),
        ('processing_conditions', 'encoded sRGB -> Oklab -> monotone lightness curve -> chroma gain by '
         'lightness band x hue sector -> lightness-banded (a,b) cast with end taper -> sRGB -> gamut '
         'chroma reduction -> clip; independent component amounts; overall strength only in generate_lut'),
    )
    result = replace(provisional, warnings=tuple(sorted(warnings)), provenance=provenance)
    checkpoint(context, stage='image_look.fit', fraction=1)
    checkpoint(JobContext(cancel=context.cancel) if context else None, stage='image_look.fit', fraction=1)
    return result


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def _band_interp(l, values):
    w = _band_weights(l)
    w = w / np.maximum(w.sum(1, keepdims=True), 1e-12)
    return w @ values


def _sector_interp(hue, values):
    pos = (hue + np.pi) * _SECTORS / (2 * np.pi) - .5
    i0 = np.floor(pos).astype(int)
    f = pos - i0
    v0 = values[i0 % _SECTORS]
    v1 = values[(i0 + 1) % _SECTORS]
    return v0 * (1 - f) + v1 * f


def _smoothstep(t):
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)


def _predict(data, model):
    lab = srgb_to_oklab(data)
    l, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    m = _pchip_slopes(model.tone_x, model.tone_y)
    l2 = np.clip(_pchip(model.tone_x, model.tone_y, m, np.clip(l, 0, 1)), 0, 1)
    gain = np.clip(_band_interp(l2, model.band_gain) * _sector_interp(np.arctan2(b, a), model.sector_gain), *_TOTAL_GAIN)
    taper = _smoothstep(l2 / .08) * _smoothstep((1 - l2) / .05)
    cast = _band_interp(l2, model.cast) * taper[:, None]
    a2 = a * gain + cast[:, 0]
    b2 = b * gain + cast[:, 1]
    out = oklab_to_srgb(np.column_stack((l2, a2, b2)))
    outside = np.any((out < -1e-6) | (out > 1 + 1e-6), axis=1)
    if outside.any():
        # Largest chroma factor (bisection) that brings the colour inside sRGB; continuous in the input.
        idx = np.flatnonzero(outside)
        lo, hi = np.zeros(len(idx)), np.ones(len(idx))
        for _ in range(16):
            mid = .5 * (lo + hi)
            trial = oklab_to_srgb(np.column_stack((l2[idx], a2[idx] * mid, b2[idx] * mid)))
            inside = ~np.any((trial < -1e-6) | (trial > 1 + 1e-6), axis=1)
            lo, hi = np.where(inside, mid, lo), np.where(inside, hi, mid)
        out[idx] = oklab_to_srgb(np.column_stack((l2[idx], a2[idx] * lo, b2[idx] * lo)))
    return np.clip(out, 0, 1), outside


def apply_image_look(rgb: RGB, model: ImageLookModel, *, context: JobContext | None = None) -> RGB:
    checkpoint(context, stage='image_look.apply', fraction=0)
    if not isinstance(model, ImageLookModel):
        _fail('Expected ImageLookModel.')
    try:
        data = np.asarray(rgb)
        if (data.dtype.kind not in 'iuf' or data.ndim < 1 or data.shape[-1] != 3 or not data.size
                or not np.isfinite(data).all() or np.any((data < 0) | (data > 1))):
            _fail('RGB must be finite, nonempty (...,3), in encoded-sRGB [0,1].')
    except (TypeError, ValueError) as exc:
        raise ValidationError('Invalid RGB input.', code='image_look_invalid') from exc
    flat = data.reshape(-1, 3)
    result = np.empty_like(flat, dtype=np.float32)
    for start in range(0, len(flat), _CHUNK):
        end = min(start + _CHUNK, len(flat))
        result[start:end] = _predict(flat[start:end].astype(np.float64), model)[0]
        checkpoint(context, stage='image_look.apply', fraction=end / len(flat))
    checkpoint(JobContext(cancel=context.cancel) if context else None, stage='image_look.apply', fraction=1)
    return np.frombuffer(result.tobytes(), dtype=np.float32).reshape(data.shape)


@dataclass(frozen=True, slots=True)
class _ImageLookTransform:
    model: ImageLookModel
    input_space: SpaceSpec = SpaceSpec('srgb')
    output_space: SpaceSpec = SpaceSpec('srgb')

    @property
    def provenance(self):
        return self.model.provenance

    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB:
        return apply_image_look(rgb, self.model, context=context)

    def close(self) -> None:
        """The transform owns no native handles."""


def create_image_look_transform(model: ImageLookModel) -> RGBTransform:
    if not isinstance(model, ImageLookModel):
        _fail('Expected ImageLookModel.')
    return _ImageLookTransform(model)
