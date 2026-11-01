import os
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image, ImageCms
import pytest
import tifffile

from camera_profile_studio.errors import ImageError
from camera_profile_studio.images import load_image
from camera_profile_studio.types import RawRecipe


def test_tiff_uint16_alpha_orientation_and_precision(tmp_path):
    pixels = np.array([[[12345, 23456, 34567, 32768], [111, 222, 333, 65535]],
                       [[22222, 33333, 44444, 0], [55555, 44444, 33333, 60000]]], np.uint16)
    path = tmp_path/'rgba16.tif'
    tifffile.imwrite(path, pixels, photometric='rgb', extrasamples='unassalpha',
                     extratags=[(274, 'H', 1, 6, False)])
    image = load_image(path)
    full = pixels.astype(np.float32)/65535
    expected = full[..., :3] * full[..., 3:] + 1-full[..., 3:]
    np.testing.assert_allclose(image.rgb, np.rot90(expected, 3), atol=1e-7)
    assert dict(image.metadata)['source_bit_depth'] == '16'
    assert 'alpha' in dict(image.metadata)
    assert image.warnings


def test_png_uint16_not_silently_truncated(tmp_path):
    rgb = np.array([[[12345, 23456, 34567], [12346, 23457, 34568]]], np.uint16)
    path = tmp_path/'rgb16.png'
    assert cv2.imwrite(str(path), rgb[..., ::-1])
    image = load_image(path)
    np.testing.assert_allclose(image.rgb, rgb.astype(np.float32)/65535, atol=1e-8)
    assert np.all(image.rgb[0, 1] > image.rgb[0, 0])


def test_embedded_srgb_icc_float_normalization(tmp_path):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    pixels = np.array([[[12345, 23456, 34567], [12346, 23457, 34568]]], np.uint16)
    path = tmp_path/'icc16.tif'
    tifffile.imwrite(path, pixels, photometric='rgb', extratags=[(34675, 'B', len(profile), profile, False)])
    image = load_image(path)
    np.testing.assert_allclose(image.rgb, pixels.astype(np.float32)/65535, atol=3e-5)
    assert 'embedded_icc_sha256' in dict(image.metadata)
    assert np.all(image.rgb[0, 1] > image.rgb[0, 0])
    assert not image.warnings


def test_malformed_icc_is_never_ignored(tmp_path):
    path = tmp_path/'bad.png'
    Image.new('RGB', (3, 2), (12, 34, 56)).save(path, icc_profile=b'not an ICC')
    with pytest.raises(ImageError) as exc: load_image(path)
    assert exc.value.code == 'invalid_embedded_icc'


def test_cmyk_and_float_tiff_rejected(tmp_path):
    path = tmp_path/'cmyk.jpg'
    Image.new('CMYK', (3, 2)).save(path)
    with pytest.raises(ImageError): load_image(path)
    path = tmp_path/'float.tif'
    tifffile.imwrite(path, np.ones((2, 3, 3), np.float32), photometric='rgb')
    with pytest.raises(ImageError): load_image(path)


def test_jpeg_orientation_resize(tmp_path):
    path = tmp_path/'oriented.jpg'
    image = Image.new('RGB', (20, 10), (30, 80, 120))
    exif = Image.Exif()
    exif[274] = 6
    image.save(path, exif=exif)
    result = load_image(path, max_dimension=8)
    assert result.rgb.shape == (8, 4, 3)
    assert dict(result.metadata)['orientation_applied'] == '6'


def _raw_mock(monkeypatch, wb=(2, 1, 1.5, 0)):
    class Raw:
        camera_whitebalance = wb
        color_desc = b'RGBG'
        num_colors = 3
        raw_pattern = np.array([[0, 1], [3, 2]], np.uint8)
        sizes = (100, 200)
        options = None
        closed = False
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def postprocess(self, **kwargs):
            self.options = kwargs
            return np.array([[[12345, 23456, 34567]]], np.uint16)
    raw = Raw()
    mock = SimpleNamespace(__version__='mock-0.25.1', libraw_version=(0, 21, 4),
                           DemosaicAlgorithm=SimpleNamespace(AHD=3), ColorSpace=SimpleNamespace(sRGB=1),
                           HighlightMode=SimpleNamespace(Clip=0), FBDDNoiseReductionMode=SimpleNamespace(Off=0),
                           imread=lambda _: raw)
    monkeypatch.setitem(sys.modules, 'rawpy', mock)
    return raw


