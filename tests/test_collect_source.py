"""collect_source.py leaves every release-* folder out by itself, so build scripts run
directly (without the launcher's filtered copy) cannot put one into the source archive.

Runs a copy of the real script inside a dummy repository, the way those build scripts call it.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEFT_OUT = tuple(f'packaging/windows-launcher/{name}/portable/_internal/payload.py'
                 for name in ('release-0.2.5', 'release-0.2.6', 'release-0.2.6-1102-1423', 'release-0.2.7')) + (
    'docs/release-archive/old.md',
    'src/camera_profile_studio/release-old/module.py',
    'licenses/upstream-source/library/release-1.0/data.txt',   # not a notice
)
KEPT = (
    'pyproject.toml',
    'src/camera_profile_studio/module.py',
    'packaging/release_workspace.py',
    'packaging/windows-launcher/Build-0.2.7.ps1',
    'docs/release-notes.md',
    'docs/release_checklist/steps.md',
    'docs/prerelease-1/steps.md',
    # notices keep the paths of their upstream archives, whatever those are called
    'licenses/upstream-source/library/release-1.0/LICENSE.txt',
    'licenses/upstream-source/library/release-1.0/NOTICE.md',
)


@pytest.fixture()
def repository(tmp_path) -> Path:
    folder = tmp_path / 'repository'
    for name in (*LEFT_OUT, *KEPT):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x\n', encoding='utf-8')
    (folder / 'packaging').mkdir(exist_ok=True)
    shutil.copyfile(ROOT / 'packaging' / 'collect_source.py', folder / 'packaging' / 'collect_source.py')
    return folder


def collected(repository: Path, script: str, destination: Path, cwd: Path) -> list[str]:
    subprocess.run([sys.executable, script, str(destination)], cwd=cwd, check=True, capture_output=True, text=True, timeout=60)
    with zipfile.ZipFile(destination) as archive:
        return archive.namelist()


@pytest.fixture(params=['relative', 'absolute'])
def names(request, repository, tmp_path) -> list[str]:
    """build-fresh-source.ps1 runs it relative to the root; build-release.ps1 by full path from anywhere."""
    destination = tmp_path / 'staging-source.zip'
    if request.param == 'relative':
        return collected(repository, 'packaging/collect_source.py', destination, repository)
    return collected(repository, str(repository / 'packaging' / 'collect_source.py'), destination, tmp_path)


@pytest.mark.parametrize('name', LEFT_OUT)
def test_release_folders_are_left_out(names, name):
    assert name not in names


@pytest.mark.parametrize('name', KEPT)
def test_similar_names_and_notices_are_kept(names, name):
    assert name in names


@pytest.mark.parametrize('script', ['build-release.ps1', 'build-fresh-source.ps1', 'build-windows.ps1'])
def test_build_scripts_collect_with_this_script(script):
    assert 'packaging/collect_source.py' in (ROOT / 'scripts' / script).read_text(encoding='utf-8-sig')
