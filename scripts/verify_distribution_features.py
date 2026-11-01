"""Exercise packaged DCP look and SciPy matching, without importing application source."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time

import numpy as np

root = Path(__file__).resolve().parents[1]
exe, sandbox = (Path(p).resolve() for p in sys.argv[1:3])
env = os.environ.copy()
for key in ('PYTHONPATH', 'PYTHONHOME', 'QTDIR', 'QT_PLUGIN_PATH', 'QTPLUGINPATH',
            'QT_QPA_PLATFORM_PLUGIN_PATH', 'CPS_LCMS2_DLL'):
    env.pop(key, None)
windows_root = os.environ['SYSTEMROOT']
env['PATH'] = os.pathsep.join((str(Path(windows_root) / 'System32'), windows_root))
profile = root / 'input/ccd_profiles/Nikon_D70s.dcp'
assert hashlib.sha256(profile.read_bytes()).hexdigest() == '0fc337fafc606ef121a8ea80ad6b0e8bd06b1fdd1f37a595104f92167946a6e8'
request = {'input_space': {'kind': 'srgb'}, 'output_space': {'kind': 'srgb'},
           'mode': 'look', 'profile_path': str(profile), 'look_mode': 'look_only'}
(sandbox / 'look-request.json').write_text(json.dumps(request), encoding='utf-8')
rng = np.random.default_rng(20261020)
source = rng.uniform(0.05, 0.95, (256, 3)).astype(np.float32)
target = np.clip(source ** np.array([1.08, 0.95, 1.04]) * np.array([0.91, 0.96, 0.89]) + 0.025, 0, 1).astype(np.float32)
groups = np.array([f'synthetic-block-{i // 16}' for i in range(len(source))])
np.savez(sandbox / 'match-samples.npz', source_rgb=source, target_rgb=target, groups=groups,
         evidence=np.array('Synthetic paired numerical dependency smoke; no OEM/camera reproduction claim'))
(sandbox / 'match-settings.json').write_text(json.dumps({'fit': 'ccm_tone_lut', 'grid_size': 5,
    'regularization': 0.01, 'holdout_fraction': 0.25, 'seed': 42}), encoding='utf-8')


def run(name, arguments):
    start = time.monotonic()
    result = subprocess.run([str(exe), *arguments], cwd=sandbox, env=env,
                            capture_output=True, text=True, timeout=120)
    (sandbox / f'{name}.stdout.txt').write_text(result.stdout, encoding='utf-8')
    (sandbox / f'{name}.stderr.txt').write_text(result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'Packaged {name} failed ({result.returncode}): {result.stderr}')
    return json.loads(result.stdout), time.monotonic() - start


def validate_cube(name):
    path = sandbox / f'{name}.cube'
    rows, size = [], None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(('#', 'TITLE', 'DOMAIN_')):
            continue
        if line.startswith('LUT_3D_SIZE'):
            size = int(line.split()[1])
        else:
            rows.append(tuple(map(float, line.split())))
    values = np.asarray(rows)
    assert size == 17 and values.shape == (17 ** 3, 3) and np.isfinite(values).all()
    assert values.min() >= -1e-6 and values.max() <= 1 + 1e-6
    i = np.arange(17 ** 3)
    identity = np.stack((i % 17, (i // 17) % 17, i // 17 ** 2), axis=1) / 16
    difference = float(np.max(np.abs(values - identity)))
    assert difference > 0.001, f'{name} unexpectedly identity'
    metadata = json.loads(path.with_suffix('.json').read_text())
    assert metadata['cube_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    return {'rows': len(rows), 'finite': True, 'max_difference_from_identity': difference,
            'companion_cube_sha256_matches': True}

look, look_seconds = run('dcp-look', ['convert', '--request', str(sandbox / 'look-request.json'),
    '--output', str(sandbox / 'dcp-look.cube'), '--size', '17'])
match, match_seconds = run('scipy-match', ['match', '--samples', str(sandbox / 'match-samples.npz'),
    '--settings', str(sandbox / 'match-settings.json'), '--output', str(sandbox / 'scipy-match.cube'), '--size', '17'])
report = match['holdout_report']
train, holdout = set(report['train_indices']), set(report['holdout_indices'])
assert train and holdout and train.isdisjoint(holdout) and train | holdout == set(range(len(source)))
assert set(groups[list(train)]).isdisjoint(groups[list(holdout)]), 'Group leakage into holdout'
for key in ('before', 'after'):
    assert report[key]['count'] == len(holdout)
    assert all(np.isfinite(report[key][k]) for k in ('mean', 'median', 'p95', 'maximum'))
assert report['after']['mean'] < report['before']['mean']
result = {'packaged_dcp_look': {'status': 'PASS', 'seconds': look_seconds, **validate_cube('dcp-look')},
          'packaged_scipy_match': {'status': 'PASS', 'seconds': match_seconds, **validate_cube('scipy-match'),
              'train_count': len(train), 'holdout_count': len(holdout), 'indices_and_groups_disjoint': True,
              'mean_delta_e00_before': report['before']['mean'], 'mean_delta_e00_after': report['after']['mean']},
          'camera_oem_reproduction_claim': False, 'runtime_raw_gui_workflow_tested': False,
          'clean_vm': False, 'network_isolated': False}
(sandbox / 'feature-results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
