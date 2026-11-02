"""LUT health check: load any 3D LUT file, diagnose it, and (only on request) repair it.

Headless module (no Qt).  Public entry points

* :func:`load_lut_file`, :func:`to_lut3d`, :func:`from_lut3d`   -- any .cube/.3dl/Hald/strip LUT <-> engine LUT
* :func:`diagnose_lut`                                           -- eight findings + input-kind estimate
* :func:`repair_lut`                                             -- explicit, never automatic

All metrics are computed on the LUT's own grid (no resampling) with vectorised NumPy.
Colour differences are measured in Oklab via :func:`image_look.srgb_to_oklab` (encoded sRGB
in [0, 1]; values outside are clipped for the Oklab conversion only).  Every threshold is a
module constant, collected in :data:`THRESHOLDS` and recorded in the report provenance.

What ``HealthFinding.value`` / ``threshold`` mean (``value`` is meant to be shown to the user as is;
``value > threshold`` is the warning condition unless noted):

=============== ====================================================== ===================== ==========
code            ``value``                                              unit                  threshold
=============== ====================================================== ===================== ==========
range           share of nodes outside [0, 1] or not finite            fraction of nodes     0
tone_reversal   share of grid edges whose Oklab L falls by > 0.002     fraction of edges     0.005 (*)
gray_tint       largest Oklab chroma on the grey diagonal              Oklab chroma          0.02 (**)
clipping        share of nodes pinned to 0 / 1 while the input is not  fraction of nodes     0.05
tone_jump       largest Oklab step of an unusual bend (see below)      Oklab L (black=0,     0.03
                                                                       white=1)
hue_fold        number of grid edges whose hue turns by > 60 degrees   edges                 0
banding_risk    steepest local gain: Oklab distance of the output per  ratio (output/input)  4
                Oklab distance of the input, along a grid edge
flatten         share of grid cells whose Jacobian determinant is      fraction of cells     0.02
                below 1e-3 of the median (colours collapse)
=============== ====================================================== ===================== ==========

(*) also a warning for any reversal on the grey diagonal, whatever the share.
(**) the grey tint itself is only 'info' (creative LUTs tint on purpose); the threshold applies to the
largest second difference of the grey-axis (a, b) path, reported in ``details['max_second_difference']``.

``tone_jump`` is judged per node from the second difference of the output along each axis: it is a jump
when it is unusual for this LUT (robust ratio = |second difference| / MAD of all second differences > 12,
``details['robust_ratio']``) *and* it moves the node by more than 0.03 in Oklab.  ``value`` is that Oklab
step (the largest among the unusual nodes, 0 when there are none); for a pure lightness jump it equals the
Oklab-L step, ``details['step_L_component']`` gives the lightness part of the worst step.  Because the
step is only taken from unusual nodes, ``value > 0.03`` is exactly the warning condition.

Log curves used by the input-kind estimate (scene-linear reflectance, 18 % grey = 0.18 in,
non-linear code value 0..1 out; constants copied from the manufacturers' public documents
and cross-checked against the colour-science 0.4.6 reference implementations):

* S-Log3      Sony, "Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3"
              http://community.sony.com/sony/attachments/sony/large-sensor-camera-F5-F55/12359/2/TechnicalSummary_for_S-Gamut3Cine_S-Gamut3_S-Log3_V1_00.pdf
* V-Log       Panasonic, "VARICAM V-Log/V-Gamut" (2014)
              http://pro-av.panasonic.net/en/varicam/common/pdf/VARICAM_V-Log_V-Gamut.pdf
* F-Log       Fujifilm, "F-Log Data Sheet Ver.1.1"
              https://dl.fujifilm-x.com/support/lut/F-Log_DataSheet_E_Ver.1.1.pdf
* F-Log2      Fujifilm, "F-Log2 Data Sheet Ver.1.0"
              https://dl.fujifilm-x.com/support/lut/F-Log2_DataSheet_E_Ver.1.0.pdf
* N-Log       Nikon, "N-Log Specification Document Version 1.0.0"
              http://download.nikonimglib.com/archive3/hDCmK00m9JDI03RPruD74xpoU905/N-Log_Specification_(En)01.pdf
* C-Log3      Canon, "Canon Log Gamma Curves" white paper and the Cinema Gamut/Canon Log 3 input
              transform v1.2 (2020): https://www.usa.canon.com/content/dam/canon-assets/white-papers/pro/white-paper-canon-log-gamma-curves.pdf
              (the reflectance input is divided by 0.9 so that 18 % grey encodes to 34.3 %)
* ARRI LogC3  ARRI, "ALEXA Log C Curve - Usage in VFX", EI 800 parameter set
              https://www.arri.com/resource/blob/31918/66f56e6abb6e5b6553929edf9aa7483e/2017-03-alexa-logc-curve-in-vfx-data.pdf
* Apple Log   Apple, "Apple Log Profile White Paper" (2023)
              https://developer.apple.com/download/all/?q=Apple%20log%20profile
* Rec.709 OETF used as the nominal display encoding: ITU-R BT.709-6, table 1.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import functools
import json
import math
from pathlib import Path
import struct
import warnings
import zlib

import numpy as np

from .errors import ValidationError, checkpoint
from .image_look import srgb_to_oklab
from .lut import _SIZES, _tetrahedral, decode_png
from .types import JobContext, Lut3D, SpaceSpec

SCHEMA = 'lut_health/1'
MIN_SIZE, MAX_SIZE = 2, 129
FINDING_CODES = ('range', 'tone_reversal', 'gray_tint', 'clipping', 'tone_jump',
                 'hue_fold', 'banding_risk', 'flatten')
SEVERITIES = ('ok', 'info', 'warning', 'error')
SOURCE_FORMATS = ('cube', '3dl', 'hald_png', 'strip_png', 'cube_cps')
KIND_NAMES = ('identity', 'display', 'log_to_display', 'display_to_log', 'unknown')
KIND_NOTE_KEYS = {
    'identity': 'health.kind_note.identity',
    'display': 'health.kind_note.display_709_srgb',
    'log_to_display': 'health.kind_note.log_to_display',
    'display_to_log': 'health.kind_note.display_to_log',
    'unknown': 'health.kind_note.unknown',
}
# Node flag bits stored in HealthReport.hue_samples[:, 4].
FLAG_BITS = {'range': 1, 'tone_reversal': 2, 'clipping': 4, 'tone_jump': 8,
             'hue_fold': 16, 'banding_risk': 32, 'flatten': 64}
GRAY_SAMPLES = 257
MAX_LOCATIONS = 64
_MAX_FILE_BYTES = 256 * 1024 * 1024

# ---- thresholds (all recorded in HealthReport.provenance) --------------------------------------
RANGE_TOLERANCE = 1e-6          # values within this of [0, 1] are not "outside"
REVERSAL_DL = 0.002             # Oklab L decrease per edge that counts as a reversal
REVERSAL_EDGE_FRACTION = 0.005  # warn when more than this fraction of edges reverse
GRAY_TINT_INFO_CHROMA = 0.005   # grey-axis Oklab chroma above which the tint is reported (info)
GRAY_TINT_D2 = 0.02             # warn when the grey-axis (a, b) second difference exceeds this
CLIP_EPSILON = 5e-4             # output within this of 0/1 counts as sitting on the bound
CLIP_FRACTION = 0.05            # warn when more than this fraction of nodes are clipped
TONE_JUMP_RATIO = 12.0          # robust ratio (second difference / MAD) ...
TONE_JUMP_ABS = 0.03            # ... and absolute second difference (Oklab units) to warn
TONE_JUMP_MAD_FLOOR = 1e-5      # lower bound for the MAD so exactly smooth LUTs do not divide by 0
HUE_FOLD_DEGREES = 60.0         # relative hue rotation across one edge
HUE_FOLD_MIN_CHROMA = 0.04      # output chroma required at both ends of the edge
HUE_FOLD_MIN_INPUT_CHROMA = 0.03  # input chroma needed to credit the input's own hue change
BANDING_GAIN = 4.0              # output Oklab distance per input Oklab distance
FLATTEN_DET_RATIO = 1e-3        # |det J| below this fraction of the median counts as flat
FLATTEN_DET_FLOOR = 1e-2        # ... where the median is floored at this fraction of the identity volume
FLATTEN_CELL_FRACTION = 0.02    # warn when more than this fraction of cells is flat
IDENTITY_TOLERANCE = 0.003      # max |out - in| (encoded RGB) of an "identity" LUT
LOG_BLACK_CODE = 0.06           # input level where output first exceeds BLACK_OUTPUT
LOG_SLOPE = 1.4                 # slope at mid (output Oklab L per input Oklab L)
LOG_CHROMA_GAIN = 1.2
LOG_WHITE_CODE = 0.88            # a log -> display LUT reaches white at or below this input level
BLACK_OUTPUT = 0.02
MID_LIGHTNESS = 0.5

THRESHOLDS = {
    'range_tolerance': RANGE_TOLERANCE,
    'reversal_dL': REVERSAL_DL, 'reversal_edge_fraction': REVERSAL_EDGE_FRACTION,
    'gray_tint_info_chroma': GRAY_TINT_INFO_CHROMA, 'gray_tint_second_difference': GRAY_TINT_D2,
    'clip_epsilon': CLIP_EPSILON, 'clip_fraction': CLIP_FRACTION,
    'tone_jump_ratio': TONE_JUMP_RATIO, 'tone_jump_abs': TONE_JUMP_ABS,
    'hue_fold_degrees': HUE_FOLD_DEGREES, 'hue_fold_min_chroma': HUE_FOLD_MIN_CHROMA,
    'banding_gain': BANDING_GAIN,
    'flatten_det_ratio': FLATTEN_DET_RATIO, 'flatten_cell_fraction': FLATTEN_CELL_FRACTION,
    'identity_tolerance': IDENTITY_TOLERANCE,
    'log_black_code': LOG_BLACK_CODE, 'log_slope': LOG_SLOPE, 'log_chroma_gain': LOG_CHROMA_GAIN,
    'log_white_code': LOG_WHITE_CODE,
}


# ------------------------------------------------------------------------------------------------
# Log curves (encode: scene-linear -> code value, decode: code value -> scene-linear)
# ------------------------------------------------------------------------------------------------
def _f64(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _log10(x) -> np.ndarray:
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.log10(np.maximum(x, 1e-300))


def _slog3_encode(x):
    x = _f64(x)
    log = (420.0 + _log10((x + 0.01) / 0.19) * 261.5) / 1023.0
    lin = (x * (171.2102946929 - 95.0) / 0.01125 + 95.0) / 1023.0
    return np.where(x >= 0.01125, log, lin)


def _slog3_decode(y):
    y = _f64(y)
    log = np.power(10.0, (y * 1023.0 - 420.0) / 261.5) * 0.19 - 0.01
    lin = (y * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0)
    return np.where(y >= 171.2102946929 / 1023.0, log, lin)


def _vlog_encode(x):
    x = _f64(x)
    return np.where(x < 0.01, 5.6 * x + 0.125, 0.241514 * _log10(x + 0.00873) + 0.598206)


def _vlog_decode(y):
    y = _f64(y)
    return np.where(y < 0.181, (y - 0.125) / 5.6, np.power(10.0, (y - 0.598206) / 0.241514) - 0.00873)


def _fujifilm(cut1, cut2, a, b, c, d, e, f):
    def encode(x):
        x = _f64(x)
        return np.where(x >= cut1, c * _log10(a * x + b) + d, e * x + f)

    def decode(y):
        y = _f64(y)
        return np.where(y >= cut2, (np.power(10.0, (y - d) / c) - b) / a, (y - f) / e)
    return encode, decode


_flog_encode, _flog_decode = _fujifilm(0.00089, 0.100537775223865, 0.555556, 0.009468, 0.344676,
                                       0.790453, 8.735631, 0.092864)
_flog2_encode, _flog2_decode = _fujifilm(0.000889, 0.100686685370811, 5.555556, 0.064829, 0.245281,
                                         0.384316, 8.799461, 0.092864)


def _nlog_encode(x):
    x = _f64(x)
    with np.errstate(divide='ignore', invalid='ignore'):
        log = (150.0 / 1023.0) * np.log(np.maximum(x, 1e-300)) + 619.0 / 1023.0
    return np.where(x < 0.328, (650.0 / 1023.0) * np.cbrt(x + 0.0075), log)


def _nlog_decode(y):
    y = _f64(y)
    return np.where(y < 452.0 / 1023.0, (y * 1023.0 / 650.0) ** 3 - 0.0075,
                    np.exp((y - 619.0 / 1023.0) * 1023.0 / 150.0))


def _clog3_encode(x):
    x = _f64(x) / 0.9
    low = -0.36726845 * _log10(1.0 - 14.98325 * x) + 0.12783901
    mid = 1.9754798 * x + 0.12512219
    high = 0.36726845 * _log10(14.98325 * x + 1.0) + 0.12240537
    return np.where(x < -0.014, low, np.where(x <= 0.014, mid, high))


def _clog3_decode(y):
    y = _f64(y)
    low = -(np.power(10.0, (0.12783901 - y) / 0.36726845) - 1.0) / 14.98325
    mid = (y - 0.12512219) / 1.9754798
    high = (np.power(10.0, (y - 0.12240537) / 0.36726845) - 1.0) / 14.98325
    return 0.9 * np.where(y < 0.097465473, low, np.where(y <= 0.15277891, mid, high))


_LOGC3 = dict(cut=0.010591, a=5.555556, b=0.052272, c=0.247190, d=0.385537, e=5.367655, f=0.092809)


def _logc3_encode(x):
    x = _f64(x)
    p = _LOGC3
    return np.where(x > p['cut'], p['c'] * _log10(p['a'] * x + p['b']) + p['d'], p['e'] * x + p['f'])


def _logc3_decode(y):
    y = _f64(y)
    p = _LOGC3
    return np.where(y > p['e'] * p['cut'] + p['f'],
                    (np.power(10.0, (y - p['d']) / p['c']) - p['b']) / p['a'], (y - p['f']) / p['e'])


_APPLE = dict(r0=-0.05641088, rt=0.01, c=47.28711236, beta=0.00964052, gamma=0.08550479, delta=0.69336945)


def _apple_encode(x):
    x = _f64(x)
    p = _APPLE
    with np.errstate(invalid='ignore'):
        log = p['gamma'] * np.log2(np.maximum(x + p['beta'], 1e-300)) + p['delta']
    quad = p['c'] * np.maximum(x - p['r0'], 0.0) ** 2
    return np.where(x >= p['rt'], log, np.where(x >= p['r0'], quad, 0.0))


def _apple_decode(y):
    y = _f64(y)
    p = _APPLE
    knee = p['c'] * (p['rt'] - p['r0']) ** 2
    log = np.power(2.0, (y - p['delta']) / p['gamma']) - p['beta']
    quad = np.sqrt(np.maximum(y, 0.0) / p['c']) + p['r0']
    return np.where(y >= knee, log, np.where(y > 0.0, quad, p['r0']))


LOG_CURVES = {
    'S-Log3': (_slog3_encode, _slog3_decode),
    'V-Log': (_vlog_encode, _vlog_decode),
    'F-Log2': (_flog2_encode, _flog2_decode),
    'F-Log': (_flog_encode, _flog_decode),
    'N-Log': (_nlog_encode, _nlog_decode),
    'C-Log3': (_clog3_encode, _clog3_decode),
    'ARRI LogC3': (_logc3_encode, _logc3_decode),
    'Apple Log': (_apple_encode, _apple_decode),
}


def log_encode(curve: str, linear) -> np.ndarray:
    """Scene-linear reflectance (18 % grey = 0.18) -> code value of a published log curve."""
    return LOG_CURVES[curve][0](linear)


def log_decode(curve: str, code) -> np.ndarray:
    """Code value of a published log curve -> scene-linear reflectance (may be negative near black)."""
    return LOG_CURVES[curve][1](code)


def rec709_oetf(linear) -> np.ndarray:
    """ITU-R BT.709 opto-electronic transfer function (nominal display encoding of the estimator)."""
    x = np.maximum(_f64(linear), 0.0)
    return np.where(x < 0.018, 4.5 * x, 1.099 * np.power(x, 0.45) - 0.099)


def rec709_inverse_oetf(code) -> np.ndarray:
    v = np.maximum(_f64(code), 0.0)
    return np.where(v < 0.081, v / 4.5, np.power((v + 0.099) / 1.099, 1.0 / 0.45))


# ------------------------------------------------------------------------------------------------
# Records
# ------------------------------------------------------------------------------------------------
def _fail(message: str, code: str = 'invalid_data') -> None:
    raise ValidationError(message, code=code)


def _frozen_array(value, name: str, dtype, *, ndim: int, last: int | None = None,
                  allow_nonfinite: bool = False) -> np.ndarray:
    """Validated private copy backed by immutable bytes (cannot be made writeable again)."""
    try:
        original = np.asarray(value)
        if original.dtype.kind not in 'biuf':
            _fail(f'{name} must contain numeric values.')
        data = np.array(original, dtype=dtype, order='C', copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f'Invalid {name} array.', code='invalid_array') from exc
    if data.ndim != ndim or (last is not None and data.shape[-1] != last):
        _fail(f'{name} has an invalid shape.')
    if data.dtype.kind == 'f' and not allow_nonfinite and not np.all(np.isfinite(data)):
        _fail(f'{name} must contain finite values.')
    return np.frombuffer(data.tobytes(order='C'), dtype=data.dtype).reshape(data.shape)


def _finite(value, name: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float, np.integer, np.floating)):
        _fail(f'{name} must be a finite number.')
    value = float(value)
    if not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
        _fail(f'{name} is outside its valid range.')
    return value


def _pairs(value, name: str) -> tuple[tuple[str, str], ...]:
    try:
        pairs = tuple((k, v) for k, v in value)
    except (TypeError, ValueError):
        _fail(f'{name} must contain key/value pairs.')
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in pairs):
        _fail(f'{name} keys and values must be strings.')
    return pairs


@dataclass(frozen=True, slots=True)
class LoadedLut:
    """A 3D LUT of any grid size 2..129 as found in a file (values may lie outside [0, 1])."""
    values: np.ndarray            # float64 (n, n, n, 3) indexed [r, g, b], read-only
    size: int
    title: str
    source_format: str            # 'cube' | '3dl' | 'hald_png' | 'strip_png' | 'cube_cps'
    source_path: Path
    domain_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    domain_max: tuple[float, float, float] = (1.0, 1.0, 1.0)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        data = _frozen_array(self.values, 'values', np.float64, ndim=4, last=3, allow_nonfinite=True)
        if data.shape[0] != data.shape[1] or data.shape[0] != data.shape[2] or not data.size:
            _fail('LUT grid must be cubic.')
        if not MIN_SIZE <= data.shape[0] <= MAX_SIZE:
            raise ValidationError(f'LUT grid size {data.shape[0]} is outside {MIN_SIZE}..{MAX_SIZE}.',
                                  code='lut_size_unsupported')
        object.__setattr__(self, 'values', data)
        if isinstance(self.size, bool) or self.size != data.shape[0]:
            _fail('size must equal the grid size of values.')
        object.__setattr__(self, 'size', int(self.size))
        if not isinstance(self.title, str):
            _fail('title must be text.')
        if self.source_format not in SOURCE_FORMATS:
            _fail('Unknown LUT source format.')
        if not isinstance(self.source_path, (str, Path)):
            _fail('source_path must be a path.')
        object.__setattr__(self, 'source_path', Path(self.source_path))
        for name in ('domain_min', 'domain_max'):
            vec = tuple(getattr(self, name))
            if len(vec) != 3:
                _fail(f'{name} must have three components.')
            object.__setattr__(self, name, tuple(_finite(v, name) for v in vec))
        if any(b <= a for a, b in zip(self.domain_min, self.domain_max)):
            _fail('domain_max must exceed domain_min.')
        warns = tuple(self.warnings)
        if any(not isinstance(w, str) for w in warns):
            _fail('warnings must be text.')
        object.__setattr__(self, 'warnings', warns)


@dataclass(frozen=True, slots=True)
class HealthFinding:
    code: str
    severity: str
    value: float
    threshold: float
    count: int
    locations: np.ndarray         # (k, 3) int64 grid indices of the worst nodes, k <= 64
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.code not in FINDING_CODES:
            _fail('Unknown finding code.')
        if self.severity not in SEVERITIES:
            _fail('Unknown severity.')
        object.__setattr__(self, 'value', _finite(self.value, 'value'))
        object.__setattr__(self, 'threshold', _finite(self.threshold, 'threshold'))
        if isinstance(self.count, bool) or not isinstance(self.count, (int, np.integer)) or self.count < 0:
            _fail('count must be a non-negative integer.')
        object.__setattr__(self, 'count', int(self.count))
        loc = _frozen_array(np.asarray(self.locations).reshape(-1, 3) if np.size(self.locations) else
                            np.zeros((0, 3), np.int64), 'locations', np.int64, ndim=2, last=3)
        if len(loc) > MAX_LOCATIONS or (loc.size and loc.min() < 0):
            _fail('locations hold at most 64 non-negative grid indices.')
        object.__setattr__(self, 'locations', loc)
        object.__setattr__(self, 'details', _pairs(self.details, 'details'))


@dataclass(frozen=True, slots=True)
class InputKindEstimate:
    kind: str
    confidence: float
    log_candidates: tuple[tuple[str, float], ...] = ()
    note_key: str = ''

    def __post_init__(self) -> None:
        if self.kind not in KIND_NAMES:
            _fail('Unknown input kind.')
        object.__setattr__(self, 'confidence', _finite(self.confidence, 'confidence', low=0, high=1))
        cands = tuple((str(n), _finite(s, 'score', low=0, high=1)) for n, s in self.log_candidates)
        if len(cands) > 3 or any(n not in LOG_CURVES for n, _ in cands):
            _fail('At most three known log candidates are allowed.')
        object.__setattr__(self, 'log_candidates', cands)
        if not isinstance(self.note_key, str) or not self.note_key:
            object.__setattr__(self, 'note_key', KIND_NOTE_KEYS[self.kind])


@dataclass(frozen=True, slots=True)
class HealthReport:
    size: int
    findings: tuple[HealthFinding, ...]
    kind: InputKindEstimate
    gray_axis: np.ndarray         # (257, 7): input t, out R, G, B, Oklab L, a, b
    hue_samples: np.ndarray       # (k, 6): input a, b ; output a, b ; flag bits ; output chroma
    overall: str
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.size, bool) or not isinstance(self.size, (int, np.integer)) or not MIN_SIZE <= self.size <= MAX_SIZE:
            _fail('Invalid report size.')
        object.__setattr__(self, 'size', int(self.size))
        findings = tuple(self.findings)
        if tuple(f.code for f in findings if isinstance(f, HealthFinding)) != FINDING_CODES or len(findings) != len(FINDING_CODES):
            _fail('A report holds exactly one finding per code, in the documented order.')
        object.__setattr__(self, 'findings', findings)
        if not isinstance(self.kind, InputKindEstimate):
            _fail('kind must be an InputKindEstimate.')
        object.__setattr__(self, 'gray_axis', _frozen_array(self.gray_axis, 'gray_axis', np.float64, ndim=2, last=7))
        object.__setattr__(self, 'hue_samples', _frozen_array(self.hue_samples, 'hue_samples', np.float64, ndim=2, last=6))
        worst = SEVERITIES[max(SEVERITIES.index(f.severity) for f in findings)]
        if self.overall != worst:
            _fail('overall must be the worst finding severity.')
        pairs = _pairs(self.provenance, 'provenance')
        if len({k for k, _ in pairs}) != len(pairs):
            _fail('provenance keys must be unique.')
        object.__setattr__(self, 'provenance', pairs)

    def finding(self, code: str) -> HealthFinding:
        for item in self.findings:
            if item.code == code:
                return item
        raise KeyError(code)


@dataclass(frozen=True, slots=True)
class RepairSettings:
    smooth: float = 0.0           # 0..1 global Laplacian smoothing of the residual (out - identity)
    fix_jumps: bool = False       # local smoothing around tone_jump / hue_fold nodes (+1 ring)
    round_clip: bool = False      # local smoothing at the border of clipped plateaus
    lock_gray: bool = False       # grey inputs -> neutral output (keeps Oklab L)
    iterations: int = 8           # 1..64

    def __post_init__(self) -> None:
        object.__setattr__(self, 'smooth', self._check(_finite, 'smooth', self.smooth, low=0, high=1))
        for name in ('fix_jumps', 'round_clip', 'lock_gray'):
            if not isinstance(getattr(self, name), bool):
                raise ValidationError(f'{name} must be boolean.', code='invalid_repair_settings')
        if isinstance(self.iterations, bool) or not isinstance(self.iterations, (int, np.integer)) \
                or not 1 <= self.iterations <= 64:
            raise ValidationError('iterations must be an integer in 1..64.', code='invalid_repair_settings')
        object.__setattr__(self, 'iterations', int(self.iterations))

    @staticmethod
    def _check(check, name, value, **limits):
        try:
            return check(value, name, **limits)
        except ValidationError as exc:
            raise ValidationError(exc.message, code='invalid_repair_settings') from exc

    @property
    def is_default(self) -> bool:
        return self.smooth == 0.0 and not (self.fix_jumps or self.round_clip or self.lock_gray)


@dataclass(frozen=True, slots=True)
class RepairResult:
    lut: LoadedLut
    change: tuple[float, float, float]     # DeltaE2000 mean, p99, max over the grid (before vs after)
    before: HealthReport
    after: HealthReport

    def __post_init__(self) -> None:
        if not isinstance(self.lut, LoadedLut) or not isinstance(self.before, HealthReport) \
                or not isinstance(self.after, HealthReport):
            _fail('Repair result requires a LoadedLut and two HealthReport records.')
        change = tuple(_finite(v, 'change', low=0) for v in self.change)
        if len(change) != 3:
            _fail('change holds mean, p99 and max.')
        object.__setattr__(self, 'change', change)


# ------------------------------------------------------------------------------------------------
# Loading
# ------------------------------------------------------------------------------------------------
def _unreadable(path: Path, why: str, code: str = 'lut_unreadable') -> ValidationError:
    return ValidationError(f'Cannot read LUT {path}: {why}', code=code, details=(('path', str(path)),))


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def _parse_numbers(text: str, expected: int, path: Path, what: str) -> np.ndarray:
    """Whitespace separated floats; anything unparsable or a wrong count is an error."""
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        try:
            values = np.fromstring(text, dtype=np.float64, sep=' ')
        except (ValueError, DeprecationWarning) as exc:
            raise _unreadable(path, f'{what} contain text that is not a number') from exc
    if values.size != expected:
        raise _unreadable(path, f'{what} hold {values.size} numbers, expected {expected}')
    return values


def _is_numeric_start(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _value_warnings(values: np.ndarray) -> list[str]:
    result = []
    finite = np.isfinite(values)
    if not finite.all():
        result.append('values_nonfinite')
    if finite.any():
        shown = values[finite]
        if shown.min() < -RANGE_TOLERANCE or shown.max() > 1 + RANGE_TOLERANCE:
            result.append('values_outside_unit')
    return result


def _cube_title(rest: str, fallback: str) -> str:
    rest = rest.strip()
    if rest.startswith('"'):
        try:
            value = json.loads(rest)
            if isinstance(value, str):
                return value
        except ValueError:
            pass
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else rest[1:]
    return rest.split('#', 1)[0].strip() or fallback


def _load_cube(path: Path, context: JobContext | None) -> LoadedLut:
    text = _decode_text(path.read_bytes())
    lines = text.splitlines()
    title, size3, size1, cps = path.stem, None, None, False
    domain_min, domain_max = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    start = None
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('#'):
            cps = cps or line.startswith('# CPS_')
            continue
        key = line.split(None, 1)[0]
        if _is_numeric_start(key):
            start = index
            break
        rest = line[len(key):]
        upper = key.upper()
        if upper == 'TITLE':
            title = _cube_title(rest, title)
            continue
        parts = rest.split('#', 1)[0].split()
        try:
            if upper in ('LUT_3D_SIZE', 'LUT_1D_SIZE'):
                if len(parts) != 1:
                    raise ValueError('size needs one integer')
                value = int(parts[0])
                if upper == 'LUT_3D_SIZE':
                    size3 = value
                else:
                    size1 = value
            elif upper in ('DOMAIN_MIN', 'DOMAIN_MAX'):
                vec = tuple(float(v) for v in parts)
                if len(vec) != 3:
                    raise ValueError('domain needs three numbers')
                if upper == 'DOMAIN_MIN':
                    domain_min = vec
                else:
                    domain_max = vec
            elif upper in ('LUT_3D_INPUT_RANGE', 'LUT_1D_INPUT_RANGE'):
                low, high = (float(v) for v in parts)
                if upper == 'LUT_3D_INPUT_RANGE':
                    domain_min, domain_max = (low,) * 3, (high,) * 3
        except ValueError as exc:
            raise _unreadable(path, f'bad {upper} line ({exc})') from exc
    checkpoint(context, stage='health.load', fraction=.2)
    if size3 is None:
        if size1 is not None:
            raise _unreadable(path, 'the file holds only a 1D LUT (LUT_1D_SIZE) and no 3D table', 'lut_1d_only')
        raise _unreadable(path, 'LUT_3D_SIZE is missing')
    if not MIN_SIZE <= size3 <= MAX_SIZE:
        raise _unreadable(path, f'LUT_3D_SIZE {size3} is outside {MIN_SIZE}..{MAX_SIZE}', 'lut_size_unsupported')
    if size1 is not None and not 2 <= size1 <= 65536:
        raise _unreadable(path, f'LUT_1D_SIZE {size1} is not usable')
    if start is None:
        raise _unreadable(path, 'no table data found')
    body = '\n'.join(lines[start:])
    if '#' in body:
        body = '\n'.join(line.split('#', 1)[0] for line in lines[start:])
    shaper = size1 or 0
    numbers = _parse_numbers(body, 3 * (shaper + size3 ** 3), path, 'table rows')
    checkpoint(context, stage='health.load', fraction=.8)
    rows = numbers.reshape(-1, 3)[shaper:]
    values = rows.reshape(size3, size3, size3, 3).transpose(2, 1, 0, 3)   # file: red fastest
    found = []
    if shaper:
        found.append('shaper_1d_ignored')
    if domain_min != (0.0, 0.0, 0.0) or domain_max != (1.0, 1.0, 1.0):
        found.append('domain_not_unit')
    found += _value_warnings(values)
    return LoadedLut(values, size3, title, 'cube_cps' if cps else 'cube', path, domain_min, domain_max, tuple(found))


def _load_3dl(path: Path, context: JobContext | None) -> LoadedLut:
    text = _decode_text(path.read_bytes())
    lines = text.splitlines()
    out_bits, start, breakpoints = None, None, None
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        tokens = line.split()
        low = tokens[0].lower()
        if low == 'mesh':
            try:
                out_bits = int(tokens[2])
            except (IndexError, ValueError) as exc:
                raise _unreadable(path, 'bad Mesh header line') from exc
            continue
        if not _is_numeric_start(tokens[0]):
            continue            # 3DMESH and other vendor header words
        try:
            breakpoints = [float(t) for t in tokens]
        except ValueError as exc:
            raise _unreadable(path, 'the input breakpoint line is not numeric') from exc
        start = index + 1
        break
    if breakpoints is None:
        raise _unreadable(path, 'no input breakpoint line found')
    n = len(breakpoints)
    if not MIN_SIZE <= n <= MAX_SIZE:
        raise _unreadable(path, f'{n} input breakpoints are outside {MIN_SIZE}..{MAX_SIZE}', 'lut_size_unsupported')
    if any(b <= a for a, b in zip(breakpoints, breakpoints[1:])):
        raise _unreadable(path, 'the input breakpoints are not increasing')
    checkpoint(context, stage='health.load', fraction=.2)
    body = '\n'.join(lines[start:])
    if '#' in body:
        body = '\n'.join(line.split('#', 1)[0] for line in lines[start:])
    numbers = _parse_numbers(body, 3 * n ** 3, path, '3dl rows')
    checkpoint(context, stage='health.load', fraction=.8)
    peak = float(np.nanmax(numbers)) if np.isfinite(numbers).any() else 1.0
    is_float = any(ch in body for ch in '.eE')
    if out_bits is not None and 1 <= out_bits <= 16:
        denominator = float(2 ** out_bits - 1)
    elif is_float and peak <= 1.0 + 1e-6:
        denominator = 1.0
    elif peak <= 1023:
        denominator = 1023.0
    elif peak <= 4095:
        denominator = 4095.0
    elif peak <= 65535:
        denominator = 65535.0
    else:
        raise _unreadable(path, f'the largest value {peak:g} does not fit 10/12/16 bit')
    values = (numbers / denominator).reshape(n, n, n, 3)        # blue fastest = [r][g][b]
    found = _value_warnings(values)
    return LoadedLut(values, n, path.stem, '3dl', path, warnings=tuple(found))


def _png_info(raw: bytes, path: Path) -> dict:
    """IHDR and tEXt chunks of a PNG without decoding the pixels."""
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise _unreadable(path, 'not a PNG file')
    pos, header, texts = 8, None, {}
    while pos + 8 <= len(raw):
        length, kind = struct.unpack('>I4s', raw[pos:pos + 8])
        data = raw[pos + 8:pos + 8 + length]
        if len(data) != length:
            break
        if kind == b'IHDR' and length == 13:
            header = struct.unpack('>IIBBBBB', data)
        elif kind == b'tEXt' and b'\x00' in data:
            key, _, value = data.partition(b'\x00')
            texts[key.decode('latin-1')] = value.decode('latin-1')
        elif kind == b'IEND':
            break
        pos += 12 + length
    if header is None:
        raise _unreadable(path, 'PNG header missing')
    width, height, bits, colour, _, _, interlace = header
    return dict(width=width, height=height, bits=bits, colour=colour, interlace=interlace, texts=texts)


def _png_pixels(raw: bytes, info: dict, path: Path) -> np.ndarray:
    """RGB pixels (H, W, 3) as uint8/uint16 from any PNG Pillow/OpenCV can read."""
    try:
        return decode_png(raw)      # exact, fast path for the PNGs this program writes
    except ValidationError:
        pass
    if info['bits'] == 16:
        try:
            import cv2
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)
        except Exception as exc:    # noqa: BLE001 - any decoder failure is "unreadable"
            raise _unreadable(path, f'16-bit PNG could not be decoded ({exc})') from exc
        if image is None or image.ndim != 3 or image.shape[2] < 3 or image.dtype != np.uint16:
            raise _unreadable(path, '16-bit PNG must be RGB')
        return np.ascontiguousarray(image[..., 2::-1])      # BGR(A) -> RGB
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as picture:
            return np.asarray(picture.convert('RGB'), dtype=np.uint8)
    except Exception as exc:        # noqa: BLE001
        raise _unreadable(path, f'PNG could not be decoded ({exc})') from exc


def _tiff_pixels(path: Path) -> np.ndarray:
    try:
        import tifffile
        image = np.asarray(tifffile.imread(str(path)))
    except Exception as exc:        # noqa: BLE001
        raise _unreadable(path, f'TIFF could not be decoded ({exc})') from exc
    while image.ndim > 3:
        image = image[0]
    if image.ndim == 3 and image.shape[0] in (3, 4) and image.shape[-1] not in (3, 4):
        image = np.moveaxis(image, 0, -1)
    if image.ndim != 3 or image.shape[-1] not in (3, 4):
        raise _unreadable(path, 'TIFF must be an RGB image', 'lut_png_layout')
    return np.ascontiguousarray(image[..., :3])


def _image_geometry(height: int, width: int, path: Path) -> tuple[str, int]:
    if height == width and height >= 8:
        level = int(round(height ** (1 / 3)))
        if level ** 3 == height:
            n = level * level
            if n > MAX_SIZE:
                raise _unreadable(path, f'Hald level {level} (grid {n}) exceeds {MAX_SIZE}', 'lut_size_unsupported')
            return 'hald', n
    if height >= 2 and width == height * height:
        if height > MAX_SIZE:
            raise _unreadable(path, f'strip height {height} exceeds {MAX_SIZE}', 'lut_size_unsupported')
        return 'strip', height
    raise _unreadable(path, f'{width}x{height} pixels is neither a Hald CLUT (side = level^3) nor a strip (width = height^2)',
                      'lut_png_layout')


def _load_image(path: Path, suffix: str, context: JobContext | None) -> LoadedLut:
    raw = path.read_bytes() if suffix == '.png' else b''
    info = _png_info(raw, path) if suffix == '.png' else {'texts': {}}
    if suffix == '.png':
        if info['interlace']:
            raise _unreadable(path, 'interlaced PNG LUTs are not supported')
        layout = _image_geometry(info['height'], info['width'], path)      # before decoding the pixels
        image = _png_pixels(raw, info, path)
    else:
        image = _tiff_pixels(path)
        layout = _image_geometry(image.shape[0], image.shape[1], path)
    checkpoint(context, stage='health.load', fraction=.6)
    kind, n = layout
    if image.dtype == np.uint8:
        scaled = image.astype(np.float64) / 255.0
    elif image.dtype == np.uint16:
        scaled = image.astype(np.float64) / 65535.0
    elif image.dtype.kind == 'f':
        scaled = image.astype(np.float64)
    else:
        raise _unreadable(path, f'pixel type {image.dtype} is not supported')
    found = []
    if kind == 'hald':
        values = scaled.reshape(n, n, n, 3).transpose(2, 1, 0, 3)           # index = r + n*g + n^2*b
        source = 'hald_png'
    else:
        flipped = 'green increases upwards' in info['texts'].get('Description', '')
        if flipped:
            scaled = scaled[::-1]
            found.append('strip_green_flipped')
        elif not info['texts'].get('Description', '').startswith('Camera Profile Studio'):
            found.append('strip_orientation_assumed')
        values = scaled.reshape(n, n, n, 3).transpose(2, 0, 1, 3)           # pixel (x = b*n + r, y = g)
        source = 'strip_png'
    found += _value_warnings(values)
    title = info['texts'].get('Title') or path.stem
    return LoadedLut(values, n, title, source, path, warnings=tuple(found))


def load_lut_file(path, *, context: JobContext | None = None) -> LoadedLut:
    """Load a .cube, .3dl, Hald or strip PNG/TIFF LUT of any grid size 2..129.

    Values outside [0, 1] are kept (warning ``values_outside_unit``) so they can be diagnosed.
    Errors: ``lut_unreadable``, ``lut_1d_only``, ``lut_png_layout``, ``lut_size_unsupported``.
    """
    checkpoint(context, stage='health.load', fraction=0)
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValidationError('A LUT file path is required.', code='lut_unreadable')
    path = Path(path)
    try:
        if not path.is_file():
            raise _unreadable(path, 'the file does not exist')
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise _unreadable(path, 'the file is larger than 256 MiB')
        suffix = path.suffix.lower()
        if suffix == '.cube':
            loaded = _load_cube(path, context)
        elif suffix == '.3dl':
            loaded = _load_3dl(path, context)
        elif suffix in ('.png', '.tif', '.tiff'):
            loaded = _load_image(path, suffix, context)
        else:
            raise _unreadable(path, 'supported files are .cube, .3dl, .png, .tif and .tiff')
    except OSError as exc:
        raise _unreadable(path, str(exc)) from exc
    checkpoint(context, stage='health.load', fraction=1)
    return loaded


# ------------------------------------------------------------------------------------------------
# Conversion to / from the engine's Lut3D
# ------------------------------------------------------------------------------------------------
def _identity_grid(n: int) -> np.ndarray:
    axis = np.linspace(0.0, 1.0, n)
    return np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)


def _nearest_size(n: int) -> int:
    return min(_SIZES, key=lambda s: (abs(s - n), -s))


def from_lut3d(lut: Lut3D) -> LoadedLut:
    """Wrap an in-app LUT so it can be diagnosed or repaired."""
    if not isinstance(lut, Lut3D):
        raise ValidationError('Expected a Lut3D.', code='invalid_lut')
    return LoadedLut(np.asarray(lut.values, dtype=np.float64), int(lut.values.shape[0]), lut.title, 'cube',
                     Path('(in-app LUT)'))


def to_lut3d(loaded: LoadedLut, size: int | None = None, *, input_space: SpaceSpec = SpaceSpec('srgb'),
             output_space: SpaceSpec = SpaceSpec('srgb'), title: str | None = None,
             context: JobContext | None = None) -> Lut3D:
    """Resample (tetrahedral, ``lut._tetrahedral``) to 17/33/65 and clip to [0, 1].

    ``size`` defaults to the nearest supported size.  Non-finite nodes are replaced by the
    identity before resampling.  A file domain other than 0..1 is not applied.
    """
    checkpoint(context, stage='health.to_lut3d', fraction=0)
    if not isinstance(loaded, LoadedLut):
        raise ValidationError('Expected a LoadedLut.', code='invalid_lut')
    if size is None:
        target = _nearest_size(loaded.size)
    elif type(size) is int and size in _SIZES:
        target = size
    else:
        raise ValidationError('LUT size must be 17, 33 or 65', code='invalid_lut_size')
    n = loaded.size
    values = loaded.values
    finite = np.isfinite(values).all(axis=-1, keepdims=True)
    if not finite.all():
        values = np.where(finite, values, _identity_grid(n))
    if target == n:
        out = np.clip(values, 0.0, 1.0)
    else:
        table = np.ascontiguousarray(values.reshape(-1, 3))
        strides = np.array([n * n, n, 1], dtype=np.intp)
        axis = np.linspace(0.0, 1.0, target)
        points = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1).reshape(-1, 3) * (n - 1)
        out = np.empty_like(points)
        chunk = 32768
        for start in range(0, len(points), chunk):
            out[start:start + chunk] = _tetrahedral(table, strides, points[start:start + chunk], n)
            checkpoint(context, stage='health.to_lut3d', fraction=min(1.0, (start + chunk) / len(points)))
        out = np.clip(out, 0.0, 1.0).reshape(target, target, target, 3)
    provenance = (('imported_from', loaded.source_path.name), ('source_format', loaded.source_format),
                  ('source_size', str(n)),
                  ('resampling', 'none' if target == n else f'tetrahedral from {n}^3 to {target}^3'),
                  ('import_warnings', ','.join(loaded.warnings) or 'none'), ('jpeg_accuracy', 'unverified'))
    return Lut3D(out.astype(np.float32), input_space, output_space, loaded.title if title is None else title,
                 1.0, provenance)


# ------------------------------------------------------------------------------------------------
# Diagnosis
# ------------------------------------------------------------------------------------------------
class _Ctx:
    """Per-diagnosis scratch data (never exposed)."""
    __slots__ = ('n', 'values', 'clean', 'finite', 'grid', 'in_lab', 'in_chroma', 'in_hue', 'in_dist',
                 'out_lab', 'out_chroma', 'out_hue', 'flags')


@functools.lru_cache(maxsize=2)
def _input_reference(n: int):
    grid = _identity_grid(n)
    lab = srgb_to_oklab(grid)
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = np.arctan2(lab[..., 2], lab[..., 1])
    dist = []
    for axis in range(3):
        lo, hi = _lo_hi(lab, axis)
        dist.append(np.sqrt(np.sum((hi - lo) ** 2, axis=-1)))
    for array in (grid, lab, chroma, hue, *dist):
        array.flags.writeable = False
    return grid, lab, chroma, hue, tuple(dist)


def _lo_hi(array: np.ndarray, axis: int):
    lo = [slice(None)] * 3
    hi = [slice(None)] * 3
    lo[axis], hi[axis] = slice(0, -1), slice(1, None)
    return array[tuple(lo)], array[tuple(hi)]


def _wrap(angle):
    return (angle + np.pi) % (2 * np.pi) - np.pi


def _prepare(values: np.ndarray) -> _Ctx:
    c = _Ctx()
    c.n = n = values.shape[0]
    c.values = values
    c.finite = np.isfinite(values).all(axis=-1)
    c.grid, c.in_lab, c.in_chroma, c.in_hue, c.in_dist = _input_reference(n)
    c.clean = values if c.finite.all() else np.where(c.finite[..., None], values, c.grid)
    c.out_lab = srgb_to_oklab(np.clip(c.clean, 0.0, 1.0))
    c.out_chroma = np.hypot(c.out_lab[..., 1], c.out_lab[..., 2])
    c.out_hue = np.arctan2(c.out_lab[..., 2], c.out_lab[..., 1])
    c.flags = np.zeros((n, n, n), np.uint8)
    return c


def _top_locations(n: int, coords, scores, limit: int = MAX_LOCATIONS) -> np.ndarray:
    """The ``limit`` highest-scoring distinct nodes (ties by grid order), deterministic."""
    coords = np.asarray(coords, dtype=np.int64).reshape(-1, 3)
    if not len(coords):
        return np.zeros((0, 3), np.int64)
    scores = np.asarray(scores, dtype=np.float64).reshape(-1)
    flat = (coords[:, 0] * n + coords[:, 1]) * n + coords[:, 2]
    order = np.lexsort((flat, -scores))
    _, first = np.unique(flat[order], return_index=True)
    return coords[order[np.sort(first)[:limit]]]


def _fmt(value: float) -> str:
    return f'{float(value):.6g}'


def _make(code, severity, value, threshold, count, locations, details):
    return HealthFinding(code, severity, float(value), float(threshold), int(count), locations,
                         tuple((k, str(v)) for k, v in details))


def _edge_flag(c: _Ctx, axis: int, mask: np.ndarray, bit: int) -> None:
    lo = [slice(None)] * 3
    hi = [slice(None)] * 3
    lo[axis], hi[axis] = slice(0, -1), slice(1, None)
    marked = mask.astype(np.uint8) * np.uint8(bit)
    c.flags[tuple(lo)] |= marked
    c.flags[tuple(hi)] |= marked


def _finding_range(c: _Ctx) -> HealthFinding:
    v = c.values
    with np.errstate(invalid='ignore'):
        outside = ((v < -RANGE_TOLERANCE) | (v > 1 + RANGE_TOLERANCE)).any(axis=-1)
    bad = outside | ~c.finite
    count = int(bad.sum())
    c.flags[bad] |= np.uint8(FLAG_BITS['range'])
    coords = np.argwhere(bad)
    excess = np.where(c.finite[..., None], np.maximum(-v, v - 1.0), np.inf).max(axis=-1)
    locations = _top_locations(c.n, coords, np.minimum(excess[bad], 1e9)) if count else np.zeros((0, 3), np.int64)
    shown = v[c.finite]
    nonfinite = int((~c.finite).sum())
    severity = 'error' if nonfinite else ('warning' if count else 'ok')
    return _make('range', severity, count / c.finite.size, 0.0, count, locations,
                 [('nonfinite_nodes', nonfinite), ('outside_unit_nodes', int(outside.sum())),
                  ('min_value', _fmt(shown.min()) if shown.size else 'nan'),
                  ('max_value', _fmt(shown.max()) if shown.size else 'nan')])


def _finding_reversal(c: _Ctx) -> HealthFinding:
    L = c.out_lab[..., 0]
    total = bad_count = 0
    worst = 0.0
    coords, scores = [], []
    for axis in range(3):
        lo, hi = _lo_hi(L, axis)
        flo, fhi = _lo_hi(c.finite, axis)
        valid = flo & fhi
        step = hi - lo
        bad = valid & (step < -REVERSAL_DL)
        total += int(valid.sum())
        bad_count += int(bad.sum())
        if bad.any():
            worst = min(worst, float(step[bad].min()))
            _edge_flag(c, axis, bad, FLAG_BITS['tone_reversal'])
            found = np.argwhere(bad)
            coords.append(found)
            scores.append(-step[bad])
    idx = np.arange(c.n)
    diag_L, diag_ok = L[idx, idx, idx], c.finite[idx, idx, idx]
    diag_step = np.diff(diag_L)
    diag_valid = diag_ok[:-1] & diag_ok[1:]
    diag_bad = diag_valid & (diag_step < -REVERSAL_DL)
    grey_reversals = int(diag_bad.sum())
    total += int(diag_valid.sum())
    bad_count += grey_reversals
    if grey_reversals:
        worst = min(worst, float(diag_step[diag_bad].min()))
        lower = np.nonzero(diag_bad)[0]
        c.flags[lower, lower, lower] |= np.uint8(FLAG_BITS['tone_reversal'])
        c.flags[lower + 1, lower + 1, lower + 1] |= np.uint8(FLAG_BITS['tone_reversal'])
        coords.append(np.repeat(lower[:, None], 3, axis=1))
        scores.append(-diag_step[diag_bad] + 1.0)      # grey-axis reversals rank first
    fraction = bad_count / max(total, 1)
    severity = 'warning' if fraction > REVERSAL_EDGE_FRACTION or grey_reversals else 'ok'
    locations = _top_locations(c.n, np.concatenate(coords) if coords else [], np.concatenate(scores) if scores else [])
    return _make('tone_reversal', severity, fraction, REVERSAL_EDGE_FRACTION, bad_count, locations,
                 [('edges', total), ('reversing_edges', bad_count), ('grey_axis_reversals', grey_reversals),
                  ('worst_dL', _fmt(worst)), ('reversal_dL', REVERSAL_DL)])


def _finding_gray_tint(c: _Ctx) -> HealthFinding:
    idx = np.arange(c.n)
    lab = c.out_lab[idx, idx, idx]
    ok = c.finite[idx, idx, idx]
    chroma = np.where(ok, np.hypot(lab[:, 1], lab[:, 2]), 0.0)
    peak = int(np.argmax(chroma))
    d2 = np.zeros(c.n)
    if c.n >= 3:
        second = lab[:-2, 1:] - 2 * lab[1:-1, 1:] + lab[2:, 1:]
        d2[1:-1] = np.where(ok[:-2] & ok[1:-1] & ok[2:], np.hypot(second[:, 0], second[:, 1]), 0.0)
    max_d2 = float(d2.max())
    tinted = chroma > GRAY_TINT_INFO_CHROMA
    jagged = d2 > GRAY_TINT_D2
    if jagged.any():
        severity, nodes, score = 'warning', np.nonzero(jagged)[0], d2[jagged]
    else:
        severity = 'info' if tinted.any() else 'ok'
        nodes, score = np.nonzero(tinted)[0], chroma[tinted]
    hue = math.degrees(math.atan2(float(lab[peak, 2]), float(lab[peak, 1]))) % 360.0
    return _make('gray_tint', severity, float(chroma.max()), GRAY_TINT_D2, len(nodes),
                 _top_locations(c.n, np.repeat(nodes[:, None], 3, axis=1), score),
                 [('max_chroma', _fmt(chroma.max())), ('max_second_difference', _fmt(max_d2)),
                  ('threshold_applies_to', 'second_difference'), ('info_chroma', GRAY_TINT_INFO_CHROMA),
                  ('tinted_nodes', int(tinted.sum())), ('hue_at_max_degrees', f'{hue:.1f}'),
                  ('max_chroma_node', peak)])


def _finding_clipping(c: _Ctx) -> HealthFinding:
    v, g = c.clean, c.grid
    low = (v <= CLIP_EPSILON) & (g > CLIP_EPSILON)
    high = (v >= 1 - CLIP_EPSILON) & (g < 1 - CLIP_EPSILON)
    hit = (low | high) & c.finite[..., None]
    node = hit.any(axis=-1)
    count = int(node.sum())
    c.flags[node] |= np.uint8(FLAG_BITS['clipping'])
    depth = np.maximum(np.where(low, g, 0.0), np.where(high, 1.0 - g, 0.0)).max(axis=-1)
    coords = np.argwhere(node)
    locations = _top_locations(c.n, coords, depth[node]) if count else np.zeros((0, 3), np.int64)
    fraction = count / max(int(c.finite.sum()), 1)
    return _make('clipping', 'warning' if fraction > CLIP_FRACTION else 'ok', fraction, CLIP_FRACTION, count,
                 locations, [('low_nodes', int((low.any(axis=-1) & c.finite).sum())),
                             ('high_nodes', int((high.any(axis=-1) & c.finite).sum())),
                             ('clip_epsilon', CLIP_EPSILON)])


def _finding_tone_jump(c: _Ctx) -> HealthFinding:
    """Discontinuities: places where the output bends far more sharply than anywhere else in the LUT.

    The second difference is taken in encoded RGB, where the interpolation happens, so the
    steep Oklab lightness near black does not turn a smooth tone curve into a "jump".  Two numbers
    judge each node (centre of three neighbours along an axis):

    * the *robust ratio*: |second difference| over the median absolute deviation (MAD) of all second
      differences of the LUT, which says how unusual the bend is for this LUT, and
    * the *step*: the Oklab distance the second difference moves the centre node (``visible``).  For a
      pure lightness jump this is the Oklab-L step; a colour jump adds its a/b part.  Oklab L runs
      0..1 from black to white, so the unit is "L".

    A node is a jump when ratio > ``TONE_JUMP_RATIO`` and step > ``TONE_JUMP_ABS``.  ``value`` is the
    largest step among the unusual nodes (ratio above the limit), so ``value > threshold`` is exactly
    the warning condition; it is 0 when no node is unusual.  The robust ratio is reported in
    ``details['robust_ratio']`` (largest over the whole LUT).
    """
    empty = np.zeros((0, 3), np.int64)
    n = c.n
    per_axis = []
    if n >= 3:
        for axis in range(3):
            take = lambda arr, s: np.take(arr, np.arange(s, n - 2 + s), axis=axis)    # noqa: E731
            a, b, d = take(c.clean, 0), take(c.clean, 1), take(c.clean, 2)
            second = a - 2 * b + d
            mag = np.sqrt(np.sum(second ** 2, axis=-1))
            valid = take(c.finite, 0) & take(c.finite, 1) & take(c.finite, 2)
            shifted = srgb_to_oklab(np.clip(b + second, 0.0, 1.0))
            delta = shifted - take(c.out_lab, 1)
            visible = np.sqrt(np.sum(delta ** 2, axis=-1))
            per_axis.append((mag, visible, np.abs(delta[..., 0]), valid))
    pooled = np.concatenate([mag[valid] for mag, _, _, valid in per_axis]) if per_axis else np.zeros(0)
    if not pooled.size:
        return _make('tone_jump', 'ok', 0.0, TONE_JUMP_ABS, 0, empty,
                     [('robust_ratio', '0'), ('ratio_limit', TONE_JUMP_RATIO), ('step_unit', 'Oklab L'),
                      ('max_second_difference', '0'), ('mad', '0')])
    median = float(np.median(pooled))
    mad = max(float(np.median(np.abs(pooled - median))), TONE_JUMP_MAD_FLOOR)
    step_max = step_light = ratio_max = 0.0
    count = 0
    coords, scores = [], []
    for axis, (mag, visible, light, valid) in enumerate(per_axis):
        if not valid.any():
            continue
        ratio = mag / mad
        ratio_max = max(ratio_max, float(ratio[valid].max()))
        unusual = valid & (ratio > TONE_JUMP_RATIO)
        if not unusual.any():
            continue
        worst = int(np.argmax(np.where(unusual, visible, -1.0)))
        if float(visible.reshape(-1)[worst]) > step_max:
            step_max = float(visible.reshape(-1)[worst])
            step_light = float(light.reshape(-1)[worst])
        hit = unusual & (visible > TONE_JUMP_ABS)
        if hit.any():
            found = np.argwhere(hit)
            scores.append(visible[hit])
            found[:, axis] += 1                  # centre node of the three
            c.flags[tuple(found.T)] |= np.uint8(FLAG_BITS['tone_jump'])
            coords.append(found)
            count += int(hit.sum())
    locations = _top_locations(n, np.concatenate(coords) if coords else [], np.concatenate(scores) if scores else [])
    return _make('tone_jump', 'warning' if step_max > TONE_JUMP_ABS else 'ok', step_max, TONE_JUMP_ABS, count,
                 locations, [('robust_ratio', _fmt(ratio_max)), ('ratio_limit', TONE_JUMP_RATIO),
                             ('step_unit', 'Oklab L'), ('step_L_component', _fmt(step_light)),
                             ('max_second_difference', _fmt(pooled.max())), ('mad', _fmt(mad))])


def _finding_hue_fold(c: _Ctx) -> HealthFinding:
    limit = math.radians(HUE_FOLD_DEGREES)
    count = examined = 0
    worst = 0.0
    coords, scores = [], []
    for axis in range(3):
        co_lo, co_hi = _lo_hi(c.out_chroma, axis)
        ho_lo, ho_hi = _lo_hi(c.out_hue, axis)
        ci_lo, ci_hi = _lo_hi(c.in_chroma, axis)
        hi_lo, hi_hi = _lo_hi(c.in_hue, axis)
        flo, fhi = _lo_hi(c.finite, axis)
        eligible = flo & fhi & (np.minimum(co_lo, co_hi) > HUE_FOLD_MIN_CHROMA)
        if not eligible.any():
            continue
        examined += int(eligible.sum())
        credit = np.where(np.minimum(ci_lo, ci_hi) > HUE_FOLD_MIN_INPUT_CHROMA, _wrap(hi_hi - hi_lo), 0.0)
        rotation = np.abs(_wrap(_wrap(ho_hi - ho_lo) - credit))
        hit = eligible & (rotation > limit)
        if hit.any():
            count += int(hit.sum())
            worst = max(worst, float(rotation[hit].max()))
            _edge_flag(c, axis, hit, FLAG_BITS['hue_fold'])
            coords.append(np.argwhere(hit))
            scores.append(rotation[hit])
    locations = _top_locations(c.n, np.concatenate(coords) if coords else [], np.concatenate(scores) if scores else [])
    return _make('hue_fold', 'warning' if count else 'ok', count, 0.0, count, locations,
                 [('edges_examined', examined), ('max_rotation_degrees', f'{math.degrees(worst):.1f}'),
                  ('limit_degrees', HUE_FOLD_DEGREES), ('min_chroma', HUE_FOLD_MIN_CHROMA)])


def _finding_banding(c: _Ctx) -> HealthFinding:
    count = 0
    peak = 0.0
    total = 0.0
    edges = 0
    coords, scores = [], []
    samples = []
    for axis in range(3):
        lo, hi = _lo_hi(c.out_lab, axis)
        flo, fhi = _lo_hi(c.finite, axis)
        d_out = np.sqrt(np.sum((hi - lo) ** 2, axis=-1))
        d_in = c.in_dist[axis]
        gain = np.divide(d_out, d_in, out=np.zeros_like(d_out), where=d_in > 1e-9)
        valid = flo & fhi
        if not valid.any():
            continue
        shown = gain[valid]
        samples.append(shown)
        peak = max(peak, float(shown.max()))
        total += float(shown.sum())
        edges += int(valid.sum())
        hit = valid & (gain > BANDING_GAIN)
        if hit.any():
            count += int(hit.sum())
            _edge_flag(c, axis, hit, FLAG_BITS['banding_risk'])
            coords.append(np.argwhere(hit))
            scores.append(gain[hit])
    p99 = float(np.percentile(np.concatenate(samples), 99)) if samples else 0.0
    locations = _top_locations(c.n, np.concatenate(coords) if coords else [], np.concatenate(scores) if scores else [])
    return _make('banding_risk', 'warning' if peak > BANDING_GAIN else 'ok', peak, BANDING_GAIN, count, locations,
                 [('mean_gain', _fmt(total / max(edges, 1))), ('p99_gain', _fmt(p99)), ('edges', edges)])


def _finding_flatten(c: _Ctx) -> HealthFinding:
    v = c.clean
    e0 = np.diff(v, axis=0)
    e1 = np.diff(v, axis=1)
    e2 = np.diff(v, axis=2)
    m0 = (e0[:, :-1, :-1] + e0[:, 1:, :-1] + e0[:, :-1, 1:] + e0[:, 1:, 1:]) / 4
    m1 = (e1[:-1, :, :-1] + e1[1:, :, :-1] + e1[:-1, :, 1:] + e1[1:, :, 1:]) / 4
    m2 = (e2[:-1, :-1] + e2[1:, :-1] + e2[:-1, 1:] + e2[1:, 1:]) / 4
    det = np.abs(np.einsum('ijkc,ijkc->ijk', m0, np.cross(m1, m2)))
    f = c.finite
    cell_ok = (f[:-1, :-1, :-1] & f[1:, :-1, :-1] & f[:-1, 1:, :-1] & f[:-1, :-1, 1:]
               & f[1:, 1:, :-1] & f[1:, :-1, 1:] & f[:-1, 1:, 1:] & f[1:, 1:, 1:])
    cells = int(cell_ok.sum())
    unit = (1.0 / (c.n - 1)) ** 3
    if not cells:
        return _make('flatten', 'ok', 0.0, FLATTEN_CELL_FRACTION, 0, np.zeros((0, 3), np.int64), [('cells', 0)])
    median = float(np.median(det[cell_ok]))
    reference = max(median, FLATTEN_DET_FLOOR * unit)
    flat = cell_ok & (det < FLATTEN_DET_RATIO * reference)
    count = int(flat.sum())
    coords = np.argwhere(flat)
    if count:
        c.flags[:-1, :-1, :-1][flat] |= np.uint8(FLAG_BITS['flatten'])
    locations = _top_locations(c.n, coords, -det[flat] / reference) if count else np.zeros((0, 3), np.int64)
    fraction = count / cells
    return _make('flatten', 'warning' if fraction > FLATTEN_CELL_FRACTION else 'ok', fraction,
                 FLATTEN_CELL_FRACTION, count, locations,
                 [('cells', cells), ('flat_cells', count), ('median_abs_det_ratio', _fmt(median / unit)),
                  ('det_ratio', FLATTEN_DET_RATIO)])



# ------------------------------------------------------------------------------------------------
# Grey axis, input-kind estimate, report assembly
# ------------------------------------------------------------------------------------------------
def _grey_axis(c: _Ctx) -> np.ndarray:
    """Output along the neutral diagonal at 257 evenly spaced inputs (exact for tetrahedral sampling)."""
    n = c.n
    idx = np.arange(n)
    diag = c.clean[idx, idx, idx]
    t = np.linspace(0.0, 1.0, GRAY_SAMPLES)
    p = t * (n - 1)
    lo = np.minimum(p.astype(np.intp), n - 2)
    frac = (p - lo)[:, None]
    out = diag[lo] * (1 - frac) + diag[lo + 1] * frac
    lab = srgb_to_oklab(np.clip(out, 0.0, 1.0))
    return np.column_stack([t, out, lab])


def _cross(t: np.ndarray, y: np.ndarray, target: float):
    """Input position where ``y`` first reaches ``target`` (linear interpolation) or None."""
    hit = y >= target
    if not hit.any():
        return None
    k = int(np.argmax(hit))
    if k == 0:
        return float(t[0])
    y0, y1 = float(y[k - 1]), float(y[k])
    return float(t[k - 1] + (target - y0) / (y1 - y0) * (t[k] - t[k - 1]))


@functools.lru_cache(maxsize=1)
def _grey_input_lightness() -> np.ndarray:
    t = np.linspace(0.0, 1.0, GRAY_SAMPLES)
    return srgb_to_oklab(np.column_stack([t, t, t]))[:, 0]


def _grey_features(level: np.ndarray, lightness: np.ndarray) -> dict:
    t = np.linspace(0.0, 1.0, level.size)
    black = 0.0 if level[0] > BLACK_OUTPUT else _cross(t, level, BLACK_OUTPUT)
    mid = _cross(t, lightness, MID_LIGHTNESS)
    grey = _cross(t, lightness, _GREY18_LIGHTNESS)
    white = _cross(t, level, 0.98)
    slope = None
    if mid is not None:
        t1, t2 = max(mid - 0.05, 0.0), min(mid + 0.05, 1.0)
        l_in = _grey_input_lightness()
        out1, out2 = np.interp([t1, t2], t, lightness)
        in1, in2 = np.interp([t1, t2], t, l_in)
        slope = float((out2 - out1) / (in2 - in1)) if in2 > in1 else None
    return dict(black=1.0 if black is None else black, mid=mid, grey=grey, white=1.0 if white is None else white,
                slope=slope, out0=float(level[0]), out_end=float(level[-1]))


_GREY18_LIGHTNESS = float(srgb_to_oklab(np.full((1, 3), float(rec709_oetf(0.18))))[0, 0])


@functools.lru_cache(maxsize=1)
def _curve_models() -> dict:
    """Grey-axis features of 'published log curve -> Rec.709 display' and 'Rec.709 display -> log curve'."""
    t = np.linspace(0.0, 1.0, GRAY_SAMPLES)
    result = {}
    for name, (encode, decode) in LOG_CURVES.items():
        level = rec709_oetf(np.maximum(decode(t), 0.0))
        lightness = srgb_to_oklab(np.column_stack([np.minimum(level, 1.0)] * 3))[:, 0]
        forward = _grey_features(np.minimum(level, 1.0), lightness)
        encoded = np.clip(encode(rec709_inverse_oetf(t)), 0.0, 1.0)
        reverse = dict(out0=float(encoded[0]), grey_out=float(np.interp(float(rec709_oetf(0.18)), t, encoded)))
        result[name] = (forward, reverse)
    return result


def _chroma_gain(c: _Ctx) -> float:
    """Mean output/input Oklab chroma for moderately saturated colours around the neutral axis."""
    levels = (0.3, 0.5, 0.7)
    targets = np.array([[1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 1, 1], [0, 0, 1], [1, 0, 1]], dtype=np.float64)
    inputs = np.array([(1 - 0.35) * g + 0.35 * p for g in levels for p in targets])
    inputs = np.clip(inputs, 0, 1)
    n = c.n
    table = np.ascontiguousarray(c.clean.reshape(-1, 3))
    strides = np.array([n * n, n, 1], dtype=np.intp)
    out = np.clip(_tetrahedral(table, strides, inputs * (n - 1), n), 0.0, 1.0)
    lab_in, lab_out = srgb_to_oklab(inputs), srgb_to_oklab(out)
    chroma_in = np.hypot(lab_in[:, 1], lab_in[:, 2])
    chroma_out = np.hypot(lab_out[:, 1], lab_out[:, 2])
    return float(chroma_out.mean() / max(chroma_in.mean(), 1e-9))


def _score(distance_squared: float) -> float:
    return float(math.exp(-0.5 * distance_squared))


def _estimate_kind(c: _Ctx, grey: np.ndarray) -> tuple[InputKindEstimate, dict]:
    deviation = float(np.abs(c.clean - c.grid).max()) if c.finite.any() else 1.0
    level = grey[:, 1:4].mean(axis=1)
    lightness = grey[:, 4]
    feat = _grey_features(np.clip(level, 0, 1), lightness)
    gain = _chroma_gain(c)
    feat['chroma_gain'] = gain
    feat['deviation'] = deviation
    if not c.finite.all():
        return InputKindEstimate('unknown', 0.0, (), KIND_NOTE_KEYS['unknown']), feat
    if deviation <= IDENTITY_TOLERANCE:
        confidence = 1.0 - 0.5 * deviation / IDENTITY_TOLERANCE
        return InputKindEstimate('identity', confidence, (), KIND_NOTE_KEYS['identity']), feat
    slope = feat['slope']
    black = feat['black']
    models = _curve_models()
    if slope is None:
        return InputKindEstimate('unknown', 0.2, (), KIND_NOTE_KEYS['unknown']), feat
    log_like = black >= LOG_BLACK_CODE and slope >= LOG_SLOPE and gain >= LOG_CHROMA_GAIN
    is_log = log_like and feat['white'] <= LOG_WHITE_CODE
    lifted = feat['out0'] >= LOG_BLACK_CODE
    is_encode = lifted and slope <= 0.8 and gain <= 0.85
    if is_log:
        scored = []
        for name, (forward, _) in models.items():
            d2 = ((feat['black'] - forward['black']) / 0.03) ** 2
            if feat['grey'] is not None and forward['grey'] is not None:
                d2 += ((feat['grey'] - forward['grey']) / 0.04) ** 2
            else:
                d2 += 4.0
            scored.append((name, _score(d2)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        top = tuple(scored[:3])
        margin = min(1.0, (slope - LOG_SLOPE) / 0.6 + 0.5) * min(1.0, (gain - LOG_CHROMA_GAIN) / 0.6 + 0.5)
        confidence = float(np.clip(0.4 + 0.3 * margin + 0.3 * top[0][1], 0.0, 1.0))
        return InputKindEstimate('log_to_display', confidence, top, KIND_NOTE_KEYS['log_to_display']), feat
    if is_encode:
        scored = []
        for name, (_, reverse) in models.items():
            d2 = ((feat['out0'] - reverse['out0']) / 0.03) ** 2
            probe = float(np.interp(float(rec709_oetf(0.18)), grey[:, 0], level))
            d2 += ((probe - reverse['grey_out']) / 0.04) ** 2
            scored.append((name, _score(d2)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        top = tuple(scored[:3])
        confidence = float(np.clip(0.4 + 0.3 * min(1.0, (0.8 - slope) / 0.4 + 0.5) + 0.3 * top[0][1], 0.0, 1.0))
        return InputKindEstimate('display_to_log', confidence, top, KIND_NOTE_KEYS['display_to_log']), feat
    unclear = (black >= LOG_BLACK_CODE) or lifted
    if log_like or (unclear and (slope >= LOG_SLOPE or gain >= LOG_CHROMA_GAIN or slope <= 0.8 or gain <= 0.85)):
        return InputKindEstimate('unknown', 0.3, (), KIND_NOTE_KEYS['unknown']), feat
    # an ordinary display-referred (creative) LUT; Rec.709 and sRGB cannot be told apart from the data
    fit = 1.0 if 0.6 <= slope <= 1.6 and 0.6 <= gain <= 1.6 else 0.6
    return InputKindEstimate('display', 0.5 + 0.3 * fit, (), KIND_NOTE_KEYS['display']), feat


def _lattice(n: int) -> np.ndarray:
    return np.unique(np.round(np.linspace(0, n - 1, min(n, 13))).astype(np.intp))


MAX_FLAGGED_SAMPLES = 2048


def _hue_samples(c: _Ctx) -> np.ndarray:
    n = c.n
    lat = _lattice(n)
    mask = np.zeros((n, n, n), bool)
    mask[np.ix_(lat, lat, lat)] = True
    flagged = np.flatnonzero(c.flags.reshape(-1))
    if len(flagged) > MAX_FLAGGED_SAMPLES:
        flagged = flagged[np.linspace(0, len(flagged) - 1, MAX_FLAGGED_SAMPLES).astype(np.intp)]
    mask.reshape(-1)[flagged] = True
    chosen = np.flatnonzero(mask.reshape(-1))
    in_lab = c.in_lab.reshape(-1, 3)[chosen]
    out_lab = c.out_lab.reshape(-1, 3)[chosen]
    return np.column_stack([in_lab[:, 1], in_lab[:, 2], out_lab[:, 1], out_lab[:, 2],
                            c.flags.reshape(-1)[chosen].astype(np.float64),
                            np.hypot(out_lab[:, 1], out_lab[:, 2])])


_FINDINGS = (_finding_range, _finding_reversal, _finding_gray_tint, _finding_clipping, _finding_tone_jump,
             _finding_hue_fold, _finding_banding, _finding_flatten)


def _provenance(lut: LoadedLut) -> tuple[tuple[str, str], ...]:
    return (('schema', SCHEMA), ('lut_size', str(lut.size)), ('source_format', lut.source_format),
            ('title', lut.title),
            ('metric_space', 'Oklab (image_look.srgb_to_oklab); values clipped to [0,1] for the conversion only'),
            ('nominal_display', 'Rec.709 OETF for the log-curve candidates'),
            ('thresholds', json.dumps(THRESHOLDS, sort_keys=True)))


def _as_loaded(lut) -> LoadedLut:
    if isinstance(lut, LoadedLut):
        return lut
    if isinstance(lut, Lut3D):
        return from_lut3d(lut)
    raise ValidationError('Expected a LoadedLut or Lut3D.', code='invalid_lut')


def diagnose_lut(lut: LoadedLut | Lut3D, *, context: JobContext | None = None) -> HealthReport:
    """Run the eight checks and the input-kind estimate on the LUT's own grid."""
    checkpoint(context, stage='health.diagnose', fraction=0)
    lut = _as_loaded(lut)
    c = _prepare(lut.values)
    findings = []
    for number, check in enumerate(_FINDINGS, 1):
        findings.append(check(c))
        checkpoint(context, stage='health.diagnose', fraction=number / (len(_FINDINGS) + 2))
    grey = _grey_axis(c)
    kind, _ = _estimate_kind(c, grey)
    checkpoint(context, stage='health.diagnose', fraction=(len(_FINDINGS) + 1) / (len(_FINDINGS) + 2))
    samples = _hue_samples(c)
    overall = SEVERITIES[max(SEVERITIES.index(f.severity) for f in findings)]
    report = HealthReport(lut.size, tuple(findings), kind, grey, samples, overall, _provenance(lut))
    checkpoint(context, stage='health.diagnose', fraction=1)
    return report


