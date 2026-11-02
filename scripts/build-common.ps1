# Shared helpers for the build scripts (dot-sourced; not run on its own).
# Persistent build caches are opt-in: packaging/windows-launcher/Build-0.2.6.ps1 sets CPS_BUILD_CACHE (unless -NoCache);
# running a build script directly keeps the previous cache-free behavior. A cached file is only used after its SHA-256
# (or its content-derived key) matches, so a cache can make a build faster but never changes what it produces.

$script:CpsPhases = @()
$script:CpsPhaseName = $null
$script:CpsPhaseStart = $null

function Set-CpsPhase([string]$Name) {
    # Closes the running phase (if any) and starts the next one; Save-CpsPhases writes the durations for later analysis.
    $now = Get-Date
    if ($script:CpsPhaseName) {
        $script:CpsPhases += [pscustomobject]@{ phase = $script:CpsPhaseName; seconds = [math]::Round(($now - $script:CpsPhaseStart).TotalSeconds, 1) }
    }
    $script:CpsPhaseName = $Name
    $script:CpsPhaseStart = $now
}

function Save-CpsPhases([string]$Path) {
    Set-CpsPhase $null
    ConvertTo-Json -InputObject @($script:CpsPhases) -Depth 3 | Set-Content -Encoding UTF8 -LiteralPath $Path
}

function Get-CpsCacheRoot {
    $root = $env:CPS_BUILD_CACHE
    if ([string]::IsNullOrWhiteSpace($root)) { return $null }
    $root = [IO.Path]::GetFullPath($root)
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    return $root
}

function Get-CpsParallelWidth([int]$PerWorkerMb, [string]$OverrideVariable, [int]$Maximum = [int]::MaxValue) {
    # Parallel width decided at run time: one worker per logical processor, reduced when the free physical memory
    # cannot hold $PerWorkerMb for each. The environment variable $OverrideVariable (a positive integer) replaces it.
    $override = 0
    if ([int]::TryParse("$([Environment]::GetEnvironmentVariable($OverrideVariable))", [ref]$override) -and $override -gt 0) { return [math]::Min($override, $Maximum) }
    $width = [math]::Min([Environment]::ProcessorCount, $Maximum)
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $width = [math]::Min($width, [int][math]::Floor($os.FreePhysicalMemory / 1KB / $PerWorkerMb))
    } catch { }
    return [math]::Max(1, $width)
}

# Memory budget per worker: a cl.exe job compiling a large Nuitka module, a pytest worker with the scientific
# stack imported, and one Inno Setup LZMA2 block thread (6 MB + 11.5 x dictionary + 2 x block size, about 9 MB for
# lzma2/fast, with headroom). Inno Setup 6.0.5 accepts at most 256 block threads.
function Get-CpsCompileJobs { return Get-CpsParallelWidth 1200 'CPS_BUILD_JOBS' }
function Get-CpsTestWorkers { return Get-CpsParallelWidth 1000 'CPS_TEST_WORKERS' }
function Get-CpsInstallerThreads { return Get-CpsParallelWidth 64 'CPS_INSTALLER_THREADS' 256 }

function Get-CpsTextHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))) -replace '-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-CpsFile {
    # Fetch $Url into $Destination. With a build cache and a known SHA-256, the verified cached copy replaces the
    # download; a fresh download is stored in the cache only after its SHA-256 matched. Callers still verify the
    # destination exactly as before.
    param([string]$Url, [string]$Destination, [string]$Sha256 = '')
    $cached = $null
    $cacheRoot = Get-CpsCacheRoot
    if ($cacheRoot -and $Sha256) {
        $folder = Join-Path $cacheRoot 'downloads'
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $cached = Join-Path $folder $Sha256.ToLowerInvariant()
        if (Test-Path -LiteralPath $cached) {
            if ((Get-FileHash -LiteralPath $cached -Algorithm SHA256).Hash -eq $Sha256) {
                Copy-Item -LiteralPath $cached -Destination $Destination -Force
                Write-Host ('Build cache hit: {0}' -f (Split-Path $Destination -Leaf))
                return
            }
            Remove-Item -LiteralPath $cached -Force
        }
    }
    Invoke-WebRequest -Uri $Url -OutFile $Destination
    if ($cached -and ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -eq $Sha256)) {
        Copy-Item -LiteralPath $Destination -Destination $cached -Force
    }
}
