"""scripts/build-lcms.ps1 works without the Visual Studio Installer folder on PATH.

vcvars64.bat looks vswhere.exe up on PATH and reports a miss on stderr, which the script's
$ErrorActionPreference = 'Stop' turns into a failure.  Runs the script's own statements (taken with
the PowerShell parser) against a stand-in vswhere.exe and vcvars64.bat, so no Visual Studio is needed.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='PowerShell and vcvars64.bat are Windows-only')

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'build-lcms.ps1'
INSTALLER = Path('Microsoft Visual Studio') / 'Installer'
COMPILE_VSWHERE = r"""
$ErrorActionPreference = 'Stop'
Add-Type -OutputType ConsoleApplication -OutputAssembly $env:CPS_TEST_OUTPUT -TypeDefinition @'
public static class Program {
    public static void Main() { System.Console.WriteLine(System.Environment.GetEnvironmentVariable("CPS_TEST_FAKE_VS")); }
}
'@
"""
# As the launcher runs it: every stream merged, so native stderr becomes an error record.
RUN_STATEMENTS = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:CPS_TEST_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "cannot parse the script: $errors" }
$statements = @($ast.EndBlock.Statements)
$texts = @($statements | ForEach-Object { $_.Extent.Text })
$from = @(0..($texts.Count - 1) | Where-Object { $texts[$_] -like '$vswhere =*' })[0]
$to = @(0..($texts.Count - 1) | Where-Object { $texts[$_] -like 'if ($cacheRoot)*' -and $texts[$_] -like '*$toolchain*' })[0]
if ($null -eq $from -or $null -eq $to -or $to -lt $from) { throw 'the toolchain statements of the script were not found' }
. (Join-Path (Split-Path $env:CPS_TEST_SCRIPT -Parent) 'build-common.ps1')
$work = $env:CPS_TEST_WORK; $source = $work; $expected = 'x'
$code = ($texts[$from..$to] -join "`n") + @'

Write-Output ('VSWHERE=' + $vswhere)
Write-Output ('VS=' + $vs)
Write-Output ('TOOLCHAIN=' + ($toolchain -join ';'))
Write-Output ('PROCESS_PATH=' + $env:PATH)
'@
try { & ([scriptblock]::Create($code)) *>&1 | ForEach-Object { "$_" } }
catch { Write-Output ('ERROR: ' + $_.Exception.Message); exit 1 }
exit 0
"""
VCVARS = """@echo off\r
vswhere.exe -nologo >nul\r
set VCToolsVersion=14.99\r
set WindowsSDKVersion=10.0.99\r
set VisualStudioVersion=17.99\r
exit /b 0\r
"""


def powershell(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-EncodedCommand', encoded], env=env, capture_output=True, timeout=120, check=False)


def without_installer(path: str) -> str:
    return os.pathsep.join(entry for entry in path.split(os.pathsep) if 'microsoft visual studio\\installer' not in entry.lower())


@pytest.fixture(scope='module')
def run(tmp_path_factory):
    base = tmp_path_factory.mktemp('lcms path')
    program_files, install = base / 'Program Files (x86)', base / 'VS'
    (program_files / INSTALLER).mkdir(parents=True)
    vcvars = install / 'VC' / 'Auxiliary' / 'Build' / 'vcvars64.bat'
    vcvars.parent.mkdir(parents=True)
    vcvars.write_bytes(VCVARS.encode('ascii'))
    (base / 'work').mkdir()
    env = dict(os.environ)
    env.update({'PATH': without_installer(env['PATH']), 'PROGRAMFILES(X86)': str(program_files), 'CPS_TEST_FAKE_VS': str(install),
                'CPS_TEST_SCRIPT': str(SCRIPT), 'CPS_TEST_WORK': str(base / 'work'), 'CPS_BUILD_CACHE': str(base / 'cache'),
                'CPS_TEST_OUTPUT': str(program_files / INSTALLER / 'vswhere.exe')})
    compiled = powershell(COMPILE_VSWHERE, env)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = powershell(RUN_STATEMENTS, env)
    text = result.stdout.decode('cp932', 'replace') + result.stderr.decode('cp932', 'replace')
    values = dict(line.split('=', 1) for line in text.splitlines() if re.match(r'^(VSWHERE|VS|TOOLCHAIN|PROCESS_PATH)=', line))
    return {'code': result.returncode, 'text': text, 'values': values, 'install': install, 'env_path': env['PATH'],
            'vswhere': program_files / INSTALLER / 'vswhere.exe'}


def test_toolchain_query_works_without_the_installer_folder_on_path(run):
    assert run['code'] == 0, run['text']
    assert run['values']['TOOLCHAIN'] == '14.99;10.0.99;17.99'


def test_vswhere_is_called_by_its_fixed_path(run):
    assert Path(run['values']['VSWHERE']) == run['vswhere']
    assert Path(run['values']['VS']) == run['install']


def test_installer_folder_is_added_after_the_existing_path(run):
    assert run['values']['PROCESS_PATH'] == run['env_path'] + os.pathsep + str(run['vswhere'].parent)


def test_script_never_writes_persistent_settings():
    source = SCRIPT.read_text(encoding='utf-8-sig').lower()
    for persistent in ('setx', 'setenvironmentvariable', 'hklm:', 'hkcu:', 'hkey_', 'set-itemproperty', 'new-itemproperty'):
        assert persistent not in source, persistent
