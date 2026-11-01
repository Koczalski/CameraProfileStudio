"""Bounded ICC v2/v4 structure inspection, independent of native LCMS loading."""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
import numpy as np

from .errors import InvalidProfileError, UnsupportedError, checkpoint
from .types import Capability, JobContext, ProfileInfo

MAX_PROFILE_BYTES = 64 * 1024 * 1024
_MAX_TAGS = 4096
_MATRIX_TAGS = {'rXYZ', 'gXYZ', 'bXYZ', 'rTRC', 'gTRC', 'bTRC'}
_KNOWN_TYPES = {'XYZ ', 'curv', 'para', 'desc', 'mluc', 'text', 'mft1', 'mft2', 'mAB ', 'mBA ',
                'sf32', 'chad', 'sig ', 'meas', 'view', 'dtim', 'chrm', 'data', 'ui08', 'ui16', 'ui32', 'ui64'}


def _invalid(message: str) -> None:
    raise InvalidProfileError(message, code='invalid_icc')


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        _invalid('ICC field extends beyond its containing structure.')
    return struct.unpack_from('>I', data, offset)[0]


def _sig(data: bytes) -> str:
    return data.decode('ascii', errors='replace')


def _curve(data: bytes, offset: int = 0) -> tuple[int, str]:
    if offset + 12 > len(data):
        _invalid('Truncated ICC curve.')
    kind = data[offset:offset + 4]
    if kind == b'curv':
        count = _u32(data, offset + 8)
        end = offset + 12 + count * 2
        if count > 1048576 or end > len(data):
            _invalid('ICC curve sample count exceeds tag bounds or the one-million-sample safety limit.')
        if count == 1 and struct.unpack_from('>H', data, offset + 12)[0] == 0:
            _invalid('ICC gamma must be positive.')
        return end, f'curve: {count} entries' if count != 1 else f'gamma: {struct.unpack_from(">H", data, offset + 12)[0] / 256:g}'
    if kind == b'para':
        function = struct.unpack_from('>H', data, offset + 8)[0]
        if function > 4:
            _invalid('Invalid parametric curve function number.')
        count = (1, 3, 4, 5, 7)[function]
        end = offset + 12 + count * 4
        if end > len(data):
            _invalid('Truncated parametric curve.')
        if struct.unpack_from('>i', data, offset + 12)[0] <= 0:
            _invalid('ICC parametric gamma must be positive.')
        return end, f'parametric curve: function {function}'
    _invalid('Expected ICC curveType or parametricCurveType.')


def _curve_directions(data: bytes) -> tuple[bool, bool, str]:
    """Prove matrix/TRC direction suitability, independently of LCMS tag presence."""
    _curve(data)
    if data[:4] == b'curv':
        count = _u32(data, 8)
        if count < 2:
            return True, True, ''
        values = np.frombuffer(data, dtype='>u2', count=count, offset=12).astype(np.int32)
        changes = np.diff(values)
        if np.any(changes < 0):
            return False, False, 'Sampled TRC is nonmonotonic.'
        if not np.any(changes > 0):
            return False, False, 'Sampled TRC is constant and has no inverse.'
        if np.any(changes == 0):
            return True, False, 'Sampled TRC contains flat intervals; a unique inverse is unavailable.'
        return True, True, ''
    function = struct.unpack_from('>H', data, 8)[0]
    count = (1, 3, 4, 5, 7)[function]
    parameters = np.array(struct.unpack_from('>' + 'i' * count, data, 12), dtype=float) / 65536
    gamma = parameters[0]
    if function == 0:
        return True, True, ''
    a, b = parameters[1:3]
    if a == 0:
        return False, False, 'Parametric TRC has a zero power-branch scale.'
    if function in (1, 2):
        boundary = -b / a
        slope = 0.0
        offset = parameters[3] if function == 2 else 0.0
        high_offset = offset
    else:
        slope, boundary = parameters[3:5]
        high_offset = parameters[5] if function == 4 else 0.0
        offset = parameters[6] if function == 4 else 0.0
    high_active, low_active = boundary <= 1, boundary > 0
    if high_active and (a < 0 or a * max(0, boundary) + b < -1e-10):
        return False, False, 'Parametric TRC power branch is decreasing or outside its real-valued domain.'
    if low_active and slope < 0:
        return False, False, 'Parametric TRC linear branch is decreasing.'
    if low_active and not high_active and slope == 0:
        return False, False, 'Parametric TRC is constant over the device domain.'
    if high_active:
        with np.errstate(over='ignore', invalid='ignore'):
            endpoint = np.power(a + b, gamma) + high_offset
        if not np.isfinite(endpoint):
            return False, False, 'Parametric TRC produces a nonfinite endpoint.'
        if boundary >= 1 and slope == 0 and endpoint == offset:
            return False, False, 'Parametric TRC is constant over the device domain.'
    continuous = True
    if 0 < boundary < 1:
        high = max(0, a * boundary + b) ** gamma + high_offset
        low = slope * boundary + offset
        # ICC s15Fixed16 parameter rounding can introduce tiny boundary mismatches.
        tolerance = 2 / 65536
        if high < low - tolerance:
            return False, False, 'Parametric TRC has a decreasing branch discontinuity.'
        continuous = abs(high - low) <= tolerance
    if low_active and slope == 0:
        return True, False, 'Parametric TRC contains a flat interval; a unique inverse is unavailable.'
    if not continuous:
        return True, False, 'Parametric TRC has a discontinuity; a continuous inverse is unavailable.'
    return True, True, ''


