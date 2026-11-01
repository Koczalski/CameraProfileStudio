"""Bounded standalone DCP parsing and explicit camera-linear rendering.

No RAW decoding, individual camera calibration, or OEM JPEG pipeline is inferred.
See docs/DCP_IMPLEMENTATION.md for the supported subset and numerical choices.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import struct

import numpy as np
from scipy.interpolate import PchipInterpolator

from .errors import InvalidProfileError, UnsupportedError, ValidationError, checkpoint
from .types import (Capability, DcpProfile, DcpSettings, JobContext, ProfileInfo,
                    RGB, RGBTransform, SpaceSpec, TagValue)

_MAX_BYTES = 32 * 1024 * 1024
_MAX_VALUES = 4_000_000
_NAMES = {
    50708: 'UniqueCameraModel', 50721: 'ColorMatrix1', 50722: 'ColorMatrix2',
    50723: 'CameraCalibration1', 50724: 'CameraCalibration2',
    50725: 'ReductionMatrix1', 50726: 'ReductionMatrix2', 50727: 'AnalogBalance',
    50728: 'AsShotNeutral', 50729: 'AsShotWhiteXY', 50730: 'BaselineExposure',
    50778: 'CalibrationIlluminant1', 50779: 'CalibrationIlluminant2',
    50932: 'ProfileCalibrationSignature', 50936: 'ProfileName',
    50937: 'ProfileHueSatMapDims', 50938: 'ProfileHueSatMapData1',
    50939: 'ProfileHueSatMapData2', 50940: 'ProfileToneCurve',
    50941: 'ProfileEmbedPolicy', 50942: 'ProfileCopyright',
    50964: 'ForwardMatrix1', 50965: 'ForwardMatrix2',
    50981: 'ProfileLookTableDims', 50982: 'ProfileLookTableData',
    51107: 'ProfileHueSatMapEncoding', 51108: 'ProfileLookTableEncoding',
    51109: 'BaselineExposureOffset', 51110: 'DefaultBlackRender',
    50879: 'ColorimetricReference', 50931: 'CameraCalibrationSignature',
    50934: 'AsShotProfileName', 52525: 'ProfileGainTableMap',
    52529: 'CalibrationIlluminant3', 52531: 'ColorMatrix3',
    52532: 'ForwardMatrix3', 52533: 'IlluminantData1', 52534: 'IlluminantData2',
    52537: 'ProfileHueSatMapData3', 52538: 'ReductionMatrix3',
    52543: 'RGBTables', 52544: 'ProfileGainTableMap2',
    52551: 'ProfileDynamicRange', 52552: 'ProfileGroupName', 53535: 'IlluminantData3',
}
# Unknown tags are retained, but not assumed to be harmless processing metadata.
_METADATA = {254, 270, 271, 272, 305, 306, 315, 33432, 50706, 50707,
             50708, 50735, 50931, 50932, 50934, 50936, 50941, 50942, 52552}
_CAMERA_EXTRA = {50723, 50724, 50725, 50726, 50727, 50728, 50729, 50730}
_THIRD_CAMERA = {52529, 52531, 52532, 52538, 53535}
_CUSTOM_ILLUMINANT = {52533, 52534, 53535}
_GLOBAL_PROCESSING = {52525, 52543, 52544, 52551}
_SUPPORTED = set(_NAMES) | _METADATA
_TYPES = {1: (1, 'B'), 2: (1, None), 3: (2, 'H'), 4: (4, 'I'),
          5: (8, 'II'), 6: (1, 'b'), 7: (1, None), 8: (2, 'h'),
          9: (4, 'i'), 10: (8, 'ii'), 11: (4, 'f'), 12: (8, 'd'), 13: (4, 'I')}
_EXPECTED = {
    **{t: ({10}, 9) for t in (50721, 50722, 50723, 50724, 50725, 50726, 50964, 50965)},
    50727: ({5}, 3), 50728: ({3, 5}, 3), 50729: ({5}, 2), 50730: ({10}, 1),
    50778: ({3}, 1), 50779: ({3}, 1), 50937: ({4}, 3), 50981: ({4}, 3),
    **{t: ({11}, None) for t in (50938, 50939, 50940, 50982)},
    **{t: ({4}, 1) for t in (50941, 51107, 51108, 51110)},
    51109: ({10}, 1), 50879: ({3}, 1),
    **{t: ({2}, None) for t in (50708, 50931, 50932, 50934, 50936, 50942, 52552)},
    50706: ({1}, 4), 50707: ({1}, 4),
}
_TEMPERATURES = {17: 2856., 18: 4874., 19: 6774., 20: 5503.,
                 21: 6504., 22: 7504., 23: 5003., 24: 3200.}
_D50 = np.array([0.3457 / 0.3585, 1., (1 - 0.3457 - 0.3585) / 0.3585])
_D65 = np.array([0.3127 / 0.3290, 1., (1 - 0.3127 - 0.3290) / 0.3290])
_BRADFORD = np.array([[.8951, .2664, -.1614], [-.7502, 1.7135, .0367],
                      [.0389, -.0685, 1.0296]])


def _primaries(xy: tuple[tuple[float, float], ...], white: np.ndarray) -> np.ndarray:
    p = np.array([[x / y, 1., (1 - x - y) / y] for x, y in xy]).T
    return p @ np.diag(np.linalg.solve(p, white))


_SRGB_XYZ = _primaries(((.64, .33), (.30, .60), (.15, .06)), _D65)
_PROPHOTO_XYZ = _primaries(((.7347, .2653), (.1596, .8404), (.0366, .0001)), _D50)


def _adapt(source: np.ndarray, destination: np.ndarray) -> np.ndarray:
    src = _BRADFORD @ source
    dst = _BRADFORD @ destination
    if np.any(src <= 1e-12) or np.any(dst <= 1e-12):
        raise InvalidProfileError('White point is outside supported Bradford domain', code='invalid_white')
    return np.linalg.solve(_BRADFORD, np.diag(dst / src) @ _BRADFORD)


_SRGB_TO_WORK = np.linalg.solve(_PROPHOTO_XYZ, _adapt(_D65, _D50) @ _SRGB_XYZ)
_WORK_TO_SRGB = np.linalg.inv(_SRGB_TO_WORK)


def _decode(x: np.ndarray) -> np.ndarray:
    return np.where(x <= .04045, x / 12.92, ((np.maximum(x, 0) + .055) / 1.055) ** 2.4)


def _encode(x: np.ndarray) -> np.ndarray:
    return np.where(x <= .0031308, 12.92 * x, 1.055 * np.maximum(x, 0) ** (1 / 2.4) - .055)


def _array(value: TagValue) -> np.ndarray:
    try:
        a = np.asarray(value, dtype=np.float64).reshape(-1)
    except (ValueError, TypeError) as exc:
        raise InvalidProfileError('Expected numeric DCP tag', code='invalid_tag') from exc
    if not np.all(np.isfinite(a)):
        raise InvalidProfileError('Nonfinite DCP tag', code='nonfinite_profile')
    return a


def _scalar(tags: dict[int, TagValue], tag: int, default: int | float = 0) -> float:
    a = _array(tags.get(tag, default))
    if a.size != 1:
        raise InvalidProfileError(f'Tag {tag} must be scalar', code='invalid_count')
    return float(a[0])


def _matrix(tags: dict[int, TagValue], tag: int) -> np.ndarray:
    a = _array(tags[tag])
    if a.size != 9:
        raise InvalidProfileError(f'{_NAMES.get(tag, tag)} must be 3x3', code='invalid_matrix')
    a = a.reshape(3, 3)
    if np.linalg.cond(a) > 1e10 or np.linalg.svd(a, compute_uv=False)[-1] <= 1e-12:
        raise InvalidProfileError(f'{_NAMES.get(tag, tag)} is singular or ill-conditioned', code='singular_matrix')
    return a


def _table(tags: dict[int, TagValue], dims_tag: int, data_tag: int) -> np.ndarray | None:
    if data_tag not in tags:
        return None
    if dims_tag not in tags:
        raise InvalidProfileError(f'Table {data_tag} has no dimensions', code='invalid_table')
    dims = _array(tags[dims_tag])
    if dims.size != 3 or np.any(dims != np.floor(dims)):
        raise InvalidProfileError('Invalid HSV table dimensions', code='invalid_table')
    h, s, v = map(int, dims)
    if h < 1 or s < 2 or v < 1 or h * s * v * 3 > _MAX_VALUES:
        raise InvalidProfileError('Unsupported or excessive HSV dimensions', code='invalid_table')
    a = _array(tags[data_tag])
    if a.size == h * (s - 1) * v * 3:
        # SDK legacy serialization may omit saturation-zero entries.
        expanded = np.empty((v, h, s, 3), dtype=np.float64)
        expanded[:, :, 1:, :] = a.reshape(v, h, s - 1, 3)
        expanded[:, :, 0, :] = expanded[:, :, 1, :]
        expanded[:, :, 0, 2] = 1.
        a = expanded
    elif a.size == h * s * v * 3:
        a = a.reshape(v, h, s, 3)
    else:
        raise InvalidProfileError('HSV data count disagrees with dimensions (including legacy skipSat0)', code='invalid_table')
    if np.any(a[..., 1:] < 0):
        raise InvalidProfileError('Negative HSV saturation/value scale', code='invalid_table')
    if not np.allclose(a[:, :, 0, 2], 1., rtol=0, atol=1e-6):
        raise InvalidProfileError('Zero-saturation entries must have value scale 1', code='invalid_table')
    return a


def _tone(tags: dict[int, TagValue]) -> PchipInterpolator | None:
    if 50940 not in tags:
        return None
    a = _array(tags[50940])
    if a.size < 4 or a.size % 2:
        raise InvalidProfileError('Tone curve needs x/y pairs', code='invalid_tone')
    a = a.reshape(-1, 2)
    if (np.any(a < 0) or np.any(a > 1) or np.any(np.diff(a[:, 0]) <= 0)
            or np.any(np.diff(a[:, 1]) < 0) or not np.allclose(a[0], 0, atol=1e-7)
            or not np.allclose(a[-1], 1, atol=1e-7)):
        raise InvalidProfileError('Tone must be monotone with endpoints (0,0), (1,1)', code='invalid_tone')
    return PchipInterpolator(a[:, 0], a[:, 1], extrapolate=False)


def _validate_tags(tags: dict[int, TagValue]) -> None:
    for tag in (50721, 50722, 50723, 50724, 50725, 50726, 50964, 50965):
        if tag in tags:
            m = _matrix(tags, tag)
            if tag in (50964, 50965) and not np.allclose(m @ np.ones(3), _D50, atol=.005, rtol=0):
                raise InvalidProfileError('ForwardMatrix must map unit neutral to D50', code='invalid_forward_white')
    for dims, data in ((50937, 50938), (50937, 50939), (50981, 50982)):
        _table(tags, dims, data)
    if 50937 in tags and 50938 not in tags or 50981 in tags and 50982 not in tags:
        raise InvalidProfileError('Table dimensions without first data table', code='invalid_table')
    if 50939 in tags and 50938 not in tags:
        raise InvalidProfileError('Second HueSatMap without first map', code='invalid_table')
    if 50722 in tags and 50721 not in tags:
        raise InvalidProfileError('ColorMatrix2 without ColorMatrix1', code='invalid_matrix')
    _tone(tags)


def _read_tags(data: bytes, context: JobContext | None) -> dict[int, TagValue]:
    if len(data) < 8 or data[:2] not in (b'II', b'MM'):
        raise InvalidProfileError('Not a classic TIFF/DCP header', code='invalid_header')
    endian = '<' if data[:2] == b'II' else '>'
    magic, offset = struct.unpack_from(endian + 'HI', data, 2)
    if magic not in (42, 0x4352):
        raise InvalidProfileError('Unsupported DCP magic (BigTIFF is not supported)', code='invalid_header')
    if offset < 8 or offset + 2 > len(data):
        raise InvalidProfileError('IFD offset outside file', code='invalid_offset')
    count = struct.unpack_from(endian + 'H', data, offset)[0]
    end = offset + 2 + 12 * count + 4
    if not 1 <= count <= 1024 or end > len(data):
        raise InvalidProfileError('Truncated or excessive IFD', code='invalid_ifd')
    if struct.unpack_from(endian + 'I', data, end - 4)[0]:
        raise UnsupportedError('Multiple IFDs / embedded DNG images are not standalone DCPs', code='unsupported_ifd')
    tags: dict[int, TagValue] = {}
    total = 0
    for i in range(count):
        checkpoint(context, stage='dcp.parse', fraction=i / count)
        pos = offset + 2 + i * 12
        tag, kind, n, pointer = struct.unpack_from(endian + 'HHII', data, pos)
        if tag in tags:
            raise InvalidProfileError(f'Duplicate tag {tag}', code='duplicate_tag')
        if kind not in _TYPES or not 1 <= n <= _MAX_VALUES:
            raise InvalidProfileError(f'Invalid type/count for tag {tag}', code='invalid_tag')
        size, fmt = _TYPES[kind]
        length = size * n
        total += length
        if length > _MAX_BYTES or total > _MAX_BYTES:
            raise InvalidProfileError('Excessive total tag payload', code='profile_too_large')
        start = pos + 8 if length <= 4 else pointer
        if start + length > len(data) or (length > 4 and (start < 8 or start < end and start + length > offset)):
            raise InvalidProfileError(f'Tag {tag} payload outside data region', code='invalid_offset')
        if tag in _EXPECTED:
            kinds, expected_count = _EXPECTED[tag]
            if kind not in kinds or expected_count is not None and n != expected_count:
                raise InvalidProfileError(f'Wrong TIFF type/count for {_NAMES[tag]}', code='invalid_tag')
        payload = data[start:start + length]
        if kind == 2:
            if not payload.endswith(b'\0'):
                raise InvalidProfileError('Unterminated profile string', code='invalid_string')
            try:
                value: TagValue = payload.rstrip(b'\0').decode('utf-8')
            except UnicodeDecodeError as exc:
                raise InvalidProfileError('Invalid profile string encoding', code='invalid_string') from exc
        elif kind == 7:
            value = payload
        elif kind in (5, 10):
            pairs = tuple(struct.iter_unpack(endian + fmt, payload))
            if any(den == 0 for _, den in pairs):
                raise InvalidProfileError('Zero rational denominator', code='invalid_rational')
            values = tuple(num / den for num, den in pairs)
            value = values[0] if n == 1 else values
        else:
            values = tuple(item[0] for item in struct.iter_unpack(endian + fmt, payload))
            if not np.all(np.isfinite(values)):
                raise InvalidProfileError('NaN/Inf in tag', code='nonfinite_profile')
            value = values[0] if n == 1 else values
        tags[tag] = value
    checkpoint(context, stage='dcp.parse', fraction=1.)
    return tags


def _unsupported(tags: dict[int, TagValue], active: tuple[bool, bool, bool], *, technical: bool) -> None:
    unknown = set(tags) - _SUPPORTED
    if unknown:
        raise UnsupportedError(f'Unrecognized potentially processing-affecting tags: {sorted(unknown)}', code='unsupported_tags')
    affected = _GLOBAL_PROCESSING & tags.keys()
    if technical:
        affected |= (_THIRD_CAMERA | _CUSTOM_ILLUMINANT) & tags.keys()
        if _scalar(tags, 50879) != 0:
            affected.add(50879)
    if active[0]:
        affected |= ({52537, 52529} | _CUSTOM_ILLUMINANT) & tags.keys()
    if affected:
        raise UnsupportedError(f'Unsupported active DNG processing tags: {sorted(affected)}', code='unsupported_tags')
    if technical:
        for tag in _CAMERA_EXTRA & tags.keys():
            a = _array(tags[tag])
            identity = (tag in (50723, 50724, 50725, 50726) and np.allclose(a.reshape(3, 3), np.eye(3), atol=1e-12))
            identity |= tag == 50727 and np.allclose(a, 1, atol=1e-12)
            identity |= tag == 50730 and np.all(a == 0)
            if not identity:
                raise UnsupportedError(f'{_NAMES[tag]} requires an individual RAW/calibration pipeline', code='unsupported_calibration')
    for enabled, tag, dims_tag in ((active[0], 51107, 50937), (active[1], 51108, 50981)):
        if enabled and _scalar(tags, tag) not in (0, 1):
            raise UnsupportedError(f'Unsupported active table encoding tag {tag}', code='unsupported_encoding')
        if enabled and _scalar(tags, tag) == 1 and _array(tags[dims_tag])[2] == 1:
            raise UnsupportedError('Encoding 1 with a single value slice has ambiguous SDK compatibility', code='unsupported_encoding_dimensions')
    if technical or active[1] or active[2]:
        if _scalar(tags, 51109) != 0:
            raise UnsupportedError('Nonzero BaselineExposureOffset requires an exposure recipe', code='unsupported_exposure')
    if active[2] and _scalar(tags, 51110) not in (0, 1):
        raise UnsupportedError('Unsupported DefaultBlackRender value', code='unsupported_black_render')


def _settings(settings: DcpSettings, *, technical: bool) -> None:
    if settings.gamut not in ('clip', 'error'):
        raise ValidationError('Gamut must be clip or error', code='invalid_settings')
    if any(type(x) is not bool for x in (settings.include_hue_sat_map, settings.include_look_table, settings.include_tone_curve)):
        raise ValidationError('Component switches must be booleans', code='invalid_settings')
    if settings.illuminant_kelvin is not None:
        k = settings.illuminant_kelvin
        if isinstance(k, bool) or not np.isfinite(k) or k <= 0:
            raise ValidationError('Illuminant Kelvin must be finite and positive', code='invalid_wb')
    if technical and settings.camera_neutral is None:
        raise ValidationError('Camera-linear rendering requires camera_neutral', code='missing_wb')
    if settings.camera_neutral is not None:
        a = np.asarray(settings.camera_neutral, dtype=np.float64)
        if a.shape != (3,) or not np.all(np.isfinite(a)) or np.any(a <= 0):
            raise ValidationError('camera_neutral must contain three finite positive values', code='invalid_wb')


def _weight(tags: dict[int, TagValue], kelvin: float | None, *, dual: bool) -> float:
    if not dual:
        return 1.
    if kelvin is None:
        raise ValidationError('Dual-illuminant processing requires explicit illuminant_kelvin', code='missing_illuminant')
    temps = []
    for tag in (50778, 50779):
        code = _scalar(tags, tag)
        if code not in _TEMPERATURES:
            raise UnsupportedError(f'Calibration illuminant {code:g} has no supported fixed CCT', code='unsupported_illuminant')
        temps.append(_TEMPERATURES[code])
    t1, t2 = temps
    if t1 == t2:
        raise InvalidProfileError('Dual calibration illuminants must differ', code='invalid_illuminants')
    return float(np.clip((1 / kelvin - 1 / t2) / (1 / t1 - 1 / t2), 0., 1.))


def _camera_to_xyz(tags: dict[int, TagValue], settings: DcpSettings) -> np.ndarray:
    if 50721 not in tags:
        raise UnsupportedError('ColorMatrix1 is required for technical conversion', code='missing_color_matrix')
    dual = 50722 in tags or 50965 in tags
    w = _weight(tags, settings.illuminant_kelvin, dual=dual)
    cm = _matrix(tags, 50721)
    if 50722 in tags:
        cm = w * cm + (1 - w) * _matrix(tags, 50722)
    if np.linalg.cond(cm) > 1e10 or np.linalg.svd(cm, compute_uv=False)[-1] <= 1e-12:
        raise InvalidProfileError('Interpolated ColorMatrix is singular', code='singular_matrix')
    neutral = np.asarray(settings.camera_neutral, dtype=np.float64)
    neutral = neutral / np.max(neutral)
    if np.any(neutral <= 1e-12):
        raise ValidationError('Normalized camera neutral is numerically degenerate', code='invalid_wb')
    if 50964 in tags or 50965 in tags:
        def forward(tag: int) -> np.ndarray:
            m = _matrix(tags, tag)
            # Correct allowed coefficient-rounding error in each endpoint's PCS white.
            return np.diag(_D50 / m.sum(axis=1)) @ m
        fm = forward(50964 if 50964 in tags else 50965)
        if 50964 in tags and 50965 in tags:
            fm = w * fm + (1 - w) * forward(50965)
        if np.linalg.cond(fm) > 1e10 or np.linalg.svd(fm, compute_uv=False)[-1] <= 1e-12:
            raise InvalidProfileError('Interpolated ForwardMatrix is singular', code='singular_matrix')
        return fm @ np.diag(1 / neutral)
    inv = np.linalg.inv(cm)
    white = inv @ neutral
    if np.any(white <= 0):
        raise InvalidProfileError('Camera neutral maps to nonpositive XYZ white', code='invalid_white')
    return _adapt(white / white[1], _D50) @ inv / white[1]


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    hi = np.max(rgb, axis=-1)
    lo = np.min(rgb, axis=-1)
    d = hi - lo
    safe = np.where(d == 0, 1., d)
    r, g, b = np.moveaxis(rgb, -1, 0)
    h = np.where(hi == r, (g - b) / safe, np.where(hi == g, (b - r) / safe + 2, (r - g) / safe + 4))
    h = np.where(d == 0, 0., h / 6 % 1)
    s = np.divide(d, hi, out=np.zeros_like(d), where=hi != 0)
    return np.stack((h, s, hi), axis=-1)


def _hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    h, s, v = np.moveaxis(hsv, -1, 0)
    k = (h[..., None] * 6 + np.array([0., 4., 2.])) % 6
    shape = np.clip(np.abs(k - 3) - 1, 0, 1)
    return v[..., None] * (1 - s[..., None] + s[..., None] * shape)


def _apply_table(rgb: np.ndarray, table: np.ndarray, encoding: int) -> np.ndarray:
    hsv = _rgb_to_hsv(rgb)
    h, s, v = np.moveaxis(hsv, -1, 0)
    nv, nh, ns, _ = table.shape
    if encoding == 1 and nv == 1:
        raise UnsupportedError('Encoding 1 with a single value slice has ambiguous SDK compatibility', code='unsupported_encoding_dimensions')
    ve = _encode(v) if encoding == 1 else v
    coords = (np.clip(ve, 0, 1) * (nv - 1), (h % 1) * nh, np.clip(s, 0, 1) * (ns - 1))
    base = [np.floor(c).astype(np.int64) for c in coords]
    frac = [c - i for c, i in zip(coords, base)]
    result = np.zeros_like(hsv)
    for dv in (0, 1):
        for dh in (0, 1):
            for ds in (0, 1):
                weight = ((frac[0] if dv else 1 - frac[0]) * (frac[1] if dh else 1 - frac[1])
                          * (frac[2] if ds else 1 - frac[2]))
                result += weight[..., None] * table[np.minimum(base[0] + dv, nv - 1),
                                                     (base[1] + dh) % nh,
                                                     np.minimum(base[2] + ds, ns - 1)]
    outv = np.clip(ve * result[..., 2], 0, 1)
    hsv = np.stack(((h + result[..., 0] / 360) % 1,
                    np.clip(s * result[..., 1], 0, 1), _decode(outv) if encoding == 1 else outv), axis=-1)
    return _hsv_to_rgb(hsv)


def _apply_tone(rgb: np.ndarray, curve: PchipInterpolator) -> np.ndarray:
    lo = np.min(rgb, axis=-1, keepdims=True)
    hi = np.max(rgb, axis=-1, keepdims=True)
    a, b = curve(lo), curve(hi)
    fraction = np.divide(rgb - lo, hi - lo, out=np.zeros_like(rgb), where=hi != lo)
    return a + fraction * (b - a)


def _bounded(rgb: np.ndarray, gamut: str, where: str) -> np.ndarray:
    if not np.all(np.isfinite(rgb)):
        raise ValidationError(f'Nonfinite RGB at {where}', code='nonfinite_rgb')
    if gamut == 'error' and (np.any(rgb < -2e-7) or np.any(rgb > 1 + 2e-7)):
        raise ValidationError(f'RGB outside [0,1] at {where}', code='out_of_gamut')
    return np.clip(rgb, 0., 1.)


def _input(rgb: RGB, gamut: str) -> np.ndarray:
    try:
        a = np.asarray(rgb, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValidationError('RGB must be numeric', code='invalid_rgb') from exc
    if a.ndim < 1 or a.shape[-1] != 3:
        raise ValidationError('RGB shape must be (...,3)', code='invalid_rgb')
    return _bounded(a, gamut, 'input')


def _components(tags: dict[int, TagValue], settings: DcpSettings, active: tuple[bool, bool, bool]):
    hsm = _table(tags, 50937, 50938) if active[0] else None
    if hsm is not None and 50939 in tags:
        w = _weight(tags, settings.illuminant_kelvin, dual=True)
        hsm = w * hsm + (1 - w) * _table(tags, 50937, 50939)
    look = _table(tags, 50981, 50982) if active[1] else None
    curve = _tone(tags) if active[2] else None
    return hsm, look, curve


def _run_components(rgb: np.ndarray, tags: dict[int, TagValue], components, gamut: str) -> np.ndarray:
    hsm, look, curve = components
    if hsm is not None:
        rgb = _apply_table(rgb, hsm, int(_scalar(tags, 51107)))
    if look is not None:
        rgb = _apply_table(rgb, look, int(_scalar(tags, 51108)))
    if curve is not None:
        rgb = _apply_tone(_bounded(rgb, gamut, 'tone input'), curve)
    return rgb


def _technical_prepare(profile: DcpProfile, settings: DcpSettings):
    _settings(settings, technical=True)
    tags = dict(profile.tags)
    _validate_tags(tags)
    active = (settings.include_hue_sat_map and 50938 in tags,
              settings.include_look_table and 50982 in tags,
              settings.include_tone_curve and 50940 in tags)
    _unsupported(tags, active, technical=True)
    matrix = np.linalg.solve(_PROPHOTO_XYZ, _camera_to_xyz(tags, settings))
    return tags, matrix, _components(tags, settings, active)


def _technical_capability(tags: dict[int, TagValue]) -> Capability:
    reasons = ['Requires normalized camera-linear RGB and explicit camera neutral; no RAW decoding.',
               'Individual CameraCalibration and AnalogBalance are not inferred.']
    try:
        _unsupported(tags, (50938 in tags, 50982 in tags, 50940 in tags), technical=True)
        if 50721 not in tags:
            raise UnsupportedError('Missing ColorMatrix1', code='missing_color_matrix')
        if 50722 in tags or 50965 in tags or 50939 in tags:
            _weight(tags, 5000., dual=True)
            reasons.append('Dual profile requires explicit illuminant_kelvin.')
    except (UnsupportedError, InvalidProfileError) as exc:
        return Capability('technical', 'unsupported', (str(exc),))
    return Capability('technical', 'supported', tuple(reasons))


def read_dcp(path: Path, *, context: JobContext | None = None) -> DcpProfile:
    checkpoint(context, stage='dcp.read', fraction=0.)
    path = Path(path)
    try:
        with path.open('rb') as stream:
            data = stream.read(_MAX_BYTES + 1)
    except OSError as exc:
        raise InvalidProfileError(f'Cannot read DCP: {path}', code='profile_io') from exc
    if len(data) > _MAX_BYTES:
        raise InvalidProfileError('DCP exceeds 32 MiB limit', code='profile_too_large')
    tags = _read_tags(data, context)
    _validate_tags(tags)
    warnings = ['Camera JPEG reproduction is unverified. Standalone DCP is not a sensor/RAW pipeline.',
                'No individual-camera calibration or analog balance is inferred.',
                'No host default tone, exposure compensation, or automatic black/shadow rendering is added.']
    if 50940 in tags:
        warnings.append('Tone uses monotone PCHIP, not the Adobe SDK spline; results are not bitwise Adobe renders.')
    if _scalar(tags, 51110) == 0:
        warnings.append('DefaultBlackRender=Auto (including absent tag): host-dependent automatic black rendering is not reproduced.')
    for tag, label in ((50938, 'HueSatMap'), (50982, 'LookTable'), (50940, 'ToneCurve')):
        if tag not in tags:
            warnings.append(f'{label} absent; no replacement is invented.')
    if not any(t in tags for t in (50938, 50982, 50940)):
        warnings.append('Matrix-only profile contains no camera JPEG-look information.')
    if set(tags) - _SUPPORTED:
        warnings.append(f'Unknown tags prevent rendering: {sorted(set(tags) - _SUPPORTED)}')
    summary = tuple((_NAMES.get(tag, f'Tag{tag}'), str(value) if not isinstance(value, tuple) or len(value) < 10
                     else f'{len(value)} numeric values') for tag, value in sorted(tags.items()))
    info = ProfileInfo(path=path, sha256=hashlib.sha256(data).hexdigest(), format='dcp',
                       description=str(tags.get(50936, tags.get(50708, path.stem))),
                       device_class='camera', color_space='camera-linear RGB', pcs='XYZ D50',
                       version='DCP classic TIFF', tags=summary,
                       capabilities=(_technical_capability(tags), Capability('match_reference', 'missing'),
                                     Capability('jpeg_accuracy', 'unverified')),
                       warnings=tuple(warnings))
    profile = DcpProfile(info, tuple(sorted(tags.items())))
    from .look import look_capabilities
    profile = replace(profile, info=replace(info, capabilities=info.capabilities + look_capabilities(profile)))
    checkpoint(context, stage='dcp.read', fraction=1.)
    return profile


class _DcpTransform:
    def __init__(self, profile: DcpProfile, settings: DcpSettings):
        self.input_space = SpaceSpec('camera_linear', profile.info.path)
        self.output_space = SpaceSpec('srgb')
        self._tags, self._matrix, self._components = _technical_prepare(profile, settings)
        self._gamut = settings.gamut
        self._closed = False

    def apply(self, rgb: RGB, *, context: JobContext | None = None) -> RGB:
        checkpoint(context, stage='dcp.render', fraction=0.)
        if self._closed:
            raise ValidationError('Transform is closed', code='closed_transform')
        a = _input(rgb, self._gamut)
        flat = a.reshape(-1, 3)
        out = np.empty_like(flat, dtype=np.float32)
        for start in range(0, len(flat), 32768):
            checkpoint(context, stage='dcp.render', fraction=start / max(len(flat), 1))
            working = _bounded(flat[start:start + 32768] @ self._matrix.T, self._gamut, 'linear ProPhoto')
            working = _run_components(working, self._tags, self._components, self._gamut)
            linear = _bounded(working @ _WORK_TO_SRGB.T, self._gamut, 'linear sRGB output')
            out[start:start + 32768] = _encode(linear)
        checkpoint(context, stage='dcp.render', fraction=1.)
        return out.reshape(a.shape)

    def close(self) -> None:
        self._closed = True


def create_dcp_transform(profile: DcpProfile, *, settings: DcpSettings,
                         context: JobContext | None = None) -> RGBTransform:
    checkpoint(context, stage='dcp.prepare', fraction=0.)
    transform = _DcpTransform(profile, settings)
    checkpoint(context, stage='dcp.prepare', fraction=1.)
    return transform


def render_dcp(rgb: RGB, profile: DcpProfile, *, settings: DcpSettings,
               context: JobContext | None = None) -> RGB:
    transform = create_dcp_transform(profile, settings=settings, context=context)
    try:
        return transform.apply(rgb, context=context)
    finally:
        transform.close()
