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
- 本机 API 边界（2026-09-15 定稿）：`agent/api/auth.py` 的 `SessionAuth` 要求会话令牌
  （`Authorization: Bearer` / `X-QIO-Session`，由桌面壳或开发脚本注入 `QIO_SESSION_TOKEN`），
  CORS 只信任 QIO WebView origin（`http://tauri.localhost` / `tauri://localhost`）与显式开启的
  开发 origin，Host 必须是回环地址；SSE 因为不能带 header，改用一次性、短 TTL、
  `scope=events` 的 ticket（`POST /api/events/ticket`），主令牌不进 URL。
  `GET /api/instance` 返回 `instance_id`/`pid` 供身份确认；`POST /api/events/test`
  只在开发模式注册（生产构建里路由不存在）。

### 4.2 TurnManager（单轮边界）

`agent/core/turn.py` 定义两个东西：

- `TurnContext`：属于**一轮**的全部可变状态（turn_id、输入、话题、预判、注入计划、loop 引用、取消状态、工具记录、通知、用量、结果）
- `TurnManager`：负责创建 turn_id、维护 active turn、排队、取消、关闭清理、以及把通知投递到正确的 turn

原则：**进程级服务挂在 AppContext / RuntimeServices；单轮状态挂在 TurnContext。** 不允许再把「当前 turn」的状态放在长生命周期对象上。

**生命周期协议（唯一事实源）**：TurnManager 负责发出全部 turn 事件——

```
accepted ──▶ running ──┬──▶ completed
                       ├──▶ failed
                       ├──▶ cancelled
                       └──▶ unavailable
```

- 每个被受理的 turn **恰好**一个 `TURN_START` 与**恰好**一个 `TURN_END`
  （后者在 `finally` 里收口，异常路径也不例外）；
- `TURN_END` 带 `{turn_id, status, final_content, error}`，`final_content` 是最终回答的
  唯一权威来源；`ERROR` 只表示「出错了」，永远不承担结束 turn 的职责；
- `AgentLoop` 只负责 planning/act/observe，不发 turn 事件 —— 它同时被 subagent、
  维护任务、工具开发流水线复用，这些都不是 turn；
- `POST /api/turns` 在受理时就返回 `{turn_id, status}`，前端不必从 SSE 里猜请求身份；
- 取消检查点覆盖模型调用前后、工具调用前后、下一次迭代前、持久化最终回答前、
  收尾记忆处理前：取消后不再发起新的模型/工具调用，也不把后续内容保存成正常最终回答。

### 4.2.1 工具执行状态（终态可恢复）

`EventBus` 是**实时通知**渠道：极端积压下允许丢弃可合并的过程事件（`TOOL_START`、
`TOOL_END` 也在其中）。但「这次工具调用最终是成功、失败还是取消」是服务器**已经知道的事实**，
不能因为一条通知没送到就永久变成「未知」。所以工具执行有一份独立于事件流的权威状态：
`backend/src/agent/core/tool_state.py` 的 `ToolExecutionState`（进程级、纯内存、有界）。

```
tool/start ──▶ 权威状态 running ──────────────────▶ SSE TOOL_START
tool/end   ──▶ 权威状态 success / failed / cancelled ──▶ SSE TOOL_END（带 status）
```

- 写权威状态**先于**发实时事件：事件丢了，终态仍然查得到；
- `GET /api/runtime/state` 的 `tools` 由它生成（活工具 + 最近结束的工具），
  这是 RESYNC 的恢复入口。前端按 `tool_call_id` 匹配、用 `turn_id` 校验后把卡片核对成
  真实终态 —— 「没收到通知」不等于「结果未知」；
- retention 以恢复需求为准：active Turn 的记录一律保留，其余终态记录按 TTL 与最大条数回收；
- 它**不是**事件日志、不是工具输出归档，也不落盘：进程重启后为空，此时界面显示
  「结果未收到」是诚实答案（不接受限制与保证见 `docs/status.md` 的 P13）；
