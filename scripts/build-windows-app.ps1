param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $ProjectRoot ".build\windows-app"
$Capture = Join-Path $BuildRoot "albion-capture-windows-app-amd64.exe"
$Venv = Join-Path $BuildRoot "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Output = Join-Path $ProjectRoot "dist\AlbionMarketLedger.exe"

$Go = Get-Command go -ErrorAction SilentlyContinue
if (-not $Go) { throw "Go 1.24 or newer is required to build the capture component." }
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { throw "Python 3.10 or newer is required to build the desktop app." }

New-Item -ItemType Directory -Force $BuildRoot | Out-Null

Push-Location $ProjectRoot
try {
    if (-not $SkipTests) {
        & $Python.Source -m unittest discover -v
        if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }
        & $Go.Source test ./...
        if ($LASTEXITCODE -ne 0) { throw "Go tests failed." }
    }

    $env:CGO_ENABLED = "0"
    $env:GOOS = "windows"
    $env:GOARCH = "amd64"
    & $Go.Source build -trimpath -ldflags "-H=windowsgui" -o $Capture ./cmd/albion-capture
    if ($LASTEXITCODE -ne 0) { throw "Capture build failed." }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        & $Python.Source -m venv $Venv
    }
    & $VenvPython -m pip install --disable-pip-version-check --upgrade "pyinstaller>=6.10,<7"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

    & $VenvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "AlbionMarketLedger" `
        --distpath (Join-Path $ProjectRoot "dist") `
        --workpath (Join-Path $BuildRoot "pyinstaller") `
        --specpath $BuildRoot `
        --paths $ProjectRoot `
        --add-data "$ProjectRoot\web;web" `
        --add-binary "$Capture;bin" `
        "$ProjectRoot\albion_tracker\windows_app.py"
    if ($LASTEXITCODE -ne 0) { throw "Desktop app packaging failed." }

    Write-Host "Windows desktop app built successfully: $Output"
} finally {
    Pop-Location
}