def test_raw_explicit_recipe_and_camera_wb_metadata(tmp_path, monkeypatch):
    raw = _raw_mock(monkeypatch)
    image = load_image(tmp_path/'test.nef')
    options = raw.options
    assert options['output_bps'] == 16
    assert options['gamma'] == (2.4, 12.92)
    assert options['no_auto_bright'] and not options['no_auto_scale']
    assert options['use_camera_wb'] and not options['use_auto_wb']
    assert options['exp_shift'] == 1
    assert options['demosaic_algorithm'] == 3
    assert raw.closed
    assert 'camera_whitebalance' in dict(image.metadata)
    assert 'raw_effective_options' in dict(image.metadata)
    assert 'libraw_version' in dict(image.metadata)
    np.testing.assert_allclose(image.rgb, np.array([[[12345,23456,34567]]])/65535, atol=1e-8)


def test_raw_missing_camera_wb_requires_explicit_user_wb(tmp_path, monkeypatch):
    raw = _raw_mock(monkeypatch, wb=(0, 0, 0, 0))
    with pytest.raises(ImageError) as exc: load_image(tmp_path/'test.nef')
    assert exc.value.code == 'missing_camera_wb'
    assert raw.closed
    image = load_image(tmp_path/'test.nef', raw_recipe=RawRecipe(False, (2, 1, 1.5, 1), 1.5))
    assert raw.options['user_wb'] == (2, 1, 1.5, 1)
    assert not raw.options['use_camera_wb']
    assert raw.options['exp_shift'] == 1.5
    assert image.rgb.dtype == np.float32


@pytest.mark.reference
def test_optional_real_raw():
    path = os.environ.get('CPS_TEST_RAW')
    if not path:
        pytest.skip('Set CPS_TEST_RAW to an independently supplied real RAW fixture')
    image = load_image(Path(path), max_dimension=512)
    assert image.rgb.dtype == np.float32 and image.rgb.shape[-1] == 3
    assert dict(image.metadata)['source_bit_depth'] == '16'


def test_area_resize_roundoff_is_bounded(tmp_path):
    path = tmp_path/'white.tif'
    tifffile.imwrite(path, np.full((997, 1499, 3), 65535, np.uint16), photometric='rgb')
    image = load_image(path, max_dimension=333)
    assert image.rgb.max() <= 1
    np.testing.assert_allclose(image.rgb, 1, atol=3e-7)


def test_resize_does_not_mask_large_invalid_outputs(tmp_path, monkeypatch):
    import camera_profile_studio.images as module
    path = tmp_path/'white.png'
    Image.new('RGB', (20, 10), 'white').save(path)
    monkeypatch.setattr(module.cv2, 'resize', lambda *a, **k: np.full((5, 10, 3), 1.1, np.float32))
    with pytest.raises(ImageError, match='Resize'):
        load_image(path, max_dimension=10)


def test_nontrivial_embedded_icc_then_white_alpha(tmp_path):
    from scripts.validate_lut_quality import create_gamma_fixture
    from camera_profile_studio.color import srgb_encode
    profile = tmp_path/'gamma18.icc'
    create_gamma_fixture(profile)
    icc = profile.read_bytes()
    pixels = np.array([[[12345, 23456, 34567, 32768], [6000, 22222, 50000, 65535]]], np.uint16)
    path = tmp_path/'icc-alpha.tif'
    tifffile.imwrite(path, pixels, photometric='rgb', extrasamples='unassalpha',
                     extratags=[(34675, 'B', len(icc), icc, False)])
    image = load_image(path)
    normalized = pixels.astype(np.float32)/65535
    expected = srgb_encode(normalized[..., :3]**1.8)*normalized[..., 3:]+1-normalized[..., 3:]
    # ICC matrix/white-point tags are fixed-point; the analytic primaries are only approximate.
    np.testing.assert_allclose(image.rgb, expected, atol=1e-4)
    from camera_profile_studio.lcms import create_icc_transform
    from camera_profile_studio.types import SpaceSpec
    transform = create_icc_transform(SpaceSpec('icc_rgb', profile), SpaceSpec('srgb'))
    try:
        direct = transform.apply(normalized[..., :3])*normalized[..., 3:]+1-normalized[..., 3:]
        np.testing.assert_allclose(image.rgb, direct, atol=1e-7)
    finally:
        transform.close()


