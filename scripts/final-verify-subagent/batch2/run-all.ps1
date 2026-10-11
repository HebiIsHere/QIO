# 第二批独立对抗性探针 · 一键复跑（未突变 + 全部突变），证据落在本目录 evidence/。
# 关键约束：起任何脚本前显式设置 QIO_DATA_DIR 到临时目录（本机环境变量指向用户真实库）。
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $here '..\..\..')).Path
$frontend = Join-Path $repo 'frontend'
$backend = Join-Path $repo 'backend'
$evidence = Join-Path $here 'evidence'
New-Item -ItemType Directory -Force -Path $evidence | Out-Null

$env:QIO_DATA_DIR = Join-Path $env:TEMP ('qio-b2-runall-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $env:QIO_DATA_DIR | Out-Null
Write-Host ('[ENV] QIO_DATA_DIR=' + $env:QIO_DATA_DIR)
Write-Host ('[ENV] repo=' + $repo)
Write-Host ('[ENV] HEAD=' + (git -C $repo rev-parse HEAD))

$cfg = Join-Path $here 'vitest.probe.config.mjs'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$probe08 = Join-Path $here 'probe08_impact_gate.py'
$mutate = Join-Path $here 'mutate-run.mjs'

Write-Host ''
Write-Host '===== 1) 未突变：前端 batch2 三个探针 ====='
Push-Location $frontend
npx vitest run --config $cfg 2>&1 | Tee-Object -FilePath (Join-Path $evidence 'batch2-vitest-unmutated.txt')
$vitestExit = $LASTEXITCODE
Pop-Location

Write-Host ''
Write-Host '===== 2) 未突变：后端 08 探针（真实 sqlite 临时库）====='
& $python $probe08 2>&1 | Tee-Object -FilePath (Join-Path $evidence 'batch2-probe08-unmutated.txt')
$pyExit = $LASTEXITCODE

$mutations = @(
  @{ n = 'm06_newerCandidate_false'; f = 'probe-06-board-rev' },
  @{ n = 'm07_consume_disabled'; f = 'probe-07-draft-clear-binding' },
  @{ n = 'm02_drop_version'; f = 'probe-02-04-send-identity' },
  @{ n = 'm03_drop_topic'; f = 'probe-02-04-send-identity' },
  @{ n = 'm03_drop_capture_topic_check'; f = 'probe-02-04-send-identity' },
  @{ n = 'm03_drop_bind_disarm'; f = 'probe-02-04-send-identity' },
  @{ n = 'm03_drop_bind_and_capture'; f = 'probe-02-04-send-identity' },
  @{ n = 'm08_drop_server_gate'; f = '' },
  @{ n = 'm08_drop_stale_check'; f = '' },
  @{ n = 'm08_drop_stale_state'; f = '' }
)
foreach ($m in $mutations) {
  Write-Host ''
  Write-Host ('===== 3) 突变 ' + $m.n + ' =====')
  $out = Join-Path $evidence ('mut-' + $m.n + '.txt')
  if ($m.f) {
    node $mutate $m.n $m.f 2>&1 | Tee-Object -FilePath $out
  } else {
    node $mutate $m.n 2>&1 | Tee-Object -FilePath $out
  }
}

Write-Host ''
Write-Host '===== 4) 还原后工作区状态 ====='
git -C $repo status --short 2>&1 | Tee-Object -FilePath (Join-Path $evidence 'git-status-after-mutations.txt')
git -C $repo diff --stat 2>&1 | Tee-Object -FilePath (Join-Path $evidence 'git-diff-stat-after-mutations.txt')

Write-Host ''
Write-Host ('[SUMMARY] 未突变 vitest 退出码=' + $vitestExit + '；未突变 python 退出码=' + $pyExit)
