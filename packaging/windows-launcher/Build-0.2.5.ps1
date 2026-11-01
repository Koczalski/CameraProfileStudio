#requires -Version 5.1
<#
.SYNOPSIS
    Camera Profile Studio 0.2.5 を Windows でビルドします（Setup.exe / Portable.zip / Source.zip）。

.DESCRIPTION
    同梱ソースの公式手順（docs/BUILD_0.2.5.md）を、新しい短いビルドフォルダーで順に実行します。
      1. 事前チェック（PC の設定は変更しません）
      2. ソースを新しいビルドフォルダーへ複製（元のソースフォルダーは変更しません）
      3. scripts/build-fresh-source.ps1 -ValidateOnly
      4. .venv の作成と、固定版の依存パッケージのインストール
      5. scripts/build-fresh-source.ps1 -BuildApproved
         （全テスト → LittleCMS → Nuitka → Inno Setup → 配布物の検査）
      6. 成果物を release-0.2.5 フォルダーへコピーし、SHA-256 を照合

.PARAMETER Python
    使う python.exe（3.12 x64）。省略時は py ランチャーと標準のインストール先から探します。
.PARAMETER BuildRoot
    ビルドフォルダーを作る親フォルダー。省略時はこのスクリプトと同じ場所の "b"。
    NTFS の短いパス（全体で 64 文字以内が目安）にしてください。
.PARAMETER CheckOnly
    事前チェックだけを行います。
.PARAMETER Yes
    開始前の確認を省略します。
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$BuildRoot = '',
    [switch]$CheckOnly,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 の進捗表示は Invoke-WebRequest を大幅に遅くするため、子プロセスでも止める。
$ProgressPreference = 'SilentlyContinue'

