# 一条命令产出「内置 fp32 模型」的 Windows 安装包。
#
# 顺序是有意的，每一步失败就停：
#   1) fetch_model.py   —— 取模型（校验 sha256）放进 Tauri 资源目录；
#                          没有它 `tauri build` 会直接失败（这是想要的行为：宁可构建失败，
#                          也不要静默出一个没有模型的安装包）
#   2) build_sidecar    —— 重建后端 sidecar；**必须**在打包前跑，否则打进包的是旧后端
#                          （实测踩过：旧后端不认内置的 fp32 模型，静默退回 BM25）
#   3) tauri build      —— 编译壳并产出安装包（含更新用的 .sig，见 createUpdaterArtifacts）
#   4) 更新清单         —— 校验签名产物 → 生成 latest.json → 复制到 dist/ → 更新 SHA256SUMS.txt
#
# 更新签名（第 3、4 步都依赖它）：
#   私钥与口令必须由环境提供，缺失就**构建失败** —— 宁可不出包，也不出"没有签名"的更新包：
#     $env:TAURI_SIGNING_PRIVATE_KEY_PATH   = "…\qio-updater.key"
#     $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = "<你的口令>"
#
# 用法：
#   powershell -File scripts\build_installer.ps1                  # 默认从本机模型目录复制
#   powershell -File scripts\build_installer.ps1 -FromModelScope   # 改为从 ModelScope 下载
#   powershell -File scripts\build_installer.ps1 -ModelSource "D:\models\bge-small-zh-v1.5"
# （装了 PowerShell 7 的话 pwsh 也行；脚本对 Windows PowerShell 5.1 同样兼容。）
param(
  [string]$ModelSource = "C:\Tools\models\bge-small-zh-v1.5",
  [switch]$FromModelScope,
  [string]$Bundles = "nsis",
  # 发布产物目录（安装包与更新清单都放这里）
  [string]$DistDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = "python"

# Windows PowerShell 5.1 会把原生命令写到 stderr 的正常输出当成终止性错误
# （$ErrorActionPreference="Stop" 下尤其明显）。所有外部命令统一走这个包装：
# 临时放宽、只看退出码、失败时抛出我们自己的消息。
function Invoke-External {
  param([string]$What, [scriptblock]$Command)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Command
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($code -ne 0) { throw "$What 失败（exit $code）" }
}

# 无 BOM 的 UTF-8 写入：latest.json 会被 Rust 侧的 serde_json 直接解析，
# 带 BOM 的 JSON 会让更新检查直接失败（PowerShell 5.1 的 Set-Content -Encoding UTF8 带 BOM）。
function Write-Utf8NoBom {
  param([string]$Path, [string]$Text)
  [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "== 1/3 准备内置模型 =="
Push-Location $root
try {
  if ($FromModelScope) {
    Invoke-External "准备内置模型" { & $python "scripts\models\fetch_model.py" --from-modelscope }
  } else {
    Invoke-External "准备内置模型" { & $python "scripts\models\fetch_model.py" --from-dir $ModelSource }
  }
} finally {
  Pop-Location
}

Write-Host "== 2/3 重建后端 sidecar =="
& (Join-Path $PSScriptRoot "build_sidecar.ps1")
if ($LASTEXITCODE -ne 0) { throw "sidecar 构建失败" }

Write-Host "== 3/4 打包安装包（--bundles $Bundles）=="
Push-Location (Join-Path $root "frontend")
try {
  if (-not $env:TAURI_SIGNING_PRIVATE_KEY_PATH -and -not $env:TAURI_SIGNING_PRIVATE_KEY) {
    throw "缺少更新签名私钥：请设置 TAURI_SIGNING_PRIVATE_KEY_PATH（见脚本头部说明）。"
  }
  if (-not $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD) {
    throw "缺少更新签名口令：请设置 TAURI_SIGNING_PRIVATE_KEY_PASSWORD。"
  }
  Invoke-External "tauri build" { npm run tauri build -- --bundles $Bundles }
} finally {
  Pop-Location
}

$out = Join-Path $root "frontend\src-tauri\target\release\bundle\$Bundles"
Write-Host "安装包在：$out"

Write-Host "== 4/4 生成更新清单并落到发布目录 =="
if (-not $DistDir) {
  $DistDir = Join-Path (Split-Path $root -Parent) "dist"
}
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

$exe = Get-ChildItem -Path $out -Filter "QIO_*_x64-setup.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $exe) { throw "在 $out 里找不到安装包" }
$sigPath = "$($exe.FullName).sig"
if (-not (Test-Path $sigPath)) {
  # tauri build 应该已经签好；万一没有（配置变了），这里补签一次，绝不静默跳过
  Write-Host "未发现 .sig，使用 tauri signer sign 补签"
  Push-Location (Join-Path $root "frontend")
  try {
    Invoke-External "签名安装包" {
      npx tauri signer sign -f $env:TAURI_SIGNING_PRIVATE_KEY_PATH -p $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD $exe.FullName
    }
  } finally {
    Pop-Location
  }
  if (-not (Test-Path $sigPath)) { throw "签名失败：$sigPath 不存在" }
}

$signature = (Get-Content -Raw $sigPath).Trim()
$version = (Get-Content -Raw (Join-Path $root "frontend\src-tauri\tauri.conf.json") | ConvertFrom-Json).version
$fileName = $exe.Name
$notesUrl = "https://github.com/HebiIsHere/QIO/releases/latest/download/$fileName"
$manifest = [ordered]@{
  version   = $version
  notes     = "见 GitHub Release 说明"
  pub_date  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  platforms = [ordered]@{
    "windows-x86_64" = [ordered]@{ signature = $signature; url = $notesUrl }
  }
}
Copy-Item -Force $exe.FullName (Join-Path $DistDir $fileName)
Copy-Item -Force $sigPath (Join-Path $DistDir "$fileName.sig")
$manifestPath = Join-Path $DistDir "latest.json"
Write-Utf8NoBom -Path $manifestPath -Text ($manifest | ConvertTo-Json -Depth 4)

# SHA256SUMS.txt：重新汇总发布目录里所有安装包（不删除旧版本记录）
$sums = Get-ChildItem -Path $DistDir -Filter "QIO_*_x64-setup.exe" | Sort-Object Name | ForEach-Object {
  $hash = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash
  "$($_.Name)  $hash"
}
Write-Utf8NoBom -Path (Join-Path $DistDir "SHA256SUMS.txt") -Text ($sums -join "`r`n")

Write-Host "完成。"
Write-Host "  安装包：$(Join-Path $DistDir $fileName)"
Write-Host "  签名：  $(Join-Path $DistDir "$fileName.sig")"
Write-Host "  清单：  $manifestPath"
Write-Host "下一步：powershell -File scripts\publish_release.ps1 -Version $version"
