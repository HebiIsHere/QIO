# D 端到端验收（契约 §6 第 3、6 条）——真实链路 + 无头截图
#
# 阶段二在**集成分支**上跑；阶段一未运行（实现还没合并，跑出来的只能是红的）。
# 跑法：
#     powershell -ExecutionPolicy Bypass -File scripts/verify-e2e.ps1
#
# 链路：假厂商端点（真 SSE 分片）→ 后端 uvicorn → 前端 vite dev → 真 SSE → msedge 无头截图。
# 结论只能读成「QIO 自己的链路对」：provider 是本机扮演的，不证明任何真实厂商。
#
# 产出：
#   docs/verification-e2e-stream.json   SSE 取证（回答是否先于 provider 结束到达）
#   docs/verification-shots/*.png       截图（宽/窄窗口）
# 日志：$env:TEMP\qio-e2e-logs\（backend.log / vite.log）；provider 日志 %TEMP%\qio-verify-provider.log

param(
  [int]$ProviderPort = 8798,
  [string]$DataDir = "",
  [string]$OutDir = "docs/verification-shots",
  [switch]$SkipShots
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not $DataDir) { $DataDir = Join-Path $env:TEMP ("qio-verify-d-" + (Get-Date -Format "yyyyMMdd-HHmmss")) }
$evidence = Join-Path $root "docs\verification-e2e-stream.json"
$shots = Join-Path $root $OutDir
$providerLog = Join-Path $env:TEMP "qio-verify-provider.log"
$providerPy = Join-Path $root "scripts\verify_stream_provider.py"
$capturePy = Join-Path $root "scripts\verify_sse_capture.py"
$upPy = Join-Path $root "scripts\e2e_up.py"
$downPy = Join-Path $root "scripts\e2e_down.py"
$failures = @()

function Step([string]$text) { Write-Output ""; Write-Output ("== " + $text + " ==") }

# 端口预检 + 杀进程树：旧进程会替我们应答 /api/health（假成功），实测踩过一次
function Assert-PortFree([int]$port, [string]$what) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    $owner = ($conn | Select-Object -First 1).OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $owner) | Select-Object -First 1
    throw ($what + " 端口 " + $port + " 已被占用（pid=" + $owner + " " + $proc.Name + "）：先跑 scripts/e2e_down.py 清掉旧进程")
  }
}
function Stop-Tree($proc) {
  if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null }
}

Step "0. 环境"
if (Test-Path $edge) {
  $version = (Get-Item $edge).VersionInfo.ProductVersion
  Write-Output ("msedge: " + $edge + " (version " + $version + ")")
} else {
  $SkipShots = $true
  Write-Output ("[WARN] 找不到 msedge（" + $edge + "）：截图这一步**未验证**，如实记录。")
}
Write-Output ("data dir: " + $DataDir)

Assert-PortFree $ProviderPort "假厂商端点"
Assert-PortFree 8734 "后端"
Assert-PortFree 5199 "前端"

Step "1. 起假厂商端点（真 SSE 分片）"
$providerCmd = 'uv run --frozen python "' + $providerPy + '" --port ' + $ProviderPort + ' > "' + $providerLog + '" 2>&1'
$provider = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $providerCmd) -WorkingDirectory $backend -PassThru -WindowStyle Hidden
$providerReady = $false
for ($i = 0; $i -lt 40; $i++) {
  try {
    $health = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $ProviderPort + "/__health") -TimeoutSec 2
    if ($health.ok) { $providerReady = $true; break }
  } catch { Start-Sleep -Milliseconds 500 }
}
Write-Output ("provider_ready=" + $providerReady + " (pid=" + $provider.Id + ")")
if (-not $providerReady) { $failures += "provider 没起来（日志 " + $providerLog + "）" }

try {
  Step "2. 起后端 + 前端 dev server（scripts/e2e_up.py）"
  $env:QIO_DATA_DIR = $DataDir
  $up = (& python $upPy 2>&1 | Out-String)
  Write-Output $up
  if ($LASTEXITCODE -ne 0) { $failures += "e2e_up.py 退出码 " + $LASTEXITCODE }

  Step "3. 建一条指向本机假厂商的凭据（明文 HTTP 仅限 loopback；密钥是假的）"
  $body = @{
    provider      = "custom"
    endpoint      = ("http://127.0.0.1:" + $ProviderPort + "/v1")
    secret        = "sk-verify-fake-0001"
    default_model = "verify-model"
    tags          = @("main-loop")
  } | ConvertTo-Json -Depth 5
  try {
    $created = Invoke-RestMethod -Uri "http://127.0.0.1:8734/api/credentials" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 60
    Write-Output ("credential: key_id=" + $created.key_id + " verify_ok=" + $created.verify.ok + " verify_state=" + $created.verify.state)
    if (-not $created.verify.ok) { $failures += "凭据没有通过验证（verify.state=" + $created.verify.state + "）" }
  } catch {
    $failures += "建凭据失败：" + $_.Exception.Message
  }

  Step "4. SSE 取证：回答是否在 provider 结束之前到达"
  Push-Location $backend
  $capture = (& uv run --frozen python $capturePy --base "http://127.0.0.1:8734" --provider ("http://127.0.0.1:" + $ProviderPort) --out $evidence 2>&1 | Out-String)
  $captureExit = $LASTEXITCODE
  Pop-Location
  Write-Output $capture
  if ($captureExit -ne 0) { $failures += "SSE 取证有失败项（见 " + $evidence + "）" }

  if (-not $SkipShots) {
    Step "5. 状态截图（Playwright 驱动真实交互，msedge 无头）"
    New-Item -ItemType Directory -Force -Path $shots | Out-Null
    $shotsJs = Join-Path $root "scripts\verify-e2e-shots.mjs"
    $env:QIO_E2E_BASE = "http://127.0.0.1:5199"
    $env:QIO_E2E_PROVIDER = ("http://127.0.0.1:" + $ProviderPort)
    $env:QIO_E2E_SHOTS = $shots
    Push-Location $root
    $shotOut = (& node $shotsJs 2>&1 | Out-String)
    $shotExit = $LASTEXITCODE
    Pop-Location
    Write-Output $shotOut
    if ($shotExit -ne 0) {
      $failures += "状态截图有失败项（见 " + (Join-Path $shots "shots-summary.json") + "）"
      Step "5b. 回退：msedge 静态截图"
      & $edge --headless=new --disable-gpu --hide-scrollbars --window-size=1440,900 --screenshot="$shots\conversation-wide.png" "http://127.0.0.1:5199/" | Out-Null
      Start-Sleep -Seconds 3
      & $edge --headless=new --disable-gpu --hide-scrollbars --window-size=480,900 --screenshot="$shots\conversation-narrow.png" "http://127.0.0.1:5199/" | Out-Null
    }
    Get-ChildItem $shots -Filter *.png | ForEach-Object { Write-Output ("shot: " + $_.Name + " (" + $_.Length + " bytes)") }
  }
}
finally {
  Step "6. 收尾"
  & python $downPy 2>&1 | Out-String | Write-Output
  Stop-Tree $provider
}

Step "结论"
if ($failures.Count) {
  Write-Output "[FAIL]"
  $failures | ForEach-Object { Write-Output ("  - " + $_) }
  exit 1
}
Write-Output ("[PASS] provider/后端/前端就绪，SSE 取证全通过，截图已落 " + $shots)
exit 0
