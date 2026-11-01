param([switch]$BuildApproved, [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
# This path is intentionally unavailable in the delivery worktree, even in validation mode.
if (Test-Path "$root/output") { throw 'Fresh-source build requires NO existing output tree; use the protected local release path or a new extraction' }
if ((Get-Item $root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Fresh source root must not be a junction' }
foreach ($tree in @('src','assets','packaging','scripts','docs','tests','licenses')) {
    if (Test-Path "$root/$tree") {
        $links = @(Get-ChildItem "$root/$tree" -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
        if (((Get-Item "$root/$tree").Attributes -band [IO.FileAttributes]::ReparsePoint) -or $links.Count) { throw "Source junctions are not allowed: $tree" }
    }
}
foreach ($name in @('pyproject.toml','requirements-lock.txt','packaging/collect_source.py','packaging/release_workspace.py',
    'packaging/testing/acquire_profiles.py','packaging/testing/PROFILE-SOURCES.txt','packaging/upstream-sources.json',
    'docs/IMAGE_LOOK_API.md','docs/BUILD_0.2.5.md','scripts/build-windows.ps1','scripts/build-lcms.ps1',
    'scripts/verify_distribution.ps1','scripts/verify_distribution_image_look.py','tests/test_gui_workflows.py')) {
    if (!(Test-Path "$root/$name" -PathType Leaf)) { throw "Fresh source dependency missing: $name" }
}
if ((Get-Content "$root/pyproject.toml" -Raw) -notmatch 'version = "0.2.5"') { throw 'Expected 0.2.5 source' }
if ($ValidateOnly) { Write-Output 'Fresh source dependencies and isolation guard: PASS (no build/test/install)'; return }
if (!$BuildApproved) { throw 'Explicit fresh build authorization required; no build started' }
$python = Join-Path $root '.venv/Scripts/python.exe'
if (!(Test-Path $python)) { throw 'Create the pinned local .venv first; see docs/BUILD_0.2.5.md' }
$release = Join-Path $root 'output/releases/0.2.5'
$stage = [IO.Path]::GetFullPath((Join-Path $release 'workspace'))
$previous = @{guard=$env:CPS_ISOLATED_BUILD; pythonpath=$env:PYTHONPATH; lcms=$env:CPS_LCMS2_DLL; qt=$env:QT_QPA_PLATFORM}
$location = Get-Location
try {
    # Exclusive output creation prevents a concurrent run from inheriting another build's state.
    New-Item -ItemType Directory -Path "$root/output" -ErrorAction Stop | Out-Null
    New-Item -ItemType Directory -Path $release | Out-Null
    Set-Location $root
    & $python packaging/collect_source.py "$release/staging-source.zip"
    if ($LASTEXITCODE -ne 0) { throw 'Fresh source collection failed' }
    Expand-Archive "$release/staging-source.zip" $stage
    New-Item -ItemType Junction -Path "$stage/.venv" -Target "$root/.venv" | Out-Null
    # No output or .tools link: all build products and tools are private to this fresh stage.
    '{"mode":"fresh-source","native_desktop_acceptance":false}' | Set-Content "$stage/.fresh-source-workspace.json"
    Set-Location $stage
    $env:CPS_ISOLATED_BUILD=$stage
    $env:PYTHONPATH="$stage/src"
    $env:QT_QPA_PLATFORM='offscreen'
    $runtime="$stage/output/distribution/lcms-runtime"
    $env:CPS_LCMS2_DLL="$runtime/lcms2.dll"
    & "$stage/scripts/build-lcms.ps1" -OutputDirectory $runtime -WorkDirectory "$stage/output/distribution/lcms-build"
    & $python "$stage/packaging/testing/acquire_profiles.py"
    if ($LASTEXITCODE -ne 0) { throw 'Pinned test profile acquisition failed' }
    foreach ($name in @('Nikon_D70s.dcp','Nikon_D200.icc','Sony_DSC-F828.icc','Olympus_E1.dcp')) {
        if (!(Test-Path "$stage/input/ccd_profiles/$name")) { throw "Missing required source-test profile: $name" }
    }
    # This is a build-input snapshot, NOT approval of the protected local release freeze.
    & $python "$stage/packaging/release_workspace.py" freeze "$release/fresh-build-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'Fresh input snapshot failed' }
    $ErrorActionPreference='Continue'
    & $python -m pytest --junitxml="$release/source-tests.xml" *> "$release/source-tests.log"
    $testExit=$LASTEXITCODE
    $ErrorActionPreference='Stop'
    if ($testExit -ne 0) { throw 'Full source test suite failed; see source-tests.log' }
    & "$stage/scripts/build-windows.ps1" -ReleaseVersion '0.2.5'
    & $python "$stage/packaging/release_workspace.py" check "$release/fresh-build-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'Fresh inputs changed during test/build' }
    & "$stage/scripts/verify_distribution.ps1" -ReleaseVersion '0.2.5' -HostedCi
    foreach ($name in @('CameraProfileStudio-0.2.5-Setup.exe','CameraProfileStudio-0.2.5-Portable.zip')) {
        Copy-Item "$stage/output/$name" "$release/$name"
    }
    Copy-Item "$stage/output/distribution/launcher.dist/CameraProfileStudio-0.2.5-Source.zip" "$release/CameraProfileStudio-0.2.5-Source.zip"
    Get-FileHash "$release/CameraProfileStudio-0.2.5-Setup.exe","$release/CameraProfileStudio-0.2.5-Portable.zip","$release/CameraProfileStudio-0.2.5-Source.zip" |
        ForEach-Object { "$($_.Hash.ToLowerInvariant())  $(Split-Path $_.Path -Leaf)" } | Set-Content -Encoding ascii "$release/DELIVERY_SHA256SUMS.txt"
    Write-Output 'Fresh source build and hosted smoke complete; native desktop/Image Look Creator window acceptance remains UNVERIFIED.'
} finally {
    $ErrorActionPreference='Stop'
    $env:CPS_ISOLATED_BUILD=$previous.guard; $env:PYTHONPATH=$previous.pythonpath
    $env:CPS_LCMS2_DLL=$previous.lcms; $env:QT_QPA_PLATFORM=$previous.qt
    Set-Location $location
}
