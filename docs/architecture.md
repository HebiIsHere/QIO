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

### 2.1 sidecar 进程所有权（2026-10-03 实测定稿）

发布产物里的 `qio-backend.exe` 是 **PyInstaller onefile**：它是 **launcher + child 两个进程**，
**真正监听端口、提供服务的那个是 child**；两者 `ExecutablePath` 完全相同，**按名字或路径都分不出父子**。
（`backend/.venv/Scripts/python.exe` 在开发态有同样的形状：它是 uv 的 trampoline，工具代码跑在它起的子进程里。）

所有权规则（唯一事实源，实测记录见 `docs/process-lifecycle-verification.md`）：

- 壳在 `spawn()` 返回后**立刻**把 sidecar 放进一个 `KILL_ON_JOB_CLOSE` 的 Job Object，句柄由壳持有；
  **句柄随壳消失 = 系统连带终止 job 内所有进程**，所以壳正常退出、被强杀、被安装器结束都干净。
- **时序是这条修复的一部分**：child 比 launcher 晚约 1~2 秒才创建（launcher 要先解包），
  assign 必须紧跟 `spawn()`；等 child 出现再 assign，child 会**逃逸**。这条有单测反证钉住。
- job 建不出来 / assign 失败时，退出路径按 **pid** 结束整棵树（`taskkill /PID <pid> /T /F`）。
  **绝不允许按进程名杀** —— 会误伤开发实例、测试实例与其它安装实例。
- 运行中的 `qio-backend.exe` **可以改名，但不能删除、不能原地覆盖**（WinError 5 / EACCES）。
  所以「端口已经关了」**不等于**「文件没被锁」，更新与卸载都要按这个事实设计。

### 2.2 工具专用环境（ToolEnv）用哪个解释器

冻结后 `sys.executable` 就是 `qio-backend.exe`，**它不能当 Python 解释器用**（拿它跑 `-m venv` 只会再起一个后端）。
所以：非冻结态仍用后端自己的解释器；**冻结态按 `QIO_PYTHON` → Windows `py -0p` → `PATH` 找**，
并要求 major.minor 与后端一致；找不到时给出**可行动的明确失败**且不起任何子进程。
这意味着**安装版要为用户工具准备依赖环境，前提是机器上有一个匹配的 Python** —— 这是已知的产品级限制。

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

记忆检索的排序**只有一个权威入口**：`agent/services/retrieval.py::Retriever.search`。
`agent/selector/` 只负责产出候选与底层相关度（规则分项作为 `signals` 带出，**不参与候选阶段排序、也不据此截断**）；
权重集中在 `agent/services/params.py::RETRIEVAL`（`RetrievalConfig` 的默认值直接取自它，不再各写一份）。
话题判定同理只有一个入口：`agent/services/affinity.py::classify`，由 `services/predict.py` 提供 owner 证据。

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

强制力的真实边界（2026-09-12 首测，2026-10-02 更新）：

**已经真实的（内核强制，2026-10-02 起）**——受限子进程创建之后由父进程施加，不改 worker 协议：

- **Job Object**：本次调用的进程内存 1 GiB、活动进程 32、`KILL_ON_JOB_CLOSE`。
  实测：超限分配 → `MemoryError`；连拉 5 个进程 → 4 个 `WinError 1816 配额不足`；
  关句柄 → 该 Job 里的 worker 与孙进程全部消失。
- **低完整性（MIC）降级 —— 默认关闭**（`QIO_TOOL_LOW_INTEGRITY=1` 打开）：写边界随完整性级别生效，
  机制实测有效（对照组可写 `user_files` 与 QIO 数据目录，降级后两者 `WRITE-DENIED`）。
  但它在**普通完整性**的 Windows runner 上会让工具连自己的一次性目录都写不进去（标签落地不可核实），
  所以默认不开；打开时先读回核实标签，核实不了就 fail-safe 跳过。
  **默认生效的内核强制只有 Job Object 这一层。**

**仍然只靠「声明」的**——按策略拒绝高风险声明、剥离环境变量、限制超时与输出：

- **读没有隔离**：知道路径就能读用户目录与 QIO 数据目录（有用例钉住这条边界）。
- **网络没有隔离**：低完整性不影响出网。
- **AppContainer 没接上**：本机 `CreateAppContainerProfile` 返回 `0x80070005`（需提权）。
  受限令牌（`CreateRestrictedToken`）可创建也可启动进程，但令牌仍是同一用户 SID，**本身不构成边界**。
- Job 只收容「指派之后创建」的后代（真实执行流程没有这个窗口，已用时序用例锁住）。

所以：**能力声明仍然是给用户看的契约，不是内核级保证**；谎报能力的工具不会被完全拦住。
完整的威胁模型、能力矩阵与分阶段方案见 `docs/security/tool-execution-isolation.md`。
要真正强制读与网络，仍需容器（或等价的命名空间/ACL）隔离。

