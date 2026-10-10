# D 本地 SSE 冒烟（不含前端/截图）：provider + uvicorn + 假凭据 + SSE 取证
#
# 与 verify-e2e.ps1 的区别：不起 vite、不截图，只验「后端真实进程 + 真 SSE + 假厂商」。
# 阶段二先跑它（快、失败点少），再跑 verify-e2e.ps1（含前端与截图）。
#
# 跑法：
#     powershell -ExecutionPolicy Bypass -File scripts/verify-sse-local.ps1
#
# 注意：基线（流式未实现）上这个脚本**应当报失败**：QIO 根本不会向 provider 请求流式，
# 所以 SSE 上看不到任何非空 ASSISTANT 增量 —— 那正是阶段一要固定的现状证据。

param(
  [int]$ProviderPort = 8798,
  [int]$BackendPort = 8734,
  [string]$Out = "docs/verification-e2e-stream-local.json"
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
# 每次用干净数据目录：复用旧目录会把上一轮留下的凭据带进来（实测过一次误判）
$dataDir = Join-Path $env:TEMP ("qio-verify-local-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
$providerLog = Join-Path $env:TEMP "qio-verify-provider.log"
$backendLog = Join-Path $env:TEMP "qio-verify-backend.log"
$providerPy = Join-Path $root "scripts\verify_stream_provider.py"
$capturePy = Join-Path $root "scripts\verify_sse_capture.py"
$evidence = Join-Path $root $Out
$failures = @()
$provider = $null
$server = $null

function Step([string]$text) { Write-Output ""; Write-Output ("== " + $text + " ==") }

# 端口预检：8734 上可能还留着上一轮/别人启动的后端。旧进程会**代替**我们应答
# /api/health（假成功），然后我们用新代码去测旧进程 —— 实测踩过一次（基线遗留进程
# 一直占着 8734，导致「真实链路没有流式」的误判）。
function Assert-PortFree([int]$port, [string]$what) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    $owner = ($conn | Select-Object -First 1).OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $owner) | Select-Object -First 1
    throw ($what + " 端口 " + $port + " 已被占用（pid=" + $owner + " " + $proc.Name + "）：先清掉旧进程再跑")
  }
}

# 杀进程树：uv/cmd 会再起 python 子进程，只杀包装进程会留下僵尸占着端口
function Stop-Tree($proc) {
  if ($proc -and -not $proc.HasExited) {
    & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
  }
}

Assert-PortFree $ProviderPort "假厂商端点"
Assert-PortFree $BackendPort "后端"

try {
  Step "1. 起假厂商端点"
  $providerCmd = 'uv run --frozen python "' + $providerPy + '" --port ' + $ProviderPort + ' > "' + $providerLog + '" 2>&1'
  $provider = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $providerCmd) -WorkingDirectory $backend -PassThru -WindowStyle Hidden
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    try {
      $health = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $ProviderPort + "/__health") -TimeoutSec 2
      if ($health.ok) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
  }
  Write-Output ("provider_ready=" + $ready)
  if (-not $ready) { throw "provider 没起来（日志 " + $providerLog + "）" }

  Step "2. 起后端 uvicorn（独立进程，无热加载）"
  $env:PYTHONPATH = Join-Path $backend "src"
  $env:QIO_DATA_DIR = $dataDir
  $env:QIO_DEV_INSECURE = "1"
  $serverCmd = 'uv run --frozen python -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port ' + $BackendPort + ' --log-level warning > "' + $backendLog + '" 2>&1'
  $server = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $serverCmd) -WorkingDirectory $backend -PassThru -WindowStyle Hidden
  $ready = $false
  for ($i = 0; $i -lt 60; $i++) {
    try {
      $h = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $BackendPort + "/api/health") -TimeoutSec 2
      if ($h.status -eq "ok") { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
  }
  # 端口预检之后这里应答的应当就是我们刚起的进程；进程已退出却还能探测到就说明有人抢答
  if ($server.HasExited -and $ready) { throw "后端进程已退出但端口仍有应答：端口被别的进程占用" }
  Write-Output ("backend_ready=" + $ready + " pid=" + $server.Id)
  if (-not $ready) { throw "后端没起来（日志 " + $backendLog + "）" }

  Step "3. 建一条指向本机假厂商的凭据"
  $body = @{
    provider      = "custom"
    endpoint      = ("http://127.0.0.1:" + $ProviderPort + "/v1")
    secret        = "sk-verify-fake-0001"
    default_model = "verify-model"
    tags          = @("main-loop")
  } | ConvertTo-Json -Depth 5
  $created = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $BackendPort + "/api/credentials") -Method Post -Body $body -ContentType "application/json" -TimeoutSec 60
  Write-Output ("credential: key_id=" + $created.key_id + " verify_ok=" + $created.verify.ok + " verify_state=" + $created.verify.state)
  if (-not $created.verify.ok) { $failures += "凭据没有通过验证（verify.state=" + $created.verify.state + "）" }

  Step "4. SSE 取证"
  Push-Location $backend
  $capture = (& uv run --frozen python $capturePy --base ("http://127.0.0.1:" + $BackendPort) --provider ("http://127.0.0.1:" + $ProviderPort) --out $evidence 2>&1 | Out-String)
  $captureExit = $LASTEXITCODE
  Pop-Location
  Write-Output $capture
  Write-Output ("capture_exit=" + $captureExit)
  if ($captureExit -ne 0) { $failures += "SSE 取证有失败项（基线应为红：没有非空回答增量）" }
}
catch {
  $failures += $_.Exception.Message
}
finally {
  Step "5. 收尾"
  Stop-Tree $server
  Stop-Tree $provider
}

Step "结论"
if ($failures.Count) {
  Write-Output "[FAIL]"
  $failures | ForEach-Object { Write-Output ("  - " + $_) }
  exit 1
}
Write-Output "[PASS] 本地 SSE 冒烟全通过"
exit 0
