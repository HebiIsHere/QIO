# 打包 Python 后端为 Tauri sidecar 可执行文件
$py = 'C:\Users\zxy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$root = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $root 'backend'
$binaries = Join-Path $root 'frontend\src-tauri\binaries'

Set-Location $backend
& $py -m PyInstaller --noconfirm --onefile --name smart-agent-backend `
  --collect-all tiktoken `
  --hidden-import keyring.backends.Windows `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  --distpath dist-sidecar --workpath build-sidecar `
  src/agent/main.py 2>&1 | Select-Object -Last 5
if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller failed: $LASTEXITCODE"; exit 1 }

New-Item -ItemType Directory -Force -Path $binaries | Out-Null
$target = Join-Path $binaries 'smart-agent-backend-x86_64-pc-windows-msvc.exe'
Copy-Item (Join-Path $backend 'dist-sidecar\smart-agent-backend.exe') $target -Force
Write-Host "sidecar written: $target ($([math]::Round((Get-Item $target).Length / 1MB, 1)) MB)"