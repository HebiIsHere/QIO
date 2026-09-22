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
#   pwsh -File scripts/publish_release.ps1 -Version 0.1.3
param(
  [Parameter(Mandatory = $true)][string]$Version,
  [string]$DistDir = "",
  [string]$NotesFile = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
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

# 清单里的 url 必须指向本次 tag，否则客户端会去 latest 之外的地方下载
$json = Get-Content -Raw $manifest | ConvertFrom-Json
$json.platforms.'windows-x86_64'.url = "https://github.com/HebiIsHere/QIO/releases/download/$tag/QIO_${Version}_x64-setup.exe"
$json | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $manifest

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
