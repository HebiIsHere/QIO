# Smart Agent

一个长对话场景的本地优先 agent：agentloop 主循环 + 记忆/知识双域 + 网状话题导航 + 可自主创建工具。

当前状态：第一批骨架（M0-M4 范围）已完成。

## 架构

- 后端：Python 3.11+ / FastAPI / openai SDK，本地 HTTP + SSE 服务（sidecar）
- 前端：Tauri 2 壳 + Vue 3 + Vite + TypeScript + Pinia
- 存储：SQLite（WAL），数据目录默认 `%APPDATA%/smart-agent`

## 目录

- `backend/src/agent/api`：HTTP + SSE 事件协议（14 个事件类型）
- `backend/src/agent/core`：agentloop 状态机、迭代/令牌双预算
- `backend/src/agent/adapters`：模型三态适配（native/text/unsupported）+ 能力探测
- `backend/src/agent/credentials`：BYOK 凭据（keyring + 授权策略 + 审计）
- `backend/src/agent/storage`：SQLite schema、迁移、冷归档
- `backend/src/agent/tools`：工具注册表与内置工具
- `frontend/src-tauri`：Tauri 壳（启动 Python sidecar）
- `frontend/src`：Vue 应用

## 运行

后端（开发）：

```
cd backend
python -m uvicorn agent.main:create_app --factory --port 8734
```

SSE 冒烟验证：

```
python backend/scripts/verify_sse.py
```

前端（开发，需 Rust 工具链）：

```
cd frontend
npm install
npm run tauri dev
```

测试：

```
cd backend
python -m pytest
```

## 已冻结设计（摘要）

- 模型三态：native（tool calling）/ text（文本协议兜底）/ unsupported（默认拒启 + 强制继续开关）
- 凭据：用户自带 Key（BYOK），keyring 存密钥，SQLite 存元数据，授权取类别默认与 Key scope 的最严格交集
- 记忆域：原文 → 摘要 → 目录三层，6 个月冷归档；知识域：状态机 draft → pending_review → verified → active → expired/revoked
- 图导航：用户/实体/话题三类节点；锚点 = 当前话题 + 片段位置
- 工具：创建需双段审批（创建 + 凭据授权），交叉测试，沙箱执行
- 选择器（M5）：规则层常开 + 可插拔召回（BM25 / ONNX 嵌入 / 远程嵌入）+ 可选精排