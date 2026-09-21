# 打包 Python 后端为 Tauri sidecar 可执行文件。
#
# ⚠️ 这个步骤**必须在 `tauri build` 之前跑**：Tauri 只负责把已有的 sidecar 打进安装包，
# 不会帮你重建它。漏跑一次就会把旧构建的后端打进包 —— 实测踩过：安装包里的后端比源码
# 早一个多月，于是内置模型加载失败、静默退回 BM25（安装包看起来是好的）。
# 想省事就直接用 `scripts/build_installer.ps1`，它按正确顺序把三件事跑完。
#
# 解释器：优先 $env:QIO_PYTHON，其次 backend\.venv；没有 PyInstaller 时用
# `uv run --frozen --with pyinstaller` 临时提供（不改动任何环境）。

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $root "backend"
$binaries = Join-Path $root "frontend\src-tauri\binaries"

$pyiArgs = @(
  "--noconfirm", "--onefile", "--name", "qio-backend",
  "--collect-all", "tiktoken",
  "--hidden-import", "keyring.backends.Windows",
  "--hidden-import", "uvicorn.logging",
  "--hidden-import", "uvicorn.loops.auto",
  "--hidden-import", "uvicorn.protocols.http.auto",
  "--hidden-import", "uvicorn.protocols.websockets.auto",
  "--hidden-import", "uvicorn.lifespan.on",
  "--distpath", "dist-sidecar",
  "--workpath", "build-sidecar",
  "src/agent/main.py"
)

Push-Location $backend
try {
  $candidate = $env:QIO_PYTHON
  if (-not $candidate) {
    $venv = Join-Path $backend ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $candidate = $venv }
  }

  $done = $false
  if ($candidate) {
    & $candidate -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -eq 0) {
      & $candidate -m PyInstaller @pyiArgs 2>&1 | Select-Object -Last 3
      $done = ($LASTEXITCODE -eq 0)
    } else {
      Write-Host "PyInstaller 不在 $candidate 里，改用 uv 临时提供"
    }
  }

  if (-not $done) {
    uv run --frozen --with pyinstaller python -m PyInstaller @pyiArgs 2>&1 | Select-Object -Last 3
    if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller failed: $LASTEXITCODE"; exit 1 }
  }
} finally {
  Pop-Location
}

New-Item -ItemType Directory -Force -Path $binaries | Out-Null
$target = Join-Path $binaries "qio-backend-x86_64-pc-windows-msvc.exe"
Copy-Item (Join-Path $backend "dist-sidecar\qio-backend.exe") $target -Force
Write-Host "sidecar written: $target ($([math]::Round((Get-Item $target).Length / 1MB, 1)) MB)"
