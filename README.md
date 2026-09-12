# QIO

QIO 是一个面向长对话、不会随着会话增长而简单丢失上下文的本地优先 Agent。

普通聊天应用把历史当成一条越拉越长的线：要么全文塞进上下文，要么粗暴滚动截断。QIO 换一种组织方式——对话按**话题**组织成网状结构，并显式区分「对话记忆」和「稳定知识」，每一轮只把当前真正需要的那部分放进模型上下文。

## 它解决什么

**话题图（Topic Graph）**
对话不是一条线，而是一张图。话题、实体、用户三类节点通过边连接；当前所在位置是一个锚点，切换话题时历史位置被保留而不是丢弃。你可以从任意话题重新进入长对话，而不是从头翻记录。

**记忆域（Memory）**
对话原文按片段组织，封块时生成摘要与机械索引（关键词、实体、token 估算）。注入时优先保证最近的对话连续性，再按相关性、新鲜度与话题亲和度挑选更早的内容。

**知识域（Knowledge）**
跨对话稳定成立的信息（偏好、决定、事实）走独立的状态机与版本链：新结论会替代旧结论，而不是两条并存。低影响条目可自动生效，高影响的（用户画像、目标、Agent 自我认知）需要你确认。

**工具（Tools）**
Agent 可以在对话中创建自己的工具，与内置工具并列注册，重启后自动恢复。创建需要人工审批，并且审批界面会写清楚这个工具「能访问什么」。新建工具默认拿到最小能力：不联网、不碰用户文件、只能在自己受控的临时目录里运行；要扩大能力必须重新审批。

**BYOK（自带模型 Key）**
模型全部由你自己提供，没有系统自有 Key。Key 存放在本机系统凭据库，元数据存 SQLite；界面永远显示掩码。没有配置 Key 时应用不会假装可用，而是直接告诉你。

## 设计取向

- **本地优先**：数据留在本机（SQLite + 系统凭据库），后端独占管理数据，前端只通过 API 访问。
- **可替换模型**：核心层只依赖 QIO 自己的模型协议，不绑定任何单一厂商的 SDK 结构；同一个主循环可以跑在不同 provider 上。
- **能力显式**：工具能做什么写进策略并在审批时展示给你看，而不是隐含在实现里。
- **边界诚实**：受限子进程不是安全沙箱；当前版本的主 Agent 也不是并发多轮的。这些边界写在架构文档里，而不是靠措辞模糊掉。

## 文档

| 想了解 | 看这里 |
| --- | --- |
| 现在真的做到哪了、有哪些已知限制 | [`docs/status.md`](docs/status.md) |
| 分层结构、并发契约、沙箱安全契约 | [`docs/architecture.md`](docs/architecture.md) |
| 最近一次验收结果与遗留问题 | [`docs/release-qualification.md`](docs/release-qualification.md) |
| 安装与运行 | [`docs/SETUP.md`](docs/SETUP.md) |
| 前端设计规范与组件 | [`docs/frontend-design.md`](docs/frontend-design.md)、[`docs/frontend-components.md`](docs/frontend-components.md) |
| 协作约定（含 AI 助手约定） | [`AGENTS.md`](AGENTS.md) |

## 运行

前置：安装 [uv](https://docs.astral.sh/uv/)（`python -m pip install uv`）。后端依赖由
`backend/uv.lock` 锁定，CI 与本地使用同一条命令安装，避免「本地任意解析、CI 另一组版本」。

后端（开发）：

```
cd backend
uv sync --frozen --extra dev
uv run --frozen uvicorn agent.main:create_app --factory --port 8734
```

SSE 冒烟验证：

```
cd backend
uv run --frozen python scripts/verify_sse.py
```

前端（开发，需 Rust 工具链）：

```
cd frontend
npm ci
npm run tauri dev
```

测试：

```
cd backend
uv run --frozen pytest

cd frontend
npm ci
npm test
```

一键拉起后端 + 前端：`python scripts/e2e_up.py`（关闭用 `scripts/e2e_down.py`）。

CI（`.github/workflows/ci.yml`）在 push / PR 上跑：后端 Python 3.11 与 3.12 全量测试、
前端类型检查 + 测试 + 构建、Rust `cargo check`；不需要任何真实 API Key。

## 技术栈

- 后端：Python 3.11+ / FastAPI，本地 HTTP + SSE 服务（作为 sidecar 运行）
- 前端：Tauri 2 壳 + Vue 3 + Vite + TypeScript + Pinia
- 存储：SQLite（WAL）+ 顺序迁移；数据目录默认 `%APPDATA%\qio`

更细的技术说明见 `docs/architecture.md`。
