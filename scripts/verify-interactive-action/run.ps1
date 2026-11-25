# QIO 互动模式底部操作区 · 实机取证 harness 一键入口（verifier 维护）
#
# 作用：清端口 → 起 fake provider（本机扮演的假厂商）→ 起 backend(8734) + vite(5199)
#      （隔离 QIO_DATA_DIR）→ 建假凭据 + 完成首次引导 → 跑指定的 Node 取证脚本
#      → 归档 provider 台账 / 日志 → 只关自己起的进程。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/verify-interactive-action/run.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/verify-interactive-action/run.ps1 -ScriptArgs --only=basics
#   # 对其它工作树（例如改动前的 q46-r9 基线）：
#   powershell -ExecutionPolicy Bypass -File scripts/verify-interactive-action/run.ps1 -Root D:\qio-dev\q46-r9 -NodeScript D:\qio-dev\qio-action\scripts\verify-interactive-action\baseline-current.mjs -OutDir D:\qio-dev\qio-action\_lead-logs\interactive-action\baseline
#
# 端口：backend 8734 / vite 5199 / fake provider 8807（e2e_up.py 的既有约定）；
#       CDP 调试端口 9333。任一端口在启动前必须先清空。
# 边界：fake provider 只证明 QIO 自己的链路，不证明真实厂商；不改产品代码/测试、不加 npm 依赖。
param(
  [string]$Root = "",
  [string]$OutDir = "",
  [string]$DataDir = "",
  [string]$NodeScript = "",
  [string[]]$ScriptArgs = @(),
  [int]$ProviderPort = 8807,
  [switch]$KeepRunning,
  [switch]$NoProvider
)

$ErrorActionPreference = "Continue"
$StartedAt = (Get-Date).ToUniversalTime().ToString("o")

# 本机链路只走 127.0.0.1：若启动 shell 带着 HTTP(S)_PROXY，OpenAI 客户端会把 localhost
# 也送去代理（pm-qio032 实测 Connection error / 台账 0 请求）。只在本次运行的进程环境里清掉。
foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")) {
  Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
}
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = $env:NO_PROXY

if (-not $Root) { $Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $OutDir) { $OutDir = Join-Path $Root "_lead-logs\interactive-action" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$ShotsDir = Join-Path $OutDir "shots"
New-Item -ItemType Directory -Force -Path $ShotsDir | Out-Null
if (-not $NodeScript) { $NodeScript = Join-Path $PSScriptRoot "frames.mjs" }
if (-not [System.IO.Path]::IsPathRooted($NodeScript)) { $NodeScript = Join-Path $PSScriptRoot $NodeScript }
if (-not (Test-Path -LiteralPath $NodeScript)) { Write-Output ("找不到取证脚本：" + $NodeScript); exit 2 }
if (-not $DataDir) {
  # 隔离数据域：每次跑一个干净库（冷库迁移实测要 60s+，所以等待预算给到 180s）
  $DataDir = Join-Path $env:TEMP ("qio-ia-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}

$RunLog = Join-Path $OutDir "run.log"
Remove-Item -LiteralPath $RunLog -Force -ErrorAction SilentlyContinue
$Failures = @()
$Pids = @{}

function Say([string]$text) {
  Write-Output $text
  Add-Content -LiteralPath $RunLog -Value $text -Encoding UTF8
}
function Step([string]$text) { Say ""; Say ("== " + $text + " ==") }

function Test-PortOpen([int]$port) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect("127.0.0.1", $port, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne(700)) { return $false }
    $client.EndConnect($iar)
    return $true
  } catch { return $false } finally { $client.Close() }
}

function WaitHttp([string]$url, [int]$timeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try { $null = Invoke-RestMethod -Uri $url -TimeoutSec 3; return $true } catch { Start-Sleep -Milliseconds 800 }
  }
  return $false
}

function Get-ListenerPid([int]$port) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($conn -and $conn.OwningProcess) { return [int]$conn.OwningProcess }
  # Get-NetTCPConnection 在部分受限环境会安静地返回空列表，退回 netstat 文本解析。
  $text = (netstat -ano -p tcp | Out-String)
  $m = [regex]::Match($text, ":" + $port + "\s+\S+\s+LISTENING\s+(\d+)")
  if ($m.Success) { return [int]$m.Groups[1].Value }
  return 0
}

function Clear-Port([int]$port) {
  if (-not (Test-PortOpen $port)) { Say ("端口 " + $port + "：空闲"); return $true }
  $ownerPid = Get-ListenerPid $port
  if (-not $ownerPid) {
    Say ("端口 " + $port + "：有进程监听但查不到 PID，拒绝猜测，停止（不误杀无关进程）")
    return $false
  }
  Say ("端口 " + $port + "：被 PID " + $ownerPid + " 占用，只终止该进程")
  Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
  $deadline = (Get-Date).AddSeconds(8)
  while ((Get-Date) -lt $deadline) {
    if (-not (Test-PortOpen $port)) { return $true }
    Start-Sleep -Milliseconds 300
  }
  return -not (Test-PortOpen $port)
}