$Version = '0.2.5'
$AppIdKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B15FF861-C98A-4F3F-B46D-143C62935FA3}_is1'
$Kit = $PSScriptRoot
$Source = Join-Path $Kit "CameraProfileStudio-$Version-Source"
# リポジトリ内（packaging\windows-launcher）から実行した場合は、リポジトリのルートをソースにする。
$RepoMode = $false
if (-not (Test-Path -LiteralPath (Join-Path $Source 'pyproject.toml'))) {
    $repoRoot = [IO.Path]::GetFullPath((Join-Path $Kit '..\..'))
    if ((Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml')) -and
        (Test-Path -LiteralPath (Join-Path $repoRoot 'src\camera_profile_studio'))) {
        $Source = $repoRoot
        $RepoMode = $true
    }
}
if (-not $BuildRoot) { $BuildRoot = Join-Path $Kit 'b' }
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$Stamp = Get-Date -Format 'MMdd-HHmm'
if (Test-Path -LiteralPath (Join-Path $BuildRoot $Stamp)) { $Stamp = Get-Date -Format 'MMdd-HHmmss' }
$Work = Join-Path $BuildRoot $Stamp
$LogDir = Join-Path (Join-Path $Kit 'build-logs') $Stamp
$PsExe = (Get-Process -Id $PID).Path
$Release = $null

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
try { Start-Transcript -LiteralPath (Join-Path $LogDir 'console.log') | Out-Null } catch { }

$Summary = [ordered]@{
    product = 'Camera Profile Studio'; version = $Version; status = 'running'
    started = (Get-Date).ToString('s'); finished = $null
    kit = $Kit; source = $Source; work = $Work; logs = $LogDir
    powershell = $PSVersionTable.PSVersion.ToString(); powershell_exe = $PsExe
    python = $null; checks = @(); steps = @(); failed_step = $null; error = $null
    release = $null; artifacts = @(); exe = $null
}

function Save-Summary {
    $Summary.finished = (Get-Date).ToString('s')
    try {
        $Summary | ConvertTo-Json -Depth 6 |
            Set-Content -LiteralPath (Join-Path $LogDir 'summary.json') -Encoding UTF8
    } catch { }
}

function Write-Section([string]$Text) {
    Write-Host ''
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Format-Duration([int]$Seconds) {
    if ($Seconds -lt 60) { return "$Seconds 秒" }
    return ('{0} 分 {1:00} 秒' -f [math]::Floor($Seconds / 60), ($Seconds % 60))
}

# ---------------------------------------------------------------- 実行の補助

function Invoke-Logged {
    <# ネイティブコマンドを実行し、出力を画面とログ（UTF-8）の両方へ流す。 #>
    param([string]$Exe, [string[]]$Arguments, [string]$LogName, [int[]]$SuccessCodes = @(0))
    $log = Join-Path $LogDir $LogName
    $writer = New-Object System.IO.StreamWriter($log, $true, (New-Object System.Text.UTF8Encoding($false)))
    $previous = $ErrorActionPreference
    # 5.1 では stderr の行がエラーレコードになるため、終了コードで成否を判定する。
    $ErrorActionPreference = 'Continue'
    # コマンドが起動できなかった場合に、前回の終了コードで成功と誤判定しないようにする。
    $global:LASTEXITCODE = -1
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            $writer.WriteLine($line)
            $writer.Flush()
            Write-Host "   $line"
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        $writer.Dispose()
    }
    if ($SuccessCodes -notcontains $code) { throw "終了コード $code（ログ: $log）" }
}

$PhaseNames = @{
    'source-tests.log' = '全テスト'; 'bootstrap.log' = 'ツールと上流ソースの準備'; 'licenses.log' = 'ライセンスの収集'
    'nuitka-build.log' = 'EXE の生成（Nuitka。いちばん時間がかかります）'; 'installer-build.log' = 'インストーラーの生成（Inno Setup）'
    'lcms-rebuild.log' = 'LittleCMS のビルド'
}

function Get-LatestLog {
    $rel = Join-Path $Work "output\releases\$Version"
    $dist = Join-Path $rel 'workspace\output\distribution'
    $files = @()
    foreach ($dir in @($rel, $dist, (Join-Path $dist 'lcms-build'))) {
        if (Test-Path -LiteralPath $dir) {
            $files += @(Get-ChildItem -LiteralPath $dir -File -Recurse:($dir -like '*lcms-build') -Filter '*.log' -ErrorAction SilentlyContinue)
        }
    }
    return ($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}

function Write-NewLines([string]$Path, [long]$Position) {
    <# 子プロセスが書いている途中のログから、増えた分だけを画面に出す。 #>
    if (-not (Test-Path -LiteralPath $Path)) { return $Position }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $length = $stream.Length
        if ($length -le $Position) { return $Position }
        [void]$stream.Seek($Position, [IO.SeekOrigin]::Begin)
        $reader = New-Object System.IO.StreamReader($stream, [Text.Encoding]::Default)
        foreach ($line in ($reader.ReadToEnd() -split "`r?`n")) { if ($line) { Write-Host "   $line" } }
        return $length
    } finally {
        $stream.Dispose()
    }
}

function Invoke-ProjectScript([string]$Script, [string]$Switch, [string]$LogName) {
    <#
      同梱スクリプトを別の PowerShell プロセスで実行する（環境変数や場所の変更を持ち込まない）。
      出力を画面へ流しつつ、無音の時間が長い段階（Nuitka など）でも 1 分ごとに経過と現在の段階を表示する。
    #>
    $quoted = $Script.Replace("'", "''")
    $command = "`$ProgressPreference = 'SilentlyContinue'; " +
        "try { & '$quoted' -$Switch *>&1 | ForEach-Object { `"`$_`" } } catch { Write-Output ('ERROR: ' + `$_.Exception.Message); " +
        "Write-Output `$_.InvocationInfo.PositionMessage; exit 1 }; exit 0"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $log = Join-Path $LogDir $LogName
    $errorLog = Join-Path $LogDir ($LogName + '.stderr.txt')
    $process = Start-Process -FilePath $PsExe -NoNewWindow -PassThru `
        -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-OutputFormat', 'Text', '-EncodedCommand', $encoded) `
        -RedirectStandardOutput $log -RedirectStandardError $errorLog
    # Windows PowerShell 5.1 では、ハンドルを先に取得しておかないと終了コードを読めないことがある。
    try { $null = $process.Handle } catch { }
    $started = Get-Date
    $beat = $started
    $position = 0
    while (-not $process.HasExited) {
        Start-Sleep -Seconds 5
        $position = Write-NewLines $log $position
        if (((Get-Date) - $beat).TotalSeconds -ge 60) {
            $beat = Get-Date
            $text = '   … 実行中 ' + (Format-Duration ([int]($beat - $started).TotalSeconds))
            $latest = Get-LatestLog
            if ($latest) {
                $phase = $PhaseNames[$latest.Name]
                if (-not $phase) { $phase = $latest.Name }
                $text += "　現在: $phase（$($latest.Name) を $($latest.LastWriteTime.ToString('HH:mm')) に更新）"
            }
            Write-Host $text
        }
    }
    $process.WaitForExit()
    $position = Write-NewLines $log $position
    $code = $process.ExitCode
    if ((Test-Path -LiteralPath $errorLog) -and (Get-Item -LiteralPath $errorLog).Length -gt 0) {
        Get-Content -LiteralPath $errorLog | ForEach-Object { Write-Host "   $_" }
    }
    if ($code -ne 0) {
        # 同梱スクリプトが出した理由（ERROR: 行）を、そのまま失敗の内容として示す。
        $reason = Select-String -LiteralPath $log -Pattern '^ERROR: (.+)$' -ErrorAction SilentlyContinue | Select-Object -Last 1
        if ($reason) { throw ("{0}（ログ: {1}）" -f $reason.Matches[0].Groups[1].Value.Trim(), $log) }
        throw "終了コード $code（ログ: $log）"
    }
}

function Invoke-Step([string]$Name, [scriptblock]$Action) {
    $started = Get-Date
    Write-Section $Name
    $entry = [ordered]@{ name = $Name; status = 'running'; started = $started.ToString('s'); seconds = $null }
    $Summary.steps += $entry
    Save-Summary
    try {
        & $Action
        $entry.status = 'ok'
    } catch {
        $entry.status = 'failed'
        $Summary.failed_step = $Name
        throw
    } finally {
        $entry.seconds = [int]((Get-Date) - $started).TotalSeconds
        Save-Summary
    }
    Write-Host ("   完了（{0}）" -f (Format-Duration $entry.seconds)) -ForegroundColor Green
}

# ---------------------------------------------------------------- 事前チェック

$Checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Name, [string]$Status, [string]$Detail, [string]$Fix = '') {
    $Checks.Add([pscustomobject]@{ name = $Name; status = $Status; detail = $Detail; fix = $Fix })
}

