# 把外层工作区文件夹改名（默认 `Front agent` → `qio`）。
#
# 为什么要单独写成脚本：直接在会话里改会失败 —— 只要有**任何进程**把这个目录当作
# 当前工作目录或持有它的句柄（编辑器/终端/本应用打开着这个工作区都算），
# Windows 就会拒绝改名（`The process cannot access the file because it is being used
# by another process`）。脚本会把失败原因原样打出来，不做「看起来成功了」的处理。
#
# 用法（在一个**不在**该目录内的终端里跑）：
#   pwsh -File scripts/rename_workspace_dir.ps1
#   pwsh -File scripts/rename_workspace_dir.ps1 -NewName qio -Parent "C:\Users\zxy\Documents"

param(
  # 默认：本脚本位于 <父目录>\<仓库目录>\scripts\ 下，所以父目录往上三层
  [string]$Parent = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path,
  [string]$CurrentName = "Front agent",
  [string]$NewName = "qio"
)

$ErrorActionPreference = "Stop"
$source = Join-Path $Parent $CurrentName
$target = Join-Path $Parent $NewName
# 独立进程跑时看不到控制台输出：结果同时写一份到临时日志
$log = Join-Path $env:TEMP "qio-rename-workspace.log"
function Say($text) { Write-Host $text; Add-Content -Path $log -Value $text -Encoding UTF8 }
Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $source)) {
  if (Test-Path -LiteralPath $target) {
    Say "已经是目标名字了：$target"
    exit 0
  }
  Say "找不到源目录：$source"
  exit 1
}
if (Test-Path -LiteralPath $target) {
  Say "目标已存在，拒绝覆盖：$target"
  exit 1
}

try {
  Rename-Item -LiteralPath $source -NewName $NewName -ErrorAction Stop
  Say "已改名：$source → $target"
  exit 0
} catch {
  Say "改名失败：$($_.Exception.Message)"
  Say "原因通常是这个目录正被占用：关掉打开它的编辑器 / 终端 / 本应用（Codex 打开着该工作区也算），再重跑一次。"
  exit 1
}

