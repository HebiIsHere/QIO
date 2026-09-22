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

# Windows PowerShell 5.1 会把原生命令写到 stderr 的正常输出（uv 的 "Uninstalled 1 package"、
# python 的 traceback 之类）当成终止性错误，即使重定向了也一样。所有外部命令统一走这个包装：
# 临时放宽错误偏好、只取退出码。返回 0/非 0，由调用处决定。
function Invoke-External {
  param([scriptblock]$Command)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    # Out-Host：日志照常打印，但不进入函数的返回值（否则 $code 会变成"输出行 + 退出码"的数组）
    & $Command 2>&1 | Select-Object -Last 3 | Out-Host
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  return $code
}

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
    # Windows PowerShell 5.1 的坑：原生命令一旦往 stderr 写东西，在
    # $ErrorActionPreference="Stop" 下会被当成终止性错误（即使 2>$null 也一样），
    # 于是"探测不到 PyInstaller"这种正常分支会把整个脚本干掉。
    # 探测期间临时放宽，只看退出码。
    $probePrev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $null = & $candidate -c "import PyInstaller" 2>&1
      $hasPyInstaller = ($LASTEXITCODE -eq 0)
    } finally {
      $ErrorActionPreference = $probePrev
    }
    if ($hasPyInstaller) {
      $code = Invoke-External { & $candidate -m PyInstaller @pyiArgs }
      $done = ($code -eq 0)
    } else {
      Write-Host "PyInstaller 不在 $candidate 里，改用 uv 临时提供"
    }
  }

  if (-not $done) {
    $code = Invoke-External { uv run --frozen --with pyinstaller python -m PyInstaller @pyiArgs }
    if ($code -ne 0) { Write-Host "PyInstaller failed: $code"; exit 1 }
  }
} finally {
  Pop-Location
}

New-Item -ItemType Directory -Force -Path $binaries | Out-Null
$target = Join-Path $binaries "qio-backend-x86_64-pc-windows-msvc.exe"
Copy-Item (Join-Path $backend "dist-sidecar\qio-backend.exe") $target -Force
Write-Host "sidecar written: $target ($([math]::Round((Get-Item $target).Length / 1MB, 1)) MB)"
