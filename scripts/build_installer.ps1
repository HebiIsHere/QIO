# 一条命令产出「内置 fp32 模型」的 Windows 安装包。
#
# 顺序是有意的，每一步失败就停：
#   1) fetch_model.py        —— 取模型（校验 sha256）放进 Tauri 资源目录；
#                               没有它 `tauri build` 会直接失败（这是想要的行为：宁可构建失败，
#                               也不要静默出一个没有模型的安装包）
#   2) build_runtime         —— 生成安装包自带的 Python 运行时（resources/python-runtime）；
#                               冻结后端不能拿 sys.executable 当解释器，依赖型工具靠它建环境。
#                               缺失即构建失败：否则会出一个「依赖工具用不了」的包
#   3) build_uninstall_helper —— 编译卸载帮助程序（所有权判定，只按 pid 收 sidecar）放进资源目录
#   4) build_sidecar         —— 重建后端 sidecar；**必须**在打包前跑，否则打进包的是旧后端
#                               （实测踩过：旧后端不认内置的 fp32 模型，静默退回 BM25）
#   5) tauri build           —— 编译壳并产出安装包（含更新用的 .sig，见 createUpdaterArtifacts）
#   6) 更新清单              —— 校验签名产物 → 生成 latest.json → 复制到 dist/ → 更新 SHA256SUMS.txt
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
#   powershell -File scripts\build_installer.ps1 -UnsignedTestArtifact   # 没有签名口令时：显式产出未签名测试产物
# （装了 PowerShell 7 的话 pwsh 也行；脚本对 Windows PowerShell 5.1 同样兼容。）
#
# 关于 -UnsignedTestArtifact（**只在明确拿不到更新签名口令时用**）：
#   它把 createUpdaterArtifacts 关掉（用 --config 覆盖，**不改**仓库里的 tauri.conf.json），
#   产物名字强制带 -UNSIGNED-TEST 标记，并且**不会**写 latest.json、不会补签 .sig ——
#   目的是让「未签名的测试包」在发布目录里一眼可辨，绝不冒充可发布的更新产物。
#   真发布必须回到带签名的路径（需要 TAURI_SIGNING_PRIVATE_KEY_PATH + 口令，
#   且私钥必须与 tauri.conf.json 内嵌公钥成对）。
param(
  [string]$ModelSource = "C:\Tools\models\bge-small-zh-v1.5",
  [switch]$FromModelScope,
  [string]$Bundles = "nsis",
  # 发布产物目录（安装包与更新清单都放这里）
  [string]$DistDir = "",
  # 显式产出「未签名测试产物」：见文件头说明。签名口令缺失时唯一诚实的出路。
  [switch]$UnsignedTestArtifact,
  # 构建用临时目录。本机 %TEMP% 带拒绝访问的 ACL；且 D: 盘上的临时目录会让 esbuild
  # 删不掉自己的临时文件（[vite:esbuild-transpile] remove ...: Access is denied），
  # 所以默认放在检出内的 .build-tmp（C: 盘）。
  [string]$TempDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = "python"

# 先钉住可写的 TEMP：%TEMP% 在这台机器上不可用，esbuild / PyInstaller / cargo 都会因此
# 报出与代码无关的假故障（Access is denied / Could not create temporary directory / MSVC D8050）。
if (-not $TempDir) { $TempDir = Join-Path $root ".build-tmp" }
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null
$env:TEMP = $TempDir
$env:TMP = $TempDir
Write-Host "TEMP = $TempDir"

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

Write-Host "== 2/6 生成安装包自带的 Python 运行时 =="
& (Join-Path $PSScriptRoot "build_runtime.ps1")
if ($LASTEXITCODE -ne 0) { throw "自带 Python 运行时构建失败" }

Write-Host "== 3/6 编译卸载帮助程序 =="
& (Join-Path $PSScriptRoot "build_uninstall_helper.ps1")
if ($LASTEXITCODE -ne 0) { throw "卸载帮助程序构建失败" }

Write-Host "== 4/6 重建后端 sidecar =="
& (Join-Path $PSScriptRoot "build_sidecar.ps1")
if ($LASTEXITCODE -ne 0) { throw "sidecar 构建失败" }

Write-Host "== 5/6 打包安装包（--bundles $Bundles）=="
Push-Location (Join-Path $root "frontend")
try {
  if ($UnsignedTestArtifact) {
    Write-Warning "未签名测试产物模式：createUpdaterArtifacts 已关，本次产物没有更新签名，不能发布。"
    $override = Join-Path $TempDir "tauri-unsigned-test.json"
    [System.IO.File]::WriteAllText($override, '{"bundle":{"createUpdaterArtifacts":false}}', (New-Object System.Text.UTF8Encoding($false)))
    # NSIS 模板补丁必须夹在「Tauri 生成 installer.nsi」与「makensis 编译」之间：
    # 模板卸载段里有一处按**可执行文件名**的主程序检查（杀当前用户所有 qio.exe → 壳死 →
    # job 关闭 → 另一份安装的 backend 也死），Tauri 的 installerHooks 只能追加宏、不能替换它。
    # scripts/build_nsis_with_patch.py 负责盯着生成文件、在编译前把补丁打进去，
    # 并在构建结束后核对"补丁真的进了 installer.nsi"；没打上就**构建失败**（绝不出无补丁的包）。
    Invoke-External "tauri build（未签名测试产物）" {
      python (Join-Path $root "scripts\build_nsis_with_patch.py") -- npm run tauri build -- --bundles $Bundles --config $override
    }
  } else {
    if (-not $env:TAURI_SIGNING_PRIVATE_KEY_PATH -and -not $env:TAURI_SIGNING_PRIVATE_KEY) {
      throw "缺少更新签名私钥：请设置 TAURI_SIGNING_PRIVATE_KEY_PATH（见脚本头部说明）。"
    }
    if (-not $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD) {
      throw "缺少更新签名口令：请设置 TAURI_SIGNING_PRIVATE_KEY_PASSWORD。"
    }
    # 关键细节（实测踩过）：`tauri build` 的打包器只读 TAURI_SIGNING_PRIVATE_KEY（私钥**内容**），
    # 只有 `tauri signer sign` 子命令才认 TAURI_SIGNING_PRIVATE_KEY_PATH。
    # 这里把路径读成内容，避免出现"公钥有了、私钥没找到"从而只出安装包、不出更新签名的情况。
    if (-not $env:TAURI_SIGNING_PRIVATE_KEY) {
      if (-not (Test-Path $env:TAURI_SIGNING_PRIVATE_KEY_PATH)) {
        throw "找不到私钥文件：$env:TAURI_SIGNING_PRIVATE_KEY_PATH"
      }
      $env:TAURI_SIGNING_PRIVATE_KEY = (Get-Content -Raw $env:TAURI_SIGNING_PRIVATE_KEY_PATH).Trim()
    }
    Invoke-External "tauri build" {
      python (Join-Path $root "scripts\build_nsis_with_patch.py") -- npm run tauri build -- --bundles $Bundles
    }
  }
} finally {
  Pop-Location
}

$out = Join-Path $root "frontend\src-tauri\target\release\bundle\$Bundles"
Write-Host "安装包在：$out"

Write-Host "== 6/6 生成更新清单并落到发布目录 =="
if (-not $DistDir) {
  $DistDir = Join-Path (Split-Path $root -Parent) "dist"
}
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

$exe = Get-ChildItem -Path $out -Filter "QIO_*_x64-setup.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $exe) { throw "在 $out 里找不到安装包" }
$version = (Get-Content -Raw (Join-Path $root "frontend\src-tauri\tauri.conf.json") | ConvertFrom-Json).version
$manifestPath = ""

if ($UnsignedTestArtifact) {
  # 未签名测试产物：文件名强制带标记，且**不**写 .sig / latest.json ——
  # 否则发布目录会看起来像有更新签名（那正是要避免的「假装验证」）。
  $fileName = "QIO_${version}_x64-setup-UNSIGNED-TEST.exe"
  Copy-Item -Force $exe.FullName (Join-Path $DistDir $fileName)
  Remove-Item -Force (Join-Path $DistDir "$fileName.sig") -ErrorAction SilentlyContinue
  Write-Warning "产物是**未签名测试产物**：$(Join-Path $DistDir $fileName)"
  Write-Warning "没有生成 .sig，也没有更新 latest.json：发布闸门的 installer.sig / latest.json.signature 会（也应该）报红。"
  Write-Warning "要出可发布的更新包，必须提供与 tauri.conf.json 内嵌公钥成对的私钥 + 它的口令。"
} else {
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
}

# SHA256SUMS.txt：重新汇总发布目录里的安装包（含未签名测试产物；不删除旧版本记录）
$sums = Get-ChildItem -Path $DistDir -File |
  Where-Object { $_.Name -match '^QIO_.*_x64-setup(-UNSIGNED-TEST)?\.exe$' } |
  Sort-Object Name | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash
    "$($_.Name)  $hash"
  }