function Get-PythonInfo([string]$Exe, [string[]]$Prefix) {
    # 引数に空白や二重引用符を含めない（5.1 のネイティブ引数の引用符処理を避ける）。
    $code = "import sys,struct,venv,ensurepip;print(sys.version.split()[0]);print(struct.calcsize('P')*8);print(sys.executable)"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = @(& $Exe @Prefix -c $code 2>$null)
        $rc = $LASTEXITCODE
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($rc -ne 0 -or $out.Count -lt 3) { return $null }
    try { $ver = [version]($out[0].Trim()) } catch { return $null }
    return [pscustomobject]@{ version = $ver; bits = [int]($out[1].Trim()); exe = $out[2].Trim() }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[object]
    if ($Python) {
        $candidates.Add(@{ exe = $Python; prefix = @() })
    } else {
        $paths = @()
        # py ランチャーは一覧（-0p）だけを使う。「py -3.12」は未導入時に自動インストールを始める版があるため使わない。
        if (Get-Command py.exe -ErrorAction SilentlyContinue) {
            $previous = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try { $listing = @(& py.exe -0p 2>$null) } catch { $listing = @() } finally { $ErrorActionPreference = $previous }
            foreach ($l in $listing) {
                if ("$l" -match '3\.12' -and "$l" -match '([A-Za-z]:\\[^*?"<>|]*?python\.exe)') { $paths += $matches[1] }
            }
        }
        $paths += @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")
        $uv = Join-Path $env:APPDATA 'uv\python'
        if (Test-Path -LiteralPath $uv) {
            $paths += @(Get-ChildItem -LiteralPath $uv -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'cpython-3.12*-windows-x86_64*' } |
                ForEach-Object { Join-Path $_.FullName 'python.exe' })
        }
        try { $paths += @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }) } catch { }
        # Microsoft Store 版とその起動エイリアス（WindowsApps）は対象外。実行すると Store が開くことがある。
        foreach ($p in $paths) {
            if ($p -and $p -notlike '*\WindowsApps\*' -and (Test-Path -LiteralPath $p)) { $candidates.Add(@{ exe = $p; prefix = @() }) }
        }
    }
    $found = @()
    foreach ($c in $candidates) {
        $info = Get-PythonInfo $c.exe $c.prefix
        if ($info -and $info.version.Major -eq 3 -and $info.version.Minor -eq 12 -and $info.bits -eq 64 -and
            $info.exe -notlike '*\WindowsApps\*') { $found += $info }
    }
    if (-not $found) { return $null }
    $exact = @($found | Where-Object { $_.version -eq [version]'3.12.15' })
    if ($exact) { return $exact[0] }
    return @($found | Sort-Object version -Descending)[0]
}

