import ctypes as C
from pathlib import Path
import numpy as np
import pytest
from camera_profile_studio.lcms import _load_library, create_icc_transform, probe_lcms, TYPE_RGB_FLT
from camera_profile_studio.types import SpaceSpec, JobContext
from camera_profile_studio.errors import CancelledError, DependencyError, ValidationError
from camera_profile_studio.color import srgb_decode


def srgb_profile_bytes():
    lib = _load_library()
    profile = lib.cmsCreate_sRGBProfileTHR(None)
    assert profile
    try:
        size = C.c_uint32()
        assert lib.cmsSaveProfileToMem(profile, None, C.byref(size))
        buffer = C.create_string_buffer(size.value)
        assert lib.cmsSaveProfileToMem(profile, buffer, C.byref(size))
        return buffer.raw[:size.value]
    finally:
        lib.cmsCloseProfile(profile)


@pytest.mark.lcms
def test_native_version_and_float_identity():
    assert probe_lcms().startswith('2.')
    assert TYPE_RGB_FLT == 0x0044001C
    pixels = np.random.default_rng(4).uniform(0, 1, (4096, 3)).astype(np.float32)
    with create_icc_transform(SpaceSpec('srgb'), SpaceSpec('srgb')) as transform:
        output = transform.apply(pixels)
    np.testing.assert_allclose(output, pixels, atol=2e-7, rtol=0)
    assert np.max(np.abs(output * 255 - np.round(output * 255))) > 0.49


@pytest.mark.lcms
def test_nonidentity_analytic_linear_srgb():
    pixels = np.array([[0, 0, 0], [1, 1, 1], [0.5, 0.2, 0.8], [0.5, 0.50001, 0.50002]], dtype=np.float32)
    with create_icc_transform(SpaceSpec('srgb'), SpaceSpec('linear_srgb')) as transform:
        output = transform.apply(pixels)
    np.testing.assert_allclose(output, srgb_decode(pixels), atol=2e-6, rtol=0)
    assert output[3, 0] < output[3, 1] < output[3, 2]
    with create_icc_transform(SpaceSpec('linear_srgb'), SpaceSpec('srgb')) as transform:
        recovered = transform.apply(output)
    np.testing.assert_allclose(recovered, pixels, atol=3e-6, rtol=0)


@pytest.mark.lcms
def test_unicode_profile_memory_open(tmp_path):
    path = tmp_path / '\u30ab\u30e1\u30e9-profile.icc'
    path.write_bytes(srgb_profile_bytes())
    source = np.array([[0.12, 0.34, 0.56]], dtype=np.float32)
    with create_icc_transform(SpaceSpec('icc_rgb', path), SpaceSpec('srgb')) as transform:
        # Serialized ICC colorants use s15Fixed16, unlike the in-memory built-in profile.
        np.testing.assert_allclose(transform.apply(source), source, atol=1e-4)
        assert len(transform.profile_hashes['input']) == 64


@pytest.mark.lcms
def test_cancellation_and_lifecycle():
    class Cancel:
        value = False
        def is_cancelled(self):
            return self.value
    cancel = Cancel()
    def update(progress):
        if progress.stage == 'lcms.transform' and progress.fraction > 0:
            cancel.value = True
    transform = create_icc_transform(SpaceSpec('srgb'), SpaceSpec('srgb'))
    with pytest.raises(CancelledError):
        transform.apply(np.full((150000, 3), 0.5, dtype=np.float32), context=JobContext(update, cancel))
    transform.close()
    transform.close()
    with pytest.raises(ValidationError, match='closed'):
        transform.apply(np.zeros((1, 3), dtype=np.float32))


@pytest.mark.lcms
@pytest.mark.parametrize('data', [np.array([1, 2]), np.array([0, float('inf'), 0]), np.array([-0.1, 0, 0])])
def test_native_input_validation(data):
    with create_icc_transform(SpaceSpec('srgb'), SpaceSpec('srgb')) as transform:
        with pytest.raises(ValidationError):
            transform.apply(data)


def test_missing_explicit_native_library(tmp_path, monkeypatch):
    missing = tmp_path / 'not-a-library.dll'
    with pytest.raises(DependencyError) as error:
        probe_lcms(library_path=missing)
    assert error.value.code == 'missing_lcms'
    monkeypatch.setenv('CPS_LCMS2_DLL', str(missing))
    with pytest.raises(DependencyError):
        create_icc_transform(SpaceSpec('srgb'), SpaceSpec('srgb'))
