# build.ps1 - CogniFind Full Build Script
# Builds Python backend (PyInstaller) + Tauri frontend into a single installer

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$TAURI_DIR = Join-Path $ROOT "frontend\src-tauri"
$BINARIES_DIR = Join-Path $TAURI_DIR "binaries"
$SIDECAR_NAME = "cognifind-backend-x86_64-pc-windows-msvc.exe"

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

# Step 2: Install PyInstaller if needed
Write-Host "[2/6] Ensuring PyInstaller is installed..." -ForegroundColor Yellow
$pipResult = python -m pip show pyinstaller 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing PyInstaller..." -ForegroundColor Gray
    python -m pip install pyinstaller --quiet
}
Write-Host "  PyInstaller OK." -ForegroundColor Green

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

$ZIP = Join-Path $ROOT "dist\CogniFind-portable.zip"
if (Test-Path $ZIP) {
    Remove-Item $ZIP -Force
}
Compress-Archive -Path (Join-Path $PORTABLE_DIR "*") -DestinationPath $ZIP
Write-Host "  Portable package: $PORTABLE_DIR" -ForegroundColor Green
Write-Host "  Portable zip:     $ZIP" -ForegroundColor Green

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
Write-Host "Distribution: ship dist\CogniFind-portable (or the .zip)." -ForegroundColor Cyan
Write-Host "To UPDATE an existing install, replace only CogniFind.exe and" -ForegroundColor Cyan
Write-Host "cognifind-backend.exe; the models\ folder persists." -ForegroundColor Cyan
Write-Host ""
Write-Host "Done." -ForegroundColor Green
