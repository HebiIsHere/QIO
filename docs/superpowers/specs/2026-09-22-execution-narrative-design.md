# Execution Narrative（执行叙事层）设计

> 状态：已确认，待实现。日期：2026-09-22。
> 相关规范：`2026-08-09-qio-frontend-design.md`（视觉语言 v2/v3）、`2026-09-21-tool-state-recovery.md`（工具事实恢复）。

## 1. 背景与目标

QIO 的工具过程提示目前完全由系统模板生成：一句全局状态「正在使用工具」，加上系统按 `present_call`
拼出来的工具卡标题。用户看不出这一轮在确认什么、为什么先读这三个文件，也无法区分
「连续低价值的读取」和「真正的阶段变化」。

本轮引入 **Execution Narrative（执行叙事层）**，让模型自己决定：

* 需不需要说明（`silent` 是默认选项，不是异常）；
* 什么时候说（执行前、阶段推进、遇到异常、阶段结果）；
* 说什么（面向用户的一句意图，而不是复述工具名与参数）。

同时审批窗口获得模型生成的 `explanation`：为什么需要执行、准备做什么、可能影响什么。

**边界（不可协商）**：QIO 只控制"如何表达"，不控制"是否允许执行"。
工具、参数、目标、命令、风险等级、审批按钮、UI 结构与最终权限仍完全由系统决定。

## 2. 现状链路（实现基线）

| 环节 | 现状 | 关键文件 |
| --- | --- | --- |
| Turn 生命周期 | `TurnManager` 单飞 + FIFO；`TURN_START/TURN_END` 唯一收口 | `backend/src/agent/core/turn.py` |
| 单轮流程 | `begin → build_context → execute_loop → persist → post_turn` | `backend/src/agent/services/turn_orchestrator.py` |
| Agent 循环 | `PLANNING → TOOL_EXEC → OBSERVING`；把 registry 管线事件转成 `TOOL_START/TOOL_END` | `backend/src/agent/core/loop.py` |
| 工具管线 | `start → pre-execute → execute → post-execute → result → end` | `backend/src/agent/tools/registry.py` |
| 审批 | `ApprovalService.request()` 发 `APPROVAL_REQUIRED`；`payload` 由 `describe_tool_call` 生成人话字段 | `backend/src/agent/tools/approval.py`、`approval_present.py` |
| 事件总线 | 有界 fan-out + `Last-Event-ID` 补发；事件分「可合并 / 关键 / 控制」三类且分类完备 | `backend/src/agent/api/events.py`、`backend/src/agent/api/bus.py` |
| 状态恢复 | `/api/runtime/state` 返回 turn 队列、pending 审批、任务、工具执行事实 | `backend/src/agent/api/server.py`、`backend/src/agent/services/app.py` |
| 前端消息流 | 按「用户消息开新轮」分组；工具卡按 `call_id` 原地更新 | `frontend/src/stores/session.ts`、`MessageStream.vue`、`MessageItem.vue` |

## 3. 设计原则

1. **一套结构**：过程说明与审批 explanation 共用同一个模型侧信封、同一条落库记录、同一个 SSE 事件族。
2. **两层载荷**：叙事（模型文案）与工具事实（系统生成）永不合并；叙事事件只带系统提供的溯源字段。
3. **默认安静**：不写叙事 = 完全安静；一次工具批次最多一条叙事，天然的阶段合并。
4. **默认收纳**：前端每个叙事行默认收起它覆盖的工具调用，只有用户主动展开才显示明细。
5. **异常不隐藏**：收起态必须在头部如实显示运行中 / 失败 / 已取消，收纳不等于掩盖。
6. **可恢复**：叙事先落库再广播；重连、刷新、分页都按同一条 `narrative_id` 去重。

## 4. 数据契约

### 4.1 模型侧信封 `_qio`

模型在工具调用参数中可选携带保留字段：

```json
{
  "path": "backend/src/agent/core/narrative.py",
  "_qio": {
    "kind": "announce | progress | warning | result",
    "text": "面向用户的一句话意图说明（≤120 字）",
    "explanation": "若这次调用可能需要用户确认：为什么需要/准备做什么/可能影响什么（≤200 字）"
  }
}
```

