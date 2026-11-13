# QIO 0.3.2 集成轮 · 真实界面验收 launcher（ui-verify）
#
# 用法（先让服务起来；推荐先手动跑 run.ps1 -KeepRunning）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File run-ui.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File run-ui.ps1 -Setup        # 自动调 run.ps1 -KeepRunning（耗时长）
#   powershell -NoProfile -ExecutionPolicy Bypass -File run-ui.ps1 -Only s1,s3   # 只跑指定场景
#   powershell -NoProfile -ExecutionPolicy Bypass -File run-ui.ps1 -SkipCleanup  # 保留服务，便于手工复核
#
# 默认行为：
#   1) 校验 backend 8734 /api/health、vite 5199、fake provider 8807 /__health 都已就绪；
#   2) 调用同目录 ui-verify.mjs（自己 launch Edge + CDP，不依赖 Playwright）；
#   3) 收尾：e2e_down.py 停 backend/vite + 结束 provider 端口进程。
#
# 注意：不要与另一个 run.ps1 / 另一个 ui-verify 并发，它们抢 8734/5199/8807/9333。
param(
  [switch]$Setup,
  [switch]$SkipCleanup,
  [string]$Only = "",
  [string]$OutDir = "",
  [string]$Worktree = "D:/qio-dev/pm-qio032-integration",
  [int]$CdpPort = 9333,
  [int]$BackendPort = 8734,
  [int]$VitePort = 5199,
  [int]$ProviderPort = 8807
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = $Worktree
$py = Join-Path $root "backend/.venv/Scripts/python.exe"
$script = Join-Path $here "ui-verify.mjs"
$runPs1 = Join-Path $root "scripts/verify-pm-qio032/run.ps1"
$downPy = Join-Path $root "scripts/e2e_down.py"
$out = if ($OutDir) { $OutDir } else { $here }
if (-not (Test-Path -LiteralPath $script)) { throw "找不到 ui-verify.mjs：$script" }
if (-not (Test-Path -LiteralPath $py)) { throw "找不到后端解释器：$py" }
New-Item -ItemType Directory -Force -Path $out | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out "shots") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out "materials") | Out-Null

function Test-Http([string]$Url) {
  try {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
    return ($resp.StatusCode -lt 500)
  } catch {
    return $false
  }
}

function Stop-Provider {
  $conn = Get-NetTCPConnection -LocalPort $ProviderPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($conn) {
    $owner = $conn.OwningProcess
    Write-Output ("[cleanup] 结束 provider pid=" + $owner)
    & taskkill /PID $owner /T /F 2>&1 | Out-Null
    Start-Sleep -Milliseconds 800
  }
}

function Assert-PortsFreeAfterCleanup {
  $still = @()
  foreach ($port in @($BackendPort, $VitePort, $ProviderPort)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) { $still += $port }
  }
  if ($still.Count) { Write-Warning ("收尾后端口仍被占用：" + ($still -join ",")) }
  else { Write-Output "[cleanup] 8734/5199/8807 均确认不再监听" }
}

$proxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
foreach ($name in $proxyNames) { Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue }
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = $env:NO_PROXY

if ($Setup) {
  Write-Output "[setup] 调用 run.ps1 -KeepRunning（它会先跑一遍自带 E2E，冷数据域迁移 60s+，请耐心等）..."
  & powershell -NoProfile -ExecutionPolicy Bypass -File $runPs1 -KeepRunning -ProviderPort $ProviderPort
  $setupCode = $LASTEXITCODE
  if ($setupCode -ne 0) {
    Write-Warning ("run.ps1 返回 " + $setupCode + "（自带 E2E 有 UNVERIFIED/失败项时也会非 0）；继续校验服务是否真的就绪")
  }
}

$checks = [ordered]@{
  backend = Test-Http ("http://127.0.0.1:" + $BackendPort + "/api/health")
  vite = Test-Http ("http://127.0.0.1:" + $VitePort + "/")
  provider = Test-Http ("http://127.0.0.1:" + $ProviderPort + "/__health")
}
Write-Output ("[check] backend=" + $checks.backend + " vite=" + $checks.vite + " provider=" + $checks.provider)
if (-not ($checks.backend -and $checks.vite -and $checks.provider)) {
  throw "服务未就绪：请先跑 powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -KeepRunning，或给 run-ui.ps1 加 -Setup。"
}

$node = (Get-Command node -ErrorAction Stop).Source
Write-Output ("[run] node=" + $node)
$env:QIO_UI_WT = $root
$env:QIO_E2E_BASE = "http://127.0.0.1:" + $VitePort
$env:QIO_E2E_API = "http://127.0.0.1:" + $BackendPort
$env:QIO_E2E_PROVIDER = "http://127.0.0.1:" + $ProviderPort
$env:QIO_UI_CDP_PORT = [string]$CdpPort
$env:QIO_UI_OUT = $out
if ($Only) { $env:QIO_UI_ONLY = $Only } else { Remove-Item Env:QIO_UI_ONLY -ErrorAction SilentlyContinue }

$exitCode = 1
Push-Location $root
try {
  & $node $script
  $exitCode = $LASTEXITCODE
} finally {
  Pop-Location
}

$summaryPath = Join-Path $out "summary.json"
Write-Output ("[result] exit=" + $exitCode + " summary=" + $summaryPath)
if (Test-Path -LiteralPath $summaryPath) {
  try {
    $summary = Get-Content -LiteralPath $summaryPath -Raw -Encoding utf8 | ConvertFrom-Json
    Write-Output ("[result] PASS=" + $summary.pass + " FAIL=" + $summary.fail + " UNVERIFIED=" + $summary.unverified + " TOTAL=" + $summary.total)
  } catch { Write-Warning "summary.json 解析失败（不影响退出码）" }
}

if (-not $SkipCleanup) {
  Write-Output "[cleanup] e2e_down.py + provider"
  if (Test-Path -LiteralPath $downPy) { & $py $downPy 2>&1 | Out-String | ForEach-Object { Write-Output $_ } }
  Stop-Provider
  Assert-PortsFreeAfterCleanup
} else {
  Write-Output "[cleanup] 已跳过（-SkipCleanup）：backend/vite/provider 仍在运行，手工复核后请自行 e2e_down.py"
}

exit $exitCode