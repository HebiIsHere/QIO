# QIO 新机器安装与运行指南

QIO 是一个本地优先、长对话场景的 agent：Python(FastAPI) 后端 + Vue3 前端 + Tauri(Rust) 桌面壳，记忆/知识双域，向量召回（本地 ONNX embedding），BYOK（用户自带模型 Key）。

> 适用：Windows 10/11（keyring 依赖 Windows 凭据库；Linux/macOS 未验证）。
> 一键安装依赖：`powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1`（或按下面分步装）。
> 依赖锁定：后端用 **uv**（`backend/uv.lock`），前端用 **npm ci**（`frontend/package-lock.json`）——
> 安装命令与 CI 完全一致，换机器不会解析出另一组版本。

---

## 1. 系统环境

- Windows 10/11
- Git
- Chrome/Chromium（可选，WebGL 调试用）

## 2. 后端（Python 3.11+，实测 3.12）

```powershell
python -m pip install uv          # 只需一次；已有 uv 可跳过
cd backend
uv sync --frozen --extra dev      # 按 uv.lock 精确安装（CI 同款命令）
```

`uv sync` 会在 `backend/.venv` 建好隔离环境（已 gitignore），无需手动激活；
后续命令统一用 `uv run --frozen <cmd>` 执行，自动指向该环境。
应急替代（不推荐长期使用）：`python -m pip install -e ".[dev]"`——能跑，但版本解析可能与 CI 不一致。

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
npm ci
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

## 5. Embedding 模型（安装包内置 fp32；开发可指向本地模型）

**打包内置**：构建安装包前跑一次抓取脚本，把模型放进 Tauri 资源目录：

```powershell
# 从本机已有模型目录复制（校验 sha256，生成清单）
python scripts/models/fetch_model.py --from-dir "C:\Tools\models\bge-small-zh-v1.5"
# 或从 ModelScope 下载（hf-mirror 太慢时用这个源）
python scripts/models/fetch_model.py --from-modelscope
```

产物落在 `frontend/src-tauri/resources/models/bge-small-zh-v1.5/`（该目录已被 gitignore，90MB 二进制不进仓库），
由 `tauri.conf.json` 的 `bundle.resources` 打进安装包。**构建时缺这份资源会直接失败**，不会静默少模型。

内置档是 **fp32**（`model.onnx`，90.5MB，无量化损失）；想换成量化档就改清单里的 `default` 并换文件。

**运行时**：桌面壳启动时把内置模型复制到用户数据目录（幂等；内容指纹一致就跳过），
并把 `QIO_MODELS_DIR` 与 `QIO_DATA_DIR` 一起注入后端：

```
%APPDATA%\qio\models\bge-small-zh-v1.5\
├── model.onnx              # 内置默认档（fp32）
├── tokenizer.json
├── model_manifest.json     # 体积 + sha256 + 默认档
├── LICENSE-…txt / NOTICE.txt
└── .ready                  # 内容指纹：内容没变就不重复复制
```

**开发/自定义**：用 `QIO_MODELS_DIR` 指向任意符合上面布局的目录即可（例如你已经下好的
`C:\Tools\models`）；模型目录里放 `model_manifest.json` 就能选择加载哪一档，
向量缓存按「模型身份 + 维度」区分，换档位会重新编码。

**缺失影响**：不崩——向量召回自动降级为 **BM25**，话题预测走规则兜底；
日志里会写明原因（`model files missing; falling back`）。

## 6. 环境变量（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QIO_DATA_DIR` | `%APPDATA%\qio` | 数据目录（app.db / models / logs） |
| `QIO_MODELS_DIR` | `<data_dir>/models` | 模型目录（覆盖默认） |
| `QIO_PORT` / `QIO_HOST` | `8734` / `127.0.0.1` | 后端监听 |
| `PYTHONPATH` | — | 仅裸解释器运行时需要指向 `backend/src`；`uv run` 已自动处理 |
| `QIO_SESSION_TOKEN` | — | 本机 API 的会话令牌（桌面壳/开发脚本注入）。设置即强制认证 |
| `QIO_SESSION_TOKEN_FILE` | — | 让后端把自己的令牌写到这个文件（桌面壳用；0600，绝不进日志） |
| `QIO_DEV_INSECURE` | `0` | `1` = 显式开发豁免：不要求令牌（只允许本机 dev 用） |
| `QIO_ENABLE_TEST_EVENTS` | `0` | `1` = 注册开发用的 `POST /api/events/test`（生产构建里不注册） |
| `QIO_ALLOWED_ORIGINS` | — | 追加允许的 WebView origin（逗号分隔） |

> 不设任何令牌变量时后端会**自己生成**一个进程内令牌并拒绝所有未带令牌的请求
> （fail-closed，日志只提示、不打印令牌）。要连上它，要么设 `QIO_SESSION_TOKEN`，
> 要么用 `scripts/e2e_up.py`（默认开发豁免口径，`--secure` 为带令牌口径）。
> QIO 自己的 WebView 走的永远是带令牌路径：桌面壳挑一个随机空闲端口，让后端
> 生成令牌写到用户私有临时文件，再通过 `qio_backend_info` 命令交给自己的前端。

## 7. 启动

```powershell
# 后端（PowerShell，已 uv sync 过）
$env:QIO_DATA_DIR = "$env:TEMP\qio-dev"      # 或任意数据目录
cd backend
uv run --frozen uvicorn agent.main:create_app --factory --host 127.0.0.1 --port 8734

# 前端 dev（另开终端）
cd frontend
npm run dev      # Vite 端口见 vite.config.ts（默认 1420；e2e 用 5199）
```

参考脚本：`scripts/e2e_up.py`（一键拉起后端 + 前端，pid 写入 `scripts/.e2e-pids`）。

```powershell
python scripts/e2e_up.py             # 开发豁免口径（QIO_DEV_INSECURE=1 + 测试事件口）
python scripts/e2e_up.py --secure    # 带令牌口径：令牌只写进文件，终端只打印路径
python scripts/e2e_down.py           # 停止（按 pid + 真实端口探测确认已停）
python scripts/verify_stage1.py [--secure --token-file <path>]   # 第一阶段 HTTP 层验收
```

> 不用 uv、直接跑裸解释器时才需要 `$env:PYTHONPATH = "backend/src"`（或 `src`，视当前目录而定）。

## 8. 验证

```powershell
# 文档一致性（里程碑状态、被引用的路径与命令、与 CI 的命令对齐）
python scripts/check_docs.py

# 后端测试
cd backend
uv run --frozen pytest -q

# 前端测试 / 类型 / 构建（与 CI 顺序一致）
cd frontend
npm ci
npx vue-tsc --noEmit
npm test
npm run build

# 健康检查
curl http://127.0.0.1:8734/api/health   # -> {"status":"ok"}
```

同一条流水线在 CI 上运行，见 `.github/workflows/ci.yml`。

## 9. 常见问题

- **识别不出 API Key**：确认后端是**最新代码**并已重启（依赖/代码变更后 uvicorn 不热加载）；`POST /api/credentials/identify` 会用 key 逐个探测 16 个内置服务 `/models`，全部非 200 才判定失败。
- **embedding 不可用**：检查模型文件是否存在（见 §5）；后端日志会打印 `onnx embedding backend: model files missing; falling back`。
- **凭据测试报 402**：key 有效但余额不足（OpenAI 系），与 QIO 无关。
- **登录状态不生效 / 功能异常**：前端刷新加 `?fresh=N`（应用内浏览器对模块缓存不重新校验）。
