# D 耗时口径取证（契约 §3 + task-4 第 2 条）——可复现命令 + 真实数字
#
# 跑法（在 worktree 根目录或任意位置；Windows PowerShell 5.1 与 pwsh 都可以）：
#     powershell -ExecutionPolicy Bypass -File scripts/verify-timing-accounting.ps1
#     pwsh -File scripts/verify-timing-accounting.ps1
#
# 做两件事：
#   1) 跑耗时契约用例（基线应为红：TURN_END 还没有 duration_ms/queue_ms/started_at/ended_at）；
#   2) 真跑一轮「三个并行工具」的 turn，打印 TIMING_EVIDENCE：
#      duration_ms / 顶层阶段合计 / 全部 span 原始合计 / 各工具耗时合计 /
#      tool_wait 墙钟 / model_wait / queue_wait / after_turn。
#
# 说明：第 2 步量的是 agent/trace 台账的既有事实（基线就成立），
# 与「TURN_END 是否带耗时字段」的契约验证是两件事。

# 不用 Stop：uv 会把安装信息写到 stderr，PS 5.1 会把它当 NativeCommandError 终止脚本。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"

Write-Output "== 后端验证测试：耗时契约 =="
Push-Location $backend
# 走 cmd /c：避免 PS 5.1 把 uv 的 stderr 安装信息当 NativeCommandError 刷屏
$contract = (& cmd /c "uv run --frozen --extra dev pytest tests/test_timing_contract_verify.py -q --no-header -p no:cacheprovider --tb=short 2>&1" | Out-String)
$contractExit = $LASTEXITCODE
Pop-Location
Write-Output $contract
Write-Output "pytest(test_timing_contract_verify) exit=$contractExit"

Write-Output ""
Write-Output "== 耗时口径取证：并行三工具一轮 =="
Push-Location $backend
$raw = (& cmd /c "uv run --frozen --extra dev pytest tests/test_timing_contract_verify.py -q --no-header -p no:cacheprovider -s -k parallel 2>&1" | Out-String)
Pop-Location
$lines = $raw -split "\r?\n"
$evidenceLine = ($lines | Where-Object { $_ -like "TIMING_EVIDENCE *" } | Select-Object -Last 1)
$summaryLine = ($lines | Where-Object { $_ -match "passed|failed" } | Select-Object -Last 1)
Write-Output $summaryLine

if (-not $evidenceLine) {
  Write-Output "[FAIL] 没有拿到 TIMING_EVIDENCE（取证用例失败，请看上面的 pytest 输出）"
  exit 1
}
$json = $evidenceLine -replace "^TIMING_EVIDENCE ", ""
Write-Output $json
$data = $json | ConvertFrom-Json

Write-Output ""
Write-Output "== 口径结论（数字来自上面这行） =="
Write-Output ("总耗时 duration_ms           : {0} ms" -f $data.duration_ms)
Write-Output ("顶层阶段合计 top_level_sum   : {0} ms（+ residual {1} ms = duration）" -f $data.top_level_sum_ms, $data.residual_ms)
Write-Output ("全部 span 原始合计           : {0} ms（含嵌套细分，不能当总耗时用）" -f $data.all_spans_raw_sum_ms)
Write-Output ("各工具自身耗时之和           : {0} ms（{1} 次调用）" -f $data.tool_runs_sum_ms, $data.tool_run_count)
Write-Output ("工具批次墙钟 tool_wait       : {0} ms" -f $data.tool_wait_wall_ms)
Write-Output ("排队 queue_wait              : {0} ms" -f $data.queue_wait_ms)
Write-Output ("轮后整理 after_turn          : {0} ms" -f $data.after_turn_ms)
$overstate = [int]$data.tool_runs_sum_ms - [int]$data.tool_wait_wall_ms
Write-Output ("结论：并行分项求和比批次墙钟多出 {0} ms —— 界面只能用 duration_ms 当总耗时，" -f $overstate)
Write-Output "      分项只作占比说明；residual 由后端按 duration - 顶层合计算，不得为负。"
exit $contractExit
