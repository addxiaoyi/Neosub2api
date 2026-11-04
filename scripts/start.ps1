#Requires -Version 5.1
<#
启动控制面（本地运行，非容器）。前端静态产物 apps/web/out 由控制面直接托管。
用法：
  powershell -File scripts\start.ps1                 # 默认 127.0.0.1:8010
  powershell -File scripts\start.ps1 -Port 18109     # 换端口（调试/避让占用）
  $env:PYTHON = "C:\path\python.exe"; powershell -File scripts\start.ps1
说明：
  - 口令不写死：优先读仓库根 .env 的 UNIFIED_ADMIN_PASSWORD；没设则首次启动随机生成并写入 data\admin-password.txt。
  - 只做本机开发/自托管启动；容器方式见 deploy/docker-compose.yml。
#>
param(
    [int]$Port = 0
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
if (-not $env:UNIFIED_HOST) { $env:UNIFIED_HOST = "127.0.0.1" }
if ($Port -gt 0) { $env:UNIFIED_PORT = "$Port" } elseif (-not $env:UNIFIED_PORT) { $env:UNIFIED_PORT = "8010" }
if (-not $env:UNIFIED_WEB_DIR) { $env:UNIFIED_WEB_DIR = Join-Path $root "apps\web\out" }

New-Item -ItemType Directory -Force -Path $env:UNIFIED_DATA_DIR | Out-Null

$ctrlDir = Join-Path $root "apps\control"

Write-Step "初始化数据库（幂等建表）"
Push-Location $ctrlDir
try {
    # 显式把 app 包目录塞进 sys.path：嵌入式 Python 的 _pth 会忽略 PYTHONPATH 与 cwd
    $initCode = "import sys; sys.path.insert(0, r'$ctrlDir'); from app.config import load_settings; from app.db import Database; s = load_settings(); Database(s.db_path); print(f'    DB: {s.db_path}')"
    & $py -c $initCode
    if ($LASTEXITCODE -ne 0) { throw "数据库初始化失败（退出码 $LASTEXITCODE）" }
} finally {
    Pop-Location
}

$outIndex = Join-Path $root "apps\web\out\index.html"
if (-not (Test-Path -LiteralPath $outIndex)) {
    Write-Warning "未找到前端产物 apps\web\out；控制面只提供 API，页面会是提示 JSON。"
    Write-Host "    构建前端：cd apps\web; npm ci; npm run build:export"
}
elseif (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "    提示：未检测到 Node.js；仅影响重新构建前端，已有 out 可直接使用。" -ForegroundColor DarkGray
}

$displayHost = if ($env:UNIFIED_HOST -eq "0.0.0.0") { "127.0.0.1" } else { $env:UNIFIED_HOST }
Write-Host ""
Write-Host ("前端页面: http://{0}:{1}/" -f $displayHost, $env:UNIFIED_PORT) -ForegroundColor Green
Write-Host ("健康探针: http://{0}:{1}/healthz" -f $displayHost, $env:UNIFIED_PORT) -ForegroundColor Green
Write-Host ("API 入口: http://{0}:{1}/api/" -f $displayHost, $env:UNIFIED_PORT) -ForegroundColor Green
Write-Host "按 Ctrl+C 停止。`n"

Write-Step "启动控制面"
Push-Location $ctrlDir
try {
    & $py -m uvicorn --app-dir $ctrlDir app.main:app --host $env:UNIFIED_HOST --port $env:UNIFIED_PORT
} finally {
    Pop-Location
}
