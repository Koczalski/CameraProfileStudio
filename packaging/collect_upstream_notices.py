"""Preserve verbatim license texts from acquired source archives without extraction."""
from pathlib import Path, PurePosixPath
import json
import tarfile

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'packaging/upstream-sources.json').read_text())
records = []
for entry in manifest['archives']:
    archive = root / 'output/distribution/third-party-source' / entry['filename']
    with tarfile.open(archive) as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (not member.isfile() or '..' in path.parts or path.is_absolute()
                    or member.size > 1_000_000):
                continue
            name = path.name.lower()
            if not (name.startswith(('license', 'copying', 'copyright', 'notice'))
                    or name in ('readme.ijg', 'readme.ijg.txt')):
                continue
            if path.suffix.lower() in ('.c', '.cpp', '.h', '.py', '.cmake', '.in'):
                continue
            target = root / 'licenses/upstream-source' / entry['component'] / Path(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            records.append({'archive': entry['filename'], 'member': member.name,
                            'notice': target.relative_to(root / 'licenses').as_posix()})
(root / 'licenses/upstream-notice-manifest.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
print(f'Preserved {len(records)} upstream source notice files')
