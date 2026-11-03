# e2e 冒烟一键跑：假上游 18112 + 控制面 18111（直出 apps/web/out 静态导出）。
# 端口写死，避免和开发实例/其它会话抢；每次用独立临时数据目录，可重复跑。
$ErrorActionPreference = 'Stop'

$CtrlPort = 18111
$FakePort = 18112
$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$bundled = Join-Path $Repo '.runtime\python\python.exe'
if (Test-Path $bundled) {
    $Py = $bundled
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Py = 'python'
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Py = 'py'
} else {
    throw 'Python 3 not found (looked for .runtime\python\python.exe, python, py)'
}
$RunDir = Join-Path $PSScriptRoot ('.run\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$DataDir = Join-Path $RunDir 'data'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Test-PortBusy([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

foreach ($port in @($CtrlPort, $FakePort)) {
    if (Test-PortBusy $port) { throw "端口 $port 已被占用，先停掉占用进程再跑" }
}

function Wait-Url([string]$Url, [int]$Seconds, [string]$Name) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -lt 500) { return }
        } catch { }
        Start-Sleep -Milliseconds 800
    }
    throw "$Name 启动超时：$Url"
}

$env:UNIFIED_DATA_DIR = $DataDir
$env:UNIFIED_DB_PATH = Join-Path $DataDir 'unified.db'
$env:UNIFIED_ADMIN_USER = 'admin'
$env:UNIFIED_ADMIN_PASSWORD = 'test'
$env:CODEBUDDY_BASE = "http://127.0.0.1:$FakePort"
$env:OPENCODE_UPSTREAM_BASE = "http://127.0.0.1:$FakePort"
$env:UNIFIED_WEB_DIR = Join-Path $Repo 'apps\web\out'
$env:UNIFIED_ALLOW_PRIVATE_UPSTREAMS = '1'
$env:UNIFIED_ALLOW_ANONYMOUS_GATEWAY = 'false'
$env:GROK_REGISTER_EXECUTOR_URL = ''

$fake = $null
$ctrl = $null
$exitCode = 1
try {
    $fake = Start-Process -FilePath $Py `
        -ArgumentList @((Join-Path $Repo 'apps\control\scripts\fake_upstream.py'), $FakePort) `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $RunDir 'fake.log') `
        -RedirectStandardError (Join-Path $RunDir 'fake.err.log')

    $ctrl = Start-Process -FilePath $Py `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $CtrlPort, '--log-level', 'warning') `
        -WorkingDirectory (Join-Path $Repo 'apps\control') `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput (Join-Path $RunDir 'control.log') `
        -RedirectStandardError (Join-Path $RunDir 'control.err.log')

    Wait-Url "http://127.0.0.1:$FakePort/healthz" 30 '假上游'
    Wait-Url "http://127.0.0.1:$CtrlPort/healthz" 60 '控制面'
    Wait-Url "http://127.0.0.1:$CtrlPort/login/" 30 '前端静态页'

    & $Py (Join-Path $PSScriptRoot 'smoke_mainline.py') `
        --web-url "http://127.0.0.1:$CtrlPort" `
        --artifacts (Join-Path $PSScriptRoot 'artifacts')
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        Write-Host "e2e 失败，日志在 $RunDir"
    }
} finally {
    foreach ($p in @($ctrl, $fake)) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}

exit $exitCode
