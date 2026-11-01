import struct
import pytest
from camera_profile_studio.icc import inspect_icc, _parse_icc, _lut_summary
from camera_profile_studio.errors import InvalidProfileError, UnsupportedError
from camera_profile_studio.lcms import create_icc_transform
from camera_profile_studio.types import SpaceSpec
from test_lcms import srgb_profile_bytes


@pytest.mark.lcms
def test_real_icc_metadata(tmp_path):
    path = tmp_path / 'srgb.icc'
    path.write_bytes(srgb_profile_bytes())
    info = inspect_icc(path)
    assert info.format == 'icc' and info.color_space == 'RGB' and info.pcs == 'XYZ'
    assert info.version.startswith('4.') and len(info.sha256) == 64
    assert {'rXYZ', 'gXYZ', 'bXYZ', 'rTRC', 'gTRC', 'bTRC'} <= {name for name, _ in info.tags}
    assert {cap.feature: cap.status for cap in info.capabilities}['technical'] == 'supported'
    assert 'profile-specific' in info.warnings[-1]


@pytest.mark.lcms
@pytest.mark.parametrize('mutation', ['truncate', 'signature', 'count', 'offset', 'duplicate', 'version'])
def test_bad_icc(tmp_path, mutation):
    data = bytearray(srgb_profile_bytes())
    if mutation == 'truncate':
        data = data[:-1]
    elif mutation == 'signature':
        data[36:40] = b'nope'
    elif mutation == 'count':
        struct.pack_into('>I', data, 128, 0xffffffff)
    elif mutation == 'offset':
        struct.pack_into('>I', data, 136, len(data) + 4)
    elif mutation == 'duplicate':
        data[144:148] = data[132:136]
    elif mutation == 'version':
        data[8] = 5
    path = tmp_path / 'bad.icc'
    path.write_bytes(data)
    with pytest.raises((InvalidProfileError, UnsupportedError)):
        inspect_icc(path)


@pytest.mark.lcms
def test_device_link_is_inspected_but_refused(tmp_path):
    data = bytearray(srgb_profile_bytes())
    data[12:16] = b'link'
    path = tmp_path / 'link.icc'
    path.write_bytes(data)
    info = inspect_icc(path)
    assert info.capabilities[0].status == 'unsupported'
    with pytest.raises(UnsupportedError):
        create_icc_transform(SpaceSpec('icc_rgb', path), SpaceSpec('srgb'))


def test_clut_dimension_bounds():
    data = bytearray(52)
    data[:4] = b'mft2'
    data[8:11] = bytes([3, 3, 255])
    struct.pack_into('>HH', data, 48, 256, 256)
    with pytest.raises(InvalidProfileError):
        _lut_summary(bytes(data))


def test_forward_only_clut_not_inverted(tmp_path):
    # Valid structural 2x2x2 lut8Type; inspect direction without fabricating an inverse.
    clut = bytearray(48 + 3 * 256 + 2 ** 3 * 3 + 3 * 256)
    clut[:4] = b'mft1'
    clut[8:11] = bytes([3, 3, 2])
    header = bytearray(144)
    struct.pack_into('>I', header, 0, 144 + len(clut))
    header[8] = 2
    header[12:24] = b'scnrRGB XYZ '
    header[36:40] = b'acsp'
    struct.pack_into('>I', header, 128, 1)
    header[132:136] = b'A2B0'
    struct.pack_into('>II', header, 136, 144, len(clut))
    path = tmp_path / 'forward.icc'
    path.write_bytes(header + clut)
    info = inspect_icc(path)
    states = {cap.feature: cap.status for cap in info.capabilities}
    assert states['technical.input'] == 'supported' and states['technical.output'] == 'unsupported'
    with pytest.raises(UnsupportedError):
        create_icc_transform(SpaceSpec('srgb'), SpaceSpec('icc_rgb', path))

def _replace_trcs(data, payload):
    result = bytearray(data)
    for index in range(struct.unpack_from('>I', result, 128)[0]):
        name, offset, size = struct.unpack_from('>4sII', result, 132 + 12 * index)
        if name in (b'rTRC', b'gTRC', b'bTRC'):
            assert len(payload) <= size
            result[offset:offset + size] = payload + bytes(size - len(payload))
    return bytes(result)


