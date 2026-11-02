# validate config/prices.yaml: duplicate keys, token_class enum, positive prices, micro-USD precision, sources
# usage: pwsh -File scripts/check-prices.ps1 [-ConfigPath <path>] [-Strict]
# exit codes: 0 pass, 1 validation errors, 2 missing config / parser failure
param(
    [string]$ConfigPath = "",
    [string]$AliasPath = "",
    [switch]$Strict
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$validator = Join-Path $PSScriptRoot "check-prices.py"
if (-not (Test-Path $validator)) {
    Write-Host "validator not found: $validator" -ForegroundColor Red
    exit 2
}

$bundled = Join-Path $repo ".runtime\python\python.exe"
$launcher = $null
$prefix = @()
if (Test-Path $bundled) {
    $launcher = $bundled
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $launcher = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $launcher = "py"
    $prefix = @("-3")
} else {
    Write-Host "Python 3 not found (looked for .runtime\python\python.exe, python, py)" -ForegroundColor Red
    exit 2
}

$cli = @($validator, "--repo-root", $repo)
if ($ConfigPath) { $cli += @("--config", $ConfigPath) }
if ($AliasPath) { $cli += @("--aliases", $AliasPath) }
if ($Strict) { $cli += "--strict" }

Write-Host "== prices ==" -ForegroundColor Cyan
& $launcher @($prefix + $cli)
exit $LASTEXITCODE
