# build.ps1 - CogniFind Build Script
# Builds the Python backend (PyInstaller) + Tauri frontend into the portable
# package (dist\CogniFind-portable). Pass -Release to bump the patch version
# (baked into the exe) before building; otherwise it just builds.
param([switch]$Release)

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$TAURI_DIR = Join-Path $ROOT "frontend\src-tauri"
$BINARIES_DIR = Join-Path $TAURI_DIR "binaries"
$SIDECAR_NAME = "cognifind-backend-x86_64-pc-windows-msvc.exe"

# --- Release: bump the patch version in the exe's version sources ---
if ($Release) {
    $CONF  = Join-Path $TAURI_DIR "tauri.conf.json"
    $CARGO = Join-Path $TAURI_DIR "Cargo.toml"
    $PKG   = Join-Path $ROOT "frontend\package.json"

    # Use .NET file IO so this is immune to any Get-Content/Set-Content proxies
    # in the user's PowerShell profile (which can drop -Raw / -NoNewline).
    # NOTE: PowerShell variables are case-insensitive, so the content holders
    # must NOT reuse the path names ($CONF/$CARGO/$PKG) -- reusing them would
    # overwrite the path variable with the file content.
    $confText = [System.IO.File]::ReadAllText($CONF)
    if ($confText -match '"version":\s*"(\d+)\.(\d+)\.(\d+)"') {
        $old = "$($matches[1]).$($matches[2]).$($matches[3])"
        $new = "$($matches[1]).$($matches[2]).$([int]$matches[3] + 1)"
    } else {
        Write-Host "ERROR: could not find version in tauri.conf.json" -ForegroundColor Red
        exit 1
    }

    # tauri.conf.json
    $confText = $confText -replace ('"version":\s*"' + [regex]::Escape($old) + '"'), ('"version": "' + $new + '"')
    [System.IO.File]::WriteAllText($CONF, $confText)

    # Cargo.toml (package version, at line start)
    $cargoText = [System.IO.File]::ReadAllText($CARGO)
    $cargoText = $cargoText -replace ('(?m)^version = "' + [regex]::Escape($old) + '"'), ('version = "' + $new + '"')
    [System.IO.File]::WriteAllText($CARGO, $cargoText)

    # package.json (its own version line, whatever value)
    $pkgText = [System.IO.File]::ReadAllText($PKG)
    $pkgText = $pkgText -replace '"version":\s*"\d+\.\d+\.\d+"', ('"version": "' + $new + '"')
    [System.IO.File]::WriteAllText($PKG, $pkgText)

    Write-Host "Release build: version bumped $old -> $new" -ForegroundColor Magenta
    Write-Host ""
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  CogniFind Build Pipeline" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Check prerequisites
Write-Host "[1/6] Checking prerequisites..." -ForegroundColor Yellow
$missing = @()
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { $missing += "python" }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { $missing += "node" }
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { $missing += "cargo (Rust)" }
if (-not (Get-Command npx -ErrorAction SilentlyContinue)) { $missing += "npx" }

if ($missing.Count -gt 0) {
    Write-Host "ERROR: Missing tools: $($missing -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "  All prerequisites OK." -ForegroundColor Green

# Step 2: Install build dependencies (PyInstaller + OCR libs bundled for
# scanned-PDF support). The spec lists fitz/rapidocr_onnxruntime as hidden
# imports, so they must be present at build time.
Write-Host "[2/6] Ensuring build dependencies..." -ForegroundColor Yellow
$pipResult = python -m pip show pyinstaller 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing PyInstaller..." -ForegroundColor Gray
    python -m pip install pyinstaller --quiet
}
python -m pip install --quiet pymupdf rapidocr-onnxruntime
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install OCR dependencies!" -ForegroundColor Red
    exit 1
}
Write-Host "  Build dependencies OK." -ForegroundColor Green