* `kind` 与 `text` 可省略；`explanation` 可单独存在。
* 未登记字段一律丢弃；类型不符按缺失处理；空文本按 `silent` 处理。
* `_qio` 在 adapter 层就被剥离，**不会**出现在工具参数、风险判断、沙箱判定或审批摘要里。

### 4.2 后端结构 `agent/core/narrative.py`

```python
NARRATIVE_KEY = "_qio"
NARRATIVE_KINDS = ("announce", "progress", "warning", "result")

@dataclass(frozen=True)
class Narrative:
    kind: str
    text: str
    explanation: str = ""
    silent: bool = False   # 只带 explanation、没有 text 时也为 True

def parse_narrative(raw: object) -> Narrative | None: ...
def narrative_event_payload(...) -> dict: ...
```

`parse_narrative` 是唯一入口：白名单取键、长度截断、`trace/redact.redact_text()` 清洗，
任何异常都返回 `None`（= 安静），绝不抛出到执行路径。

### 4.3 持久化

复用 `messages` 表（**不新增迁移**）：

| 列 | 值 |
| --- | --- |
| `role` | `assistant`（满足 CHECK；且不会像 `system` 那样在消息流里开新轮） |
| `content_type` | `narrative` |
| `content` | 模型文案（`text`） |
| `turn_id` | 本轮 turn |
| `raw` | `{"narrative": {"kind": ..., "tool": ..., "call_id": ..., "silent": bool}, "calls": [...]}` |

`raw.calls` 是**系统生成的调用摘要**（B 方案），在批次结束时由系统补写一次：

```json
{"call_id": "call_ab12", "tool": "fs_read", "title": "读取 approval.py",
 "status": "success", "error": null, "duration_ms": 210}
```

模型文本不进入 `raw.calls`，也无法改写它。`narrative_id` 就是这条 `messages` 行的 id。

叙事行是展示记录，不是记忆内容：`FragmentManager.content_tokens` 统计容量时排除
`content_type = 'narrative'`，避免展示文本加速片段封存。

### 4.4 SSE 事件

新增 `EventType.NARRATIVE`，归入 `CRITICAL_EVENTS`（不可静默丢弃；背压溢出时走既有 RESYNC 路径）。

```json
{
  "type": "NARRATIVE",
  "data": {
    "narrative_id": "msg_5f3a1c2b",
    "turn_id": "turn_9c02",
    "kind": "announce",
    "text": "我先确认审批请求从后端到前端的完整路径。",
    "tool": "grep_search",
    "call_id": "call_ab12",
    "call_ids": ["call_ab12", "call_cd34"],
    "created_at": "2026-09-22T09:41:09.123456+00:00"
  }
}
```

`tool` / `call_id` / `call_ids` / `turn_id` / `narrative_id` 全部由系统填写。

### 4.5 运行时快照

`GET /api/runtime/state` 增加：

```json
{"narratives": [{"narrative_id": "...", "turn_id": "...", "kind": "...",
                 "text": "...", "calls": [...], "created_at": "..."}]}
```

范围与 `tools` 一致：只返回**主 Turn** 的叙事（内部循环 `subagent:*` 不返回）。

## 5. 事件如何产生、传输、恢复、展示

### 产生

1. `NativeAdapter` / `TextAdapter` 解析工具调用时调用共享的 `split_narrative_arguments()`，
   把 `_qio` 剥离成 `ToolCall.narrative`；
2. `AgentLoop._dispatch_tool_calls()` 在取消检查之后、真正执行之前，按调用顺序取
   **整个批次的第一条有效叙事**（每批最多一条）；
3. 调用 `narrative_sink(turn_id, narrative, call, call_ids)`（由 `TurnOrchestrator` 注入
   `AppContext._on_narrative`）。子 agent / 维护循环不注入 sink，也就不会写入主对话。

### 传输与落库

`AppContext._on_narrative()`：

1. 解析本轮绑定（topic / fragment），拿不到绑定就只发事件、不落库；
2. `memory.append_message(role="assistant", content_type="narrative", raw={...})`（先落库）；
3. `bus.publish(NARRATIVE)`，`narrative_id` = 刚落库的消息 id。

