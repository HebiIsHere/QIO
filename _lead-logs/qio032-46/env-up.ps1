# QIO032-46 定向验收 · 隔离环境启动（只写 _lead-logs/qio032-46）
# 后端 uvicorn（uv run --frozen）+ 前端 vite dev + 本机 fake provider。
# 数据目录隔离到 _lead-logs/qio032-46/e2e-data，不碰用户真实数据。
$ErrorActionPreference = "Continue"
foreach ($name in @("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy")) {
  Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
}
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = $env:NO_PROXY

$root = "D:\qio-dev\pm-qio032-integration"
$out  = Join-Path $root "_lead-logs\qio032-46"
$logs = Join-Path $out "logs"
$data = Join-Path $out "e2e-data"
New-Item -ItemType Directory -Force -Path $logs, $data | Out-Null

function PortBusy([int]$p) {
  try { return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop) } catch { return $false }
}
$busy = @()
foreach ($p in 8734,5199,8807) { if (PortBusy $p) { $busy += $p } }
if ($busy.Count) { Write-Host ("PORTS_BUSY=" + ($busy -join ',')); exit 2 }

$py = Join-Path $root "backend\.venv\Scripts\python.exe"
$node = "C:\Program Files\nodejs\node.exe"
if (-not (Test-Path $node)) { $node = (Get-Command node).Source }

# 1) fake provider（真 SSE + /__log 台账）
$provLog = Join-Path $logs "provider.log"
$provider = Start-Process -FilePath $py -ArgumentList @((Join-Path $root "scripts\verify-pm-qio032\fake_provider.py"), "--port", "8807") -WorkingDirectory (Join-Path $root "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $provLog -RedirectStandardError ($provLog + ".err")
Write-Host ("provider_pid=" + $provider.Id)

# 2) 后端（uv run --frozen），隔离数据目录
$env:QIO_DATA_DIR = $data
$env:QIO_PORT = "8734"
$env:QIO_HOST = "127.0.0.1"
$env:QIO_DEV_INSECURE = "1"
$env:QIO_ENABLE_TEST_EVENTS = "1"
$backendLog = Join-Path $logs "backend.log"
$backend = Start-Process -FilePath "uv" -ArgumentList @("run","--frozen","uvicorn","agent.main:create_app","--factory","--host","127.0.0.1","--port","8734","--log-level","warning") -WorkingDirectory (Join-Path $root "backend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $backendLog -RedirectStandardError ($backendLog + ".err")
Write-Host ("backend_pid=" + $backend.Id)

# 3) 前端 vite dev（5199，strictPort）
$env:VITE_QIO_BACKEND_URL = "http://127.0.0.1:8734"
$viteLog = Join-Path $logs "vite.log"
$vite = Start-Process -FilePath $node -ArgumentList @("node_modules/vite/bin/vite.js","--port","5199","--strictPort","--host","127.0.0.1") -WorkingDirectory (Join-Path $root "frontend") -PassThru -WindowStyle Hidden -RedirectStandardOutput $viteLog -RedirectStandardError ($viteLog + ".err")
Write-Host ("vite_pid=" + $vite.Id)

$pids = @{ provider = $provider.Id; backend = $backend.Id; vite = $vite.Id; startedAt = (Get-Date).ToString("o"); dataDir = $data }
$pids | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logs "pids.json") -Encoding UTF8

function WaitHttp([string]$url, [int]$timeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try { $null = Invoke-RestMethod -Uri $url -TimeoutSec 3; return $true } catch { Start-Sleep -Milliseconds 800 }
  }
  return $false
}
$provReady = WaitHttp "http://127.0.0.1:8807/__health" 40
$backendReady = WaitHttp "http://127.0.0.1:8734/api/health" 240
$viteReady = WaitHttp "http://127.0.0.1:5199/" 90
Write-Host ("provider_ready=$provReady backend_ready=$backendReady vite_ready=$viteReady")
if (-not $provReady) { Write-Host "--- provider.log ---"; Get-Content $provLog -Tail 20 -ErrorAction SilentlyContinue }
if (-not $backendReady) { Write-Host "--- backend.log ---"; Get-Content $backendLog -Tail 30 -ErrorAction SilentlyContinue; Get-Content ($backendLog + ".err") -Tail 30 -ErrorAction SilentlyContinue }
if (-not $viteReady) { Write-Host "--- vite.log ---"; Get-Content $viteLog -Tail 30 -ErrorAction SilentlyContinue }
if ($provReady -and $backendReady -and $viteReady) { Write-Host "ENV_OK"; exit 0 } else { Write-Host "ENV_FAIL"; exit 1 }