# Step 3: Build Python backend with PyInstaller
Write-Host "[3/6] Building Python backend (PyInstaller)..." -ForegroundColor Yellow
Push-Location $ROOT
try {
    python -m PyInstaller cognifind-backend.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: PyInstaller build failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

$PYEXE = Join-Path $ROOT "dist\cognifind-backend.exe"
if (-not (Test-Path $PYEXE)) {
    Write-Host "ERROR: PyInstaller output not found at $PYEXE" -ForegroundColor Red
    exit 1
}

# Copy to Tauri binaries directory
if (-not (Test-Path $BINARIES_DIR)) {
    New-Item -ItemType Directory -Path $BINARIES_DIR -Force | Out-Null
}
$DEST = Join-Path $BINARIES_DIR $SIDECAR_NAME
Copy-Item $PYEXE $DEST -Force
Write-Host "  Backend built: $DEST" -ForegroundColor Green

# Step 4: Install npm dependencies
Write-Host "[4/6] Installing frontend dependencies..." -ForegroundColor Yellow
Push-Location (Join-Path $ROOT "frontend")
try {
    npm install --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: npm install failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
Write-Host "  Frontend dependencies OK." -ForegroundColor Green

# Step 5: Build Tauri application (exe only; the portable package does not need
# NSIS/MSI installers). Must go through the Tauri CLI so the frontend assets are
# embedded for production -- a plain `cargo build` produces an exe that tries to
# load the dev server (localhost) and fails with ERR_CONNECTION_REFUSED.
Write-Host "[5/6] Building Tauri application (exe, no installer)..." -ForegroundColor Yellow
Push-Location (Join-Path $ROOT "frontend")
try {
    npx tauri build --no-bundle
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Tauri build failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

# Step 6: Assemble the portable package (exe + sidecar + models next to each
# other). Updates ship just the two exes; the models/ folder persists.
Write-Host "[6/6] Assembling portable package..." -ForegroundColor Yellow
$TAURI_EXE = Join-Path $TAURI_DIR "target\release\CogniFind.exe"
if (-not (Test-Path $TAURI_EXE)) {
    Write-Host "ERROR: Tauri exe not found at $TAURI_EXE" -ForegroundColor Red
    exit 1
}

$PORTABLE_DIR = Join-Path $ROOT "dist\CogniFind-portable"
if (Test-Path $PORTABLE_DIR) {
    Remove-Item $PORTABLE_DIR -Recurse -Force
}
New-Item -ItemType Directory -Path $PORTABLE_DIR -Force | Out-Null

Copy-Item $TAURI_EXE (Join-Path $PORTABLE_DIR "CogniFind.exe") -Force
Copy-Item $PYEXE (Join-Path $PORTABLE_DIR "cognifind-backend.exe") -Force
# AGPL-3.0 requires the license text to accompany binary distribution.
Copy-Item (Join-Path $ROOT "LICENSE") (Join-Path $PORTABLE_DIR "LICENSE") -Force

# Models live next to the exe so exe-only updates leave them untouched.
Push-Location $ROOT
try {
    python scripts\fetch_models.py (Join-Path $PORTABLE_DIR "models")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Model fetch failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

Write-Host "  Portable package: $PORTABLE_DIR" -ForegroundColor Green

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Build Complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""

# Show output files
$BUNDLE_DIR = Join-Path $TAURI_DIR "target\release\bundle"
if (Test-Path $BUNDLE_DIR) {
    Write-Host "Installer outputs:" -ForegroundColor Cyan
    Get-ChildItem -Path $BUNDLE_DIR -Recurse -Include *.exe,*.msi,*.nsis | ForEach-Object {
        $sizeMB = [math]::Round($_.Length / 1MB, 1)
        Write-Host "  $($_.FullName) ($sizeMB MB)" -ForegroundColor White
    }
}

$TAURI_EXE = Join-Path $TAURI_DIR "target\release\CogniFind.exe"
if (Test-Path $TAURI_EXE) {
    $sizeMB = [math]::Round((Get-Item $TAURI_EXE).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Main executable:" -ForegroundColor Cyan
    Write-Host "  $TAURI_EXE ($sizeMB MB)" -ForegroundColor White
}

Write-Host ""
Write-Host "Portable build is at dist\CogniFind-portable." -ForegroundColor Cyan
Write-Host "Run .\release.ps1 to bump the version and produce a distributable .zip." -ForegroundColor Cyan
Write-Host "To UPDATE an existing install, replace only CogniFind.exe and" -ForegroundColor Cyan
Write-Host "cognifind-backend.exe; the models\ folder persists." -ForegroundColor Cyan
Write-Host ""
Write-Host "Done." -ForegroundColor Green