- Subagent 内部工具不属于主对话：既不返回给前端，也不新增对应 UI（独立任务仍只恢复 Task 级状态）。

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
| Anchor | 当前对话**真实继续发生的位置**（话题 + 片段） | 不是「该 Topic 最新 Fragment」的别名，不是检索结果，不是 Agent 读历史的副作用，不是 Planet 的选中态 |
| Memory Search | 只读检索：哪些过去的信息可能对当前问题有帮助 | 绝不改变 Anchor（调用多少次都一样） |
| StartHere | 用户在 Planet 明确选择历史位置 →「从这里继续」（**新建接续片段**，来源片段只读） | 不是「重置到最新位置」，也不是「重新打开旧片段继续写」 |
| Agent Continue | `continue_from_fragment` 工具：Agent 在用户明确意图下显式改变讨论位置（与 StartHere 同一套语义） | 与检索分离的独立动作；不是 `set_anchor` 这类数据库动作 |
| Selected Topic | 用户在 Planet 上**正在浏览**的话题（纯前端状态） | 不是 Anchor，选中什么都不会改变当前对话位置 |
| Reference Topic | 为回答当前问题**临时读取**的另一个话题（检索 / Focus / 实体卡） | 不是导航，不改变 Anchor，也不改变选中态 |
| Pending Switch | 预测器认为「这段内容可能属于另一个话题」时给出的**建议** | 不是切换本身；用户确认前 Anchor 一动不动 |

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
| active=(A, F13) | Agent `continue_from_fragment(F18)`（F18 ∈ B） | 新建接续片段 F19（`source_fragment_id=F18`），active=(B, F19) | F18（接续片段本身是空的） |
| active=(A, F13) | Agent `memory_search(...)` | 不变 | 不变 |
| active=(A, 无有效片段) | 任意话题内对话 | active=(A, …)（begin 只补默认话题，不猜片段） | 无 Focus |

安全降级：位置指向不存在 / 不属于该话题的片段时，一律当作「无位置」（返回 None），
既不注入错误片段，也不因为坏数据让切换抛错。

**Anchor 只有一个写入者（第二阶段）**：所有改变对话位置的动作都必须经过
`services/navigation.py::TopicNavigationService`（进入话题 / 创建话题 / 确认切换 /
从历史继续）。Planet 选中、检索命中、Predictor 判断、API 直连都**不允许**直接写
`cursor` 表；`tests/test_topic_navigation.py::test_anchor_writes_are_centralized_in_the_navigator`
是一条架构守卫测试，用源码扫描强迫这条规则。

### 4.4.2 Planet（长期话题的浏览景观）

**定位**：星球是**长期话题的空间化浏览与导航景观**，不是认知地图、不是语义地图、
不是知识图谱、不是全量数据可视化工具，也不承诺「两个话题在认知空间中有多接近」。

三个必须分开的概念：

| 概念 | 含义 | 上限 |
| --- | --- | --- |
| 总话题数 | 数据库里真实存在的话题 | 无上限 |
| 可见容量 `VISIBLE_CAPACITY` | 星球表面同时承载多少个话题点（`services/planet.py` 与 `frontend/src/planet/browseSession.ts` 必须一致） | 固定值，且 ≤ 融合环 shader 的 uniform 上限 `MAX_TOPICS` |
| 展示窗口 | 此刻窗口里具体是哪几个话题（由浏览会话维护） | 长度 = 可见容量 |

**旋转 = 推动话题流**：用户视觉上在转一个星球，产品逻辑上旋转同时在推进话题流。
位置规则分两种情况，二者的边界就是「用户此刻有没有在拖动」：

| 时刻 | 位置行为 |
| --- | --- |
| 打开星球时 | 在**整个球面**随机铺开（同一会话种子可复现），保持最小角间距、不堆在正南北极 |
| 正在拖动 / 惯性期间 | 转到背面的槽位被换成下一批话题，**新话题在球体背面随机落点**（避开已占用的方向，保持最小角间距） |
| 没有拖动（静止） | 窗口里的话题位置**完全不动**：不漂移、不跳位、不互换 |

