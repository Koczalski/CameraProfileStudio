"""CLI: ``diagnose`` and ``repair`` (LUT health check)."""
import json

import numpy as np

from camera_profile_studio import lut_health as lh
from camera_profile_studio.cli import main


def _grid(n):
    axis = np.linspace(0.0, 1.0, n)
    return np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)


def _write_cube(path, values):
    rows = values.transpose(2, 1, 0, 3).reshape(-1, 3)             # red fastest
    lines = [f'TITLE "cli fixture"', f'LUT_3D_SIZE {values.shape[0]}'] + [f'{r:.9f} {g:.9f} {b:.9f}' for r, g, b in rows]
    path.write_text('\n'.join(lines) + '\n')
    return path


def _spike(n=17):
    values = _grid(n)
    values[n // 2, 5, 9, 0] += 0.05                                   # one node jumps: a tone_jump finding
    return np.clip(values, 0, 1)


def test_diagnose_prints_json_and_writes_the_full_report(tmp_path, capsys):
    lut = _write_cube(tmp_path / 'spike.cube', _spike())
    assert main(['diagnose', '--lut', str(lut)]) == 0                  # warnings never change the exit code
    brief = json.loads(capsys.readouterr().out)
    assert brief['overall'] == 'warning' and brief['size'] == 17 and brief['lut']['title'] == 'cli fixture'
    assert [f['code'] for f in brief['findings']] == list(lh.FINDING_CODES)
    jump = next(f for f in brief['findings'] if f['code'] == 'tone_jump')
    assert jump['severity'] == 'warning' and jump['value'] > jump['threshold'] == 0.03
    assert float(jump['details']['robust_ratio']) > 12
    assert all('locations' not in f for f in brief['findings']) and 'gray_axis' not in brief
    full = tmp_path / 'report.json'
    assert main(['diagnose', '--lut', str(lut), '--json', str(full)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['report_file'] == str(full)
    report = json.loads(full.read_text())
    assert len(report['gray_axis']) == 257 and report['findings'][4]['locations'][0] == [8, 5, 9]
    assert main(['diagnose', '--lut', str(lut), '--json', str(full)]) == 3          # no silent overwrite
    assert json.loads(capsys.readouterr().err)['code'] == 'output_exists'
    assert main(['diagnose', '--lut', str(lut), '--json', str(full), '--overwrite']) == 0
    capsys.readouterr()
    identity = _write_cube(tmp_path / 'identity.cube', _grid(17))
    assert main(['diagnose', '--lut', str(identity)]) == 0
    assert json.loads(capsys.readouterr().out)['overall'] == 'ok'


def test_repair_writes_cube_and_hald_png_with_health_sidecar(tmp_path, capsys):
    lut = _write_cube(tmp_path / 'spike.cube', _spike())
    original = lut.read_bytes()
    cube = tmp_path / 'fixed.cube'
    assert main(['repair', '--lut', str(lut), '--output', str(cube), '--fix-jumps', '--smooth', '0.2']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['before']['overall'] == 'warning' and result['after']['overall'] == 'ok'
    assert result['repair']['settings']['fix_jumps'] is True and result['exported']['overall'] == 'ok'
    sidecar = json.loads(cube.with_suffix('.json').read_text())
    assert sidecar['health']['overall'] == 'ok' and sidecar['repair']['settings']['smooth'] == 0.2
    assert lh.diagnose_lut(lh.load_lut_file(cube)).overall == 'ok' and lut.read_bytes() == original
    hald = tmp_path / 'fixed_hald.png'                  # the .json sidecar of 'fixed' is already taken
    assert main(['repair', '--lut', str(lut), '--output', str(hald), '--lock-gray', '--format', 'hald-png',
                 '--size', '17']) == 0
    capsys.readouterr()
    loaded = lh.load_lut_file(hald)
    assert loaded.source_format == 'hald_png' and loaded.size == 16
    assert main(['repair', '--lut', str(lut), '--output', str(cube)]) == 3             # exists: needs --overwrite
    assert json.loads(capsys.readouterr().err)['code'] == 'output_exists'
    assert main(['repair', '--lut', str(lut), '--output', str(lut)]) in (2, 3)         # never over the input
    capsys.readouterr()
    assert lut.read_bytes() == original


def test_unreadable_and_one_dimensional_luts_give_structured_errors(tmp_path, capsys):
    assert main(['diagnose', '--lut', str(tmp_path / 'missing.cube')]) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'lut_unreadable'
    shaper = tmp_path / 'shaper.cube'
    shaper.write_text('LUT_1D_SIZE 2\n0 0 0\n1 1 1\n')
    assert main(['diagnose', '--lut', str(shaper)]) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'lut_1d_only'
    assert main(['repair', '--lut', str(shaper), '--output', str(tmp_path / 'out.cube')]) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'lut_1d_only'
    assert not (tmp_path / 'out.cube').exists()
