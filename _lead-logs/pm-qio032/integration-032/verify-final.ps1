# 最终固定 SHA 上的本地定向验收（本轮不跑 CI / 全量 pytest / 无筛选 npm test）
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File _lead-logs/pm-qio032/integration-032/verify-final.ps1
param([string]$OutDir = "")
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
$root = (Resolve-Path (Join-Path $here "../../..")).Path
if (-not $OutDir) { $OutDir = Join-Path $here "final" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$log = Join-Path $OutDir "verify.log"
Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
function Say([string]$t) { Write-Output $t; Add-Content -LiteralPath $log -Value $t -Encoding UTF8 }
$fail = 0
$py = Join-Path $root "backend/.venv/Scripts/python.exe"
Push-Location $root
Say "== 0. 身份 =="
$head = (git rev-parse HEAD)
Say ("HEAD=" + $head)
Say ("branch=" + (git rev-parse --abbrev-ref HEAD))
Say ("status=" + (((git status --porcelain=v1) -join "; ")))
Say ""
Say "== 1. 五个来源 SHA 的祖先关系 =="
$sources = @(
  @("shell","a8c9222b1525ed0e5e6f8874487e7748f070cafb"),
  @("interactive","08d08955549d3843603dc160b4da2fdee1cb799d"),
  @("planet","0db3521dba6e8b38ba2d4a6017fdd1d7bd49c1fa"),
  @("settings","baf2a1f9ce4c2ef333e73ed68bba7f56b7afb2ab"),
  @("records","30575cebcf942b7c0a4f137996554c962d35a8de")
)
foreach ($s in $sources) {
  git merge-base --is-ancestor $s[1] HEAD
  $ok = ($LASTEXITCODE -eq 0)
  if (-not $ok) { $fail += 1 }
  Say ("  " + $s[0] + " " + $s[1].Substring(0,8) + " ancestor=" + $ok)
}
Say ""
Say "== 2. 冲突标记 / 空白 =="
$markers = (git grep -n -E "^(<<<<<<< |>>>>>>> )" -- . 2>$null | Measure-Object).Count
Say ("conflict_marker_lines=" + $markers)
if ($markers -ne 0) { $fail += 1 }
Say ("diff_check=" + ((git diff --check 2>&1 | Out-String).Trim()))
Say ""
Say "== 3. 后端定向 pytest（材料版本与首轮读取 + 静态边界）=="
$env:QIO_DATA_DIR = Join-Path $root ".tmp-test-data"
$env:QIO_DISABLE_DB_CHECK = "1"
Push-Location (Join-Path $root "backend")
$pytestFiles = @(
  "tests/pm_qio032_material_scope_test.py",
  "tests/pm_qio032_submit_version_basis.py",
  "tests/pm_qio032_submit_material_versions.py",
  "tests/pm_qio032_submit_first_round_materials.py",
  "tests/test_p3_b_tools.py",
  "tests/test_p3_d_tools_registry.py",
  "tests/test_p2_d_participation_endpoints_display_version.py",
  "tests/test_p3_d_static_boundaries.py",
  "tests/test_p2_d_static_boundaries.py"
)
& $py -m pytest @pytestFiles -q -p no:cacheprovider 2>&1 | Tee-Object -FilePath (Join-Path $OutDir "pytest.log") | Select-Object -Last 4
$pytestExit = $LASTEXITCODE
Say ("pytest_exit=" + $pytestExit)
if ($pytestExit -ne 0) { $fail += 1 }
Pop-Location
Say ""
Say "== 4. 前端定向 vitest（受影响入口）=="
Push-Location (Join-Path $root "frontend")
$files = @(
  "src/components/interactive/__tests__/pm032-canvas-import-batch.test.ts",
  "src/components/interactive/__tests__/pm032-canvas-group-ops.test.ts",
  "src/conversation/layoutPrefs.test.ts",
  "src/views/settings/__tests__/settings-search.test.ts",
  "src/views/settings/__tests__/settings-content.test.ts",
  "src/views/settings/__tests__/settings-reset-layout.test.ts",
  "src/components/conversation/__tests__/cv-c-preview-50-53-state.test.ts",
  "src/components/conversation/__tests__/d-preview-52-tab-focus.test.ts",
  "src/components/conversation/__tests__/cv-c-preview-feedback.test.ts",
  "src/views/__tests__/i2v2-a-recovery-entry.test.ts",
  "src/components/__tests__/fu-w5-RecoveryInbox.spec.ts",
  "src/services/__tests__/uf2-notice-navigation.test.ts",
  "src/components/interactive/__tests__/fix025-attach-notice.test.ts"
)
& npx --no-install vitest run @files 2>&1 | Tee-Object -FilePath (Join-Path $OutDir "vitest.log") | Select-Object -Last 5
$vitestExit = $LASTEXITCODE
Say ("vitest_exit=" + $vitestExit)
if ($vitestExit -ne 0) { $fail += 1 }
Say ""
Say "== 5. 前端类型检查 =="
& npx --no-install vue-tsc --noEmit 2>&1 | Tee-Object -FilePath (Join-Path $OutDir "vue-tsc.log") | Select-Object -Last 3
$tscExit = $LASTEXITCODE
Say ("vue_tsc_exit=" + $tscExit)
if ($tscExit -ne 0) { $fail += 1 }
Pop-Location
Say ""
Say "== 6. 文档一致性 / workflow 契约 =="
& $py (Join-Path $root "scripts/check_docs.py") 2>&1 | Tee-Object -FilePath (Join-Path $OutDir "check-docs.log")
$docsExit = $LASTEXITCODE
Say ("check_docs_exit=" + $docsExit)
if ($docsExit -ne 0) { $fail += 1 }
& $py (Join-Path $root "scripts/targeted-verify/verify_workflow_contract.py") 2>&1 | Select-Object -Last 2 | Tee-Object -FilePath (Join-Path $OutDir "workflow-contract.log")
$wfExit = $LASTEXITCODE
Say ("workflow_contract_exit=" + $wfExit)
if ($wfExit -ne 0) { $fail += 1 }
Say ""
Say ("== 结论：fail=" + $fail + " ==")
Pop-Location
if ($fail -ne 0) { exit 1 }
exit 0