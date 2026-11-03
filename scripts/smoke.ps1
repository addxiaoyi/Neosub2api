# smoke test: start fake upstream + control plane, hit key endpoints, assert status, cleanup
# usage: powershell -ExecutionPolicy Bypass -File scripts/smoke.ps1
$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$ctrl = Join-Path $repo "apps\control"
$bundled = Join-Path $repo ".runtime\python\python.exe"
if (Test-Path $bundled) {
    $py = $bundled
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $py = "py"
} else {
    Write-Host "Python 3 not found (looked for .runtime\python\python.exe, python, py)" -ForegroundColor Red
    exit 1
}
$fakePort = 8021
$ctrlPort = 8020

Write-Host "== smoke ==" -ForegroundColor Cyan

function Test-PortFree($p) {
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
        $l.Stop()
        return $true
    } catch { return $false }
}

if (-not (Test-PortFree $fakePort)) { Write-Host "port $fakePort occupied" -ForegroundColor Red; exit 1 }
if (-not (Test-PortFree $ctrlPort)) { Write-Host "port $ctrlPort occupied" -ForegroundColor Red; exit 1 }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "u2a-smoke-$(Get-Random)"
$null = New-Item -ItemType Directory -Path $tmp -Force
$dataDir = Join-Path $tmp "data"; $null = New-Item -ItemType Directory -Path $dataDir -Force

$fakeProc = Start-Process -FilePath $py `
    -ArgumentList @((Join-Path $ctrl "scripts\fake_upstream.py"), "$fakePort") `
    -PassThru -WindowStyle Hidden

$env:UNIFIED_DATA_DIR = $dataDir
$env:UNIFIED_DB_PATH = (Join-Path $dataDir "unified.db")
$env:UNIFIED_ADMIN_USER = "admin"
$env:UNIFIED_ADMIN_PASSWORD = "test"
$env:CODEBUDDY_BASE = "http://127.0.0.1:$fakePort"
$env:OPENCODE_UPSTREAM_BASE = "http://127.0.0.1:$fakePort"
$env:UNIFIED_WEB_DIR = (Join-Path $tmp "no-web")
$env:UNIFIED_ALLOW_ANONYMOUS_GATEWAY = "false"
$env:GROK_REGISTER_EXECUTOR_URL = ""

$ctrlProc = Start-Process -FilePath $py `
    -ArgumentList @("-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$ctrlPort","--log-level","warning") `
    -PassThru -WorkingDirectory $ctrl -WindowStyle Hidden

try {
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/healthz" -TimeoutSec 1 -UseBasicParsing
            $ok = $true; break
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $ok) { throw "control plane not ready" }

    Write-Host "services up" -ForegroundColor Green

    # WebSession auto-manages cookies across Invoke-WebRequest calls
    $s = New-Object Microsoft.PowerShell.Commands.WebRequestSession

    $login = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/api/login" `
        -Method POST -ContentType "application/json" -Body '{"username":"admin","password":"test"}' `
        -UseBasicParsing -WebSession $s
    if ($login.StatusCode -ne 200) { throw "login failed" }

    $newKey = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/api/keys" `
        -Method POST -ContentType "application/json" -Body '{"name":"smoke"}' `
        -UseBasicParsing -WebSession $s
    $keyBody = $newKey.Content | ConvertFrom-Json
    $bearer = $keyBody.key
    Write-Host "  key id=$($keyBody.id)"

    function Assert-Status($name, $resp, $expect=200) {
        if ($resp.StatusCode -ne $expect) {
            throw "[$name] status=$($resp.StatusCode) expect=$expect body=$($resp.Content)"
        }
        Write-Host "  OK  $name -> $($resp.StatusCode)"
    }

    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/healthz" -UseBasicParsing
    Assert-Status "healthz" $r

    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/v1/models" `
        -Headers @{ Authorization = "Bearer $bearer" } -UseBasicParsing
    Assert-Status "v1/models" $r

    $body = '{"model":"m","messages":[{"role":"user","content":"hi"}]}'
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/v1/chat/completions" `
        -Method POST -ContentType "application/json" -Body $body `
        -Headers @{ Authorization = "Bearer $bearer" } -UseBasicParsing
    Assert-Status "v1/chat/completions" $r

    $body = '{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}'
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/v1/chat/completions" `
        -Method POST -ContentType "application/json" -Body $body `
        -Headers @{ Authorization = "Bearer $bearer" } -UseBasicParsing
    Assert-Status "v1/chat/completions stream" $r

    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ctrlPort/v1/chat/completions" `
            -Method POST -ContentType "application/json" -Body $body -UseBasicParsing
        throw "expect 401 got $($r.StatusCode)"
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode.value__ -eq 401) {
            Write-Host "  OK  no-auth -> 401"
        } else { throw }
    }

    Write-Host ""
    Write-Host "== SMOKE PASSED ==" -ForegroundColor Green
} finally {
    foreach ($p in @($fakeProc, $ctrlProc)) {
        if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "cleaned up" -ForegroundColor DarkGray
}
