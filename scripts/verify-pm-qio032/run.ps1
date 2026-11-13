# PM-QIO032 互动模式 E2E 一键跑（fake provider → uvicorn → vite → Edge/CDP）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -Only s1
#
# 产物：_lead-logs/pm-qio032/e2e/{shots/*.png, summary.json, provider-log.json, console.log,
#       run.log, provider-server.log, backend.log, vite.log, README.md}
# 边界：provider 是本机扮演的假厂商（真 SSE、有请求台账）；结论只读成「QIO 自己的链路对」，
#       不证明真实厂商 / Tauri 原生窗口行为。
param(
  [string]$Only = "",
  [int]$ProviderPort = 8807,
  [string]$DataDir = "",
  [string]$OutDir = "",
  [switch]$KeepRunning
)

$ErrorActionPreference = "Continue"
# 本机链路只走 127.0.0.1。若启动 shell 里带着 HTTP(S)_PROXY，OpenAI 客户端会
# 把 localhost 也送去代理（实测 Connection error，provider 台账 0 请求）。
# 这是测试胶水，不动产品配置：只在本次运行的进程环境里清掉代理。
foreach ($name in @("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy")) {
  Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
}
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = $env:NO_PROXY
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$py = Join-Path $root "backend\.venv\Scripts\python.exe"
$e2eDir = Join-Path $root "_lead-logs\pm-qio032\e2e"
if (-not $OutDir) { $OutDir = $e2eDir }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutDir "shots") | Out-Null
if (-not $DataDir) {
  # 隔离数据域：每次跑一个干净库（迁移在冷库上要跑 60s 以上，这是实测）
  $DataDir = Join-Path $env:TEMP ("qio-pm-qio032-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
$flags = @()
$failures = @()
$runLog = Join-Path $OutDir "run.log"
Remove-Item -LiteralPath $runLog -Force -ErrorAction SilentlyContinue

function Say([string]$text) {
  Write-Output $text
  Add-Content -LiteralPath $runLog -Value $text -Encoding UTF8
}
function Step([string]$text) { Say ""; Say ("== " + $text + " ==") }
function WaitHttp([string]$url, [int]$timeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try { $null = Invoke-RestMethod -Uri $url -TimeoutSec 3; return $true } catch { Start-Sleep -Milliseconds 800 }
  }
  return $false
}

Say ("root=" + $root)
Say ("data=" + $DataDir)
Say ("out=" + $OutDir)

Step "1. 端口清理（e2e_down.py + 杀 provider）"
& $py (Join-Path $root "scripts\e2e_down.py") 2>&1 | Out-String | ForEach-Object { Say $_ }
$conn = Get-NetTCPConnection -LocalPort $ProviderPort -State Listen -ErrorAction SilentlyContinue
if ($conn) {
  $pid8807 = ($conn | Select-Object -First 1).OwningProcess
  & taskkill /PID $pid8807 /T /F 2>&1 | Out-Null
  Start-Sleep -Seconds 1
  Say ("killed provider pid " + $pid8807)
}

Step "2. 假厂商端点（真 SSE + /__log 台账）"
$providerLog = Join-Path $OutDir "provider-server.log"
$provider = Start-Process -FilePath $py -ArgumentList @((Join-Path $root "scripts\verify-pm-qio032\fake_provider.py"), "--port", "$ProviderPort") -WorkingDirectory (Join-Path $root "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $providerLog -RedirectStandardError ($providerLog + ".err")
$providerReady = WaitHttp ("http://127.0.0.1:" + $ProviderPort + "/__health") 30
Say ("provider_ready=" + $providerReady + " pid=" + $provider.Id)
if (-not $providerReady) { $failures += "provider 没起来（见 provider-server.log）" }

try {
  Step "3. 后端 + 前端 dev server（scripts/e2e_up.py，隔离 QIO_DATA_DIR）"
  $env:QIO_DATA_DIR = $DataDir
  $up = (& $py (Join-Path $root "scripts\e2e_up.py") 2>&1 | Out-String)
  Say $up
  # e2e_up.py 自带的就绪预算只有 25s：冷库上迁移要 60s+，实测它会报 backend_ready=False
  # （这不是失败原因，只是预算不足）。这里用更长的预算自己确认，并把事实记进 flags。
  $backendReady = WaitHttp "http://127.0.0.1:8734/api/health" 180
  $viteReady = WaitHttp "http://127.0.0.1:5199/" 60
  Say ("backend_ready_long_wait=" + $backendReady + " vite_ready=" + $viteReady)
  if (-not $backendReady) { $failures += "后端 8734 没就绪（180s 预算）" }
  if (-not $viteReady) { $failures += "vite 5199 没就绪" }

  Step "4. 建凭据（假 Key，只指向本机 fake provider）"
  $body = @{ provider = "custom"; endpoint = ("http://127.0.0.1:" + $ProviderPort + "/v1"); secret = "sk-pm-qio032-fake-0001"; default_model = "verify-model"; tags = @("main-loop") } | ConvertTo-Json -Depth 5
  try {
    $created = Invoke-RestMethod -Uri "http://127.0.0.1:8734/api/credentials" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 120
    Say ("credential key_id=" + $created.key_id + " verify_ok=" + $created.verify.ok + " mode=" + $created.verify.mode)
    if (-not $created.verify.ok) { $failures += "凭据验证未通过" }
  } catch { $failures += ("建凭据失败：" + $_.Exception.Message) }

  Step "4b. 完成首次引导（干净数据域下模式切换会被「初始设置尚未完成」拦住）"
  try {
    $onb = Invoke-RestMethod -Uri "http://127.0.0.1:8734/api/onboarding/complete" -Method Post -TimeoutSec 30
    Say ("onboarding done=" + $onb.done + " has_credential=" + $onb.has_credential)
  } catch { Say ("onboarding complete 失败（不阻断，记录为事实）：" + $_.Exception.Message) }

  Step "5. E2E（正常界面入口 + provider 台账断言）"
  $env:QIO_E2E_BASE = "http://127.0.0.1:5199"
  $env:QIO_E2E_API = "http://127.0.0.1:8734"
  $env:QIO_E2E_PROVIDER = ("http://127.0.0.1:" + $ProviderPort)
  $env:QIO_E2E_OUT = $OutDir
  $nodeArgs = @((Join-Path $root "scripts\verify-pm-qio032\e2e.mjs"))
  if ($Only) { $nodeArgs += ("--only=" + $Only) }
  Push-Location $root
  # e2e.mjs 自己会写结构化 console.log；这里只落原始 stdout，避免两个写者抢同一个文件。
  $consoleLog = Join-Path $OutDir "console.raw.log"
  Remove-Item -LiteralPath $consoleLog -Force -ErrorAction SilentlyContinue
  & node @nodeArgs 2>&1 | ForEach-Object { Write-Output $_; Add-Content -LiteralPath $consoleLog -Value $_ -Encoding UTF8 }
  $e2eExit = $LASTEXITCODE
  Pop-Location
  Say ("e2e_exit=" + $e2eExit)
  if ($e2eExit -ne 0) { $failures += "E2E 有失败项（见 summary.json / console.log）" }

  Step "6. 归档 provider 台账 + 后端/前端日志"
  try {
    $logJson = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $ProviderPort + "/__log") -TimeoutSec 10
    $logJson | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $OutDir "provider-log.json") -Encoding UTF8
    Say ("provider requests=" + ($logJson.requests | Measure-Object).Count)
  } catch { Say ("provider 台账归档失败：" + $_.Exception.Message) }
  foreach ($pair in @(@("backend.log","backend.log"), @("vite.log","vite.log"))) {
    $src = Join-Path $env:TEMP ("qio-e2e-logs\" + $pair[0])
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $OutDir $pair[1]) -Force }
  }
}
finally {
  if (-not $KeepRunning) {
    Step "7. 收尾（e2e_down + 杀 provider）"
    & $py (Join-Path $root "scripts\e2e_down.py") 2>&1 | Out-String | ForEach-Object { Say $_ }
    if ($provider -and -not $provider.HasExited) { & taskkill /PID $provider.Id /T /F 2>&1 | Out-Null }
    $stillOpen = @()
    foreach ($port in @(8734, 5199, $ProviderPort)) {
      $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
      if ($c) { $stillOpen += $port }
    }
    if ($stillOpen.Count) { $failures += ("端口仍被占用：" + ($stillOpen -join ",")) }
  } else {
    Say ("KeepRunning：服务继续跑（data=" + $DataDir + "）")
  }
}

Step "结论"
if ($failures.Count) {
  foreach ($f in $failures) { Say ("  [FAIL] " + $f) }
  exit 1
}
Say "  [OK] 全流程完成，E2E 全部通过"
exit 0