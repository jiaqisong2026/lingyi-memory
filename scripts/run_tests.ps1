# Run all unit tests (no network, no infra required).
# Usage: pwsh -File scripts\run_tests.ps1

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root

Write-Host "==> Running unit suite (no network)" -ForegroundColor Cyan
py -3.12 -m pytest tests/test_ssh_mock_v2.py tests/test_memory_v2.py `
    -v --no-header --tb=short `
    -p no:cacheprovider 2>&1 | Tee-Object -FilePath "$root\evidence\test_run.txt"

Write-Host "==> Done. Summary saved to evidence\test_run.txt" -ForegroundColor Green