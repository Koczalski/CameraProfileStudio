"""The build scripts that call vcvars64.bat work without the Visual Studio Installer folder on PATH.

vcvars64.bat looks vswhere.exe up on PATH and reports a miss on stderr, which $ErrorActionPreference = 'Stop'
turns into a failure.  Runs the scripts' own statements (taken with the PowerShell parser) against a stand-in
vswhere.exe and vcvars64.bat, so no Visual Studio is needed.
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
SCRIPTS = ROOT / 'scripts'
LCMS, WINDOWS = 'build-lcms.ps1', 'build-windows.ps1'
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
# Several scripts run one after the other in one process, as build-fresh-source.ps1 runs them.
RUN_STATEMENTS = r"""
$ErrorActionPreference = 'Stop'
. (Join-Path $env:CPS_TEST_SCRIPTS 'build-common.ps1')
$work = $env:CPS_TEST_WORK; $source = $work; $expected = 'x'
$code = ''
foreach ($name in ($env:CPS_TEST_NAMES -split ';')) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:CPS_TEST_SCRIPTS $name), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "cannot parse $name : $errors" }
    $texts = @($ast.EndBlock.Statements | ForEach-Object { $_.Extent.Text })
    $from = @(0..($texts.Count - 1) | Where-Object { $texts[$_] -like '$vswhere =*' })[0]
    $to = @(0..($texts.Count - 1) | Where-Object { ($texts[$_] -like 'if ($cacheRoot)*' -and $texts[$_] -like '*$toolchain*') -or
                                                   $texts[$_] -like 'cmd /c*$vcvars*' })[0]
    if ($null -eq $from -or $null -eq $to -or $to -lt $from) { throw "the toolchain statements of $name were not found" }
    $code += ($texts[$from..$to] -join "`n") + "`n"
}
$code += @'
Write-Output ('VSWHERE=' + $vswhere)
Write-Output ('VS=' + $vs)
Write-Output ('TOOLCHAIN=' + ($toolchain -join ';'))
Write-Output ('IMPORTED=' + $env:VCToolsVersion)
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
def stand_ins(tmp_path_factory):
    """A vswhere.exe at the fixed path, a vcvars64.bat that needs it on PATH, and a PATH without it."""
    base = tmp_path_factory.mktemp('vswhere path')
    program_files, install = base / 'Program Files (x86)', base / 'VS'
    (program_files / INSTALLER).mkdir(parents=True)
    vcvars = install / 'VC' / 'Auxiliary' / 'Build' / 'vcvars64.bat'
    vcvars.parent.mkdir(parents=True)
    vcvars.write_bytes(VCVARS.encode('ascii'))
    (base / 'work').mkdir()
    env = dict(os.environ)
    env.update({'PATH': without_installer(env['PATH']), 'PROGRAMFILES(X86)': str(program_files), 'CPS_TEST_FAKE_VS': str(install),
                'CPS_TEST_SCRIPTS': str(SCRIPTS), 'CPS_TEST_WORK': str(base / 'work'), 'CPS_BUILD_CACHE': str(base / 'cache'),
                'CPS_TEST_OUTPUT': str(program_files / INSTALLER / 'vswhere.exe')})
    compiled = powershell(COMPILE_VSWHERE, env)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    return {'env': env, 'install': install, 'vswhere': program_files / INSTALLER / 'vswhere.exe'}


def run(stand_ins, *names: str) -> dict:
    result = powershell(RUN_STATEMENTS, dict(stand_ins['env'], CPS_TEST_NAMES=';'.join(names)))
    text = result.stdout.decode('cp932', 'replace') + result.stderr.decode('cp932', 'replace')
    values = dict(line.split('=', 1) for line in text.splitlines() if re.match(r'^(VSWHERE|VS|TOOLCHAIN|IMPORTED|PROCESS_PATH)=', line))
    return {'code': result.returncode, 'text': text, 'values': values}


@pytest.fixture(scope='module', params=[LCMS, WINDOWS])
def each_script(request, stand_ins):
    return {**run(stand_ins, request.param), 'name': request.param}


def test_toolchain_query_works_without_the_installer_folder_on_path(each_script):
    assert each_script['code'] == 0, each_script['text']
    if each_script['name'] == LCMS:
        assert each_script['values']['TOOLCHAIN'] == '14.99;10.0.99;17.99'
    else:
        assert each_script['values']['IMPORTED'] == '14.99'   # vcvars64 is imported into this process


def test_vswhere_is_called_by_its_fixed_path(each_script, stand_ins):
    assert Path(each_script['values']['VSWHERE']) == stand_ins['vswhere']
    assert Path(each_script['values']['VS']) == stand_ins['install']


def test_installer_folder_is_added_after_the_existing_path(each_script, stand_ins):
    expected = stand_ins['env']['PATH'] + os.pathsep + str(stand_ins['vswhere'].parent)
    assert each_script['values']['PROCESS_PATH'] == expected


def test_folder_is_added_once_when_both_scripts_run_in_one_process(stand_ins):
    # build-fresh-source.ps1 runs build-lcms.ps1 and then build-windows.ps1 in the same process
    both = run(stand_ins, LCMS, WINDOWS)
    assert both['code'] == 0, both['text']
    folder = str(stand_ins['vswhere'].parent).lower()
    assert [entry.lower() for entry in both['values']['PROCESS_PATH'].split(os.pathsep)].count(folder) == 1


@pytest.mark.parametrize('name', [LCMS, WINDOWS, 'build-common.ps1'])
def test_scripts_never_write_persistent_settings(name):
    source = (SCRIPTS / name).read_text(encoding='utf-8-sig').lower()
    for persistent in ('setx', 'hklm:', 'hkcu:', 'hkey_', 'set-itemproperty', 'new-itemproperty', "'machine'", "'user'"):
        assert persistent not in source, persistent
    # build-windows.ps1 imports vcvars64 into its own process, which is the only allowed target
    assert source.count('setenvironmentvariable') == source.count("'process')")
