# QIO 架构文档

版本：v0.9（Turn Runtime / Trace / 预算 / 能力安全重构后，2026-09-12）
实现进度与已知限制看 `docs/status.md`；本文只描述**结构与契约**。

---

## 1. 项目定位

QIO 是一个面向长对话的本地优先 agent：对话以网状话题组织，而不是线性会话列表。

- 长对话不因会话变长而简单丢失上下文：记忆域 + 知识域双基础设施
- agent 可通过对话自主创建工具，工具与内置工具并列，创建需人工审批
- 模型全部由用户自带 Key（BYOK），没有系统自有 Key
- 数据留在本机：SQLite + 系统凭据库，后端独占管理

## 2. 技术选型

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 后端 | Python 3.11+ / FastAPI | sidecar 子进程，本地 HTTP + SSE |
| 依赖 | uv（`backend/uv.lock`） | 本地与 CI 同一套锁定版本 |
| 存储 | SQLite（WAL）+ 顺序迁移 | 嵌入式；schema 版本见 `agent/storage/schema.py` |
| 凭据 | 系统凭据库（keyring） | 密钥永不落盘 |
| 前端壳 | Tauri 2（Rust） | 窗口与 Python sidecar 生命周期 |
| 前端框架 | Vue 3 + Vite + TypeScript + Pinia | — |
| 前端 npm | `npm ci`（`package-lock.json`） | 与 CI 一致 |

运行形态：Tauri 壳启动 Python sidecar，前端通过 localhost HTTP + SSE 与后端通信。前端壳可整体替换，后端协议不变。

## 3. 分层总览

请求自上而下穿过这些层，每层只依赖它下面的层：

```
UI（Tauri 壳 + Vue）
   │  HTTP + SSE
   ▼
API / EventBus
   │
   ▼
TurnManager                    ← 每个 turn 的身份、排队与取消在这里确定
   │
   ▼
TurnOrchestrator / Agent Runtime
   │
   ├───────────────┬───────────────────┐
   ▼               ▼                   ▼
Context Engine   Capability Engine   Model Adapters
   │               │                   │
   └───────────────┴───────────────────┘
                   ▼
              Local Store（SQLite / 归档 / 嵌入 / 审计 / Trace）
```

依赖方向是单向的：下层不认识上层。核心层（Runtime、Context、Capability）只使用 QIO 自己定义的数据模型，不接触任何厂商 SDK 的响应对象。

## 4. 各层职责

### 4.1 API 层

- FastAPI 路由 + `EventBus`：订阅者扇出、断线重连后的重放缓冲
- SSE 信封：`{ type, id, ts, data }`；事件类型集合以 `agent/api/events.py` 为准，文档不写死数量
- 事件里带 `turn_id`，前端据此把工具事件、警告、用量归属到具体一轮

### 4.2 TurnManager（单轮边界）

`agent/core/turn.py` 定义两个东西：

- `TurnContext`：属于**一轮**的全部可变状态（turn_id、输入、话题、预判、注入计划、loop 引用、取消状态、工具记录、通知、用量、结果）
- `TurnManager`：负责创建 turn_id、维护 active turn、排队、取消、关闭清理、以及把通知投递到正确的 turn

原则：**进程级服务挂在 AppContext / RuntimeServices；单轮状态挂在 TurnContext。** 不允许再把「当前 turn」的状态放在长生命周期对象上。

### 4.3 TurnOrchestrator / Agent Runtime

`agent/services/turn_orchestrator.py` 把一轮拆成可读的流水线：

```
begin          adapter 选择 + 锚点 + Trace 开始
build_context  话题预判 / 分类 + 记忆写入 + 上下文组装
execute_loop   AgentLoop：planning → act → observe
persist        话题切换时迁移当前消息 + 写入助手消息
post_turn      片段封块 / 滚动整理
finish         Trace 收尾 + 结果
```

Agent Runtime 自身的状态机在 `agent/core/loop.py`：

| 阶段 | 做什么 |
| --- | --- |
| PLANNING | 组装提示 → 调适配层 → 得到文本或工具调用 |
| ACT（TOOL_EXEC） | 执行工具，单工具失败被隔离成结果 + 警告 |
| OBSERVE（OBSERVING） | 工具结果回喂模型 |
| BUDGET | 迭代次数与 token 双约束；`core/guard.py` 另外拦截「同一失败调用反复重试」 |