工具的能力由 `ToolExecutionPolicy` 显式声明，而不是由「用了哪个凭据」推断：

| 级别 | 含义 |
| --- | --- |
| PURE（0） | AI 新生成工具的**默认**权限：无凭据、无外网、无任意 shell、只能访问受控 scratch/temp、不访问用户文件、严格超时与资源限制 |
| RESTRICTED（1） | 工具显式申请能力（联网 / 某个文件路径 / 某类凭据 / 某个端点），必须在审批界面展示给人看 |
| TRUSTED（2） | 需要较强本机能力，必须用户显式批准，**不允许自动授予** |

执行与隔离：

- Docker 是否可用 = 命令行在 PATH 里**且**守护进程应答（`docker version` 报出服务端版本）。只装了 Docker Desktop 没启动时，按「没有容器隔离」处理——不能只凭「装了 docker」就选容器，否则每次工具调用都以 `docker exit code 125` 失败
- Docker 模式：默认 `--network none`；只有策略允许时才对内网开放；限制挂载、环境变量、CPU、内存、进程数、超时与输出
- 受限子进程模式：可用，但对高风险能力**拒绝执行**，不做静默降级；要执行必须走用户明确批准的 TRUSTED
- `auto` 在「容器起不来」（`docker run` 退出码 125 / docker 进程起不来）时改走受限子进程：那时工具代码一行都没执行过，回退不等于重跑。退出码来自容器里的工具自己、或容器超时不回退（重跑等于在没有隔离的情况下又执行一遍）。显式 `executor="docker"` 不回退，直接报「守护进程没有应答」
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
| 凭据管理（新增 / 更换 API Key / 重新验证 / 设为默认 / 暂停 / 删除 / 审计） | 设置 → 凭据 | 没有可用凭据时的提示指向这里 | 是 |
| 联网搜索通道 | 设置 → 模型与联网 | Agent 调 `web_search` | 是 |
| 电脑操控权限 | 设置 → 工具与权限 | 越界操作触发审批 | 是 |
| 记忆封块大小 | 设置 → 对话与记忆 | — | 是（语义为「轮」） |
| 离线维护 | 设置 → 数据与维护 | 后台定时 | 是 |
| Trace / 注入明细 / 原始事件 | Developer Mode（`/debug`） | — | 是（开发者能力） |
| `POST /api/turns/cancel`（取消当前轮） | — | 前端按 `turn_id` 取消 | 明确内部能力 |
| `GET /api/graph/positions` | — | 第二阶段后星球改用 overview / browse | 明确内部能力（兼容保留） |
| `POST /api/credentials/{id}/revoke` | — | 安全侧的吊销动作，保留审计记录 | 明确内部能力（用户入口是「删除」） |
| `POST /api/credentials/{id}/verify` | — | 「保存后没通过验证」的重试入口 | 明确内部能力（入口是卡片「更多操作 → 重新验证」） |
| `POST /api/credentials/{id}/default` | 设置 → 凭据「设为默认」 | 第一条验证可用的主对话凭据自动成为默认 | 是（只改排序，不绕过停用/撤销/预算/用途/验证） |
| `GET /api/credentials/providers` | 设置 → 凭据（厂商下拉） | 表单打开时拉取 | 是（厂商预设的唯一来源） |
| `POST /api/credentials/verify-draft` | 设置 → 凭据「更换 API Key」 | 换钥前先验证新 Key | 是（只请求给定的服务地址，不落库） |

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

## 13. 可靠性与一致性契约（2026-10-09 定稿）

这一节固定本轮新增的**结构性边界**：它们不是实现细节，而是后续改动必须遵守的接口。

### 13.1 实例归属与存活判据

- 权威来源：`backend/src/agent/storage/instance_registry.py`（`InstanceRegistry`）。
  实例身份在 `AppContext.__init__` 生成一次，HTTP 层、事件、台账共用同一个 id。
- 归属写在 `instances` 与 `record_owners`（迁移 **29**，见下面 13.8 的号段说明）；台账、待确认事项、派生任务各自带
  `owner_instance_id`。
- 存活是**四态**：显式退出 = 死；心跳新鲜 = 活；心跳过期且 pid 不存在 = 死；其余 = 未知。
  **未知不得被当成死**：恢复只能在确认已退出时动作，否则继续保留并在后续维护里重判。
- 「只允许一个可写实例」不是本轮的假设——多实例共享数据目录是合法形态，
  所以恢复必须按归属判断，而不是「启动时无条件中断所有在跑记录」。

### 13.2 受理提交点：先持久化、再派发

- `TurnManager.submit()` 的提交点顺序固定：**可靠持久化 → 进入可执行队列 → 返回受理成功**。
  持久化失败必须抛 `TurnAcceptError`，受理接口返回 503，不产生内存里的假接受项。
