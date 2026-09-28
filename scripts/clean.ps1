# Remove __pycache__ and stale .tmp files from this package directory.
# Usage: pwsh -File scripts\clean.ps1

$ErrorActionPreference = "SilentlyContinue"
$root = (Resolve-Path "$PSScriptRoot\..").Path

Write-Host "==> Cleaning $root" -ForegroundColor Cyan

Get-ChildItem -Path $root -Recurse -Directory -Filter "__pycache__" | ForEach-Object {
  Write-Host "  removing $($_.FullName)"
  Remove-Item -Path $_.FullName -Recurse -Force
}

Get-ChildItem -Path $root -Recurse -File -Filter "*.tmp" | ForEach-Object {
  Write-Host "  removing $($_.FullName)"
  Remove-Item -Path $_.FullName -Force
}

Write-Host "==> Done." -ForegroundColor Green