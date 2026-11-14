# QIO032-46 定向验收 · 环境收尾：只杀本脚本记录的 pid + 验证端口不再响应
$root = "D:\qio-dev\pm-qio032-integration"
$out  = Join-Path $root "_lead-logs\qio032-46"
$logs = Join-Path $out "logs"
$pidsFile = Join-Path $logs "pids.json"
if (Test-Path $pidsFile) {
  $pids = Get-Content $pidsFile -Raw | ConvertFrom-Json
  foreach ($name in @("provider","backend","vite")) {
    $procId = $pids.$name
    if ($procId) { & taskkill /PID $procId /T /F 2>&1 | Out-Null; Write-Host ("killed " + $name + " pid=" + $procId) }
  }
}
Start-Sleep -Seconds 2
foreach ($p in 8734,5199,8807) {
  $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    foreach ($procId in ($conn | Select-Object -ExpandProperty OwningProcess -Unique)) { & taskkill /PID $procId /T /F 2>&1 | Out-Null; Write-Host ("killed leftover port " + $p + " pid=" + $procId) }
  }
}
Start-Sleep -Seconds 1
$still = @()
foreach ($p in 8734,5199,8807) { $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; if ($c) { $still += $p } }
if ($still.Count) { Write-Host ("STILL_LISTENING=" + ($still -join ',')); exit 1 } else { Write-Host "ALL_PORTS_FREE"; exit 0 }
