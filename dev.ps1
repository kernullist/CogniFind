# dev.ps1 - CogniFind Development Script
# Runs the app in dev mode using local Python (no PyInstaller needed)

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$TAURI_DIR = Join-Path $ROOT "frontend\src-tauri"
$BINARIES_DIR = Join-Path $TAURI_DIR "binaries"
$SIDECAR_NAME = "cognifind-backend-x86_64-pc-windows-msvc.exe"

Write-Host "CogniFind Dev Mode" -ForegroundColor Cyan
Write-Host ""

# Ensure dummy sidecar exists for cargo build
if (-not (Test-Path $BINARIES_DIR)) {
    New-Item -ItemType Directory -Path $BINARIES_DIR -Force | Out-Null
}
$DEST = Join-Path $BINARIES_DIR $SIDECAR_NAME
if (-not (Test-Path $DEST)) {
    Write-Host "Creating dummy sidecar binary for dev..." -ForegroundColor Gray
    "" | Out-File -FilePath $DEST -Encoding ascii
}

# Install frontend deps if needed
$NODE_MODULES = Join-Path $ROOT "frontend\node_modules"
if (-not (Test-Path $NODE_MODULES)) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
    Push-Location (Join-Path $ROOT "frontend")
    try {
        npm install --silent
    } finally {
        Pop-Location
    }
}

Write-Host "Starting Tauri dev server..." -ForegroundColor Yellow
Write-Host "  Backend: python api.py (launched by Tauri fallback)" -ForegroundColor Gray
Write-Host "  Frontend: http://localhost:5173" -ForegroundColor Gray
Write-Host ""

Push-Location (Join-Path $ROOT "frontend")
try {
    npx tauri dev
} finally {
    Pop-Location
}