数据替换只发生在**球体背面（用户看不见）**，用户看到的是话题从远处自然转进视野。
持续同向旋转会不断遇见新话题；短距离掉头会把刚离开的话题**连同它原来的位置**
一起放回去，因此反向浏览有连续感。

相机本身有**纵向限位**：极角限制在 35°~145°，既避免拖到极点后横向旋转退化
（那里方位角失去意义，用户会觉得「怎么拖都不动」），也保证画面里始终能看出
哪边是「上」。程序性相机移动（聚焦/拉回）遵守同一条限位，不会在补间结束后被
OrbitControls 拽一下。

**没有永久球面坐标**：`nodes.meta.layout`（旧的斐波那契球面位置）保留兼容，但不再
是核心语义；当前展示用的临时布局由 `frontend/src/planet/layoutSlots.ts` 按
「话题 + 浏览会话」的稳定种子确定性生成，用户不需要记住「橡胶实验在东北侧」。

**三层数据接口**（打开星球绝不读全量原文）：

| 层 | 接口 | 内容 |
| --- | --- | --- |
| 第一层 Planet Overview | `GET /api/planet/overview` | 有哪些话题可以展示：id / 标题 / 片段数 / 最近活动 / 摘要预览 / `visual_seed` |
| 第一层续 浏览批次 | `POST /api/planet/browse` | 接下来展示哪一批：确定性浏览序列 + 可前进可后退的游标（不返回原文） |
| 第二层 Topic Detail | `GET /api/graph/topics/{id}` | 选中话题后才读：片段目录（含真实 `message_count`）、实体、知识；**不内联 Message** |
| 第三层 Fragment Raw | `GET /api/fragments/{id}/messages?offset&limit` | 只有真正展开某段历史时才按页取原文 |

Planet 与 List/Search 职责并列：Planet 负责浏览、发现、重新遇见；List/Search 负责
准确寻找、快速进入。搜索命中的话题会被**注入当前展示窗口**，而不是要求它本来就
待在某个固定地点。

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
  → 话题预判 → 明确切换命令（切到 / 回到 + 已知话题名）直接进入话题；
    推测切换只发 TOPIC_SWITCH_SUGGESTED 与「待确认切换」，Anchor 一动不动
  → 记忆写入（当前消息绑定到开放片段）
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

## 12. 状态可见性与能力可达性（第三阶段）

第三阶段的主题不是「显示更多」，而是**让已经存在的能力被完整使用**：用户要知道
「现在发生了什么 / 要不要我做决定 / 什么时候完成 / 失败后能做什么」，而不是看到
内部机制。优先级：**能力完整性 > 状态可理解性 > 操作可达性 > 用户控制边界 >
信息克制 > 视觉精致度**。

### 12.1 事件协议与可见性（唯一事实） <!-- docs-check: ignore —— 分节编号「12.1 事件」会被 HARDCODED 正则当成硬编码事件数，这里不是数量 -->

事件只有三种结论：**保留**（产生 → 传输 → 消费 → 必要呈现）、**内部使用**
（不进前端协议）、**删除**（没有实际作用）。不允许「后端发、前端完全忽略」或
「前端写 case、后端从不发」的半协议 —— `backend/tests/test_event_protocol.py`
是守卫测试：前后端事件集合必须完全相等、每个事件都要有生产者、都必须被前端消费。

