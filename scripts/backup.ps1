<#
.SYNOPSIS
  备份 unified-2api：生成单文件归档（库快照 + 配置 + 版本元数据 + sha256）。
.DESCRIPTION
  实际工作全部在 scripts/backup_core.py，本脚本只负责找 Python、拼参数。
  Linux 侧 scripts/backup.sh 调用同一个核心，两个平台语义一致。

  归档是 unified-backup-<时间戳>.zip，内含：
    manifest.json          版本、时间戳、sha256、每表行数、demo 剔除记录
    db/unified.db          SQLite 在线快照（VACUUM INTO，不直接拷贝正在写的库）
    config/ providers/ upstreams/   仓库配置
  同目录另有 <归档>.sha256，恢复时用来校验归档本身。

  演示数据（provider=demo_local、密钥名以 "[demo] " 开头）默认剔除并在清单里记录；
  确实要保留时加 -IncludeDemo。
.PARAMETER DbPath
  数据库路径，默认 <repo>\data\unified.db
.PARAMETER OutDir
  归档目录，默认 <repo>\data\backups（data\ 已被 .gitignore 覆盖）
.PARAMETER Keep
  保留最近 N 份归档与 pre-restore 快照，默认 14
.PARAMETER IncludeDemo
  保留演示数据（默认剔除）
.PARAMETER UseContainer
  数据在命名卷时用：在 control 容器内做快照，再 docker compose cp 出来交给核心打包
.EXAMPLE
  powershell -File scripts\backup.ps1
.EXAMPLE
  powershell -File scripts\backup.ps1 -Keep 7
.EXAMPLE
  powershell -File scripts\backup.ps1 -UseContainer
#>
param(
    [string]$DbPath = "",
    [string]$OutDir = "",
    [int]$Keep = 14,
    [switch]$IncludeDemo,
    [switch]$UseContainer
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $DbPath) { $DbPath = Join-Path $repo "data\unified.db" }
if (-not $OutDir) { $OutDir = Join-Path $repo "data\backups" }
$core = Join-Path $PSScriptRoot "backup_core.py"

function Resolve-Python {
    $runtime = Join-Path $repo ".runtime\python\python.exe"
    if (Test-Path $runtime) { return @($runtime) }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return @($cmd.Source) }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { return @($launcher.Source, "-3") }
    throw "未找到 Python 3（查过 .runtime\python\python.exe、python、py）"
}

$runner = @(Resolve-Python)
$exe = [string]$runner[0]
$prefix = @()
if ($runner.Count -gt 1) { $prefix = @($runner[1..($runner.Count - 1)]) }

try {
    if ($Keep -lt 1) { throw "Keep 必须 >= 1（保留份数）" }
    if (-not (Test-Path $OutDir)) { $null = New-Item -ItemType Directory -Path $OutDir -Force }

    if (-not $UseContainer) {
        $args = @($prefix) + @($core, "backup", "--db", $DbPath, "--out-dir", $OutDir, "--keep", "$Keep")
        if ($IncludeDemo) { $args += "--include-demo" }
        & $exe @args
        exit $LASTEXITCODE
    }

    $compose = Join-Path $repo "deploy\docker-compose.prod.yml"
    if (-not (Test-Path $compose)) { throw "找不到 deploy\docker-compose.prod.yml" }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $snapshot = Join-Path $OutDir "unified-snapshot-$stamp.db"
    $inner = "/data/.unified-backup-$stamp.db"

    # 用 stdin 把脚本喂给容器里的 python，避免 PowerShell 5.1 剥掉 -c 参数里的引号
    $snapshotPy = @'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(src, timeout=30)
try:
    conn.execute("VACUUM INTO ?", (dst,))
finally:
    conn.close()
'@
    $snapshotPy | & docker compose -f $compose exec -T control python - /data/unified.db $inner
    if ($LASTEXITCODE -ne 0) { throw "容器内快照失败（control 是否在运行？）" }

    & docker compose -f $compose cp "control:$inner" $snapshot | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "从容器导出快照失败" }
    & docker compose -f $compose exec -T control rm -f $inner | Out-Null

    try {
        $args = @($prefix) + @($core, "pack", "--snapshot", $snapshot, "--out-dir", $OutDir,
            "--keep", "$Keep", "--method", "container-vacuum-into", "--source-label", "/data/unified.db")
        if ($IncludeDemo) { $args += "--include-demo" }
        & $exe @args
        $code = $LASTEXITCODE
    } finally {
        if (Test-Path $snapshot) { Remove-Item -LiteralPath $snapshot -Force }
    }
    exit $code
} catch {
    Write-Error $_
    exit 1
}
