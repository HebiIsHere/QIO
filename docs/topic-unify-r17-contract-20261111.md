# R17 · 复核补修共享契约（2026-11-11 · 第六轮）

> 本文件由 Lead 冻结，是本轮**唯一共享契约**。先读完再动手；不得单方面修改。
> 承接对象：`929e8d7c` 复核发现的问题 + 双连接测试等待顺序。上游契约是
> `docs/topic-unify-20261111-contract.md`（R16 v2，下文简称「R16 契约」），本轮**不推翻**其中
> I01—I14、完整位置状态、两类版本、统一入口、导航命令身份、共享暂存与写入权规则，只做补修。

## 0. 基线与隔离

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`。
- 开工核实：`origin/fix/topic-review-20261110` = 本地 = `929e8d7c12cf5073b32bf832b391df1ea22c2e1a`，
  工作树干净（fetch 后 ahead/behind 0/0）。本契约所在提交是补修轮的**脚手架基线**。
- 集成工作树：`D:\qio-dev\qio-r14`（Lead 独占：合并、集成验收、文档、git push）。
- 角色 worktree（均由脚手架提交拉出；`backend/.venv` 与 `frontend/node_modules` 是共享 junction，
  **不要删除/重装/提交**）：
  | 角色 | worktree | 分支 | 领域 |
  | --- | --- | --- | --- |
  | A | `D:\qio-dev\r17-a` | `r17/a-turn-basis` | 轮次受理依据（P1-1） |
  | B | `D:\qio-dev\r17-b` | `r17/b-nav-cancel` | 取消目标身份 + 单事务导航后端（P1-2/P2-3） |
  | C | `D:\qio-dev\r17-c` | `r17/c-frontend` | 前端迟到取消响应 + 一次点击一次导航（P1-2/P2-3） |
  | D | `D:\qio-dev\r17-d` | `r17/d-answer-identity` | 普通回答接入统一答案身份（P2-4） |
  | E | `D:\qio-dev\r17-e` | `r17/e-concurrency-test` | 双连接测试等待顺序修正 |
  | F | `D:\qio-dev\r17-f` | `r17/f-independent` | 独立验证（只写测试，不改生产实现） |
- 原始证据写 `D:\qio-dev\r17-evidence\<role>\`，**不要写进工作树**。工作树 `git status` 只含自己的改动。
- 禁止：真实/付费模型、联网、真实应用数据库、改历史迁移、新依赖、全量测试、生产构建、触发 CI、push、
  切换分支、merge/rebase、建 worktree、改别人的 worktree/文件、用 sleep 构造并发时序。
- 同一时刻最多 3 个 pytest 进程；前端只跑相关 spec；模型一律 fake/scripted provider，数据库一律临时 SQLite。

## 1. 本轮目标与不可变决定

补修 `929e8d7c` 复核的四项问题 + 修正双连接测试等待顺序：

1. **P1-1**：旧排队轮次不得覆盖用户的新导航（轮次受理依据冻结；旧轮次只按已受理归属完成回答）。
2. **P1-2**：迟到的取消响应不得清空较新的接续选择（取消响应整份接受；取消命令明确关联目标选择）。
3. **P2-3**：「返回当前开放片段 / 进入话题最新位置」是一次原子导航命令（前端不再先发独立取消）。
4. **P2-4**：普通正式回答接入统一 answer_key 身份、唯一性与共享暂存/恢复收口。
5. **测试**：`test_two_connections_same_answer_key_single_executor` 的等待顺序与异常释放。

不可变：主模型对话题意图的判断、现有切换/新建工具、确认/收起/拒绝后回答/草稿行为、共享暂存与旧执行者
写入权校验、I01—I14 全部保留。**不放宽/删除既有断言换通过**；旧测试只做与冻结接口一致的最小适配。

## 2. 冻结接口（跨角色接缝；实现细节可自定，签名与语义不得擅改）

### 2.1 轮次受理依据（A 实现；B 在 `/api/turns` 调用）

```python
# core/turn.py
TurnManager.submit(message, topic_id=None, *, notify=False, intent_id=None,
                   turn_id=None, request_id=None, basis: dict | None = None) -> TurnContext
TurnManager.reserve(message, topic_id=None, *, notify=False, intent_id=None,
                    prepare_id=None, request_id=None, basis: dict | None = None) -> TurnContext