| 事件 | 产生方 | 消费方 | 用户可见 | 用途 |
| --- | --- | --- | --- | --- |
| `TURN_START` | `core/turn.py::TurnManager` | `stores/events.ts` | 是（全局轻状态） | 一轮开始；`notify=true` 表示系统驱动的轮 |
| `TURN_END` | 同上（`finally`，恰好一次） | `stores/events.ts` | 是 | 唯一终态 + 最终回答的唯一权威来源 |
| `TURN_QUEUE` | 同上 | `QueueChip.vue` | 是（有排队时） | 排队 / 取消快照 |
| `ASSISTANT` | `core/loop.py` | `stores/events.ts` | 是 | 流式正文 / 工具前中间话 |
| `TOOL_START` | `core/loop.py`（转发 `tool/start`） | 工具卡 | 是 | 工具开始执行（卡片立即进入运行态） |
| `TOOL_END` | 同上（`tool/end`） | 同一张工具卡（按 `call_id`） | 是 | 结果 / 失败原因 / 耗时，原地更新 |
| `NARRATIVE` | `services/app.py::_on_narrative`（由 `AgentLoop` 的叙事 sink 触发） | 叙事抽屉（`NarrativeStage.vue`） | 是 | 模型自主决定的过程说明（announce / progress / warning / result）；**不是**工具事实 |
| `SUBAGENT_STATUS` | `tools/task_manager.py` | 独立任务卡（按 `task_id`） | 是 | 独立任务 queued/running/done/failed |
| `TOOL_CREATE_STATUS` | `tools/dev_tools.py`、`tools/lifecycle.py` | 工具创建卡（按 `group_id`） | 是 | 同一张卡的创建阶段推进 |
| `KNOWLEDGE_CANDIDATE` | `services/memory_lifecycle.py` + turn 收尾 | 对话内确认卡 | 是（回答完成后） | 高影响知识的保存 / 修改 / 忽略 |
| `APPROVAL_REQUIRED` | `tools/approval.py` | `stores/approvals.ts` | 是 | 需要用户决定的操作 |
| `APPROVAL_RESULT` | `tools/approval.py` | `stores/approvals.ts` | 是（卡片状态） | 授权的结局（单次使用） |
| `CAPABILITY` | `services/app.py`（模式变化时） | `stores/events.ts` | 否（正常不显示） | 适配档位（native / text / unsupported） |
| `FALLBACK` | `services/app.py`（进入兼容文本模式那一次） | 一次性轻提示 | 是（仅降级时） | 能力降级说明，不重复、不阻塞 |
| `CREDENTIAL_STATUS` | `api/server.py`（凭据增删改）+ `turn_orchestrator` | `stores/events.ts` | 仅当阻止功能 | 凭据可用性（不含内部标识） |
| `ANCHOR` | `services/app.py::_publish_anchor_event` | `stores/events.ts` | 是（话题行） | 当前位置变化 |
| `TOPIC_SWITCH_SUGGESTED` | `services/turn_orchestrator.py` | `TopicSwitchPrompt.vue` | 是 | 推测切换待确认 |
| `USAGE` | `core/loop.py` | `stores/events.ts` | 否（仅 Developer Mode） | 单轮 token / 迭代 / 工具计数 |
| `WARNING` | `services/app.py::make_warning`、`core/loop.py` | `ConversationView.vue` | 是 | 非致命提示 |
| `ERROR` | `services/app.py::make_error`、`core/loop.py` | `ConversationView.vue` | 是 | 出错了（不承担结束 turn 的职责） |

`MEMORY_INJECT` 在第三阶段被**删除**：用户不需要每次知道「QIO 注入了 4 条记忆」。
需要调试时看 Developer Mode 的单轮详情（`GET /api/traces/{turn_id}` 的 `injection`：
哪些 Fragment / Knowledge / Entity 进入了上下文）。

### 12.1.1 执行叙事（Execution Narrative）

模型可以在工具调用参数里携带可选保留字段 `_qio`（`kind` / `text` / `explanation`）。
它表达的只是"这一步想让用户知道什么"，与工具事实是**两套载荷**：

* adapter 解析时就把 `_qio` 剥离，工具参数、风险判断、沙箱判定、审批摘要都看不到它；
* `AgentLoop` 每批工具调用最多输出一条叙事，经 `AppContext._on_narrative` **先落库再广播**
  （`messages` 行：`role='assistant'`、`content_type='narrative'`），
  批次结束由系统把真实调用结果补写进同一行的 `raw.calls`；
