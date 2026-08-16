$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Capture = Join-Path $ProjectRoot "bin\albion-capture-windows-amd64.exe"

if (-not (Test-Path -LiteralPath $Capture)) {
    throw "Capture executable not found: $Capture. Run scripts\build-windows.ps1 first."
}

$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) {
    $PythonExe = $Python.Source
    $PythonArgs = "-3 -m albion_tracker serve"
} else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { throw "Python 3.10 or newer is required." }
    $PythonExe = $Python.Source
    $PythonArgs = "-m albion_tracker serve"
}

$ServerRunning = $false
try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 1
    $ServerRunning = $Health.ok -eq $true
} catch {}

if ($ServerRunning) {
    Write-Host "Tracker is already running at http://127.0.0.1:8765"
} else {
    Start-Process -FilePath $PythonExe -ArgumentList $PythonArgs -WorkingDirectory $ProjectRoot
    Start-Sleep -Seconds 2
}

Start-Process "http://127.0.0.1:8765"
Write-Host "Tracker is ready. Approve the UAC prompt to start packet capture; keep the capture window open."
Start-Process -FilePath $Capture -WorkingDirectory $ProjectRoot -Verb RunAs
