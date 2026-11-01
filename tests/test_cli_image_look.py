import json

import numpy as np
import pytest

from camera_profile_studio.cli import main
from test_image_look import fixture_rgb, save_reference


def request_file(tmp_path, payload=None):
    if payload is None:
        save_reference(tmp_path)
        payload = {'schema_version': 1, 'references': [{'path': 'reference.png'}]}
    path = tmp_path / 'project.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def args_for(tmp_path, request=None):
    return ['image-look', '--request', str(request or request_file(tmp_path)),
            '--output', str(tmp_path / 'look.cube'), '--size', '17', '--strength', '.4']


def test_success_relative_paths_report_and_no_overwrite(tmp_path, capsys):
    args = args_for(tmp_path)
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['export']['validation']['valid']
    report = output['image_look_report']
    assert report['status'] == 'approximate_unverified'
    # Without base photos the built-in typical-photo baseline is used and flagged.
    assert report['baseline'] == 'typical_photo'
    assert report['base_count'] == 0
    assert 'typical_photo_baseline' in report['warnings']
    assert 'holdout_report' not in output
    cube = tmp_path / 'look.cube'
    original = cube.read_bytes()
    metadata = json.loads(cube.with_suffix('.json').read_text())
    assert metadata['strength'] == .4
    assert main(args) == 3
    assert json.loads(capsys.readouterr().err)['code'] == 'output_exists'
    assert cube.read_bytes() == original
    assert main(args + ['--overwrite']) == 0
    capsys.readouterr()
    assert cube.read_bytes() == original


@pytest.mark.parametrize('payload', [
    {}, {'references': []}, {'references': ['bad']}, {'references': [{'path': 'x', 'weight': 0}]},
    {'references': [{'path': 'x', 'typo': 3}]}, {'references': [{'path': 'x'}], 'unknown': 1},
    {'references': [{'path': 'x'}], 'settings': {'typo': 1}},
    {'references': [{'path': 'x'}], 'settings': {'neutral_protection': 1.0}},
    {'references': [{'path': 'x', 'role': 'target'}]},
    {'references': [{'path': 'x', 'role': 3}]},
    {'references': [{'path': 'x', 'role': None}]},
    {'references': [{'path': 'x', 'role': 'base'}]},
    {'references': [{'path': 'x'}], 'settings': None},
    {'references': [{'path': 'x'}], 'settings': {'acknowledge_conflicts': 'yes'}},
    {'schema_version': 2, 'references': []},
])
def test_request_validation(tmp_path, capsys, payload):
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 2
    assert 'code' in json.loads(capsys.readouterr().err)
    assert not (tmp_path / 'look.cube').exists()


def read_cube_values(path):
    rows = [tuple(map(float, line.split())) for line in path.read_text().splitlines()
            if line[:1] in '0123456789-.' and len(line.split()) == 3]
    return np.asarray(rows)


def test_base_role_baseline_report_and_metadata(tmp_path, capsys):
    save_reference(tmp_path, fixture_rgb(cast=.04, tone=.4), 'finished.png')
    save_reference(tmp_path, fixture_rgb(), 'mine.png')
    # 'role' is optional: omitted means 'reference'; both spellings are equivalent.
    with_base = request_file(tmp_path, {'schema_version': 1, 'references': [
        {'path': 'finished.png', 'role': 'reference'}, {'path': 'mine.png', 'role': 'base', 'weight': 2.0}]})
    args = args_for(tmp_path, with_base)
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)['image_look_report']
    assert report['baseline'] == 'base_images'
    assert report['base_count'] == 1
    assert 'typical_photo_baseline' not in report['warnings']
    assert report['status'] == 'approximate_unverified'
    based = read_cube_values(tmp_path / 'look.cube')
    metadata = json.loads((tmp_path / 'look.json').read_text())
    assert metadata['provenance']['engine'] == 'image-look-v2'
    assert json.loads(metadata['provenance']['baseline'])['kind'] == 'base_images'
    sources = {item['path']: item for item in json.loads(metadata['provenance']['references'])}
    assert sources['finished.png']['role'] == 'reference'
    assert sources['mine.png']['role'] == 'base' and sources['mine.png']['weight'] == 2.0
    # The same reference without base photos gives another look (typical-photo baseline).
    without_base = request_file(tmp_path, {'references': [{'path': 'finished.png'}]})
    assert main(args_for(tmp_path, without_base) + ['--overwrite']) == 0
    typical = json.loads(capsys.readouterr().out)['image_look_report']
    assert typical['baseline'] == 'typical_photo' and typical['base_count'] == 0
    assert np.max(np.abs(read_cube_values(tmp_path / 'look.cube') - based)) > 1e-3


