# 配置 rsproxy.cn Rust 镜像源（cargo 卡在 crates.io 的解决方案）
$ErrorActionPreference = 'Stop'
$cfg = Join-Path $env:USERPROFILE '.cargo\config.toml'
$configLines = @(
  '[source.crates-io]',
  'replace-with = "rsproxy-sparse"',
  '',
  '[source.rsproxy-sparse]',
  'registry = "sparse+https://rsproxy.cn/index/"',
  '',
  '[registries.rsproxy]',
  'index = "sparse+https://rsproxy.cn/index/"',
  '',
  '[net]',
  'git-fetch-with-cli = true'
)
$content = $configLines -join "`n"
New-Item -ItemType Directory -Force -Path (Split-Path $cfg) | Out-Null
[System.IO.File]::WriteAllText($cfg, $content, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "config written: $cfg"

try {
    $r = Invoke-WebRequest -Uri 'https://rsproxy.cn/index/config.json' -UseBasicParsing -TimeoutSec 12
    Write-Host "rsproxy OK: $($r.StatusCode)"
} catch {
    Write-Host "rsproxy FAIL: $($_.Exception.Message)"
    exit 1
}

$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
Set-Location (Join-Path $PSScriptRoot '..\frontend\src-tauri')
cargo check 2>&1 | Select-Object -Last 20
Write-Host "cargo check exit=$LASTEXITCODE"