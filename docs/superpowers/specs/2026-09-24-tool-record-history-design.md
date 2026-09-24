# 工具调用历史（参数与输出落库）设计

**日期：** 2026-09-24
**状态：** 设计已确认，待实现
**范围：** 后端存储 / 工具审计写入链 / 历史读取接口 / 设置项 / 前端工具卡。

## 一、背景

工具卡只活在实时事件流里：`messages` 表只存用户消息、助手回答和过程说明行，
工具调用本身不落库。于是刷新、重开应用、切换话题之后，那一轮"它调了哪些工具、
各自成功还是失败、为什么失败"在对话里就看不到了（真实事故：一次工具创建流程
连续失败 5 次，事后只剩一条过程说明行和一句空回答）。

现状还有一处更细的缺口：实时卡片的展开内容来自 `TOOL_END.content_preview`，
后端只给 200 字（`tools/registry.py::_finish`）。也就是说**无论实时还是历史，
用户都看不到工具的完整输出**。

## 二、目标与非目标

**目标**

1. 每次工具调用都落库：打码后的参数、打码后的输出全文、状态、一行错误、耗时。
2. 历史里能看到那一轮的工具卡，展开后能看到参数与输出全文。
3. 实时卡片与历史卡片是同一张卡、同一套数据来源，展开后看到的内容一致。
4. 两个用户可改的设置：是否保存输出全文，以及输出保留多少天。

**非目标（明确不做）**

- 不进上下文、不进片段摘要、不进检索索引；模型**不能**检索到这些记录（用户已选「只给人看」）。
- 星球页的历史浏览（片段原文接口）不显示工具卡。
- 不引入「整条记录自动删除」的规则。
- 不把工具卡写进 `messages` 表（理由见第三节）。

## 三、数据模型

迁移 20（只追加，不改历史迁移）新建一张专用表：

```sql
CREATE TABLE IF NOT EXISTS tool_records (
    id             TEXT PRIMARY KEY,          -- new_id("tr")
    turn_id        TEXT NOT NULL,
    topic_id       TEXT,                      -- 写入时的锚点话题（仅作信息，归属判定用 turn_id）
    call_id        TEXT NOT NULL DEFAULT '',  -- 模型给出的调用 id（与实时卡片同一身份）
    seq            INTEGER NOT NULL DEFAULT 0,
    tool_name      TEXT NOT NULL,
    arguments      TEXT NOT NULL DEFAULT '{}',-- 打码后的 JSON
    output         TEXT NOT NULL DEFAULT '',  -- 打码后的输出（上限见第四节）
    status         TEXT NOT NULL DEFAULT 'success',  -- success / failed / cancelled
    error          TEXT NOT NULL DEFAULT '',
    duration_ms    INTEGER,
    truncated      INTEGER NOT NULL DEFAULT 0,
    output_missing INTEGER NOT NULL DEFAULT 0,       -- 1 = 库里没有输出
    missing_reason TEXT NOT NULL DEFAULT '',         -- '' | 'setting' | 'retention'
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_records_turn ON tool_records(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_records_created ON tool_records(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_records_call
    ON tool_records(turn_id, call_id) WHERE call_id <> '';
```

标题（中文展示名）不落库：读取时用 `tool_label(tool_name)` 现算，文案改进后历史卡片跟着改。

**为什么新开一张表，而不是复用现有两处**

- `messages`：它被焦点块（取开头 2 条 + 结尾 3 条）、片段封存与容量统计、摘要输入、
  索引、轮次计数五处消费。把工具输出写进去，每一处都要新增「跳过工具行」，漏一处
  就会把工具输出喂进上下文或摘要 —— 这正是早先刻意不走这条路的原因。
- `tool_calls`（轨迹审计表）：按设计只存截断摘要，而且追踪可以被用户关掉
  （`trace.enabled=false`）；工具历史跟着追踪一起消失是不可接受的。
  本设计不改动它，它继续按原样服务轨迹分析。

**代价（如实记录）**：每次调用会写两行（审计一行、历史一行）。两行来自同一个写入点，
不新增第二条链路。

## 四、写入路径

**触发点**：`tool/end` 监听器里调用 `self.tool_trace(payload)`（计划阶段修正，实现按此）。
`tool/result` 看不到「取消」标记 —— 取消与失败必须分开说（状态以 `core/tool_state.py`
的取值为准）—— 而完整输出只有 `tool/result` 拿得到；所以 `tool/result` 只把 `call` 与
`result` **暂存**在 `_pending_tool_io[call_id]`，等 `tool/end` 拿到权威终态后再落库，
耗时也在那时由 `_on_pipeline_start` 记下的起始时刻算出。

目前只有主轮（`turn_orchestrator`）与系统通知轮（`app._execute_notify_turn`）注入这个
回调，子任务与维护循环不注入 —— 所以**子任务内部的工具调用天然不入表**，
与「子任务内部的工具不进主对话」的既有边界一致。

