# <#
#   本轮回归门禁（可复跑）：在**冻结提交**上一次性跑完
#     1) 前端类型检查 vue-tsc --noEmit
#     2) 前端全量 vitest run（store/services/types 是全局共享模块，前端整包就是受影响范围）
#     3) 后端**受影响模块**回归：所有引用 interactive / board_store 的测试文件（不机械重跑后端全仓）
#     4) 文档一致性 scripts/check_docs.py
#   每条命令的真实输出与退出码落到 scripts/recovery-lead-verify/out/（运行产物，不入库）。
#
#   用法：powershell -NoProfile -File scripts/recovery-lead-verify/final-gate.ps1
#   后端解释器固定用 backend/.venv（pytest 在 dev 可选依赖组里，必须 --extra dev 才会装）。
# #>
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
  Write-Output ('== ' + $name)
  $lines = & $body 2>&1 | Out-String
  $exit = $LASTEXITCODE
  $lines | Out-File (Join-Path $OutDir $file) -Encoding utf8
  ('exit=' + $exit) | Out-File (Join-Path $OutDir $file) -Encoding utf8 -Append
  Write-Output ('   exit=' + $exit + '  -> ' + $file)
  return $exit
}

$sha = (git -C $repo rev-parse HEAD)
$branch = (git -C $repo rev-parse --abbrev-ref HEAD)
('branch=' + $branch) | Out-File (Join-Path $OutDir 'gate-sha.txt') -Encoding utf8
('sha=' + $sha) | Out-File (Join-Path $OutDir 'gate-sha.txt') -Encoding utf8 -Append
('dirty=' + ((git -C $repo status --porcelain=v1) -join '|')) | Out-File (Join-Path $OutDir 'gate-sha.txt') -Encoding utf8 -Append
Write-Output ('== 冻结提交 ' + $branch + ' ' + $sha)

$exits = [ordered]@{}
$exits.tsc = Section '前端类型检查（vue-tsc --noEmit）' 'tsc.txt' { Set-Location $frontend; npx vue-tsc --noEmit; }
$exits.frontend = Section '前端全量（vitest run）' 'vitest-full.txt' { Set-Location $frontend; npx vitest run; }

if (-not (Test-Path $python)) {
  Write-Output ('缺少后端解释器：' + $python + ' —— 先跑 uv sync --frozen --extra dev')
  $exits.backend = 127
} else {
  $affected = Get-ChildItem (Join-Path $backend 'tests') -Filter 'test_*.py' |
    Where-Object { (Get-Content $_.FullName -Raw) -match 'interactive|board_store' } |
    ForEach-Object { 'tests/' + $_.Name }
  Write-Output ('   受影响后端测试文件 ' + $affected.Count + ' 个')
  $junitArg = '--junit-xml=' + (Join-Path $OutDir 'pytest-junit.xml')
  $affected | Out-File (Join-Path $OutDir 'backend-affected-files.txt') -Encoding utf8
  $exits.backend = Section '后端受影响模块回归（pytest -q）' 'pytest-affected.txt' {
    Set-Location $backend
    & $python -m pytest -q $junitArg @affected
  }
}
$exits.docs = Section '文档一致性（check_docs.py）' 'check-docs.txt' { Set-Location $repo; & $python (Join-Path $repo 'scripts\check_docs.py'); }
Set-Location $repo

$summary = ($exits.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value }) -join ' '
Write-Output ('== 汇总 ' + $summary)
$summary | Out-File (Join-Path $OutDir 'summary.txt') -Encoding utf8
if (($exits.Values | Where-Object { $_ -ne 0 }).Count -gt 0) { exit 1 }
exit 0