def test_base_images_do_not_raise_conflicts_and_relative_paths(tmp_path, capsys):
    # Conflicts are judged between reference images only; a base photo may differ freely.
    (tmp_path / 'sub').mkdir()
    save_reference(tmp_path, fixture_rgb(cast=.03), 'sub/finished.png')
    save_reference(tmp_path, fixture_rgb(cast=-.05, tone=.3), 'sub/mine-warm.png')
    save_reference(tmp_path, fixture_rgb(cast=.05, tone=-.3), 'sub/mine-cool.png')
    payload = {'references': [{'path': 'sub/finished.png'},
                              {'path': 'sub/mine-warm.png', 'role': 'base'},
                              {'path': 'sub/mine-cool.png', 'role': 'base'}]}
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 0
    report = json.loads(capsys.readouterr().out)['image_look_report']
    assert report['base_count'] == 2 and report['baseline'] == 'base_images'
    assert report['conflicts'] == []


@pytest.mark.parametrize('role', ['target', 'Reference', '', 3, None, ['base']])
def test_invalid_role_is_structured_error(tmp_path, capsys, role):
    save_reference(tmp_path)
    payload = {'references': [{'path': 'reference.png'}, {'path': 'reference.png', 'role': role}]}
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'image_look_invalid'
    assert not (tmp_path / 'look.cube').exists()


def test_base_only_request_is_refused(tmp_path, capsys):
    save_reference(tmp_path)
    payload = {'references': [{'path': 'reference.png', 'role': 'base'}]}
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'image_look_invalid'
    assert not (tmp_path / 'look.cube').exists()
    assert not (tmp_path / 'look.json').exists()


def test_base_image_is_protected_from_overwrite(tmp_path, capsys):
    save_reference(tmp_path)
    payload = {'references': [{'path': 'reference.png'}, {'path': 'look.json', 'role': 'base'}]}
    path = request_file(tmp_path, payload)
    (tmp_path / 'look.json').write_bytes(b'private base bytes')
    assert main(['image-look', '--request', str(path), '--output', str(tmp_path / 'look.cube'), '--overwrite']) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'source_output_collision'
    assert (tmp_path / 'look.json').read_bytes() == b'private base bytes'


@pytest.mark.parametrize('settings', [{'neutral_protection': 1.0}, {'neutral_protection': 0, 'seed': 1}, []])
def test_removed_and_malformed_settings_are_image_look_invalid(tmp_path, capsys, settings):
    save_reference(tmp_path)
    payload = {'references': [{'path': 'reference.png'}], 'settings': settings}
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 2
    error = json.loads(capsys.readouterr().err)
    assert error['code'] == 'image_look_invalid'
    if isinstance(settings, dict):
        assert 'neutral_protection' in error['message']
    assert not (tmp_path / 'look.cube').exists()
    assert not (tmp_path / 'look.json').exists()


def test_v1_baseline_version_is_rejected(tmp_path, capsys):
    save_reference(tmp_path)
    payload = {'references': [{'path': 'reference.png'}],
               'settings': {'baseline_version': 'cps-designed-prior-v1'}}
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'image_look_baseline'
    assert not (tmp_path / 'look.cube').exists()


