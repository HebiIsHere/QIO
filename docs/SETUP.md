# QIO 新机器安装与运行指南

QIO 是一个本地优先、长对话场景的 agent：Python(FastAPI) 后端 + Vue3 前端 + Tauri(Rust) 桌面壳，记忆/知识双域，向量召回（本地 ONNX embedding），BYOK（用户自带模型 Key）。

> 适用：Windows 10/11（keyring 依赖 Windows 凭据库；Linux/macOS 未验证）。
> 一键安装依赖：`powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1`（或按下面分步装）。

---

## 1. 系统环境

- Windows 10/11
- Git
- Chrome/Chromium（可选，WebGL 调试用）

## 2. 后端（Python 3.11+，实测 3.12）

```powershell
cd backend
python -m pip install -e ".[dev]"
```

依赖（已完整声明在 `backend/pyproject.toml`）：

| 包 | 用途 |
| --- | --- |
| fastapi / uvicorn[standard] | HTTP + SSE 服务 |
| openai | 主模型适配（native / text） |
| pydantic | 事件 / 摘要契约 |
| keyring | Windows 凭据库存 API Key |
| tiktoken | token 估算 |
| httpx | 凭据识别 / 端点探测 / 远端 embedding |
| numpy / onnxruntime / tokenizers | 本地向量召回（bge-small-zh-v1.5） |

dev 额外：`pytest`、`pytest-asyncio`。

## 3. 前端（Node.js ≥ 20，实测 24）

```powershell
cd frontend
npm install
```

依赖见 `frontend/package.json`：Vue 3 / Pinia / Vue Router / Vite / Vitest / three.js（星球）/ @tanstack/vue-virtual / remark 系等。

## 4. Tauri 桌面壳（Rust）

```powershell
rustup toolchain install stable   # https://rustup.rs
cd frontend/src-tauri
cargo check
```

- crates.io 直连不通时，配置 `rsproxy.cn` 镜像（见 `~/.cargo/config.toml`）。
- Cargo 依赖：tauri 2、tauri-plugin-shell、serde、serde_json。

## 5. ⚠️ Embedding 模型（项目未内置，必须手动准备）

运行时从数据目录读取：

```
<QIO_DATA_DIR>/models/bge-small-zh-v1.5/
├── model_quantized.onnx   # 量化 ONNX（512 维，CPU）
└── tokenizer.json
```

- **获取**：从 HuggingFace `BAAI/bge-small-zh-v1.5` 导出并量化成 onnx；或从一台跑过的机器拷贝这两个文件。
- **缺失影响**：不崩——向量召回自动降级为 **BM25**，话题预测走规则兜底；但向量记忆检索与 embedding 话题相似度不可用。

## 6. 环境变量（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QIO_DATA_DIR` | `%APPDATA%\qio` | 数据目录（app.db / models / logs） |
| `QIO_MODELS_DIR` | `<data_dir>/models` | 模型目录（覆盖默认） |
| `QIO_PORT` / `QIO_HOST` | `8734` / `127.0.0.1` | 后端监听 |
| `PYTHONPATH` | — | 跑后端/测试时需指向 `backend/src` |

## 7. 启动

```powershell
# 后端（PowerShell）
$env:PYTHONPATH = "backend/src"
$env:QIO_DATA_DIR = "$env:TEMP\qio-dev"      # 或任意数据目录
cd backend
python -m uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8734

# 前端 dev（另开终端）
cd frontend
npm run dev      # Vite 端口见 vite.config.ts（默认 1420；e2e 用 5199）
```

参考脚本：`scripts/e2e_up.py`（一键拉起后端 + 前端，pid 写入 `scripts/.e2e-pids`）。

## 8. 验证

```powershell
# 后端测试
cd backend
$env:PYTHONPATH = "src"
python -m pytest -q

# 前端测试 / 类型 / 构建
cd frontend
npm run test
npm run build

# 健康检查
curl http://127.0.0.1:8734/api/health   # -> {"status":"ok"}
```

## 9. 常见问题

- **识别不出 API Key**：确认后端是**最新代码**并已重启（依赖/代码变更后 uvicorn 不热加载）；`POST /api/credentials/identify` 会用 key 逐个探测 16 个内置服务 `/models`，全部非 200 才判定失败。
- **embedding 不可用**：检查模型文件是否存在（见 §5）；后端日志会打印 `onnx embedding backend: model files missing; falling back`。
- **凭据测试报 402**：key 有效但余额不足（OpenAI 系），与 QIO 无关。
- **登录状态不生效 / 功能异常**：前端刷新加 `?fresh=N`（应用内浏览器对模块缓存不重新校验）。
