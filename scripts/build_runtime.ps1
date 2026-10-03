# 安装包自带的 Python 运行时（P4-B）：取官方 CPython → 裁剪 → 自检 → 落进 resources\。
#
# 为什么需要：安装版的后端是 PyInstaller 冻结产物，冻结态里 sys.executable 是
# qio-backend.exe，**不能**当解释器用（它只认 --tool-worker，-m venv 会被当成未知参数忽略掉，
# 于是又启动一个后端）；而带第三方依赖的工具要能建环境，就不能假设用户机器上有 Python
# （新 VM 上 PATH 里什么都没有）。所以把解释器随包带上。
#
# 产物：frontend\src-tauri\resources\python-runtime\（不入库，构建期生成）
#   保留：python.exe、python3*.dll、vcruntime*、DLLs\、Lib\（含 Lib\venv 与
#         Lib\ensurepip\_bundled —— -m venv 用的离线 pip 就从这里来）
#   裁剪：tcl\、include\、libs\、Lib\test\、Lib\idlelib\、Lib\tkinter\、所有 __pycache__、
#         site-packages 里的 pip / setuptools / _distutils_hack 与 distutils-precedence.pth，
#         以及 DLLs 里的 Tcl/Tk 二进制（tcl86t.dll / tk86t.dll / _tkinter.pyd —— 去掉了
#         tkinter 包它们就是 3.3 MB 的死重量；这也是计划里「约 37 MB」与实测的主要差项）。
#   自检一律用 -B 跑产物解释器：不带 -B 时跑一次就会在 Lib\__pycache__ 里重新生成
#   约 2.9 MB 字节码缓存，把裁剪成果吃掉一半（实测）。
#   ⚠️ 删 _distutils_hack 就**必须**连 distutils-precedence.pth 一起删：那个 .pth 会去
#   import _distutils_hack，单独留着它每次启动都报 ModuleNotFoundError（实测踩到）。
#
# 版本来源：scripts\python-version.txt（唯一来源）。自带运行时的 major.minor 必须与
# 冻结后端一致 —— ToolEnvManager._prepare 会拒绝版本不一致的环境（依赖 ABI 按后端算）。
#
# 来源解释器：默认 uv python install <ver> 取官方 python-build-standalone（可重复、
# 不依赖本机装的 Python）；没有网络时用 -SourceDir 指向一个已存在的解释器目录，例如
#   -SourceDir "$env:APPDATA\uv\python\cpython-3.11.15-windows-x86_64-none"
#
# 自检（任何一步失败 → exit 1；坏运行时绝不当成功产物）：
#   1) 产物 python.exe 跑得起来、报得出自己的版本；
#   2) 产物 python.exe -m venv <临时目录> 成功；
#   3) 临时 venv 里的 python -m pip --version 成功（pip 来自 ensurepip\_bundled，离线可用）；
#   4) venv 的 major.minor == python-version.txt 里的版本 == 产物自身的版本。
# 自检报告：.build-tmp\python-runtime-selftest.json（无 BOM）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_runtime.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_runtime.ps1 -SourceDir <dir>
#
# 兼容 Windows PowerShell 5.1（仓库脚本的既有口径）：不用 pwsh 7 专有语法。

