<#
  [closure-A / 反例 R4] 证据复跑脚本（仓库相对路径，不写死盘符）。

  用法（在仓库任意位置执行；Windows PowerShell 5.1 即可，有 pwsh 也可）：
    powershell -NoProfile -File scripts/closure-a-verify/verify.ps1
        # 跑单元用例 + 组件级用例，要求全绿
    powershell -NoProfile -File scripts/closure-a-verify/verify.ps1 -BaselineRed
        # 基线复现：store 未接线时跑组件用例，期望「2 红 5 绿」并写出 baseline-red.txt
    powershell -NoProfile -File scripts/closure-a-verify/verify.ps1 -StorePatch <补丁路径>
        # 临时应用 lead 的 store 接线补丁（工作区未提交）跑绿，跑完 git restore 还原；
        # 若 frontend/src/stores/interactive.ts 在跑之前就是脏的，脚本直接拒绝执行（不覆盖别人的改动）。

  层次说明：本脚本的「重开」是同一份本机 localStorage + 新建 pinia/store 实例（组件级等价物）；
  真进程关闭重开由验收角色 D 负责。
#>
param(
  [switch]$BaselineRed,
  [string]$StorePatch = ""
)

# 不要用 Stop：npx 通过 stderr 输出进度时会产生 NativeCommandError 记录，Stop 会提前终止脚本
$ErrorActionPreference = "Continue"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$frontend = Join-Path $root "frontend"
$storeRel = "frontend/src/stores/interactive.ts"

$unitTest = "src/interactive/__tests__/closure-a-local-removal-purpose.test.ts"
$componentTest = "src/components/interactive/__tests__/closure-a-r4-reopen.test.ts"

function Invoke-Vitest([string]$relativeTestPath, [string]$logName) {
  $log = Join-Path $PSScriptRoot $logName
  Push-Location $frontend
  try {
    $output = & npx vitest run $relativeTestPath --reporter=verbose *>&1 | Out-String
    $code = $LASTEXITCODE
  } finally {
    Pop-Location
  }
  $output | Out-File -FilePath $log -Encoding utf8
  Write-Host "---- $relativeTestPath (exit=$code, log=$logName) ----"
  $display = ($output -split "`r?`n") | Select-String -Pattern "Test Files|Tests  |FAIL |×" | Select-Object -Last 20 | ForEach-Object { $_.Line }
  Write-Host ($display -join "`n")
  return $code
}

$restoreNeeded = $false
if ($StorePatch) {
  $dirty = & git -C $root status --porcelain -- $storeRel
  if ($dirty) {
    throw "$storeRel 在当前工作区已是脏的：请先 git restore 再跑，避免覆盖别人未提交的改动。"
  }
  & git -C $root apply $StorePatch
  if ($LASTEXITCODE -ne 0) { throw "git apply 失败：$StorePatch" }
  $restoreNeeded = $true
  Write-Host "已临时应用 store 补丁：$StorePatch"
}

try {
  $unitCode = Invoke-Vitest $unitTest "unit-output.txt"
  if ($unitCode -ne 0) { Write-Host "单元用例未全绿（exit=$unitCode）"; exit 1 }

  $logName = if ($BaselineRed) { "baseline-red.txt" } else { "component-output.txt" }
  $componentCode = Invoke-Vitest $componentTest $logName

  if ($BaselineRed) {
    # 基线复现：期望看到 2 条 R4 失败（cleared 依据 + 重开后服务器稿变空）
    $tail = Get-Content (Join-Path $PSScriptRoot "baseline-red.txt") -Raw
    if ($componentCode -eq 0) { throw "基线复现失败：store 未接线时组件用例本应报红" }
    if ($tail -notmatch "Tests  2 failed \| 5 passed") { throw "基线复现失败：期望 2 红 5 绿，实际见 baseline-red.txt" }
    if ($tail -notmatch '"kind": "cleared"') { throw "基线红里看不到 kind=cleared 证据" }
    Write-Host "基线红复现成功：2 failed | 5 passed，且含 kind=cleared 证据（见 baseline-red.txt）"
    exit 0
  }

  if ($componentCode -ne 0) { Write-Host "组件用例未全绿（exit=$componentCode）"; exit 1 }
  Write-Host "全绿：单元 14 + 组件 7 用例（见 unit-output.txt / component-output.txt）"
  exit 0
} finally {
  if ($restoreNeeded) {
    & git -C $root restore -- $storeRel
    Write-Host "已还原 $storeRel（未提交 store 改动）"
  }
}
