# 探 /healthz 与静态首页；任一非 200 则退出码 1。
# 用法：
#   powershell -File deploy\healthcheck.ps1 -BaseUrl https://api.example.com
#   powershell -File deploy\healthcheck.ps1 -BaseUrl https://localhost -Insecure   # 自签名证书调试
param(
  [string]$BaseUrl = "https://localhost",
  [int]$TimeoutSec = 8,
  [switch]$Insecure
)

$ErrorActionPreference = "Stop"
$base = $BaseUrl.TrimEnd('/')

function Get-StatusCode([string]$url) {
    $req = @{
        Uri = $url
        Method = 'Get'
        TimeoutSec = $TimeoutSec
        UseBasicParsing = $true
    }
    if ($Insecure -and $PSVersionTable.PSVersion.Major -ge 7) {
        $req.SkipCertificateCheck = $true
    }
    try {
        return [int](Invoke-WebRequest @req).StatusCode
    } catch {
        $resp = $_.Exception.Response
        if ($resp -and $resp.StatusCode) { return [int]$resp.StatusCode }
        return 0
    }
}

if ($Insecure -and $PSVersionTable.PSVersion.Major -lt 7) {
    Write-Warning "-Insecure 需要 PowerShell 7+（当前 $($PSVersionTable.PSVersion)），自签名证书会校验失败"
}

$failed = $false
foreach ($target in @(
    @{ Label = 'healthz'; Url = "$base/healthz" },
    @{ Label = 'homepage'; Url = "$base/" }
)) {
    $code = Get-StatusCode $target.Url
    if ($code -eq 200) {
        Write-Host ("OK   {0,-8} {1} 200" -f $target.Label, $target.Url) -ForegroundColor Green
    } else {
        Write-Host ("FAIL {0,-8} {1} status={2}" -f $target.Label, $target.Url, $code) -ForegroundColor Red
        $failed = $true
    }
}

if ($failed) { exit 1 }
exit 0
