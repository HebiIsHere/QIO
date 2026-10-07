# 等四个子智能体交出提交（或超时），期间只做只读检查
$dirs = @{
  'A' = 'D:\qio-dev\qio-ui-a'; 'B' = 'D:\qio-dev\qio-ui-b'; 'C' = 'D:\qio-dev\qio-ui-c'; 'D' = 'D:\qio-dev\qio-ui-d'
}
$deadline = (Get-Date).AddMinutes(25)
while ((Get-Date) -lt $deadline) {
  $done = 0
  foreach ($k in $dirs.Keys) {
    $c = git -C $dirs[$k] rev-list --count 12266d9..HEAD
    if ([int]$c -gt 0) { $done++ }
  }
  if ($done -eq 4) { Write-Output '四个子智能体都已提交'; break }
  Start-Sleep -Seconds 30
}
foreach ($k in $dirs.Keys) {
  $d = $dirs[$k]
  $c = git -C $d rev-list --count 12266d9..HEAD
  $dirty = (git -C $d status --porcelain | Measure-Object -Line).Lines
  Write-Output ($k + '：提交=' + $c + ' 未提交=' + $dirty)
  git -C $d log --oneline 12266d9..HEAD 2>&1 | Select-Object -First 3 | ForEach-Object { '   ' + $_ }
}