def _lut_summary(data: bytes) -> str:
    kind = data[:4]
    if len(data) < (52 if kind == b'mft2' else 48 if kind == b'mft1' else 32):
        _invalid('Truncated ICC LUT header.')
    inputs, outputs = data[8:10]
    if not 1 <= inputs <= 15 or not 1 <= outputs <= 15:
        _invalid('Invalid ICC LUT channel count.')
    if kind in (b'mft1', b'mft2'):
        grid = data[10]
        if grid < 2:
            _invalid('ICC LUT grid must contain at least two points per dimension.')
        if kind == b'mft1':
            required = 48 + 256 * inputs + (grid ** inputs) * outputs + 256 * outputs
        else:
            nin, nout = struct.unpack_from('>HH', data, 48)
            if not 2 <= nin <= 4096 or not 2 <= nout <= 4096:
                _invalid('Invalid ICC 16-bit LUT table lengths.')
            required = 52 + 2 * (nin * inputs + (grid ** inputs) * outputs + nout * outputs)
        if required > len(data):
            _invalid('ICC CLUT/table dimensions exceed tag bounds.')
        return f'{_sig(kind)}: {inputs}->{outputs} channels, grid {grid}'
    offsets = [_u32(data, at) for at in (12, 16, 20, 24, 28)]
    for offset in offsets:
        if offset and (offset < 32 or offset % 4 or offset >= len(data)):
            _invalid('Invalid multi-stage ICC LUT offset.')
    b_offset, matrix_offset, m_offset, clut_offset, a_offset = offsets
    if not b_offset:
        _invalid('Multi-stage ICC LUT requires B curves.')
    reverse = kind == b'mBA '
    for offset, count in ((b_offset, inputs if reverse else outputs),
                          (m_offset, inputs if reverse else outputs),
                          (a_offset, outputs if reverse else inputs)):
        if offset:
            for _ in range(count):
                end, _ = _curve(data, offset)
                offset = (end + 3) & ~3
    if matrix_offset and matrix_offset + 48 > len(data):
        _invalid('Truncated multi-stage ICC matrix.')
    if clut_offset:
        if clut_offset + 20 > len(data):
            _invalid('Truncated multi-stage ICC CLUT header.')
        grids = data[clut_offset:clut_offset + inputs]
        precision = data[clut_offset + 16]
        if any(g < 2 for g in grids) or precision not in (1, 2):
            _invalid('Invalid multi-stage ICC CLUT dimensions/precision.')
        entries = 1
        for grid in grids:
            entries *= grid
        if clut_offset + 20 + entries * outputs * precision > len(data):
            _invalid('Multi-stage ICC CLUT extends beyond tag bounds.')
    return f'{_sig(kind)}: {inputs}->{outputs} channels, stages {sum(bool(o) for o in offsets)}'


