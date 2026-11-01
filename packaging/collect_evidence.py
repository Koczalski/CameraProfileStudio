"""Copy bounded smoke evidence, never sandbox installations or entire desktops."""
from pathlib import Path
import json
import shutil
import sys

root = Path(__file__).resolve().parents[1]
out = root / 'output/distribution'
destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
for name in ('smoke-results.json', 'qt-source-coverage.json', 'native-binary-inventory.json',
             'toolchain-environment.json', 'lcms-rebuild-validation.json', 'compiled-inputs.json'):
    source = out / name
    if source.is_file():
        shutil.copy2(source, destination / name)
smoke = out / 'smoke-results.json'
if smoke.exists():
    sandbox = Path(json.loads(smoke.read_text(encoding='utf-8-sig'))['sandbox']).resolve()
    if not sandbox.is_relative_to(out.resolve()) or not sandbox.name.startswith('sandbox-'):
        raise RuntimeError('Refusing evidence path outside the known distribution sandbox')
    for source in sandbox.iterdir():
        if source.is_file() and (source.suffix.lower() in ('.json', '.txt', '.log')
                                 or source.name == 'native-app-window.png'):
            shutil.copy2(source, destination / source.name)
(destination / 'README.txt').write_text(
    'Smoke evidence describes the tested native code and the earlier package instance.\n'
    'A documentation/evidence-only repack changes ZIP/Setup hashes; rerun verification\n'
    'on final artifacts and retain final SHA256.txt outside these artifacts.\n'
    'Do not interpret missing/skipped native GUI evidence as a passed visual gate.\n', encoding='ascii')