[CmdletBinding()]
param(
  # 期望的 major.minor；默认读 scripts\python-version.txt。
  [string]$Version = "",
  # 已存在的解释器目录（无网络时用）；给了就不走 uv。
  [string]$SourceDir = "",
  # 产物目录；默认 frontend\src-tauri\resources\python-runtime。
  [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$nl = [string][char]10

$root = Split-Path $PSScriptRoot -Parent
$versionFile = Join-Path $PSScriptRoot "python-version.txt"
$workRoot = Join-Path $root ".build-tmp"
$reportPath = Join-Path $workRoot "python-runtime-selftest.json"
$selftestVenv = Join-Path $workRoot "python-runtime-selftest-venv"
$target = if ($OutDir) { $OutDir } else { Join-Path $root "frontend\src-tauri\resources\python-runtime" }

# 无 BOM 写文本：NSIS / Rust 侧对 BOM 敏感，仓库里已有前车之鉴（PowerShell 5.1 的
# Set-Content -Encoding UTF8 会写 BOM，所以一律走 .NET 的 UTF8Encoding($false)）。
function Write-Utf8NoBom {
  param([string]$Path, [string]$Text)
  $parent = Split-Path $Path -Parent
  if ($parent -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# 跑原生命令：返回 @{ Code = 退出码; Output = stdout+stderr 全文 }。
# Windows PowerShell 5.1 会把原生命令写到 stderr 的正常输出当成终止性错误，所以这里
# 临时把 $ErrorActionPreference 放宽、只看退出码（与 scripts\build_sidecar.ps1 同一套写法）。
function Invoke-Native {
  param([string]$Exe, [string[]]$Arguments)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $lines = & $Exe @Arguments 2>&1
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  return [pscustomobject]@{ Code = $code; Output = (($lines | ForEach-Object { "$_" }) -join $nl) }
}

# uv 会给 3.11 建一个 junction 目录（cpython-3.11-... → cpython-3.11.15-...）。
# 复制 junction 本身不是想要的结果，所以先解析成真实目录。
function Resolve-ReparseDir {
  param([string]$Path)
  $item = Get-Item -LiteralPath $Path -Force
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    $resolved = @($item.Target) | Where-Object { $_ } | Select-Object -First 1
    if ($resolved) { return [string]$resolved }
  }
  return $item.FullName
}

# 从一段输出里取最后一行匹配正则的内容（找不到返回空串）。
function Select-LastMatch {
  param([string]$Text, [string]$Pattern)
  $hit = @($Text -split $nl | Where-Object { $_ -match $Pattern } | ForEach-Object { $_.Trim() })
  if ($hit.Count -eq 0) { return "" }
  return [string]$hit[$hit.Count - 1]
}

$checks = @()
function Add-Check {
  param([string]$Name, [bool]$Ok, [string]$Detail)
  $script:checks += [pscustomobject]@{ name = $Name; ok = $Ok; detail = $Detail }
}

$started = Get-Date
$report = [ordered]@{
  ok = $false
  version = $Version
  version_file = $versionFile
  source_kind = ""
  source_dir = ""
  product_dir = $target
  product_bytes = 0
  product_mb = 0
  python_version = ""
  venv_python_version = ""
  pip_version = ""
  checks = @()
  error = ""
  started_at = $started.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  finished_at = ""
  duration_seconds = 0
}

function Save-Report {
  $script:report.finished_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $script:report.duration_seconds = [math]::Round(((Get-Date) - $script:started).TotalSeconds, 1)
  Write-Utf8NoBom -Path $reportPath -Text (($script:report | ConvertTo-Json -Depth 6) + $nl)
}

try {
  # 1) 版本：scripts\python-version.txt 是侧车与自带运行时共用的唯一来源。
  if (-not $Version) {
    if (-not (Test-Path -LiteralPath $versionFile)) {
      throw "找不到版本文件 $versionFile（scripts\python-version.txt 是唯一版本来源）。"
    }
    $Version = ((Get-Content -LiteralPath $versionFile -TotalCount 1) -replace '\s', '')
  }
  if ($Version -notmatch '^\d+\.\d+$') {
    throw "版本号 '$Version' 不是 major.minor 形状（例如 3.11）。"
  }
  $report.version = $Version

  # 2) 来源解释器目录
  if ($SourceDir) {
    if (-not (Test-Path -LiteralPath $SourceDir)) {
      throw "SourceDir 不存在：$SourceDir"
    }
    $src = Resolve-ReparseDir ((Resolve-Path -LiteralPath $SourceDir).Path)
    $report.source_kind = "source-dir"
    Write-Host "来源：-SourceDir $src"
  } else {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
      throw "找不到 uv：装一个 uv，或用 -SourceDir 指向已存在的解释器目录。"
    }
    Write-Host "来源：uv python install $Version"
    $install = Invoke-Native -Exe "uv" -Arguments @("python", "install", $Version)
    if ($install.Code -ne 0) {
      throw "uv python install $Version 失败（退出码 $($install.Code)，离线？）：$($install.Output)"
    }
    $found = Invoke-Native -Exe "uv" -Arguments @(
      "python", "find", "--no-project", "--python-preference", "only-managed", $Version
    )
    if ($found.Code -ne 0) {
      throw "uv python find $Version 失败（退出码 $($found.Code)）：$($found.Output)"
    }
    $exe = Select-LastMatch -Text $found.Output -Pattern '(?i)python\.exe\s*$'
    if (-not $exe) {
      throw "uv python find 没给出 python.exe 路径：$($found.Output)"
    }
    $src = Resolve-ReparseDir (Split-Path -Parent $exe)
    $report.source_kind = "uv"
  }
  $report.source_dir = $src

  # 来源解释器必须能跑、且就是期望的 major.minor（版本不对就当场失败，不静默换一个）。
  $srcPython = Join-Path $src "python.exe"
  if (-not (Test-Path -LiteralPath $srcPython)) {
    throw "来源目录里没有 python.exe：$src"
  }
  # 注意：-c 的代码里只能用单引号。Windows PowerShell 5.1 把原生命令参数里的双引号
  # 吃掉（实测：-c 收到的是 print(%d.%d.%d ...)，SyntaxError），所以这里不能写 "。
  $srcProbe = Invoke-Native -Exe $srcPython -Arguments @("-c", 'import sys; print(''%d.%d.%d'' % sys.version_info[:3])')
  $srcVersion = Select-LastMatch -Text $srcProbe.Output -Pattern '^\d+\.\d+\.\d+$'
  if ($srcProbe.Code -ne 0 -or -not $srcVersion) {
    throw "来源解释器跑不起来：$srcPython（退出码 $($srcProbe.Code)）：$($srcProbe.Output)"
  }
  $srcMajorMinor = (@($srcVersion -split '\.')[0, 1]) -join '.'
  if ($srcMajorMinor -ne $Version) {
    throw "来源解释器是 Python $srcVersion，与期望的 $Version 不一致（版本必须与冻结后端一致）。"
  }
  Add-Check "source_interpreter" $true "Python $srcVersion at $src"

  # 3) 复制 + 裁剪
  if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  Copy-Item -Path (Join-Path $src "*") -Destination $target -Recurse -Force

  $pruned = @()
  foreach ($rel in @("tcl", "include", "libs", "Lib\test", "Lib\idlelib", "Lib\tkinter")) {
    $path = Join-Path $target $rel
    if (Test-Path -LiteralPath $path) {
      Remove-Item -LiteralPath $path -Recurse -Force
      $pruned += $rel
    }
  }
  $caches = @(Get-ChildItem -LiteralPath $target -Recurse -Directory -Force -Filter "__pycache__")
  foreach ($cache in $caches) {
    Remove-Item -LiteralPath $cache.FullName -Recurse -Force
  }
  $pruned += "__pycache__ x$($caches.Count)"
  # Tcl/Tk 二进制：Lib\tkinter 已经没了，DLLs 里这几个也没人再用。
  foreach ($rel in @("DLLs\tcl86t.dll", "DLLs\tk86t.dll", "DLLs\_tkinter.pyd")) {
    $path = Join-Path $target $rel
    if (Test-Path -LiteralPath $path) {
      Remove-Item -LiteralPath $path -Force
      $pruned += $rel
    }
  }
  $site = Join-Path $target "Lib\site-packages"
  if (Test-Path -LiteralPath $site) {
    # _distutils_hack 与 distutils-precedence.pth 必须成对处理（见文件头注释）。
    foreach ($pattern in @("pip", "pip-*", "setuptools", "setuptools-*", "_distutils_hack", "_distutils_hack-*", "distutils-precedence.pth")) {
      Get-ChildItem -LiteralPath $site -Force -Filter $pattern | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
        $pruned += "Lib\site-packages\$($_.Name)"
      }
    }
  }
  Write-Host "裁剪：$($pruned -join '、')"

  # 裁剪过头就是坏产物：-m venv 的离线 pip 与 DLL 扩展模块都得在。
  $missing = @()
  foreach ($rel in @("python.exe", "Lib\venv\__init__.py", "Lib\ensurepip\_bundled", "Lib\encodings\__init__.py", "DLLs")) {
    if (-not (Test-Path -LiteralPath (Join-Path $target $rel))) { $missing += $rel }
  }
  if (-not (Get-ChildItem -LiteralPath $target -Force -Filter "python3*.dll" | Select-Object -First 1)) {
    $missing += "python3*.dll"
  }
  if ($missing.Count -gt 0) {
    throw "裁剪把必需的东西删掉了：$($missing -join '、')"
  }
  Add-Check "pruned_but_complete" $true ("removed: " + ($pruned -join ", "))

  # 4) 自检
  $productPython = Join-Path $target "python.exe"
  # 自检期间不许写字节码缓存：Windows 的 venv 共享**基础安装**的 Lib，而 `python -m venv`
  # 内部还会起一个 `python -Im ensurepip` 子进程 —— 命令行上的 -B 传不到它那里，
  # 只有环境变量会被子进程继承（实测：只用 -B 时产物里又冒出 21 个 __pycache__）。
  # 环境变量是本进程私有的，脚本进程结束就没了。
  $env:PYTHONDONTWRITEBYTECODE = "1"
  $productProbe = Invoke-Native -Exe $productPython -Arguments @("-B", "-c", 'import sys; print(''%d.%d.%d'' % sys.version_info[:3])')
  $productVersion = Select-LastMatch -Text $productProbe.Output -Pattern '^\d+\.\d+\.\d+$'
  if ($productProbe.Code -ne 0 -or -not $productVersion) {
    throw "自检 1/4 失败：产物解释器跑不起来（退出码 $($productProbe.Code)）：$($productProbe.Output)"
  }
  $productMajorMinor = (@($productVersion -split '\.')[0, 1]) -join '.'
  if ($productMajorMinor -ne $Version) {
    throw "自检 1/4 失败：产物是 Python $productVersion，期望 $Version。"
  }
  $report.python_version = $productVersion
  Add-Check "product_interpreter" $true "Python $productVersion at $productPython"
  Write-Host "自检 1/4 通过：产物解释器 Python $productVersion"

  if (Test-Path -LiteralPath $selftestVenv) {
    Remove-Item -LiteralPath $selftestVenv -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $workRoot | Out-Null
  $venvCreation = Invoke-Native -Exe $productPython -Arguments @("-B", "-m", "venv", $selftestVenv)
  $venvPython = Join-Path $selftestVenv "Scripts\python.exe"
  if ($venvCreation.Code -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
    throw "自检 2/4 失败：-m venv 建不出来（退出码 $($venvCreation.Code)）：$($venvCreation.Output)"
  }
  Add-Check "venv_creation" $true "-m venv $selftestVenv"
  Write-Host "自检 2/4 通过：-m venv $selftestVenv"

  # 注意：Windows 的 venv 共享**基础安装**的 Lib —— venv 里的解释器一 import 标准库就会往
  # 产物的 Lib\__pycache__ 写缓存（实测 21 个目录），所以这里也一律加 -B。
  $pipProbe = Invoke-Native -Exe $venvPython -Arguments @("-B", "-m", "pip", "--version")
  $pipLine = Select-LastMatch -Text $pipProbe.Output -Pattern '(?i)^pip\s+\S+'
  $pipVersion = ""
  if ($pipLine -match '(?i)^pip\s+([0-9][^\s]*)') { $pipVersion = $Matches[1] }
  if ($pipProbe.Code -ne 0 -or -not $pipVersion) {
    throw "自检 3/4 失败：venv 里的 pip 不可用（退出码 $($pipProbe.Code)）：$($pipProbe.Output)"
  }
  $report.pip_version = $pipVersion
  Add-Check "venv_pip" $true "pip $pipVersion - $pipLine"
  Write-Host "自检 3/4 通过：venv 内 pip $pipVersion"

  $venvProbe = Invoke-Native -Exe $venvPython -Arguments @("-B", "-c", 'import sys; print(''%d.%d'' % sys.version_info[:2])')
  $venvVersion = Select-LastMatch -Text $venvProbe.Output -Pattern '^\d+\.\d+$'
  if ($venvProbe.Code -ne 0 -or -not $venvVersion) {
    throw "自检 4/4 失败：venv 解释器跑不起来（退出码 $($venvProbe.Code)）：$($venvProbe.Output)"
  }
  if ($venvVersion -ne $Version) {
    throw "自检 4/4 失败：venv 是 Python $venvVersion，期望 $Version。"
  }
  $report.venv_python_version = $venvVersion
  Add-Check "venv_version" $true "Python $venvVersion == $Version"
  Write-Host "自检 4/4 通过：venv Python $venvVersion == 期望 $Version"

  # 5) 兜底：自检（含它的子进程）本该一个字节码缓存都不写；万一还是写了，扫掉并如实记数，
  #    不假装没发生（正常情况下这里是 0）。
  $leftoverCaches = @(Get-ChildItem -LiteralPath $target -Recurse -Directory -Force -Filter "__pycache__")
  foreach ($cache in $leftoverCaches) {
    Remove-Item -LiteralPath $cache.FullName -Recurse -Force
  }
  Add-Check "bytecode_sweep" $true "removed after self-test: $($leftoverCaches.Count) (expected 0)"
  if ($leftoverCaches.Count -gt 0) {
    Write-Host "⚠ 自检之后又出现了 $($leftoverCaches.Count) 个 __pycache__（已扫掉）"
  }

  # 6) 结论 + 报告
  $bytes = (Get-ChildItem -LiteralPath $target -Recurse -File -Force | Measure-Object -Property Length -Sum).Sum
  $report.product_bytes = [int64]$bytes
  $report.product_mb = [math]::Round($bytes / 1MB, 1)
  $report.ok = $true
  $report.checks = $checks
  Save-Report
  # 自检 venv 是再生产的中间产物：证据留在报告里，磁盘上不留。
  Remove-Item -LiteralPath $selftestVenv -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "✓ 自带运行时就绪：$target"
  Write-Host "  Python $productVersion / venv pip $pipVersion / $($report.product_mb) MB"
  Write-Host "  自检报告：$reportPath"
  exit 0
} catch {
  $report.error = "$_"
  $report.checks = $checks
  Save-Report
  Write-Host "✗ 自带运行时构建失败：$_"
  Write-Host "  自检报告：$reportPath"
  exit 1
}
