$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Capture = Join-Path $ProjectRoot "bin\albion-capture-windows-amd64.exe"

if (-not (Test-Path $Capture)) {
    throw "找不到 $Capture，請先執行 scripts\build-windows.ps1"
}

$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) {
    $PythonExe = $Python.Source
    $PythonArgs = "-3 -m albion_tracker serve"
} else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { throw "需要 Python 3.10 以上版本" }
    $PythonExe = $Python.Source
    $PythonArgs = "-m albion_tracker serve"
}

$ServerRunning = $false
try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 1
    $ServerRunning = $Health.ok -eq $true
} catch {}

if ($ServerRunning) {
    Write-Host "沿用已啟動的統計服務。"
} else {
    Start-Process -FilePath $PythonExe -ArgumentList $PythonArgs -WorkingDirectory $ProjectRoot
    Start-Sleep -Seconds 2
}
Start-Process "http://127.0.0.1:8765"

Write-Host "統計頁面已啟動。接下來 Windows 會詢問系統管理員權限，以讀取本機網路封包。"
Start-Process -FilePath $Capture -WorkingDirectory $ProjectRoot -Verb RunAs
