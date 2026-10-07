# 后端全量回归：保留退出码与汇总行
Set-Location D:\qio-dev\qio-ui\backend
$out = Join-Path $env:TEMP 'qio-ui-integration\pytest-full.log'
uv run --frozen pytest -q --color=no 2>&1 | Out-File -FilePath $out -Encoding utf8
$code = $LASTEXITCODE
Write-Output ("PYTEST_EXIT=" + $code)
Get-Content $out | Select-String -Pattern "passed|failed|error|FAILED|ERROR" | Select-Object -Last 8 | ForEach-Object { $_.Line }
