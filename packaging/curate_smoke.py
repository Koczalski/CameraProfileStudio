"""Publish only reviewable smoke evidence, never extracted payloads or profile fixtures."""
from pathlib import Path
import hashlib
import json
import shutil

root = Path(__file__).resolve().parents[1]
out = root / 'output/distribution'
result_path = out / 'smoke-results.json'
result = json.loads(result_path.read_text(encoding='utf-8-sig'))
sandbox = Path(result['sandbox']).resolve()
if not sandbox.is_relative_to(out.resolve()) or not sandbox.name.startswith('sandbox-'):
    raise RuntimeError('Smoke sandbox is outside the distribution workspace')
curated = out / 'final-smoke'
curated.mkdir(exist_ok=True)
files = [result_path]
files.extend(p for p in sandbox.iterdir() if p.is_file()
             and (p.suffix.lower() in ('.txt', '.json', '.log') or p.name == 'native-app-window.png'))
manifest = []
for source in sorted(files):
    destination = curated / source.name
    shutil.copy2(source, destination)
    manifest.append({'file': source.name, 'bytes': destination.stat().st_size,
                     'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
(curated / 'evidence-manifest.json').write_text(json.dumps({'sandbox': str(sandbox),
    'files': manifest, 'extracted_payload_included': False, 'profile_fixtures_included': False}, indent=2), encoding='utf-8')
print(f'Curated smoke evidence: {curated} ({len(files)} files)')
