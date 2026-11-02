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
    'docs/IMAGE_LOOK_API.md','docs/BUILD_0.2.6.md','scripts/build-windows.ps1','scripts/build-lcms.ps1','scripts/build-common.ps1',
    'scripts/verify_distribution.ps1','scripts/verify_distribution_image_look.py','tests/test_gui_workflows.py')) {
    if (!(Test-Path "$root/$name" -PathType Leaf)) { throw "Fresh source dependency missing: $name" }
}
if ((Get-Content "$root/pyproject.toml" -Raw) -notmatch 'version = "0.2.6"') { throw 'Expected 0.2.6 source' }
if ($ValidateOnly) { Write-Output 'Fresh source dependencies and isolation guard: PASS (no build/test/install)'; return }
if (!$BuildApproved) { throw 'Explicit fresh build authorization required; no build started' }
. "$PSScriptRoot/build-common.ps1"
Set-CpsPhase 'collect-and-stage'
$python = Join-Path $root '.venv/Scripts/python.exe'
if (!(Test-Path $python)) { throw 'Create the pinned local .venv first; see docs/BUILD_0.2.6.md' }
$release = Join-Path $root 'output/releases/0.2.6'
$stage = [IO.Path]::GetFullPath((Join-Path $release 'workspace'))
$previous = @{guard=$env:CPS_ISOLATED_BUILD; pythonpath=$env:PYTHONPATH; lcms=$env:CPS_LCMS2_DLL; qt=$env:QT_QPA_PLATFORM; updates=$env:CPS_DISABLE_UPDATE_CHECK}
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
    # Tests and smoke runs must never reach the network or show the update bar.
    $env:CPS_DISABLE_UPDATE_CHECK='1'
    $runtime="$stage/output/distribution/lcms-runtime"
    $env:CPS_LCMS2_DLL="$runtime/lcms2.dll"
    Set-CpsPhase 'lcms'
    & "$stage/scripts/build-lcms.ps1" -OutputDirectory $runtime -WorkDirectory "$stage/output/distribution/lcms-build"
    Set-CpsPhase 'test-profiles-and-freeze'
    & $python "$stage/packaging/testing/acquire_profiles.py"
    if ($LASTEXITCODE -ne 0) { throw 'Pinned test profile acquisition failed' }
    foreach ($name in @('Nikon_D70s.dcp','Nikon_D200.icc','Sony_DSC-F828.icc','Olympus_E1.dcp')) {
        if (!(Test-Path "$stage/input/ccd_profiles/$name")) { throw "Missing required source-test profile: $name" }
    }
    # This is a build-input snapshot, NOT approval of the protected local release freeze.
    & $python "$stage/packaging/release_workspace.py" freeze "$release/fresh-build-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'Fresh input snapshot failed' }
    Set-CpsPhase 'tests'
    # The full suite runs once, as two pytest runs that together select every test: the non-GUI tests in parallel
    # (pytest-xdist; each worker is its own process) and the tests marked 'gui' serially in a single process. A .venv
    # without xdist (created before it joined the lock file) runs the whole suite serially, exactly as before.
    $ErrorActionPreference='Continue'
    & $python -c 'import xdist' 2>$null
    $hasXdist = ($LASTEXITCODE -eq 0)
    if ($hasXdist) {
        & $python -m pytest -m 'not gui' -n (Get-CpsTestWorkers) --dist worksteal --junitxml="$release/source-tests-parallel.xml" *> "$release/source-tests-parallel.log"
        $testExit=$LASTEXITCODE
        if ($testExit -eq 0) {
            & $python -m pytest -m gui --junitxml="$release/source-tests-gui.xml" *> "$release/source-tests-gui.log"
            $testExit=$LASTEXITCODE
        }
    } else {
        & $python -m pytest --junitxml="$release/source-tests.xml" *> "$release/source-tests.log"
        $testExit=$LASTEXITCODE
    }
    $ErrorActionPreference='Stop'
    if ($hasXdist) {
        # Keep the established names: one source-tests.log / source-tests.xml covering both runs.
        $parts = @('parallel', 'gui') | Where-Object { Test-Path -LiteralPath "$release/source-tests-$_.log" }
        $combined = foreach ($part in $parts) { "===== pytest run: $part ====="; Get-Content -LiteralPath "$release/source-tests-$part.log" }
        $combined | Set-Content -Encoding Unicode -LiteralPath "$release/source-tests.log"
        $merged = New-Object System.Xml.XmlDocument
        [void]$merged.AppendChild($merged.CreateXmlDeclaration('1.0', 'utf-8', $null))
        $suites = $merged.AppendChild($merged.CreateElement('testsuites'))
        foreach ($part in $parts) {
            if (!(Test-Path -LiteralPath "$release/source-tests-$part.xml")) { continue }
            $xml = New-Object System.Xml.XmlDocument
            $xml.Load("$release/source-tests-$part.xml")
            foreach ($suite in @($xml.SelectNodes('//testsuite'))) {
                $suite.SetAttribute('name', "pytest-$part")
                [void]$suites.AppendChild($merged.ImportNode($suite, $true))
            }
            $line = Select-String -LiteralPath "$release/source-tests-$part.log" -Pattern ' (passed|failed|error)' | Select-Object -Last 1
            if ($line) { Write-Output ("pytest ($part): " + $line.Line.Trim()) }
        }
        $merged.Save("$release/source-tests.xml")
    }
    if ($testExit -ne 0) { throw 'Full source test suite failed; see source-tests.log' }
    Set-CpsPhase 'build-windows'
    & "$stage/scripts/build-windows.ps1" -ReleaseVersion '0.2.6'
    Set-CpsPhase 'input-check'
    & $python "$stage/packaging/release_workspace.py" check "$release/fresh-build-inputs.json"
    if ($LASTEXITCODE -ne 0) { throw 'Fresh inputs changed during test/build' }
    Set-CpsPhase 'verify-distribution'
    & "$stage/scripts/verify_distribution.ps1" -ReleaseVersion '0.2.6' -HostedCi
    Set-CpsPhase 'copy-and-hash'
    foreach ($name in @('CameraProfileStudio-0.2.6-Setup.exe','CameraProfileStudio-0.2.6-Portable.zip')) {
        Copy-Item "$stage/output/$name" "$release/$name"
    }
    Copy-Item "$stage/output/distribution/launcher.dist/CameraProfileStudio-0.2.6-Source.zip" "$release/CameraProfileStudio-0.2.6-Source.zip"
    Get-FileHash "$release/CameraProfileStudio-0.2.6-Setup.exe","$release/CameraProfileStudio-0.2.6-Portable.zip","$release/CameraProfileStudio-0.2.6-Source.zip" |
        ForEach-Object { "$($_.Hash.ToLowerInvariant())  $(Split-Path $_.Path -Leaf)" } | Set-Content -Encoding ascii "$release/DELIVERY_SHA256SUMS.txt"
    Write-Output 'Fresh source build and hosted smoke complete; native desktop/Image Look Creator window acceptance remains UNVERIFIED.'
} finally {
    $ErrorActionPreference='Stop'
    if (Test-Path $release) { try { Save-CpsPhases "$release/phase-timings.json" } catch { } }
    $env:CPS_ISOLATED_BUILD=$previous.guard; $env:PYTHONPATH=$previous.pythonpath
    $env:CPS_LCMS2_DLL=$previous.lcms; $env:QT_QPA_PLATFORM=$previous.qt; $env:CPS_DISABLE_UPDATE_CHECK=$previous.updates
    Set-Location $location
}
