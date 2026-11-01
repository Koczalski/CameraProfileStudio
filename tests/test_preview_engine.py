import numpy as np
import pytest

from camera_profile_studio.errors import UnsupportedError
from camera_profile_studio.images import render_preview
from camera_profile_studio.lut import export_cube, generate_lut, read_cube
from camera_profile_studio.types import ImageData, Lut3D, SpaceSpec


class Look:
    input_space = output_space = SpaceSpec('srgb')
    def apply(self, rgb, *, context=None):
        return (rgb * [.5, .7, .9] + [.1, .15, .05]).astype(np.float32)


def test_preview_matches_export_without_double_strength(tmp_path):
    lut = generate_lut(Look(), size=17, strength=.4)
    path = export_cube(lut, tmp_path / 'look.cube').cube_path
    restored = read_cube(path, input_space=lut.input_space, output_space=lut.output_space)
    pixels = np.random.default_rng(22).random((21, 13, 3)).astype(np.float32)
    image = ImageData(pixels, None)
    preview = render_preview(image, lut)
    np.testing.assert_array_equal(preview.rgb, render_preview(image, restored).rgb)
    np.testing.assert_allclose(preview.rgb, .6*pixels + .4*Look().apply(pixels), atol=1.5e-7)


def test_camera_rgb_preview_is_rejected(tmp_path):
    lut = generate_lut(Look(), size=17)
    camera_lut = Lut3D(lut.values, SpaceSpec('camera_linear', tmp_path/'camera.dcp'), SpaceSpec('srgb'))
    with pytest.raises(UnsupportedError, match='sRGB-input'):
        render_preview(ImageData(np.zeros((1, 1, 3), np.float32), None), camera_lut)


def test_linear_output_converts_to_display_srgb():
    from camera_profile_studio.lcms import create_icc_transform
    from camera_profile_studio.color import srgb_encode
    class Linear(Look):
        output_space = SpaceSpec('linear_srgb')
        def apply(self, rgb, *, context=None): return rgb
    lut = generate_lut(Linear(), size=17)
    image = ImageData(np.array([[[.1, .3, .6]]], np.float32), None)
    np.testing.assert_allclose(render_preview(image, lut).rgb, srgb_encode(image.rgb), atol=3e-5)