**载荷扩展**（同一个回调，不新增第二条链路）：

```python
self.tool_trace({
    "tool_name": ..., "arguments": ..., "ok": ..., "result": ..., "error": ...,
    "call_id": call.id,
    "turn_id": self.turn_id,
    "seq": <本轮内序号，派发时分配>,
    "duration_ms": <now - _tool_started_at[call_id]>,
    "status": "success" | "failed" | "cancelled",
})
```

**落库**（`AppContext._record_tool_call` 内，写入逻辑集中在 `storage/tool_records.py`）

1. 原有 `tool_calls` 审计行照旧写（列与语义不变）。
2. 再写一行 `tool_records`：
   - 参数：`json.dumps(redact_any(arguments))`，上限 40_000 字，超出截断并置 `truncated=1`。
   - 输出：`redact_text(result)`；`tools.record_outputs=0` 时改写为
     `output='' / output_missing=1 / missing_reason='setting'`。
   - 输出同样 40_000 字上限（与 `web_fetch` 的现有上限对齐）；截断时在末尾追加
     「…（已截断，原文 N 字）」，`truncated=1`（前端据此显示「已截断」徽标，
     不需要为此再存原始长度）。
3. 用 `INSERT OR IGNORE`（依赖 `(turn_id, call_id)` 唯一索引）保证重复事件不会写出重复卡片；
   命中重复时返回已有行的 id（幂等，不产生第二张卡）。
4. 回调返回记录 id（写库失败返回 `None`）：loop 把它并进 `_tool_facts[call_id]`，
   `TOOL_END` 事件带上 `record_id`（为 `null` 时前端退回只显示预览）。
   `_on_pipeline_end` 写 `_tool_facts` 时必须**合并**已有字段，不能整条覆盖。
5. 任何异常只记日志（`logger.warning`），**绝不改变工具本身的结果**，也不让 turn 失败。

**打码**：唯一入口是 `trace/redact.py`（`redact_any` / `redact_text`），与仓库
「密钥原文不得入库」的既有硬规矩一致。

## 五、读取路径

**历史接口附带预览**

`GET /api/session/context` 与 `GET /api/session/messages` 的返回体新增
`"tool_records": [...]`。归属规则：

1. 取这一页消息里出现过的 `turn_id` 集合（消息自带 `turn_id` 字段）。
2. 查询这些 turn 的全部记录，按 `(created_at, seq)` 升序。

用 `turn_id` 归属，而不是按时间窗口或按话题过滤：工具换话题时整轮跟着走，话题字段
在那一瞬间并不可靠；按 `turn_id` 归属也不会因为分页边界把某次调用丢掉。跨页边界最多
重复一次（某一轮的 user 与助手消息分属两页时），前端按记录 id 去重。

每条预览的字段：`id, turn_id, call_id, seq, tool_name, title, status, error,
duration_ms, truncated, output_missing, missing_reason, output_chars, preview,
created_at`。`preview` 是输出的前 400 字；`output_chars` 是**库里实际存下的**
字符数（用于判断除预览外是否还有更多内容），不含被截断掉的部分。

**按 id 取全文**

```
GET /api/tool-records/{record_id}
→ { id, turn_id, call_id, tool_name, title, status, error, duration_ms,
    truncated, output_missing, missing_reason, created_at,
    arguments: {...},   # 解析后的 JSON；解析失败时回退为字符串
    output: "…" }       # 完整输出；output_missing=1 时为空串
```

记录不存在返回 404；输出被保留期清掉时记录仍在，返回 200 且
`output_missing=1 / missing_reason='retention'`。

**实时卡片也走同一条路**

`TOOL_END` 事件新增 `record_id`（写入时生成，经 loop 的 `_tool_facts` 回填）。
前端实时卡片在需要看全文时用同一个接口取，从此实时与历史两种卡片的数据来源完全一致。

## 六、设置

| 键 | 类型 | 默认 | 范围 | 含义 |
| --- | --- | --- | --- | --- |
| `tools.record_outputs` | 布尔（"0"/"1"） | `1` | — | 是否保存输出全文 |
| `tools.output_retention_days` | 整数 | `90` | `0`–`3650` | 输出保留天数；`0` = 永久 |

**接口**：新增 `GET /api/settings/tools` 与 `PUT /api/settings/tools`（与
`/api/settings/computer` 同一套写法）。非数字的 `output_retention_days` 返回 400；
数字超出范围时按边界收敛（与 `search.max_fetch_chars` 的既有做法一致）。
`PUT` 的响应在设置对象之外多一个 `purged` 字段：保存后立即执行一次清理
（把天数调小要马上生效），`purged` 就是这次清掉输出全文的记录条数，供界面提示。