# ------------------------------------------------------------------------------------------------
# Report serialisation helpers (CLI, JSON sidecar)
# ------------------------------------------------------------------------------------------------
def health_summary(report: HealthReport) -> dict:
    """Compact JSON-safe summary for the export sidecar: ``{"overall", "findings": [{code, severity, value}]}``."""
    return {'overall': report.overall,
            'findings': [{'code': f.code, 'severity': f.severity, 'value': f.value} for f in report.findings]}


def report_to_dict(report: HealthReport, *, include_arrays: bool = False) -> dict:
    """JSON-safe dict of a report; ``include_arrays`` adds every finding's ``locations``, ``gray_axis`` and ``hue_samples``."""
    findings = []
    for f in report.findings:
        item = {'code': f.code, 'severity': f.severity, 'value': f.value, 'threshold': f.threshold,
                'count': f.count, 'details': dict(f.details)}
        if include_arrays:
            item['locations'] = f.locations.tolist()
        findings.append(item)
    result = {'schema_version': 1, 'size': report.size, 'overall': report.overall,
              'kind': {'kind': report.kind.kind, 'confidence': report.kind.confidence,
                       'log_candidates': [{'curve': n, 'score': s} for n, s in report.kind.log_candidates],
                       'note_key': report.kind.note_key},
              'findings': findings, 'provenance': dict(report.provenance)}
    if include_arrays:
        result['gray_axis'] = report.gray_axis.tolist()
        result['hue_samples'] = report.hue_samples.tolist()
        result['flag_bits'] = dict(FLAG_BITS)
    return result


