# QIO #46 R8 · final 分组跑（产品 vite 5201 / 后端 8736 / provider 8809 / 数据目录 final）
param(
  [string]$OutRoot = "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8\final",
  [string]$Groups = "g1,g2,g3,g4,g5,g6,g7"
)
$ErrorActionPreference = "Continue"
$env:QIO_E2E_BASE = "http://127.0.0.1:5201"
$env:QIO_E2E_API = "http://127.0.0.1:8736"
$env:QIO_E2E_PROVIDER = "http://127.0.0.1:8809"
$env:QIO_UI_DATA_DIR = "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8\e2e-data\final"
$env:QIO_UI_CDP_PORT = "9335"
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
$map = @{ g1 = "s1"; g2 = "s2"; g3 = "s3a,s3b"; g4 = "s4"; g5 = "s5"; g6 = "s6a"; g7 = "s7a" }
foreach ($g in ($Groups -split ',')) {
  $g = $g.Trim(); if (-not $g) { continue }
  $only = $map[$g]; if (-not $only) { continue }
  $out = Join-Path $OutRoot $g
  New-Item -ItemType Directory -Force -Path $out | Out-Null
  $env:QIO_UI_OUT = $out
  $env:Q48_ONLY = $only
  $log = Join-Path $OutRoot ($g + ".log")
  Write-Host ("===== FINAL " + $g + " (" + $only + ") =====")
  Push-Location "D:\qio-dev\q46-r8\_lead-logs\qio032-46\r8\verify"
  node run-48.mjs 2>&1 | Tee-Object -FilePath $log
  $code = $LASTEXITCODE
  Pop-Location
  Write-Host ("----- END " + $g + " exit=" + $code + " -----")
}
Write-Host "ALL_FINAL_GROUPS_DONE"