* 前端每个叙事行 = 一个**默认收起**的抽屉头，收纳它之后、下一行叙事之前的调用卡；
  折叠头由系统状态决定显示 `N 次调用` / `N 运行中` / `N 失败` / `N 已取消`；
* 恢复：`GET /api/runtime/state.narratives` 补齐断线期间丢失的叙事，
  历史分页返回叙事行与 `raw`（kind + 系统生成的调用摘要），前端按 `narrative_id` 去重。

审批的 `explanation` 复用同一个信封：`ApprovalService.request` 只在载荷自己没有
explanation 时补上模型文案，`description` / `access` / `capabilities` / `scope` /
`detail` 等系统字段一个都不动。详细设计见
`docs/superpowers/specs/2026-09-22-execution-narrative-design.md`。

### 12.2 用户可见状态的层级

反馈层级从局部到全局，**能局部解决就局部解决**：

| 层级 | 例子 | 表达方式 |
| --- | --- | --- |
| 字段级 | 凭据格式不对 | 字段附近的错误文字 |
| 组件级 | Knowledge 保存失败 | 该卡片内的状态行（不弹全局提示） |
| 任务级 | 工具创建失败 | 工具创建卡内的失败原因 + 可继续修复 |
| 全局 | 后端连接中断 | 页面级提示条 |

约束：成功的反馈要**短暂**（按钮变「已保存」再恢复、卡片状态在原位收敛），失败的
反馈要**持久**（用户必须能处理）；Toast 只用于「跨区域、短期、无需进一步处理」
的信息 —— 本阶段把 Knowledge / Entity 的操作反馈从全局 Toast 收回到卡片内。

普通用户**不会**看到：内部事件名、`capability fingerprint` / `policy hash` /
`sandbox profile`、credential id / keychain identifier、检索得分、模型调用细节、
内部状态机，以及任何形式的 Chain of Thought / hidden reasoning / system prompt。
技术明细统一进 Developer Mode（`/debug`）。

### 12.3 能力可达性（Feature Reachability）

每个用户级能力必须至少属于一种：**直接入口** / **上下文自动出现** /
**明确内部能力** / **开发者能力**。不允许「存在但无法到达」。

| 能力 | 入口 | 自动触发位置 | 用户能否完成 |
| --- | --- | --- | --- |
| 对话 | 输入框 | — | 是 |
| 排队 / 取消 | 输入区（排队徽标 / 停止） | 主 turn 运行中 | 是 |
| 工具执行状态 | 工具卡 | Agent 调用工具 | 是 |
| 审批 | 审批窗口 / 顶部「有 N 项操作等待确认」入口 | 高风险操作 | 是 |
| 独立任务（subagent） | 独立任务卡 | Agent 派发子任务 | 是 |
| 工具创建 | 对话里说明需求（Agent 调 `create_tool`）→ 工具创建卡 | Agent 判断需要新工具 | 是 |
| 话题浏览 / 切换 / 从历史继续 | 星球 + 话题详情 | 明确说「切到 X」 | 是 |
| 记忆浏览（片段摘要 → 原文） | 星球 → 话题详情 → 片段「查看原文」 | — | 是 |
| 知识浏览 / 修正 / 归档 | 星球 → 知识页签 + 对话内高影响候选卡 | 高影响候选在回答完成后出现 | 是 |
| 实体卡浏览 / 修正 | 星球 → 实体页签 | Agent 提取实体卡 | 是 |
| 凭据管理（新增 / 测试 / 暂停 / 删除 / 审计） | 设置 → 凭据 | 没有可用凭据时的提示指向这里 | 是 |
| 联网搜索通道 | 设置 → 模型与联网 | Agent 调 `web_search` | 是 |
| 电脑操控权限 | 设置 → 工具与权限 | 越界操作触发审批 | 是 |
| 记忆封块大小 | 设置 → 对话与记忆 | — | 是（语义为「轮」） |
| 离线维护 | 设置 → 数据与维护 | 后台定时 | 是 |
| Trace / 注入明细 / 原始事件 | Developer Mode（`/debug`） | — | 是（开发者能力） |
| `POST /api/turns/cancel`（取消当前轮） | — | 前端按 `turn_id` 取消 | 明确内部能力 |
| `GET /api/graph/positions` | — | 第二阶段后星球改用 overview / browse | 明确内部能力（兼容保留） |
| `POST /api/credentials/{id}/revoke` | — | 安全侧的吊销动作，保留审计记录 | 明确内部能力（用户入口是「删除」） |

