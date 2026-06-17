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

# Step 5: Fetch embedding models for offline bundling
Write-Host "[5/6] Fetching embedding models (offline bundle)..." -ForegroundColor Yellow
$MODELS_DIR = Join-Path $TAURI_DIR "resources\models"
Push-Location $ROOT
try {
    python scripts\fetch_models.py "$MODELS_DIR"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Model fetch failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
Write-Host "  Models ready: $MODELS_DIR" -ForegroundColor Green

# Step 6: Build Tauri application
Write-Host "[6/6] Building Tauri application..." -ForegroundColor Yellow
Push-Location (Join-Path $ROOT "frontend")
try {
    npx tauri build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Tauri build failed!" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

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
Write-Host "Done." -ForegroundColor Green
