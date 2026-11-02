"""Precision-preserving sRGB image ingress and baked-LUT previews (no Qt)."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile

import cv2
import numpy as np
from PIL import Image
import tifffile

from .errors import DependencyError, ImageError, StudioError, UnsupportedError, checkpoint
from .types import ImageData, JobContext, Lut3D, RawRecipe, SpaceSpec
from .lut import sample_lut

_RAW_EXTENSIONS = {'.3fr', '.arw', '.cr2', '.cr3', '.crw', '.dcr', '.dng', '.erf', '.fff',
                   '.iiq', '.kdc', '.mef', '.mos', '.mrw', '.nef', '.nrw', '.orf', '.pef',
                   '.raf', '.raw', '.rw2', '.rwl', '.sr2', '.srf', '.srw', '.x3f'}
_CHUNK = 65536


def _orientation(array: np.ndarray, orientation: int) -> np.ndarray:
    if orientation == 1:
        return array
    if orientation == 2:
        return array[:, ::-1]
    if orientation == 3:
        return array[::-1, ::-1]
    if orientation == 4:
        return array[::-1]
    if orientation == 5:
        return array.transpose(1, 0, 2)
    if orientation == 6:
        return np.rot90(array, 3)
    if orientation == 7:
        return array.transpose(1, 0, 2)[::-1, ::-1]
    if orientation == 8:
        return np.rot90(array, 1)
    raise ImageError('Invalid EXIF orientation', code='invalid_orientation')


def _convert(rgb: np.ndarray, source: SpaceSpec, *, context: JobContext | None,
             stage: str) -> np.ndarray:
    from .lcms import create_icc_transform
    transform = create_icc_transform(source, SpaceSpec('srgb'), context=context)
    try:
        flat = rgb.reshape(-1, 3)
        result = np.empty_like(flat)
        for start in range(0, len(flat), _CHUNK):
            end = min(start + _CHUNK, len(flat))
            checkpoint(context, stage=stage, fraction=start / len(flat))
            child_context = JobContext(cancel=context.cancel) if context is not None else None
            result[start:end] = transform.apply(flat[start:end], context=child_context)
        checkpoint(context, stage=stage, fraction=1)
        return result.reshape(rgb.shape)
    finally:
        transform.close()


def _load_raw(path: Path, recipe: RawRecipe, context: JobContext | None) -> ImageData:
    if recipe.demosaic != 'AHD' or not np.isfinite(recipe.exposure_shift) or recipe.exposure_shift <= 0:
        raise ImageError('Invalid RAW development recipe', code='invalid_raw_recipe')
    if recipe.use_camera_wb == (recipe.user_wb is not None):
        raise ImageError('Choose exactly camera WB or explicit user WB', code='invalid_raw_wb')
    if recipe.user_wb is not None and (len(recipe.user_wb) != 4 or not np.isfinite(recipe.user_wb).all() or np.any(np.asarray(recipe.user_wb) <= 0)):
        raise ImageError('User WB must contain four finite positive multipliers', code='invalid_raw_wb')
    try:
        import rawpy
    except ImportError as exc:
        raise DependencyError('Install the raw extra to decode RAW files', code='raw_decoder_missing') from exc
    metadata = {'raw_recipe': json.dumps(asdict(recipe), sort_keys=True), 'rawpy_version': rawpy.__version__,
                'libraw_version': str(rawpy.libraw_version), 'color_space': 'srgb',
                'raw_purpose': 'RAW developed to sRGB; not camera-linear DCP input or OEM JPEG emulation'}
    options = dict(demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                   output_color=rawpy.ColorSpace.sRGB, output_bps=16, gamma=(2.4, 12.92),
                   no_auto_bright=True, no_auto_scale=False, use_auto_wb=False,
                   use_camera_wb=recipe.use_camera_wb, user_wb=recipe.user_wb,
                   exp_shift=recipe.exposure_shift, exp_preserve_highlights=0.0,
                   bright=1.0, half_size=False, four_color_rgb=False,
                   highlight_mode=rawpy.HighlightMode.Clip, median_filter_passes=0,
                   fbdd_noise_reduction=rawpy.FBDDNoiseReductionMode.Off)
    metadata['raw_effective_options'] = json.dumps(options, sort_keys=True, default=str)
    metadata['raw_remaining_options'] = 'rawpy Params defaults for recorded version; no automatic exposure/WB requested'
    try:
        with rawpy.imread(str(path)) as raw:
            pattern = getattr(raw, 'raw_pattern', None)
            description = getattr(raw, 'color_desc', b'')
            pattern = np.asarray(pattern) if pattern is not None else None
            if (pattern is None or pattern.shape != (2, 2) or getattr(raw, 'num_colors', None) != 3
                    or pattern.dtype.kind not in 'iu' or np.any(pattern < 0) or np.any(pattern >= len(description))
                    or sorted(description[int(index)] for index in pattern.flat) != sorted(b'RGBG')):
                raise UnsupportedError('The explicit AHD recipe requires a three-color Bayer RAW; X-Trans, monochrome and stacked sensors are unsupported',
                                       code='unsupported_raw_sensor')
            if recipe.use_camera_wb:
                wb = np.asarray(raw.camera_whitebalance, dtype=float)
                # LibRaw uses a zero fourth entry for three-colour cameras; RGB must be real, positive WB.
                if wb.shape != (4,) or not np.isfinite(wb).all() or np.any(wb[:3] <= 0) or wb[3] < 0:
                    raise ImageError('RAW has no valid camera white balance; specify user WB', code='missing_camera_wb')
                metadata['camera_whitebalance'] = json.dumps(wb.tolist())
            for attr in ('color_desc', 'num_colors', 'raw_pattern', 'black_level_per_channel',
                         'white_level', 'camera_white_level_per_channel', 'daylight_whitebalance', 'sizes'):
                value = getattr(raw, attr, None)
                if value is not None:
                    metadata['raw_' + attr] = json.dumps(value.tolist() if isinstance(value, np.ndarray) else value, default=str)
            checkpoint(context, stage='raw_decode', fraction=0)
            array = raw.postprocess(**options)
            checkpoint(context, stage='raw_decode', fraction=1)
        if array.dtype != np.uint16 or array.ndim != 3 or array.shape[2] != 3:
            raise ImageError('RAW decoder did not return uint16 RGB', code='invalid_raw_output')
        rgb = np.empty(array.shape, dtype=np.float32)
        row_chunk = max(1, _CHUNK // array.shape[1])
        for start in range(0, len(array), row_chunk):
            end = min(start + row_chunk, len(array))
            checkpoint(context, stage='raw_normalize', fraction=start / len(array))
            rgb[start:end] = array[start:end].astype(np.float32) / np.float32(65535)
        checkpoint(context, stage='raw_normalize', fraction=1)
        metadata['source_bit_depth'] = '16'
        metadata['orientation'] = 'LibRaw automatic camera orientation'
        return ImageData(rgb, path, tuple(metadata.items()))
    except StudioError:
        raise
    except MemoryError as exc:
        raise ImageError(f'RAW file is too large to decode: {path}', code='image_too_large', details=(('path', str(path)),)) from exc
    except Exception as exc:
        if not path.exists():
            raise ImageError(f'RAW file not found: {path}', code='image_not_found', details=(('path', str(path)),)) from exc
        raise ImageError(f'RAW decode failed: {exc}', code='raw_decode_error', details=(('path', str(path)),)) from exc


def load_image(path: Path, *, max_dimension: int | None = None,
               raw_recipe: RawRecipe | None = None, context: JobContext | None = None) -> ImageData:
    checkpoint(context, stage='load_image', fraction=0)
    path = Path(path)
    if max_dimension is not None and (type(max_dimension) is not int or max_dimension <= 0):
        raise ImageError('max_dimension must be a positive integer', code='invalid_image_size')
    if path.suffix.lower() in _RAW_EXTENSIONS:
        image = _load_raw(path, raw_recipe or RawRecipe(), context)
        rgb, metadata, warnings = image.rgb, dict(image.metadata), list(image.warnings)
    else:
        if raw_recipe is not None:
            raise ImageError('RAW recipe supplied for a non-RAW image', code='unexpected_raw_recipe')
        if not path.exists():
            raise ImageError(f'Image file not found: {path}', code='image_not_found', details=(('path', str(path)),))
        if path.is_dir():
            raise ImageError(f'Not an image file (a folder): {path}', code='image_not_found', details=(('path', str(path)),))
        try:
            rgb, metadata, warnings = _load_raster(path, context)
        except PermissionError as exc:
            raise ImageError(f'Image file cannot be read (permission): {path}', code='image_io', details=(('path', str(path)),)) from exc
        except (OSError, ValueError, SyntaxError, cv2.error, tifffile.TiffFileError) as exc:
            if isinstance(exc, OSError) and exc.errno is not None and not isinstance(exc, (FileNotFoundError,)) and 'cannot identify' not in str(exc).lower():
                raise ImageError(f'Image file cannot be read: {exc}', code='image_io', details=(('path', str(path)),)) from exc
            raise ImageError(f'Image decode failed: {exc}', code='image_decode_error', details=(('path', str(path)),)) from exc
        except MemoryError as exc:
            raise ImageError(f'Image is too large to decode: {path}', code='image_too_large', details=(('path', str(path)),)) from exc
    if not np.isfinite(rgb).all() or np.any(rgb < -1e-6) or np.any(rgb > 1 + 1e-6):
        raise ImageError('Image normalization produced non-unit RGB', code='invalid_image_output')
    rgb = np.clip(rgb, 0, 1)
    checkpoint(context, stage='load_image', fraction=.9)
    if max_dimension is not None and max(rgb.shape[:2]) > max_dimension:
        scale = max_dimension / max(rgb.shape[:2])
        target = (max(1, round(rgb.shape[1] * scale)), max(1, round(rgb.shape[0] * scale)))
        rgb = cv2.resize(rgb, target, interpolation=cv2.INTER_AREA)
        # AREA is a convex average; its float32 accumulator can overshoot white by a few ULPs.
        if not np.isfinite(rgb).all() or np.any(rgb < -1e-6) or np.any(rgb > 1 + 1e-6):
            raise ImageError('Resize produced invalid RGB', code='invalid_image_output')
        rgb = np.clip(rgb, 0, 1)
        metadata['resize'] = json.dumps({'max_dimension': max_dimension, 'width': target[0], 'height': target[1], 'filter': 'area'})
    checkpoint(context, stage='load_image', fraction=1)
    return ImageData(rgb, path, tuple(metadata.items()), tuple(warnings))


def _load_raster(path: Path, context: JobContext | None) -> tuple[np.ndarray, dict[str, str], list[str]]:
    metadata: dict[str, str] = {'color_space': 'srgb'}
    warnings: list[str] = []
    associated = False
    if path.suffix.lower() in ('.tif', '.tiff'):
        with tifffile.TiffFile(path) as file:
            if len(file.pages) != 1:
                raise ImageError('Multipage TIFF is ambiguous', code='unsupported_image')
            page = file.pages[0]
            if int(page.photometric) != 2 or page.samplesperpixel not in (3, 4):
                raise ImageError('Only RGB/RGBA TIFF is supported', code='unsupported_image')
            try:
                array = page.asarray()
            except ValueError as exc:
                if 'imagecodecs' not in str(exc) or page.samplesperpixel != 3 or int(page.planarconfig) != 1:
                    raise
                # OpenCV/libtiff supplies common lossless codecs without an extra Python dependency.
                array = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                if array is None or array.ndim != 3 or array.shape[2] != 3:
                    raise ImageError('Compressed TIFF decoder failed', code='image_decode_error') from exc
                array = array[..., ::-1]
                expected_bits = page.bitspersample
                if array.dtype.itemsize * 8 != expected_bits:
                    raise ImageError('TIFF fallback would truncate source precision', code='unsupported_image_dtype')
            if int(page.planarconfig) == 2:
                array = array.transpose(1, 2, 0)
            icc_tag = page.tags.get(34675)
            icc = bytes(icc_tag.value) if icc_tag is not None else None
            orientation_tag = page.tags.get(274)
            orientation = int(orientation_tag.value) if orientation_tag is not None else 1
            if page.samplesperpixel == 4:
                extras = tuple(int(v) for v in page.extrasamples)
                if extras not in ((1,), (2,)):
                    raise ImageError('TIFF fourth channel is not a declared alpha channel', code='ambiguous_alpha')
                associated = extras == (1,)
            metadata['source_format'] = 'TIFF'
    else:
        with Image.open(path) as image:
            # Camera JPEGs often carry an MPF (CIPA DC-007) preview after the primary
            # image; Pillow reports such files as 'MPO'. They are ordinary JPEGs: the
            # decoder below reads the primary image, which is the photograph itself.
            if image.format not in ('JPEG', 'MPO', 'PNG'):
                raise ImageError('Only JPEG/PNG/TIFF and supported RAW formats are accepted '
                                 f'(detected: {image.format or "unknown"})', code='unsupported_image')
            if image.format == 'MPO':
                metadata['jpeg_mpf_images'] = str(getattr(image, 'n_frames', 1))
            if image.mode in ('CMYK', 'LAB', 'F', 'I'):
                raise ImageError('CMYK/Lab/floating/HDR images are unsupported', code='unsupported_image')
            # Pillow can expose a broken iCCP chunk as a present-but-None value.
            icc = (image.info.get('icc_profile') or b'') if 'icc_profile' in image.info else None
            orientation = int(image.getexif().get(274, 1))
            metadata['source_format'] = 'JPEG' if image.format == 'MPO' else str(image.format)
            # Pillow exposes RGB16 PNG as RGB8: use unchanged OpenCV decoding for true precision.
            array = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if array is None:
                raise ImageError('Image decoder returned no pixels', code='image_decode_error')
            if array.ndim == 2:
                array = np.repeat(array[..., None], 3, axis=2)
            elif array.shape[2] == 3:
                array = array[..., ::-1]
            elif array.shape[2] == 4:
                array = array[..., [2, 1, 0, 3]]
            else:
                raise ImageError('Unsupported channel count', code='unsupported_image')
    checkpoint(context, stage='decode_image', fraction=1)
    if array.dtype.kind != 'u' or array.dtype.itemsize not in (1, 2) or array.ndim != 3 or array.shape[2] not in (3, 4):
        raise ImageError('Only unsigned 8/16-bit RGB images are supported; floating/HDR is ambiguous', code='unsupported_image_dtype')
    metadata['source_bit_depth'] = str(array.dtype.itemsize * 8)
    metadata['orientation_applied'] = str(orientation)
    array = _orientation(array, orientation)
    rgb = np.empty((*array.shape[:2], 3), dtype=np.float32)
    alpha = np.empty((*array.shape[:2], 1), dtype=np.float32) if array.shape[2] == 4 else None
    row_chunk = max(1, _CHUNK // array.shape[1])
    denominator = np.float32(np.iinfo(array.dtype).max)
    for start in range(0, len(array), row_chunk):
        end = min(start + row_chunk, len(array))
        checkpoint(context, stage='image_normalize', fraction=start / len(array))
        rgb[start:end] = array[start:end, :, :3].astype(np.float32) / denominator
        if alpha is not None:
            alpha[start:end] = array[start:end, :, 3:4].astype(np.float32) / denominator
            if associated:
                color, opacity = rgb[start:end], alpha[start:end]
                if np.any(color > opacity + 1 / denominator):
                    raise ImageError('Invalid associated alpha samples', code='invalid_alpha')
                rgb[start:end] = np.clip(np.divide(color, opacity, out=np.zeros_like(color), where=opacity > 0), 0, 1)
    checkpoint(context, stage='image_normalize', fraction=1)
    if icc is not None:
        if not isinstance(icc, bytes) or not icc:
            raise ImageError('Malformed embedded ICC', code='invalid_embedded_icc')
        metadata['embedded_icc_sha256'] = hashlib.sha256(icc).hexdigest()
        with tempfile.TemporaryDirectory(prefix='cps-icc-') as directory:
            profile = Path(directory) / 'embedded.icc'
            profile.write_bytes(icc)
            try:
                rgb = _convert(rgb, SpaceSpec('icc_rgb', profile), context=context, stage='image_icc')
            except DependencyError:
                raise
            except StudioError as exc:
                # Cancellation remains cancellation, not an image-format error.
                from .errors import CancelledError
                if isinstance(exc, CancelledError):
                    raise
                raise ImageError(f'Embedded ICC cannot be normalized: {exc}', code='invalid_embedded_icc') from exc
        metadata['icc_normalization'] = 'embedded RGB ICC to encoded sRGB; LCMS float32'
    else:
        warnings.append('Untagged RGB image: assuming encoded sRGB')
        metadata['icc_normalization'] = 'untagged; assumed encoded sRGB'
    if alpha is not None:
        for start in range(0, len(rgb), row_chunk):
            end = min(start + row_chunk, len(rgb))
            checkpoint(context, stage='image_alpha', fraction=start / len(rgb))
            rgb[start:end] = rgb[start:end] * alpha[start:end] + (1 - alpha[start:end])
        checkpoint(context, stage='image_alpha', fraction=1)
        metadata['alpha'] = 'composited against encoded sRGB white after ICC normalization; ' + ('associated' if associated else 'unassociated')
    return rgb, metadata, warnings


def shrink_image(image: ImageData, max_dimension: int) -> ImageData:
    """``image`` with its long side limited to ``max_dimension`` (never enlarged)."""
    rgb = image.rgb
    if max(rgb.shape[:2]) <= max_dimension:
        return image
    scale = max_dimension / max(rgb.shape[:2])
    target = (max(1, round(rgb.shape[1] * scale)), max(1, round(rgb.shape[0] * scale)))
    # AREA is a convex average; its float32 accumulator can overshoot white by a few ULPs.
    small = np.clip(cv2.resize(rgb, target, interpolation=cv2.INTER_AREA), 0, 1)
    return ImageData(small, image.source_path, image.metadata, image.warnings)


def render_preview(image: ImageData, lut: Lut3D, *, context: JobContext | None = None) -> ImageData:
    checkpoint(context, stage='render_preview', fraction=0)
    if lut.input_space != SpaceSpec('srgb'):
        raise UnsupportedError('Ordinary sRGB photos require an sRGB-input LUT, not camera RGB', code='preview_input_space')
    if lut.output_space.kind not in ('srgb', 'linear_srgb', 'icc_rgb'):
        raise UnsupportedError('LUT output cannot be displayed as sRGB', code='preview_output_space')
    rgb = sample_lut(lut, image.rgb, context=context)
    if lut.output_space != SpaceSpec('srgb'):
        rgb = _convert(rgb, lut.output_space, context=context, stage='preview_display')
    metadata = dict(image.metadata)
    metadata.update(color_space='srgb', preview_lut_title=lut.title,
                    preview_strength=str(lut.strength), preview_sampling='baked LUT tetrahedral; no second strength blend')
    checkpoint(context, stage='render_preview', fraction=1)
    return ImageData(rgb, image.source_path, tuple(metadata.items()), image.warnings)
