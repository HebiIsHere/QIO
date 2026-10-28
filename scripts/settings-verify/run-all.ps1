# 设置页取证总入口（PowerShell 包装）：起环境 → 视觉核查 → 交互取证 → 录屏 → 汇总 → 停环境。
#
# 用法（仓库根目录）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/settings-verify/run-all.ps1 -ReportLabel baseline
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/settings-verify/run-all.ps1 -Quick -SkipVideo
#
# 更细的参数（--url / --no-up / --keep-up / --skip-*）直接传给 run-all.mjs：
#   node scripts/settings-verify/run-all.mjs --help 只列参数，不执行。
param(
  [string]$ReportLabel = "",
  [string]$Url = "http://127.0.0.1:5106",
  [string]$ServeLabel = "d",
  [switch]$Quick,
  [switch]$AllowFail,
  [switch]$SkipVideo,
  [switch]$SkipInteraction,
  [switch]$SkipVisual,
  [switch]$NoUp,
  [switch]$KeepUp
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $ReportLabel) { $ReportLabel = "run-" + (Get-Date -Format "yyyyMMdd-HHmmss") }
$argsList = @("scripts/settings-verify/run-all.mjs", "--url", $Url, "--label", $ServeLabel, "--report-label", $ReportLabel)
if ($Quick) { $argsList += "--quick" }
if ($AllowFail) { $argsList += "--allow-fail" }
if ($SkipVideo) { $argsList += "--skip-video" }
if ($SkipInteraction) { $argsList += "--skip-interaction" }
if ($SkipVisual) { $argsList += "--skip-visual" }
if ($NoUp) { $argsList += "--no-up" }
if ($KeepUp) { $argsList += "--keep-up" }
Push-Location $repo
try {
  & node @argsList
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