def _description(data: bytes) -> str:
    kind = data[:4]
    if kind == b'desc':
        count = _u32(data, 8)
        if count < 1 or 12 + count > len(data):
            _invalid('Invalid ICC description length.')
        return data[12:12 + count].rstrip(b'\0').decode('latin-1')
    if kind == b'mluc':
        count, record_size = _u32(data, 8), _u32(data, 12)
        if count > _MAX_TAGS or record_size < 12 or 16 + count * record_size > len(data):
            _invalid('Invalid multilingual ICC description directory.')
        texts = []
        for index in range(count):
            start = 16 + index * record_size
            length, offset = _u32(data, start + 4), _u32(data, start + 8)
            if length % 2 or offset < 16 + count * record_size or offset + length > len(data):
                _invalid('Invalid multilingual ICC string bounds.')
            try:
                texts.append((data[start:start + 2], data[offset:offset + length].decode('utf-16-be')))
            except UnicodeDecodeError as exc:
                raise InvalidProfileError('Malformed UTF-16 ICC description.', code='invalid_icc') from exc
        return next((value for language, value in texts if language == b'en'), texts[0][1] if texts else '')
    return ''


def _read_profile(path: Path) -> bytes:
    path = Path(path)
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_PROFILE_BYTES + 1)
    except OSError as exc:
        raise InvalidProfileError(f'Cannot read profile: {path}', code='profile_io', details=(('path', str(path)),)) from exc
    if len(data) > MAX_PROFILE_BYTES:
        _invalid('ICC profile exceeds the 64 MiB safety limit.')
    return data


