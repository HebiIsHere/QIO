# D 独立验收（阶段一）：一键复跑「审计七项」的验收用例（修复前应红，修复后应全绿）
#
# 用法：
#   pwsh -File scripts/verify-audit-phase1.ps1
#   pwsh -File scripts/verify-audit-phase1.ps1 -Worktree D:\qio-dev\qio-fix-d
#
# 不做的事：不联网、不需要真实 Key、不启动应用（真机验证见 docs/verification-audit-phase2.md）。
param(
  [string]$Worktree = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Continue"
$backend = Join-Path $Worktree "backend"
$frontend = Join-Path $Worktree "frontend"

$backendFiles = @(
  "tests/test_audit_stream_role_verify.py",
  "tests/test_audit_attachment_binding_verify.py",
  "tests/test_audit_attachment_io_verify.py",
  "tests/test_audit_attachment_content_verify.py",
  "tests/test_audit_turn_end_facts_verify.py"
)

$frontendFiles = @(
  "src/components/__tests__/ProcessDefaultVisibility.audit.verify.test.ts",
  "src/components/__tests__/ApprovalFacts.audit.verify.test.ts",
  "src/components/__tests__/AnswerRoleStability.audit.verify.test.ts",
  "src/components/__tests__/HistoryAttachmentOpen.audit.verify.test.ts",
  "src/stores/__tests__/TurnEndFacts.audit.verify.test.ts",
  "src/stores/__tests__/SendAttachmentIds.audit.verify.test.ts"
)

Write-Output "== 后端验收（事件层：真 HTTP/SSE 假厂商 + 真 AppContext/HTTP API） =="
Push-Location $backend
try {
  uv run --frozen --extra dev pytest @backendFiles -q --tb=line -p no:cacheprovider
  $backendExit = $LASTEXITCODE
} finally {
  Pop-Location
}

Write-Output "== 前端验收（DOM 层：真挂载组件） =="
Push-Location $frontend
try {
  npx vitest run @frontendFiles
  $frontendExit = $LASTEXITCODE
  Write-Output "== 类型检查 =="
  npx vue-tsc --noEmit
  $tscExit = $LASTEXITCODE
} finally {
  Pop-Location
}

Write-Output ("backend exit={0}  vitest exit={1}  vue-tsc exit={2}" -f $backendExit, $frontendExit, $tscExit)
if ($backendExit -ne 0 -or $frontendExit -ne 0 -or $tscExit -ne 0) {
  Write-Output "有红：符合预期（修复前）/ 需要排查（修复后）"
  exit 1
}
Write-Output "全绿：七项验收通过"
exit 0