### 12.4 工具创建的产品流程

工具创建不做多步骤向导，而是**当前对话里的一张持续更新的卡**（同一 `group_id`
原地变化，不产生一串卡）：

```
提案 → 正在构建 → 正在测试 → （等待你的确认） → 正在启用 → 已创建
                                ↘ 测试失败 / 创建失败（可继续修复）
```

默认只显示工具名 + 当前状态 + 一行说明；源代码、文件路径、内部工作区只在
「查看详情」里。失败时给用户能理解的结论（不显示 `Error`），并在 QIO 还能继续
修复时提供「继续修复」。审批与创建卡是两件事：卡表达进度，审批表达授权（见 12.5）。

这条流程里的开发工具调用（`create_tool` / `dev_write_file` / `dev_run_tests` /
`dev_submit_tool` 等）**不再各出一张普通工具卡**：它们的进度汇总到同一张创建卡上，
失败会把「创建没有完成：<原因>」写回这张卡（信息不丢，也不产生一串卡）。

### 12.5 审批的表达原则

第一阶段解决审批**安全**（绑定 turn / session、单次使用、过期、摘要、能力指纹）；
第三阶段解决审批**可理解**。审批界面必须回答五个问题：

| 问题 | 字段 |
| --- | --- |
| QIO 想做什么 | `description`（行为句，例如「想修改当前项目中的 3 个文件」） |
| 为什么需要 | `explanation` |
| 会访问什么 | `access`（具体路径 / 命令 / 网址） |
| 会造成什么影响 | `capabilities` 的副作用 + 风险标签 |
| 一次性还是长期 | `scope`（`once` / `long_term`） |

工具名与原始参数仍然保留，但只出现在默认折叠的「高级详情」里；`capability
fingerprint` / 策略哈希同样只在那里。危险动作（自由 shell、结束进程、长期注册
工具）用更明确的措辞表达，但不用夸张警告，也不把批准按钮做成「推荐你点」。

### 12.6 高影响知识候选的流程

```
回答完成 → 高影响候选以低干扰卡片出现在对话流
        → 保存（verify + activate）/ 修改（生成新版本并生效）/ 忽略（记录 ignored）
```

关键约束：

- 候选**只在回答完成之后**出现，绝不打断正在进行的回答（`KNOWLEDGE_CANDIDATE`
  由 turn 收尾发出，前端也在 `TURN_END` 之后才显示）；
- 忽略过一次的内容不再自动重复提示（`knowledge.provenance` 里的 `ignored_at`）；
- Knowledge Panel 仍然是浏览 / 修正 / 归档 / 审核历史的入口，但**不再**是高影响
  候选唯一的确认入口。

### 12.7 独立任务（subagent）的产品语义

用户看到的是「QIO 正在单独处理这项任务」，而不是「内部智能体进程」。独立任务
有独立卡片（开始 / 进行中 / 已完成 / 失败），只显示任务目标、当前状态与最终结果；
内部 reasoning、chain of thought、system prompt、model messages 一律不显示。
任务完成后结果自然回到主 Agent：主 Agent 还在跑就继续回答，已经在等就从
「进行中」变成「已完成」。