def _parse_icc(data: bytes, path: Path) -> ProfileInfo:
    if len(data) < 132:
        _invalid('ICC header/tag directory is truncated.')
    size = _u32(data, 0)
    if size < 132 or size > len(data) or size > MAX_PROFILE_BYTES:
        _invalid('ICC declared size is invalid or truncated.')
    if data[36:40] != b'acsp':
        _invalid('Missing ICC acsp signature.')
    major = data[8]
    if major not in (2, 4):
        raise UnsupportedError(f'ICC version {major} is unsupported.', code='unsupported_icc_version')
    version = f'{major}.{data[9] >> 4}.{data[9] & 15}'
    device_class, color_space, pcs = (_sig(data[start:start + 4]) for start in (12, 16, 20))
    count = _u32(data, 128)
    end_directory = 132 + count * 12
    if count > _MAX_TAGS or end_directory > size:
        _invalid('ICC tag count exceeds file bounds or safety limits.')
    tags: dict[str, bytes] = {}
    summaries = []
    warnings = []
    regions: list[tuple[int, int]] = []
    description = ''
    if size != len(data):
        warnings.append('Trailing bytes after declared ICC profile size are ignored.')
    for index in range(count):
        pos = 132 + index * 12
        name, offset, length = _sig(data[pos:pos + 4]), _u32(data, pos + 4), _u32(data, pos + 8)
        if name in tags:
            _invalid(f'Duplicate ICC tag: {name}')
        if offset < end_directory or offset % 4 or length < 8 or offset + length > size:
            _invalid(f'ICC tag {name} has invalid bounds/alignment.')
        region = (offset, offset + length)
        if any(region != old and region[0] < old[1] and old[0] < region[1] for old in regions):
            _invalid('ICC tags overlap without sharing the exact same data block.')
        regions.append(region)
        tag = data[offset:offset + length]
        tags[name] = tag
        tag_type = _sig(tag[:4])
        summary = f'{tag_type}: {length} bytes'
        if tag_type in ('desc', 'mluc'):
            text = _description(tag)
            summary = text
            if name == 'desc':
                description = text
        elif tag_type in ('curv', 'para'):
            _, summary = _curve(tag)
        elif tag_type == 'XYZ ':
            if len(tag) < 20 or (len(tag) - 8) % 12:
                _invalid('Invalid ICC XYZ array length.')
            xyz = struct.unpack_from('>iii', tag, 8)
            summary = 'XYZ: ' + ', '.join(f'{v / 65536:.7g}' for v in xyz)
        elif tag_type in ('mft1', 'mft2', 'mAB ', 'mBA '):
            summary = _lut_summary(tag)
        elif tag_type == 'sf32' and (len(tag) - 8) % 4:
            _invalid('Invalid ICC s15Fixed16 array length.')
        elif tag_type not in _KNOWN_TYPES:
            warnings.append(f'Uninterpreted tag {name} type {tag_type}.')
        summaries.append((name, summary))
    matrix = _MATRIX_TAGS.issubset(tags)
    if matrix:
        if any(tags[name][:4] != b'XYZ ' for name in ('rXYZ', 'gXYZ', 'bXYZ')) or any(tags[name][:4] not in (b'curv', b'para') for name in ('rTRC', 'gTRC', 'bTRC')):
            _invalid('Matrix/TRC profile has incompatible tag data types.')
        columns = np.array([struct.unpack_from('>iii', tags[name], 8) for name in ('rXYZ', 'gXYZ', 'bXYZ')], dtype=float).T / 65536
        if abs(np.linalg.det(columns)) < 1e-12:
            _invalid('ICC colorant matrix is singular.')
    matrix_forward = matrix_reverse = matrix
    forward_curve_reasons: list[str] = []
    reverse_curve_reasons: list[str] = []
    if matrix:
        for name in ('rTRC', 'gTRC', 'bTRC'):
            can_forward, can_reverse, reason = _curve_directions(tags[name])
            matrix_forward = matrix_forward and can_forward
            matrix_reverse = matrix_reverse and can_reverse
            if not can_forward:
                forward_curve_reasons.append(f'{name}: {reason}')
            if not can_reverse:
                reverse_curve_reasons.append(f'{name}: {reason}')
        warnings.extend(dict.fromkeys(forward_curve_reasons + reverse_curve_reasons))
    forward = matrix_forward or any(k in ('A2B0', 'A2B1', 'A2B2') and v[:4] in (b'mft1', b'mft2', b'mAB ') for k, v in tags.items())
    reverse = matrix_reverse or any(k in ('B2A0', 'B2A1', 'B2A2') and v[:4] in (b'mft1', b'mft2', b'mBA ') for k, v in tags.items())
    reasons = []
    if device_class not in ('scnr', 'mntr', 'prtr', 'spac'):
        reasons.append(f'Unsupported profile class: {device_class}.')
    if color_space != 'RGB ' or pcs not in ('XYZ ', 'Lab '):
        reasons.append('Only RGB device profiles with XYZ/Lab PCS are supported.')
    if not forward and not reverse:
        reasons.append('No supported RGB matrix/TRC or directional CLUT path.')
        reasons.extend(dict.fromkeys(forward_curve_reasons + reverse_curve_reasons))
    if any(name.startswith(('D2B', 'B2D')) for name in tags):
        reasons.append('Floating-point multi-process-element ICC paths require separate validation.')
    intent_capabilities = []
    for direction, prefix, matrix_valid, curve_reasons, types in (
        ('input', 'A2B', matrix_forward, forward_curve_reasons, (b'mft1', b'mft2', b'mAB ')),
        ('output', 'B2A', matrix_reverse, reverse_curve_reasons, (b'mft1', b'mft2', b'mBA ')),
    ):
        for intent, index in (('perceptual', 0), ('relative', 1), ('saturation', 2), ('absolute', 1)):
            tag_name = prefix + str(index)
            allowed = tags[tag_name][:4] in types if tag_name in tags else matrix_valid
            explanation = tuple(reasons) if reasons else () if allowed else tuple(curve_reasons) or (f'No validated {tag_name} or invertible matrix/TRC path for {intent}.',)
            intent_capabilities.append(Capability(f'technical.{direction}.{intent}',
                'supported' if allowed and not reasons else 'unsupported', explanation))
    warnings.append('ICC device RGB encoding is profile-specific; not assumed camera-linear or sRGB.')
    return ProfileInfo(path=Path(path), sha256=hashlib.sha256(data).hexdigest(), format='icc',
                       description=description or Path(path).name, device_class=device_class.strip(),
                       color_space=color_space.strip(), pcs=pcs.strip(), version=version,
                       tags=tuple(summaries), capabilities=(
                           Capability('technical', 'unsupported' if reasons else 'supported', tuple(reasons)),
                           Capability('technical.input', 'supported' if forward and not reasons else 'unsupported', tuple(reasons) if reasons else () if forward else tuple(forward_curve_reasons) or ('Missing device-to-PCS path.',)),
                           Capability('technical.output', 'supported' if reverse and not reasons else 'unsupported', tuple(reverse_curve_reasons + reasons) if reasons else () if reverse else tuple(reverse_curve_reasons) or ('Missing PCS-to-device path; profile inversion is not inferred.',)),
                           Capability('look', 'unsupported', ('ICC camera correction is not an isolated camera JPEG look.',)),
                           Capability('match_reference', 'missing'), Capability('jpeg_accuracy', 'unverified'),
                           *intent_capabilities), warnings=tuple(warnings))


def inspect_icc(path: Path, *, context: JobContext | None = None) -> ProfileInfo:
    checkpoint(context, stage='icc.inspect', fraction=0)
    result = _parse_icc(_read_profile(path), Path(path))
    checkpoint(context, stage='icc.inspect', fraction=1)
    return result
