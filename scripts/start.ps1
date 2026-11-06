#Requires -Version 5.1
<#
一键启动 / 停止（本机，非容器）。
默认：页面 18100（Node 静态服务）、控制面 18101；端口被占时自动顺延到下一个可用端口。

用法：
  powershell -File scripts\start.ps1                      # 启动
  powershell -File scripts\start.ps1 -Stop                # 停止（不需要记 PID）
  powershell -File scripts\start.ps1 -WebPort 18110 -ApiPort 18111

脚本会：检查 Python / 前端产物 / 控制面依赖 → 幂等建库补列 → 选端口
        → 起控制面并等 /healthz 通过 → 起静态前端并等首页 200 → 打印地址与账号。
重复执行是安全的：已有进程在跑时只提示，不会再起第二份。
#>
param(
    [switch]$Stop,
    [int]$WebPort = 18100,
    [Alias('Port')]
    [int]$ApiPort = 18101
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$runDir = Join-Path $root ".run"
$stateFile = Join-Path $runDir "start.json"
$ctrlDir = Join-Path $root "apps\control"
$serveScript = Join-Path $PSScriptRoot "serve-web.mjs"
$webLog = Join-Path $runDir "web.log"
$webErr = Join-Path $runDir "web.err.log"
$ctrlLog = Join-Path $runDir "control.log"
$ctrlErr = Join-Path $runDir "control.err.log"
$migrateLog = Join-Path $runDir "migrate.log"

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

function Fail([int]$Code, [string]$Title, [string]$Hint) {
    Write-Host ""
    Write-Host "启动失败：$Title" -ForegroundColor Red
    Write-Host "下一步：$Hint"
    exit $Code
}

function Read-RunState {
    if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
    try { return (Get-Content -Raw -LiteralPath $stateFile -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Test-Alive([int]$ProcessId) {
    if (-not $ProcessId) { return $false }
    try { $p = Get-Process -Id $ProcessId -ErrorAction Stop; return -not $p.HasExited } catch { return $false }
}

function Stop-ProcessTree([int]$ProcessId) {
    if (-not $ProcessId) { return }
    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
}

function Resolve-AgainstRoot([string]$Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) { return $Path }
    return (Join-Path $root $Path)
}

function Test-PortFree([int]$Port) {
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Find-FreePort([int]$Start, [int]$Skip) {
    for ($p = $Start; $p -lt $Start + 40; $p++) {
        if ($p -eq $Skip) { continue }
        if (Test-PortFree $p) { return $p }
    }
    return 0
}

function Test-Http([int]$Port, [string]$Path) {
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
        $request.Proxy = $null
        $request.Timeout = 2000
        $response = $request.GetResponse()
        $status = $response.StatusCode.value__
        $response.Close()
        return ($status -eq 200)
    } catch {
        return $false
    }
}

function Wait-Http([int]$Port, [string]$Path, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Http $Port $Path) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Show-Urls($state) {
    $webPort = $state.web_port
    $apiPort = $state.api_port
    Write-Host ""
    Write-Host "一键启动已完成" -ForegroundColor Green
    if ($webPort) {
        Write-Host ("  页面      http://127.0.0.1:{0}/" -f $webPort) -ForegroundColor Green
    } else {
        Write-Host ("  页面      http://127.0.0.1:{0}/（未装 Node，由控制面同端口托管）" -f $apiPort) -ForegroundColor Green
    }
    Write-Host ("  健康探针  http://127.0.0.1:{0}/healthz" -f $apiPort)
    Write-Host ("  API 入口  http://127.0.0.1:{0}/api/" -f $apiPort)
    Write-Host ("  停止      powershell -File scripts\start.ps1 -Stop")
    if ($state.account) {
        Write-Host ("  登录账号  {0}" -f $state.account)
        if ($state.password) { Write-Host ("  登录口令  {0}" -f $state.password) }
        Write-Host "  首次登录后请立刻改密（设置 → 用户），别把默认口令留在机器上。" -ForegroundColor Yellow
    }
    Write-Host "  日志      .run\control.err.log / .run\web.err.log"
    Write-Host ""
}

try {
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null

    # ── 停止：只认状态文件，不让用户记 PID ──
    if ($Stop) {
        $state = Read-RunState
        if (-not $state) {
            Write-Host "没有找到 .run\start.json：当前没有由本脚本启动的进程。"
            exit 0
        }
        $stopped = @()
        foreach ($id in @($state.web_pid, $state.api_pid)) {
            if ($id -and (Test-Alive ([int]$id))) {
                Stop-ProcessTree ([int]$id)
                $stopped += $id
            }
        }
        Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
        if ($stopped.Count -gt 0) {
            Write-Host ("已停止进程：{0}" -f ($stopped -join ", ")) -ForegroundColor Green
        } else {
            Write-Host "状态文件存在，但进程已经退出；已清理状态。" -ForegroundColor DarkGray
        }
        Write-Host ("释放端口：页面 {0}，控制面 {1}" -f $state.web_port, $state.api_port)
        exit 0
    }

    # ── 幂等：已经在跑就不再起第二份 ──
    $prev = Read-RunState
    if ($prev) {
        $apiAlive = Test-Alive ([int]$prev.api_pid)
        $webAlive = $prev.web_pid -and (Test-Alive ([int]$prev.web_pid))
        if ($apiAlive -and ($webAlive -or -not $prev.web_pid)) {
            Write-Host "已经在运行，未重复启动。" -ForegroundColor Yellow
            if ($prev.web_port) {
                Write-Host ("  页面：http://127.0.0.1:{0}/" -f $prev.web_port)
            }
            Write-Host ("  控制面：http://127.0.0.1:{0}/healthz" -f $prev.api_port)
            Write-Host "  停止：powershell -File scripts\start.ps1 -Stop"
            exit 0
        }
        Write-Host "发现上次运行残留（进程已退出），先清理状态再启动。" -ForegroundColor DarkYellow
        foreach ($id in @($prev.web_pid, $prev.api_pid)) {
            if ($id -and (Test-Alive ([int]$id))) { Stop-ProcessTree ([int]$id) }
        }
        Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
    }

    # ── Python ──
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
        Fail 3 "未找到 Python（需要 3.11+）" "装 Python 3.11+（https://www.python.org/downloads/），或用 `$env:PYTHON='<路径>' 指定后重试。"
    }
    & $py -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Fail 3 "Python 版本过低：$(& $py --version)" "控制面要求 Python 3.11+；升级后重试。"
    }
    Write-Host ("    {0}" -f (& $py --version))

    # ── .env（只补空缺，不覆盖外部显式变量）──
    $envFile = Join-Path $root ".env"
    if (Test-Path -LiteralPath $envFile) {
        Write-Step "读取 .env（不覆盖已存在的环境变量）"
        foreach ($line in Get-Content -LiteralPath $envFile) {
            $text = $line.Trim()
            if (-not $text -or $text.StartsWith("#")) { continue }
            $eq = $text.IndexOf("=")
            if ($eq -lt 1) { continue }
            $key = $text.Substring(0, $eq).Trim()
            $value = $text.Substring($eq + 1).Trim()
            if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { continue }
            if (-not (Get-Item "Env:$key" -ErrorAction SilentlyContinue)) { Set-Item "Env:$key" $value }
        }
    }

    if (-not $env:UNIFIED_DATA_DIR) { $env:UNIFIED_DATA_DIR = Join-Path $root "data" }
    if (-not $env:UNIFIED_DB_PATH) { $env:UNIFIED_DB_PATH = Join-Path $env:UNIFIED_DATA_DIR "unified.db" }
    if (-not $env:UNIFIED_WEB_DIR) { $env:UNIFIED_WEB_DIR = Join-Path $root "apps\web\out" }
    # .env 里常写相对路径（./apps/web/out）：统一按仓库根解析，避免受调用方当前目录影响
    $env:UNIFIED_DATA_DIR = Resolve-AgainstRoot $env:UNIFIED_DATA_DIR
    $env:UNIFIED_DB_PATH = Resolve-AgainstRoot $env:UNIFIED_DB_PATH
    $env:UNIFIED_WEB_DIR = Resolve-AgainstRoot $env:UNIFIED_WEB_DIR
    $webDir = $env:UNIFIED_WEB_DIR
    $dataDir = $env:UNIFIED_DATA_DIR

    # ── 前端产物 ──
    Write-Step "检查前端产物"
    $outIndex = Join-Path $webDir "index.html"
    if (-not (Test-Path -LiteralPath $outIndex)) {
        Fail 2 "前端产物不存在：$outIndex" ("先构建前端，再重跑本脚本：`n" +
            "  cd apps\web`n" +
            "  npm ci`n" +
            "  npm run build:export`n" +
            "没装 Node.js 的话先装 20+：https://nodejs.org/")
    }
    Write-Host ("    {0}" -f $webDir)

    # ── 控制面依赖 ──
    Write-Step "检查控制面依赖"
    # PS 5.1 下 2>&1 会把原生命令的 stderr 变成 ErrorRecord，Stop 模式下会直接跳 catch；
    # 这里临时放宽，靠退出码判断，错误原文留给下面的人工提示
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $probe = & $py -c "import fastapi, uvicorn, httpx, pydantic, yaml" 2>&1
    $probeCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($probeCode -ne 0) {
        $probeText = ($probe | Out-String)
        $missing = ($probeText -split "`n" | Where-Object { $_ -match "ModuleNotFoundError|No module named" } | Select-Object -First 1)
        $detail = if ($missing) { $missing.Trim() } else { "缺少 Python 包" }
        Fail 3 "控制面依赖缺失（$detail）" ("装依赖后重跑：`n" +
            "  & '$py' -m pip install -r apps\control\requirements.txt")
    }
    Write-Host "    依赖齐全"

    # ── 默认账号：只在全新库上播种 addxiaoyi / Lsc123456；已有库沿用现有口令 ──
    $freshCheck = @"
import json, os, sqlite3
path = os.environ["UNIFIED_DB_PATH"]
if not os.path.exists(path):
    print(json.dumps({"state": "fresh", "username": ""}))
else:
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT username FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
        print(json.dumps({"state": "existing", "username": row[0] if row else ""}))
    except Exception:
        print(json.dumps({"state": "unknown", "username": ""}))
"@
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $fresh = $freshCheck | & $py -
    $freshCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($freshCode -ne 0) {
        Fail 5 "数据库迁移失败（库文件打不开）" "确认 data 目录可写、库文件没有被另一个进程独占，或换一个 UNIFIED_DB_PATH 后重试。"
    }
    $dbState = $fresh | ConvertFrom-Json
    $usedDefaultPassword = $false
    if ($dbState.state -eq "fresh") {
        if (-not $env:UNIFIED_ADMIN_USER) { $env:UNIFIED_ADMIN_USER = "addxiaoyi" }
        if (-not $env:UNIFIED_ADMIN_PASSWORD) {
            $env:UNIFIED_ADMIN_PASSWORD = "Lsc123456"
            $usedDefaultPassword = $true
        }
    }

    # ── 建档 / 补列（幂等）──
    Write-Step "初始化数据库（幂等建表与补列）"
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $env:CTRL_DIR = $ctrlDir
    $initCode = "import os, sys; sys.path.insert(0, os.environ['CTRL_DIR']); from app.config import load_settings; from app.db import Database; s = load_settings(); Database(s.db_path); print(f'DB: {s.db_path}')"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $migrateOut = & $py -c $initCode 2>&1
    $migrateCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $migrateText = ($migrateOut | Out-String)
    [System.IO.File]::WriteAllText($migrateLog, $migrateText, (New-Object System.Text.UTF8Encoding($false)))
    if ($migrateCode -ne 0) {
        Fail 5 ("数据库迁移失败：$($env:UNIFIED_DB_PATH)`n" +
            "  常见原因：data 目录不可写、库文件被另一个进程占用、磁盘满。`n" +
            "  详细输出：.run\migrate.log") ("关闭占用该库的进程，或设置 UNIFIED_DB_PATH 指向可写的目录后重试。")
    }
    $migrateOut | ForEach-Object { Write-Host "    $_" }

    # ── 端口 ──
    Write-Step "选择端口"
    # 注意：PowerShell 变量名不区分大小写，$WebPort/$webPort 会互相覆盖，所以落点用 picked 前缀
    if ($WebPort -eq $ApiPort) { $ApiPort = $WebPort + 1 }
    $pickedWeb = Find-FreePort $WebPort 0
    if ($pickedWeb -eq 0) {
        Fail 4 ("端口全占：$WebPort 起连续 40 个端口都不可用") "先释放这些端口，或用 -WebPort 指定其他起始端口。"
    }
    $pickedApi = Find-FreePort $ApiPort $pickedWeb
    if ($pickedApi -eq 0) {
        Fail 4 ("端口全占：$ApiPort 起连续 40 个端口都不可用") "先释放这些端口，或用 -ApiPort 指定其他起始端口。"
    }
    if ($pickedWeb -ne $WebPort) {
        Write-Host ("    页面端口 {0} 被占用，改用 {1}" -f $WebPort, $pickedWeb) -ForegroundColor DarkYellow
    } else {
        Write-Host ("    页面端口 {0}" -f $pickedWeb)
    }
    if ($pickedApi -ne $ApiPort) {
        Write-Host ("    控制面端口 {0} 被占用，改用 {1}" -f $ApiPort, $pickedApi) -ForegroundColor DarkYellow
    } else {
        Write-Host ("    控制面端口 {0}" -f $pickedApi)
    }

    # ── 启动控制面 ──
    $env:UNIFIED_HOST = "127.0.0.1"
    $env:UNIFIED_PORT = "$pickedApi"
    Write-Step "启动控制面"
    $ctrlArgs = @("-m", "uvicorn", "--app-dir", "`"$ctrlDir`"", "app.main:app", "--host", "127.0.0.1", "--port", "$pickedApi")
    $ctrlProc = Start-Process -FilePath $py -ArgumentList $ctrlArgs -WorkingDirectory $ctrlDir `
        -RedirectStandardOutput $ctrlLog -RedirectStandardError $ctrlErr -PassThru -WindowStyle Hidden
    if (-not (Wait-Http $pickedApi "/healthz" 45)) {
        $tail = ""
        if (Test-Path -LiteralPath $ctrlErr) { $tail = (Get-Content -LiteralPath $ctrlErr -Tail 30 | Out-String) }
        Stop-ProcessTree $ctrlProc.Id
        if ($tail -match "address already in use|Only one usage of each socket address|WinError 10048") {
            Fail 6 ("控制面端口 {0} 刚被别的进程抢走" -f $pickedApi) "释放该端口，或换 -ApiPort 重新运行。"
        }
        if ($tail -match "ModuleNotFoundError|No module named") {
            Fail 3 "控制面依赖缺失（启动时导入失败）" "& '$py' -m pip install -r apps\control\requirements.txt 后重试；详情见 .run\control.err.log。"
        }
        if ($tail -match "sqlite|OperationalError|database is locked") {
            Fail 5 "数据库不可用（被占用或损坏）" "关闭占用数据库的进程；详情见 .run\control.err.log。"
        }
        Fail 6 "控制面在 45 秒内没有通过 /healthz" "看 .run\control.err.log 的前几行错误；修好后重跑本脚本。"
    }
    Write-Host ("    http://127.0.0.1:{0}/healthz 已通过" -f $pickedApi)

    # ── 启动静态前端 ──
    $webProc = $null
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        Write-Step "启动前端静态服务"
        $webArgs = @("`"$serveScript`"", "--port", "$pickedWeb", "--backend", "$pickedApi", "--dir", "`"$webDir`"")
        $webProc = Start-Process -FilePath $node.Source -ArgumentList $webArgs -WorkingDirectory $root `
            -RedirectStandardOutput $webLog -RedirectStandardError $webErr -PassThru -WindowStyle Hidden
        if (-not (Wait-Http $pickedWeb "/" 15)) {
            $webTail = ""
            if (Test-Path -LiteralPath $webErr) { $webTail = (Get-Content -LiteralPath $webErr -Tail 30 | Out-String) }
            Stop-ProcessTree $webProc.Id
            Stop-ProcessTree $ctrlProc.Id
            if ($webTail -match "EADDRINUSE") {
                Fail 7 ("页面端口 {0} 刚被别的进程抢走" -f $pickedWeb) "释放该端口，或换 -WebPort 重新运行。"
            }
            Fail 7 "前端静态服务启动失败" "详情见 .run\web.err.log；也可以先不跑它，直接在控制面端口访问页面。"
        }
        Write-Host ("    http://127.0.0.1:{0}/ 已可访问" -f $pickedWeb)
    } else {
        Write-Host "    未检测到 Node.js：跳过独立静态服务，页面改由控制面在 $pickedApi 同端口托管。" -ForegroundColor DarkYellow
        Write-Host "    要分开跑前端静态服务，请安装 Node.js 20+：https://nodejs.org/" -ForegroundColor DarkYellow
    }

    # ── 登录信息 ──
    $adminInfo = @"
import json, os, sqlite3
conn = sqlite3.connect(os.environ["UNIFIED_DB_PATH"])
row = conn.execute("SELECT username FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
print(json.dumps({"username": row[0] if row else ""}))
"@
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $adminJson = $adminInfo | & $py -
    $adminCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $account = ""
    if ($adminCode -eq 0) { $account = ($adminJson | ConvertFrom-Json).username }

    $passwordFile = Join-Path $dataDir "admin-password.txt"
    # 用户改过密码后，admin-password.txt 里的初始口令就失效了；验证一下，别把过期口令当成可用的
    $filePwdMatches = $null
    if (-not $usedDefaultPassword -and -not $env:UNIFIED_ADMIN_PASSWORD -and (Test-Path -LiteralPath $passwordFile)) {
        $verifySrc = @"
import json, os, sqlite3, sys
sys.path.insert(0, os.environ["CTRL_DIR"])
from app.security import verify_password
conn = sqlite3.connect(os.environ["UNIFIED_DB_PATH"])
row = conn.execute("SELECT password_hash FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
with open(os.environ["ADMIN_PW_FILE"], encoding="utf-8") as fh:
    pw = fh.read().strip()
print(json.dumps({"match": bool(row) and verify_password(pw, row[0])}))
"@
        $env:ADMIN_PW_FILE = $passwordFile
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $verifyOut = $verifySrc | & $py - 2>$null
        $verifyRc = $LASTEXITCODE
        $ErrorActionPreference = $prevEap
        if ($verifyRc -eq 0) { $filePwdMatches = ($verifyOut | ConvertFrom-Json).match }
    }
    if ($usedDefaultPassword) {
        $passwordText = "$($env:UNIFIED_ADMIN_PASSWORD)（快速开始默认口令）"
    } elseif ($env:UNIFIED_ADMIN_PASSWORD) {
        $passwordText = "$($env:UNIFIED_ADMIN_PASSWORD)（来自 .env / 环境变量）"
    } elseif (Test-Path -LiteralPath $passwordFile) {
        $filePassword = (Get-Content -Raw -LiteralPath $passwordFile -Encoding UTF8).Trim()
        if ($filePwdMatches -eq $true) {
            $passwordText = "$filePassword（来自 data\admin-password.txt）"
        } elseif ($filePwdMatches -eq $false) {
            $passwordText = "已改过，data\admin-password.txt 里的初始口令已失效，请用你当前设置的口令"
        } else {
            $passwordText = "$filePassword（来自 data\admin-password.txt；若你改过密码，以新口令为准）"
        }
    } else {
        $passwordText = "未知（库里已有管理员；用你设置过的口令，或设 UNIFIED_ADMIN_PASSWORD 后重启）"
    }

    $state = [ordered]@{
        started_at = (Get-Date).ToString("s")
        root = $root
        api_port = $pickedApi
        web_port = $(if ($webProc) { $pickedWeb } else { $null })
        api_pid = $ctrlProc.Id
        web_pid = $(if ($webProc) { $webProc.Id } else { $null })
        account = $account
        password = $passwordText
        python = $py
    }
    $json = $state | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($stateFile, $json, (New-Object System.Text.UTF8Encoding($false)))

    Show-Urls (New-Object psobject -Property @{
        web_port = $state.web_port
        api_port = $state.api_port
        account = $state.account
        password = $state.password
    })
} catch {
    Write-Host ""
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "下一步：看 .run 下的日志；修好后重跑 scripts\start.ps1。"
    exit 1
}
