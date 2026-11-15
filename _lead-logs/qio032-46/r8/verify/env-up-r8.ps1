# QIO #46 第八轮（R8）浏览器验收 · 隔离环境启动
# 只写 _lead-logs/qio032-46/r8；数据目录按 -Name 隔离，绝不碰 D:\QIO-data。
#
# 用法：
#   pwsh -File env-up-r8.ps1 -Root D:\qio-dev\q46-r8 -VitePort 5201 -Name product
#   pwsh -File env-up-r8.ps1 -Root D:\qio-dev\_q46-r8-baseline -VitePort 5202 -Name baseline
#
# 后端始终用 D:\qio-dev\q46-r8\backend 起（本轮后端可能同时在改），前端 vite 用 -Root。
param(
  [string]$Root = "D:\qio-dev\q46-r8",
  [int]$VitePort = 5201,
  [int]$BackendPort = 8735,
  [int]$ProviderPort = 8808,
  [string]$Name = "main",
  [string]$BackendRoot = "D:\qio-dev\q46-r8",
  [string]$OutRoot = "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8"
)
$ErrorActionPreference = "Continue"
foreach ($n in @("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy")) {
  Remove-Item -LiteralPath ("Env:" + $n) -ErrorAction SilentlyContinue
}
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = $env:NO_PROXY

$logs = Join-Path $OutRoot "logs"
$data = Join-Path $OutRoot ("e2e-data\" + $Name)
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

function PortBusy([int]$p) {
  try { return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop) } catch { return $false }
}
$busy = @()
foreach ($p in $VitePort, $BackendPort, $ProviderPort) { if (PortBusy $p) { $busy += $p } }
if ($busy.Count) { Write-Host ("PORTS_BUSY=" + ($busy -join ',')); exit 2 }

$py = Join-Path $BackendRoot "backend\.venv\Scripts\python.exe"
$node = (Get-Command node -ErrorAction Stop).Source

# 1) fake provider（真 SSE + /__log 台账）
$provLog = Join-Path $logs ($Name + "-provider.log")
$provider = Start-Process -FilePath $py -ArgumentList @((Join-Path $BackendRoot "scripts\verify-pm-qio032\fake_provider.py"), "--port", "$ProviderPort") -WorkingDirectory (Join-Path $BackendRoot "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $provLog -RedirectStandardError ($provLog + ".err")
Write-Host ("provider_pid=" + $provider.Id)

# 2) 后端（uv run --frozen），隔离数据目录
$env:QIO_DATA_DIR = $data
$env:QIO_PORT = "$BackendPort"
$env:QIO_HOST = "127.0.0.1"
$env:QIO_DEV_INSECURE = "1"
$env:QIO_ENABLE_TEST_EVENTS = "1"
$backendLog = Join-Path $logs ($Name + "-backend.log")
$backend = Start-Process -FilePath "uv" -ArgumentList @("run","--frozen","uvicorn","agent.main:create_app","--factory","--host","127.0.0.1","--port","$BackendPort","--log-level","warning") -WorkingDirectory (Join-Path $BackendRoot "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $backendLog -RedirectStandardError ($backendLog + ".err")
Write-Host ("backend_pid=" + $backend.Id)

# 3) 前端 vite dev（strictPort），后端地址指到隔离后端
$env:VITE_QIO_BACKEND_URL = "http://127.0.0.1:$BackendPort"
$viteLog = Join-Path $logs ($Name + "-vite.log")
$vite = Start-Process -FilePath $node -ArgumentList @("node_modules/vite/bin/vite.js","--port","$VitePort","--strictPort","--host","127.0.0.1") -WorkingDirectory (Join-Path $Root "frontend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $viteLog -RedirectStandardError ($viteLog + ".err")
Write-Host ("vite_pid=" + $vite.Id)

$pids = @{ name = $Name; root = $Root; provider = $provider.Id; backend = $backend.Id; vite = $vite.Id; startedAt = (Get-Date).ToString("o"); dataDir = $data; vitePort = $VitePort; backendPort = $BackendPort; providerPort = $ProviderPort }
$pids | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logs ($Name + "-pids.json")) -Encoding UTF8

function WaitHttp([string]$url, [int]$timeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try { $null = Invoke-RestMethod -Uri $url -TimeoutSec 3; return $true } catch { Start-Sleep -Milliseconds 800 }
  }
  return $false
}
$provReady = WaitHttp "http://127.0.0.1:$ProviderPort/__health" 40
$backendReady = WaitHttp "http://127.0.0.1:$BackendPort/api/health" 300
$viteReady = WaitHttp "http://127.0.0.1:$VitePort/" 120
Write-Host ("provider_ready=$provReady backend_ready=$backendReady vite_ready=$viteReady")
if (-not $provReady) { Write-Host "--- provider.log ---"; Get-Content $provLog -Tail 20 -ErrorAction SilentlyContinue }
if (-not $backendReady) { Write-Host "--- backend.log ---"; Get-Content $backendLog -Tail 30 -ErrorAction SilentlyContinue; Get-Content ($backendLog + ".err") -Tail 30 -ErrorAction SilentlyContinue }
if (-not $viteReady) { Write-Host "--- vite.log ---"; Get-Content $viteLog -Tail 30 -ErrorAction SilentlyContinue; Get-Content ($viteLog + ".err") -Tail 30 -ErrorAction SilentlyContinue }
if ($provReady -and $backendReady -and $viteReady) { Write-Host "ENV_OK name=$Name vite=$VitePort backend=$BackendPort data=$data"; exit 0 } else { Write-Host "ENV_FAIL"; exit 1 }
