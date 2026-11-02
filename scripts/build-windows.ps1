param([switch]$SkipCompile, [ValidateSet('', '0.2.7')][string]$ReleaseVersion = '')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/build-common.ps1"
Set-CpsPhase 'lcms-and-environment'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$stem = 'CameraProfileStudio'
if ($ReleaseVersion) {
    if ($SkipCompile) { throw 'A new release must compile fresh source' }
    if ($env:CPS_ISOLATED_BUILD -ne $root -or ($root -notlike '*/output/releases/0.2.7/workspace' -and $root -notlike '*\output\releases\0.2.7\workspace')) {
        throw 'Use build-release.ps1 with an explicitly approved source freeze'
    }
    $stem += "-$ReleaseVersion"
} elseif ((Get-Content "$root/pyproject.toml" -Raw) -match 'version = "0.2.7"') {
    throw '0.2.7 cannot write legacy release output; use build-release.ps1'
}
$python = Join-Path $root '.venv/Scripts/python.exe'
$out = Join-Path $root 'output/distribution'
New-Item -ItemType Directory -Force $out | Out-Null
if (!(Test-Path 'src/camera_profile_studio/gui/app.py')) { throw 'GUI source is not ready' }
if (!(Test-Path 'src/camera_profile_studio/cli.py')) { throw 'CLI source is not ready' }
$lcms = Join-Path $root 'src/camera_profile_studio/vendor/lcms2'
if (!(Test-Path "$lcms/lcms2.dll")) {
    # Source ZIP intentionally excludes binaries; rebuild without writing into app source.
    $lcms = Join-Path $root '.tools/packaging/lcms-runtime'
    if ($ReleaseVersion) {
        $lcms = Join-Path $out 'lcms-runtime'
        if (!(Test-Path "$lcms/lcms2.dll")) {
            & "$PSScriptRoot/build-lcms.ps1" -OutputDirectory $lcms -WorkDirectory (Join-Path $out 'lcms-build')
        }
    } else {
        & "$PSScriptRoot/build-lcms.ps1" -OutputDirectory $lcms
    }
}
foreach ($file in @('lcms2.dll','LICENSE','SOURCE.json')) {
    if (!(Test-Path "$lcms/$file")) { throw "LittleCMS runtime or provenance missing: $file" }
}
& $python -c 'import sys; assert sys.version_info[:2] == (3,12); import nuitka, PySide6, numpy, scipy'
if ($LASTEXITCODE -ne 0) { throw 'Build environment check failed' }
Set-CpsPhase 'bootstrap'
& "$PSScriptRoot/build-bootstrap.ps1" *> "$out/bootstrap.log"
if (!$?) { throw 'Bootstrap failed' }
Set-CpsPhase 'toolchain-and-licenses'
# Import the existing MSVC toolchain into this process only.
$vswhere = "${env:ProgramFiles(x86)}/Microsoft Visual Studio/Installer/vswhere.exe"
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw 'MSVC x64 toolchain is required' }
$vcvars = Join-Path $vs 'VC/Auxiliary/Build/vcvars64.bat'
cmd /c "`"$vcvars`" >nul && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
}
@{os=[Environment]::OSVersion.VersionString; python=(& $python --version);
  VCToolsVersion=$env:VCToolsVersion; WindowsSDKVersion=$env:WindowsSDKVersion;
  VisualStudioVersion=$env:VisualStudioVersion; Nuitka='2.7.12'; PySide6='6.9.1'} |
  ConvertTo-Json | Set-Content "$out/toolchain-environment.json"
Copy-Item "$out/toolchain-environment.json" "$out/third-party-source/" -Force
$env:PYTHONPATH = Join-Path $root 'src'
# Nuitka's cache holds its downloaded helper tools (depends.exe), the C object cache (clcache) and its bytecode and
# DLL-dependency caches, all content-addressed. With CPS_BUILD_CACHE it persists across builds; otherwise it stays
# private to this stage as before.
$buildCache = Get-CpsCacheRoot
if ($buildCache) { $env:NUITKA_CACHE_DIR = Join-Path $buildCache 'nuitka' }
else { $env:NUITKA_CACHE_DIR = Join-Path $root '.tools/packaging/nuitka-cache' }
New-Item -ItemType Directory -Force $env:NUITKA_CACHE_DIR | Out-Null
if (!$ReleaseVersion) {
    & $python -m pip freeze --all | Where-Object { $_ -notmatch 'camera-profile-studio|^-e | @ file:' } | Set-Content -Encoding ascii requirements-lock.txt
} else {
    & $python -m pip freeze --all | Set-Content -Encoding ascii "$out/installed-packages.txt"
}
& $python packaging/collect_licenses.py *> "$out/licenses.log"
if ($LASTEXITCODE -ne 0) { throw 'License collection failed' }
& $python packaging/collect_upstream_notices.py >> "$out/licenses.log"
if ($LASTEXITCODE -ne 0) { throw 'Upstream license collection failed' }
Copy-Item '.tools/packaging/inno-6.0.5/app/License.txt' 'licenses/Inno-Setup-License.txt' -ErrorAction Stop
New-Item -ItemType Directory -Force 'licenses/LittleCMS' | Out-Null
Copy-Item "$lcms/LICENSE","$lcms/SOURCE.json" 'licenses/LittleCMS/' -Force
$dist = Join-Path $out 'launcher.dist'
# Normalize both supported source layouts to icons.py's preferred runtime directory.
$iconSource = 'assets/icons/fluent'
if (!(Test-Path "$iconSource/ic_fluent_settings_24_regular.svg")) {
    $iconSource = 'src/camera_profile_studio/gui/icons'
}
foreach ($file in @('ic_fluent_settings_24_regular.svg','LICENSE','NOTICE.txt')) {
    if (!(Test-Path "$iconSource/$file")) { throw "Icon artwork/notices not ready: $iconSource/$file" }
}
if ($SkipCompile) {
    & $python packaging/source_fingerprint.py check "$out/compiled-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'SkipCompile cannot reuse changed or unrecorded compiled inputs' }
} else {
    & $python packaging/source_fingerprint.py write "$out/compiled-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to snapshot compilation inputs' }
    Set-CpsPhase 'nuitka'
    $jobs = Get-CpsCompileJobs
    Write-Host "Nuitka: --jobs=$jobs, cache directory $env:NUITKA_CACHE_DIR"
    $args = @('-m','nuitka','--mode=standalone','--msvc=latest','--enable-plugin=pyside6',
        '--include-qt-plugins=platforms,styles,imageformats','--windows-console-mode=attach',
        '--noinclude-dlls=*/qpdf.dll','--noinclude-dlls=qt6pdf.dll','--noinclude-dlls=Qt6Pdf.dll','--noinclude-dlls=*/Qt6Pdf.dll','--noinclude-dlls=*/opencv_videoio_ffmpeg*.dll',
        '--output-filename=CameraProfileStudio.exe',"--output-dir=$out",'--assume-yes-for-downloads',
        '--include-package=camera_profile_studio','--include-package-data=camera_profile_studio',
        '--include-distribution-metadata=camera-profile-studio',
        "--include-data-dir=$iconSource=camera_profile_studio/gui/icons",
        '--report=output/distribution/nuitka-report.xml',"--jobs=$jobs",'packaging/launcher.py')
    $ErrorActionPreference = 'Continue'
    & $python @args *> "$out/nuitka-build.log"
    $compileExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($compileExit -ne 0) { throw "Nuitka failed; see $out/nuitka-build.log" }
    $cacheLine = Select-String -LiteralPath "$out/nuitka-build.log" -Pattern 'cache hits' -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($cacheLine) { Write-Host ('Nuitka C object cache: ' + $cacheLine.Line.Trim()) }
    Set-CpsPhase 'post-compile-checks'
    & $python packaging/source_fingerprint.py check "$out/compiled-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'Source changed during compilation; do not distribute this build' }
}
if (!(Test-Path "$dist/CameraProfileStudio.exe")) { throw 'Standalone executable missing' }
@{executable='launcher.dist/CameraProfileStudio.exe'; sha256=(Get-FileHash "$dist/CameraProfileStudio.exe").Hash;
  bytes=(Get-Item "$dist/CameraProfileStudio.exe").Length; compiled_inputs='compiled-inputs.json';
  compiled_inputs_sha256=(Get-FileHash "$out/compiled-inputs.json").Hash; source_stability_verified=$true;
  reuse_without_compile=[bool]$SkipCompile} | ConvertTo-Json | Set-Content "$out/compiled-executable.json"
& $python packaging/verify_qt_sources.py $dist "$out/qt-source-coverage.json"
if ($LASTEXITCODE -ne 0) { throw 'Actual Qt DLL inventory has missing corresponding-source module coverage' }
# Explicitly retain native LCMS runtime and its notices (not treated as generic data by Nuitka).
if (Test-Path 'src/camera_profile_studio/vendor') {
    New-Item -ItemType Directory -Force "$dist/camera_profile_studio" | Out-Null
    Copy-Item 'src/camera_profile_studio/vendor' "$dist/camera_profile_studio/" -Recurse -Force
}
New-Item -ItemType Directory -Force "$dist/camera_profile_studio/vendor/lcms2" | Out-Null
Copy-Item "$lcms/*" "$dist/camera_profile_studio/vendor/lcms2/" -Force
foreach ($file in @('lcms2.dll','LICENSE','SOURCE.json')) {
    $packaged = "$dist/camera_profile_studio/vendor/lcms2/$file"
    if (!(Test-Path $packaged)) { throw "Packaged LCMS file missing: $file" }
    if ((Get-FileHash "$lcms/$file").Hash -ne (Get-FileHash $packaged).Hash) {
        throw "Packaged LCMS file differs from runtime worker output: $file"
    }
}
Set-CpsPhase 'native-audit-and-collection'
& $python packaging/audit_native.py $dist "$out/native-binary-inventory.json"
if ($LASTEXITCODE -ne 0) { throw 'Native library boundary audit failed' }
Copy-Item licenses "$dist/" -Recurse -Force
if (Test-Path "$dist/source") { throw 'Legacy unfiltered source directory exists; use a fresh standalone build directory' }
& $python packaging/collect_source.py "$dist/$stem-Source.zip"
if ($LASTEXITCODE -ne 0) { throw 'Filtered source collection failed' }
& $python packaging/verify_no_camera_inputs.py $dist "$out/camera-input-exclusion.json"
if ($LASTEXITCODE -ne 0) { throw 'Unreviewed camera inputs entered the payload' }
# LGPL corresponding source (Qt/PySide6, LibRaw, LittleCMS) must accompany every payload.
if (-not (Test-Path "$out/third-party-source/manifest.json")) {
    throw 'Corresponding third-party source bundle is missing; run scripts/build-bootstrap.ps1 first'
}
Copy-Item "$out/third-party-source" "$dist/" -Recurse -Force
& $python -c "import sys; from pathlib import Path; p=Path(sys.base_prefix)/'LICENSE.txt'; import shutil; shutil.copy2(p, 'licenses/Python-LICENSE.txt') if p.exists() else None"
if (Test-Path 'licenses/Python-LICENSE.txt') { Copy-Item 'licenses/Python-LICENSE.txt' "$dist/licenses/" }
& $python packaging/collect_evidence.py "$dist/evidence"
if ($LASTEXITCODE -ne 0) { throw 'Evidence collection failed' }
Set-CpsPhase 'installer-and-portable-zip'
# The portable ZIP and the installer are independent readers of the finished payload: build the ZIP in a second
# process while ISCC (the slow one) runs, each with its own log. Both results are checked before continuing.
$zipCode = "import shutil; shutil.make_archive(r'$root/output/$stem-Portable', 'zip', r'$root/output/distribution', 'launcher.dist')"
$zipProcess = Start-Process -FilePath ([IO.Path]::GetFullPath($python)) -ArgumentList @('-c', ('"{0}"' -f $zipCode)) `
    -WorkingDirectory $root -NoNewWindow -PassThru -RedirectStandardOutput "$out/portable-zip.log" -RedirectStandardError "$out/portable-zip.err.txt"
