$ErrorActionPreference = "Stop"

# Re-launch elevated: Npcap needs an administrator token to open the live adapter.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments
    exit
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$capture = Join-Path $projectRoot "bin\albion-capture-windows-amd64.exe"
if (-not (Test-Path -LiteralPath $capture)) {
    throw "Missing capture executable: $capture"
}

$npcap = Get-Service -Name "npcap" -ErrorAction SilentlyContinue
if (-not $npcap -or $npcap.Status -ne "Running") {
    throw "Npcap is not running. Install Npcap with WinPcap API-compatible Mode, then retry."
}

# The interface that owns the IPv4 default gateway is the actual Internet path.
$ipConfig = Get-NetIPConfiguration | Where-Object {
    $_.IPv4DefaultGateway -and $_.IPv4Address -and $_.NetAdapter.Status -eq "Up"
} | Select-Object -First 1
if (-not $ipConfig) {
    throw "No active IPv4 default-route adapter was found. Connect to the Internet, then retry."
}
$adapter = Get-NetAdapter -InterfaceIndex $ipConfig.InterfaceIndex
$device = "\Device\NPF_$($adapter.InterfaceGuid.ToString().ToUpperInvariant())"

# ExitLag encapsulates Albion traffic before it reaches Npcap. Capturing the
# relay's dynamic port only exposes the encrypted tunnel, which the passive
# Photon parser intentionally cannot decrypt.
$exitLagProcesses = @(Get-Process -Name "ExitLag" -ErrorAction SilentlyContinue)
if ($exitLagProcesses.Count -gt 0) {
    throw "ExitLag is running. Its encrypted tunnel cannot be parsed. Close ExitLag completely, restart Albion, then run this script again."
}
$capturePort = 5056

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    $pythonExe = $python.Source
    $pythonArgs = "-3 -m albion_tracker serve"
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw "Python 3.10 or newer is required." }
    $pythonExe = $python.Source
    $pythonArgs = "-m albion_tracker serve"
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 2
    $trackerReady = $health.ok -eq $true
} catch {
    $trackerReady = $false
}
if (-not $trackerReady) {
    Start-Process -FilePath $pythonExe -ArgumentList $pythonArgs -WorkingDirectory $projectRoot -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Write-Host "Npcap: $($npcap.Status)" -ForegroundColor Green
Write-Host "Monitoring adapter: $($adapter.Name) ($($ipConfig.IPv4Address.IPAddress))" -ForegroundColor Green
Write-Host "Device: $device"
Write-Host "Game traffic port: $capturePort" -ForegroundColor Green
Write-Host "Tracker: http://127.0.0.1:8765"
Write-Host "Open Albion, enter the marketplace, then load offers/requests before placing a transaction." -ForegroundColor Yellow
Write-Host "Press Ctrl+C to stop capture."

Push-Location $projectRoot
try {
    & $capture -devices $device -port $capturePort -api "http://127.0.0.1:8765"
} finally {
    Pop-Location
}
