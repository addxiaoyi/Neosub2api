<#
.SYNOPSIS
  比对 upstreams/upstreams.yaml 中记录的 analyzed_commit 与上游仓库当前 HEAD。

.DESCRIPTION
  优先使用 `git ls-remote`，失败时回退到 GitHub REST API。
  输出三态：一致 / 有更新 / 无法访问。
  默认只读；-Update 时把有更新且可访问的记录改写到新 HEAD。

.PARAMETER Manifest
  清单路径。默认取脚本上级目录的 upstreams/upstreams.yaml。

.PARAMETER Update
  确认跟进后更新 analyzed_commit 与 analyzed_at（仅限有更新且可访问的记录）。

.PARAMETER Json
  输出单行 JSON，便于 CI 解析。

.EXAMPLE
  powershell -File scripts/check-upstreams.ps1

.EXAMPLE
  powershell -File scripts/check-upstreams.ps1 -Update

.EXAMPLE
  powershell -File scripts/check-upstreams.ps1 -Json
#>
[CmdletBinding()]
param(
    [string]$Manifest,
    [switch]$Update,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

# PowerShell 5.1 默认不启用 TLS 1.2，GitHub API 会直接握手失败。
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    # 老系统上枚举值可能不存在，忽略即可。
}

if (-not $Manifest) {
    $Manifest = Join-Path (Split-Path -Parent $PSScriptRoot) 'upstreams\upstreams.yaml'
}
if (-not (Test-Path -LiteralPath $Manifest)) {
    Write-Error "找不到清单文件：$Manifest"
    exit 1
}
$Manifest = (Resolve-Path -LiteralPath $Manifest).Path

function Read-Manifest {
    param([string]$Path)

    $text = [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)
    $records = New-Object System.Collections.ArrayList
    $current = $null

    for ($i = 0; $i -lt $text.Count; $i++) {
        $line = $text[$i]

        if ($line -match '^\s*-\s+repo:\s*([^\s#]+)\s*$') {
            $current = [pscustomobject]@{
                Repo           = $Matches[1]
                AnalyzedCommit = $null
                AnalyzedAt     = $null
                CommitLine     = -1
                AtLine         = -1
            }
            [void]$records.Add($current)
            continue
        }

        if ($null -eq $current) { continue }

        if ($line -match '^    analyzed_commit:\s*([^\s#]+)') {
            $current.AnalyzedCommit = $Matches[1]
            $current.CommitLine = $i
            continue
        }
        if ($line -match '^    analyzed_at:\s*([^\s#]+)') {
            $current.AnalyzedAt = $Matches[1]
            $current.AtLine = $i
        }
    }

    return @{ Lines = $text; Records = $records }
}

function Get-RemoteHead {
    param([string]$Repo)

    $url = "https://github.com/$Repo.git"
    $git = Get-Command git -ErrorAction SilentlyContinue

    if ($git) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $code = 1
        # 网络抖动会让 ls-remote 偶发 exit 128，重试一次再判定不可访问。
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            $raw = @(& $git.Source ls-remote --exit-code $url HEAD 2>&1)
            $code = $LASTEXITCODE
            if ($code -eq 0) {
                $ErrorActionPreference = $previous
                foreach ($entry in $raw) {
                    $line = [string]$entry
                    if ($line -match '^([0-9a-f]{40})\s+HEAD\s*$') {
                        return @{ Sha = $Matches[1].ToLower(); Source = 'git'; Error = $null }
                    }
                }
                return @{ Sha = $null; Source = 'git'; Error = 'git 返回成功但未解析到 HEAD' }
            }
        }
        $ErrorActionPreference = $previous
        $gitError = 'git ls-remote 失败（exit ' + $code + '）'
    } else {
        $gitError = '未安装 git'
    }

    # 兜底：GitHub REST API。
    $headers = @{
        'User-Agent' = 'unified-2api-upstream-watch'
        'Accept'     = 'application/vnd.github+json'
    }
    if ($env:GITHUB_TOKEN) {
        $headers['Authorization'] = "Bearer $($env:GITHUB_TOKEN)"
    }

    try {
        $resp = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/HEAD" -Headers $headers -TimeoutSec 20
        if ($resp -and $resp.sha) {
            return @{ Sha = ([string]$resp.sha).ToLower(); Source = 'api'; Error = $null }
        }
        return @{ Sha = $null; Source = 'api'; Error = 'API 返回内容缺少 sha' }
    } catch {
        return @{ Sha = $null; Source = 'api'; Error = $gitError + '；GitHub API 也失败：' + $_.Exception.Message }
    }
}

