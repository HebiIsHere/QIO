# 编译卸载帮助程序并放进 Tauri 资源目录（随安装包落到安装目录里）。
#
# 为什么需要独立的 exe（不能复用 qio.exe）：
#   * qio.exe 正在运行时**删不掉也换不掉**（映像以 FILE_SHARE_DELETE 打开，delete/overwrite 被拒），
#     而卸载场景恰恰就是"qio.exe 可能正在跑"；
#   * 卸载器（NSIS）需要一个能取退出码与输出的普通控制台程序。
#
# 它和主程序共用 frontend/src-tauri/src/ownership.rs 里的同一份实现 ——
# 所有权判定只有一处，不许出现第二份。
#
# 用法：powershell -File scripts\build_uninstall_helper.ps1
# 产物：frontend/src-tauri/resources/qio-uninstall-helper.exe（resources/ 已在 .gitignore）

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$srcTauri = Join-Path $root "frontend\src-tauri"
$resources = Join-Path $srcTauri "resources"
$built = Join-Path $srcTauri "target\release\qio-uninstall-helper.exe"

function Invoke-External {
  param([scriptblock]$Command)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Command 2>&1 | Select-Object -Last 5 | Out-Host
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  return $code
}

Write-Host "== 编译 qio-uninstall-helper =="
Push-Location $srcTauri
try {
  $code = Invoke-External { cargo build --release --bin qio-uninstall-helper }
  if ($code -ne 0) { Write-Host "cargo build 失败：$code"; exit 1 }
} finally {
  Pop-Location
}

if (-not (Test-Path $built)) { Write-Host "找不到构建产物：$built"; exit 1 }
New-Item -ItemType Directory -Force -Path $resources | Out-Null
$target = Join-Path $resources "qio-uninstall-helper.exe"
Copy-Item $built $target -Force
Write-Host "uninstall helper written: $target ($([math]::Round((Get-Item $target).Length / 1MB, 2)) MB)"
