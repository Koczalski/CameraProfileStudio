"""Offline release preparation checks and explicit approved-input fingerprints."""
from pathlib import Path
import hashlib
import json
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'output/image-look-packaging/prep/immutable-baseline.json'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def immutable():
    records = json.loads(BASELINE.read_text(encoding='utf-8-sig'))
    expected = {item['path'].replace('\\', '/') for item in records
                if item['path'].replace('\\', '/').startswith('output/distribution/')}
    actual = {p.relative_to(ROOT).as_posix() for p in (ROOT / 'output/distribution').rglob('*')
              if p.is_file()}
    if actual != expected:
        raise RuntimeError('Prior distribution file set changed: added=' + str(sorted(actual - expected))
                           + '; missing=' + str(sorted(expected - actual)))
    for item in records:
        path = ROOT / item['path']
        if not path.is_file() or path.stat().st_size != item['length'] or digest(path).lower() != item['sha256'].lower():
            raise RuntimeError(f'Prior release changed: {path}')
    print(f'Prior release immutable: PASS ({len(records)} files)')


def fingerprint():
    files = []
    for name in ('src', 'assets', 'packaging', 'scripts'):
        files.extend(p for p in (ROOT / name).rglob('*') if p.is_file()
                     and not any(part in ('__pycache__', '.pytest_cache') or part.endswith('.egg-info') for part in p.parts)
                     and p.suffix.lower() not in ('.pyc', '.pyo'))
    files.extend(ROOT / name for name in ('pyproject.toml', 'requirements-lock.txt', 'docs/IMAGE_LOOK_API.md',
                                         'docs/BUILD_0.2.6.md', '.github/workflows/windows-distribution.yml'))
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(files))}


def validate():
    from collect_source import include
    assert tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'] == '0.2.6'
    assert "__version__ = '0.2.6'" in (ROOT / 'src/camera_profile_studio/__init__.py').read_text()
    installer = (ROOT / 'packaging/CameraProfileStudio.iss').read_text()
    assert 'AppId={{B15FF861-C98A-4F3F-B46D-143C62935FA3}' in installer
    assert 'AppVersion={#ReleaseVersion}' in installer
    for name in ('tasks/logs/old.zip', 'input/test.nef', 'src/__pycache__/x.pyc',
                 'docs/logs/nested.zip', 'tests/fixtures/test.dng', 'docs/delivery/old.json'):
        assert not include(ROOT / name), name
    for name in ('src/camera_profile_studio/image_look.py', 'licenses/LICENSE', 'packaging/upstream-sources.json'):
        assert include(ROOT / name), name
    immutable()
    print('Version, AppId, filtered-source policy: PASS; no compile/install/smoke executed')


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'validate':
        validate()
    elif action == 'immutable':
        immutable()
    elif action == 'freeze':
        target = Path(sys.argv[2])
        # Invoke only after the lead explicitly declares source freeze.
        with target.open('x', encoding='utf-8') as stream:
            json.dump(fingerprint(), stream, indent=2)
        print(f'Explicit source-freeze snapshot: {target}')
    elif action == 'check-stage':
        expected = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8-sig'))
        stage = Path(sys.argv[3])
        for name, sha256 in expected.items():
            path = stage / name
            if not path.is_file() or digest(path) != sha256:
                raise RuntimeError(f'Staged frozen input differs: {name}')
        print('Staged frozen inputs: PASS')
    elif action == 'check':
        previous = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8-sig'))
        current = fingerprint()
        changed = sorted(k for k in previous.keys() | current.keys() if previous.get(k) != current.get(k))
        if changed:
            raise RuntimeError('Approved inputs changed: ' + ', '.join(changed))
        print('Approved source freeze: PASS')
    else:
        raise ValueError('Expected validate, immutable, freeze, check-stage or check')