### 4.4 Context Engine

只做**读侧**的上下文构造（`agent/services/context.py` 的 ContextAssembler），不修改 turn / 进程状态。

| 组成 | 位置 |
| --- | --- |
| Memory | `agent/memory/`、`agent/services/memory_lifecycle.py` |
| Knowledge | `agent/knowledge/` |
| Topics / 锚点 | `agent/graph/` |
| Entities | `agent/entities/` |
| Retrieval | `agent/services/retrieval.py`、`agent/services/affinity.py`、`agent/selector/` |
| Context packing | `agent/services/injection.py`、`agent/services/token_budget.py`、`agent/services/decay.py` |

预算模型（`TokenBudgetPlanner`）不是「上下文窗口 × 固定比例」：

```
context_window
  - system prompt
  - adapter / 协议开销
  - 工具定义
  - 当前用户输入
  - completion reserve（显式保留，绝不交给注入）
  = 可用于注入的空间
```

在这个空间内再按用途分配。注入总量受硬上限约束；强制项超预算时走确定性截断，不允许静默突破。

### 4.4.1 Anchor 生命周期（2026-09-12 定稿）

先固定语义（`docs/status.md` 与前端都以此为准）：

| 概念 | 含义 | 不是什么 |
| --- | --- | --- |
| Topic | 长期存在的讨论主题（QIO、某门课、某个项目） | 不是「一个局部问题的容器」 |
| Fragment | 记忆域的分块单位：存消息、封块、生成摘要、建立索引、控制上下文规模 | 本轮**不**引入 fragment 树 / 分支 / 讨论发展节点 |
| Anchor | 当前用户在某个 Topic 中明确关注或恢复到的历史位置 | 不是「该 Topic 最新 Fragment」的别名，不是检索结果，不是 Agent 读历史的副作用 |
| Memory Search | 只读检索：哪些过去的信息可能对当前问题有帮助 | 绝不改变 Anchor（调用多少次都一样） |
| StartHere | 用户在 Planet 明确选择历史位置 →「从这里继续」 | 不是「重置到最新位置」 |
| Agent Continue | `continue_from_fragment` 工具：Agent 在用户明确意图下显式改变讨论位置 | 与检索分离的独立动作；不是 `set_anchor` 这类数据库动作 |

位置存在 `cursor` 表（`active` 行 = 当前话题的位置；离开话题时旧位置写入 `history` 行），
**没有新增表、没有新增迁移**。优先级：用户当前明确选择 > Agent 显式 continue > Topic 历史保存位置 > Topic 默认位置。

一轮的 Focus 规则：只有「历史位置」（不是当前开放片段）才注入 Focus 块；
当前开放片段已经由短期转录覆盖，重复注入只是噪声。Focus 块结构是
`标题 + 摘要 + 开头少量消息 + （省略标记）+ 结尾少量消息`，受 `services/params.py::FOCUS.max_tokens` 硬上限
与 `TokenBudgetPlanner` 双重约束。

状态转换表（唯一事实，模糊描述（如「可能持续几轮，视情况而定」）不允许出现在实现或文档里）：

| 输入状态 | 动作 | 新状态 | 下一轮 Focus |
| --- | --- | --- | --- |
| active=(A, None) 或 (A, F27) | 用户 StartHere A13 | active=(A, F13) | F13（若 F13 不是当前开放片段） |
| active=(A, F13)（历史位置） | 一轮**成功** | active=(A, 本轮消息所在片段) | 不再重复 F13 |
| active=(A, F13)（历史位置） | 一轮失败 / 取消 | 不变 (A, F13) | 仍是 F13（重试不丢用户选择） |
| active=(A, F13) | switch_topic(B) | active=(B, B 的历史位置或 None)；A 的位置写入 history | B 的历史位置（若有，且不是当前开放片段） |
| active=(B, …) | switch_topic(A) | active=(A, A 的历史位置)；无效/跨话题片段降级为 None | A 的历史位置（若有） |
| active=(A, F13) | Agent `continue_from_fragment(F18)`（F18 ∈ B） | active=(B, F18) | F18 |
| active=(A, F13) | Agent `memory_search(...)` | 不变 | 不变 |
| active=(A, 无有效片段) | 任意话题内对话 | active=(A, …)（begin 只补默认话题，不猜片段） | 无 Focus |

安全降级：位置指向不存在 / 不属于该话题的片段时，一律当作「无位置」（返回 None），
既不注入错误片段，也不因为坏数据让切换抛错。