function Get-BehindCount {
    param([string]$Repo, [string]$Base, [string]$Head)

    if (-not $env:GITHUB_TOKEN) {
        # 匿名请求容易触发限流，compare 只作为锦上添花，失败即返回 $null。
    }

    $headers = @{
        'User-Agent' = 'unified-2api-upstream-watch'
        'Accept'     = 'application/vnd.github+json'
    }
    if ($env:GITHUB_TOKEN) {
        $headers['Authorization'] = "Bearer $($env:GITHUB_TOKEN)"
    }

    $uri = "https://api.github.com/repos/$Repo/compare/$Base...$Head"
    try {
        $resp = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 20
        if ($null -ne $resp.ahead_by) {
            return [int]$resp.ahead_by
        }
    } catch {
        return $null
    }
    return $null
}

$manifestData = Read-Manifest -Path $Manifest
$lines = $manifestData.Lines
$records = $manifestData.Records

if ($records.Count -eq 0) {
    Write-Error "清单中没有解析到任何 repo 记录：$Manifest"
    exit 1
}

$results = New-Object System.Collections.ArrayList
$today = (Get-Date).ToString('yyyy-MM-dd')

foreach ($record in $records) {
    $head = Get-RemoteHead -Repo $record.Repo
    $baseline = [string]$record.AnalyzedCommit

    $item = [pscustomobject]@{
        repo            = $record.Repo
        status          = 'unreachable'
        analyzed_commit = $baseline
        latest_commit   = $head.Sha
        behind          = $null
        message         = ''
    }

    if (-not $head.Sha) {
        $item.status = 'unreachable'
        $item.message = '未获取到远端 HEAD（' + $head.Error + '）'
        [void]$results.Add($item)
        continue
    }

    if ($baseline -eq 'unknown' -or -not $baseline) {
        $item.status = 'outdated'
        $item.message = '基线为 unknown，无法比对，需人工确认'
        [void]$results.Add($item)
        continue
    }

    if ($baseline.ToLower() -eq $head.Sha) {
        $item.status = 'ok'
        $item.message = '与基线一致（' + $head.Source + '）'
        [void]$results.Add($item)
        continue
    }

    $item.status = 'outdated'
    $item.behind = Get-BehindCount -Repo $record.Repo -Base $baseline -Head $head.Sha
    if ($null -ne $item.behind) {
        $item.message = '落后 ' + $item.behind + ' 个 commit，最新 ' + $head.Sha
    } else {
        $item.message = '基线已变化，最新 ' + $head.Sha + '（无法取得落后数）'
    }
    [void]$results.Add($item)
}

if ($Update) {
    $advanced = @{}
    foreach ($r in $results) {
        if ($r.status -eq 'outdated' -and $r.latest_commit -and $r.analyzed_commit -ne 'unknown') {
            $advanced[$r.repo] = $r.latest_commit
        }
    }

    if ($advanced.Count -gt 0) {
        foreach ($record in $records) {
            if (-not $advanced.ContainsKey($record.Repo)) { continue }
            $sha = $advanced[$record.Repo]
            if ($record.CommitLine -ge 0) {
                $lines[$record.CommitLine] = '    analyzed_commit: ' + $sha
            }
            if ($record.AtLine -ge 0) {
                $lines[$record.AtLine] = '    analyzed_at: ' + $today
            }
        }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines($Manifest, $lines, $utf8)
    }
}

if ($Json) {
    $payload = @($results | ForEach-Object {
        [pscustomobject]@{
            repo            = $_.repo
            status          = $_.status
            analyzed_commit = $_.analyzed_commit
            latest_commit   = $_.latest_commit
            behind          = $_.behind
            message         = $_.message
        }
    })
    $jsonText = ConvertTo-Json -InputObject $payload -Depth 4 -Compress
    Write-Output $jsonText
} else {
    foreach ($r in $results) {
        $tag = ''
        if ($r.status -eq 'ok') { $tag = '一致    ' }
        elseif ($r.status -eq 'outdated') { $tag = '有更新  ' }
        else { $tag = '无法访问' }

        $base = $r.analyzed_commit
        if ($base -and $base.Length -ge 12) { $base = $base.Substring(0, 12) }
        Write-Host ('[{0}] {1,-42} {2}  {3}' -f $tag, $r.repo, $base, $r.message)
    }

    $okCount = @($results | Where-Object { $_.status -eq 'ok' }).Count
    $outCount = @($results | Where-Object { $_.status -eq 'outdated' }).Count
    $badCount = @($results | Where-Object { $_.status -eq 'unreachable' }).Count
    Write-Host ''
    Write-Host ('共 {0} 个仓库：一致 {1}，有更新 {2}，无法访问 {3}' -f $results.Count, $okCount, $outCount, $badCount)

    if ($Update) {
        if ($advanced.Count -gt 0) {
            Write-Host ('已更新 {0} 条基线：{1}' -f $advanced.Count, ($advanced.Keys -join ', '))
        } else {
            Write-Host '没有需要更新基线的记录。'
        }
    }
}