```
- `basis` 键（缺失/None 表示旧调用方，行为不变）：`topic_id`、`fragment_id`、`intent_id`、
  `intent_version`、`nav_version`、`position_version`（全部按受理那一刻的真实值，不得事后重读）。
- `TurnContext` 新增字段（全部有默认值，旧测试替身用 `getattr` 兼容）：
  `submit_basis_topic_id`、`submit_basis_fragment_id`、`submit_basis_intent_id`、
  `submit_basis_intent_version`、`submit_basis_nav_version`、`submit_basis_position_version`，
  以及本轮自己写位置后的 CAS 链 `position_basis_version: int | None`、
  `position_basis_nav_version: int | None`、`position_basis_superseded: bool = False`。
- **P1-1 行为规则**：轮次开始/结束的任何内部位置写入都以 `position_basis_version` 作
  `expected_position_version`（绝不临时读 `current_position_version()` 洗白旧动作）；写入成功后只依据
  **本回合自己命令结果**更新链。若链与当前权威位置不一致（用户后来的真实导航），旧轮次的轮首补锚、
  内部片段落位、轮末推进全部跳过（trace 记录，零位置副作用），回答仍按受理归属写回原话题/片段。
  本回合自己已获批准的工具导航/正常自动分段/边界分段按其真实因果更新链并推进位置。`basis` 为 None 的
  旧调用方不强制（保持旧行为），但生产入口 `/api/turns` 必须传。

### 2.2 取消接续命令的目标身份（B 实现；C 消费）

```
POST /api/anchor/continue/cancel
{request_id, base_nav_version, pending_intent_id: str, pending_intent_version: int | null}
```
- `pending_intent_id` **必填**（缺失 → 400 `continuation_cancel_target_required`）；
  `pending_intent_version` 可选，给了就必须与当前 registered 意图版本一致。
- `PositionTransition` 新增冻结字段 `pending_intent_version: int | None = None`；
  `param_digest` **仅在非 None 时**把该字段计入（保证旧库里已存摘要不变，I05 不误报）。
- 统一入口在事务内先校验目标再接续取消（零副作用拒绝）：
  * 目标意图不存在 / 不是当前 registered / 版本不一致 →
    409 `continuation_cancel_target_mismatch`（`pending_intent_id` 不存在或版本不一致）或既有
    `continuation_request_inactive`（意图已被 replace/cancel/resolve）；两者都零位置写入、零广播。
  * 目标一致 → 现行为：registered→cancelled、nav_version 不推进、position_version 有变化 +1。
- 服务端取消 A 的请求不得隐式取消当时刚好存在的 B；B 的位置/pending/版本保持不变。

### 2.3 前端取消响应（C 实现）

```ts
export interface CancelContinuationResult {
  ok: boolean;              // commandSucceeded && stateAdopted（兼容旧调用方的布尔语义）
  commandSucceeded: boolean; // 服务端命令是否成功（含 replay）
  stateAdopted: boolean;     // 响应所带 position 是否被 acceptPositionState 整份接受
  code: string | null;
  error: string | null;
}
session.cancelPendingContinuation(): Promise<CancelContinuationResult>
```
- 捕获 `pendingContinuation.intentId + intentVersion` 作为取消目标随请求发送；
  `pendingContinuation` 本地载荷新增 `intentVersion: number | null`（`setPendingContinuation` 的入参同步扩展）。
- **所有取消响应（含旧/重放/冲突）先经过 `acceptPositionState(position, "command")`**；只有整份接受
  （accepted=true）后才更新位置/历史标记/接续提示（由接受入口统一写，**禁止**在校验前直接
  `pendingContinuation = null`）。响应无 position 的旧后端兼容路径除外（只允许旧后端，文档标注）。
- 拒绝（stale/identity/同版本冲突）时不得部分修改任何状态；触发既有 `_requestPositionStateRestore()`
  权威恢复；返回 `stateAdopted=false` 且 `ok=false`，本地提示保留直到权威状态到达。
- 正常取消（响应 position 被接受且 pending 为 null）→ 提示清理、`ok=true`、`stateAdopted=true`。
- 用户单独点击取消接续继续走本入口；只有「返回当前开放片段/进入话题最新位置」不再先取消。

### 2.4 一次点击一次导航（C 前端 + B 后端验证）

- `ManualNavigationInput` **删除 `cancelPending` 字段**；`_runManualNavigation` 删除独立取消步骤。
  「进入话题最新位置」= `enter_topic`（fragment=null），「返回当前开放片段」= `continue_from_history`
  + 开放片段（后端 `open_fragment`）；两者都是**一个** `POST /api/anchor`。
- 后端保证：同一事务内完成命令身份/参数校验 → 依据 CAS → 落位 → 清理不再适用的 registered 接续
  （既有 `real_navigation_decision` 规则）；失败整份回滚（目标不存在 404/400、依据过期 409、
  事务中途异常回滚），原位置与原接续一起保留；成功只广播一次；同 request_id 重放零落位/零广播。
- 用户单独点击取消接续仍走 2.2 入口。

### 2.5 普通正式回答保存（D 实现；A 在 `turn_orchestrator.persist` 调用）

```python
# services/topic_recovery.py（脚手架已含占位实现，D 替换为真实实现；签名冻结）
def save_normal_answer(scope, *, topic_id: str, turn_id: str, user_message_id: str | None,
                       content: str, content_type: str = "text", model: str | None = None,
                       raw: dict | None = None, fragment_id: str | None = None) -> dict:
    """返回 {"answer_id": str, "created": bool, "existing": bool,
              "closed_record_ids": list[str], "answer_key": str}"""
