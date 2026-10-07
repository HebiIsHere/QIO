# 等独立复核报告写完（文件超过 3KB 或出现新提交），最多 25 分钟
$f = 'D:\qio-dev\qio-ui\docs\interactive-ui-verify-integrated.md'
$base = (git -C D:\qio-dev\qio-ui rev-parse HEAD)
$deadline = (Get-Date).AddMinutes(25)
while ((Get-Date) -lt $deadline) {
  $size = (Get-Item $f).Length
  $head = (git -C D:\qio-dev\qio-ui rev-parse HEAD)
  if ($size -gt 3000 -or $head -ne $base) {
    Write-Output ("复核报告已更新：大小=" + $size + " 字节；HEAD=" + $head.Substring(0,7) + "（起始 " + $base.Substring(0,7) + "）")
    break
  }
  Start-Sleep -Seconds 30
}
Write-Output ("最终大小=" + (Get-Item $f).Length)
git -C D:\qio-dev\qio-ui log --oneline -4
