# I2V2F · D 组验收一键运行（阶段 2）
# 用法：pwsh -File scripts/i2v2-verify/run.ps1 [-Ui http://127.0.0.1:5303] [-Board board_d_accept]
param(
  [string]$Ui = "http://127.0.0.1:5301",
  [string]$Api = "http://127.0.0.1:8744",
  [string]$Board = "board_d_accept",
  [string]$Out = "D:\qio-dev\_i2v2-evidence\D-shots",
  [string[]]$Flows = @()
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here "..\..")

$env:QIO_ACCEPT_API = $Api
$env:QIO_ACCEPT_BOARD = $Board
python (Join-Path $here "seed_env.py") --board $Board
if ($LASTEXITCODE -ne 0) { throw "seed failed: $LASTEXITCODE" }

$env:QIO_ACCEPT_UI = $Ui
$env:QIO_ACCEPT_API = $Api
$env:QIO_ACCEPT_BOARD = $Board
$env:QIO_ACCEPT_OUT = $Out
$args = @((Join-Path $here "playwright_flows.mjs")) + $Flows
node @args
exit $LASTEXITCODE