批次结束时，`AgentLoop` 再调一次 `narrative_settler(narrative_id, results)`；
`AppContext` 用它把 `raw.calls` 补写成真实终态（成功/失败/取消、耗时、错误摘要）。
补写失败只记日志，不影响工具执行与 turn 终态。

### 恢复

* 页面刷新 / 翻页：历史分页（`/api/session/context`、`/api/session/messages`）返回叙事行，
  前端按 `raw.narrative.kind` 与 `raw.calls` 还原叙事行与抽屉内容；
* 断线重连：`Last-Event-ID` 补发 + 客户端事件 id 去重；超出补发窗口时 RESYNC，
  由 `runtime/state.narratives` 补齐；
* 去重三层：SSE `event.id`、store 内 `narrative_id`、同 turn 内「同 kind 同 text」。

### 展示

* 叙事行是一条**内联行**（小标记 + 一句话），不是卡片、不是气泡；
* 历史里 `role=assistant, content_type=narrative` 映射为前端虚拟 role `narrative`，
  不参与 turn 分组边界、不影响最终回答的合并逻辑；
* 全局状态条移除机械的「正在使用工具」；`等待你确认`、`正在处理独立任务` 等系统状态保留。

## 6. 审批 explanation 接入

* `ToolRegistry.execute()` 在管线前后设置/复位 `ContextVar`「当前调用的 Narrative」
  （并行调用各自 task 隔离）；
* `ApprovalService.request()` 构造请求前：
  * `payload["explanation"]` 已非空（如工具创建流程的提案说明）→ 保持不动；
  * 否则从 `ContextVar` 取模型 `explanation`，清洗截断后写入 `payload["explanation"]`；
* 其余一切不变：`approval_id`、`turn_id`、`session_id`、`expires_at`、`request_digest`、
  单次使用、超时、拒绝、取消、`respond` 校验。

覆盖范围：`tool_execution`、`computer`（fs/cmd 工具内部发起）、`create_topic`。
预算 `continue`、维护类审批在工具执行之外 → 无 explanation，走现状。

前端 `ApprovalModal.vue`：模型说明单独成段并标注「QIO 的说明」，放在系统 intent 之后、
事实区之前；系统字段（description / access / capabilities / scope / detail / 高级详情）
全部保留；`whyNeeded` 不再重复输出 explanation；explanation 缺失时与现状完全一致。

## 7. 事实不可覆盖（不变量）

1. 叙事与工具事实是两套载荷；`TOOL_START/TOOL_END` 载荷不因叙事发生任何变化。
2. 只读白名单键（`kind` / `text` / `explanation`）；其它键、类型错误、超长文本一律丢弃或截断。
3. `_qio` 在 adapter 层剥离，工具 `run(**arguments)`、风险判断、沙箱判定看不到它。
4. 审批事实由 `describe_tool_call` / `describe_computer_action` 生成，模型只能追加 `explanation`。
5. 叙事不参与任何分支判断；`silent` 不改变审批是否弹出、不改变工具是否执行。
6. 叙事文本经 `redact_text()` 清洗后才落库与广播。
7. 抽屉折叠头必须显示运行中 / 失败 / 已取消，异常不得因为收纳而消失。

## 8. 前端 UI 契约（抽屉）

* **归属**：一行叙事收纳「它之后、下一行叙事之前」的工具调用；`call_ids` 是权威分组依据，
  位置关系作为兜底。连续无声的调用自然并入上一组；轮次开头的无声调用才单独成卡。
* **默认收起**：所有叙事抽屉初始 `data-open="false"`；运行中也**不自动展开**。
  展开只由用户点击触发，状态只在本地（不落库、不跨会话）。
* **折叠头状态**（由系统根据真实调用状态算出，模型无法影响）：
  `2 次调用 · 0.6s` / `1 运行中`（带呼吸点，链接色）/ `1 失败`（危险色）/ `1 已取消`。
  任一批次内出现失败或取消，折叠头必须显示对应标记。
* **展开内容**：实时轮次显示系统工具卡（可再点开看参数与结果）；
  历史轮次显示 `raw.calls` 生成的「调用摘要 · 系统生成」列表（工具名 · 状态 · 耗时 · 失败原因）。
