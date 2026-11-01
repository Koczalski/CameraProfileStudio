"""0.2.4: exception handling found by the adversarial scan.

E1  unhandled GUI-thread exceptions were invisible in the packaged app
E2  export failed on file systems without hard links (exFAT/FAT32, some shares)
E3  export failures all read as a generic I/O error
E4  a missing/unreadable image read as 'decode failed: [Errno 2] ...'
E5  batch: two inputs with the same name silently wrote one output
E6  export_lut(None) raised TypeError
E7  decode_png on truncated data raised struct/zlib errors
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from camera_profile_studio.errors import ExportError, ImageError, ValidationError
from camera_profile_studio.images import load_image
from camera_profile_studio.lut import decode_png, encode_png, export_lut, generate_lut
from camera_profile_studio.types import SpaceSpec


class Identity:
    input_space = output_space = SpaceSpec('srgb')

    def apply(self, rgb, context=None):
        return np.asarray(rgb, np.float32)


LUT = generate_lut(Identity(), size=17)


# ---------------------------------------------------------------------- E2
def test_export_without_hard_links_still_refuses_to_overwrite(tmp_path, monkeypatch):
    import camera_profile_studio.lut as lutmod

    def no_link(src, dst, *a, **k):
        raise OSError(errno.EINVAL, 'Incorrect function')   # what Windows reports on exFAT
    monkeypatch.setattr(lutmod.os, 'link', no_link)
    result = export_lut(LUT, tmp_path / 'a.cube')
    assert result.cube_path.exists() and result.metadata_path.exists()
    assert 'LUT_3D_SIZE 17' in result.cube_path.read_text()
    before = result.cube_path.read_bytes()
    with pytest.raises(ExportError) as exc:
        export_lut(LUT, tmp_path / 'a.cube')
    assert exc.value.code == 'output_exists' and result.cube_path.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.suffix in ('.tmp', '.lock')]


def test_export_fallback_does_not_hide_real_permission_errors(tmp_path, monkeypatch):
    import camera_profile_studio.lut as lutmod
    monkeypatch.setattr(lutmod.os, 'link', lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, 'denied')))
    monkeypatch.setattr(lutmod.os, 'open', lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, 'denied')) if len(a) > 1 and a[1] & os.O_EXCL and str(a[0]).endswith('.cube') else os.open.__wrapped__(*a, **k) if hasattr(os.open, '__wrapped__') else _real_open(*a, **k))
    with pytest.raises(ExportError) as exc:
        export_lut(LUT, tmp_path / 'denied.cube')
    assert exc.value.code in ('output_permission', 'export_io_error')
    assert not (tmp_path / 'denied.cube').exists()


_real_open = os.open


# ---------------------------------------------------------------------- E3
@pytest.mark.parametrize('make, code', [
    (lambda d: d / 'missing' / 'x.cube', 'output_dir_missing'),
    (lambda d: (d / 'isdir.cube').mkdir() or d / 'isdir.cube', 'invalid_export_path'),
    (lambda d: ((d / 'taken.json').write_text('{}'), d / 'taken.cube')[1], 'output_exists'),
])
def test_export_errors_are_specific(tmp_path, make, code):
    path = make(tmp_path)
    with pytest.raises(ExportError) as exc:
        export_lut(LUT, path)
    assert exc.value.code == code
    assert dict(exc.value.details)['path'] == str(path)


@pytest.mark.skipif(sys.platform == 'win32' or os.geteuid() == 0, reason='needs a read-only directory')
def test_export_into_readonly_directory(tmp_path):
    ro = tmp_path / 'ro'
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        with pytest.raises(ExportError) as exc:
            export_lut(LUT, ro / 'x.cube')
        assert exc.value.code == 'output_permission'
    finally:
        os.chmod(ro, 0o700)


# ---------------------------------------------------------------------- E6/E7
def test_export_requires_a_path():
    for bad in (None, '', '   ', 3):
        with pytest.raises(ExportError) as exc:
            export_lut(LUT, bad)
        assert exc.value.code == 'invalid_export_path'


def test_decode_png_rejects_truncated_and_corrupt_data():
    blob = encode_png(np.zeros((4, 4, 3), np.uint8))
    for bad in (blob[:-10], blob[:20], blob[:-1], blob[:33] + b'\0' * 4 + blob[37:]):
        with pytest.raises(ValidationError) as exc:
            decode_png(bad)
        assert exc.value.code == 'invalid_png'


# ---------------------------------------------------------------------- E4
def test_image_errors_say_what_is_wrong(tmp_path):
    with pytest.raises(ImageError) as missing:
        load_image(tmp_path / 'nope.png')
    assert missing.value.code == 'image_not_found'
    with pytest.raises(ImageError) as folder:
        load_image(tmp_path)
    assert folder.value.code == 'image_not_found'
    junk = tmp_path / 'junk.png'
    junk.write_bytes(b'\x89PNG not really')
    with pytest.raises(ImageError) as bad:
        load_image(junk)
    assert bad.value.code == 'image_decode_error' and dict(bad.value.details)['path'] == str(junk)
    gif = tmp_path / 'x.gif'
    Image.new('RGB', (2, 2)).save(gif)
    with pytest.raises(ImageError) as unsupported:
        load_image(gif)
    assert unsupported.value.code == 'unsupported_image'
    with pytest.raises(ImageError) as raw_missing:
        load_image(tmp_path / 'nope.nef')
    assert raw_missing.value.code == 'image_not_found'


def test_gui_error_mapping_is_localised():
    pytest.importorskip('PySide6')
    from camera_profile_studio.gui.backend import Engine
    from camera_profile_studio.gui.i18n import T
    from camera_profile_studio.gui.strings import STRINGS
    engine = Engine()
    for exc in (ImageError('x', code='image_not_found'), ExportError('x', code='output_exists'), ExportError('x', code='disk_full')):
        info = engine.map_error(exc)
        assert info.key.startswith('error.code.'), info.key
        assert info.key in STRINGS['en'] and info.key in STRINGS['ja']
    T.set_language('ja')
    assert '見つかりません' in engine.map_error(ImageError('x', code='image_not_found')).summary()
    T.set_language('en')


def test_export_with_overlong_name_is_reported_not_raised(tmp_path):
    with pytest.raises(ExportError) as exc:
        export_lut(LUT, tmp_path / ('a' * 300 + '.cube'))
    assert exc.value.code in ('path_too_long', 'export_io_error')