Write-Utf8NoBom -Path (Join-Path $DistDir "SHA256SUMS.txt") -Text ($sums -join "`r`n")

# 构建 manifest：把「这次构建到底用了什么配置」写成事实，供 release_gate.py 核对。
# 没有它的时候，闸门只能读 tauri.conf.json 猜这次构建用了什么 —— 上一阶段就因为
# 构建时用 --config 关掉 createUpdaterArtifacts 而漏判过一次（真产物没签名，闸门报 PASS）。
$installerPath = Join-Path $DistDir $fileName
$installerHash = (Get-FileHash -Algorithm SHA256 $installerPath).Hash.ToLower()
$sidecarFile = Get-ChildItem -Path (Join-Path $root "frontend\src-tauri\binaries") -Filter "qio-backend-*.exe" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime | Select-Object -Last 1
$sidecarInfo = $null
if ($sidecarFile) {
  $sidecarInfo = [ordered]@{
    name   = $sidecarFile.Name
    sha256 = (Get-FileHash -Algorithm SHA256 $sidecarFile.FullName).Hash.ToLower()
    bytes  = $sidecarFile.Length
  }
}
$commit = ""
$prevPref = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { $commit = ((& git -C $root rev-parse HEAD 2>$null | Select-Object -First 1) + "").Trim() } catch { $commit = "" }
$ErrorActionPreference = $prevPref

