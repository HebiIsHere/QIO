# 打包 Python 后端为 Tauri sidecar 可执行文件。
#
# ⚠️ 这个步骤**必须在 `tauri build` 之前跑**：Tauri 只负责把已有的 sidecar 打进安装包，
# 不会帮你重建它。漏跑一次就会把旧构建的后端打进包 —— 实测踩过：安装包里的后端比源码
# 早一个多月，于是内置模型加载失败、静默退回 BM25（安装包看起来是好的）。
# 想省事就直接用 `scripts/build_installer.ps1`，它按正确顺序把三件事跑完。
#
# 解释器：优先 $env:QIO_PYTHON，其次 backend\.venv；没有 PyInstaller 时用
# `uv run --frozen --with pyinstaller` 临时提供（不改动任何环境）。
#
# ⚠️ 版本必须与安装包自带的 Python 运行时**同一个 major.minor**（P4-B）：
#   * 后端自己按 sys.version_info[:2] 记环境身份（tools/tool_envs.py），
#   * ToolEnvManager._prepare 会拒绝与后端 major.minor 不一致的环境；
#   * 自带运行时由 scripts\build_runtime.ps1 按 scripts\python-version.txt 生成。
# 所以这里**先核对**再打包：选中的解释器不是 python-version.txt 里的版本就明确失败
# （而不是悄悄产出一个"装到自己带的运行时上会明确失败"的后端）。仓库根还有
# .python-version（uv 的口径），两者必须一致。

$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $root "backend"
$binaries = Join-Path $root "frontend\src-tauri\binaries"
$versionFile = Join-Path $PSScriptRoot "python-version.txt"

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
  # 工具 worker：main.py 在函数里 import 它（为了在加载服务前分流），
  # 这里显式声明，避免将来改成动态导入时被 PyInstaller 漏掉。
  "--hidden-import", "agent.tool_worker",
  # 容器隔离执行要把 worker 源码文本带进容器（tools/executor_env.py::worker_source）。
  # 冻结产物里没有 .py 文件，所以随包放一份数据文件到 _MEIPASS/agent/tool_worker.py。
  "--add-data", "src/agent/tool_worker.py;agent",
  "--hidden-import", "uvicorn.logging",
  "--hidden-import", "uvicorn.loops.auto",
  "--hidden-import", "uvicorn.protocols.http.auto",
  "--hidden-import", "uvicorn.protocols.websockets.auto",
  "--hidden-import", "uvicorn.lifespan.on",
  "--distpath", "dist-sidecar",
  "--workpath", "build-sidecar",
  "src/agent/main.py"
)

# 期望的 major.minor（scripts\python-version.txt 是侧车与自带运行时共用的唯一来源）
$wanted = ""
if (Test-Path $versionFile) { $wanted = ((Get-Content -Raw $versionFile) + "").Trim() }
if (-not $wanted) {
  Write-Host "读不到 $versionFile：侧车与自带运行时的版本必须同一来源，拒绝继续。"
  exit 1
}

# 问一个解释器它自己的 major.minor；不是 Python 就返回空串。
function Get-PythonMajorMinor {
  param([string]$Exe)
  if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return "" }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    # 单引号包住 Python 代码：PowerShell 5.1 的 -c 参数会把双引号吃掉（实测）
    $out = & $Exe -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>&1
    $mm = ("$out" -split "?
" | Where-Object { $_ -match '^d+.d+$' } | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0) { return "" }
    return ("$mm").Trim()
  } catch {
    return ""
  } finally {
    $ErrorActionPreference = $prev
  }
}

Push-Location $backend
try {
  $candidate = $env:QIO_PYTHON
  if (-not $candidate) {
    $venv = Join-Path $backend ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $candidate = $venv }
  }

  if ($candidate) {
    $actual = Get-PythonMajorMinor $candidate
    if ($actual -and $actual -ne $wanted) {
      Write-Host "选中的解释器是 Python $actual，但 scripts\python-version.txt 要求 $wanted。"
      Write-Host "侧车与安装包自带的运行时必须同一 major.minor（后端按 sys.version_info[:2] 记环境身份）。"
      Write-Host "请先执行：uv sync --frozen --extra dev --python $wanted（重建 backend\.venv），或用 QIO_PYTHON 指向 $wanted 的解释器。"
      exit 1
    }
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
    # --extra dev 不是给打包用的，是**保护开发环境**用的（2026-10-02 实测踩到）：
    # 不带 extras 的 uv run --frozen 会把环境同步成"默认依赖集"，于是把 dev extra 里的
    # pytest / pytest-asyncio 从 .venv 里卸掉 —— 之后跑 uv run --frozen pytest 会以
    # 150 个 collection error（ModuleNotFoundError）收场，看起来像代码坏了，
    # 其实是构建脚本动了共享 venv。PyInstaller 只跟着 main.py 的 import 走，
    # 不会把 pytest 打进包里。
    $code = Invoke-External { uv run --frozen --extra dev --python $wanted --with pyinstaller python -m PyInstaller @pyiArgs }
    if ($code -ne 0) { Write-Host "PyInstaller failed: $code"; exit 1 }
  }
} finally {
  Pop-Location
}

New-Item -ItemType Directory -Force -Path $binaries | Out-Null
$target = Join-Path $binaries "qio-backend-x86_64-pc-windows-msvc.exe"
Copy-Item (Join-Path $backend "dist-sidecar\qio-backend.exe") $target -Force
Write-Host "sidecar written: $target ($([math]::Round((Get-Item $target).Length / 1MB, 1)) MB)"
