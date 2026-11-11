# R19 · 复核补修共享契约（2026-11-11 · 第八轮）

> 本文件由 Lead 冻结，是本轮**唯一共享契约**。先读完再动手；不得单方面修改。
> 承接对象：`9de91bb`（R18 交付）复核确认的两个剩余缺口。
> 上游契约：R16 v2、`docs/topic-unify-r17-contract-20261111.md`、`docs/topic-unify-r18-contract-20261111.md`。
> 本轮不推翻既有 I01—I14、完整位置状态、两类版本、统一入口、依据链、共享暂存与写入权规则，只补两条生产行为。

## 0. 基线与隔离

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`；基线 `9de91bb`（= origin，工作树干净）。
- 集成工作树 `D:\qio-dev\qio-r14`（Lead 独占：合并、集成验收、文档、git 操作）。
- 角色 worktree（由本契约提交拉出；`backend/.venv` 与 `frontend/node_modules` 是共享 junction，不要删除/重装）：
  | 角色 | worktree | 分支 | 领域 |
  | --- | --- | --- | --- |
  | A | `D:\qio-dev\r19-a` | `r19/a-default-topic` | P1：首次互动板提交的默认话题归属（后端） |
  | B | `D:\qio-dev\r19-b` | `r19/b-recovery-refresh` | P2：保存失败后的恢复清单可见性（后端事件 + 前端刷新） |
- 原始证据写 `D:\qio-dev\r19-evidence\<role>\`，不要写进工作树。
- 禁止：真实/付费模型、联网、真实应用数据库、改历史迁移、新依赖、全量测试、生产构建、触发 CI、push、
  merge/rebase、建 worktree、改别人的 worktree/文件、用 sleep 构造并发时序。
- 测试：后端 fake provider + 临时 SQLite（apply_migrations）；前端 vitest 单文件 + mock fetch；
  不跑全量。前端只改/跑自己相关 spec。

## 1. 本轮目标

1. **P1｜首次互动板提交归错话题**：互动板提交受理时若还没有当前位置，按现有规则解析默认话题，
   **不改写当前位置**，并把该话题与受理时的位置版本一起固定给本轮；后续执行沿用受理归属与版本校验。
   验收：无当前位置时提交互动板，暂停模型准备，手动切到 B，再放行。提问和回答都写入默认话题，
   当前位置保持 B，旧轮不推进位置版本，也不发布把当前位置改回默认话题的事件。
2. **P2｜当前页面看不到回答重试入口**：普通回答保存失败时，在暂存正文与待恢复记录**提交成功后**，
   通知前端刷新现有「未完成事项」清单（复用既有 `loadRecoveryInbox` 读取逻辑）；即使已有请求在途，
   也要确保新记录最终可见。用户点击「重试回答」继续走现有补存入口。验收：已打开且清单为空的会话
   注入一次保存失败，无需刷新/重连即可看到「重试回答」；解除故障后点击重试只补存一条正式回答，
   模型调用次数不增加，恢复记录正确结束。

不可变：全部既有语义、既有测试断言与事件协议已有字段；**不放宽/删除既有断言换通过**。

## 2. 冻结接缝

### 2.1 P1：互动板提交的默认话题（A 实现，纯后端）

入口：`backend/src/agent/api/interactive_store.py` 的 `submit()` 内 `accept_turn`（R18 已在此冻结
`submission_basis(ctx)` 并传给 `ctx.turns.submit(..., basis=basis)`）。

- 现有缺陷：无当前位置时 `basis["topic_id"]` 为 None，`turn_topic` 为 None；轮次在 `begin` 时
  才走 `ctx.initial_topic or submit_basis_topic_id or app.current_topic()` 兜底 —— 若排队/准备期间
  用户已切到 B，就会把本轮归属解析成 B。
- 修复：`accept_turn` 在受理这一刻，如果快照没有 topic_id，就**按现有规则解析默认话题**
  （即 `AppContext.current_topic()` 的既有兜底语义：无 active Anchor 时取默认话题，必要时创建
  默认话题节点；**只解析，不 enter_topic、不写 Anchor/cursor、不推进任何版本**），把该话题写回
  同一份 `basis["topic_id"]`，并作为 `ctx.turns.submit(message, topic_id, basis=basis)` 的 topic 与
  `turn.board_topic_id`。解析失败时如实告警并按旧路径降级（不得伪造话题）。
- 位置版本：沿用 R18 的同一份受理快照（nav_version/position_version），不额外读取。
- 允许改动的文件：`backend/src/agent/api/interactive_store.py`；如确需共享小工具，可在
  `backend/src/agent/services/turn_basis.py` 增加只读解析函数（不得改变 `submission_basis` 返回键集/语义）。
- 边界：只修互动板提交入口；`/api/turns`、重发、`start_interactive_turn` 等保持现行为（另开任务）。

### 2.2 P2：保存失败后的恢复清单可见性（B 实现，后端 + 前端）

**(a) 后端通知事件（新事件类型，冻结）**
- `backend/src/agent/api/events.py` 新增 `RECOVERY_INBOX_CHANGED = "RECOVERY_INBOX_CHANGED"`。
  payload 键（冻结，全部可缺省但至少 record_id/turn_id）：`record_id`、`turn_id`、`topic_id`、
  `kind`（= `topic_answer_recovery`）、`failed_stage`（= `"answer"`）、`action`（= `"retry_answer"`）。
- 触发点：`backend/src/agent/services/turn_orchestrator.py::persist` 中 `save_normal_answer` 的失败路径。
  `save_normal_answer` 内部先在**已提交事务**里写共享暂存 + pending 恢复记录，再把原异常抛出；
  orchestrator 捕获后按 `answer_key_for(turn_id, user_message_id)` 查**已提交**的 pending 记录
  （建议在 `services/topic_recovery.py` 加只读 helper `find_pending_by_answer_key`），查到才
  `await app.bus.publish(make_event(EventType.RECOVERY_INBOX_CHANGED, payload))`，随后**原异常照抛**。
  查不到（暂存/记录没写成功、被另一活跃执行者持有等）→ **不发事件**。
  通知本身任何异常都不得掩盖原异常、不得改变轮次结局。
- 不改 `save_normal_answer` 冻结签名/返回键集，不改 schema/迁移，不改恢复接口与清单协议。

**(b) 前端刷新（复用既有逻辑 + 在途合并）**
- `frontend/src/services/events.ts`：`EVENT_TYPES` 增加 `"RECOVERY_INBOX_CHANGED"`。
- `frontend/src/stores/events.ts`：新增 case → `session.requestRecoveryInboxRefresh()`（见下）。
- `frontend/src/stores/session.ts`：
  * 新增 `requestRecoveryInboxRefresh()`：空闲时调用既有 `loadRecoveryInbox()`；已有收件箱请求在途时
    只登记「完成后补拉一轮」（同一实例），**不得并发第二个快照请求**；
  * `loadRecoveryInbox()` 的 finally（请求成功取得结果并解锁时）若存在补拉标记，清标记后再跑一轮。
  * **注意**：普通并发调用 `loadRecoveryInbox()` 的既有单飞语义/既有测试必须保持不变（第二个普通调用
    仍然直接返回、不额外发请求）；补拉只由通知入口登记。
- 点击「重试回答」继续走既有 `session.retryTopicAnswer(recordId)` → `POST /api/recovery/records/{id}/retry-answer`，
  不改协议、不加乐观成功。
- 允许改动：`backend/src/agent/api/events.py`、`backend/src/agent/services/turn_orchestrator.py`、
  `backend/src/agent/services/topic_recovery.py`（仅新增 helper）、`frontend/src/services/events.ts`、
  `frontend/src/stores/events.ts`、`frontend/src/stores/session.ts`。
- 边界：旧后端不已知新事件 → 前端不刷新（保持现状，不伪造）；事件不是「清单权威」，权威仍是
  `GET /api/recovery/records`；SSE 重放/重复事件必须幂等（重复只多拉一次清单，不产生坏状态）。

## 3. 文件所有权（同一文件只有一个编辑者）

| 角色 | 独占文件 |
| --- | --- |
| A | `backend/src/agent/api/interactive_store.py`；必要时 `backend/src/agent/services/turn_basis.py`（只加只读解析）；新建 `backend/tests/test_r19_a_default_topic.py` |
| B | `backend/src/agent/api/events.py`、`backend/src/agent/services/turn_orchestrator.py`、`backend/src/agent/services/topic_recovery.py`（仅 helper）、`frontend/src/services/events.ts`、`frontend/src/stores/events.ts`、`frontend/src/stores/session.ts`；新建 `backend/tests/test_r19_b_recovery_notify.py`、`frontend/src/stores/__tests__/r19-b-recovery-refresh.test.ts` |
| Lead | `docs/*`、`backend/tests/test_r19_lead_integration.py`、集成/最终验证/git；不代写实现 |

需要别人文件的改动 → 先发消息给 Lead 裁决；既有测试只允许最小适配且需先报 Lead。

## 4. 验证与汇报

- 后端定向：`cd <worktree>\backend; $env:PYTHONPATH='<worktree>\backend\src'; & .\.venv\Scripts\python.exe -m pytest tests\<file> -o addopts= -q -p no:cacheprovider`
  （仓库默认 `addopts="-q"`，证据命令用 `-o addopts=` 覆盖，确保汇总行可见。）
- 前端定向：`cd <worktree>\frontend; npx vitest run src/stores/__tests__/<file>`
- 先写失败用例（基线红证据：命令 + 失败输出，写 `D:\qio-dev\r19-evidence\<role>\`），再实现到绿；
  只跑自己的定向文件 + 直接相邻用例；不跑全量、不跑 build、不触发 CI。
- 汇报：角色/状态；分支 + commit SHA；改动文件清单；逐项行为变化 + 最小复现；定向命令/通过数/exit code/
  证据路径；与契约偏差（必须显式）；未完成/已知限制/集成接缝。

## 5. 验收矩阵

| 编号 | 场景 | 断言 |
| --- | --- | --- |
| A1 | 无当前位置提交互动板 → 暂停准备 → 切 B → 放行（真实 HTTP） | 问题/回答归属默认话题；权威位置 B；position_version 不推进；无「切回默认话题」迟到 ANCHOR |
| A2 | 无当前位置提交且无导航干扰 | 正常写入默认话题并补锚（既有行为） |
| A3 | 相邻回归 | 既有互动提交/轮次依据/普通发送用例保持通过 |
| B1 | 保存失败（注入一次写失败）且记录提交成功 | 后端在**事务提交后**发出 RECOVERY_INBOX_CHANGED（record_id 对应真实 pending 记录）；无半截答案 |
| B2 | 前端收到事件 | 复用 loadRecoveryInbox 刷新；事件到达时已有请求在途 → 完成后补拉一轮，新记录最终可见 |
| B3 | 解除故障后既有入口重试 | 只补存一条正式回答；模型调用不增；原问题不重复；记录 answered；重复重试同一 answer_id |
| B4 | 相邻回归 | 既有收件箱单飞/实例竞态测试、R18 保存失败/恢复用例保持通过 |