$overrides = [ordered]@{}
$signingState = "signed"
if ($UnsignedTestArtifact) {
  $overrides = [ordered]@{ bundle = [ordered]@{ createUpdaterArtifacts = $false } }
  $signingState = "unsigned-test"
}

$buildManifest = [ordered]@{
  schema            = 1
  product           = "QIO"
  version           = $version
  commit            = $commit
  built_at          = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  bundles           = $Bundles
  installer         = $fileName
  installer_sha256  = $installerHash
  installer_bytes   = (Get-Item $installerPath).Length
  sidecar           = $sidecarInfo
  updater_artifacts = (-not $UnsignedTestArtifact)
  config_overrides  = $overrides
  signing           = $signingState
}
$buildManifestPath = "$installerPath.build.json"
Write-Utf8NoBom -Path $buildManifestPath -Text ($buildManifest | ConvertTo-Json -Depth 5)

Write-Host "完成。"
Write-Host "  安装包：$(Join-Path $DistDir $fileName)"
if ($UnsignedTestArtifact) {
  Write-Host "  签名：  （无 —— 未签名测试产物）"
  Write-Host "  清单：  （未更新 latest.json）"
  Write-Host "下一步：先拿到更新签名私钥口令，再按不带 -UnsignedTestArtifact 的方式重跑本脚本。"
} else {
  Write-Host "  签名：  $(Join-Path $DistDir "$fileName.sig")"
  Write-Host "  清单：  $manifestPath"
  Write-Host "下一步：powershell -File scripts\publish_release.ps1 -Version $version"
}
Write-Host "  构建 manifest：$buildManifestPath（release_gate.py 用它核对构建配置身份）"
