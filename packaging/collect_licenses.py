"""Collect installed distribution notices verbatim, with an auditable manifest."""
from importlib import metadata
from pathlib import Path
import json
import shutil
import sys

root = Path(__file__).resolve().parents[1]
out = root / "licenses" / "third-party"
out.mkdir(parents=True, exist_ok=True)
manifest = []
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
    name = dist.metadata['Name']
    if name == 'camera-profile-studio':
        continue
    entry = {'name': name, 'version': dist.version,
             'license': dist.metadata.get('License-Expression') or dist.metadata.get('License', ''),
             'project_urls': dist.metadata.get_all('Project-URL', []), 'notice_files': []}
    for item in dist.files or []:
        if any(word in item.name.lower() for word in ('license', 'copying', 'copyright', 'notice')):
            source = Path(dist.locate_file(item))
            if source.is_file() and source.suffix.lower() not in ('.pyc', '.pyd', '.dll', '.py'):
                # Keep wheel-relative paths to avoid collisions and preserve nested notices.
                safe = [p for p in item.parts if p not in ('..', '.', '/')]
                target = out / name / Path(*safe)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                entry['notice_files'].append(target.relative_to(root / 'licenses').as_posix())
    manifest.append(entry)
(root / 'licenses' / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
# The application's own licence travels with the third-party notices (shown in Help > Licences).
if (root / 'LICENSE').is_file():
    shutil.copyfile(root / 'LICENSE', root / 'licenses' / 'APP-LICENSE.txt')
print(f'Collected notices for {len(manifest)} installed distributions with Python {sys.version}')
