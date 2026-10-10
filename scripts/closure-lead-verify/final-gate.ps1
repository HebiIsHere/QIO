<#
  最终门禁（可复跑）：在**冻结提交**上一次性跑完前端类型检查、前端全量、后端全量、文档一致性检查，
  把每条命令的真实输出与退出码落到 scripts/closure-lead-verify/out/。

  用法（仓库任意位置）：
    powershell -NoProfile -File scripts/closure-lead-verify/final-gate.ps1

  说明：
  - 脚本不修改任何被版本管理的内容；只写 out/ 目录（该目录不入库，属于运行产物）。
  - 后端解释器固定用 backend/.venv（worktree 自洽）；缺失时脚本会明确报错而不是假装通过。
  - 文档检查用同一个解释器跑 scripts/check_docs.py（stdlib only）。
#>
param(
  [string]$OutDir = (Join-Path $PSScriptRoot 'out')
)
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$frontend = Join-Path $repo 'frontend'
$backend = Join-Path $repo 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Section([string]$name, [string]$file, [scriptblock]$body) {
  Write-Output ("== " + $name)
  $lines = & $body 2>&1 | Out-String
  $exit = $LASTEXITCODE
  $lines | Out-File (Join-Path $OutDir $file) -Encoding utf8
  ("exit=" + $exit) | Out-File (Join-Path $OutDir $file) -Encoding utf8 -Append
  Write-Output ("   exit=" + $exit + "  -> " + $file)
  return $exit
}

$sha = (git -C $repo rev-parse HEAD)
$branch = (git -C $repo rev-parse --abbrev-ref HEAD)
("branch=" + $branch) | Out-File (Join-Path $OutDir 'gate-sha.txt') -Encoding utf8
("sha=" + $sha) | Out-File (Join-Path $OutDir 'gate-sha.txt') -Encoding utf8 -Append
("dirty=" + ((git -C $repo status --porcelain=v1) -join '|')) | Out-File (Join-Path $OutDir 'gate-sha.txt') -Encoding utf8 -Append
Write-Output ("== 冻结提交 " + $branch + " " + $sha)

$exits = [ordered]@{}
$exits.tsc = Section '前端类型检查（vue-tsc --noEmit）' 'tsc.txt' { Set-Location $frontend; npx vue-tsc --noEmit; }
$exits.frontend = Section '前端全量（vitest run）' 'vitest-full.txt' { Set-Location $frontend; npx vitest run; }
if (-not (Test-Path $python)) {
  Write-Output ("缺少后端解释器：" + $python + " —— 先跑 uv sync --frozen --extra dev")
  $exits.backend = 127
} else {
  $exits.backend = Section '后端全量（pytest -q）' 'pytest-full.txt' { Set-Location $backend; & $python -m pytest -q; }
  $exits.docs = Section '文档一致性（check_docs.py）' 'check-docs.txt' { Set-Location $repo; & $python (Join-Path $repo 'scripts\check_docs.py'); }
}
Set-Location $repo

$summary = ($exits.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value }) -join ' '
Write-Output ("== 汇总 " + $summary)
$summary | Out-File (Join-Path $OutDir 'summary.txt') -Encoding utf8
if (($exits.Values | Where-Object { $_ -ne 0 }).Count -gt 0) { exit 1 }
exit 0
