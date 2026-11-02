"""Record every shipped native binary and verify intended shared-library boundaries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys

payload, report = map(Path, sys.argv[1:3])
_review = json.loads((Path(__file__).resolve().parent / 'upstream-sources.json').read_text(encoding='utf-8-sig'))


def inventory(path):
    output = subprocess.check_output(['dumpbin.exe', '/dependents', str(path)], text=True)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'path': path.relative_to(payload).as_posix(), 'bytes': path.stat().st_size, 'sha256': digest,
            'imports': re.findall(r'^\s+([\w.-]+\.dll)\s*$', output, re.M | re.I)}


# One dumpbin.exe run and one hash per binary, independent of each other: use all logical processors
# (the work is subprocess/hashing, which releases the GIL). map() keeps the sorted order of the report.
paths = [path for path in sorted(payload.rglob('*'))
         if path.suffix.lower() in ('.dll', '.pyd', '.exe') and path.is_file()]
with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
    rows = list(pool.map(inventory, paths))
errors = []
if any(Path(row['path']).name.lower() == 'qt6pdf.dll' for row in rows):
    errors.append('Unexpected Qt6Pdf.dll: its source (qtwebengine) is not shipped and the app does not use it')
if any('ffmpeg' in row['path'].lower() for row in rows):
    errors.append('Unexpected FFmpeg binary: still-image distribution deliberately excludes it')
raw_extensions = [row for row in rows if Path(row['path']).name.startswith('_rawpy.')]
for row in raw_extensions:
    if 'raw_r.dll' not in [name.lower() for name in row['imports']]:
        errors.append('rawpy extension no longer uses the reviewed dynamically replaceable LibRaw DLL')
if raw_extensions and not any(Path(row['path']).name.lower() == 'raw_r.dll' for row in rows):
    errors.append('rawpy is present but replaceable raw_r.dll is missing')
report.write_text(json.dumps({'native_binaries': rows, 'errors': errors,
    'public_redistribution_cleared': bool(_review.get('public_redistribution_cleared')),
    'review': _review.get('reason', '')}, indent=2), encoding='utf-8')
if errors:
    raise RuntimeError('; '.join(errors))
print(f'Native inventory: {len(rows)} binaries; shared LibRaw/Qt6Pdf/FFmpeg boundary checks pass')
