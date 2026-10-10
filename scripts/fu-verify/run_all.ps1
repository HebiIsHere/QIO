# V 组一键验证脚本（补充修复轮 · 独立验证组）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fu-verify\run_all.ps1 -Label baseline
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fu-verify\run_all.ps1 -Label after
#   powershell ... -File scripts\fu-verify\run_all.ps1 -BackendOnly
#   powershell ... -File scripts\fu-verify\run_all.ps1 -FrontendOnly
#
# 三段：
#   1) 后端受控用例   backend/tests/test_fu_verify_*.py
#   2) 原始反例证据   scripts/fu-verify/output/repro/repro_*.py（只打印可观察结果，不做断言）
#   3) 前端受控用例   frontend/src/**/__tests__/fu-v-*.spec.ts（用仓库自带 vitest 配置收集）
#
# 所有原始输出写入 scripts/fu-verify/output/<label>-<part>-<时间戳>.txt（UTF-8），
# 供「基线 vs 集成后」前后对照。退出码：0 = 全绿，1 = 有用例失败。
#
# 基线（da0436b）上本脚本**预期以 1 退出** —— 那一轮红的就是「反例成立」的证据。

[CmdletBinding()]
param(
    [string]$Label = "run",
    [switch]$BackendOnly,
    [switch]$FrontendOnly
)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent $here)
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$outDir = Join-Path $here "output"
$reproDir = Join-Path $outDir "repro"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$exit = 0
$summary = New-Object System.Collections.Generic.List[string]
$summary.Add("label      = $Label")
$summary.Add("stamp      = $stamp")
$summary.Add("worktree   = $root")
$summary.Add("")

function Write-Part {
    param(
        [string]$Name,
        [string]$WorkDir,
        [scriptblock]$Work
    )
    $log = Join-Path $outDir "$Label-$Name-$stamp.txt"
    Write-Host "== $Name ==" -ForegroundColor Cyan
    Push-Location $WorkDir
    try {
        $captured = & $Work
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }
    $captured | Out-File -FilePath $log -Encoding utf8
    Add-Content -Path $log -Value ""
    Add-Content -Path $log -Value "[fu-verify] exit code = $code"
    $captured | Out-Host
    if ($code -ne 0) { $script:exit = 1 }
    $script:summary.Add(("{0,-10} exit={1}  {2}" -f $Name, $code, $log))
    Write-Host "   原始输出：$log" -ForegroundColor DarkGray
}

if (-not $FrontendOnly) {
    $files = Get-ChildItem (Join-Path $backend "tests") -Filter "test_fu_verify_*.py" |
        Sort-Object Name | ForEach-Object { "tests/" + $_.Name }
    if ($files.Count -eq 0) {
        Write-Host "!! 没有找到 backend/tests/test_fu_verify_*.py" -ForegroundColor Red
        $exit = 1
        $summary.Add("backend    exit=1  没有找到 test_fu_verify_*.py")
    }
    else {
        Write-Part -Name "backend" -WorkDir $backend -Work {
            uv run --frozen pytest @files -q --no-header -p no:cacheprovider *>&1
        }
    }

    # 原始反例证据：每个 repro_*.py 单独一段，失败也不中断（它们是「取的现场」）
    $reproLog = Join-Path $outDir "$Label-repro-$stamp.txt"
    Write-Host "== repro（反例现场） ==" -ForegroundColor Cyan
    $reproLines = New-Object System.Collections.Generic.List[string]
    if (Test-Path $reproDir) {
        foreach ($script in (Get-ChildItem $reproDir -Filter "repro_*.py" | Sort-Object Name)) {
            $reproLines.Add("").Add("### $($script.Name)")
            Push-Location $backend
            try {
                $out = & uv run --frozen python $script.FullName *>&1
                $code = $LASTEXITCODE
            }
            finally { Pop-Location }
            $reproLines.AddRange([string[]]$out)
            $reproLines.Add("[fu-verify] $($script.Name) exit code = $code")
            $out | Out-Host
        }
    }
    $reproLines | Out-File -FilePath $reproLog -Encoding utf8
    $summary.Add(("repro      exit=0  {0}" -f $reproLog))
    Write-Host "   原始输出：$reproLog" -ForegroundColor DarkGray
}

if (-not $BackendOnly) {
    $specs = @(
        "src/stores/__tests__/fu-v-a01-recovery-inbox.spec.ts",
        "src/components/planet/__tests__/fu-v-a04-candidates.spec.ts"
    )
    Write-Part -Name "frontend" -WorkDir $frontend -Work {
        npx vitest run @specs *>&1
    }
}

$summary.Add("")
$summary.Add("（baseline 预期为红：那就是「原缺陷仍在」的原始证据）")
$summaryPath = Join-Path $outDir "$Label-summary-$stamp.txt"
$summary | Out-File -FilePath $summaryPath -Encoding utf8
Write-Host ""
$summary | Out-Host
if ($exit -eq 0) {
    Write-Host "全部通过（绿）。日志目录：$outDir" -ForegroundColor Green
}
else {
    Write-Host "存在失败用例（红）——见上面的断言理由与日志目录：$outDir" -ForegroundColor Yellow
}
exit $exit