### 4.5 Capability Engine

| 组成 | 位置 |
| --- | --- |
| Tools | `agent/tools/registry.py`、`agent/tools/builtin.py`、各类内置工具 |
| Subagents | `agent/tools/subagent_tool.py`、`agent/tools/task_manager.py` |
| Credentials | `agent/credentials/` |
| Approval | `agent/tools/approval.py` |
| Sandbox | `agent/tools/sandbox.py` |
| ExecutionPolicy | `agent/tools/policy.py` |

### 4.6 Local Store

| 用途 | 位置 |
| --- | --- |
| SQLite（WAL）+ 迁移 | `agent/storage/db.py`、`schema.py`、`migrate.py` |
| 冷归档 | `agent/storage/archive.py` |
| 嵌入向量 | `embeddings` 表 + `agent/selector/onnx.py`、`remote.py` |
| 审计 | `credential_audit` 表 + `agent/credentials/store.py` |
| Trace | `turn_traces` 表 + `agent/trace/` |

---

## 5. 并发契约

这是当前版本必须被准确理解的边界。**HTTP 层可以同时收到多个 turn 请求，但 runtime 不是多 turn 并发的。**

| 项 | 当前契约 |
| --- | --- |
| 主 turn 调度 | single-flight：任意时刻活跃主 turn ≤ 1，`TurnManager` 保证 |
| 溢出请求 | 进入 FIFO 队列，不丢弃、不静默合并；队列状态通过 `TURN_QUEUE` 事件上报 |
| 取消 | 按 `turn_id` 定位；`/api/turns/cancel` 取消当前活跃或排队的 turn，`/api/turns/{turn_id}/cancel` 指定目标 |
| 工具事件归属 | 每次执行带 turn / 调用标识，监听方按标识过滤，不跨 turn 串线 |
| 子 agent 并发 | 并行运行，受 `TaskManager` 信号量限制（上限见 `agent/tools/task_manager.py`） |
| 子 agent 完成后的通知 | 作为独立 notify turn 投递，使用 `subagent:<task_id>` 作为 turn 标识；不写用户消息、不递归触发 |
| 取消与通知 | 取消作用于确定目标；不会因为一个 turn 结束就切断另一个 turn 的状态 |

**不要把「POST 后后台执行」理解成支持完全安全的多 turn 并发。** 当前是「可靠的单飞主 Agent + 明确排队」，而不是多用户 Agent Server；多用户并发属明确不做的范围（见 `status.md`）。

---

## 6. 沙箱安全契约

**受限子进程不是安全沙箱。** 它降低误操作风险（临时目录、最小环境变量、超时、输出上限），但不能安全执行任意不可信的 AI 代码。文档与界面都不得把它描述成强隔离。

强制力的真实边界（2026-09-12 实测）：执行器**只强制「声明」**——按策略拒绝高风险声明、
剥离环境变量、限制超时与输出；它**不强制**文件系统与网络隔离。声明为 PURE 的工具仍然能读取用户目录
（`os.listdir` 实测成功）。能力声明是给用户看的契约，不是内核级保证；谎报能力的工具不会被拦住。
要真正强制，需要容器（或等价的命名空间/ACL）隔离。

工具的能力由 `ToolExecutionPolicy` 显式声明，而不是由「用了哪个凭据」推断：

| 级别 | 含义 |
| --- | --- |
| PURE（0） | AI 新生成工具的**默认**权限：无凭据、无外网、无任意 shell、只能访问受控 scratch/temp、不访问用户文件、严格超时与资源限制 |
| RESTRICTED（1） | 工具显式申请能力（联网 / 某个文件路径 / 某类凭据 / 某个端点），必须在审批界面展示给人看 |
| TRUSTED（2） | 需要较强本机能力，必须用户显式批准，**不允许自动授予** |

执行与隔离：

- Docker 模式：默认 `--network none`；只有策略允许时才对内网开放；限制挂载、环境变量、CPU、内存、进程数、超时与输出
- 受限子进程模式：可用，但对高风险能力**拒绝执行**，不做静默降级；要执行必须走用户明确批准的 TRUSTED
- 策略扩大（新增能力）必须重新审批；沿用旧授权扩大权限是不允许的

凭据：

