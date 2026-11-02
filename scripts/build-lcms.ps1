param([string]$OutputDirectory = '', [string]$WorkDirectory = '')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/build-common.ps1"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv/Scripts/python.exe'
if (!$OutputDirectory) { $OutputDirectory = Join-Path $root '.tools/packaging/lcms-runtime' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (!$WorkDirectory) { $WorkDirectory = Join-Path $root '.tools/packaging/lcms-build' }
$work = [IO.Path]::GetFullPath($WorkDirectory)
$logs = Join-Path $root 'output/distribution'
$sourceDir = Join-Path $logs 'third-party-source'
New-Item -ItemType Directory -Force $OutputDirectory,$work,$logs,$sourceDir | Out-Null
$archive = Join-Path $sourceDir 'Little-CMS-lcms2.17.tar.gz'
$url = 'https://github.com/mm2/Little-CMS/archive/refs/tags/lcms2.17.tar.gz'
$expected = '6E6F6411DB50E85AE8FF7777F01B2DA0614AAC13B7B9FCBEA66DC56A1BC71418'
if (!(Test-Path $archive)) { Get-CpsFile -Url $url -Destination $archive -Sha256 $expected }
if ((Get-FileHash $archive).Hash -ne $expected) { throw 'LittleCMS source archive SHA256 mismatch' }
$source = Join-Path $work 'Little-CMS-lcms2.17'
$vswhere = Get-CpsVswhere
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw 'Visual Studio C++ x64 build tools are required' }
$vcvars = Join-Path $vs 'VC/Auxiliary/Build/vcvars64.bat'
$project = Join-Path $source 'Projects/VC2022/lcms2_DLL/lcms2_DLL.vcxproj'
# Build cache: the DLL is reused when the source archive SHA-256, the compiler/SDK versions and the build flags are
# identical. A reused DLL goes through the same functional and PE validation below as a freshly built one.
$cacheDir = $null
$cacheRoot = Get-CpsCacheRoot
if ($cacheRoot) {
    $batch = Join-Path $work 'toolchain.cmd'
    @('@echo off', "call `"$vcvars`" >nul", 'if errorlevel 1 exit /b 1',
      'echo %VCToolsVersion%', 'echo %WindowsSDKVersion%', 'echo %VisualStudioVersion%') | Set-Content -Encoding ascii $batch
    $toolchain = @(& cmd.exe /d /c $batch)
    if ($LASTEXITCODE -ne 0 -or $toolchain.Count -lt 3) { throw 'Visual Studio toolchain query failed' }
    $key = Get-CpsTextHash (('lcms-runtime-v1', $expected, ($toolchain -join ';'), 'msbuild /m Release|x64 lcms2_DLL.vcxproj static-CRT') -join '|').ToLowerInvariant()
    $cacheDir = Join-Path $cacheRoot "lcms\$key"
}
$cacheHit = $false
if ($cacheDir -and (Test-Path -LiteralPath "$cacheDir/lcms2.dll.sha256")) {
    $stored = (Get-Content -LiteralPath "$cacheDir/lcms2.dll.sha256" -Raw).Trim()
    $cacheHit = (Test-Path -LiteralPath "$cacheDir/lcms2.dll") -and (Test-Path -LiteralPath "$cacheDir/LICENSE") -and
        (Test-Path -LiteralPath "$cacheDir/AUTHORS") -and ((Get-FileHash -LiteralPath "$cacheDir/lcms2.dll").Hash -eq $stored)
}
if ($cacheHit) {
    Copy-Item "$cacheDir/lcms2.dll","$cacheDir/LICENSE","$cacheDir/AUTHORS" $OutputDirectory -Force
    "LittleCMS build cache hit ($key); compile skipped, validation below still runs." | Set-Content -Encoding ascii "$logs/lcms-rebuild.log"
    Write-Host "LittleCMS build cache hit: $key"
} else {
    & $python -c "import sys,tarfile; t=tarfile.open(sys.argv[1]); t.extractall(sys.argv[2], filter='data')" $archive $work
    if ($LASTEXITCODE -ne 0) { throw 'LittleCMS source extraction failed' }
    # Toolchain changes are confined to cmd.exe; upstream Release|x64 uses static /MT CRT.
    $batch = Join-Path $work 'build.cmd'
    @("@echo off", "call `"$vcvars`" >nul", 'if errorlevel 1 exit /b 1',
      "msbuild `"$project`" /nologo /m /p:Configuration=Release /p:Platform=x64 /v:minimal > `"$logs/lcms-rebuild.log`" 2>&1",
      'exit /b %errorlevel%') | Set-Content -Encoding ascii $batch
    & cmd.exe /d /c $batch
    if ($LASTEXITCODE -ne 0) { throw "LittleCMS build failed; see $logs/lcms-rebuild.log" }
    Copy-Item "$source/bin/lcms2.dll","$source/LICENSE","$source/AUTHORS" $OutputDirectory -Force
}
try {
    & $python "$root/packaging/verify_lcms_runtime.py" "$OutputDirectory/lcms2.dll" | Set-Content -Encoding utf8 "$logs/lcms-rebuild-validation.json"
    if ($LASTEXITCODE -ne 0) { throw 'Rebuilt LittleCMS functional validation failed' }
    $batch = Join-Path $work 'inventory.cmd'
    @('@echo off', "call `"$vcvars`" >nul", 'if errorlevel 1 exit /b 1',
      "dumpbin /headers /dependents /exports `"$OutputDirectory/lcms2.dll`" > `"$logs/lcms-rebuild-pe.txt`" 2>&1",
      'exit /b %errorlevel%') | Set-Content -Encoding ascii $batch
    & cmd.exe /d /c $batch
    if ($LASTEXITCODE -ne 0) { throw 'LittleCMS native inventory failed' }
    $pe = Get-Content "$logs/lcms-rebuild-pe.txt" -Raw
    if ($pe -notmatch '8664 machine \(x64\)' -or $pe -notmatch 'KERNEL32.dll') { throw 'Unexpected LCMS architecture/imports' }
    if ($pe -match '(?im)^\s+(VCRUNTIME\S*|MSVCP\S*|api-ms-win-crt-\S*)\.dll\s*$') { throw 'Expected static CRT for LCMS' }
} catch {
    # A cached DLL that fails validation must not be offered again.
    if ($cacheHit) { Remove-Item -LiteralPath $cacheDir -Recurse -Force -ErrorAction SilentlyContinue }
    throw
}
if ($cacheDir -and -not $cacheHit) {
    # Only a DLL that passed the functional and PE validation above is stored.
    New-Item -ItemType Directory -Force $cacheDir | Out-Null
    Copy-Item "$OutputDirectory/lcms2.dll","$OutputDirectory/LICENSE","$OutputDirectory/AUTHORS" $cacheDir -Force
    (Get-FileHash "$OutputDirectory/lcms2.dll").Hash | Set-Content -Encoding ascii "$cacheDir/lcms2.dll.sha256"
}
@{name='LittleCMS';version='2.17';license='MIT';source_archive_url=$url;
  source_archive_sha256=$expected;source_commit='5176347635785e53ee5cee92328f76fda766ecc6';
  recipe='scripts/build-lcms.ps1; official VC2022 Release|x64 project, static CRT';
  binary_sha256=(Get-FileHash "$OutputDirectory/lcms2.dll").Hash;
  binary_bytes=(Get-Item "$OutputDirectory/lcms2.dll").Length;
  visual_studio=$vs;validation='version 2170, float32 identity and nonidentity D50 transform PASS';
  byte_identical_rebuild_guaranteed=$false} | ConvertTo-Json | Set-Content "$OutputDirectory/SOURCE.json"
Write-Host "LittleCMS 2.17 rebuilt and verified: $OutputDirectory"