- 提交之后、派发之前退出的窗口由恢复清单覆盖：记录可见、可操作，但**不自动执行**用户消息。
- 重发是一个事务：老记录的恢复状态、新任务行、后继关联一起提交；
  只有已提交的新任务才能被派发。孤立抢占（recovered_at 有值、recovered_by 为空）
  必须能被列出并修复，不能永久隐藏。

### 13.3 知识版本链

- 身份：`knowledge_id`（版本行）/ `chain_id`（链）/ `version`（链内序号）/ `supersedes_id`。
  当前有效 = 该链内**唯一** `status='active'`。
- 唯一写入入口：`backend/src/agent/knowledge/lifecycle.py` 的 `revise_atomic` /
  `deactivate_atomic`；版本核对、撤销旧行、激活新行、写关联在同一事务里，
  失败整体回滚；版本不符返回 409（`VersionConflict`），绝不悄悄新增第二个当前版本。
- `chain_id` 为空的历史行按 supersedes 派生链纳入同一条链——迁移只加列不回填，
  派生兜底是**必需**的，不是兼容装饰。
- 范围归属由 `backend/src/agent/knowledge/scope.py` 判定：`node_ids` 为空 → 用户全局节点；
  `topic_id` 只是兼容字段。无法可靠判定归属的旧条目保持 active 但标 `unresolved`。

### 13.4 派生任务与后台生命周期

- 派生任务（summary / knowledge / entities）各自独立登记、认领、完成、失败与重试，
  带 `owner_instance_id` 与 `claim_generation`；完成 / 失败 / 释放都要带
  `expected_generation`，不匹配即丢弃迟到结果。
- `claim_due` 每次调用顺带核对一次超期 running 任务：恢复不是「只在启动跑一次」。
- 后台记忆任务统一登记在 `backend/src/agent/services/background.py`，
  `AppContext.aclose()` 先 `background.shutdown()`（拒绝新建 → 有界等待 → 取消 → 确认结束），
  再停维护 / turn / 独立任务，最后关适配器；数据库由 lifespan 最后关。

### 13.5 设置写入

- 唯一入口：`backend/src/agent/services/settings_service.py`。
  语义固定为「先全量校验 → 单事务提交 → 再应用运行时」；校验失败 400、写库失败 500，
  两种情况数据库与运行时都保持整套旧值。会触发清理的设置（保留期限）只在提交成功后清理。
- 端点只做「收 body → 交给服务 → 返回该 section 的权威现值」，不再各自边校验边写。

### 13.6 前端运行状态恢复

- 唯一入口：`frontend/src/stores/restore.ts::restoreRuntimeState(reason)`。
  首次连接、页面刷新、普通重连、`RESYNC` 都走它；单飞 + 代次 + 同步期间缓冲事件 +
  快照后按序补事件；旧代次与旧实例结果丢弃。
- 同步期间的缓冲有明确上限，溢出时登记「需要重新同步」并补拉权威快照，
  不允许「静默丢事件却宣称已同步」。

### 13.7 模型调用与用量记账

- 每次**实际请求**（含适配器内部重试的每一次响应）在发送前核对累计用量：
  `backend/src/agent/credentials/policy.py` 的 `remaining_budget`（`None` = 无上限）与
  `ensure_budget_available`（耗尽抛 `BudgetExhausted`）。耗尽后不再发新请求，
  只给简短真实原因，不静默改配置。
- 入账唯一入口：`backend/src/agent/credentials/usage.py::record_request_usage(...)`；
  失败但有已知用量照记，没有用量就标 `incomplete`，不凭空造数。
- 取消语义：外层取消必须取消**并等待**内层模型请求清理，再继续传播取消；
  迟到结果不得进入已结束任务的历史。

### 13.8 迁移号段纪律（2026-10-09 实测定稿）

- `agent/storage/migrate.py::apply_migrations` 的语义是「`target <= 已记录版本` 就**整条跳过**」。
  因此迁移号不只代表顺序，还是**跨分支的命名空间**：同基线并行开发的多个修复分支
  如果各自从「下一个空号」追加迁移，就会撞号，而撞号的后果不是冲突报错，而是
  **后合入的那条被静默整段跳过** —— 表不存在、列不存在，直到运行期才以
  `no such table` / `no such column` 的形式炸出来。
- 本次实测到的形态：基线 `main`（`6e073e9`）停在 25，另外三个同基线修复分支已经用掉
  26 / 27 / 28；本轮最初也用 26，结果被那些分支碰过的存量库把这条迁移整段跳过，
  后端在 `AppContext.__init__` 抛 `sqlite3.OperationalError: no such table: instances`。
- 规则：**新迁移取「所有并行分支已知最大号 + 1」**，不要取「当前 main 的下一个号」。
  存量库上的半截迁移仍然由 `migrate.py` 的窄口径自愈（只认 duplicate column / already exists）
  处理。回归见 `backend/tests/test_rm_lead_migration_discipline.py`。