function Stop-Recorded([string]$name) {
  $procPid = $Pids[$name]
  if (-not $procPid) { return }
  $alive = Get-Process -Id $procPid -ErrorAction SilentlyContinue
  if (-not $alive) { Say ("收尾 " + $name + "（PID " + $procPid + "）：已退出"); return }
  Say ("收尾 " + $name + "（PID " + $procPid + "）：终止")
  Stop-Process -Id $procPid -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 300
  if (Get-Process -Id $procPid -ErrorAction SilentlyContinue) {
    & taskkill /PID $procPid /T /F 2>&1 | Out-Null
    Start-Sleep -Milliseconds 300
  }
}

Say ("root=" + $Root)
Say ("out=" + $OutDir)
Say ("data=" + $DataDir)
Say ("node_script=" + $NodeScript)
Say ("provider_port=" + $ProviderPort)

try { $env:QIO_IA_GIT_HEAD = (& git -C $Root rev-parse HEAD 2>$null) } catch { $env:QIO_IA_GIT_HEAD = "" }
$Py = Join-Path $Root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) { Say ("找不到后端解释器：" + $Py); exit 2 }
$probe = & $Py -c "import fastapi, uvicorn, keyring" 2>&1
if ($LASTEXITCODE -ne 0) {
  Say ("后端解释器不可用（import fastapi, uvicorn, keyring 失败）：" + ($probe | Out-String))
  exit 2
}
$nodeExe = (Get-Command node).Source
Say ("python=" + $Py)
Say ("node=" + $nodeExe + " " + ((node --version) 2>&1))

$env:QIO_DATA_DIR = $DataDir
$env:QIO_DEV_INSECURE = "1"
$env:QIO_ENABLE_TEST_EVENTS = "1"
$env:PYTHONPATH = (Join-Path $Root "backend\src")

