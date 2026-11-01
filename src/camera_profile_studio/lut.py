"""Float LUT generation, tetrahedral sampling, strict red-fastest cube I/O and
PNG LUT images (Hald CLUT and horizontal strip) for photo and game software."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import errno
import platform
import shutil
import struct
import tempfile
import zlib

import numpy as np

from .errors import ExportError, ValidationError, checkpoint
from .types import ExportResult, Gamut, JobContext, Lut3D, Progress, RGB, RGBTransform, SpaceSpec, ValidationReport

_SIZES = (17, 33, 65)
_CHUNK = 32768


def _software() -> dict[str, str]:
    versions = {'python': platform.python_version(), 'numpy': np.__version__}
    for package in ('camera-profile-studio',):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'source-tree'
    return versions


def generate_lut(transform: RGBTransform, *, size: int = 33, strength: float = 1.0,
                 title: str = 'Camera Profile Studio', context: JobContext | None = None) -> Lut3D:
    checkpoint(context, stage='generate_lut', fraction=0)
    if type(size) is not int or size not in _SIZES:
        raise ValidationError('LUT size must be 17, 33 or 65', code='invalid_lut_size')
    if isinstance(strength, (bool, np.bool_)) or not isinstance(strength, (int, float, np.integer, np.floating)) or not np.isfinite(strength) or not 0 <= strength <= 1:
        raise ValidationError('Strength must be finite and in [0,1]', code='invalid_strength')
    if strength != 1 and transform.input_space != transform.output_space:
        raise ValidationError('Strength blending requires identical input/output spaces', code='strength_space_mismatch')
    axis = np.linspace(0, 1, size, dtype=np.float32)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1).reshape(-1, 3)
    values = np.empty_like(grid)
    for start in range(0, len(grid), _CHUNK):
        end = min(start + _CHUNK, len(grid))
        # Evaluate even at zero strength; unsupported transforms must never become identity successes.
        child_context = JobContext(cancel=context.cancel) if context is not None else None
        full = np.asarray(transform.apply(grid[start:end], context=child_context))
        if full.dtype.kind not in 'iuf' or full.shape != grid[start:end].shape or not np.isfinite(full).all() or np.any((full < 0) | (full > 1)):
            raise ValidationError('Transform returned invalid or out-of-domain RGB', code='invalid_transform_output')
        values[start:end] = np.clip((1 - strength) * grid[start:end] + strength * full, 0, 1)
        checkpoint(context, stage='generate_lut', fraction=end / len(grid))
    provenance = dict(getattr(transform, 'provenance', ()))
    provenance.setdefault('software', json.dumps(_software(), sort_keys=True))
    provenance.setdefault('jpeg_accuracy', 'unverified')
    if hasattr(transform, 'runtime_version'):
        provenance.setdefault('lcms_version', str(transform.runtime_version))
    provenance['sampling'] = 'uniform unit RGB cube; tetrahedral; strength baked once'
    for label, space in (('input', transform.input_space), ('output', transform.output_space)):
        key = label + '_profile_sha256'
        cached_hash = getattr(transform, 'profile_hashes', {}).get(label)
        if cached_hash is not None:
            provenance.setdefault(key, cached_hash)
        if space.profile_path is not None and key not in provenance:
            try:
                provenance[key] = hashlib.sha256(space.profile_path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ValidationError('Cannot hash LUT profile', code='profile_read_error') from exc
    lut = Lut3D(values.reshape(size, size, size, 3), transform.input_space,
                transform.output_space, title, strength, tuple(provenance.items()))
    _require_valid(lut)
    return lut


def validate_lut(lut: Lut3D) -> ValidationReport:
    values = np.asarray(lut.values)
    errors = []
    if values.ndim != 4 or values.shape[-1] != 3 or len(set(values.shape[:3])) != 1 or values.shape[0] not in _SIZES:
        errors.append('Expected (N,N,N,3) RGB values with N=17,33,65')
    finite = bool(np.isfinite(values).all())
    if not finite:
        errors.append('Nonfinite LUT values')
    minimum = float(values.min()) if values.size and finite else 0.0
    maximum = float(values.max()) if values.size and finite else 0.0
    if minimum < 0 or maximum > 1:
        errors.append('LUT output outside unit domain')
    if not np.isfinite(lut.strength) or not 0 <= lut.strength <= 1:
        errors.append('Invalid strength')
    if lut.strength != 1 and lut.input_space != lut.output_space:
        errors.append('Strength requires identical spaces')
    if not isinstance(lut.title, str):
        errors.append('Title must be text')
    if len(dict(lut.provenance)) != len(lut.provenance):
        errors.append('Duplicate provenance keys')
    return ValidationReport(not errors, int(values.size // 3), minimum, maximum, tuple(errors),
                            () if lut.provenance else ('No provenance; unverified import',))


def _require_valid(lut: Lut3D) -> ValidationReport:
    report = validate_lut(lut)
    if not report.valid:
        raise ValidationError('; '.join(report.errors), code='invalid_lut')
    return report


def sample_lut(lut: Lut3D, rgb: RGB, *, out_of_domain: Gamut = 'clip',
               context: JobContext | None = None) -> RGB:
    """Sample ``lut`` at ``rgb`` (clipped to the unit cube) with tetrahedral
    interpolation, the scheme used by DaVinci Resolve, OpenColorIO and ffmpeg's
    ``lut3d``.

    Each grid cell is split into six tetrahedra that all share the cell's grey
    diagonal c000-c111; the point is interpolated on the tetrahedron selected by
    the ordering of its fractional coordinates.  Unlike trilinear interpolation
    this passes exactly through the diagonal nodes, so a LUT whose grey axis is
    neutral maps every neutral input to a neutral output (trilinear mixes in the
    six off-diagonal corner colours and tints greys).  Grid nodes are reproduced
    exactly and the result is continuous across tetrahedron boundaries.
    """
    checkpoint(context, stage='sample_lut', fraction=0)
    _require_valid(lut)
    array = np.asarray(rgb)
    if array.dtype.kind not in 'iuf' or not array.size or array.ndim < 1 or array.shape[-1] != 3 or not np.isfinite(array).all():
        raise ValidationError('Expected finite (...,3) RGB', code='invalid_rgb')
    if out_of_domain not in ('clip', 'error'):
        raise ValidationError('Invalid gamut policy', code='invalid_gamut')
    if out_of_domain == 'error' and np.any((array < 0) | (array > 1)):
        raise ValidationError('RGB outside LUT unit domain', code='out_of_domain')
    flat = array.reshape(-1, 3)
    result = np.empty(flat.shape, dtype=np.float32)
    n = lut.values.shape[0]
    table = np.asarray(lut.values).reshape(-1, 3)  # row index = (r*n + g)*n + b
    strides = np.array([n * n, n, 1], dtype=np.intp)
    for start in range(0, len(flat), _CHUNK):
        end = min(start + _CHUNK, len(flat))
        out = _tetrahedral(table, strides, np.clip(flat[start:end], 0, 1).astype(np.float64) * (n - 1), n)
        # Convex interpolation is unit-bounded analytically; contain float roundoff.
        result[start:end] = np.clip(out, 0, 1)
        checkpoint(context, stage='sample_lut', fraction=end / len(flat))
    return result.reshape(array.shape)


def _tetrahedral(table: np.ndarray, strides: np.ndarray, p: np.ndarray, n: int) -> np.ndarray:
    """Tetrahedral interpolation of ``table`` ((n**3, 3), red slowest) at grid
    coordinates ``p`` ((m, 3), each in [0, n-1]); returns float64 (m, 3).

    With cell corner ``c000`` and fractions (fr, fg, fb) the six cases are the
    orderings of the fractions, e.g. fr >= fg >= fb walks
    c000 -> c100 -> c110 -> c111 and fb > fg > fr walks c000 -> c001 -> c011 -> c111.
    In general, with fractions sorted as fa >= fb >= fc along axes a, b, c::

        out = (1-fa)*c000 + (fa-fb)*c[+a] + (fb-fc)*c[+a+b] + fc*c111

    The four weights are non-negative and sum to one.  Ties (fractions that are
    equal) select any adjacent tetrahedron and give the same value.
    """
    lo = np.minimum(p.astype(np.intp), n - 2)
    f = p - lo
    f0, f1, f2 = f[:, 0], f[:, 1], f[:, 2]
    # Rank of each axis by descending fraction (0 = largest); ties favour the lower axis.
    ge01, ge02, ge12 = f0 >= f1, f0 >= f2, f1 >= f2
    rank = np.stack(((~ge01).astype(np.intp) + ~ge02,
                     ge01.astype(np.intp) + ~ge12,
                     ge02.astype(np.intp) + ge12), axis=1)
    base = lo @ strides
    step_a = (rank == 0) @ strides           # first axis walked (largest fraction)
    step_ab = (rank <= 1) @ strides          # first two axes walked
    big = np.maximum(f0, np.maximum(f1, f2))
    small = np.minimum(f0, np.minimum(f1, f2))
    mid = np.maximum(np.minimum(f0, f1), np.minimum(np.maximum(f0, f1), f2))
    return (table[base] * (1 - big)[:, None] + table[base + step_a] * (big - mid)[:, None]
            + table[base + step_ab] * (mid - small)[:, None]
            + table[base + int(strides.sum())] * small[:, None])


def read_cube(path: Path, *, input_space: SpaceSpec, output_space: SpaceSpec,
              context: JobContext | None = None) -> Lut3D:
    checkpoint(context, stage='read_cube', fraction=0)
    path = Path(path)
    title, size, rows, seen = 'Camera Profile Studio', None, [], set()
    strength = 1.0
    hasher = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            for line_number, raw_line in enumerate(stream, 1):
                hasher.update(raw_line)
                line = raw_line.decode('utf-8-sig' if line_number == 1 else 'utf-8')
                if line_number % 4096 == 0:
                    checkpoint(context, stage='read_cube', fraction=0)
                stripped = line.strip()
                if stripped.startswith('# CPS_STRENGTH '):
                    if rows or 'CPS_STRENGTH' in seen:
                        raise ValueError('Duplicate or misplaced strength metadata')
                    seen.add('CPS_STRENGTH')
                    strength = float(stripped[len('# CPS_STRENGTH '):])
                if not stripped or stripped.startswith('#'):
                    continue
                line = stripped.split('#', 1)[0].strip() if not stripped.startswith('TITLE ') else stripped
                parts = line.split()
                key = parts[0]
                if key in ('TITLE', 'LUT_3D_SIZE', 'DOMAIN_MIN', 'DOMAIN_MAX'):
                    if rows or key in seen:
                        raise ValueError('Duplicate or misplaced header')
                    seen.add(key)
                    if key == 'TITLE':
                        title = json.loads(line[len('TITLE'):].strip())
                        if not isinstance(title, str):
                            raise ValueError('TITLE must be quoted text')
                    elif key == 'LUT_3D_SIZE':
                        if len(parts) != 2 or parts[1] not in ('17', '33', '65'):
                            raise ValueError('Unsupported LUT size')
                        size = int(parts[1])
                    else:
                        expected = 0.0 if key == 'DOMAIN_MIN' else 1.0
                        if len(parts) != 4 or any(float(v) != expected for v in parts[1:]):
                            raise ValueError('Only unit domains are supported')
                else:
                    if size is None or len(parts) != 3:
                        raise ValueError('Unsupported header/shaper or malformed RGB row')
                    rows.append(tuple(float(v) for v in parts))
                    if len(rows) > size ** 3:
                        raise ValueError('Too many data rows')
        if size is None or len(rows) != size ** 3:
            raise ValueError('Incorrect cube row count')
        values = np.asarray(rows, dtype=np.float32).reshape(size, size, size, 3).transpose(2, 1, 0, 3)
        digest = hasher.hexdigest()
        provenance = {'source_cube_sha256': digest, 'jpeg_accuracy': 'unverified'}
        companion = path.with_suffix('.json')
        if companion.exists():
            metadata = json.loads(companion.read_text(encoding='utf-8'))
            expected = {'schema_version': 1, 'cube_sha256': digest, 'size': size, 'title': title,
                        'strength': strength, 'domain_min': [0, 0, 0], 'domain_max': [1, 1, 1],
                        'input_space': json.loads(json.dumps(asdict(input_space), default=str)),
                        'output_space': json.loads(json.dumps(asdict(output_space), default=str))}
            if not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in expected.items()):
                raise ValueError('Companion metadata does not match cube content or declared spaces')
            saved = metadata.get('provenance', {})
            if not isinstance(saved, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in saved.items()):
                raise ValueError('Invalid companion provenance')
            provenance.update(saved)
            provenance['source_cube_sha256'] = digest
            provenance['import_verification'] = 'content hash checked; not independent camera accuracy verification'
        lut = Lut3D(values, input_space, output_space, title, strength, tuple(provenance.items()))
        _require_valid(lut)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValidationError(f'Invalid cube {path}: {exc}', code='invalid_cube') from exc
    checkpoint(context, stage='read_cube', fraction=1)
    return lut


def _output_path(path) -> Path:
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        raise ExportError('An output file path is required', code='invalid_export_path')
    return Path(path)


def export_cube(lut: Lut3D, path: Path, *, overwrite: bool = False,
                context: JobContext | None = None) -> ExportResult:
    checkpoint(context, stage='export_cube', fraction=0)
    report = _require_valid(lut)
    path = _output_path(path)
    if path.suffix.lower() != '.cube':
        raise ExportError('Output filename must end in .cube', code='invalid_export_path')
    size = lut.values.shape[0]

    def write(temporary: Path) -> None:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write('TITLE ' + json.dumps(lut.title, ensure_ascii=True) + '\n')
            stream.write('# CPS_INPUT ' + json.dumps(asdict(lut.input_space), default=str) + '\n')
            stream.write('# CPS_OUTPUT ' + json.dumps(asdict(lut.output_space), default=str) + '\n')
            stream.write(f'# CPS_STRENGTH {lut.strength:.17g}\n')
            stream.write(f'LUT_3D_SIZE {size}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n')
            rows = lut.values.transpose(2, 1, 0, 3).reshape(-1, 3)
            for start in range(0, len(rows), _CHUNK):
                np.savetxt(stream, rows[start:start + _CHUNK], fmt='%.9g')
                checkpoint(context, stage='export_cube', fraction=.8 * min(start + _CHUNK, len(rows)) / len(rows))
            stream.flush()
            os.fsync(stream.fileno())

    def metadata(digest: str) -> dict:
        return dict(schema_version=1, cube_sha256=digest, title=lut.title, size=size,
                    input_space=asdict(lut.input_space), output_space=asdict(lut.output_space),
                    strength=lut.strength, domain_min=[0, 0, 0], domain_max=[1, 1, 1],
                    provenance=dict(lut.provenance), software=_software(), validation=asdict(report))

    return _publish(path, '.cube', 'export_cube', write, metadata, report, overwrite, context)


def _export_code(exc: OSError, path: Path) -> str:
    """A specific code for the common ways publishing a file fails, so the user
    is told what to do instead of reading a raw OS message."""
    if exc.errno == errno.ENAMETOOLONG or getattr(exc, 'winerror', None) in (206,):
        return 'path_too_long'
    try:
        is_dir, parent_exists = path.is_dir(), path.parent.exists()
    except OSError:
        is_dir, parent_exists = False, True
    if is_dir:
        return 'invalid_export_path'
    if isinstance(exc, FileExistsError):
        return 'output_exists'
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return 'output_permission'
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT or not parent_exists:
        return 'output_dir_missing'
    if isinstance(exc, (NotADirectoryError, IsADirectoryError)) or exc.errno in (errno.ENOTDIR, errno.EISDIR):
        return 'invalid_export_path'
    if exc.errno == errno.ENOSPC:
        return 'disk_full'
    return 'export_io_error'


def _publish_exclusive(temporary: Path, destination: Path) -> None:
    """Publish ``temporary`` at ``destination`` without ever replacing a file
    that appeared meanwhile.  A hard link is atomic and used where the file
    system offers one; exFAT/FAT32 drives and some network shares do not, so
    the fallback creates the destination exclusively and copies the bytes."""
    try:
        os.link(temporary, destination)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.EMLINK, errno.EACCES) \
                and getattr(exc, 'winerror', None) not in (1, 50, 4390):
            raise
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, 'wb') as stream, temporary.open('rb') as source:
            shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _publish(path: Path, suffix: str, stage: str, write, metadata_for, report: ValidationReport,
             overwrite: bool, context: JobContext | None) -> ExportResult:
    """Write the LUT file and its JSON sidecar to temporaries, then publish
    both atomically (exclusive lock, no-overwrite hard links unless asked)."""
    metadata_path = path.with_suffix('.json')
    lock_path = path.with_name(path.name + '.lock')
    temps: list[Path] = []
    published: list[str] = []
    locked = False
    try:
        lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(lock)
        locked = True
        if not overwrite and (path.exists() or metadata_path.exists()):
            raise FileExistsError('LUT or sidecar already exists')
        for temp_suffix in (suffix + '.tmp', '.json.tmp'):
            fd, name = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix=temp_suffix, dir=path.parent)
            os.close(fd)
            temps.append(Path(name))
        write(temps[0])
        digest = hashlib.sha256(temps[0].read_bytes()).hexdigest()
        metadata = metadata_for(digest)
        with temps[1].open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(metadata, stream, indent=2, ensure_ascii=True, allow_nan=False, default=str)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint(context, stage=stage, fraction=.95)
        checkpoint(JobContext(cancel=context.cancel) if context is not None else None,
                   stage=stage, fraction=.95)
        # Exclusive hard-link creation closes the exists()/replace() no-overwrite race.
        for temporary, destination in zip(temps, (path, metadata_path), strict=True):
            if overwrite:
                os.replace(temporary, destination)
            else:
                _publish_exclusive(temporary, destination)
            published.append(str(destination))
        # Publication is the commit boundary. Late cancellation cannot turn committed files into failure.
        if context is not None and context.progress is not None:
            context.progress(Progress(stage, 1))
        return ExportResult(path, metadata_path, digest, report)
    except OSError as exc:
        raise ExportError(f'Export failed: {exc}', code='partial_publication' if published else _export_code(exc, path),
                          details=(('published', json.dumps(published)), ('path', str(path))) ) from exc
    finally:
        cleanup_errors = []
        for temporary in temps + ([lock_path] if locked else []):
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise ExportError('Export temporary/lock cleanup failed',
                              code='partial_publication' if published else 'export_io_error',
                              details=(('published', json.dumps(published)),)) from cleanup_errors[0]


# ----------------------------------------------------------------------------
# PNG LUT images
# ----------------------------------------------------------------------------

EXPORT_FORMATS = ('cube', 'hald_png', 'strip_png', 'strip_png_flipped')
# Grid of each PNG layout for a built LUT size (nearest supported grid; the
# LUT is resampled with the same tetrahedral interpolation used everywhere).
_HALD_GRID = {17: 16, 33: 36, 65: 64}
_STRIP_GRID = {17: 16, 33: 32, 65: 64}


def png_layout(format: str, size: int) -> dict:
    """Image geometry of a PNG LUT ``format`` for a built LUT of ``size``.

    hald_png: Hald CLUT (ImageMagick ``hald:`` order) - square image of
    level**3 pixels per side holding a level**2 grid; red fastest, then green,
    then blue, rows top to bottom.  16-bit RGB.
    strip_png: N tiles of N x N side by side (width N*N, height N); blue
    selects the tile left to right, red increases to the right inside a tile,
    green increases downwards (Unreal Engine / ReShade order).  8-bit RGB.
    strip_png_flipped: the strip with green increasing upwards.
    """
    if format not in EXPORT_FORMATS or format == 'cube':
        raise ExportError(f'Not a PNG LUT format: {format}', code='invalid_export_format')
    if size not in _SIZES:
        raise ValidationError('LUT size must be 17, 33 or 65', code='invalid_lut_size')
    if format == 'hald_png':
        grid = _HALD_GRID[size]
        level = int(round(grid ** 0.5))
        side = level ** 3
        return dict(format=format, layout='hald', grid=grid, level=level, width=side, height=side,
                    bit_depth=16, flipped=False, source_size=size)
    grid = _STRIP_GRID[size]
    return dict(format=format, layout='strip', grid=grid, level=None, width=grid * grid, height=grid,
                bit_depth=8, flipped=format == 'strip_png_flipped', source_size=size)


def resample_lut(lut: Lut3D, grid: int, *, context: JobContext | None = None) -> np.ndarray:
    """(grid, grid, grid, 3) values indexed [r, g, b], tetrahedral from ``lut``."""
    if grid == lut.values.shape[0]:
        return np.asarray(lut.values, dtype=np.float32)
    axis = np.linspace(0, 1, grid, dtype=np.float32)
    points = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1).reshape(-1, 3)
    child = JobContext(cancel=context.cancel) if context is not None else None
    return sample_lut(lut, points, context=child).reshape(grid, grid, grid, 3)


def png_lut_pixels(lut: Lut3D, format: str, *, context: JobContext | None = None) -> tuple[np.ndarray, dict]:
    """Quantized image (H, W, 3) for a PNG LUT format and its layout record."""
    _require_valid(lut)
    layout = png_layout(format, lut.values.shape[0])
    values = resample_lut(lut, layout['grid'], context=context)
    n = layout['grid']
    if layout['layout'] == 'hald':
        # index = r + n*g + n*n*b, written row-major into a width x width image
        image = values.transpose(2, 1, 0, 3).reshape(layout['height'], layout['width'], 3)
    else:
        # pixel (x = b*n + r, y = g)
        image = values.transpose(1, 2, 0, 3).reshape(n, n * n, 3)
        if layout['flipped']:
            image = image[::-1]
    scale = 65535 if layout['bit_depth'] == 16 else 255
    quantized = np.round(np.clip(image, 0, 1) * scale).astype(np.uint16 if scale == 65535 else np.uint8)
    layout['max_quantization_error'] = float(np.abs(quantized / scale - np.clip(image, 0, 1)).max())
    return np.ascontiguousarray(quantized), layout


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(image: np.ndarray, text: dict[str, str] | None = None) -> bytes:
    """Deterministic RGB PNG (8 or 16 bit, filter 0) with no colour-management
    chunks, so viewers and engines do not alter the LUT values."""
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype not in (np.uint8, np.uint16):
        raise ExportError('PNG encoder expects (H, W, 3) uint8/uint16', code='invalid_png_data')
    height, width, _ = image.shape
    bits = 16 if image.dtype == np.uint16 else 8
    data = image.astype('>u2') if bits == 16 else image
    rows = data.reshape(height, -1).view(np.uint8).reshape(height, -1)
    raw = np.concatenate([np.zeros((height, 1), np.uint8), rows], axis=1).tobytes()
    out = [b'\x89PNG\r\n\x1a\n', _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, bits, 2, 0, 0, 0))]
    for key, value in (text or {}).items():
        out.append(_png_chunk(b'tEXt', key.encode('latin-1') + b'\x00' + value.encode('latin-1', 'replace')))
    out.append(_png_chunk(b'IDAT', zlib.compress(raw, 9)))
    out.append(_png_chunk(b'IEND', b''))
    return b''.join(out)


def decode_png(blob: bytes) -> np.ndarray:
    """Decoder for the PNGs written by :func:`encode_png` (RGB, 8/16 bit,
    filter 0, no interlace); anything else is rejected."""
    if blob[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValidationError('Not a PNG file', code='invalid_png')
    pos, header, idat = 8, None, []
    while pos < len(blob):
        if len(blob) < pos + 12:
            raise ValidationError('Truncated PNG chunk', code='invalid_png')
        length, kind = struct.unpack('>I4s', blob[pos:pos + 8])
        if len(blob) < pos + 12 + length:
            raise ValidationError('Truncated PNG chunk', code='invalid_png')
        data = blob[pos + 8:pos + 8 + length]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != struct.unpack('>I', blob[pos + 8 + length:pos + 12 + length])[0]:
            raise ValidationError('PNG CRC mismatch', code='invalid_png')
        if kind == b'IHDR':
            header = struct.unpack('>IIBBBBB', data)
        elif kind == b'IDAT':
            idat.append(data)
        elif kind == b'IEND':
            break
        pos += 12 + length
    if header is None:
        raise ValidationError('PNG header missing', code='invalid_png')
    width, height, bits, colour, _comp, _filter, interlace = header
    if colour != 2 or bits not in (8, 16) or interlace:
        raise ValidationError('Unsupported PNG layout', code='invalid_png')
    channel_bytes = bits // 8
    stride = width * 3 * channel_bytes
    try:
        decoded = zlib.decompress(b''.join(idat))
    except zlib.error as exc:
        raise ValidationError(f'PNG image data is corrupt: {exc}', code='invalid_png') from exc
    if len(decoded) != height * (stride + 1):
        raise ValidationError('PNG image data has the wrong length', code='invalid_png')
    raw = np.frombuffer(decoded, np.uint8).reshape(height, stride + 1)
    if np.any(raw[:, 0] != 0):
        raise ValidationError('Unsupported PNG row filter', code='invalid_png')
    body = raw[:, 1:].copy()
    if bits == 16:
        return body.view('>u2').reshape(height, width, 3).astype(np.uint16)
    return body.reshape(height, width, 3)


def export_lut(lut: Lut3D, path: Path, *, format: str = 'cube', overwrite: bool = False,
               context: JobContext | None = None) -> ExportResult:
    """Export ``lut`` as a .cube or as a PNG LUT image plus JSON sidecar."""
    if format == 'cube':
        return export_cube(lut, path, overwrite=overwrite, context=context)
    checkpoint(context, stage='export_png', fraction=0)
    if format not in EXPORT_FORMATS:
        raise ExportError(f'Unknown export format: {format}', code='invalid_export_format')
    report = _require_valid(lut)
    path = _output_path(path)
    if path.suffix.lower() != '.png':
        raise ExportError('Output filename must end in .png', code='invalid_export_path')
    image, layout = png_lut_pixels(lut, format, context=context)
    checkpoint(context, stage='export_png', fraction=.6)
    description = (f"Camera Profile Studio {layout['layout']} LUT {layout['grid']}^3"
                   + (f" (Hald level {layout['level']})" if layout['level'] else '')
                   + (', green increases upwards' if layout['flipped'] else ''))
    blob = encode_png(image, {'Software': 'Camera Profile Studio ' + _software().get('camera-profile-studio', ''),
                              'Description': description, 'Title': lut.title})
    # Read-back verification: the published bytes decode to exactly these values.
    if not np.array_equal(decode_png(blob), image):
        raise ExportError('PNG read-back verification failed', code='export_verification_failed')

    def write(temporary: Path) -> None:
        with temporary.open('wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        checkpoint(context, stage='export_png', fraction=.8)

    resampled = layout['grid'] != layout['source_size']

    def metadata(digest: str) -> dict:
        png = dict(layout)
        png.update(channel_order='RGB', colour_chunks='none (values are raw LUT data)',
                   axis_order=('index = r + N*g + N*N*b, row-major (ImageMagick hald order)' if layout['layout'] == 'hald'
                               else 'x = b*N + r, y = ' + ('N-1-g (green upwards)' if layout['flipped'] else 'g (green downwards)')),
                   resampling=(f"tetrahedral from {layout['source_size']}^3" if resampled else 'none'),
                   read_back='decoded PNG equals the quantized values')
        return dict(schema_version=1, format=format, png_sha256=digest, title=lut.title, size=layout['source_size'],
                    input_space=asdict(lut.input_space), output_space=asdict(lut.output_space),
                    strength=lut.strength, domain_min=[0, 0, 0], domain_max=[1, 1, 1], png=png,
                    provenance=dict(lut.provenance), software=_software(), validation=asdict(report))

    return _publish(path, '.png', 'export_png', write, metadata, report, overwrite, context)