- 未经授权时，工具环境里没有任何凭据
- 只注入明确授予该工具的最小凭据（`QIO_KEY_*`）
- Trace、错误与测试输出统一脱敏（`agent/trace/redact.py`）

---

## 7. Trace 与 Eval

### 7.1 Trace 管线（运行时）

```
turn 开始 → trace.begin(turn_id)
   ├─ 话题：预判后端、候选、分数、最终话题、切换/创建操作
   ├─ 上下文：各项标识 + token 计数 + 预算分解 + 被丢弃项
   ├─ 模型调用：provider / 适配档 / 模型 / 序号 / token / 延迟 / 工具决策 / 重试 / 错误
   ├─ 工具：call_id / 名称 / 参数预览 / 起止 / 策略 / 结果预览
   ├─ 记忆与知识写入：消息、片段、摘要、候选、状态迁移
   └─ 警告与错误：机器可读 code + 人类可读说明
turn 结束 → trace.finish(status, duration)
```

约束：默认只存标识、脱敏预览与计数，不复制大段原文；所有内容在入库前经过统一脱敏，保证 Trace 默认是 safe-to-inspect。只读入口：`GET /api/traces`、`GET /api/traces/{turn_id}`，前端调试页 `/debug`。

### 7.2 Eval 管线（离线，不在关键路径）

```
backend/evals/*.jsonl  →  agent/eval/run.py  →  指标 JSON  →  与 backend/evals/baseline.json 对比
```

- 完全离线、确定性，不调用付费模型，不依赖网络
- 覆盖话题预判与记忆检索两类
- 用途是**防止退化**：调阈值/权重前先跑基线，改动后对比
- Eval 不属于运行时关键路径：它失败不应影响对话功能，也不作为功能开关

---

## 8. 数据流（一个 turn）

```
用户输入 → POST /api/turns
  → TurnManager：创建 turn_id；若已有活跃主 turn 则排队（TURN_QUEUE）
  → TURN_START（带 turn_id）
  → 凭据快照 → CAPABILITY 校验
  → 话题预判 → 记忆写入（当前消息绑定到开放片段）
  → 上下文组装（Focus（仅历史位置）/ 短期记忆 / 知识 / 检索 / 身份去重 / 预算截断）→ MEMORY_INJECT
  → AgentLoop：PLANNING → TOOL_START/TOOL_END → OBSERVING → … → 终答
  → 话题切换时迁移当前消息 → 写入助手消息
  → 片段封块 / 摘要 / 知识提炼（阈值触发）
  → 成功：位置推进到本轮片段并广播 ANCHOR（historic=false）
  → TURN_END + USAGE → Trace 收尾
```

工具执行的事件与警告都带 `turn_id`，因此客户端可以把一轮内的所有活动归并展示。

## 9. 数据模型（SQLite）

迁移是顺序的、只前进的：改 schema 必须追加新迁移，不能修改历史迁移。表的完整清单以 `agent/storage/schema.py` 为准（本文不写死数量），主要几类：

| 类别 | 表 |
| --- | --- |
| 对话与记忆 | `messages`、`fragments`、`memory_index` |
| 知识与图 | `knowledge`、`nodes`、`edges`、`entity_mentions`、`entity_cards`、`cursor` |
| 凭据 | `credentials`、`credential_audit` |
| 检索 | `embeddings` |
| 工具 | `tools`、`tool_calls` |
| 运行 | `settings`、`turn_traces`、`schema_version` |

归档策略：消息原文超过冷归档期限后 gzip 归档，SQLite 保留元数据与归档引用；摘要、索引与知识永久保留。

## 10. 里程碑状态

各里程碑的完成情况、实现位置、测试与已知限制统一记录在 **`docs/status.md`**，本文不再重复维护一张状态表，避免两处漂移。

## 11. 环境与验证

准确的安装与运行命令以 `docs/SETUP.md` 和 `.github/workflows/ci.yml` 为准（两者必须一致）。日常改动后的验证至少包括：

```powershell
# 文档一致性（里程碑状态、被引用的路径与命令、硬编码数字）
python scripts/check_docs.py

# 后端
cd backend
uv run --frozen pytest

# 评测基线（离线确定性，改阈值前后各跑一次）
uv run --frozen python -m agent.eval.run

# 前端
cd ..\frontend
npx vue-tsc --noEmit
npm test
```

端到端联调参考脚本：`scripts/e2e_up.py` / `scripts/e2e_down.py`（拉起后端与前端并记录 pid）。