try {
  Step "1. 清端口（8734 / 5199 / provider / CDP 9333）"
  foreach ($port in @(8734, 5199, $ProviderPort, 9333)) {
    if (-not (Clear-Port $port)) { $Failures += ("端口 " + $port + " 没能清空") }
  }

  if (-not $NoProvider) {
    Step "2. fake provider（真 SSE + /__log 台账）"
    $providerLog = Join-Path $OutDir "provider-server.log"
    $providerScript = Join-Path $PSScriptRoot "..\verify-pm-qio032\fake_provider.py"
    $provider = Start-Process -FilePath $Py -ArgumentList @($providerScript, "--port", "$ProviderPort") -WorkingDirectory (Join-Path $Root "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $providerLog -RedirectStandardError ($providerLog + ".err")
    $Pids["provider"] = $provider.Id
    $providerReady = WaitHttp ("http://127.0.0.1:" + $ProviderPort + "/__health") 40
    Say ("provider_ready=" + $providerReady + " pid=" + $provider.Id)
    if (-not $providerReady) { $Failures += "fake provider 没起来（见 provider-server.log）" }
  }

  Step "3. 后端 uvicorn + 前端 vite（隔离数据域；冷库迁移预算 180s）"
  $backendLog = Join-Path $OutDir "backend.log"
  $backend = Start-Process -FilePath $Py -ArgumentList @("-m", "uvicorn", "agent.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "8734", "--log-level", "warning") -WorkingDirectory (Join-Path $Root "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $backendLog -RedirectStandardError ($backendLog + ".err")
  $Pids["backend"] = $backend.Id
  $viteLog = Join-Path $OutDir "vite.log"
  $viteEntry = Join-Path $Root "frontend\node_modules\vite\bin\vite.js"
  if (-not (Test-Path -LiteralPath $viteEntry)) { $Failures += ("找不到 vite：" + $viteEntry) }
  $vite = Start-Process -FilePath $nodeExe -ArgumentList @($viteEntry, "--port", "5199", "--strictPort", "--host", "127.0.0.1") -WorkingDirectory (Join-Path $Root "frontend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $viteLog -RedirectStandardError ($viteLog + ".err")
  $Pids["vite"] = $vite.Id
  $backendReady = WaitHttp "http://127.0.0.1:8734/api/health" 180
  $viteReady = WaitHttp "http://127.0.0.1:5199/" 90
  Say ("backend_ready=" + $backendReady + " pid=" + $backend.Id + " alive=" + (-not $backend.HasExited))
  Say ("vite_ready=" + $viteReady + " pid=" + $vite.Id + " alive=" + (-not $vite.HasExited))
  if (-not $backendReady) { $Failures += "后端 8734 没就绪（180s 预算，见 backend.log）" }
  if (-not $viteReady) { $Failures += "vite 5199 没就绪（见 vite.log）" }
  if ($Failures.Count -gt 0 -and ($Failures -join "|") -match "没就绪") { throw "服务未就绪，跳过取证" }

  Step "4. 假凭据 + 完成首次引导（干净数据域下模式切换会被「初始设置尚未完成」拦住）"
  if (-not $NoProvider) {
    $credBody = @{
      provider = "custom"
      endpoint = ("http://127.0.0.1:" + $ProviderPort + "/v1")
      secret = "sk-verify-fake"
      default_model = "verify-model"
      tags = @("main-loop")
    } | ConvertTo-Json -Depth 5
    try {
      $created = Invoke-RestMethod -Uri "http://127.0.0.1:8734/api/credentials" -Method Post -Body $credBody -ContentType "application/json" -TimeoutSec 120
      Say ("credential key_id=" + $created.key_id + " verify_ok=" + $created.verify.ok + " mode=" + $created.verify.mode)
      if (-not $created.verify.ok) { Say ("凭据验证未通过（不阻断：静态渲染/开合取证不依赖模型）") }
    } catch { Say ("建凭据失败（不阻断）：" + $_.Exception.Message) }
  }
  foreach ($pair in @(@("onboarding/complete", "POST"), @("onboarding/seen", "POST"), @("onboarding/hint", "POST"))) {
    try {
      if ($pair[0] -eq "onboarding/hint") {
        $null = Invoke-RestMethod -Uri ("http://127.0.0.1:8734/api/" + $pair[0]) -Method Post -Body '{"dismissed":true}' -ContentType "application/json" -TimeoutSec 30
      } else {
        $null = Invoke-RestMethod -Uri ("http://127.0.0.1:8734/api/" + $pair[0]) -Method Post -TimeoutSec 30
      }
      Say ("onboarding " + $pair[0] + " ok")
    } catch { Say ("onboarding " + $pair[0] + " 失败（不阻断，记录为事实）：" + $_.Exception.Message) }
  }

  Step "5. 取证脚本（系统 Edge + CDP）"
  $env:QIO_IA_BASE = "http://127.0.0.1:5199"
  $env:QIO_IA_API = "http://127.0.0.1:8734"
  $env:QIO_IA_PROVIDER = ("http://127.0.0.1:" + $ProviderPort)
  $env:QIO_IA_OUT = $OutDir
  $env:QIO_IA_ROOT = $Root
  $env:QIO_CDP_PORT = "9333"
  $consoleLog = Join-Path $OutDir "console.raw.log"
  Remove-Item -LiteralPath $consoleLog -Force -ErrorAction SilentlyContinue
  Push-Location $Root
  $nodeArgs = @($NodeScript) + $ScriptArgs
  & node @nodeArgs 2>&1 | ForEach-Object { Write-Output $_; Add-Content -LiteralPath $consoleLog -Value $_ -Encoding UTF8 }
  $nodeExit = $LASTEXITCODE
  Pop-Location
  Say ("node_exit=" + $nodeExit)
  if ($nodeExit -ne 0) { $Failures += ("取证脚本退出码 " + $nodeExit + "（见 summary.json / console.raw.log）") }

  Step "6. 归档 provider 台账"
  if (-not $NoProvider) {
    try {
      $providerLogJson = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $ProviderPort + "/__log") -TimeoutSec 10
      $providerLogJson | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $OutDir "provider-log.json") -Encoding UTF8
      Say ("provider requests=" + (($providerLogJson.requests | Measure-Object).Count))
    } catch { Say ("provider 台账归档失败：" + $_.Exception.Message) }
  }
} catch {
  Say ("运行中止：" + $_.Exception.Message)
  $Failures += ("运行中止：" + $_.Exception.Message)
} finally {
  if (-not $KeepRunning) {
    Step "7. 收尾（只关本脚本启动的进程）"
    foreach ($name in @("provider", "backend", "vite")) { Stop-Recorded $name }
    $stillOpen = @()
    foreach ($port in @(8734, 5199, $ProviderPort)) {
      if (Test-PortOpen $port) { $stillOpen += $port }
    }
    if ($stillOpen.Count -gt 0) { $Failures += ("端口仍被占用：" + ($stillOpen -join ",")) }
    else { Say "端口 8734 / 5199 / provider 均已空闲（真实 TCP 探测确认）" }
  } else {
    Say ("KeepRunning：服务继续跑（data=" + $DataDir + "；pids=" + ($Pids | ConvertTo-Json -Compress) + "）")
  }
}

try {
  $meta = @{
    root = $Root
    outDir = $OutDir
    dataDir = $DataDir
    nodeScript = $NodeScript
    nodeArgs = $ScriptArgs
    pids = $Pids
    startedAt = $StartedAt
    finishedAt = (Get-Date).ToUniversalTime().ToString("o")
    failures = $Failures
  }
  $meta | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutDir "run-meta.json") -Encoding UTF8
} catch { }

Step "结论"
if ($Failures.Count) {
  foreach ($failure in $Failures) { Say ("  [FAIL] " + $failure) }
  exit 1
}
Say "  [OK] 全流程完成"
exit 0