@pytest.mark.lcms
@pytest.mark.parametrize('samples,forward', [([0, 0], False), ([12000, 12000], False),
    ([0, 40000, 20000, 65535], False), ([0, 0, 32768, 65535], True)])
def test_noninvertible_sampled_trcs_never_authorize_inverse(tmp_path, samples, forward):
    payload = b'curv' + bytes(4) + struct.pack('>I', len(samples)) + struct.pack('>' + 'H' * len(samples), *samples)
    path = tmp_path / 'noninvertible.icc'
    path.write_bytes(_replace_trcs(srgb_profile_bytes(), payload))
    info = inspect_icc(path)
    caps = {cap.feature: cap for cap in info.capabilities}
    assert caps['technical.input'].status == ('supported' if forward else 'unsupported')
    assert caps['technical.output'].status == 'unsupported'
    assert caps['technical.output'].reasons
    for intent in ('perceptual', 'relative', 'saturation', 'absolute'):
        assert caps[f'technical.output.{intent}'].status == 'unsupported'
        with pytest.raises(UnsupportedError):
            create_icc_transform(SpaceSpec('srgb'), SpaceSpec('icc_rgb', path), intent=intent)
    from camera_profile_studio.service import convert_profile
    from camera_profile_studio.types import TransformRequest
    with pytest.raises(UnsupportedError):
        convert_profile(TransformRequest(SpaceSpec('srgb'), SpaceSpec('icc_rgb', path)), size=17)


@pytest.mark.lcms
def test_strictly_increasing_sampled_trc_can_be_inverted(tmp_path):
    payload = b'curv' + bytes(4) + struct.pack('>IHHH', 3, 0, 25000, 65535)
    path = tmp_path / 'invertible.icc'
    path.write_bytes(_replace_trcs(srgb_profile_bytes(), payload))
    info = inspect_icc(path)
    assert {cap.feature: cap.status for cap in info.capabilities}['technical.output'] == 'supported'
    with create_icc_transform(SpaceSpec('srgb'), SpaceSpec('icc_rgb', path)) as transform:
        import numpy as np
        output = transform.apply(np.array([[0.1, 0.4, 0.8], [1, 1, 1]], np.float32))
        assert np.all(output[0] > 0) and output[0, 0] < output[0, 1] < output[0, 2]


@pytest.mark.lcms
def test_parametric_black_plateau_not_inverted(tmp_path):
    # ICC parametric type 1: (x-0.5)^2 above 0.5, zero below it.
    payload = b'para' + bytes(4) + struct.pack('>HHiii', 1, 0, 2 * 65536, 65536, -32768)
    path = tmp_path / 'parametric-flat.icc'
    path.write_bytes(_replace_trcs(srgb_profile_bytes(), payload))
    caps = {cap.feature: cap for cap in inspect_icc(path).capabilities}
    assert caps['technical.input'].status == 'supported'
    assert caps['technical.output'].status == 'unsupported'
    assert 'flat' in ' '.join(caps['technical.output'].reasons)
    with pytest.raises(UnsupportedError):
        create_icc_transform(SpaceSpec('srgb'), SpaceSpec('icc_rgb', path))


@pytest.mark.lcms
def test_singular_colorant_matrix_is_invalid(tmp_path):
    data = bytearray(srgb_profile_bytes())
    offsets = {}
    for index in range(struct.unpack_from('>I', data, 128)[0]):
        name, offset, size = struct.unpack_from('>4sII', data, 132 + 12 * index)
        offsets[name] = offset
    data[offsets[b'gXYZ'] + 8:offsets[b'gXYZ'] + 20] = data[offsets[b'rXYZ'] + 8:offsets[b'rXYZ'] + 20]
    path = tmp_path / 'singular.icc'
    path.write_bytes(data)
    with pytest.raises(InvalidProfileError, match='singular'):
        inspect_icc(path)
    with pytest.raises(InvalidProfileError, match='singular'):
        create_icc_transform(SpaceSpec('srgb'), SpaceSpec('icc_rgb', path))
