# D 阶段二（R7）：实机交互取证（假厂商 SSE → uvicorn → vite → msedge 无头 + Playwright）
#
# 覆盖：
#   S1 上传失败/客户端暂停时的**界面反馈**（明确失败，不是无限转圈）
#   S2 正式回答在 provider 结束前**流式可见**且**不重复**（含未声明的降级路径表现）
#   S3 带附件重试（复制克隆）时界面仍正常、内容正确
#   S4 回归：默认折叠 / 内联审批 / 历史附件打开 / 结束原因与耗时
#
# 跑法：powershell -ExecutionPolicy Bypass -File scripts/verify-r7-phase2.ps1
# 产出：docs/verification-shots-r7-phase2/*.png、summary.json
# 结论只能读成「QIO 自己的链路对」：provider 是本机扮演的假厂商，不证明任何真实厂商行为。
param(
  [int]$ProviderPort = 8798,
  [string]$DataDir = "",
  [string]$OutDir = "docs/verification-shots-r7-phase2",
  [switch]$KeepRunning
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$py = Join-Path $backend ".venv\Scripts\python.exe"
$providerLog = Join-Path $env:TEMP "qio-r7-provider.log"
if (-not $DataDir) { $DataDir = Join-Path $env:TEMP ("qio-r7-phase2-" + (Get-Date -Format "yyyyMMdd-HHmmss")) }
$failures = @()
function Step([string]$text) { Write-Output ""; Write-Output ("== " + $text + " ==") }

Step "0. 端口预检与清理"
& $py (Join-Path $root "scripts\e2e_down.py") 2>&1 | Out-String | Write-Output
$conn = Get-NetTCPConnection -LocalPort $ProviderPort -State Listen -ErrorAction SilentlyContinue
if ($conn) { & taskkill /PID ($conn | Select-Object -First 1).OwningProcess /T /F 2>&1 | Out-Null; Start-Sleep -Seconds 1 }
Write-Output ("data dir: " + $DataDir + "；shots: " + $OutDir)

Step "1. 假厂商端点（真 SSE 分片，本机扮演）"
$providerErrLog = $providerLog + ".err"
$provider = Start-Process -FilePath $py -ArgumentList @((Join-Path $root "scripts\verify_stream_provider.py"), "--port", "$ProviderPort") -WorkingDirectory $backend -PassThru -WindowStyle Hidden -RedirectStandardOutput $providerLog -RedirectStandardError $providerErrLog
$providerReady = $false
for ($i = 0; $i -lt 40; $i++) {
  try { if ((Invoke-RestMethod -Uri ("http://127.0.0.1:" + $ProviderPort + "/__health") -TimeoutSec 2).ok) { $providerReady = $true; break } }
  catch { Start-Sleep -Milliseconds 500; if ($provider.HasExited) { break } }
}
Write-Output ("provider_ready=" + $providerReady + " (pid=" + $provider.Id + ")")
if (-not $providerReady) { $failures += "provider 没起来（日志 " + $providerLog + "）" }

try {
  Step "2. 后端 + 前端 dev server（scripts/e2e_up.py）"
  $env:QIO_DATA_DIR = $DataDir
  $up = (& $py (Join-Path $root "scripts\e2e_up.py") 2>&1 | Out-String)
  Write-Output $up
  if ($LASTEXITCODE -ne 0) { $failures += "e2e_up.py 退出码 " + $LASTEXITCODE }

  Step "3. 建凭据（指向本机假厂商；密钥是假的）"
  $body = @{ provider = "custom"; endpoint = ("http://127.0.0.1:" + $ProviderPort + "/v1"); secret = "sk-r7-phase2-fake-0001"; default_model = "verify-model"; tags = @("main-loop") } | ConvertTo-Json -Depth 5
  try {
    $created = Invoke-RestMethod -Uri "http://127.0.0.1:8734/api/credentials" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 90
    Write-Output ("credential: key_id=" + $created.key_id + " verify_ok=" + $created.verify.ok)
    if (-not $created.verify.ok) { $failures += "凭据没有通过验证" }
  } catch { $failures += "建凭据失败：" + $_.Exception.Message }

  Step "4. 实机交互取证（Playwright + msedge 无头）"
  $shots = Join-Path $root $OutDir
  New-Item -ItemType Directory -Force -Path $shots | Out-Null
  $env:QIO_E2E_BASE = "http://127.0.0.1:5199"
  $env:QIO_E2E_PROVIDER = ("http://127.0.0.1:" + $ProviderPort)
  $env:QIO_E2E_API = "http://127.0.0.1:8734"
  $env:QIO_E2E_SHOTS = $shots
  $env:QIO_E2E_DATA = $DataDir
  $env:QIO_E2E_WORKSPACE = (Join-Path $DataDir "workspace")
  Push-Location $root
  $shotOut = (& node (Join-Path $root "scripts\verify-r7-phase2-shots.mjs") 2>&1 | Out-String)
  $shotExit = $LASTEXITCODE
  Pop-Location
  Write-Output $shotOut
  if ($shotExit -ne 0) { $failures += "实机取证有失败项（见 " + (Join-Path $shots "summary.json") + "）" }
  Get-ChildItem $shots -Filter *.png | Sort-Object Name | ForEach-Object { Write-Output ("shot: " + $_.Name + " (" + $_.Length + " bytes)") }
}
finally {
  if (-not $KeepRunning) {
    Step "5. 收尾"
    & $py (Join-Path $root "scripts\e2e_down.py") 2>&1 | Out-String | Write-Output
    if ($provider -and -not $provider.HasExited) { & taskkill /PID $provider.Id /T /F 2>&1 | Out-Null }
  }
}

Step "结论"
if ($failures.Count) { Write-Output "[FAIL]"; $failures | ForEach-Object { Write-Output ("  - " + $_) }; exit 1 }
Write-Output "[OK] 实机取证全部通过（截图见 " + (Join-Path $root $OutDir) + "）"
exit 0



