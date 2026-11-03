<#
.SYNOPSIS
  在线备份 unified.db（WAL 模式下取一致性快照），支持压缩与按份数轮转。
.DESCRIPTION
  不能用文件复制：WAL 模式下 unified.db 与 unified.db-wal 可能不同步，直接 cp 只能拿到半写状态。
  本脚本按优先级走 SQLite 官方备份路径：
    1) 宿主机有 sqlite3 CLI → 执行 .backup（在线安全）
    2) 否则调用 Python（py 启动器 / python / 仓库 .runtime\python）→ Connection.backup()
  生产环境数据在命名卷里，用 -UseContainer 让 control 容器内部备份，再 docker compose cp 取出。
  备份完成后跑 PRAGMA integrity_check，不通过即判失败。

.PARAMETER DbPath
  宿主机数据库路径，默认 <repo>\data\unified.db
.PARAMETER OutDir
  备份目录，默认 <repo>\backups
.PARAMETER Keep
  保留最近 N 份（.db 与 .db.zip 一起计数），默认 14
.PARAMETER Compress
  备份后压成 zip 并删除原始 .db
.PARAMETER UseContainer
  通过 deploy/docker-compose.prod.yml 在 control 容器内备份（数据在命名卷时用这个）
.EXAMPLE
  powershell -File scripts\backup.ps1 -Keep 7 -Compress
.EXAMPLE
  powershell -File scripts\backup.ps1 -UseContainer -Compress

恢复步骤（务必先停服务，避免恢复中被写入）：
  1. 停控制面：docker compose -f deploy/docker-compose.prod.yml stop control
  2. 删除数据目录里的 unified.db、unified.db-wal、unified.db-shm（旧 WAL 不清会污染新库）
  3. 解压：Expand-Archive <备份>.zip -DestinationPath <临时目录>（未压缩的跳过）
  4. 把 unified-*.db 改名为 unified.db 放回数据目录
     宿主机部署 → <repo>\data\
     命名卷部署 → docker compose -f deploy/docker-compose.prod.yml cp <文件> control:/data/unified.db
  5. 启动：docker compose -f deploy/docker-compose.prod.yml up -d control
  6. 验证：deploy\healthcheck.ps1，并登录后台确认密钥、账号、日志都在
#>
param(
    [string]$DbPath = "",
    [string]$OutDir = "",
    [int]$Keep = 14,
    [switch]$Compress,
    [switch]$UseContainer
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $DbPath) { $DbPath = Join-Path $repo "data\unified.db" }
if (-not $OutDir) { $OutDir = Join-Path $repo "backups" }

function Get-PythonRunner {
    $runtime = Join-Path $repo ".runtime\python\python.exe"
    if (Test-Path $runtime) { return @{ File = $runtime; Prefix = @() } }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @{ File = $py.Source; Prefix = @("-3") } }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @{ File = $python.Source; Prefix = @() } }
    return $null
}

$pythonBackup = @'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
check = sqlite3.connect(sys.argv[2])
result = check.execute("PRAGMA integrity_check").fetchone()[0]
check.close()
sys.exit(0 if result == "ok" else 1)
'@

try {
    if ($Keep -lt 1) { throw "Keep 必须 >= 1（保留份数）" }
    if (-not (Test-Path $OutDir)) { $null = New-Item -ItemType Directory -Path $OutDir -Force }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $produced = Join-Path $OutDir "unified-$stamp.db"

    if ($UseContainer) {
        $compose = Join-Path $repo "deploy\docker-compose.prod.yml"
        if (-not (Test-Path $compose)) { throw "找不到 deploy\docker-compose.prod.yml" }

        $inner = "/data/unified-$stamp.db"
        # 用 stdin 把脚本喂给容器里的 python，避免 PowerShell 5.1 把 -c 参数里的引号剥掉
        $dockerArgs = @("compose", "-f", $compose, "exec", "-T", "control",
            "python", "-", "/data/unified.db", $inner)
        $pythonBackup | & docker @dockerArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "容器内备份失败（control 是否在运行？）" }

        & docker compose -f $compose cp "control:$inner" $produced | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "从容器导出备份失败" }

        & docker compose -f $compose exec -T control rm -f $inner | Out-Null
        Write-Host "container backup -> $produced" -ForegroundColor Cyan
    } else {
        if (-not (Test-Path $DbPath)) { throw "数据库不存在：$DbPath" }

        $sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
        if ($sqlite) {
            # .backup 由 SQLite 自己处理 WAL 与并发写，等价于在线快照
            & $sqlite.Source $DbPath ".backup '$produced'"
            if ($LASTEXITCODE -ne 0) { throw "sqlite3 .backup 失败" }

            $check = (& $sqlite.Source $produced "PRAGMA integrity_check;") -join ""
            if ($check -ne "ok") { throw "备份完整性校验失败：$check" }
        } else {
            $runner = Get-PythonRunner
            if (-not $runner) {
                throw "未找到 sqlite3 CLI 或 Python；请安装其中之一，或在容器运行时改用 -UseContainer"
            }
            $callArgs = @($runner.Prefix) + @("-", $DbPath, $produced)
            $pythonBackup | & $runner.File @callArgs
            if ($LASTEXITCODE -ne 0) { throw "Python SQLite 备份或完整性校验失败" }
        }
        Write-Host "file backup -> $produced" -ForegroundColor Cyan
    }

    if ($Compress) {
        $zip = "$produced.zip"
        Compress-Archive -Path $produced -DestinationPath $zip -CompressionLevel Optimal -Force
        Remove-Item -Path $produced -Force
        $produced = $zip
    }

    # 只清理本脚本自己生成的备份，不碰目录里其他文件
    $pattern = "^unified-\d{8}-\d{6}\.db(\.zip)?$"
    Get-ChildItem -Path $OutDir -File |
        Where-Object { $_.Name -match $pattern } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $Keep |
        Remove-Item -Force

    $kept = @(Get-ChildItem -Path $OutDir -File | Where-Object { $_.Name -match $pattern }).Count
    $sizeMb = (Get-Item $produced).Length / 1MB
    Write-Host ("done: {0} ({1:N2} MB), kept {2} of max {3}" -f $produced, $sizeMb, $kept, $Keep) -ForegroundColor Green
} catch {
    Write-Error $_
    exit 1
}
