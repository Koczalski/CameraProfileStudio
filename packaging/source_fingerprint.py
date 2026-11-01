"""Guard reuse of native output while allowing documentation/evidence-only repacks."""
from pathlib import Path
import hashlib
import json
import sys

root = Path(__file__).resolve().parents[1]
files = []
for directory in ('src', 'assets'):
    if (root / directory).exists():
        files.extend(p for p in (root / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix.lower() not in ('.pyc', '.pyo'))
files.extend(root / name for name in ('packaging/launcher.py', 'pyproject.toml', 'requirements-lock.txt'))
current = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(files)}
snapshot = Path(sys.argv[2])
if sys.argv[1] == 'write':
    snapshot.write_text(json.dumps(current, indent=2), encoding='utf-8')
elif sys.argv[1] == 'check':
    previous = json.loads(snapshot.read_text(encoding='utf-8'))
    changed = sorted(name for name in set(previous) | set(current)
                     if previous.get(name) != current.get(name))
    if changed:
        raise RuntimeError('Compiled input changed; full rebuild required: ' + ', '.join(changed))
    print('Compiled input fingerprint unchanged: PASS')
else:
    raise ValueError('Expected write or check')
