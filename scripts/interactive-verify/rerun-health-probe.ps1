# 复跑健康探测单测 5 次，记录负载与结果（不降低门槛、不删测试）
Set-Location D:\qio-dev\qio-ui\backend
$t = 'tests/test_interactive_during_heavy_work.py::test_health_probe_stays_responsive_while_slow_prediction_runs'
Write-Output '=== 单测复跑 5 次：健康探测在慢推理期间是否仍 <100ms ==='
for ($i = 1; $i -le 5; $i++) {
  $cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
  $freeGB = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1024 / 1024, 1)
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $out = uv run --frozen pytest $t -q --color=no --tb=short 2>&1 | Out-String
  $code = $LASTEXITCODE
  $sw.Stop()
  Write-Output ("第 " + $i + " 次：退出=" + $code + " 用时=" + [math]::Round($sw.Elapsed.TotalSeconds, 1) + "s CPU=" + $cpu + "% 空闲=" + $freeGB + "GB")
  $lines = ($out -split "`n" | Where-Object { $_.Trim() -ne '' })
  $last = $lines | Select-Object -Last 1
  if ($last) { Write-Output ("  末尾：" + $last.Trim()) }
  $gap = ($out -split "`n" | Where-Object { $_ -match '被占住' })
  if ($gap) { Write-Output ("  失败详情：" + ($gap -join ' / ')) }
}
