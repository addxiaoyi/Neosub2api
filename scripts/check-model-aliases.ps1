# validate config/model-aliases.yaml: structure, providers, weights, fallback cycles, capabilities
# usage: pwsh -File scripts/check-model-aliases.ps1 [-ConfigPath <path>] [-Strict]
# exit codes: 0 pass, 1 validation errors, 2 missing config / parser failure
param(
    [string]$ConfigPath = "",
    [switch]$Strict
)
$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$validator = Join-Path $PSScriptRoot "check_model_aliases.py"
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
if ($Strict) { $cli += "--strict" }

Write-Host "== model aliases ==" -ForegroundColor Cyan
& $launcher @($prefix + $cli)
exit $LASTEXITCODE
