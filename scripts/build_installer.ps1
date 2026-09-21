# 一条命令产出「内置 fp32 模型」的 Windows 安装包。
#
# 顺序是有意的，每一步失败就停：
#   1) fetch_model.py   —— 取模型（校验 sha256）放进 Tauri 资源目录；
#                          没有它 `tauri build` 会直接失败（这是想要的行为：宁可构建失败，
#                          也不要静默出一个没有模型的安装包）
#   2) build_sidecar    —— 重建后端 sidecar；**必须**在打包前跑，否则打进包的是旧后端
#                          （实测踩过：旧后端不认内置的 fp32 模型，静默退回 BM25）
#   3) tauri build      —— 编译壳并产出安装包
#
# 用法：
#   pwsh -File scripts/build_installer.ps1                       # 默认从本机模型目录复制
#   pwsh -File scripts/build_installer.ps1 -FromModelScope        # 改为从 ModelScope 下载
#   pwsh -File scripts/build_installer.ps1 -ModelSource "D:\models\bge-small-zh-v1.5"
param(
  [string]$ModelSource = "C:\Tools\models\bge-small-zh-v1.5",
  [switch]$FromModelScope,
  [string]$Bundles = "nsis"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = "python"

Write-Host "== 1/3 准备内置模型 =="
Push-Location $root
try {
  if ($FromModelScope) {
    & $python "scripts\models\fetch_model.py" --from-modelscope
  } else {
    & $python "scripts\models\fetch_model.py" --from-dir $ModelSource
  }
  if ($LASTEXITCODE -ne 0) { throw "模型准备失败" }
} finally {
  Pop-Location
}

Write-Host "== 2/3 重建后端 sidecar =="
& (Join-Path $PSScriptRoot "build_sidecar.ps1")
if ($LASTEXITCODE -ne 0) { throw "sidecar 构建失败" }

Write-Host "== 3/3 打包安装包（--bundles $Bundles）=="
Push-Location (Join-Path $root "frontend")
try {
  npm run tauri build -- --bundles $Bundles
  if ($LASTEXITCODE -ne 0) { throw "tauri build 失败" }
} finally {
  Pop-Location
}

$out = Join-Path $root "frontend\src-tauri\target\release\bundle\$Bundles"
Write-Host "完成。安装包在：$out"
