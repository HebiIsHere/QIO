# QIO #46 R8 · 基线分组复跑（每组独立进程 / 独立 OUT，避免一个场景卡死拖垮全部）
param(
  [string]$OutRoot = "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8\baseline",
  [string]$VitePort = "5202",
  [string]$ApiPort = "8735",
  [string]$ProviderPort = "8808",
  [string]$DataName = "baseline",
  [string]$Groups = "g1,g2,g3,g4,g5,g6,g7,g8"
)
$ErrorActionPreference = "Continue"
$env:QIO_E2E_BASE = "http://127.0.0.1:$VitePort"
$env:QIO_E2E_API = "http://127.0.0.1:$ApiPort"
$env:QIO_E2E_PROVIDER = "http://127.0.0.1:$ProviderPort"
$env:QIO_UI_DATA_DIR = "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8\e2e-data\$DataName"
$env:QIO_UI_CDP_PORT = "9334"
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
$map = @{
  g1 = "s1"; g2 = "s2"; g3 = "s3a"; g4 = "s3b"; g5 = "s4"; g6 = "s5"; g7 = "s6a"; g8 = "s7a"
}
foreach ($g in ($Groups -split ',')) {
  $g = $g.Trim(); if (-not $g) { continue }
  $only = $map[$g]; if (-not $only) { continue }
  $out = Join-Path $OutRoot $g
  New-Item -ItemType Directory -Force -Path $out | Out-Null
  $env:QIO_UI_OUT = $out
  $env:Q48_ONLY = $only
  $log = Join-Path $OutRoot ($g + "-" + $only + ".log")
  Write-Host ("===== RUN " + $g + " (" + $only + ") =====")
  Push-Location "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8\verify"
  node run-48.mjs 2>&1 | Tee-Object -FilePath $log
  $code = $LASTEXITCODE
  Pop-Location
  Write-Host ("----- END " + $g + " exit=" + $code + " -----")
}
Write-Host "ALL_GROUPS_DONE"
