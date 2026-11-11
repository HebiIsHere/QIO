# R18 · 复核补修共享契约（2026-11-11 · 第七轮）

> 本文件由 Lead 冻结，是本轮**唯一共享契约**。先读完再动手；不得单方面修改。
> 承接对象：`2384a0af` 复核确认的两个剩余缺口（R17 交付记录 §7 已列明的已知边界）。
> 上游契约：`docs/topic-unify-20261111-contract.md`（R16 v2）、`docs/topic-unify-r17-contract-20261111.md`（R17）。
> 本轮**不推翻** R16/R17 已冻结的 I01—I14、完整位置状态、两类版本、统一入口、导航命令身份、共享暂存与写入权规则，只补两条生产入口。

## 0. 基线与隔离

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`。
- 开工核实：`git rev-parse fix/topic-review-20261110` = `2384a0af`（origin 同 SHA，本地工作树干净；已核查基线）。
- 集成工作树：`D:\qio-dev\qio-r14`（Lead 独占：合并、集成验收、文档、git 操作）。
- 角色 worktree（均由本契约提交拉出；`backend/.venv` 是共享 junction，**不要删除/重装**）：
  | 角色 | worktree | 分支 | 领域 |
  | --- | --- | --- | --- |
  | A | `D:\qio-dev\r18-a` | `r18/a-interactive-basis` | 互动板提交的受理依据（P1） |
  | B | `D:\qio-dev\r18-b` | `r18/b-answer-recovery` | 普通回答保存失败的暂存+恢复记录（P2） |
  | F | `D:\qio-dev\r18-f` | `r18/f-independent` | 独立验证（只写测试，不改生产实现） |
- 原始证据写 `D:\qio-dev\r18-evidence\<role>\`，**不要写进工作树**；工作树 `git status` 只含自己的改动。
- 禁止：真实/付费模型、联网、真实应用数据库、改历史迁移、新依赖、全量测试、生产构建、触发 CI、push、
  切换分支、merge/rebase、建 worktree、改别人的 worktree/文件、用 sleep 构造并发时序。
- 测试一律 fake/scripted provider + 真实临时 SQLite（`apply_migrations`）；定向测试只用本项目 .venv。

## 1. 本轮目标

1. **P1｜互动板旧轮次覆盖新位置**：互动板提交（`POST /api/interactive/boards/{board_id}/submissions`）
   受理时，复用普通发送/重发的受理快照，把原话题、片段、两类版本（含 position_version）冻结后传给
   TurnManager；旧轮次的位置写入沿用受理时的版本校验。版本失效后仍按原话题/片段完成回答，
   当前位置保持用户后来手动导航到的 B，不广播 A 为当前位置，也不推进 position_version。
2. **P2｜普通回答保存失败后无法重试**：普通正式回答生成成功但保存失败时，按同一 `answer_key`
   **在同一事务内**保存共享暂存正文（`topic_answer_runs.staged_*`）**和一条待恢复记录**
   （`topic_recovery_records`，`failed_stage="answer"`、status=pending），复用现有
   `POST /api/recovery/records/{record_id}/retry-answer` 入口。补存只写一条正式回答、不再调用模型、
   不重复写原问题；重复/并发重试返回同一份正式答案，恢复记录正确收口为 answered。

不可变：主模型语义判断、话题切换/新建工具、已有 answer_key 唯一性与执行权/共享暂存语义、
R17 依据链行为（basis=None 的旧调用方行为不变）。**不放宽/删除既有断言换通过**。

## 2. 冻结接缝

### 2.1 P1：互动板提交的受理依据（A 实现）

生产入口只有一处：`backend/src/agent/api/interactive_store.py` 中 `submit()` 内的
`accept_turn(**kwargs)`（它由 `submission.submit_board(..., turn_acceptor=accept_turn)` 在
succeeded 分支调用一次，就是「受理这一刻」）。

- 在 `accept_turn` 内、`ctx.turns.submit(...)` **之前**一次性读取与
  `api/server.py::_submission_basis` **完全相同**的快照（禁止事后重读）：
  `topic_id`、`fragment_id`、`intent_id`、`intent_version`、`nav_version`、`position_version`。
  实现建议：把该快照提取为共享函数（如 `agent/services/turn_basis.py::submission_basis(ctx)`），
  `server.py::_submission_basis` 与 `interactive_store` 共用同一来源；避免复制两份逻辑。
- `ctx.turns.submit(message, topic_id=受理快照的 topic_id, basis=受理快照)`：
  * 轮次原话题取**受理快照的 topic_id**（不再单独 `ctx.current_topic()` 造成两次读、两套事实）；
  * `intent_id` 仍按现行为不传（互动板提交不是「落实接续」，不得消耗待接续选择）；
  * `basis` 键集沿用 R17 契约 §2.1（`topic_id/fragment_id/intent_id/intent_version/nav_version/position_version`），
    不得新增键、不得改变 TurnManager 签名。
- `turn.board_topic_id` 必须取同一份受理快照的 topic_id（提交—轮绑定与原话题同一事实）。
- 其余行为不变：失败原因、stale/identity 409、replayed、failed 语义、绑定写入顺序、可见范围求差。
- 基线红/修复绿的可观测事实（真实 HTTP + fake provider）：
  * 基线：排队中的互动提交轮 begin 补锚用实时 position_version 做 CAS，把位置从 B 改回 A 并广播 ANCHOR；
    修复后：补锚 CAS 被拒（trace `begin_anchor_superseded`），位置仍 B、无 A 的 ANCHOR 晚到广播、
    `position_version` 保持切换后的值；用户消息与正式回答都在 A（片段 topic=A）。
- 边界：`start_interactive_turn`（意图确认/续作/启动恢复）与 `app.run_turn`/notify 轮本轮**不在**
  强制范围（R17 §7 已知边界继续保留，如需另开任务）；但若 A 发现互动板提交会经该路径产生可复现的
  同一缺陷，先报 Lead 裁决，不擅自扩面。

### 2.2 P2：普通回答保存失败的暂存 + 待恢复记录（B 实现）

入口：`backend/src/agent/services/topic_recovery.py::save_normal_answer` 的失败路径
（当前 `_stash_normal_answer_failure` 只写 run 行暂存，**不建恢复记录**，所以前端/恢复清单里看不到
「重试回答」入口，这是本轮要修的缺口）。

- 保存失败时，在**同一事务**内完成两件事（`storage/db.py::transaction` 已支持外层事务内复用）：
  1. 写共享暂存唯一权威：`topic_answer_runs` 的 `staged_answer/staged_at/staged_model`，`phase=staged`、
     `status='pending'`、释放 owner/租约（保持现有语义）；
  2. upsert 一条 `topic_recovery_records` pending 记录：`kind=RECOVERY_KIND`、`failed_stage="answer"`、
     `turn_id`、`topic_id`、`user_message_id`、`fragment_id`（能可靠解析就用，解析不到如实 NULL）、
     `answer_key`（与暂存同一 answer_key）、`recovery_nav_version`（失败时刻真实导航版本，用于恢复前复核；
     读不到如实 NULL）、`message`（可读、脱敏，说明「普通回答已生成但保存失败，可重试补存」）。
  * 同一 answer_key 已存在 pending 记录时**复用该 record_id 更新**（不得重复建多条）；已 answered 时不复活。
  * 仍保留防覆盖规则：run 行被另一活跃执行者持有 / 已 answered / 执行权表不可用时**不写别人**，
    不建记录（该执行者会完成这份逻辑答案）；暂存失败不掩盖原异常。
- 复用现有「重试回答」入口：不改路由与清单协议；`GET /api/recovery/records` 应出现该记录
  （`state_class=answer_pending`、`actions=[retry_answer]`）；`POST .../retry-answer` 命中共享暂存后
  只补存、不调模型、不重新装配上下文、不写原用户消息，成功后记录 answered（answer_id=正式消息 id）、
  run 行 answered 且暂存清空。
- 幂等/并发：同一 answer_key 的重复保存失败不产生多条 pending 记录；重复/并发 `retry-answer`
  返回同一 answer_id（非执行者 202 `recovery_in_progress` 或 answered 后直接同 id），只写一条正式消息；
  既有执行权/旧执行者 superseded 语义不变。
- 不改 schema/迁移；不改 `save_normal_answer` 冻结签名（返回值键集不变）。
- 边界：外部模型 exactly-once 不声称；被另一活跃执行者持有时不建记录（其会完成答案）。

## 3. 文件所有权（同一文件只有一个编辑者）

| 角色 | 独占文件 |
| --- | --- |
| A | `backend/src/agent/api/interactive_store.py`、`backend/src/agent/api/server.py`（仅 `_submission_basis` 委托）、新建 `backend/src/agent/services/turn_basis.py`、新建 `backend/tests/test_r18_a_interactive_basis.py` |
| B | `backend/src/agent/services/topic_recovery.py`、新建 `backend/tests/test_r18_b_answer_recovery.py` |
| F | 新建 `backend/tests/test_r18_f_independent.py`（只写测试；不改生产实现、不改既有测试） |
| Lead | `docs/*`、`backend/tests/test_r18_lead_integration.py`、集成/最终验证/git；不代写实现 |

需要别人文件的改动 → 先发消息给 Lead 裁决，不得自己改。既有测试若因冻结语义必须最小适配，
只改自己测试文件或先报 Lead（不得改别人的测试）。

## 4. 验证与汇报

- 后端定向：`cd <worktree>\backend; $env:PYTHONPATH='<worktree>\backend\src'; .\.venv\Scripts\python.exe -m pytest tests\<file> -q -p no:cacheprovider`
- 先写最小失败用例（基线红证据：命令 + 失败输出），再实现到绿；测试日志写 `D:\qio-dev\r18-evidence\<role>\`。
- 只跑定向文件 + 直接受影响的相邻用例；**不跑全量 pytest、不跑前端、不触发 CI**。
- 汇报格式（send_message 给 Lead）：1) 角色/状态；2) 分支 + commit SHA 列表；3) 改动文件精确清单；
  4) 逐项行为变化 + 最小复现（测试名/命令）；5) 定向命令、通过/失败数、exit code、证据路径；
  6) 与契约偏差（必须显式）；7) 未完成/已知限制/集成接缝。

## 5. 验收矩阵

| 编号 | 场景 | 断言 |
| --- | --- | --- |
| A1 | 互动板提交排队 → 手动切 B → 旧轮放行（真实 HTTP） | 问题/回答归属 A；权威位置仍 B；无 A 为当前位置的迟到 ANCHOR；position_version 不推进 |
| A2 | 互动板提交无导航干扰 | 正常在受理话题完成回答，补锚/收尾行为不变 |
| A3 | 相邻回归 | 既有互动提交/可见范围/失败语义/幂等重放用例保持通过 |
| B1 | 普通回答保存失败（HTTP 注入一次写失败） | 暂存正文 + pending 恢复记录同事务出现；无正式回答半截 |
| B2 | 故障解除后从既有入口重试 | 只补存一条正式回答；模型调用次数不增；原问题不重复；记录 answered 且 answer_id 正确 |
| B3 | 重复/并发重试 | 同一 answer_id；不新增消息；不新增模型调用；执行权语义不变 |
| B4 | 相邻回归 | R17-D answer identity / 既有恢复入口用例保持通过 |
| F | 独立验证 | 以上边界独立复现（基线红→集成绿）+ 接缝评审 |