# ------------------------------------------------------------------------------------------------
# Colour difference (CIEDE2000) for the "how much did the repair change" metric
# ------------------------------------------------------------------------------------------------
_SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                         [0.2126729, 0.7151522, 0.0721750],
                         [0.0193339, 0.1191920, 0.9503041]])
_D65 = np.array([0.95047, 1.0, 1.08883])


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    rgb = np.clip(rgb, 0.0, 1.0)
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = (linear @ _SRGB_TO_XYZ.T) / _D65
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def delta_e2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """CIEDE2000 (Sharma, Wu, Dalal 2005; kL = kC = kH = 1) between CIELAB arrays (..., 3)."""
    L1, a1, b1 = (lab1[..., i] for i in range(3))
    L2, a2, b2 = (lab2[..., i] for i in range(3))
    c_mean = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(c_mean ** 7 / (c_mean ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    product = c1p * c2p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(product == 0, 0.0, dh)
    dHp = 2 * np.sqrt(product) * np.sin(np.radians(dh) / 2)
    hsum = h1p + h2p
    hbar = np.where(product == 0, hsum,
                    np.where(np.abs(h1p - h2p) <= 180, hsum / 2, np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2)))
    t = (1 - 0.17 * np.cos(np.radians(hbar - 30)) + 0.24 * np.cos(np.radians(2 * hbar))
         + 0.32 * np.cos(np.radians(3 * hbar + 6)) - 0.20 * np.cos(np.radians(4 * hbar - 63)))
    dtheta = 30 * np.exp(-(((hbar - 275) / 25) ** 2))
    c_bar = (c1p + c2p) / 2
    rc = 2 * np.sqrt(c_bar ** 7 / (c_bar ** 7 + 25.0 ** 7))
    l_bar = (L1 + L2) / 2
    sl = 1 + 0.015 * (l_bar - 50) ** 2 / np.sqrt(20 + (l_bar - 50) ** 2)
    sc, sh = 1 + 0.045 * c_bar, 1 + 0.015 * c_bar * t
    rt = -np.sin(np.radians(2 * dtheta)) * rc
    term_l, term_c, term_h = (L2 - L1) / sl, (c2p - c1p) / sc, dHp / sh
    return np.sqrt(np.maximum(term_l ** 2 + term_c ** 2 + term_h ** 2 + rt * term_c * term_h, 0.0))


# ------------------------------------------------------------------------------------------------
# Repair
# ------------------------------------------------------------------------------------------------
ALPHA_MAX = 0.75          # per-iteration relaxation toward the neighbour mean (stable: < 1)
LOCAL_ALPHA = 0.6         # relaxation inside the fix_jumps / round_clip regions
LOCK_GRAY_FADE = 0.12     # input Oklab chroma at which the grey correction has faded to nothing


def _sub_context(context: JobContext | None, low: float, high: float) -> JobContext | None:
    if context is None:
        return None
    from .types import Progress

    def forward(p):
        if context.progress is not None:
            context.progress(Progress(p.stage, low + (high - low) * p.fraction, p.message))
    return JobContext(progress=forward if context.progress is not None else None, cancel=context.cancel)


def _shift(array: np.ndarray, axis: int, step: int, fill=0) -> np.ndarray:
    out = np.full_like(array, fill)
    src = [slice(None)] * array.ndim
    dst = [slice(None)] * array.ndim
    if step > 0:
        src[axis], dst[axis] = slice(0, -step), slice(step, None)
    else:
        src[axis], dst[axis] = slice(-step, None), slice(0, step)
    out[tuple(dst)] = array[tuple(src)]
    return out


def _dilate(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    for axis in range(3):
        out = out | _shift(out, axis, 1, False) | _shift(out, axis, -1, False)
    return out


def _feather(mask: np.ndarray) -> np.ndarray:
    weight = mask.astype(np.float64)
    for axis in range(3):
        weight = 0.5 * weight + 0.25 * (_shift(weight, axis, 1, 0.0) + _shift(weight, axis, -1, 0.0))
    return np.clip(weight, 0.0, 1.0)


def _fill_nonfinite(values: np.ndarray, finite: np.ndarray) -> np.ndarray:
    """Replace non-finite nodes by the mean of valid neighbours (repeated), then by the identity."""
    out = np.where(finite[..., None], values, 0.0)
    valid = finite.copy()
    for _ in range(values.shape[0]):
        if valid.all():
            break
        acc = np.zeros_like(out)
        cnt = np.zeros(valid.shape)
        for axis in range(3):
            for step in (1, -1):
                acc += _shift(out * valid[..., None], axis, step)
                cnt += _shift(valid.astype(np.float64), axis, step)
        fill = ~valid & (cnt > 0)
        if not fill.any():
            break
        out[fill] = acc[fill] / cnt[fill][:, None]
        valid = valid | fill
    if not valid.all():
        out = np.where(valid[..., None], out, _identity_grid(values.shape[0]))
    return out


def _neighbour_mean(residual: np.ndarray) -> np.ndarray:
    """Mean of the axis neighbours; a node on a cube face has no neighbours along the face normal."""
    n = residual.shape[0]
    acc = np.zeros_like(residual)
    cnt = np.zeros(residual.shape[:3])
    if n >= 3:
        for axis in range(3):
            centre = [slice(None)] * 3
            below = [slice(None)] * 3
            above = [slice(None)] * 3
            centre[axis], below[axis], above[axis] = slice(1, -1), slice(0, -2), slice(2, None)
            acc[tuple(centre)] += residual[tuple(below)] + residual[tuple(above)]
            cnt[tuple(centre)] += 2
    return np.divide(acc, cnt[..., None], out=residual.copy(), where=cnt[..., None] > 0)


def _srgb_encode(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, 1.0)
    return np.where(linear <= 0.0031308, 12.92 * linear, 1.055 * np.power(linear, 1 / 2.4) - 0.055)


def _lock_gray(values: np.ndarray, in_lab: np.ndarray, in_chroma: np.ndarray, grid: np.ndarray) -> np.ndarray:
    n = values.shape[0]
    idx = np.arange(n)
    diag = values[idx, idx, idx]
    lightness = srgb_to_oklab(np.clip(diag, 0.0, 1.0))[:, 0]
    neutral = np.repeat(_srgb_encode(np.clip(lightness, 0.0, None) ** 3)[:, None], 3, axis=1)
    delta = neutral - diag
    weight = np.clip(1.0 - in_chroma / LOCK_GRAY_FADE, 0.0, 1.0) ** 2
    position = _srgb_encode(np.clip(in_lab[..., 0], 0.0, None) ** 3) * (n - 1)
    low = np.clip(np.floor(position).astype(np.intp), 0, n - 2)
    frac = (position - low)[..., None]
    shift = delta[low] * (1 - frac) + delta[low + 1] * frac
    out = values + weight[..., None] * shift
    # a channel that sits on the cube face (input and output both at the bound) stays there
    keep = ((grid <= 0.0) & (values <= 0.0)) | ((grid >= 1.0) & (values >= 1.0))
    out = np.where(keep, values, out)
    out[idx, idx, idx] = neutral
    return out


def repair_lut(lut: LoadedLut | Lut3D, settings: RepairSettings | None = None, *,
               context: JobContext | None = None) -> RepairResult:
    """Smooth / repair a LUT on explicit request; the original is never modified.

    Order: non-finite nodes are filled; the residual ``out - identity`` is relaxed toward the mean of
    its neighbours (global ``smooth`` plus local ``fix_jumps`` / ``round_clip`` regions); ``lock_gray``
    then makes grey inputs neutral at their own Oklab lightness; finally the result is clipped to [0, 1].
    Cube faces keep their position.  Default settings return a bit-identical copy.
    """
    settings = RepairSettings() if settings is None else settings
    if not isinstance(settings, RepairSettings):
        raise ValidationError('settings must be a RepairSettings record.', code='invalid_repair_settings')
    checkpoint(context, stage='health.repair', fraction=0)
    source = _as_loaded(lut)
    before = diagnose_lut(source, context=_sub_context(context, 0.0, 0.25))
    if settings.is_default:
        repaired = replace(source)
        change = (0.0, 0.0, 0.0)
        after = before
    else:
        n = source.size
        c = _prepare(source.values)
        values = _fill_nonfinite(source.values, c.finite)
        grid, in_lab, in_chroma = c.grid, c.in_lab, c.in_chroma
        alpha = np.full((n, n, n), ALPHA_MAX * settings.smooth)
        local = np.zeros((n, n, n))
        if settings.fix_jumps or settings.round_clip:
            region = np.zeros((n, n, n), bool)
            if settings.fix_jumps:
                _finding_tone_jump(c)
                _finding_hue_fold(c)
                region |= (c.flags & (FLAG_BITS['tone_jump'] | FLAG_BITS['hue_fold'])) != 0
            if settings.round_clip:
                _finding_clipping(c)
                plateau = (c.flags & FLAG_BITS['clipping']) != 0
                region |= _dilate(plateau) & _dilate(~plateau)       # one node on either side of the plateau border
            local = _feather(_dilate(region))
        alpha = np.clip(alpha + LOCAL_ALPHA * local, 0.0, ALPHA_MAX)
        checkpoint(context, stage='health.repair', fraction=0.3)
        if alpha.max() > 0:
            start = values - grid
            residual = start
            for step in range(settings.iterations):
                residual = residual + alpha[..., None] * (_neighbour_mean(residual) - residual)
                checkpoint(context, stage='health.repair',
                           fraction=0.3 + 0.35 * (step + 1) / settings.iterations)
            values = values + (residual - start)       # untouched nodes keep their exact values
        if settings.lock_gray:
            values = _lock_gray(values, in_lab, in_chroma, grid)
        values = np.clip(values, 0.0, 1.0)
        kept = tuple(w for w in source.warnings if w not in ('values_outside_unit', 'values_nonfinite'))
        repaired = LoadedLut(values, n, source.title, source.source_format, source.source_path,
                             source.domain_min, source.domain_max, kept + ('repaired',))
        old = _srgb_to_lab(np.nan_to_num(source.values, nan=0.0, posinf=1.0, neginf=0.0).reshape(-1, 3))
        new = _srgb_to_lab(repaired.values.reshape(-1, 3))
        de = delta_e2000(old, new)
        change = (float(de.mean()), float(np.percentile(de, 99)), float(de.max()))
        checkpoint(context, stage='health.repair', fraction=0.7)
        after = diagnose_lut(repaired, context=_sub_context(context, 0.7, 1.0))
    record = {'settings': {'smooth': settings.smooth, 'fix_jumps': settings.fix_jumps,
                           'round_clip': settings.round_clip, 'lock_gray': settings.lock_gray,
                           'iterations': settings.iterations},
              'change_de2000': {'mean': change[0], 'p99': change[1], 'max': change[2]},
              'method': 'residual Laplacian relaxation; faces keep position; local regions feathered'}
    after = replace(after, provenance=after.provenance + (('repair', json.dumps(record, sort_keys=True)),))
    checkpoint(context, stage='health.repair', fraction=1)
    return RepairResult(repaired, change, before, after)

