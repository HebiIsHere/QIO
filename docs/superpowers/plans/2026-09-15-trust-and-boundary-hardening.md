# QIO 第一阶段：可信性 / 安全边界 / 任务生命周期

状态：active（本文件是执行计划，不参与 `scripts/check_docs.py` 的里程碑一致性检查）

## 目标与优先级

> 把核心运行链路修到「结果可信、任务状态可信、安全边界可信」。

优先级：正确性 > 安全性 > 状态一致性 > 可恢复性 > 用户反馈 > 视觉表现。

## 现状核实（先读代码，再改）

| 编号 | 现象 | 代码位置 | 结论 |
| --- | --- | --- | --- |
| A1 | `POST /api/turns` 不返回 `turn_id` | `backend/src/agent/api/server.py` `start_turn` | 确认存在 |
| A2 | 无凭据 / 初始化失败 → 前端永久 running | `services/turn_orchestrator.py::begin` 只发 WARNING 就 `return None` | 确认存在 |
| A3 | model / tool / context 异常路径没有 `TURN_END` | `core/loop.py::_plan` 抛错 → `execute_loop` 只发 ERROR | 确认存在 |
| A4 | `TURN_END` 由 `AgentLoop` 发出 → 子 agent / 维护任务也发 | `tools/subagent_tool.py`（`turn_id=subagent:task_x`） | 确认存在（会把主 turn 提前结束） |
| A5 | 最终回答可能丢失 | `frontend/src/stores/events.ts` `TURN_END` 分支：最后一条已是 assistant 就不插入 `final_content` | 确认存在 |
| A6 | ERROR 被当成 turn 结束 | `frontend/src/stores/events.ts` `case "ERROR"` 调 `session.turnEnded()` | 确认存在 |
| A7 | 取消不终止整个 turn | `core/turn.py::cancel_active` 只取消在途工具 task | 确认存在 |
| A8 | SSE 重连重放最近事件 | `api/bus.py::stream` 每次连接回放 `_history` | 确认存在 |
| A9 | 事件线上格式没有 `id:` 行 | `api/events.py::sse_format` | 确认存在 |
| A10 | 历史读取失败 = 空历史 | `stores/session.ts::loadHistory` 的 `catch {}` | 确认存在 |
| B1 | `Access-Control-Allow-Origin: *` | `api/server.py::create_app` | 确认存在 |
| B2 | API 无身份认证 | 全部 `/api/*` | 确认存在 |
| B3 | `POST /api/events/test` 无条件注册 | `api/server.py` 末尾 | 确认存在 |
| B4 | credential endpoint 可静默改 + 复用旧 key | `credentials/store.py::update_metadata` + `PATCH /api/credentials/{id}` | 确认存在 |
| B5 | main-loop 凭据选择顺序反了 | `credentials/policy.py::resolve` 的 sort key | 确认存在（vision 会先于 main-loop） |
| B6 | approval 未绑定 session/turn，无过期 | `tools/approval.py` | 确认存在 |
| B7 | `run_cmd` 用 `create_subprocess_shell` 执行，安全判断按字符串前缀 | `tools/cmd_tools.py` + `services/computer.py` | 确认存在（`ls && ...` 被当低危自动执行） |
| B8 | `fs_find` 默认 root 是 `Path(".")`（进程 cwd） | `tools/fs_tools.py::FsFindTool` | 确认存在 |
| B9 | Markdown `link` 直接输出 `href` | `components/MarkdownContent.vue::renderNode` | 确认存在（`javascript:` 可点） |
| B10 | Tauri `csp: null`；前端硬编码 `http://127.0.0.1:8734` | `src-tauri/tauri.conf.json`、`services/events_const.ts` | 确认存在 |

## 执行分解

### 1. Turn 生命周期单一事实源（本 agent）

- `TurnManager`（`core/turn.py`）成为唯一发出 `TURN_START` / `TURN_END` 的地方：
  - 受理时发布 `TURN_QUEUE`（已有）；worker 真正开跑时发布一次 `TURN_START{turn_id, message}`；
  - 无论正常返回、异常、取消、无凭据早退，`finally` 保证恰好一次 `TURN_END{turn_id, status, final_content, error}`；
  - `status ∈ {completed, failed, cancelled, unavailable}`。
- `AgentLoop` 不再发 `TURN_START` / `TURN_END`（子 agent、维护任务、dev workflow 都受影响，全部受益）。
- `POST /api/turns` 同步返回 `{ok, accepted, turn_id, status: "accepted"}`。
- 早退路径（无凭据 / provider 初始化失败）由 orchestrator 标记 `ctx.status`，仍由 TurnManager 收口。

### 2. Cancellation（本 agent）

- `TurnContext.cancelled` 是唯一取消标记；`AgentLoop` 在每个 checkpoint 检查：
  before model call / after model call / before tool call / after tool call /
  before next iteration / before persisting final / before post-turn memory。
- 取消后不得持久化 assistant 消息、不得推进 anchor、不得跑 memory consolidation。

### 3. SSE 协议（本 agent）

- 线上格式补 `id: <event_id>`；`EventBus.stream(last_event_id=...)` 只发之后的事件。
- 新增一次性、短 TTL、`scope=events` 的 ticket（`POST /api/events/ticket`）用于 EventSource 认证。
- 前端 transport 带 `Authorization`，重连时带 `last_event_id`，并按 `event_id` 去重（有界）。

### 4. localhost API 身份认证（本 agent）

- `QIO_SESSION_TOKEN`（或 `QIO_SESSION_TOKEN_FILE`）→ Bearer / `X-QIO-Session` 校验中间件；
  无 token 且非显式 `QIO_DEV_INSECURE=1` 时，后端自建随机 token（fail-closed）。
- Origin / Host 白名单（Tauri WebView origin + dev origin），CORS 不再 `*`。
- 随机端口：Tauri 先挑空闲端口，把端口 + token 交给 sidecar 与前端。
- `/api/events/test` 只在开发模式注册，且同样要求认证。

### 5. 前端状态真实性（并行 agent 2）

- `TURN_END.final_content` 是最终回答的唯一权威来源；interim 与 final 分离。
- ERROR 只显示错误，不再结束 turn。
- 历史读取有 `idle/loading/ready/error`，失败不表现成空历史，保留旧数据。
- 提交后立刻拿到 `turn_id` → 停止按钮立即可用。

### 6. 工具与凭据边界（并行 agent 1）

- `run_program`（argv + `shell=False` + 程序/参数白名单）与 `run_shell`（高风险，必审批）分离；
  shell 元字符不得绕过审批。
- 文件工具统一走 `ComputerSandbox` 的 root + containment（含 symlink 逃逸）。
- approval 绑定 `session/turn/approval`，单次使用 + 过期 + digest 校验。
- credential endpoint 变化必须重新输入 secret 并显式确认；main-loop 路由修正。

### 7. 桌面壳边界（并行 agent 2）

- Markdown 链接协议白名单（https/http/mailto），危险 scheme 不渲染成可点链接。
- 外链走 Tauri 官方 open（`plugin:shell|open`），不直接让 WebView 导航。
- 生产 CSP 最小权限；开发与生产分离。

## 验证

| 项 | 命令 |
| --- | --- |
| 后端 | `cd backend; uv run --frozen pytest` |
| 前端类型 | `cd frontend; npx vue-tsc --noEmit` |
| 前端测试 | `cd frontend; npm test` |
| 文档 | `python scripts/check_docs.py` |
| 真实运行 | `python scripts/e2e_up.py` + 无头浏览器场景 1–7 |

未跑到的项必须在最终报告里写 NOT RUN，不得写成 PASS。
