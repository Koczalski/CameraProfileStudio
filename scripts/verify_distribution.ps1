param([switch]$SkipInstaller, [switch]$SkipNativeGui, [ValidateSet('', '0.2.6')][string]$ReleaseVersion = '', [string]$ImageLookNavigationLabel = '', [switch]$HostedCi)
$ErrorActionPreference = 'Stop'
if ($ReleaseVersion -and ($SkipInstaller -or $SkipNativeGui)) {
    throw 'Skip switches are not allowed for the release path'
}
function Get-CpsInstallRegistrations {
    $keyPath = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B15FF861-C98A-4F3F-B46D-143C62935FA3}_is1'
    foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            $key = $null
            try {
                $key = $base.OpenSubKey($keyPath, $false)
                if ($key) { [pscustomobject]@{hive="$hive";view="$view";key=$keyPath;install_location=$key.GetValue('InstallLocation')} }
            } finally { if ($key) { $key.Dispose() }; $base.Dispose() }
        }
    }
}
$root = Split-Path $PSScriptRoot -Parent
if ($HostedCi) {
    $freshRoot = [IO.Path]::GetFullPath((Join-Path $root '../../../..'))
    if (Test-Path "$freshRoot/output/image-look-packaging/prep/immutable-baseline.json") {
        throw 'Hosted CI mode cannot bypass protected local release verification'
    }
    if ($ReleaseVersion -ne '0.2.6' -or $env:CPS_ISOLATED_BUILD -ne $root -or
        !(Test-Path "$root/.fresh-source-workspace.json") -or
        ($root -notlike '*/output/releases/0.2.6/workspace' -and $root -notlike '*\output\releases\0.2.6\workspace')) {
        throw 'Hosted CI smoke requires a fresh-source staged workspace'
    }
}
$stem = 'CameraProfileStudio'
if ($ReleaseVersion) {
    if (!$ImageLookNavigationLabel -and !$HostedCi) { throw 'Frontend-provided Image Look Creator navigation label is required' }
    $stem += "-$ReleaseVersion"
}
if ((Get-Content "$root/pyproject.toml" -Raw) -match 'version = "0.2.6"' -and (!$ReleaseVersion -or $env:CPS_ISOLATED_BUILD -ne $root)) {
    throw 'Use verify_distribution_release.ps1 for isolated 0.2.6 verification'
}
$out = Join-Path $root 'output/distribution'
$artifactHashes = Get-Content "$out/artifact-hashes.json" -Raw | ConvertFrom-Json
foreach ($artifact in $artifactHashes) {
    $path = Join-Path "$root/output" $artifact.file
    if ((Get-FileHash $path).Hash -ne $artifact.sha256 -or (Get-Item $path).Length -ne $artifact.bytes) {
        throw "Artifact hash/size mismatch before verification: $($artifact.file)"
    }
}
$sandbox = Join-Path $out ('sandbox-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $sandbox | Out-Null
Expand-Archive "$root/output/$stem-Portable.zip" "$sandbox/portable"
$exe = Join-Path $sandbox 'portable/launcher.dist/CameraProfileStudio.exe'
$oldPath = $env:PATH
$oldPythonPath = $env:PYTHONPATH
$oldPythonHome = $env:PYTHONHOME
$oldQt = $env:QT_QPA_PLATFORM
$oldUpdateCheck = $env:CPS_DISABLE_UPDATE_CHECK
$gateSpecs = @()
$qtEnvironment = @{}
foreach ($name in @('QTDIR','QT_PLUGIN_PATH','QTPLUGINPATH','QT_QPA_PLATFORM_PLUGIN_PATH','CPS_LCMS2_DLL')) {
    $qtEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$results = [ordered]@{ sandbox=$sandbox; artifacts=@($artifactHashes | ForEach-Object { @{file=$_.file;sha256=$_.sha256;bytes=$_.bytes} }); python_on_path=$false; network_isolated=$false; hosted_ci=[bool]$HostedCi; native_desktop_acceptance=$false; checks=@('final artifact SHA256 and sizes: PASS') }
try {
    # Isolate executable lookup from the developer environment. This is not a clean VM.
    $env:PATH = "$env:SystemRoot/System32;$env:SystemRoot"
    $env:PYTHONPATH = ''
    $env:PYTHONHOME = ''
    # Verification must never reach the network or show the update bar (inherited by every process started below).
    $env:CPS_DISABLE_UPDATE_CHECK = '1'
    foreach ($name in $qtEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    Push-Location $sandbox
    try {
        $p = Start-Process $exe -ArgumentList '--help' -PassThru -Wait -RedirectStandardOutput "$sandbox/help.txt" -RedirectStandardError "$sandbox/help.err.txt"
        if ($p.ExitCode -ne 0 -or !(Select-String -Path "$sandbox/help.txt" -Pattern 'usage|Camera' -Quiet)) { throw 'Packaged --help failed' }
        $results.checks += 'portable --help: PASS'
        # The packaged-CLI gates only read the payload and write distinct files into $sandbox, so they run side by side
        # (each with its own log) while the GUI liveness check and the installer test below proceed. Every gate is awaited
        # after those steps and any failure fails the verification before the evidence is written.
        $gateSpecs = @(@{ name = 'conversion'; script = 'verify_distribution_conversion.py'
                failure = 'Packaged ICC inspection / nonidentity conversion failed'
                check = 'generated ICC inspect and sRGB to linear-sRGB curve conversion: PASS' })
        if ($ReleaseVersion) {
            $gateSpecs += @{ name = 'image-look'; script = 'verify_distribution_image_look.py'
                failure = 'Packaged Image Look Creator feature gate failed'
                check = 'packaged image-look reference-only export: PASS' }
        }
        $gateSpecs += @{ name = 'features'; script = 'verify_distribution_features.py'
            failure = 'Packaged DCP look / SciPy matching feature gate failed'
            check = 'packaged D70s LookOnly and grouped SciPy ccm_tone_lut matching: PASS' }
        $venvPython = [IO.Path]::GetFullPath("$root/.venv/Scripts/python.exe")
        foreach ($spec in $gateSpecs) {
            $spec.log = Join-Path $sandbox "gate-$($spec.name).log"
            $spec.errorLog = Join-Path $sandbox "gate-$($spec.name).err.txt"
            $spec.process = Start-Process -FilePath $venvPython -ArgumentList ('"{0}" "{1}" "{2}"' -f "$root/scripts/$($spec.script)", $exe, $sandbox) `
                -WorkingDirectory $sandbox -NoNewWindow -PassThru -RedirectStandardOutput $spec.log -RedirectStandardError $spec.errorLog
            # Windows PowerShell 5.1 needs the handle cached before the process exits to read ExitCode.
            try { $null = $spec.process.Handle } catch { }
        }
        $env:QT_QPA_PLATFORM = 'offscreen'
        $p = Start-Process $exe -PassThru -RedirectStandardOutput "$sandbox/gui.txt" -RedirectStandardError "$sandbox/gui.err.txt"
        Start-Sleep -Seconds 8
        if ($p.HasExited) { throw "GUI exited early: $($p.ExitCode)" }
        Stop-Process -Id $p.Id
        $results.checks += 'portable GUI stays alive for 8 seconds (offscreen): PASS'
        if (!$SkipNativeGui -and !$HostedCi) {
            & "$root/scripts/verify_distribution_window.ps1" -Executable $exe -EvidenceDirectory $sandbox -NavigationCount $(if ($ReleaseVersion) { 7 } else { 2 }) -RequiredWorkspaceLabel $ImageLookNavigationLabel
            $results.checks += 'native Windows GUI window-only capture and tab selection: PASS (visual PNG review still required)'
        } else {
            $results.checks += 'native Windows GUI: SKIPPED, not acceptance evidence'
        }
        if (!$SkipInstaller) {
            $existing = @(Get-CpsInstallRegistrations)
            ConvertTo-Json -InputObject $existing -Depth 4 | Set-Content -Encoding utf8 "$sandbox/installer-registry-before.json"
            $installerSmoke = $true
            if ($existing.Count -gt 0) {
                if (!$HostedCi) {
                    throw 'Installer safety block: AppId is already registered. No install/uninstall attempted; inspect exact-key evidence before proceeding.'
                }
                # Fresh-source (hosted) build on a PC that has Camera Profile Studio installed. The test installer shares the
                # AppId, so installing it would overwrite the registration of the installed copy and the test uninstall would
                # delete it. The installed copy is therefore left completely untouched and only this test is skipped; the
                # skip is recorded as such and is not acceptance evidence. The protected release route above still blocks.
                $installerSmoke = $false
                Write-Warning ('Installer test skipped: Camera Profile Studio is installed ({0}) and is left untouched.' -f (($existing | ForEach-Object { $_.install_location }) -join '; '))
                $results.installer_smoke = 'SKIPPED: an installed copy is registered and was left untouched'
                $results.checks += 'installer silent install/uninstall: SKIPPED (installed copy present, left untouched), not acceptance evidence'
                $skipRoot = Split-Path $exe -Parent
                $skipLongest = (Get-ChildItem $skipRoot -File -Recurse | ForEach-Object { $_.FullName.Substring($skipRoot.Length+1).Length } | Measure-Object -Maximum).Maximum
                $skipDefaultMaximum = (Join-Path $env:LOCALAPPDATA 'Programs/CameraProfileStudio').Length + 1 + $skipLongest
                @{installer_run='skipped'; longest_relative_payload_path=$skipLongest; default_user_install_maximum=$skipDefaultMaximum} |
                    ConvertTo-Json | Set-Content -Encoding utf8 "$sandbox/installer-path-budget.json"
                if ($skipDefaultMaximum -ge 250) { Write-Warning "Default per-user install path budget is tight: $skipDefaultMaximum characters." }
            }
            if ($installerSmoke) {
                # Silent install into the sandbox directory only: /DIR overrides the previous-folder logic, /TASKS="" creates no
                # shortcuts, and /CPSRELAUNCH=1 (the in-app updater's flag that starts the new version) is deliberately NOT passed.
                $results.checks += 'installer exact AppId registry preflight (HKCU/HKLM, 32/64-bit): no existing registration'
                $install = Join-Path $sandbox 'installed'
                if ($ReleaseVersion) {
                    # Keep Inno 6.0.5 below MAX_PATH without changing the shipped payload layout.
                    $install = Join-Path (Split-Path $root -Parent) ('install-' + [guid]::NewGuid().ToString('N').Substring(0,12))
                }
                if (Test-Path $install) { throw 'Installer safety block: test destination already exists' }
                $payloadRoot = Split-Path $exe -Parent
                $longestRelative = (Get-ChildItem $payloadRoot -File -Recurse | ForEach-Object { $_.FullName.Substring($payloadRoot.Length+1).Length } | Measure-Object -Maximum).Maximum
                $pathBudget = @{install_directory=$install; longest_relative_payload_path=$longestRelative;
                    maximum_destination_length=($install.Length+1+$longestRelative);
                    previous_deep_sandbox_maximum=((Join-Path $sandbox 'installed').Length+1+$longestRelative);
                    default_user_install_maximum=((Join-Path $env:LOCALAPPDATA 'Programs/CameraProfileStudio').Length+1+$longestRelative)}
                $pathBudget | ConvertTo-Json | Set-Content -Encoding utf8 "$sandbox/installer-path-budget.json"
                if ($pathBudget.maximum_destination_length -ge 250) { throw 'Test installation path leaves insufficient Win32 path budget' }
                New-Item -ItemType Directory -Path $install -ErrorAction Stop | Out-Null
                $results.install_directory=$install
                $p = Start-Process "$root/output/$stem-Setup.exe" -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-', '/TASKS=""', "/DIR=`"$install`"", "/LOG=`"$sandbox/install.log`"") -PassThru -Wait
                $results.installer_exit_code=$p.ExitCode
                if ($p.ExitCode -ne 0 -or !(Test-Path "$install/CameraProfileStudio.exe")) { throw 'Per-user install failed' }
                $p = Start-Process "$install/CameraProfileStudio.exe" -ArgumentList '--help' -PassThru -Wait -RedirectStandardOutput "$sandbox/installed-help.txt" -RedirectStandardError "$sandbox/installed-help.err.txt"
                if ($p.ExitCode -ne 0) { throw 'Installed executable failed' }
                $results.checks += 'per-user silent install and installed --help: PASS'
                $previousFailureFile='licenses/third-party/opencv-python-headless/cv2/data/haarcascade_license_plate_rus_16stages.xml'
                $installedNotice=Join-Path $install $previousFailureFile
                if ((Get-FileHash $installedNotice).Hash -ne (Get-FileHash (Join-Path $payloadRoot $previousFailureFile)).Hash) {
                    throw 'Installed long-name notice differs from the unmodified payload'
                }
                @{path=$installedNotice;characters=$installedNotice.Length;sha256=(Get-FileHash $installedNotice).Hash;
                    portable_payload_sha256=(Get-FileHash (Join-Path $payloadRoot $previousFailureFile)).Hash} |
                    ConvertTo-Json | Set-Content -Encoding utf8 "$sandbox/installer-long-name-file-proof.json"
                $p = Start-Process "$install/unins000.exe" -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=`"$sandbox/uninstall.log`"") -PassThru -Wait
                $results.uninstaller_exit_code=$p.ExitCode
                if ($p.ExitCode -ne 0 -or (Test-Path "$install/CameraProfileStudio.exe")) { throw 'Uninstall failed' }
                $remaining = @(Get-CpsInstallRegistrations)
                ConvertTo-Json -InputObject $remaining -Depth 4 | Set-Content -Encoding utf8 "$sandbox/installer-registry-after.json"
                if ($remaining.Count -gt 0) { throw 'Uninstall left AppId registration behind; do not claim cleanup success' }
                $results.checks += 'per-user silent uninstall and AppId registration removed: PASS'
            }
        }
        foreach ($spec in $gateSpecs) {
            $spec.process.WaitForExit()
            Write-Output "== packaged gate: $($spec.name) =="
            Get-Content -LiteralPath $spec.log | Write-Output
            Get-Content -LiteralPath $spec.errorLog | Write-Output
            if ($spec.process.ExitCode -ne 0) { throw $spec.failure }
            $results.checks += $spec.check
        }
    } finally { Pop-Location }
} finally {
    foreach ($spec in $gateSpecs) {
        if ($spec.process -and -not $spec.process.HasExited) { Stop-Process -Id $spec.process.Id -Force -ErrorAction SilentlyContinue }
    }
    $env:PATH=$oldPath; $env:PYTHONPATH=$oldPythonPath; $env:PYTHONHOME=$oldPythonHome; $env:QT_QPA_PLATFORM=$oldQt; $env:CPS_DISABLE_UPDATE_CHECK=$oldUpdateCheck
    foreach ($name in $qtEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $qtEnvironment[$name], 'Process') }
    $results | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 "$out/smoke-results.json"
}
& "$root/.venv/Scripts/python.exe" "$root/packaging/curate_smoke.py"
if ($LASTEXITCODE -ne 0) { throw 'Smoke evidence curation failed' }
Write-Host "Smoke logs: $sandbox; curated review copy: $out/final-smoke"
