param([switch]$BuildApproved, [string]$ImageLookNavigationLabel = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv/Scripts/python.exe'
$release = Join-Path $root 'output/releases/0.2.7'
$stage = [IO.Path]::GetFullPath((Join-Path $release 'workspace'))
if (!$ImageLookNavigationLabel) { throw 'Supply the frontend-confirmed exact navigation label' }
if (!$BuildApproved) { throw 'Explicit build/smoke authorization required; no runtime started' }
& $python "$root/packaging/release_workspace.py" check "$release/approved-source-freeze.json"
if ($LASTEXITCODE -ne 0) { throw 'Source changed since approved freeze' }
foreach ($name in @('CameraProfileStudio-0.2.7-Setup.exe','CameraProfileStudio-0.2.7-Portable.zip')) {
    if ((Get-FileHash "$release/$name").Hash -ne (Get-FileHash "$stage/output/$name").Hash) { throw "Published artifact differs: $name" }
}
# This one original fixture is test-only and is never collected by the source packer.
New-Item -ItemType Directory -Force "$stage/input/ccd_profiles" | Out-Null
Copy-Item "$root/input/ccd_profiles/Nikon_D70s.dcp" "$stage/input/ccd_profiles/Nikon_D70s.dcp"
$oldGuard = $env:CPS_ISOLATED_BUILD
try {
    $env:CPS_ISOLATED_BUILD = $stage
    & "$stage/scripts/verify_distribution.ps1" -ReleaseVersion '0.2.7' -ImageLookNavigationLabel $ImageLookNavigationLabel
    & $python "$root/packaging/release_workspace.py" check "$release/approved-source-freeze.json"
    if ($LASTEXITCODE -ne 0) { throw 'Source changed during verification' }
    New-Item -ItemType Directory -Force "$release/distribution" | Out-Null
    Copy-Item "$stage/output/distribution/final-smoke" "$release/distribution/" -Recurse
    Copy-Item "$stage/output/distribution/smoke-results.json","$stage/output/distribution/artifact-hashes.json" "$release/distribution/"
    Get-FileHash "$release/CameraProfileStudio-0.2.7-Setup.exe","$release/CameraProfileStudio-0.2.7-Portable.zip","$release/CameraProfileStudio-0.2.7-Source.zip" | ForEach-Object { "$($_.Hash.ToLowerInvariant())  $(Split-Path $_.Path -Leaf)" } | Set-Content -Encoding ascii "$release/DELIVERY_SHA256SUMS.txt"
} finally {
    $env:CPS_ISOLATED_BUILD = $oldGuard
    & $python "$root/packaging/release_workspace.py" immutable
    if ($LASTEXITCODE -ne 0) { throw 'Prior release immutability check failed' }
}
Write-Host "Fresh native verification: $release/distribution. Window PNG still requires human visual review."
