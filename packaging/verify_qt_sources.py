"""Fail closed on Qt binaries/plugins not mapped to an acquired source module."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    **{name: 'qtbase' for name in (
        'Core', 'Gui', 'Widgets', 'Network', 'OpenGL', 'OpenGLWidgets',
        'PrintSupport', 'Sql', 'Xml', 'Concurrent', 'DBus', 'Test')},
    'Svg': 'qtsvg', 'SvgWidgets': 'qtsvg', 'ShaderTools': 'qtshadertools',
}
# Nuitka may lowercase DLL names (qt6core.dll); match case-insensitively so that
# every top-level Qt library is inventoried and an unmapped one (e.g. Qt6Pdf from
# qtwebengine, whose source is not shipped) fails the gate.
MODULES_LC = {key.lower(): value for key, value in MODULES.items()}
PLUGINS = {
    **{name: 'qtbase' for name in (
        'qwindows', 'qoffscreen', 'qminimal', 'qdirect2d', 'qmodernwindowsstyle',
        'qgif', 'qico', 'qjpeg', 'qwindowsprintersupport', 'qschannelbackend',
        'qopensslbackend', 'qcertonlybackend')},
    'qsvg': 'qtsvg', 'qsvgicon': 'qtsvg',
    **{name: 'qtimageformats' for name in ('qicns', 'qtga', 'qtiff', 'qwbmp', 'qwebp')},
}


def verify(payload: Path, report: Path) -> None:
    manifest = json.loads((ROOT / 'packaging/upstream-sources.json').read_text())
    source_dir = ROOT / 'output/distribution/third-party-source'
    acquired = set()
    for entry in manifest['archives']:
        archive = source_dir / entry['filename']
        if not archive.is_file():
            raise RuntimeError(f'Missing corresponding source archive: {archive}')
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if archive.stat().st_size != entry['bytes'] or digest != entry['sha256']:
            raise RuntimeError(f'Source archive integrity mismatch: {archive}')
        acquired.add(entry['component'])
    inventory, unknown = [], []
    for path in sorted(payload.rglob('*')):
        if not path.is_file():
            continue
        module = None
        name = path.stem
        parts = [part.lower() for part in path.relative_to(payload).parts]
        if path.suffix.lower() == '.dll' and name.lower().startswith('qt6'):
            module = MODULES_LC.get(name[3:].lower())
        elif path.suffix.lower() == '.dll' and 'pyside6' in parts and any(p in parts for p in ('plugins', 'qt-plugins')):
            module = PLUGINS.get(name.lower(), PLUGINS.get(name))
        elif path.suffix.lower() == '.qm' and 'pyside6' in parts:
            module = 'qttranslations'
        else:
            continue
        item = {'path': path.relative_to(payload).as_posix(), 'source_module': module,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        inventory.append(item)
        if module not in acquired:
            unknown.append(item['path'])
    result = {'payload': str(payload), 'inventory': inventory, 'unmapped': unknown,
              'module_archive_coverage': bool(inventory) and not unknown,
              'public_redistribution_cleared': bool(manifest.get('public_redistribution_cleared')),
              'review': manifest.get('reason', '')}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, indent=2), encoding='utf-8')
    if not inventory or unknown:
        raise RuntimeError(f'Qt source coverage gate failed: {unknown or "no Qt binaries found"}')
    print(f'Qt source module inventory gate: PASS ({len(inventory)} files)')


if __name__ == '__main__':
    verify(Path(sys.argv[1]), Path(sys.argv[2]))
