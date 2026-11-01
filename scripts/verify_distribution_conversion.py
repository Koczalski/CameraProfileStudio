"""Generate a synthetic ICC fixture and test only the packaged CLI for conversion."""
from pathlib import Path
import json
import math
import os
import subprocess
import sys

from PIL import ImageCms

exe = Path(sys.argv[1]).resolve()
sandbox = Path(sys.argv[2]).resolve()
sandbox.mkdir(parents=True, exist_ok=True)
fixture = sandbox / 'generated-srgb.icc'
fixture.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
request = sandbox / 'convert-request.json'
request.write_text(json.dumps({'input_space': {'kind': 'srgb'},
                              'output_space': {'kind': 'linear_srgb'}}), encoding='utf-8')
env = os.environ.copy()
for name in ('PYTHONPATH', 'PYTHONHOME', 'QTDIR', 'QT_PLUGIN_PATH', 'QTPLUGINPATH',
             'QT_QPA_PLATFORM_PLUGIN_PATH', 'CPS_LCMS2_DLL'):
    env.pop(name, None)
windows_root = os.environ['SYSTEMROOT']
env['PATH'] = os.pathsep.join((str(Path(windows_root) / 'System32'), windows_root))
commands = [
    ['inspect', str(fixture), '--json'],
    ['convert', '--request', str(request), '--output', str(sandbox / 'srgb-linear.cube'), '--size', '17'],
]
for index, args in enumerate(commands):
    result = subprocess.run([str(exe), *args], cwd=sandbox, env=env,
                            capture_output=True, text=True, timeout=120)
    (sandbox / f'conversion-{index}.stdout.txt').write_text(result.stdout, encoding='utf-8')
    (sandbox / f'conversion-{index}.stderr.txt').write_text(result.stderr, encoding='utf-8')
    if result.returncode != 0:
        raise RuntimeError(f'Packaged CLI failed ({result.returncode}): {args}; {result.stderr}')
    if index == 0:
        inspection = json.loads(result.stdout)
        if not isinstance(inspection, dict):
            raise AssertionError('ICC inspection did not return a JSON object')

rows = []
size = None
for line in (sandbox / 'srgb-linear.cube').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith(('#', 'TITLE', 'DOMAIN_')):
        continue
    if line.startswith('LUT_3D_SIZE'):
        size = int(line.split()[1])
        continue
    row = tuple(map(float, line.split()))
    assert len(row) == 3 and all(math.isfinite(x) for x in row)
    rows.append(row)
assert size == 17 and len(rows) == size ** 3

def decode(value):
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

maximum = 0.0
for i, row in enumerate(rows):
    encoded = ((i % size) / (size - 1), ((i // size) % size) / (size - 1),
               (i // (size * size)) / (size - 1))
    maximum = max(maximum, *(abs(actual - decode(value)) for actual, value in zip(row, encoded)))
assert maximum < 0.0001, f'Nonidentity sRGB decode mismatch or channel ordering error: {maximum}'
report = {'generated_icc_inspect': 'PASS', 'srgb_to_linear_srgb_17': 'PASS',
          'rows': len(rows), 'max_absolute_error_against_direct_curve': maximum,
          'external_python_needed_by_packaged_process': False,
          'fixture_generated_with_development_python': True,
          'clean_vm': False, 'network_isolated': False}
(sandbox / 'conversion-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