* **动效**：`grid-template-rows 0fr → 1fr`（`--mo-2-in`），与既有工具卡展开一致；
  减少动画时保留短淡入，不做位移。
* **静默**：无叙事行的调用不产生任何文案；若它们落在某一组内，就只是那一组抽屉里的记录。
* **无障碍**：抽屉头是 `<button>`，`aria-expanded` 与视觉状态同步；键盘可展开/收起。

## 9. 兼容性、重复与恢复风险

| 风险 | 处理 |
| --- | --- |
| 模型不填 `_qio` | 完全等价于现状（仅少了机械提示），工具卡照常 |
| 老客户端收到新事件 | 未注册的 SSE 事件类型被忽略，不影响既有链路 |
| 历史里的旧叙事行缺少 `kind` / `calls` | `kind` 兜底 `progress`；`calls` 缺失时不显示抽屉体，也不伪造记录 |
| 并行工具 | 叙事在批次前按调用顺序输出，顺序稳定；explanation 用 ContextVar 按调用隔离 |
| 重复事件 | 三层去重（event id / narrative_id / 同 kind 同 text） |
| 超出补发窗口 | `runtime/state.narratives` 按 `created_at` 合并插入，不重复、不置顶 |
| 叙事落在失败/取消的轮次 | 已输出叙事留在历史（如实反映"说明过、没做完"），工具与审批仍按原语义收口 |
| prompt 体积 | 每个工具 schema 增加一个 `_qio` 属性（短描述），token 预算由 `specs()` 统一计算 |

## 10. 测试矩阵

后端：

1. `silent`：无 `_qio` → 无事件、无落库，工具照常执行；
2. 执行前提示：`NARRATIVE → TOOL_START → TOOL_END` 顺序，`narrative_id` 等于落库 id；
3. 批量合并：3 个连续调用 → 只 1 条叙事、3 组工具事件；
4. 自定义审批说明：`_qio.explanation` 出现在审批载荷，其它事实字段逐字段不变；
5. explanation 缺失 fallback：审批仍正常发起、批准 / 拒绝 / 超时不变；
6. 文案不能覆盖事实：恶意 `_qio`（含 `risk` / `capabilities` / `description` / `arguments` / `tool`）
   全部被丢弃；`_qio` 不出现在工具参数；事件里的 tool / call_id 来自系统；
7. `raw.calls` 补写：批次结束后记录真实终态与耗时，失败原因可读；
8. 恢复与去重：runtime state 与历史返回同一 `narrative_id`；重复事件只落一条；
9. 事件分类完备：`NARRATIVE` 已归关键类（既有守卫测试继续通过）；
10. 容量：叙事行不计入 `content_tokens`。

前端：

1. store：同 id 重复事件、历史 + 重放 → 只保留一条；
2. 抽屉：默认收起、点击展开/收起、`aria-expanded` 同步；
3. 折叠头状态：`2 次调用` / `1 运行中` / `1 失败` 文案与色彩；
4. 历史抽屉：`raw.calls` 渲染调用摘要，缺 `calls` 时不显示箭头；
5. 移除机械提示：消息流不再出现「正在使用工具」；
6. 审批：显示「QIO 的说明」且系统事实不丢失；缺 explanation 时与现状一致。

## 11. 非目标

* 不修改工具执行语义、风险与能力判定、沙箱策略、审批权限与单次使用约束；
* 不新增数据库迁移；
* 不为工具卡新增历史持久化（工具卡仍是实时视图，历史靠 `raw.calls` 摘要）；
* 不改动 turn 状态机、事件背压策略与星球/设置页。

## 12. 交付物与文档同步

* 代码：后端 `narrative` 模块 + adapter / loop / registry / approval / app / server 接线；
  前端 store / 组件 / 类型接线；
* 文档：本 spec、实现计划 `docs/superpowers/plans/2026-09-22-execution-narrative.md`、
  `docs/architecture.md`（事件与恢复契约）、`docs/status.md`（进度与已知限制）；
* 验证：`uv run --frozen pytest`、`npx vue-tsc --noEmit`、`npm test`、
  `python scripts/check_docs.py`，以及 prompt / 工具定义变更后的 `agent.eval.run` 基线对比。
