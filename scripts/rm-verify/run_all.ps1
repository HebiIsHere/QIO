# F 组一键回归脚本（独立验证组）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\rm-verify\run_all.ps1
#   powershell ... -File scripts\rm-verify\run_all.ps1 -Label after        # 集成后重跑
#   powershell ... -File scripts\rm-verify\run_all.ps1 -BackendOnly
#   （装了 PowerShell 7 的环境把 powershell 换成 pwsh 即可）
#
# 所有原始输出同时写入 scripts/rm-verify/output/<label>-<part>-<时间戳>.txt（UTF-8），
# 供「基线 vs 集成后」前后对照。退出码：0 = 全绿，1 = 有用例失败。

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
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$exit = 0

if (-not $FrontendOnly) {
    $files = Get-ChildItem (Join-Path $backend "tests") -Filter "test_rm_verify_*.py" |
        Sort-Object Name | ForEach-Object { "tests/" + $_.Name }
    if ($files.Count -eq 0) {
        Write-Host "!! 没有找到 backend/tests/test_rm_verify_*.py"
        $exit = 1
    }
    else {
        $log = Join-Path $outDir "$Label-backend-$stamp.txt"
        Write-Host "== 后端验证用例 ==" -ForegroundColor Cyan
        Push-Location $backend
        try {
            $captured = & uv run --frozen pytest @files -q --no-header -p no:cacheprovider *>&1
            $code = $LASTEXITCODE
        }
        finally { Pop-Location }
        $captured | Out-File -FilePath $log -Encoding utf8
        Add-Content -Path $log -Value ""
        Add-Content -Path $log -Value "[rm-verify] pytest exit code = $code"
        $captured | Out-Host
        if ($code -ne 0) { $exit = 1 }
        Write-Host "   原始输出：$log"
    }
}

if (-not $BackendOnly) {
    $log = Join-Path $outDir "$Label-frontend-$stamp.txt"
    Write-Host "== 前端验证用例（M12）==" -ForegroundColor Cyan
    Push-Location $frontend
    try {
        $captured = & npx vitest run --config ../scripts/rm-verify/vitest.rmf.config.mjs *>&1
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }
    $captured | Out-File -FilePath $log -Encoding utf8
    Add-Content -Path $log -Value ""
    Add-Content -Path $log -Value "[rm-verify] vitest exit code = $code"
    $captured | Out-Host
    if ($code -ne 0) { $exit = 1 }
    Write-Host "   原始输出：$log"
}

Write-Host ""
if ($exit -eq 0) {
    Write-Host "全部通过（绿）。日志目录：$outDir" -ForegroundColor Green
}
else {
    Write-Host "存在失败用例（红）——见上面的断言理由与日志目录：$outDir" -ForegroundColor Yellow
}
exit $exit