def test_associated_tiff_alpha_is_unpremultiplied(tmp_path):
    pixels = np.array([[[16384, 8192, 0, 32768], [0, 0, 0, 0]]], np.uint16)
    path = tmp_path/'premult.tif'
    tifffile.imwrite(path, pixels, photometric='rgb', extrasamples='assocalpha')
    image = load_image(path)
    expected = pixels[..., :3].astype(np.float32)/65535 + 1-pixels[..., 3:].astype(np.float32)/65535
    np.testing.assert_allclose(image.rgb, expected, atol=1e-7)
    assert 'associated' in dict(image.metadata)['alpha']


def test_lzw_tiff_fallback_retains_uint16(tmp_path):
    pixels = np.random.default_rng(8).integers(0, 65536, (11, 9, 3), dtype=np.uint16)
    path = tmp_path/'lzw.tif'
    assert cv2.imwrite(str(path), pixels[..., ::-1], [cv2.IMWRITE_TIFF_COMPRESSION, 5])
    np.testing.assert_allclose(load_image(path).rgb, pixels.astype(np.float32)/65535, atol=1e-8)


def test_broken_png_iccp_is_not_treated_as_untagged(tmp_path):
    import struct
    import zlib
    path = tmp_path/'broken-profile.png'
    Image.new('RGB', (2, 2)).save(path)
    data = path.read_bytes()
    payload = b'profile\0\0broken-zlib-stream'
    chunk = struct.pack('>I', len(payload))+b'iCCP'+payload+struct.pack('>I', zlib.crc32(b'iCCP'+payload))
    path.write_bytes(data[:33]+chunk+data[33:])
    with pytest.raises(ImageError): load_image(path)


def test_large_image_normalization_cancellation(tmp_path):
    from camera_profile_studio.errors import CancelledError
    from camera_profile_studio.types import JobContext
    path = tmp_path/'large.tif'
    tifffile.imwrite(path, np.zeros((500, 500, 3), np.uint16), photometric='rgb')
    class Token:
        cancelled = False
        def is_cancelled(self): return self.cancelled
    token = Token()
    def progress(p):
        if p.stage == 'image_normalize': token.cancelled = True
    with pytest.raises(CancelledError): load_image(path, context=JobContext(progress, token))


def test_raw_non_bayer_sensor_does_not_silently_change_demosaic(tmp_path, monkeypatch):
    from camera_profile_studio.errors import UnsupportedError
    raw = _raw_mock(monkeypatch)
    raw.raw_pattern = np.zeros((6, 6), np.uint8)
    with pytest.raises(UnsupportedError) as exc:
        load_image(tmp_path/'xtrans.raf')
    assert exc.value.code == 'unsupported_raw_sensor'
    assert raw.options is None and raw.closed


def test_camera_jpeg_with_mpf_preview_loads_primary_image(tmp_path):
    """0.2.3: camera JPEGs with an MPF preview (Pillow format 'MPO', e.g. Sony and
    Nikon bodies) were rejected as 'Only JPEG/PNG/TIFF ...'."""
    rng = np.random.default_rng(3)
    primary = Image.fromarray((rng.random((60, 90, 3)) * 255).astype(np.uint8))
    preview = Image.new('RGB', (30, 20), (255, 0, 0))
    path = tmp_path / 'DSC00001.JPG'
    exif = Image.Exif(); exif[274] = 6
    primary.save(path, format='MPO', save_all=True, append_images=[preview], quality=95, exif=exif)
    with Image.open(path) as probe:
        assert probe.format == 'MPO' and probe.n_frames == 2
        probe.seek(0)
        expected = np.asarray(probe.convert('RGB').transpose(Image.Transpose.ROTATE_270), np.float32) / 255
    image = load_image(path)
    meta = dict(image.metadata)
    assert meta['source_format'] == 'JPEG' and meta['jpeg_mpf_images'] == '2' and meta['orientation_applied'] == '6'
    assert image.rgb.shape == (90, 60, 3)                     # primary image, EXIF orientation applied
    assert np.abs(image.rgb - expected).max() < 2 / 255       # not the red preview


def test_unsupported_raster_names_the_detected_format(tmp_path):
    path = tmp_path / 'x.gif'
    Image.new('RGB', (4, 4)).save(path, format='GIF')
    with pytest.raises(ImageError) as err:
        load_image(path)
    assert err.value.code == 'unsupported_image' and 'detected: GIF' in str(err.value)
