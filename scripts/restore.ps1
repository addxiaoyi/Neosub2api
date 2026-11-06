<#
.SYNOPSIS
  从单文件归档恢复 unified-2api：校验 sha256 → 留存当前库 → 原子替换 → 只读自检。
.DESCRIPTION
  实际工作全部在 scripts/backup_core.py，本脚本只负责找 Python、拼参数。

  恢复顺序（任何一步不通过就停止，不碰目标库）：
    1. 校验归档：.sha256 旁注存在则比对归档本身，再逐个成员比对 manifest 里的 sha256
    2. 留存现场：当前 unified.db（含 -wal/-shm）原样拷到 <BackupsDir>\pre-restore-<时间戳>\unified.db.bak
    3. 原子替换：staged 临时文件 → 目标路径，同时清掉旧库残留的 -wal/-shm
    4. 只读自检：可查询、表齐全且数量与清单一致、每表行数与清单一致、
       至少一个 role=admin 的账号、integrity_check=ok、无演示数据残留
    5. 自检失败自动回滚到 unified.db.bak；-ReadyUrl 可再轮询 /api/health/ready
  恢复前必须停服务（Windows 上打开中的库文件无法替换）。

  -WithConfig 才会覆盖仓库的 config/、providers/、upstreams/；默认只恢复数据库。
.PARAMETER Archive
  归档文件（unified-backup-<时间戳>.zip），别名 -Backup
.PARAMETER Latest
  不给 -Archive 时取备份目录里最新一份
.PARAMETER DbPath
  目标库路径，默认 <repo>\data\unified.db
.PARAMETER BackupsDir
  pre-restore 快照目录，默认 <repo>\data\backups
.PARAMETER WithConfig
  同时恢复 config/、providers/、upstreams/
.PARAMETER ReadyUrl
  恢复后要验证的就绪地址，如 http://127.0.0.1:8010/api/health/ready
.PARAMETER RequireSidecar
  归档没有 .sha256 旁注时直接失败（默认降级为 manifest 成员校验并告警）
.PARAMETER Force
  跳过「库仍被占用」的前置检查强行替换；正常停服后恢复不需要它
.EXAMPLE
  powershell -File scripts\restore.ps1 -Latest
.EXAMPLE
  powershell -File scripts\restore.ps1 -Archive data\backups\unified-backup-20261103-190000.zip -ReadyUrl http://127.0.0.1:8010/api/health/ready
#>
param(
    [Alias("Backup")]
    [string]$Archive = "",
    [switch]$Latest,
    [string]$DbPath = "",
    [string]$BackupsDir = "",
    [string]$RepoRoot = "",
    [switch]$WithConfig,
    [string]$ReadyUrl = "",
    [int]$ReadyTimeout = 20,
    [switch]$RequireSidecar,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $DbPath) { $DbPath = Join-Path $repo "data\unified.db" }
if (-not $BackupsDir) { $BackupsDir = Join-Path $repo "data\backups" }
if (-not $RepoRoot) { $RepoRoot = $repo }
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
    if (-not $Archive) {
        if (-not $Latest) { throw "必须给 -Archive <文件> 或 -Latest" }
        $args = @($prefix) + @($core, "latest", "--out-dir", $BackupsDir)
        $Archive = (& $exe @args | Select-Object -Last 1).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $Archive) { throw "备份目录里没有可用归档：$BackupsDir" }
        Write-Host "latest archive: $Archive" -ForegroundColor Cyan
    }

    if (-not (Test-Path $Archive)) { throw "归档不存在：$Archive" }
    if (-not (Test-Path $BackupsDir)) { $null = New-Item -ItemType Directory -Path $BackupsDir -Force }

    $args = @($prefix) + @($core, "restore", "--archive", $Archive, "--db", $DbPath,
        "--backups", $BackupsDir, "--repo", $RepoRoot, "--ready-timeout", "$ReadyTimeout")
    if ($WithConfig) { $args += "--with-config" }
    if ($ReadyUrl) { $args += @("--ready-url", $ReadyUrl) }
    if ($RequireSidecar) { $args += "--require-sidecar" }
    if ($Force) { $args += "--force" }
    & $exe @args
    exit $LASTEXITCODE
} catch {
    Write-Error $_
    exit 1
}
