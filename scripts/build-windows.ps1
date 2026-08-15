$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Output = Join-Path $ProjectRoot "bin\albion-capture-windows-amd64.exe"
New-Item -ItemType Directory -Force (Split-Path -Parent $Output) | Out-Null

Push-Location $ProjectRoot
try {
    $env:CGO_ENABLED = "0"
    $env:GOOS = "windows"
    $env:GOARCH = "amd64"
    go build -trimpath -o $Output ./cmd/albion-capture
    Write-Host "已建置：$Output"
} finally {
    Pop-Location
}
