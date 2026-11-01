import json
import subprocess
import sys
import numpy as np
import pytest
from camera_profile_studio.cli import main
from test_lcms import srgb_profile_bytes


def test_inspect_json(tmp_path, capsys):
    path = tmp_path / 'test.icc'
    path.write_bytes(srgb_profile_bytes())
    assert main(['inspect', str(path), '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['schema_version'] == 1 and payload['format'] == 'icc'
    assert any(c['feature'] == 'jpeg_accuracy' and c['status'] == 'unverified' for c in payload['capabilities'])


def test_convert_cli_and_no_overwrite(tmp_path, capsys):
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'schema_version': 1, 'input_space': {'kind': 'srgb'},
                                   'output_space': {'kind': 'linear_srgb'}}))
    output = tmp_path / 'technical.cube'
    args = ['convert', '--request', str(request), '--output', str(output), '--size', '17']
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['export']['validation']['valid']
    assert output.exists() and output.with_suffix('.json').exists()
    original = output.read_bytes()
    assert main(args) == 3
    assert json.loads(capsys.readouterr().err)['code'] == 'output_exists'
    assert output.read_bytes() == original


@pytest.mark.parametrize('payload', [
    {'input_space': {'kind': 'srgb', 'typo': 1}, 'output_space': {'kind': 'srgb'}},
    {'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'srgb'}, 'typo': 1},
    {'schema_version': 2, 'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'srgb'}},
    {'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'srgb'}, 'black_point_compensation': 'yes'},
])
def test_request_errors_are_structured(tmp_path, capsys, payload):
    request = tmp_path / 'bad-request.json'
    request.write_text(json.dumps(payload))
    output = tmp_path / 'bad.cube'
    assert main(['convert', '--request', str(request), '--output', str(output)]) == 2
    assert 'code' in json.loads(capsys.readouterr().err)
    assert not output.exists()


def test_json_duplicate_and_nan_rejected(tmp_path, capsys):
    request = tmp_path / 'bad-request.json'
    for text in ('{"a": 1, "a": 2}', '{"a": NaN}'):
        request.write_text(text)
        assert main(['convert', '--request', str(request), '--output', str(tmp_path / 'bad.cube')]) == 2
        assert json.loads(capsys.readouterr().err)['code'] == 'invalid_json'


def test_real_cli_matching_holdout(tmp_path, capsys):
    source = np.random.default_rng(8).uniform(0.05, 0.95, (160, 3)).astype(np.float32)
    target = (source * [0.94, 0.98, 0.9]).astype(np.float32)
    samples = tmp_path / 'samples.npz'
    np.savez(samples, source_rgb=source, target_rgb=target,
             groups=np.array([f'patch-{i // 8}' for i in range(len(source))]), evidence=np.array('Synthetic paired calibration; not camera evidence'))
    settings = tmp_path / 'fit.json'
    settings.write_text(json.dumps({'fit': 'ccm', 'seed': 42}))
    output = tmp_path / 'match.cube'
    assert main(['match', '--samples', str(samples), '--settings', str(settings), '--output', str(output), '--size', '17']) == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload['holdout_report']
    assert report['after']['count'] >= 6
    assert report['after']['mean'] < report['before']['mean']
    assert not set(report['train_indices']) & set(report['holdout_indices'])


def test_unsafe_npz_refused(tmp_path, capsys):
    samples = tmp_path / 'bad.npz'
    np.savez(samples, source_rgb=np.zeros((12, 3)), target_rgb=np.zeros((12, 3)),
             groups=np.array(['x'] * 12, dtype=object), evidence=np.array('unsafe'))
    assert main(['match', '--samples', str(samples), '--output', str(tmp_path / 'bad.cube')]) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'invalid_samples'


def test_module_entrypoint_has_no_qt_import():
    code = "import sys; import camera_profile_studio.cli; assert not any(m.startswith('PySide6') for m in sys.modules)"
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)
    run = subprocess.run([sys.executable, '-m', 'camera_profile_studio', '--help'], check=True, capture_output=True, text=True)
    assert 'inspect' in run.stdout and 'convert' in run.stdout and 'match' in run.stdout


def test_cli_refuses_to_overwrite_request_with_sidecar(tmp_path, capsys):
    request = tmp_path / 'same.json'
    text = json.dumps({'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'srgb'}})
    request.write_text(text)
    assert main(['convert', '--request', str(request), '--output', str(tmp_path / 'same.cube'), '--overwrite']) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'source_output_collision'
    assert request.read_text() == text


def test_cli_rejects_deeply_nested_json_without_traceback(tmp_path, capsys):
    request = tmp_path / 'deep.json'
    request.write_text('[' * 100000 + ']' * 100000)
    assert main(['convert', '--request', str(request), '--output', str(tmp_path / 'x.cube')]) == 2
    assert json.loads(capsys.readouterr().err)['code'] == 'invalid_json'


def test_cli_output_directory_is_reported_as_invalid_path(tmp_path, capsys):
    request = tmp_path / 'r.json'
    request.write_text(json.dumps({'schema_version': 1, 'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'linear_srgb'}}))
    assert main(['convert', '--request', str(request), '--output', str(tmp_path)]) == 3
    assert json.loads(capsys.readouterr().err)['code'] == 'invalid_export_path'
