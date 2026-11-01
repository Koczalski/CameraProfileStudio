"""Immutable, GUI-independent public engine records."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Literal, Protocol
import numpy as np
from numpy.typing import NDArray
RGB = NDArray[np.float32]
Matrix = NDArray[np.float64]
Indices = NDArray[np.int64]
Mask = NDArray[np.bool_]
TagValue = int | float | str | bytes | tuple[int | float, ...]
Intent = Literal['perceptual', 'relative', 'saturation', 'absolute']
Gamut = Literal['clip', 'error']
LookMode = Literal['colors_only', 'tone_only', 'look_only', 'combined']
class _Record:
    __slots__ = ()
    def __post_init__(self) -> None:
        _validate_record(self)

@dataclass(frozen=True, slots=True)
class SpaceSpec(_Record):
    kind: Literal['srgb', 'linear_srgb', 'icc_rgb', 'camera_linear']; profile_path: Path | None = None
@dataclass(frozen=True, slots=True)
class Progress(_Record):
    stage: str; fraction: float; message: str = ''
class CancelToken(Protocol):
    def is_cancelled(self) -> bool: ...
@dataclass(frozen=True, slots=True)
class JobContext(_Record):
    progress: Callable[[Progress], None] | None = None; cancel: CancelToken | None = None
@dataclass(frozen=True, slots=True)
class Capability(_Record):
    feature: str; status: Literal['supported', 'partial', 'unsupported', 'available', 'missing', 'verified', 'unverified']; reasons: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class ProfileInfo(_Record):
    path: Path; sha256: str; format: Literal['icc', 'dcp']; description: str; device_class: str; color_space: str; pcs: str; version: str; tags: tuple[tuple[str, str], ...]; capabilities: tuple[Capability, ...]; warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class DcpProfile(_Record):
    info: ProfileInfo; tags: tuple[tuple[int, TagValue], ...]
@dataclass(frozen=True, slots=True)
class DcpSettings(_Record):
    camera_neutral: tuple[float, float, float] | None = None; illuminant_kelvin: float | None = None; gamut: Gamut = 'clip'; include_hue_sat_map: bool = True; include_look_table: bool = True; include_tone_curve: bool = True
@dataclass(frozen=True, slots=True)
class TransformRequest(_Record):
    input_space: SpaceSpec; output_space: SpaceSpec; mode: Literal['technical', 'look'] = 'technical'; profile_path: Path | None = None; intent: Intent = 'relative'; black_point_compensation: bool = False; gamut: Gamut = 'clip'; dcp: DcpSettings | None = None; look_mode: LookMode = 'combined'
class RGBTransform(Protocol):
    input_space: SpaceSpec
    output_space: SpaceSpec
    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB: ...
    def close(self) -> None: ...
@dataclass(frozen=True, slots=True)
class Lut3D(_Record):
    values: RGB; input_space: SpaceSpec; output_space: SpaceSpec; title: str = 'Camera Profile Studio'; strength: float = 1.0; provenance: tuple[tuple[str, str], ...] = ()
@dataclass(frozen=True, slots=True)
class ValidationReport(_Record):
    valid: bool; sample_count: int; minimum: float; maximum: float; errors: tuple[str, ...] = (); warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class RawRecipe(_Record):
    use_camera_wb: bool = True; user_wb: tuple[float, float, float, float] | None = None; exposure_shift: float = 1.0; demosaic: Literal['AHD'] = 'AHD'
@dataclass(frozen=True, slots=True)
class ImageData(_Record):
    rgb: RGB; source_path: Path | None; metadata: tuple[tuple[str, str], ...] = (); warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class ExportResult(_Record):
    cube_path: Path; metadata_path: Path; sha256: str; validation: ValidationReport
@dataclass(frozen=True, slots=True)
class SamplePairs(_Record):
    source_rgb: RGB; target_rgb: RGB; groups: tuple[str, ...]; evidence: str
@dataclass(frozen=True, slots=True)
class MatchSettings(_Record):
    fit: Literal['ccm', 'ccm_tone', 'ccm_tone_lut'] = 'ccm_tone_lut'; regularization: float = 0.01; grid_size: int = 9; holdout_fraction: float = 0.2; seed: int = 42
@dataclass(frozen=True, slots=True)
class ErrorStats(_Record):
    count: int; mean: float; median: float; p95: float; maximum: float
@dataclass(frozen=True, slots=True)
class MatchReport(_Record):
    train_indices: Indices; holdout_indices: Indices; before: ErrorStats; after: ErrorStats; warnings: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class MatchModel(_Record):
    ccm: Matrix; tone_curve: Matrix; residual: RGB; report: MatchReport; evidence: str
@dataclass(frozen=True, slots=True)
class AlignmentResult(_Record):
    source_aligned: RGB; target_rgb: RGB; valid_mask: Mask; homography: Matrix; inlier_fraction: float


def _fail(message: str) -> None:
    from .errors import ValidationError
    raise ValidationError(message, code='invalid_data')


def _finite_number(value: object, name: str, *, low: float | None = None,
                   high: float | None = None) -> None:
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float, np.number)):
        _fail(f'{name} must be a finite number.')
    if not np.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        _fail(f'{name} is outside its valid range.')


def _array(record: _Record, name: str, dtype: object, *, ndim: int | None = None,
           last: int | None = None, unit: bool = False) -> np.ndarray:
    try:
        original = np.asarray(getattr(record, name))
        if original.dtype.kind not in 'biuf':
            _fail(f'{name} must contain numeric values.')
        if not np.all(np.isfinite(original)):
            _fail(f'{name} must contain finite values.')
        if dtype == np.int64 and np.any(original != np.floor(original)):
            _fail(f'{name} must contain integers.')
        data = np.array(original, dtype=dtype, order='C', copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        from .errors import ValidationError
        raise ValidationError(f'Invalid {name} array.', code='invalid_array') from exc
    if (ndim is not None and data.ndim != ndim) or (last is not None and (not data.ndim or data.shape[-1] != last)):
        _fail(f'{name} has an invalid shape.')
    if not data.size or not np.all(np.isfinite(data)):
        _fail(f'{name} must be nonempty and finite.')
    if unit and (np.any(data < 0) or np.any(data > 1)):
        _fail(f'{name} must be normalized to [0, 1].')
    # Immutable backing bytes also prevents callers from re-enabling WRITEABLE.
    data = np.frombuffer(data.tobytes(order='C'), dtype=data.dtype).reshape(data.shape)
    object.__setattr__(record, name, data)
    return data


def _validate_record(record: _Record) -> None:
    kind = type(record).__name__
    for f in fields(record):
        value = getattr(record, f.name)
        if f.name in ('path', 'profile_path', 'source_path', 'cube_path', 'metadata_path') and value is not None:
            if not isinstance(value, (str, Path)):
                _fail(f'{f.name} must be a path.')
            object.__setattr__(record, f.name, Path(value))
        if f.name in ('warnings', 'errors', 'reasons', 'capabilities', 'groups'):
            object.__setattr__(record, f.name, tuple(value))
        if f.name in ('metadata', 'provenance', 'tags'):
            pairs = tuple(tuple(pair) for pair in value)
            if any(len(pair) != 2 for pair in pairs):
                _fail(f'{f.name} must contain key/value pairs.')
            if f.name in ('metadata', 'provenance'):
                if any(not isinstance(k, str) or not isinstance(v, str) for k, v in pairs):
                    _fail(f'{f.name} keys and values must be strings.')
                if len({p[0] for p in pairs}) != len(pairs):
                    _fail(f'{f.name} keys must be unique.')
            object.__setattr__(record, f.name, pairs)
    if kind == 'SpaceSpec':
        if record.kind not in ('srgb', 'linear_srgb', 'icc_rgb', 'camera_linear'):
            _fail('Unknown color space.')
        if (record.kind in ('icc_rgb', 'camera_linear')) != (record.profile_path is not None):
            _fail('Profile-based spaces require a path; built-in spaces forbid one.')
    elif kind == 'Progress':
        _finite_number(record.fraction, 'fraction', low=0, high=1)
    elif kind == 'JobContext':
        if record.progress is not None and not callable(record.progress):
            _fail('Progress callback must be callable.')
        if record.cancel is not None and not callable(getattr(record.cancel, 'is_cancelled', None)):
            _fail('Cancellation token must implement is_cancelled().')
    elif kind == 'Capability':
        if record.status not in ('supported', 'partial', 'unsupported', 'available', 'missing', 'verified', 'unverified'):
            _fail('Unknown capability status.')
    elif kind == 'ProfileInfo':
        if record.format not in ('icc', 'dcp'):
            _fail('Unknown profile format.')
        if any(not isinstance(c, Capability) for c in record.capabilities):
            _fail('Capabilities must contain Capability records.')
    elif kind == 'DcpProfile':
        if not isinstance(record.info, ProfileInfo) or record.info.format != 'dcp':
            _fail('DCP profile requires DCP metadata.')
        object.__setattr__(record, 'tags', tuple((tag, tuple(value) if isinstance(value, (list, np.ndarray)) else value) for tag, value in record.tags))
    elif kind == 'DcpSettings':
        if record.gamut not in ('clip', 'error'):
            _fail('Unknown gamut policy.')
        if record.camera_neutral is not None:
            neutral = tuple(record.camera_neutral)
            if len(neutral) != 3:
                _fail('Camera neutral must have three components.')
            for value in neutral:
                _finite_number(value, 'camera_neutral', low=0)
                if value == 0:
                    _fail('Camera neutral must be positive.')
            object.__setattr__(record, 'camera_neutral', neutral)
        if record.illuminant_kelvin is not None:
            _finite_number(record.illuminant_kelvin, 'illuminant_kelvin', low=1)
        for name in ('include_hue_sat_map', 'include_look_table', 'include_tone_curve'):
            if not isinstance(getattr(record, name), bool):
                _fail(f'{name} must be boolean.')
    elif kind == 'TransformRequest':
        if not isinstance(record.input_space, SpaceSpec) or not isinstance(record.output_space, SpaceSpec):
            _fail('Transform spaces must be SpaceSpec records.')
        if record.mode not in ('technical', 'look') or record.intent not in ('perceptual', 'relative', 'saturation', 'absolute') or record.gamut not in ('clip', 'error'):
            _fail('Unknown transform mode, intent or gamut policy.')
        if record.look_mode not in ('colors_only', 'tone_only', 'look_only', 'combined'):
            _fail('Unknown look mode.')
        if record.dcp is not None and not isinstance(record.dcp, DcpSettings):
            _fail('DCP settings must be a DcpSettings record.')
        if not isinstance(record.black_point_compensation, bool):
            _fail('Black point compensation must be boolean.')
    elif kind == 'Lut3D':
        data = _array(record, 'values', np.float32, ndim=4, last=3, unit=True)
        if len(set(data.shape[:3])) != 1 or data.shape[0] < 2:
            _fail('LUT grid must be cubic with at least two points per axis.')
        if not isinstance(record.input_space, SpaceSpec) or not isinstance(record.output_space, SpaceSpec):
            _fail('LUT spaces must be SpaceSpec records.')
        _finite_number(record.strength, 'strength', low=0, high=1)
        if record.strength != 1 and record.input_space != record.output_space:
            _fail('Strength blending requires equal input and output spaces.')
    elif kind == 'ValidationReport':
        if not isinstance(record.valid, bool) or not isinstance(record.sample_count, int) or record.sample_count < 0:
            _fail('Validation report requires a boolean status and nonnegative sample count.')
        _finite_number(record.minimum, 'minimum')
        _finite_number(record.maximum, 'maximum')
        if record.minimum > record.maximum:
            _fail('Validation report minimum exceeds maximum.')
    elif kind == 'ExportResult':
        if not isinstance(record.validation, ValidationReport):
            _fail('Export result requires a ValidationReport.')
    elif kind == 'RawRecipe':
        if not isinstance(record.use_camera_wb, bool) or record.demosaic != 'AHD':
            _fail('Invalid RAW recipe.')
        if record.user_wb is not None:
            wb = tuple(record.user_wb)
            if record.use_camera_wb or len(wb) != 4:
                _fail('User WB requires four multipliers and camera WB disabled.')
            for value in wb:
                _finite_number(value, 'user_wb', low=0)
                if value == 0:
                    _fail('WB multipliers must be positive.')
            object.__setattr__(record, 'user_wb', wb)
        elif not record.use_camera_wb:
            _fail('Camera WB or explicit user WB is required.')
        _finite_number(record.exposure_shift, 'exposure_shift', low=0.25, high=8)
    elif kind == 'ImageData':
        _array(record, 'rgb', np.float32, ndim=3, last=3, unit=True)
    elif kind == 'SamplePairs':
        source = _array(record, 'source_rgb', np.float32, ndim=2, last=3, unit=True)
        target = _array(record, 'target_rgb', np.float32, ndim=2, last=3, unit=True)
        if source.shape != target.shape or len(record.groups) != len(source):
            _fail('Paired samples and group count must agree.')
        if any(not isinstance(g, str) or not g for g in record.groups) or not isinstance(record.evidence, str) or not record.evidence.strip():
            _fail('Samples require nonempty group identifiers and evidence.')
    elif kind == 'MatchSettings':
        if record.fit not in ('ccm', 'ccm_tone', 'ccm_tone_lut') or record.grid_size not in (5, 9, 17):
            _fail('Unknown fit or unsupported fitting grid size.')
        _finite_number(record.regularization, 'regularization', low=0)
        _finite_number(record.holdout_fraction, 'holdout_fraction', low=0, high=1)
        if record.regularization == 0 or not 0 < record.holdout_fraction < 1:
            _fail('Regularization must be positive and holdout fraction strictly between zero and one.')
        if not isinstance(record.seed, int) or isinstance(record.seed, bool) or record.seed < 0:
            _fail('Seed must be a nonnegative integer.')
    elif kind == 'ErrorStats':
        if not isinstance(record.count, int) or record.count < 1:
            _fail('Error statistics require a positive sample count.')
        for name in ('mean', 'median', 'p95', 'maximum'):
            _finite_number(getattr(record, name), name, low=0)
    elif kind == 'MatchReport':
        train = _array(record, 'train_indices', np.int64, ndim=1)
        holdout = _array(record, 'holdout_indices', np.int64, ndim=1)
        if np.any(train < 0) or np.any(holdout < 0) or np.intersect1d(train, holdout).size:
            _fail('Training and holdout indices must be nonnegative and disjoint.')
        if len(np.unique(train)) != len(train) or len(np.unique(holdout)) != len(holdout):
            _fail('Split indices must be unique.')
    elif kind == 'MatchModel':
        ccm = _array(record, 'ccm', np.float64, ndim=2)
        tone = _array(record, 'tone_curve', np.float64, ndim=2, last=4, unit=True)
        residual = _array(record, 'residual', np.float32, ndim=4, last=3)
        if ccm.shape != (3, 3) or len(tone) < 2 or np.any(np.diff(tone[:, 0]) <= 0) or tone[0, 0] != 0 or tone[-1, 0] != 1:
            _fail('Invalid CCM or tone knots.')
        if len(set(residual.shape[:3])) != 1 or residual.shape[0] < 2:
            _fail('Residual must have a cubic grid.')
    elif kind == 'AlignmentResult':
        source = _array(record, 'source_aligned', np.float32, ndim=3, last=3, unit=True)
        target = _array(record, 'target_rgb', np.float32, ndim=3, last=3, unit=True)
        mask = _array(record, 'valid_mask', np.bool_, ndim=2)
        homography = _array(record, 'homography', np.float64, ndim=2)
        if source.shape != target.shape or mask.shape != source.shape[:2] or homography.shape != (3, 3):
            _fail('Invalid alignment dimensions.')
        _finite_number(record.inlier_fraction, 'inlier_fraction', low=0, high=1)
