# Rebuild docs/site/index.html from the repository Markdown.
# Works on Windows PowerShell 5.1 and PowerShell 7. ASCII-only on purpose:
# Windows PowerShell 5.1 misreads non-BOM UTF-8 script files.

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$siteDir = $PSScriptRoot
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $siteDir '..\..')).Path
$outFile = Join-Path $siteDir 'index.html'

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$python = $null
$repoPython = Join-Path $repoRoot '.runtime\python\python.exe'
if (Test-Path -LiteralPath $repoPython) {
    $python = $repoPython
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}

if (-not $python) {
    Write-Error 'Python not found. Expected .runtime\python\python.exe or python on PATH.'
    exit 1
}

$script = Join-Path $siteDir 'build.py'
Write-Host "python : $python"
Write-Host "source : $repoRoot"
Write-Host "output : $outFile"

& $python $script --repo-root $repoRoot --out $outFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "build.py failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host 'Done. Open docs/site/index.html directly in a browser (no server needed).'
