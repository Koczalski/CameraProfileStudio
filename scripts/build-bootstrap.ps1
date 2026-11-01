$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$tools = Join-Path $root '.tools/packaging'
New-Item -ItemType Directory -Force $tools | Out-Null
$downloads = @(
    @('https://github.com/jrsoftware/issrc/releases/download/is-6_0_5/innosetup-6.0.5.exe', 'innosetup-6.0.5.exe', 'AE6823B523DF87E9441789E51845434E4E0E70AAC0B88AFE80F94F20F4B98ACB'),
    @('https://github.com/dscharrer/innoextract/releases/download/1.9/innoextract-1.9-windows.zip', 'innoextract-1.9-windows.zip', '6989342C9B026A00A72A38F23B62A8E6A22CC5DE69805CF47D68AC2FEC993065')
)
foreach ($item in $downloads) {
    $path = Join-Path $tools $item[1]
    if (!(Test-Path $path)) { Invoke-WebRequest $item[0] -OutFile $path }
    if ((Get-FileHash $path -Algorithm SHA256).Hash -ne $item[2]) { throw "Hash mismatch: $path" }
}
if (!(Test-Path "$tools/innoextract/innoextract.exe")) {
    Expand-Archive "$tools/innoextract-1.9-windows.zip" "$tools/innoextract" -Force
}
if (!(Test-Path "$tools/inno-6.0.5/app/ISCC.exe")) {
    & "$tools/innoextract/innoextract.exe" --extract --output-dir "$tools/inno-6.0.5" "$tools/innosetup-6.0.5.exe"
    if ($LASTEXITCODE -ne 0) { throw 'Inno compiler extraction failed' }
}
# Never execute the downloaded installer: extract the compiler into the workspace only.
$sourceDir = Join-Path $root 'output/distribution/third-party-source'
New-Item -ItemType Directory -Force $sourceDir | Out-Null
$sourceManifest = Get-Content "$root/packaging/upstream-sources.json" -Raw | ConvertFrom-Json
foreach ($source in $sourceManifest.archives) {
    if (!$source.download_by_default) { continue }
    $path = Join-Path $sourceDir $source.filename
    if (!(Test-Path $path)) { Invoke-WebRequest $source.url -OutFile $path }
    if ((Get-Item $path).Length -ne $source.bytes -or (Get-FileHash $path).Hash -ne $source.sha256) {
        throw "Upstream source verification failed: $path"
    }
}
Copy-Item "$root/packaging/upstream-sources.json" "$sourceDir/manifest.json" -Force
Copy-Item "$root/packaging/UPSTREAM-SOURCE-README.md" "$sourceDir/README.md" -Force
New-Item -ItemType Directory -Force "$sourceDir/build-reference" | Out-Null
Copy-Item "$root/packaging/upstream/*" "$sourceDir/build-reference/" -Recurse -Force
if (Test-Path "$root/output/distribution/toolchain-environment.json") {
    Copy-Item "$root/output/distribution/toolchain-environment.json" "$sourceDir/" -Force
}
Write-Host "Pinned Inno Setup compiler ready: $tools/inno-6.0.5/app/ISCC.exe"
Write-Host 'Qt module source archives acquired; the build verifies payload coverage (packaging/verify_qt_sources.py).'