```
- `answer_key = answer_key_for(turn_id, user_message_id=user_message_id)`（与恢复入口同一函数；普通轮必须
  有原用户消息；缺失时按既有 `operation_id` 退化规则并记录，不静默造假）。
- 正式保存事务内：先按 answer_key 查既有正式答案（含下文旧库可靠归属规则）→ 已存在则复用原
  `answer_id`（不新增第二条），并把同 answer_key 的 pending 恢复记录与执行行**同一事务**收口；
  不存在则按普通回答原有语义写入一条 assistant 消息（片段由轮次绑定解析、content_type/model/raw/原轮次
  全部保留，且带 answer_key），同一事务收口恢复记录/执行行（对应 I12）。并发唯一性靠既有
  `messages.answer_key` 部分唯一索引，冲突时回查既有一份。
- **保存失败时**（异常路径）：先把正文写入共享暂存唯一权威（`topic_answer_runs` 的
  `staged_answer/staged_at/staged_model`，phase=staged）再抛出原异常；任意同 answer_key 的恢复记录
  之后只补存、不再调用模型（保留既有共享暂存与写入权校验，旧执行者迟到写仍是 superseded）。
- **旧库 answer_key 为空的历史答案**：只有能可靠确定归属时才关联（同一 turn、同一原用户消息、该轮
  只有一条正式 assistant 候选且无其它 answer_key 答案等必要条件；不满足就当作不存在、不猜）。
  可靠命中时在同一事务内补写 answer_key。**默认不新增迁移**；若确需迁移，D 追加迁移并同步
  `storage/migrate.py`/`storage/schema.py`（这两个文件本轮 D 独占）。
- A 的 `persist` 只允许改这一处调用（其余行为、formal_answer 前提、互动轮无正式回复语义不变）。

### 2.6 双连接测试等待顺序（E）

- 共享 `entered = threading.Event()`：**任一** adapter 首次进入 `complete` 即 set；主线程只等它，
  不再按 A、B 顺序各等 20s。
- 执行者阻塞释放放在 `try/finally`：断言失败/异常也必须 `release.set()`，不留挂起执行者。
- 分别控制 A 获胜、B 获胜两种顺序（确定性：先让一个执行者真正进入后再启动另一方），
  保留原验收标准（非执行者 202 recovery_in_progress、只生成一次、一份正式答案、记录同一 answer_id）。
- 只改 `backend/tests/test_r16_e_concurrency.py`；若修正后仍失败，保留证据报 Lead 按实际证据定位。

## 3. 文件所有权（同一文件只有一个编辑者）

| 角色 | 独占文件 |
| --- | --- |
| A | `backend/src/agent/core/turn.py`、`backend/src/agent/services/turn_orchestrator.py`、新建 `backend/tests/test_r17_a_turn_basis.py` |
| B | `backend/src/agent/services/position_state.py`、`backend/src/agent/api/server.py`、`backend/src/agent/services/navigation.py`（仅在必要时）、新建 `backend/tests/test_r17_b_cancel_atomic.py`；既有 anchor/cancel 导航测试的最小适配（`test_anchor_cancel_api.py`、`test_r15_b_r3_anchor_replay.py`、`test_anchor_event.py`、`test_topic_navigation.py`、`test_focus.py`、`test_r14_f_navigation.py`、`test_r16_b_nav_commands.py`） |
| C | `frontend/src/stores/session.ts`、`frontend/src/services/api.ts`、`frontend/src/views/PlanetView.vue`、`frontend/src/components/conversation/ComposerEntries.vue`（仅返回类型适配）、新建 `frontend/src/stores/__tests__/r17-c-*.test.ts` 与 `frontend/src/views/__tests__/r17-c-*.test.ts`；既有前端取消/导航测试的最小适配（`r16-c-position-state.test.ts`、`rm-e-continuation.spec.ts`、`rm-e-planet-start.spec.ts`） |
| D | `backend/src/agent/services/topic_recovery.py`（替换 2.5 占位）、`backend/src/agent/memory/ingest.py`（仅必要时）、`backend/src/agent/storage/migration_r17_*.py` + `storage/migrate.py` + `storage/schema.py`（仅确需迁移时）、新建 `backend/tests/test_r17_d_answer_identity.py`；既有恢复测试最小适配（`test_r14_b_f03.py`、`test_r14_f_recovery.py`、`test_r15_d_r4_recovery_lease.py`） |
| E | `backend/tests/test_r16_e_concurrency.py` |
| F | 新建 `backend/tests/test_r17_f_independent.py`、`frontend/src/__tests__/r17-f-independent.test.ts`（只写测试，不改生产实现、不改既有测试） |
| Lead | `docs/*`、`backend/tests/test_r17_lead_integration.py`、集成、最终验证与 git 操作；不代写实现 |

共享文件：`api/server.py` 只归 B（A 不得改）；`turn_orchestrator.py` 只归 A（D 不得改）；
`topic_recovery.py` 只归 D（A 只调用冻结接口）。需要别人文件的改动 → 发消息给 Lead 裁决，不得自己改。

## 4. 验证与汇报

- 定向命令：
  后端 `cd <worktree>\backend; $env:PYTHONPATH='<worktree>\backend\src'; .\.venv\Scripts\python.exe -m pytest tests\<file> -q -p no:cacheprovider`
  前端 `cd <worktree>\frontend; npx vitest run src/...`；交付前 `npx vue-tsc --noEmit`（C 必须）。
- 先写最小失败用例（基线红证据：命令 + 失败输出），再实现到绿；测试日志写 `r17-evidence\<role>\`。
- 本轮的受测 SHA 与全量/CI 台账沿用 r16 已启动记录（`929e8d7c`）；补修轮**不启动全量、不触发 CI**。
- 汇报格式（send_message 给 Lead）：1) 角色/状态；2) 分支 + commit SHA；3) 改动文件精确清单；
  4) 逐项行为变化 + 最小复现（测试名/命令）；5) 定向命令、通过/失败数、exit code、证据路径；
  6) 与契约偏差（必须显式）；7) 未完成/已知限制/集成接缝。

## 5. 验收矩阵（每个角色至少覆盖自己那几行）

| 编号 | 场景 | 断言 |
| --- | --- | --- |
| A1 | A 提交排队 → 手动切 B → A 执行完成 | 原问题/回答归属 A；前后端当前位置 B；A 轮期间不广播 A 为位置 |
| A2 | 离开再返回 | 旧轮不覆盖最终返回位置；无多余位置推进/广播 |
| A3 | 同话题改选历史接续 | 旧轮不落实/不取消新选择；pending 保持新选择 |
| A4 | 轮次结束前发生新导航 | 轮末不覆盖 B；回答仍归属 A |
| A5 | 无导航干扰的正常轮 | 补锚/自动分段/轮末推进行为保持 |
| B1 | 取消 A 在飞 → 接受 B → 旧响应到达（C 用例） | B 的位置/接续/草稿/position_version 全部保持；旧响应零部分修改 |
| B2 | 服务端取消目标身份 | 取消 A 不能取消 B；错误目标/版本 409 且零副作用；正常取消清提示 |
| B3 | 原子导航真实 HTTP | 目标不可用 / 版本冲突 / 事务中途失败 / 成功 / 响应丢失后重试：位置、接续来源、两类版本、广播次数 |
| B4 | 一次点击一个导航命令（C 用例） | 只发一个 /api/anchor；失败原位置+原接续保留 |
| D1 | 普通回答成功 | answer_key 非空且=answer_key_for(turn,user_msg)；内容类型/模型/raw/归属保持；一条正式消息 |
| D2 | 重复保存 / 普通答案已存在时恢复 | 返回原 answer_id；消息数不增；模型调用 0 次 |
| D3 | 保存失败后另一记录补存 | 共享暂存命中；模型调用仍 1 次；一条正式消息；记录同一 answer_id |
| D4 | 旧库空 answer_key | 可靠归属才关联；歧义不关联、不猜 |
| E1 | 双连接并发（A 赢/B 赢两种顺序） | 非执行者 202；只生成一次、一份答案、记录同一 answer_id；异常也释放执行者 |
| F | 独立验证 | 以上边界独立复现（基线红→集成绿）+ 四项边界评审 |
