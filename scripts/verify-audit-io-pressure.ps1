# D 独立验证：附件 I/O 的「最大单次停顿」在 **CPU 压力**下的复跑装置
#
# 背景（Lead 2026-10-07）：CI 两个后端 job 都在同一条用例上失败
#   tests/test_audit_attachment_io_verify.py::test_relocate_does_not_block_the_event_loop
#   windows-latest 最大单次停顿 459ms / py3.12 228ms（阈值 120ms）；本机空载只有 13–16ms。
# CI 的结论是：这条口径在共享 CPU / 高负载下抓得住真问题（工作线程分块拷贝/哈希长时间握住 GIL）。
# 本脚本用来给出「压力方式 + 前后数字」的可复现证据。
#
# 跑法（在 worktree 里）：
#     powershell -ExecutionPolicy Bypass -File scripts/verify-audit-io-pressure.ps1
#     powershell -ExecutionPolicy Bypass -File scripts/verify-audit-io-pressure.ps1 -Spinners 8
#
# 做三件事：
#   1) 空载：跑该文件，抓 [诊断] 行；
#   2) 压力：起 N 个纯 CPU 忙进程（默认 = 逻辑核数），确认负载真的上去了，再跑同一条；
#   3) 收尾：杀掉忙进程，打印对照表（最大单次停顿 / 累计 / 探针）。
# 断言语义与阈值**不改**（120ms 是 Lead 裁决的口径）。
param(
  [int]$Spinners = 0,
  [string]$Worktree = "",
  # 只跑压力档（重复取样时用；对照表里就没有空载行）
  [switch]$SkipIdle
)

$ErrorActionPreference = "Continue"
if (-not $Worktree) { $Worktree = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
$backend = Join-Path $Worktree "backend"
$py = Join-Path $backend ".venv\Scripts\python.exe"
$target = "tests/test_audit_attachment_io_verify.py"

function Get-Load {
  try { return (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average } catch { return -1 }
}

function Get-Diag([string]$logPath) {
  $lines = Get-Content $logPath -Encoding Unicode -ErrorAction SilentlyContinue
  if (-not $lines) { $lines = Get-Content $logPath -ErrorAction SilentlyContinue }
  $diag = @()
  foreach ($line in $lines) {
    if ($line -match '\[诊断\]') { $diag += ($line -replace '\s+', ' ').Trim() }
  }
  return $diag
}

function Run-Once([string]$label, [string]$logPath) {
  Push-Location $backend
  $env:PYTHONPATH = "src"
  & $py -m pytest $target -q --tb=line -p no:cacheprovider -s *> $logPath
  $code = $LASTEXITCODE
  Pop-Location
  $diag = Get-Diag $logPath
  $load = Get-Load
  Write-Output ("[" + $label + "] exit=" + $code + "  load=" + $load + "%")
  $diag | ForEach-Object { Write-Output ("  " + $_) }
  return [pscustomobject]@{ Label = $label; Exit = $code; Load = $load; Diag = ($diag -join " | ") }
}

if ($Spinners -le 0) { $Spinners = [Environment]::ProcessorCount }
$stamp = Get-Date -Format "HHmmss"
$outDir = Join-Path $env:TEMP ("qio-io-pressure-" + $stamp)
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Write-Output ("== 装置 ==")
Write-Output ("worktree: " + $Worktree)
Write-Output ("逻辑核数: " + [Environment]::ProcessorCount + "；压力进程数: " + $Spinners)
Write-Output ("日志目录: " + $outDir)

$baseline = [pscustomobject]@{ Label = "空载(跳过)"; Exit = -1; Load = -1; Diag = "n/a" }
if (-not $SkipIdle) {
  Write-Output ""
  Write-Output "== 1) 空载基线 =="
  $baseline = Run-Once "空载" (Join-Path $outDir "idle.log")
}

# 注意：PowerShell 变量名大小写不敏感 —— 不要再声明 $spinners/$Spinners，会撞到参数（实测踩过）
$burners = @()
$pressured = [pscustomobject]@{ Label = "压力(未跑)"; Exit = -1; Load = -1; Diag = "n/a" }
try {
  Write-Output ""
  Write-Output "== 2) CPU 压力 =="
  $nl = [Environment]::NewLine
  $busy = "import time" + $nl + "end = time.time() + 900" + $nl + "while time.time() < end: pass"
  $busyPath = Join-Path $outDir "busy.py"
  Set-Content -Path $busyPath -Value $busy -Encoding ASCII
  for ($i = 0; $i -lt $Spinners; $i++) {
    $burners += Start-Process -FilePath $py -ArgumentList @($busyPath) -PassThru -WindowStyle Hidden
  }
  $peak = 0
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    $load = Get-Load
    if ($load -gt $peak) { $peak = $load }
    if ($load -ge 80) { break }
  }
  Write-Output ("压力进程: " + $burners.Count + "；观测到的最高 CPU 负载: " + $peak + "%")
  $pressured = Run-Once "压力" (Join-Path $outDir "pressure.log")
} finally {
  Write-Output ""
  Write-Output "== 3) 收尾 =="
  foreach ($proc in $burners) {
    if ($proc -and -not $proc.HasExited) { & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null }
  }
  Write-Output ("已结束 " + $burners.Count + " 个压力进程；收尾后 load=" + (Get-Load) + "%")
}

Write-Output ""
Write-Output "== 对照（同一条用例、同一阈值 120ms）=="
Write-Output ("空载 : exit=" + $baseline.Exit + " :: " + $baseline.Diag)
Write-Output ("压力 : exit=" + $pressured.Exit + " :: " + $pressured.Diag)
Write-Output "结论：压力下的最大单次停顿见上面 [诊断] 行；阈值与断言语义未改。"

if ($pressured.Exit -ne 0) { exit 1 }
exit 0
