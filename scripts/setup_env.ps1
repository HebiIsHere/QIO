# QIO 依赖一键安装脚本（Windows PowerShell）
# 用法:  powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1
# 覆盖: 后端 pip 依赖 + 前端 npm 依赖 + Rust 检查 + embedding 模型检查
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent   # qio/

Write-Host "================ QIO 环境安装 ================" -ForegroundColor Cyan

# 1. 后端 Python 依赖
Write-Host "[1/4] 安装后端 Python 依赖..." -ForegroundColor Cyan
Push-Location (Join-Path $root "backend")
$uvOk = $true
python -m uv --version *> $null
if ($LASTEXITCODE -ne 0) { $uvOk = $false }
if ($uvOk) {
  # 与 CI 一致：按 uv.lock 精确安装，保证换机器版本相同
  python -m uv sync --frozen --extra dev
} else {
  Write-Warning "未检测到 uv，回退到 pip（版本解析可能与 CI 不一致）。建议：python -m pip install uv"
  python -m pip install -e ".[dev]"
}
if ($LASTEXITCODE -ne 0) { throw "后端依赖安装失败" }
Pop-Location
Write-Host "  后端依赖 OK" -ForegroundColor Green

# 2. 前端 Node 依赖
Write-Host "[2/4] 安装前端 Node 依赖..." -ForegroundColor Cyan
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Write-Warning "未检测到 npm，请先安装 Node.js >= 20（https://nodejs.org）后重跑本脚本。"
} else {
  Push-Location (Join-Path $root "frontend")
  if (Test-Path (Join-Path $root "frontend/package-lock.json")) {
    npm ci          # 与 CI 一致：严格按 lockfile 安装
  } else {
    npm install
  }
  if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败" }
  Pop-Location
  Write-Host "  前端依赖 OK" -ForegroundColor Green
}

# 3. Rust 工具链（Tauri 壳，可选——只跑 Web 版可跳过）
Write-Host "[3/4] 检查 Rust 工具链..." -ForegroundColor Cyan
if (Get-Command cargo -ErrorAction SilentlyContinue) {
  Write-Host "  cargo OK: $(cargo --version)" -ForegroundColor Green
} else {
  Write-Warning "未检测到 cargo。如需构建桌面壳，请安装 Rust：https://rustup.rs（国内可配 rsproxy.cn 镜像）。只跑 Web 版可跳过。"
}

# 4. Embedding 模型（bge-small-zh-v1.5）
Write-Host "[4/4] 检查 embedding 模型..." -ForegroundColor Cyan
$dataDir = if ($env:QIO_DATA_DIR) { $env:QIO_DATA_DIR } else { Join-Path $env:APPDATA "qio" }
$modelDir = Join-Path $dataDir "models\bge-small-zh-v1.5"
if (Test-Path (Join-Path $modelDir "model_quantized.onnx")) {
  Write-Host "  模型 OK: $modelDir" -ForegroundColor Green
} else {
  Write-Warning "未找到模型：$modelDir"
  Write-Warning "请放置 model_quantized.onnx 与 tokenizer.json（从 HuggingFace BAAI/bge-small-zh-v1.5 导出，或从已有环境拷贝）。"
  Write-Warning "缺失时向量召回降级为 BM25，功能可用但话题预测/向量检索不可用。"
}

Write-Host "================ 完成 ================" -ForegroundColor Cyan
Write-Host "启动后端: cd backend; uv run --frozen uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8734"
Write-Host "启动前端: cd frontend; npm run dev"