def test_same_photo_as_reference_and_base_is_identity(tmp_path, capsys):
    save_reference(tmp_path, fixture_rgb(cast=.03, tone=.3), 'photo.png')
    payload = {'references': [{'path': 'photo.png'}, {'path': 'photo.png', 'role': 'base'}]}
    assert main(args_for(tmp_path, request_file(tmp_path, payload)) + ['--size', '17']) == 0
    assert json.loads(capsys.readouterr().out)['image_look_report']['baseline'] == 'base_images'
    values = read_cube_values(tmp_path / 'look.cube')
    index = np.arange(17 ** 3)
    identity = np.stack((index % 17, (index // 17) % 17, index // 17 ** 2), axis=1) / 16
    assert np.max(np.abs(values - identity)) < 1e-4


def test_v2_settings_are_accepted(tmp_path, capsys):
    save_reference(tmp_path)
    settings = {'seed': 7, 'analysis_size': 96, 'max_dimension': 1024, 'tone_amount': .5, 'color_amount': .5,
                'saturation_amount': .5, 'acknowledge_conflicts': False}
    payload = {'references': [{'path': 'reference.png'}], 'settings': settings}
    assert main(args_for(tmp_path, request_file(tmp_path, payload))) == 0
    metadata = json.loads((tmp_path / 'look.json').read_text())
    recorded = json.loads(metadata['provenance']['settings'])
    assert recorded['analysis_size'] == 96 and recorded['tone_amount'] == .5
    assert 'neutral_protection' not in recorded
    capsys.readouterr()


def test_missing_file_io_exit(tmp_path, capsys):
    args = args_for(tmp_path, request_file(tmp_path, {'references': [{'path': 'missing.png'}]}))
    assert main(args) == 3
    assert json.loads(capsys.readouterr().err)['code'] == 'image_look_source_io'


@pytest.mark.parametrize('text', ['{"references": [], "references": []}', '{"references": NaN}'])
def test_invalid_json(tmp_path, capsys, text):
    path = tmp_path / 'project.json'
    path.write_text(text)
    assert main(args_for(tmp_path, path)) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'invalid_json'


def test_conflict_refusal_and_acknowledgement(tmp_path, capsys):
    save_reference(tmp_path, fixture_rgb(cast=.05), 'warm.png')
    save_reference(tmp_path, fixture_rgb(cast=-.05), 'cool.png')
    payload = {'references': [{'path': 'warm.png'}, {'path': 'cool.png'}]}
    path = request_file(tmp_path, payload)
    args = args_for(tmp_path, path)
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'image_look_conflict'
    assert not (tmp_path / 'look.cube').exists()
    payload['settings'] = {'acknowledge_conflicts': True}
    request_file(tmp_path, payload)
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert 'conflicts_acknowledged' in output['image_look_report']['warnings']


@pytest.mark.parametrize('collision', ['request', 'reference'])
def test_no_source_overwrite_even_when_explicit(tmp_path, capsys, collision):
    path = request_file(tmp_path)
    original = path.read_bytes()
    if collision == 'request':
        output = tmp_path / 'project.cube'
    else:
        # The cube companion .json is also protected when it is an input path.
        payload = {'references': [{'path': 'look.json'}]}
        request_file(tmp_path, payload)
        original = b'private source bytes'
        (tmp_path / 'look.json').write_bytes(original)
        output = tmp_path / 'look.cube'
    assert main(['image-look', '--request', str(path), '--output', str(output), '--overwrite']) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'source_output_collision'
    assert (path if collision == 'request' else tmp_path / 'look.json').read_bytes() == original


@pytest.mark.parametrize('strength', ['nan', '-.1', '1.1'])
def test_invalid_strength_no_artifacts(tmp_path, capsys, strength):
    args = args_for(tmp_path)
    args[-1] = strength
    assert main(args) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'invalid_strength'
    assert not (tmp_path / 'look.cube').exists()


def test_cli_bad_image_and_cancellation_exit_codes(tmp_path, capsys, monkeypatch):
    import camera_profile_studio.image_look as engine
    from camera_profile_studio.errors import CancelledError
    reference = tmp_path / 'bad.png'
    reference.write_bytes(b'not an image')
    request = request_file(tmp_path, {'references': [{'path': 'bad.png'}]})
    args = args_for(tmp_path, request)
    assert main(args) == 3
    assert 'code' in json.loads(capsys.readouterr().err)
    def cancelled(*args, **kwargs):
        raise CancelledError('cancelled', code='cancelled')
    monkeypatch.setattr(engine, 'analyze_image_look', cancelled)
    assert main(args) == 130
    assert json.loads(capsys.readouterr().err)['code'] == 'cancelled'
    assert not (tmp_path / 'look.cube').exists()


def test_ten_real_grayscale_ramps_cli_exports(tmp_path, capsys):
    from PIL import Image
    ramp = np.broadcast_to(np.linspace(.2, 1., 256)[None, :, None], (64, 256, 3))
    for i in range(10):
        Image.fromarray(np.round(ramp * 255).astype(np.uint8)).save(tmp_path / f'bright-{i}.png')
    request = request_file(tmp_path, {'references': [{'path': f'bright-{i}.png'} for i in range(10)]})
    assert main(args_for(tmp_path, request)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['export']['validation']['valid']
    assert 'limited_color_coverage' in output['image_look_report']['warnings']
    metadata = json.loads((tmp_path / 'look.json').read_text())
    assert metadata['provenance']['engine'] == 'image-look-v2'
    model = json.loads(metadata['provenance']['model'])
    assert 'tone_coefficients' not in model
    assert len(model['tone_x']) == len(model['tone_y']) == 15
    assert len(model['cast']) == 5 and len(model['band_gain']) == 5 and len(model['sector_gain']) == 12
    assert model['tone_x'][0] == 0 and model['tone_x'][-1] == 1
    assert all(b >= a for a, b in zip(model['tone_y'], model['tone_y'][1:]))


def test_nul_json_reference_cli_main_and_subprocess(tmp_path, capsys):
    import subprocess
    import sys
    request = request_file(tmp_path, {'references': [{'path': 'nul\0.png'}]})
    args = args_for(tmp_path, request)
    assert main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert json.loads(captured.err)['code'] == 'image_look_invalid'
    result = subprocess.run([sys.executable, '-B', '-m', 'camera_profile_studio.cli', *args],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert not result.stdout
    assert 'Traceback' not in result.stderr
    assert json.loads(result.stderr)['code'] == 'image_look_invalid'
    assert not (tmp_path / 'look.cube').exists()
    assert not (tmp_path / 'look.json').exists()
