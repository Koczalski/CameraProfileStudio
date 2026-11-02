"""The Windows build launcher keeps every release-* folder out of its copy of the
repository, and so out of Source.zip, which is collected from that copy.

Runs the launcher's own copy statements (taken from the script with the PowerShell
parser, not repeated here) through the real robocopy and ``collect_source.collect``.
"""
from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='the launcher, PowerShell and robocopy are Windows-only')

ROOT = Path(__file__).resolve().parents[1]
RUN_COPY_STATEMENTS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CPS_TEST_LAUNCHER, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "cannot parse the launcher: $errors" }
$assignments = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)
function Find-Assignment($name) {
    $found = @($assignments | Where-Object { $_.Left.Extent.Text -eq $name })
    if (-not $found.Count) { throw "the launcher has no assignment to $name" }
    $found[0]
}
$first = Find-Assignment '$exclude'
$last = Find-Assignment '$copyArgs'
$statements = @($first.Parent.Statements)
$from = [array]::IndexOf($statements, $first)
$to = [array]::IndexOf($statements, $last)
if ($from -lt 0 -or $to -lt $from) { throw 'the copy statements of the launcher are not in one block' }
Invoke-Expression (Find-Assignment '$Version').Extent.Text
$Source = $env:CPS_TEST_SOURCE; $Work = $env:CPS_TEST_WORK
$Kit = Join-Path $Source 'packaging\windows-launcher'
$BuildRoot = Join-Path $Kit 'b'
$RepoMode = $true
Invoke-Expression (($statements[$from..$to] | ForEach-Object { $_.Extent.Text }) -join "`n")
& robocopy.exe @copyArgs | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
exit 0
"""
RELEASE_FOLDERS = ('release-0.2.5', 'release-0.2.6', 'release-0.2.6-1102-1423', 'release-0.2.7')
KEPT = ('pyproject.toml', 'src/camera_profile_studio/module.py', 'packaging/windows-launcher/Build-0.2.7.cmd',
        'docs/release-notes.md', 'docs/release_checklist/steps.md')
LEFT_OUT_OF_COPY = ('.git/config', '.venv/pyvenv.cfg', 'output/old.txt', 'build/old.txt', 'dist/old.txt',
                    'packaging/windows-launcher/b/1102-1423/old.txt', 'packaging/windows-launcher/build-logs/old.log',
                    'src/camera_profile_studio/__pycache__/module.txt')


def launcher() -> Path:
    found = sorted((ROOT / 'packaging' / 'windows-launcher').glob('Build-*.ps1'))
    assert len(found) == 1, found
    return found[0]


def collect_source_module():
    spec = importlib.util.spec_from_file_location('collect_source_under_test', ROOT / 'packaging' / 'collect_source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def copied(tmp_path_factory) -> Path:
    """A dummy repository with release folders, copied the way the launcher copies it."""
    base = tmp_path_factory.mktemp('launcher')
    source, work = base / 'repository', base / 'work'
    dummies = [f'packaging/windows-launcher/{name}/portable/_internal/payload.py' for name in RELEASE_FOLDERS]
    for name in (*KEPT, *LEFT_OUT_OF_COPY, *dummies):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('x\n', encoding='utf-8')
    work.mkdir()
    env = dict(os.environ, CPS_TEST_LAUNCHER=str(launcher()), CPS_TEST_SOURCE=str(source), CPS_TEST_WORK=str(work))
    encoded = base64.b64encode(RUN_COPY_STATEMENTS.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-EncodedCommand', encoded], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return work


@pytest.mark.parametrize('name', RELEASE_FOLDERS)
def test_copy_leaves_out_every_release_folder(copied, name):
    assert (copied / 'packaging' / 'windows-launcher').is_dir()
    assert not (copied / 'packaging' / 'windows-launcher' / name).exists()


@pytest.mark.parametrize('name', KEPT)
def test_copy_keeps_ordinary_files_including_similar_names(copied, name):
    assert (copied / name).is_file()


@pytest.mark.parametrize('name', LEFT_OUT_OF_COPY)
def test_copy_still_leaves_out_what_it_did_before(copied, name):
    assert not (copied / name).exists()


def test_source_zip_has_no_release_folder(copied, tmp_path, monkeypatch):
    collect_source = collect_source_module()
    monkeypatch.setattr(collect_source, 'ROOT', copied)
    archive = tmp_path / 'Source.zip'
    collect_source.collect(archive)
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
    assert not [name for name in names if any(part.startswith('release-') for part in name.split('/')[:-1])]
    for name in ('src/camera_profile_studio/module.py', 'packaging/windows-launcher/Build-0.2.7.cmd',
                 'docs/release-notes.md', 'docs/release_checklist/steps.md', 'pyproject.toml'):
        assert name in names
