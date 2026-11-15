# QIO #46 第八轮（R8）浏览器验收 · 环境收尾
# 只杀本环境 pids.json 记录的 pid，并核对端口不再监听。
param(
  [string]$Name = "main",
  [int[]]$Ports = @(5201, 8735, 8808),
  [string]$OutRoot = "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8"
)
$logs = Join-Path $OutRoot "logs"
$pidsFile = Join-Path $logs ($Name + "-pids.json")
if (Test-Path $pidsFile) {
  $pids = Get-Content $pidsFile -Raw | ConvertFrom-Json
  foreach ($key in @("provider","backend","vite")) {
    $procId = $pids.$key
    if ($procId) { & taskkill /PID $procId /T /F 2>&1 | Out-Null; Write-Host ("killed " + $key + " pid=" + $procId) }
  }
}
Start-Sleep -Seconds 2
foreach ($p in $Ports) {
  $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    foreach ($procId in ($conn | Select-Object -ExpandProperty OwningProcess -Unique)) { & taskkill /PID $procId /T /F 2>&1 | Out-Null; Write-Host ("killed leftover port " + $p + " pid=" + $procId) }
  }
}
Start-Sleep -Seconds 1
$still = @()
foreach ($p in $Ports) { $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; if ($c) { $still += $p } }
if ($still.Count) { Write-Host ("STILL_LISTENING=" + ($still -join ',')); exit 1 } else { Write-Host "ALL_PORTS_FREE"; exit 0 }
