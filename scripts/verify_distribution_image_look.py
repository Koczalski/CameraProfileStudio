"""Fresh native CLI gate for the frozen docs/IMAGE_LOOK_API.md contract."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

exe, sandbox = (Path(arg).resolve() for arg in sys.argv[1:3])
env = os.environ.copy()
for name in ('PYTHONPATH', 'PYTHONHOME', 'QTDIR', 'QT_PLUGIN_PATH', 'QTPLUGINPATH',
             'QT_QPA_PLATFORM_PLUGIN_PATH', 'CPS_LCMS2_DLL'):
    env.pop(name, None)
env['PATH'] = os.pathsep.join((str(Path(env['SYSTEMROOT']) / 'System32'), env['SYSTEMROOT']))
rng = np.random.default_rng(42)
image = np.clip(rng.uniform(0.08, 0.9, (128, 128, 3)) ** 1.25 * 255, 0, 255).astype(np.uint8)
reference = sandbox / 'image-look-reference.png'
Image.fromarray(image).save(reference)
reference2 = sandbox / 'image-look-reference-2.png'
Image.fromarray(np.roll(image, 13, axis=0)).save(reference2)
# A flatter, darker, less colourful "own photo" the look would be applied to (role "base").
base = sandbox / 'image-look-base.png'
Image.fromarray(np.clip(32 + 0.62 * (np.roll(image, 29, axis=1).astype(np.float64) - 32), 0, 255).astype(np.uint8)).save(base)
# (case, [(file, role)]): "role" is optional and defaults to "reference"; the base case is explicit.
for case, entries in (('single', [(reference, None)]),
                      ('multiple', [(reference, None), (reference2, None)]),
                      ('base', [(reference, 'reference'), (reference2, 'reference'), (base, 'base')])):
    references = [item for item, _ in entries]
    base_count = sum(1 for _, role in entries if role == 'base')
    request = sandbox / f'image-look-{case}-request.json'
    request.write_text(json.dumps({
        'schema_version': 1,
        'references': [{'path': item.name} if role is None else {'path': item.name, 'role': role}
                       for item, role in entries],
        'settings': {'seed': 42}}), encoding='utf-8')
    output = sandbox / f'image-look-{case}.cube'
    result = subprocess.run([str(exe), 'image-look', '--request', str(request), '--output', str(output),
                             '--size', '17'], cwd=sandbox, env=env, capture_output=True, text=True, timeout=120)
    (sandbox / f'image-look-{case}.stdout.txt').write_text(result.stdout, encoding='utf-8')
    (sandbox / f'image-look-{case}.stderr.txt').write_text(result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'Native image-look failed ({result.returncode}): {result.stderr}')
    report = json.loads(result.stdout)
    assert report['schema_version'] == 1
    image_look_report = report['image_look_report']
    assert image_look_report['status'] == 'approximate_unverified'
    assert image_look_report['base_count'] == base_count
    if base_count:
        assert image_look_report['baseline'] == 'base_images'
        assert 'typical_photo_baseline' not in image_look_report['warnings']
    else:
        assert image_look_report['baseline'] == 'typical_photo'
        assert 'typical_photo_baseline' in image_look_report['warnings']
    assert 'holdout_report' not in report
    rows = []
    size = None
    for line in output.read_text().splitlines():
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
    index = np.arange(17 ** 3)
    identity = np.stack((index % 17, (index // 17) % 17, index // 17 ** 2), axis=1) / 16
    assert np.max(np.abs(values - identity)) > 0.001, 'Unexpected identity image-look export'
    metadata = json.loads(output.with_suffix('.json').read_text())
    assert metadata['cube_sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert metadata['software']['camera-profile-studio'] == '0.2.6', metadata['software']
    provenance_software = json.loads(metadata['provenance']['software'])
    assert provenance_software['camera-profile-studio'] == '0.2.6', provenance_software
    assert metadata['provenance']['engine'] == 'image-look-v2', metadata['provenance']['engine']
    roles = sorted(item['role'] for item in json.loads(metadata['provenance']['references']))
    assert roles == sorted(['base'] * base_count + ['reference'] * (len(references) - base_count)), roles
    (sandbox / f'image-look-{case}-feature-result.json').write_text(json.dumps({
        'status': 'PASS', 'reference_count': len(references) - base_count, 'base_count': base_count,
        'engine': metadata['provenance']['engine'], 'baseline': image_look_report['baseline'],
        'native_executable': str(exe), 'python_on_path': False,
        'reference': 'generated synthetic RGB, not camera evidence',
        'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
        'software': metadata['software'], 'provenance_software': provenance_software,
        'cube_sha256': metadata['cube_sha256'], 'image_look_report': image_look_report}, indent=2), encoding='utf-8')
    print(f'Native Image Look Creator ({case}) export: PASS')
