# 打包入口（不含任何密钥）：给「更新签名」准备环境变量，再调用 build_installer.ps1。
#
# 为什么要它：直接手写 $env:TAURI_SIGNING_PRIVATE_KEY_PATH 这类变量名时，
# 从聊天/文档复制容易被插入反斜杠（TAURI\_SIGNING\_...），PowerShell 直接语法报错。
# 这里把变量名固定在脚本里，调用者只需要提供口令即可。
#
# 用法（口令推荐交互输入，不落进命令历史）：
#   & "D:\qio-dev\qio-wt-fixes\scripts\run_build.ps1"
#   或一次性传入： -Password "你的口令"
param(
  [string]$Password = "",
  [string]$KeyPath = ""
)

$ErrorActionPreference = "Stop"

# 私钥默认位置：脚本在 <检出>\scripts\ 下，发布产物在 <检出的上一级>\dist\。
# 按脚本位置推导，检出换盘/换目录都不用改这里。
if (-not $KeyPath) {
  $KeyPath = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "dist\qio-updater.key"
}

if (-not (Test-Path $KeyPath)) {
  throw "找不到更新私钥：$KeyPath"
}
if (-not $Password) {
  $Password = Read-Host "更新私钥口令"
}
if (-not $Password) {
  throw "口令不能为空：没有它签不出更新包（脚本会拒绝产出未签名的包）"
}

$env:TAURI_SIGNING_PRIVATE_KEY_PATH = (Resolve-Path $KeyPath).Path
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = $Password

Write-Host "私钥：$env:TAURI_SIGNING_PRIVATE_KEY_PATH"
Write-Host "口令：已设置（$($Password.Length) 个字符）"

& (Join-Path $PSScriptRoot "build_installer.ps1")
