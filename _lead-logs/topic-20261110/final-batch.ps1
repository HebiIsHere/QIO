# 本轮唯一完整验收批次（本地，后台继续运行；日志与 JUnit 全部落盘）
$ErrorActionPreference = "Continue"
$root = "D:\qio-dev\qio"
$out = Join-Path $root "_lead-logs\topic-20261110\final"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$junit = Join-Path $out "junit"
New-Item -ItemType Directory -Force -Path $junit | Out-Null
$summary = @{}
function Record($name, $code) {
  $script:summary[$name] = $code
  Add-Content -Path (Join-Path $out "summary.txt") -Value ("{0}_exit={1} at={2}" -f $name, $code, (Get-Date -Format o)) -Encoding utf8
}
$sha = (git -C $root rev-parse HEAD).Trim()
Set-Content -Path (Join-Path $out "meta.txt") -Encoding utf8 -Value @(
  "sha=$sha",
  "start_time=$(Get-Date -Format o)",
  "timezone=Asia/Shanghai",
  "host=$env:COMPUTERNAME",
  "backend_workers=8",
  "note=本地唯一完整验收批次：后端并行+串行分组、前端类型检查+全量测试、离线评测、文档/合规/workflow/分区检查"
)

# 1) 后端完整测试（并行组 + 串行组，分阶段 JUnit）
Set-Location (Join-Path $root "backend")
& .\.venv\Scripts\python.exe ..\scripts\run_backend_suite.py --workers 8 --junit-dir $junit *> (Join-Path $out "backend.log")
Record "backend_suite" $LASTEXITCODE

# 2) 前端类型检查
Set-Location (Join-Path $root "frontend")
npx vue-tsc --noEmit *> (Join-Path $out "frontend-tsc.log")
Record "frontend_tsc" $LASTEXITCODE

# 3) 前端全量测试
npx vitest run *> (Join-Path $out "frontend-tests.log")
Record "frontend_tests" $LASTEXITCODE

# 4) 离线评测基线对比（runtime/工具策略变更要求）
Set-Location (Join-Path $root "backend")
$env:PYTHONPATH = "src"
& .\.venv\Scripts\python.exe -m agent.eval.run *> (Join-Path $out "eval.log")
Record "eval_run" $LASTEXITCODE

# 5) 文档 / 第三方声明 / workflow 契约 / 测试分区
Set-Location $root
& .\backend\.venv\Scripts\python.exe scripts\check_docs.py *> (Join-Path $out "check-docs.log")
Record "check_docs" $LASTEXITCODE
& .\backend\.venv\Scripts\python.exe scripts\licenses\check_third_party.py *> (Join-Path $out "check-third-party.log")
Record "check_third_party" $LASTEXITCODE
python scripts\targeted-verify\verify_workflow_contract.py *> (Join-Path $out "workflow-contract.log")
Record "workflow_contract" $LASTEXITCODE
& .\backend\.venv\Scripts\python.exe scripts\test_partition_check.py *> (Join-Path $out "partition-check.log")
Record "partition_check" $LASTEXITCODE

$end = Get-Date -Format o
$all = ($summary.Values | Where-Object { $_ -ne 0 }).Count
Add-Content -Path (Join-Path $out "summary.txt") -Value ("end_time=$end") -Encoding utf8
Add-Content -Path (Join-Path $out "summary.txt") -Value ("all_zero_failures=" + ($all -eq 0)) -Encoding utf8
Add-Content -Path (Join-Path $out "summary.txt") -Value ("junit_dir=$junit") -Encoding utf8