**清理语义（关键口径）**：过期只清空**输出全文**，记录本身（参数、状态、错误、耗时）
继续保留：

```sql
UPDATE tool_records
   SET output = '', output_missing = 1, missing_reason = 'retention'
 WHERE output_missing = 0 AND created_at < :cutoff;
```

这样三个月后你仍然查得到「当时哪个工具失败了、为什么失败」，只是那份完整内容不在了。

**清理时机**：应用启动时一次（`AppContext` 构造末尾，失败只记日志、不阻塞启动）、
后台维护每次（`maintenance.interval_hours`，默认 24 小时）一次、保存设置后一次。

## 七、前端

**历史加载**：`loadHistory` / 更早一页加载时，把 `tool_records` 映射成 `role: "tool"`
的流条目（`callId`、`toolName`、标题、`toolStatus`、`toolError`、`toolDurationMs`、
历史标记），与消息一起按 `created_at` 合并排序，并按记录 id 去重。顺序与当时一致：
你的话 → 过程说明行 → 工具卡 → 助手回答。

**卡片行为**（实时与历史同一张卡）：

1. 折叠时：中文标题 + 状态徽标（成功 / 失败 / 已取消）+ 耗时 + 失败原因一行。
2. 展开时显示两段：「参数」（格式化 JSON）与「输出」。
3. 库里只有预览时，展开触发按 id 取全文，期间显示「正在读取…」；失败显示原因与「重试」。
4. `output_missing=1` 时按原因显示：「输出未保存（设置里关闭了保存输出全文）」或
   「输出已按保留设置清理（保留 N 天）」。
5. `truncated=1` 时标「已截断（原始 N 字）」。

**设置界面**：设置 → 工具与权限，在「电脑操控」下方新增一节「工具调用历史」：
一个开关 + 一个数字输入（天数），文案写清 0 = 永久，以及「只影响你自己能否回看，
模型不会读到这些内容」。

## 八、测试与验收

**后端（TDD，先红后绿）**

- 迁移：老库（18 / 19 版本）升级后新表存在、旧数据一行不丢。
- 打码：输出里出现 `sk-` 形态的密钥 → 库里是 `***redacted***`；参数里的密钥字段同样被替换。
- 截断：超过 4 万字的输出被截断且 `truncated=1`；刚好等于上限时不截断。
- 开关：`tools.record_outputs=0` 时记录仍写入，`output_missing=1 / missing_reason='setting'`。
- 保留期：写入旧时间戳的记录，清理后输出为空、参数与状态与错误仍在、
  `missing_reason='retention'`；设为 `0` 时清理不碰任何行；保存设置立即触发清理。
- 去重：同一 `(turn_id, call_id)` 写两次只有一行。
- 归属与顺序：历史接口按 `turn_id` 带回记录，顺序为 `(created_at, seq)` 升序；
  分页边界处前端去重后不丢不重。
- 只给人看：工具记录不出现在上下文组装、片段摘要输入、检索索引里（用一条守卫测试断言）。
- 子任务：子任务循环的调用不写 `tool_records`。
- 写库失败（注入异常）不影响工具结果，也不让 turn 失败。

**前端（vitest）**

- 历史映射：`tool_records` → 工具卡，插入位置正确，跨页重复 id 去重。
- 懒取三态：加载中 / 成功显示参数与全文 / 失败显示原因与重试。
- 已清理与未保存两种文案；截断标记。

**真实链路**

- 隔离实例截图：历史里带长输出的工具卡展开后不横向溢出、窄窗口可读（沿用
  `scripts/ui-catalog/tool-fail-line.mjs` 的断言方式）。
- 全量后端测试、评测基线对比、前端类型检查与全部测试、文档一致性检查。

## 九、涉及文件（供实现计划参考）

- 后端：`storage/schema.py`（迁移 20）、`storage/tool_records.py`（新增：写入 / 查询 /
  清理集中一处）、`core/loop.py`（载荷与 record_id 回填）、`services/app.py`（接线、
  读取、设置读取、启动清理）、`services/maintenance.py`（清理时机）、`api/server.py`
  （历史附带、按 id 取全文、设置读写）。
- 前端：`stores/session.ts`（历史映射与去重）、`stores/events.ts`（TOOL_END 收 record_id）、
  `components/MessageItem.vue`（参数段、懒取三态）、`views/SettingsView.vue`（新设置节）、
  `services/api.ts`（三个新调用）。
- 文档：`docs/status.md`（本轮小节 + 已知限制）。

## 十、已知限制

- 打码是规则匹配，规则之外的密钥形态仍可能落库；关掉「保存输出全文」是唯一彻底的做法。
- 库会随时间增长；默认 90 天保留期只清输出，参数与错误永远保留（体积很小）。
- 本功能上线前的历史调用没有记录，不做回填。
- 子任务内部的工具调用仍只有轨迹可查，不进对话历史。
