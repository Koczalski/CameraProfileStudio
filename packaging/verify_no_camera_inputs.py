"""Reject camera-profile/RAW fixtures in runtime files or the application source ZIP."""
from pathlib import Path
import json
import sys
import zipfile

payload = Path(sys.argv[1])
forbidden = {'.icc', '.icm', '.dcp', '.dng', '.nef', '.cr2', '.cr3', '.arw', '.raf', '.orf', '.rw2', '.x3f'}
found = [p.relative_to(payload).as_posix() for p in payload.rglob('*')
         if p.is_file() and p.suffix.lower() in forbidden]
source = payload / 'CameraProfileStudio-Source.zip'
if source.exists():
    with zipfile.ZipFile(source) as archive:
        found.extend('CameraProfileStudio-Source.zip/' + name for name in archive.namelist()
                     if Path(name).suffix.lower() in forbidden)
report = {'camera_profile_or_raw_payload_files': found, 'pass': not found,
          'boundary': 'Runtime and application source ZIP; upstream library source archives retain their own licensed source/test material.'}
Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding='utf-8')
if found:
    raise RuntimeError('Unreviewed profile/RAW fixtures in payload: ' + ', '.join(found))
print('No camera profile/RAW fixtures in runtime or application source ZIP: PASS')
