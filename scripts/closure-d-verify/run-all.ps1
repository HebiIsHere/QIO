# 【独立验收 D · closure-d】一条命令复跑全部六条反例（基线 / 候选 SHA 通用）。
#
# 设计要点：
# - 每一步都显式使用**独立临时 QIO_DATA_DIR**（脚本自建 tmp/run-<时间戳>），绝不继承用户真实数据目录；
# - 端口固定 8734 / 5199：被占用时**先报错退出**，不去杀别人的进程；
# - 证据全部落到 scripts/closure-d-verify/evidence/（文件名带 phase 前缀，便于基线/候选对照）；
# - 退出码 = 0 表示全部步骤通过。
#
# 用法（在仓库根目录）：
#   pwsh -File scripts/closure-d-verify/run-all.ps1                  # 全部
#   pwsh -File scripts/closure-d-verify/run-all.ps1 -SkipBrowser     # 跳过第⑤/⑥层（无 Chrome 时）
#   pwsh -File scripts/closure-d-verify/run-all.ps1 -Phase candidate # 证据文件带 candidate 前缀
param(
  [switch]$SkipBrowser,
  [string]$Phase = "phase1",
  [string]$Chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ev = Join-Path $PSScriptRoot "evidence"
New-Item -ItemType Directory -Force -Path $ev | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dataDir = Join-Path $PSScriptRoot ("tmp\run-" + $stamp)
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Write-Host "== closure-D 复跑开始（$Phase）=="
Write-Host "临时数据目录：$dataDir"

function Test-PortBusy([int]$p) {
  return $null -ne (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}
if (Test-PortBusy 8734) { throw "端口 8734 已被占用：请先停掉已有后端（本脚本不杀别人的进程）" }
if (-not $SkipBrowser -and (Test-PortBusy 5199)) { throw "端口 5199 已被占用：请先停掉已有前端 dev server" }

$backend = $null; $vite = $null; $fail = @()
try {
  # ---- 起后端（显式临时数据目录 + 开发豁免） ----
  $env:QIO_DATA_DIR = $dataDir
  $env:QIO_DEV_INSECURE = "1"
  $env:QIO_ENABLE_TEST_EVENTS = "1"
  $env:QIO_PORT = "8734"
  $env:PYTHONPATH = Join-Path $root "backend\src"
  $backend = Start-Process -FilePath (Join-Path $root "backend\.venv\Scripts\python.exe") `
    -ArgumentList "-m","uvicorn","agent.main:create_app","--factory","--host","127.0.0.1","--port","8734","--log-level","warning" `
    -WorkingDirectory (Join-Path $root "backend") -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $dataDir "backend.log") -RedirectStandardError (Join-Path $dataDir "backend.err.log")
  $ok = $false
  for ($i = 0; $i -lt 60; $i++) {
    try { if ((Invoke-WebRequest "http://127.0.0.1:8734/api/interactive/boards" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { $ok = $true; break } }
    catch { Start-Sleep -Milliseconds 700 }
  }
  if (-not $ok) { throw "后端未就绪，日志见 $dataDir\backend.err.log" }
  Write-Host "后端就绪：$dataDir\app.db"

  # ---- 第①②层：状态 + 真实组件 DOM ----
  Push-Location (Join-Path $root "frontend")
  npx vitest run src/stores/__tests__/closure-d-r1-r4.test.ts src/stores/__tests__/closure-d-r5-r6.test.ts src/components/interactive/__tests__/closure-d-impact-dialog.test.ts --reporter=json ("--outputFile=" + $ev + "\" + $Phase + "-all.json") 2>&1 | Tee-Object -FilePath "$ev\$Phase-vitest.txt" | Select-Object -Last 6
  if ($LASTEXITCODE -ne 0) { Write-Host "!! 第①②层有失败（基线预期；候选上必须全绿）" }
  Pop-Location

  # ---- 第③层：真实临时 sqlite + 完整 ASGI 路由 ----
  Push-Location (Join-Path $root "backend")
  & ".venv\Scripts\python.exe" -m pytest -q tests/test_closure_d_acceptance.py 2>&1 | Tee-Object -FilePath "$ev\$Phase-backend.txt" | Select-Object -Last 4
  if ($LASTEXITCODE -ne 0) { $fail += "第③层 pytest" }
  Pop-Location

  # ---- 第④层：真实 HTTP 旅程（真实库） ----
  Push-Location $root
  & "backend\.venv\Scripts\python.exe" "scripts/closure-d-verify/closure-d-api-journey.py" --base http://127.0.0.1:8734 --data-dir $dataDir 2>&1 | Tee-Object -FilePath "$ev\$Phase-http.txt" | Select-Object -Last 6
  if ($LASTEXITCODE -ne 0) { $fail += "第④层 HTTP 旅程" }
  Pop-Location

  # ---- 第⑤⑥层：真浏览器 + 真实进程关闭重开 ----
  # 用**另一个**独立临时数据目录 + 干净后端进程：第④层刚刚改过第一个库（板面/任务状态），
  # 浏览器层要能自己准备种子，不能让上一层的残留把结果带偏。
  if (-not $SkipBrowser) {
    if (-not (Test-Path $Chrome)) { Write-Host "跳过浏览器层：找不到 $Chrome" }
    else {
      $browserData = Join-Path $PSScriptRoot ("tmp\run-" + $stamp + "-browser")
      New-Item -ItemType Directory -Force -Path $browserData | Out-Null
      if ($backend) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
      Get-NetTCPConnection -LocalPort 8734 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
      Start-Sleep -Seconds 1
      $env:QIO_DATA_DIR = $browserData
      $backend = Start-Process -FilePath (Join-Path $root "backend\.venv\Scripts\python.exe") `
        -ArgumentList "-m","uvicorn","agent.main:create_app","--factory","--host","127.0.0.1","--port","8734","--log-level","warning" `
        -WorkingDirectory (Join-Path $root "backend") -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $browserData "backend.log") -RedirectStandardError (Join-Path $browserData "backend.err.log")
      $bok = $false
      for ($i = 0; $i -lt 60; $i++) {
        try { if ((Invoke-WebRequest "http://127.0.0.1:8734/api/interactive/boards" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { $bok = $true; break } }
        catch { Start-Sleep -Milliseconds 700 }
      }
      if (-not $bok) { $fail += "浏览器层后端未就绪" }
      $vite = Start-Process -FilePath "node" -ArgumentList "node_modules/vite/bin/vite.js","--port","5199","--strictPort","--host","127.0.0.1" `
        -WorkingDirectory (Join-Path $root "frontend") -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $browserData "vite.log") -RedirectStandardError (Join-Path $browserData "vite.err.log")
      $vok = $false
      for ($i = 0; $i -lt 60; $i++) {
        try { if ((Invoke-WebRequest "http://127.0.0.1:5199/" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { $vok = $true; break } }
        catch { Start-Sleep -Milliseconds 700 }
      }
      if (-not $vok) { $fail += "前端 dev server 未就绪" }
      if ($bok -and $vok) {
        Push-Location $root
        node scripts/closure-d-verify/closure-d-browser-probe.mjs --app http://127.0.0.1:5199 --api http://127.0.0.1:8734 --profile (Join-Path $browserData "chrome-profile") --out $ev --port 9344 --chrome $Chrome 2>&1 | Tee-Object -FilePath "$ev\$Phase-browser.txt" | Select-Object -Last 8
        if ($LASTEXITCODE -ne 0) { $fail += "第⑤/⑥层浏览器探针" }
        Pop-Location
      }
    }
  } else { Write-Host "已按要求跳过第⑤/⑥层" }
}
finally {
  if ($vite) { Stop-Process -Id $vite.Id -Force -ErrorAction SilentlyContinue }
  if ($backend) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
  Get-NetTCPConnection -LocalPort 8734 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  Get-NetTCPConnection -LocalPort 5199 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}

if ($fail.Count) { Write-Host ("== 有失败步骤：" + ($fail -join "；")); exit 1 }
Write-Host "== 全部步骤通过 =="
exit 0
