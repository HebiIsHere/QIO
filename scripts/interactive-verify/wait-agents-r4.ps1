# 等 A/B/C/D 交出提交（或超时）
$dirs = @{
  'A' = 'D:\qio-dev\qio-polish-a'; 'B' = 'D:\qio-dev\qio-polish-b'; 'C' = 'D:\qio-dev\qio-polish-c'; 'D' = 'D:\qio-dev\qio-polish-d'
}
$deadline = (Get-Date).AddMinutes(28)
while ((Get-Date) -lt $deadline) {
  $done = 0
  foreach ($k in $dirs.Keys) { if ([int](git -C $dirs[$k] rev-list --count e8afbb6..HEAD) -gt 0) { $done++ } }
  if ($done -ge 3) { Write-Output ("已有 " + $done + " 个子智能体提交"); break }
  Start-Sleep -Seconds 40
}
foreach ($k in $dirs.Keys) {
  $d = $dirs[$k]
  Write-Output ($k + "：提交=" + (git -C $d rev-list --count e8afbb6..HEAD) + " 未提交=" + ((git -C $d status --porcelain | Measure-Object -Line).Lines))
  git -C $d log --oneline e8afbb6..HEAD 2>&1 | Select-Object -First 3 | ForEach-Object { "   " + $_ }
  git -C $d status --short | Select-Object -First 3 | ForEach-Object { "   (未提交) " + $_ }
}