function Get-CpsRegistrations {
    foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            $key = $null
            try {
                $key = $base.OpenSubKey($AppIdKey, $false)
                if ($key) {
                    [pscustomobject]@{ hive = "$hive"; view = "$view"
                        version = $key.GetValue('DisplayVersion'); location = $key.GetValue('InstallLocation') }
                }
            } finally {
                if ($key) { $key.Dispose() }
                $base.Dispose()
            }
        }
    }
}

function Test-Url([string]$Url) {
    try {
        Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -TimeoutSec 20 | Out-Null
        return $true
    } catch {
        # 4xx/5xx でも応答があれば到達はできている。
        return [bool]($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response)
    }
}

function Invoke-Preflight {
    $vsFix = 'winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"'

    # Windows
    if ($env:OS -eq 'Windows_NT' -and [Environment]::Is64BitOperatingSystem) {
        Add-Check 'Windows' 'OK' ([Environment]::OSVersion.VersionString + ' (x64)')
    } else {
        Add-Check 'Windows' 'FAIL' '64 ビット版 Windows が必要です' 'Windows 10/11 x64 で実行してください'
    }

    # PowerShell
    Add-Check 'PowerShell' 'OK' ("{0} {1}（0.2.0 は Windows PowerShell 5.1 で作成）" -f $PSVersionTable.PSEdition, $PSVersionTable.PSVersion)

    # ソース
    $pyproject = Join-Path $Source 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $pyproject)) {
        Add-Check 'ソース' 'FAIL' "見つかりません: $Source" "CameraProfileStudio-$Version-Source フォルダーをこのスクリプトと同じ場所に置いてください"
    } elseif ((Get-Content -LiteralPath $pyproject -Raw) -notmatch "version = `"$Version`"") {
        Add-Check 'ソース' 'FAIL' "pyproject.toml のバージョンが $Version ではありません" "$Version のソースを置いてください"
    } elseif ((-not $RepoMode) -and (Test-Path -LiteralPath (Join-Path $Source 'output'))) {
        Add-Check 'ソース' 'FAIL' 'ソースに output フォルダーがあります（新規ビルドの条件を満たしません）' '新しく展開した Source を使ってください'
    } else {
        if ($RepoMode) { Add-Check 'ソース' 'OK' "$Source（$Version、リポジトリ。.git・output・ビルド成果物は複製しません）" }
        else { Add-Check 'ソース' 'OK' "$Source（$Version）" }
    }

    # Python
    $py = Find-Python
    if ($py) {
        $script:PyExe = $py.exe
        $Summary.python = "$($py.version) $($py.exe)"
        if ($py.version -eq [version]'3.12.15') {
            Add-Check 'Python' 'OK' "$($py.version) x64  $($py.exe)"
        } else {
            Add-Check 'Python' 'WARN' "$($py.version) x64  $($py.exe)（ビルドの要件は 3.12 系。0.2.0 の記録は 3.12.15）" '通常はこのまま進めて問題ありません'
        }
    } else {
        $what = '3.12 x64 が見つかりません'
        if ($Python) { $what = "指定の Python が 3.12 x64 ではないか、実行できません: $Python" }
        Add-Check 'Python' 'FAIL' $what 'winget install -e --id Python.Python.3.12 （または -Python で python.exe を指定）'
    }

    # Visual Studio C++ (MSVC)
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = $null
    if (Test-Path -LiteralPath $vswhere) {
        $vs = @(& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath) | Select-Object -First 1
    }
    if ($vs -and (Test-Path -LiteralPath (Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'))) {
        $vsVersion = @(& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationVersion) | Select-Object -First 1
        $msbuild = (Test-Path -LiteralPath (Join-Path $vs 'MSBuild\Current\Bin\MSBuild.exe'))
        if (-not $msbuild) {
            Add-Check 'MSVC C++ x64' 'FAIL' "MSBuild が見つかりません: $vs" $vsFix
        } elseif ("$vsVersion" -like '17.*') {
            Add-Check 'MSVC C++ x64' 'OK' "Visual Studio $vsVersion  $vs"
        } else {
            Add-Check 'MSVC C++ x64' 'WARN' "Visual Studio $vsVersion  $vs（検証済みは 2022 = 17.x）" 'Visual Studio 2022 Build Tools の併用を推奨'
        }
    } else {
        Add-Check 'MSVC C++ x64' 'FAIL' 'Visual Studio の C++ x64 ビルドツールが見つかりません' $vsFix
    }

    # Windows SDK
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Include'
    $sdk = $null
    if (Test-Path -LiteralPath $kits) {
        $sdk = @(Get-ChildItem -LiteralPath $kits -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^10\.\d+\.\d+\.\d+$' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'um\Windows.h')) } |
            Sort-Object { [version]$_.Name } -Descending) | Select-Object -First 1
    }
    if ($sdk) { Add-Check 'Windows SDK' 'OK' $sdk.Name }
    else { Add-Check 'Windows SDK' 'FAIL' '見つかりません' "$vsFix （Windows 11 SDK も入ります）" }

    # ビルド先（ファイルシステム・空き容量・パス長）
    try {
        $drive = New-Object System.IO.DriveInfo([IO.Path]::GetPathRoot($BuildRoot))
        $fs = $drive.DriveFormat
        $free = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
        if ($fs -notin @('NTFS', 'ReFS')) {
            Add-Check 'ビルド先' 'FAIL' "$($drive.Name) は $fs です（ジャンクションを作れません）" '-BuildRoot C:\cpsb のように NTFS のドライブを指定してください'
        } elseif ($free -lt 10) {
            Add-Check 'ビルド先' 'FAIL' "$($drive.Name) $fs、空き $free GB（10 GB 以上必要）" '空きを増やすか、-BuildRoot で別ドライブを指定してください'
        } elseif ($free -lt 20) {
            Add-Check 'ビルド先' 'WARN' "$($drive.Name) $fs、空き $free GB（20 GB 以上を推奨）" '足りなくなった場合は -BuildRoot で別ドライブを指定してください'
        } else {
            Add-Check 'ビルド先' 'OK' "$($drive.Name) $fs、空き $free GB"
        }
    } catch {
        Add-Check 'ビルド先' 'FAIL' "ドライブを確認できません: $BuildRoot" 'ローカルドライブのフォルダーを -BuildRoot で指定してください'
    }
    $len = $Work.Length
    if ($len -le 64) { Add-Check 'パスの長さ' 'OK' "$Work（$len 文字）" }
    elseif ($len -le 80) { Add-Check 'パスの長さ' 'WARN' "$Work（$len 文字。64 文字以内を推奨）" '-BuildRoot C:\cpsb のように短い場所を指定できます' }
    else { Add-Check 'パスの長さ' 'FAIL' "$Work（$len 文字。Windows の 260 文字制限に掛かるおそれ）" '-BuildRoot C:\cpsb のように短い場所を指定してください' }

    # インストール済みの Camera Profile Studio（配布物の検査がインストーラーを試すため）
    try { $registered = @(Get-CpsRegistrations) } catch { $registered = $null }
    if ($null -eq $registered) {
        Add-Check 'インストール版' 'FAIL' 'レジストリを確認できません' 'Windows の通常のユーザーで実行してください'
    } elseif ($registered.Count) {
        $r = $registered[0]
        Add-Check 'インストール版' 'FAIL' ("Camera Profile Studio {0} がインストール済みです（{1}）。検査のインストーラー試験が安全のため止まります" -f $r.version, $r.location) '設定 > アプリ からアンインストールしてから実行してください（ポータブル版のフォルダーには影響しません）'
    } else {
        Add-Check 'インストール版' 'OK' 'インストール済みの Camera Profile Studio はありません'
    }

    # ネットワーク
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
    $required = @('https://pypi.org/simple/pip/', 'https://files.pythonhosted.org/', 'https://github.com/')
    $optional = @('https://codeload.github.com/', 'https://raw.githubusercontent.com/', 'https://download.qt.io/')
    $missRequired = @($required | Where-Object { -not (Test-Url $_) })
    $missOptional = @($optional | Where-Object { -not (Test-Url $_) })
    if ($missRequired.Count) {
        Add-Check 'ネットワーク' 'FAIL' ('接続できません: ' + ($missRequired -join ', ')) 'インターネット接続・プロキシ・ファイアウォールを確認してください'
    } elseif ($missOptional.Count) {
        Add-Check 'ネットワーク' 'WARN' ('確認できませんでした: ' + ($missOptional -join ', ')) 'ダウンロードの段階で失敗する場合は接続を確認してください'
    } else {
        Add-Check 'ネットワーク' 'OK' 'PyPI・GitHub・Qt に接続できます'
    }
}

function Show-Checks {
    $colors = @{ OK = 'Green'; WARN = 'Yellow'; FAIL = 'Red' }
    foreach ($c in $Checks) {
        Write-Host ('  [{0,-4}] ' -f $c.status) -ForegroundColor $colors[$c.status] -NoNewline
        Write-Host ('{0}: {1}' -f $c.name, $c.detail)
        if ($c.fix -and $c.status -ne 'OK') { Write-Host "          対処: $($c.fix)" -ForegroundColor $colors[$c.status] }
    }
}

function Save-ProjectLogs {
    <# ビルドフォルダーの主要ログを build-logs へ写す（ビルド先が別ドライブでも後から読めるように）。 #>
    $rel = Join-Path $Work "output\releases\$Version"
    $dist = Join-Path $rel 'workspace\output\distribution'
    $target = Join-Path $LogDir 'project'
    try {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        foreach ($folder in @($rel, $dist)) {
            if (Test-Path -LiteralPath $folder) {
                Get-ChildItem -LiteralPath $folder -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.Extension -in @('.log', '.json', '.txt', '.xml') -and $_.Length -lt 50MB } |
                    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $target -Force }
            }
        }
        $lcms = Join-Path $dist 'lcms-build'
        if (Test-Path -LiteralPath $lcms) {
            Get-ChildItem -LiteralPath $lcms -Recurse -File -Filter '*.log' -ErrorAction SilentlyContinue |
                ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $target -Force }
        }
    } catch { }
}

# ---------------------------------------------------------------- 本体

$script:ExitCode = 1
function Invoke-Main {
    Write-Host ''
    Write-Host "Camera Profile Studio $Version のビルド" -ForegroundColor White
    Write-Host "  ソース        : $Source"
    Write-Host "  ビルドフォルダー: $Work"
    Write-Host "  ログ          : $LogDir"

    Write-Section '1/6 事前チェック（PC の設定は変更しません）'
    $script:PyExe = $null
    Invoke-Preflight
    Show-Checks
    $Summary.checks = @($Checks | ForEach-Object { [ordered]@{ name = $_.name; status = $_.status; detail = $_.detail } })
    Save-Summary

    $failed = @($Checks | Where-Object { $_.status -eq 'FAIL' })
    if ($failed.Count) {
        Write-Host ''
        Write-Host "準備が必要な項目が $($failed.Count) 件あります。上の「対処」を行ってから、もう一度実行してください。" -ForegroundColor Red
        $Summary.status = 'preflight-failed'
        $script:ExitCode = 2
        return
    }
    if ($CheckOnly) {
        Write-Host ''
        Write-Host '事前チェックのみ完了しました（-CheckOnly）。' -ForegroundColor Green
        $Summary.status = 'checked'
        $script:ExitCode = 0
        return
    }

    Write-Host ''
    Write-Host 'これから次を行います:' -ForegroundColor White
    Write-Host '  ・固定版の部品をダウンロード（Python パッケージ・Inno Setup・LittleCMS・Qt ソース・テスト用プロファイル。合計 1～2 GB 程度）'
    Write-Host '  ・全テスト → EXE（Nuitka）→ インストーラー（Inno Setup）→ 配布物の検査'
    Write-Host '  ・検査のため、作成したインストーラーをビルドフォルダー内へ一時的にサイレントインストールし、すぐアンインストールします'
    Write-Host '    （スタートメニューやデスクトップには登録されません）'
    Write-Host '  ・所要時間の目安: 30～90 分（PC とネットワークによる）。途中で止めるときは Ctrl+C'
    Write-Host '  ・元のソースフォルダーと、既存の launcher.dist・0.2.0 の配布物は変更しません'
    if (-not $Yes) {
        $answer = Read-Host '開始しますか？ [Y] はい / [N] いいえ（既定: Y）'
        if ($answer -and $answer.Trim() -notmatch '^(y|yes|はい)$') {
            Write-Host '中止しました。何も変更していません。'
            $Summary.status = 'cancelled'
            $script:ExitCode = 3
            return
        }
    }

    # 子プロセスに不要な Python 環境変数を持ち込まない。
    foreach ($name in @('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV')) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }

    Invoke-Step '2/6 ソースを新しいビルドフォルダーへ複製' {
        New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
        New-Item -ItemType Directory -Path $Work | Out-Null
        $exclude = @('__pycache__', '.pytest_cache')
        if ($RepoMode) {
            # リポジトリの作業用ファイルと、このビルド自身の出力先は複製しない（再帰コピーを防ぐ）。
            $exclude += @('.git', '.venv', 'venv', '.mypy_cache', '.ruff_cache', "release-$Version*",
                (Join-Path $Source 'output'), (Join-Path $Source 'build'), (Join-Path $Source 'dist'),
                $BuildRoot, (Join-Path $Kit 'build-logs'))
        }
        $copyArgs = @($Source, $Work, '/E', '/XD') + $exclude + @('/XF', '*.pyc', '/R:1', '/W:1', '/NP', '/NFL', '/NDL')
        Invoke-Logged 'robocopy.exe' $copyArgs 'copy.log' -SuccessCodes @(0, 1, 2, 3, 4, 5, 6, 7)
        if (Test-Path -LiteralPath (Join-Path $Work 'output')) { throw 'ビルドフォルダーに output があります' }
        Set-Location -LiteralPath $Work
    }

    Invoke-Step '3/6 新規ビルドの条件を確認（-ValidateOnly）' {
        Invoke-ProjectScript (Join-Path $Work 'scripts\build-fresh-source.ps1') 'ValidateOnly' 'validate.log'
    }

    $VenvPy = Join-Path $Work '.venv\Scripts\python.exe'
    Invoke-Step '4/6 .venv を作成し、固定版の依存パッケージをインストール' {
        Invoke-Logged $script:PyExe @('-m', 'venv', (Join-Path $Work '.venv')) 'venv.log'
        Invoke-Logged $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
            '-r', (Join-Path $Work 'requirements-lock.txt')) 'pip-lock.log'
        Invoke-Logged $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
            '--no-deps', '--no-build-isolation', '-e', $Work) 'pip-editable.log'
        Invoke-Logged $VenvPy @('-c', 'import sys,nuitka,PySide6,numpy,scipy,camera_profile_studio;print(sys.version.split()[0],camera_profile_studio.__version__)') 'venv-check.log'
        $check = Get-Content -LiteralPath (Join-Path $LogDir 'venv-check.log') -Raw
        if ($check -notmatch [regex]::Escape($Version)) { throw ".venv のアプリのバージョンが $Version ではありません: $check" }
    }

    Invoke-Step '5/6 テスト・EXE・インストーラーのビルドと検査（-BuildApproved）' {
        Write-Host '   いちばん時間がかかる手順です。画面が止まって見えても、1 分ごとに経過と現在の段階を表示します。'
        Invoke-ProjectScript (Join-Path $Work 'scripts\build-fresh-source.ps1') 'BuildApproved' 'build.log'
    }

    Invoke-Step '6/6 成果物をコピーし、SHA-256 を照合' {
        $rel = Join-Path $Work "output\releases\$Version"
        $names = @("CameraProfileStudio-$Version-Setup.exe", "CameraProfileStudio-$Version-Portable.zip", "CameraProfileStudio-$Version-Source.zip")
        $sums = @{}
        foreach ($line in (Get-Content -LiteralPath (Join-Path $rel 'DELIVERY_SHA256SUMS.txt'))) {
            if ($line -match '^([0-9a-fA-F]{64})\s+(\S+)$') { $sums[$matches[2]] = $matches[1].ToLowerInvariant() }
        }
        $script:Release = Join-Path $Kit "release-$Version"
        if (Test-Path -LiteralPath $script:Release) { $script:Release = Join-Path $Kit "release-$Version-$Stamp" }
        New-Item -ItemType Directory -Path $script:Release | Out-Null
        foreach ($name in $names) {
            $path = Join-Path $rel $name
            if (-not (Test-Path -LiteralPath $path)) { throw "成果物がありません: $path" }
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($sums[$name] -ne $hash) { throw "SHA-256 が一致しません: $name" }
            Copy-Item -LiteralPath $path -Destination $script:Release
            $copied = (Get-FileHash -LiteralPath (Join-Path $script:Release $name) -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($copied -ne $hash) { throw "コピー後の SHA-256 が一致しません: $name" }
            $Summary.artifacts += [ordered]@{ file = $name; sha256 = $hash; bytes = (Get-Item -LiteralPath $path).Length }
            Write-Host ("   {0}  {1:N1} MB  SHA-256 一致" -f $name, ((Get-Item -LiteralPath $path).Length / 1MB))
        }
        foreach ($extra in @('DELIVERY_SHA256SUMS.txt', 'source-tests.log', 'source-tests.xml', 'fresh-build-inputs.json')) {
            $path = Join-Path $rel $extra
            if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $script:Release }
        }
        $smoke = Join-Path $rel 'workspace\output\distribution\smoke-results.json'
        if (Test-Path -LiteralPath $smoke) { Copy-Item -LiteralPath $smoke -Destination $script:Release }

        Write-Host '   ポータブル版を展開しています…'
        $portable = Join-Path $script:Release 'portable'
        Expand-Archive -LiteralPath (Join-Path $script:Release "CameraProfileStudio-$Version-Portable.zip") -DestinationPath $portable
        $exe = Join-Path $portable 'launcher.dist\CameraProfileStudio.exe'
        if (-not (Test-Path -LiteralPath $exe)) { throw "展開したポータブル版に EXE がありません: $exe" }
        if (-not (Test-Path -LiteralPath (Join-Path $portable 'launcher.dist\licenses\APP-LICENSE.txt'))) {
            throw 'ポータブル版に licenses\APP-LICENSE.txt がありません'
        }
        $Summary.release = $script:Release
        $Summary.exe = $exe
    }

    $Summary.status = 'succeeded'
    $script:ExitCode = 0
    Write-Host ''
    Write-Host "ビルドが完了しました（Camera Profile Studio $Version）。" -ForegroundColor Green
    Write-Host "  起動する EXE     : $($Summary.exe)"
    Write-Host "  インストーラー   : $(Join-Path $script:Release "CameraProfileStudio-$Version-Setup.exe")"
    Write-Host "  成果物フォルダー : $($script:Release)"
    Write-Host '  ※ コード署名はしていません。他の PC へ配ると SmartScreen の確認が出ることがあります。'
    Write-Host "  ※ ビルドフォルダー（$Work）は確認後に削除してかまいません。"
    try { Invoke-Item -LiteralPath $script:Release } catch { }
}

try {
    Invoke-Main
} catch {
    $Summary.status = 'failed'
    $Summary.error = "$($_.Exception.Message)"
    Write-Host ''
    Write-Host 'ビルドに失敗しました。' -ForegroundColor Red
    if ($Summary.failed_step) { Write-Host "  失敗した手順: $($Summary.failed_step)" -ForegroundColor Red }
    Write-Host "  内容: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  ログ: $LogDir"
    Write-Host "  ビルドフォルダーは調べるために残しています: $Work"
    Write-Host '  再実行すると、新しいビルドフォルダーで最初からやり直します（失敗したフォルダーは再利用しません）。'
    Write-Host '  Claude に「ビルドが失敗した」と伝えると、このログから原因を調べられます。'
    $script:ExitCode = 1
} finally {
    if ($Summary.status -eq 'running') { $Summary.status = 'interrupted' }
    if (Test-Path -LiteralPath $Work) { Save-ProjectLogs }
    Save-Summary
    try { Stop-Transcript | Out-Null } catch { }
}
exit $script:ExitCode
