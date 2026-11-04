#Requires -Version 5.1
<#
一键演示模式：起进程内假上游 + 控制面，并自动写入带 [demo] 标记的演示数据。
用法：
  powershell -File scripts\demo.ps1                 # 默认 127.0.0.1:8010
  powershell -File scripts\demo.ps1 -Port 18150     # 换端口
  powershell -File scripts\demo.ps1 -Clear          # 清除所有演示数据（不启动服务）
说明：
  - 强制 UNIFIED_DEMO_MODE=1 且只绑 127.0.0.1；非回环地址会直接启动失败。
  - 演示数据与真实数据隔离：provider=demo_local、密钥名以 [demo] 开头，详见 docs/DEMO.md。
  - 演示密钥在启动后写入 data\demo-credentials.txt，也可从设置项 demo_seed 里取。
#>
param(
    [int]$Port = 0,
    [switch]$Clear
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

function Import-DotEnv([string]$Path) {
    foreach ($line in Get-Content -LiteralPath $Path) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith("#")) { continue }
        $eq = $text.IndexOf("=")
        if ($eq -lt 1) { continue }
        $key = $text.Substring(0, $eq).Trim()
        $value = $text.Substring($eq + 1).Trim()
        if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
        # 外部已显式导出的变量优先，.env 只补空缺
        if (-not (Get-Item "Env:$key" -ErrorAction SilentlyContinue)) {
            Set-Item "Env:$key" $value
        }
    }
}

Write-Step "检查 Python 运行时"
$bundled = Join-Path $root ".runtime\python\python.exe"
$py = $null
foreach ($candidate in @($bundled, $env:PYTHON, "python", "python3")) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    if ($candidate -eq $bundled -and -not (Test-Path -LiteralPath $bundled)) { continue }
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) {
    throw "未找到 Python。请安装 Python 3.11+，或用 `$env:PYTHON = '<路径>'` 指定解释器。"
}
& $py -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 版本过低：$(& $py --version)。控制面要求 3.11+。"
}
Write-Host ("    {0}" -f (& $py --version))

$envFile = Join-Path $root ".env"
if (Test-Path -LiteralPath $envFile) {
    Write-Step "读取 $envFile（不覆盖已存在的环境变量）"
    Import-DotEnv $envFile
}

if (-not $env:UNIFIED_DATA_DIR) { $env:UNIFIED_DATA_DIR = Join-Path $root "data" }
if (-not $env:UNIFIED_DB_PATH) { $env:UNIFIED_DB_PATH = Join-Path $env:UNIFIED_DATA_DIR "unified.db" }
if (-not $env:UNIFIED_WEB_DIR) { $env:UNIFIED_WEB_DIR = Join-Path $root "apps\web\out" }
if (-not $env:UNIFIED_PORT) { $env:UNIFIED_PORT = "8010" }
if ($Port -gt 0) { $env:UNIFIED_PORT = "$Port" }

$ctrlDir = Join-Path $root "apps\control"

if ($Clear) {
    Write-Step "清除演示数据（provider=demo_local + [demo] 前缀密钥 + 策略/日志/流水）"
    Push-Location $ctrlDir
    try {
        $clearCode = "import sys; sys.path.insert(0, r'$ctrlDir'); from app.config import load_settings; from app.db import Database; from app.demo import clear_demo_data; s = load_settings(); print(clear_demo_data(Database(s.db_path), s))"
        & $py -c $clearCode
        if ($LASTEXITCODE -ne 0) { throw "清除失败（退出码 $LASTEXITCODE）" }
    } finally {
        Pop-Location
    }
    Write-Host "完成：只删除带 demo 标记的数据，其他行未改动。"
    exit 0
}

# 演示模式强制回环绑定：不跟随 .env / 外部环境里的 host
$env:UNIFIED_DEMO_MODE = "1"
$env:UNIFIED_HOST = "127.0.0.1"

New-Item -ItemType Directory -Force -Path $env:UNIFIED_DATA_DIR | Out-Null

Write-Step "初始化数据库（幂等建表）"
Push-Location $ctrlDir
try {
    $initCode = "import sys; sys.path.insert(0, r'$ctrlDir'); from app.config import load_settings; from app.db import Database; s = load_settings(); Database(s.db_path); print(f'    DB: {s.db_path}')"
    & $py -c $initCode
    if ($LASTEXITCODE -ne 0) { throw "数据库初始化失败（退出码 $LASTEXITCODE）" }
} finally {
    Pop-Location
}

$adminPassword = $env:UNIFIED_ADMIN_PASSWORD
if (-not $adminPassword) {
    $passFile = Join-Path $env:UNIFIED_DATA_DIR "admin-password.txt"
    if (Test-Path -LiteralPath $passFile) { $adminPassword = (Get-Content -LiteralPath $passFile -Raw).Trim() }
}

$baseUrl = "http://127.0.0.1:$($env:UNIFIED_PORT)"
$keyFile = Join-Path $env:UNIFIED_DATA_DIR "demo-credentials.txt"

Write-Host ""
Write-Host "==================================================" -ForegroundColor Yellow
Write-Host " 这是演示数据，不要用于生产" -ForegroundColor Yellow
Write-Host " 假上游只在本机进程内运行；演示 provider 不允许对公网开放" -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host ("页面地址: {0}/" -f $baseUrl) -ForegroundColor Green
Write-Host ("健康探针: {0}/api/health/ready" -f $baseUrl) -ForegroundColor Green
Write-Host ("网关地址: {0}/v1" -f $baseUrl) -ForegroundColor Green
if ($adminPassword) { Write-Host ("管理员口令: {0}" -f $adminPassword) -ForegroundColor Green }
Write-Host ("演示密钥: {0}（启动后生成）" -f $keyFile)
Write-Host ("模型: fake-chat-1 / fake-chat-2（假上游固定返回 [fake] 前缀内容）")
Write-Host "清理演示数据: powershell -File scripts\demo.ps1 -Clear"
Write-Host "按 Ctrl+C 停止。`n"

Write-Step "启动控制面（UNIFIED_DEMO_MODE=1）"
Push-Location $ctrlDir
try {
    & $py -m uvicorn --app-dir $ctrlDir app.main:app --host $env:UNIFIED_HOST --port $env:UNIFIED_PORT
} finally {
    Pop-Location
}
