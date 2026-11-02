param(
    [string]$FreezeManifest = '',
    [switch]$SourceFreezeApproved,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv/Scripts/python.exe'
$release = Join-Path $root 'output/releases/0.2.6'
$stage = [IO.Path]::GetFullPath((Join-Path $release 'workspace'))
if ($ValidateOnly) {
    & $python "$root/packaging/release_workspace.py" validate
    if ($LASTEXITCODE -ne 0) { throw 'Release preparation validation failed' }
    return
}
if (!$SourceFreezeApproved -or !$FreezeManifest) { throw 'Explicit source-freeze approval and manifest are required; no build started' }
$FreezeManifest = (Resolve-Path $FreezeManifest).Path
& $python "$root/packaging/release_workspace.py" check $FreezeManifest
if ($LASTEXITCODE -ne 0) { throw 'Approved source freeze differs from current inputs' }
if (Test-Path $stage) { throw 'Release workspace already exists; do not silently reuse a binary or overwrite evidence' }
if (Get-ChildItem "$release/CameraProfileStudio-0.2.6-*" -ErrorAction SilentlyContinue) {
    throw 'Versioned release artifacts already exist; refusing to overwrite them'
}
& $python "$root/packaging/release_workspace.py" immutable
if ($LASTEXITCODE -ne 0) { throw 'Prior release baseline failed before staging' }
if (Test-Path "$release/approved-source-freeze.json") {
    throw 'Approved freeze publication already exists; refusing to overwrite it'
}
New-Item -ItemType Directory -Force $release | Out-Null
[IO.File]::Copy($FreezeManifest, "$release/approved-source-freeze.json", $false)
& $python "$root/packaging/collect_source.py" "$release/staging-source.zip"
if ($LASTEXITCODE -ne 0) { throw 'Source staging failed' }
Expand-Archive "$release/staging-source.zip" $stage
# Only interpreter/tool caches are shared. Output, licenses, lockfile and evidence are private copies.
New-Item -ItemType Junction -Path "$stage/.venv" -Target "$root/.venv" | Out-Null
New-Item -ItemType Junction -Path "$stage/.tools" -Target "$root/.tools" | Out-Null
$lcms = 'src/camera_profile_studio/vendor/lcms2'
if (Test-Path "$root/$lcms/lcms2.dll") { Copy-Item "$root/$lcms/lcms2.dll" "$stage/$lcms/lcms2.dll" }
$sourceCache = "$stage/output/distribution/third-party-source"
New-Item -ItemType Directory -Force $sourceCache | Out-Null
# Read-only reuse of upstream archives; never link old release reports/output.
$manifest = Get-Content "$root/packaging/upstream-sources.json" -Raw | ConvertFrom-Json
foreach ($item in $manifest.archives) {
    $cached = "$root/output/distribution/third-party-source/$($item.filename)"
    if (Test-Path $cached) { Copy-Item $cached $sourceCache }
}
& $python "$root/packaging/release_workspace.py" check-stage $FreezeManifest $stage
if ($LASTEXITCODE -ne 0) { throw 'Staged sources differ from approved freeze' }
# Windows PowerShell 5.1 treats redirected native stderr as an error record.
# Preserve diagnostics without interpreting a warning as process failure.
$previousPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    $metadata = & $python -c "from importlib.metadata import version; print(version('camera-profile-studio'))" 2> "$release/editable-version-before.err.txt"
    $metadataExit = $LASTEXITCODE
} finally { $ErrorActionPreference = $previousPreference }
if ($metadataExit -ne 0) { throw 'Editable metadata probe failed' }
$metadata | Set-Content "$release/editable-version-before.txt"
if ($metadata -ne '0.2.6') {
    try {
        $ErrorActionPreference = 'Continue'
        & $python -m pip install --no-deps --no-build-isolation --no-index -e $root *> "$release/editable-refresh.log"
        $refreshExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($refreshExit -ne 0) { throw 'Local editable metadata refresh failed' }
}
try {
    $ErrorActionPreference = 'Continue'
    & $python -c "from importlib.metadata import version; v=version('camera-profile-studio'); print(v); assert v == '0.2.6'" *> "$release/editable-version-after.txt"
    $metadataExit = $LASTEXITCODE
} finally { $ErrorActionPreference = $previousPreference }
if ($metadataExit -ne 0) { throw 'Editable metadata version mismatch' }
$oldGuard = $env:CPS_ISOLATED_BUILD
$oldLocation = Get-Location
try {
    $env:CPS_ISOLATED_BUILD = $stage
    & "$stage/scripts/build-windows.ps1" -ReleaseVersion '0.2.6'
    & $python "$root/packaging/release_workspace.py" check-stage $FreezeManifest $stage
    if ($LASTEXITCODE -ne 0) { throw 'Staged inputs changed during compilation' }
    & $python "$root/packaging/release_workspace.py" check $FreezeManifest
    if ($LASTEXITCODE -ne 0) { throw 'Source changed during build; do not publish' }
    foreach ($name in @('CameraProfileStudio-0.2.6-Setup.exe','CameraProfileStudio-0.2.6-Portable.zip')) {
        Copy-Item "$stage/output/$name" "$release/$name"
    }
    Copy-Item "$stage/output/distribution/launcher.dist/CameraProfileStudio-0.2.6-Source.zip" "$release/CameraProfileStudio-0.2.6-Source.zip"
} finally {
    $env:CPS_ISOLATED_BUILD = $oldGuard
    Set-Location $oldLocation
    & $python "$root/packaging/release_workspace.py" immutable
    if ($LASTEXITCODE -ne 0) { throw 'Prior release immutability check failed' }
}
Write-Host "Built in isolated workspace: $stage. Native verification remains required."