# Windows PowerShell 5.1 needs the handle cached before the process exits to read ExitCode.
try { $null = $zipProcess.Handle } catch { }
$blockThreads = Get-CpsInstallerThreads
Write-Host "Installer: lzma2/fast with $blockThreads compression block threads"
$ErrorActionPreference = 'Continue'
& '.tools/packaging/inno-6.0.5/app/ISCC.exe' "/DBlockThreads=$blockThreads" "/DPayloadDir=$dist" "/DOutputDir=$root/output" "/DReleaseVersion=$(if ($ReleaseVersion) { $ReleaseVersion } else { '0.1.0' })" "/DArtifactStem=$stem" 'packaging/CameraProfileStudio.iss' *> "$out/installer-build.log"
$installerExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$zipProcess.WaitForExit()
if ($installerExit -ne 0) { throw 'Installer compilation failed' }
if ($zipProcess.ExitCode -ne 0) { throw "ZIP creation failed; see $out/portable-zip.err.txt" }
Set-CpsPhase 'hashes-and-inventory'
$artifactHashes = Get-FileHash "output/$stem-Setup.exe","output/$stem-Portable.zip" -Algorithm SHA256
$artifactHashes | Format-List | Out-File "$out/SHA256.txt"
$artifactHashes | ForEach-Object { @{file=(Split-Path $_.Path -Leaf); sha256=$_.Hash; bytes=(Get-Item $_.Path).Length} } |
    ConvertTo-Json | Set-Content "$out/artifact-hashes.json"
Get-ChildItem $dist -File -Recurse | Select-Object @{n='Path';e={$_.FullName.Substring($dist.Length+1)}},Length | Export-Csv "$out/inventory.csv" -NoTypeInformation
Save-CpsPhases "$out/build-windows-phases.json"
Write-Host 'Artifacts built. Run scripts/verify_distribution.ps1 before distributing.'
