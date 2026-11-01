import json
from pathlib import Path
import numpy as np
import pytest
from camera_profile_studio.errors import DependencyError, UnsupportedError, ValidationError
from camera_profile_studio.service import create_transform, convert_profile, inspect_profile
from camera_profile_studio.types import DcpSettings, SpaceSpec, TransformRequest
from test_lcms import srgb_profile_bytes


def test_native_service_provenance():
    request = TransformRequest(SpaceSpec('srgb'), SpaceSpec('linear_srgb'))
    lut = convert_profile(request, size=17)
    assert lut.values.shape == (17, 17, 17, 3)
    provenance = dict(lut.provenance)
    assert provenance['lcms_version'].startswith('2.')
    assert provenance['jpeg_accuracy'] == 'unverified'
    assert json.loads(provenance['request'])['input_space']['kind'] == 'srgb'


def test_runtime_unavailable_downgrades_inspection(tmp_path, monkeypatch):
    path = tmp_path / 's.icc'
    path.write_bytes(srgb_profile_bytes())
    monkeypatch.setenv('CPS_LCMS2_DLL', str(tmp_path / 'missing.dll'))
    info = inspect_profile(path)
    assert info.format == 'icc'
    assert {c.feature: c.status for c in info.capabilities}['technical'] == 'unsupported'
    with pytest.raises(DependencyError):
        create_transform(TransformRequest(SpaceSpec('icc_rgb', path), SpaceSpec('srgb')))


def test_unused_and_unassigned_options_fail(tmp_path):
    path = tmp_path / 's.icc'
    path.write_bytes(srgb_profile_bytes())
    with pytest.raises(ValidationError, match='assigned'):
        create_transform(TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb'), profile_path=path))
    with pytest.raises(ValidationError, match='not applicable'):
        create_transform(TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb'), dcp=DcpSettings()))
    with pytest.raises(UnsupportedError):
        create_transform(TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb'), mode='look', profile_path=path))


def test_dcp_dispatch_and_domain_checks(tmp_path):
    from test_dcp import write_fixture
    path = write_fixture(tmp_path / 'matrix.dcp')
    technical = TransformRequest(SpaceSpec('camera_linear', path), SpaceSpec('srgb'),
                                 dcp=DcpSettings(camera_neutral=(1, 1, 1)))
    transform = create_transform(technical)
    try:
        output = transform.apply(np.array([[0.1, 0.2, 0.1]], np.float32))
        assert np.all(np.isfinite(output))
        assert len(dict(transform.provenance)['source_profile_sha256']) == 64
    finally:
        transform.close()
    with pytest.raises(UnsupportedError, match='camera-linear'):
        create_transform(TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb'), profile_path=path))
    with pytest.raises(UnsupportedError, match='intent/BPC'):
        create_transform(TransformRequest(SpaceSpec('camera_linear', path), SpaceSpec('srgb'),
            intent='perceptual', dcp=DcpSettings(camera_neutral=(1, 1, 1))))
    with pytest.raises(ValidationError, match='gamut'):
        create_transform(TransformRequest(SpaceSpec('camera_linear', path), SpaceSpec('srgb'),
            dcp=DcpSettings(camera_neutral=(1, 1, 1), gamut='error')))


def test_real_look_dispatch_and_missing_elements(tmp_path):
    from test_dcp import write_fixture
    tone = write_fixture(tmp_path / 'tone.dcp', [(50721, 10, np.eye(3).ravel()),
        (50778, 3, [23]), (50940, 11, [0, 0, 0.5, 0.6, 1, 1])])
    request = TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb'), mode='look', profile_path=tone,
                               look_mode='tone_only')
    lut = convert_profile(request, size=17, strength=0.5)
    assert dict(lut.provenance)['experimental'] == 'true'
    assert lut.strength == 0.5
    assert lut.values[8, 8, 8, 0] > 0.5
    matrix = write_fixture(tmp_path / 'no-look.dcp')
    with pytest.raises(UnsupportedError):
        create_transform(TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb'), mode='look', profile_path=matrix))


def test_lazy_missing_module_is_explicit(monkeypatch):
    import camera_profile_studio.service as service
    original = service.importlib.import_module
    def missing(name, package=None):
        if name == '.lut':
            raise ModuleNotFoundError('Deliberately absent integration seam')
        return original(name, package)
    monkeypatch.setattr(service.importlib, 'import_module', missing)
    with pytest.raises(DependencyError, match='lut'):
        service.convert_profile(TransformRequest(SpaceSpec('srgb'), SpaceSpec('srgb')), size=17)

