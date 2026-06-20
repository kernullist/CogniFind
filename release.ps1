# release.ps1 - Build a versioned release and package it for distribution.
# Runs the build with -Release (which bumps the patch version baked into the
# exe), then zips the portable package as dist\CogniFind-v<version>.zip.

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$TAURI_DIR = Join-Path $ROOT "frontend\src-tauri"
$CONF = Join-Path $TAURI_DIR "tauri.conf.json"
$PORTABLE_DIR = Join-Path $ROOT "dist\CogniFind-portable"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  CogniFind Release" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# 1. Build with a version bump. (build.ps1 exits non-zero on failure, which
#    terminates this script before packaging.)
& (Join-Path $ROOT "build.ps1") -Release
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: release build failed." -ForegroundColor Red
    exit 1
}

# 2. Read the (now bumped) version.
$conf = Get-Content $CONF -Raw
if ($conf -match '"version":\s*"(\d+\.\d+\.\d+)"') {
    $version = $matches[1]
} else {
    Write-Host "ERROR: could not read version from tauri.conf.json" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $PORTABLE_DIR)) {
    Write-Host "ERROR: portable build not found at $PORTABLE_DIR" -ForegroundColor Red
    exit 1
}

# 3. Zip the portable package for distribution.
$ZIP = Join-Path $ROOT "dist\CogniFind-v$version.zip"
if (Test-Path $ZIP) {
    Remove-Item $ZIP -Force
}
Compress-Archive -Path (Join-Path $PORTABLE_DIR "*") -DestinationPath $ZIP

$sizeMB = [math]::Round((Get-Item $ZIP).Length / 1MB, 1)
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Release v$version packaged" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host "  $ZIP ($sizeMB MB)" -ForegroundColor White
Write-Host ""
Write-Host "Note: the version bump modified tauri.conf.json, Cargo.toml, and" -ForegroundColor Cyan
Write-Host "package.json -- commit and tag them (e.g. git tag v$version)." -ForegroundColor Cyan
