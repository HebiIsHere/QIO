# 把一版更新发布到 GitHub Releases（应用内更新的更新源就指向这里）。
#
# 前置：
#   * 已经跑过 scripts/build_installer.ps1（dist/ 里有 exe、exe.sig、latest.json）；
#   * gh 已登录（gh auth status）；
#   * 版本号已经改过（package.json / tauri.conf.json / Cargo.toml / pyproject.toml）。
#
# 上传的三个资产缺一不可：
#   QIO_X.Y.Z_x64-setup.exe      安装包本体
#   QIO_X.Y.Z_x64-setup.exe.sig  更新签名（客户端用它校验，缺了更新会被拒绝）
#   latest.json                  更新清单（endpoints 指向 latest/download/latest.json）
#
# 用法：
#   powershell -File scripts\publish_release.ps1 -Version 0.1.3
param(
  [Parameter(Mandatory = $true)][string]$Version,
  [string]$DistDir = "",
  [string]$NotesFile = "",
  # 安装包下载地址前缀：默认走公共加速（国内直连 GitHub 常只有几十 KB/s）。
  # 传 -AssetBaseUrl "" 就退回 GitHub 原始地址。
  [string]$AssetBaseUrl = "https://gh.llkk.cc/https://github.com/HebiIsHere/QIO"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

# latest.json 会被 Rust 侧 serde_json 直接解析：必须无 BOM
# （PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM）。
function Write-Utf8NoBom {
  param([string]$Path, [string]$Text)
  [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

if (-not $DistDir) { $DistDir = Join-Path (Split-Path $root -Parent) "dist" }
if (-not $NotesFile) { $NotesFile = Join-Path $root "docs\releases\v$Version.md" }

$tag = "v$Version"
$exe = Join-Path $DistDir "QIO_${Version}_x64-setup.exe"
$sig = "$exe.sig"
$manifest = Join-Path $DistDir "latest.json"

foreach ($file in @($exe, $sig, $manifest)) {
  if (-not (Test-Path $file)) { throw "缺少发布资产：$file（先跑 scripts/build_installer.ps1）" }
}
if (-not (Test-Path $NotesFile)) { throw "缺少发布说明：$NotesFile" }

# 清单里的 url 必须指向本次 tag，否则客户端会去 latest 之外的地方下载。
# 默认把「公共加速」写在前面（原始 GitHub 地址留作 -AssetBaseUrl "" 的兜底）。
$json = Get-Content -Raw $manifest | ConvertFrom-Json
$assetDir = if ($AssetBaseUrl) {
  "$AssetBaseUrl/releases/download/$tag"
} else {
  "https://github.com/HebiIsHere/QIO/releases/download/$tag"
}
$json.platforms.'windows-x86_64'.url = "$assetDir/QIO_${Version}_x64-setup.exe"
Write-Utf8NoBom -Path $manifest -Text ($json | ConvertTo-Json -Depth 4)

Write-Host "== 创建 release $tag 并上传资产 =="
gh release create $tag `
  --title "QIO $tag" `
  --notes-file $NotesFile `
  $exe $sig $manifest
if ($LASTEXITCODE -ne 0) { throw "gh release create 失败" }

Write-Host "完成。应用内更新源："
Write-Host "  https://github.com/HebiIsHere/QIO/releases/latest/download/latest.json"
Write-Host "latest.json："
Get-Content -Raw $manifest | Write-Host
