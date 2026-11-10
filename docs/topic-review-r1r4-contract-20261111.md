# 话题系统复核补修 · R1—R4 共享契约（2026-11-11 · 第四轮）

> 本文件由 Lead 冻结，是本轮**唯一共享契约**。所有角色先读完再动手；不得单方面修改本文件，
> 需要变更接口先发消息给 Lead。基线、分支、工作树、文件所有权、验证纪律都在这里。
> 本轮只补 R1/R2/R3/R4 四项，不重做此前十四项（F01—F14）与五项 P1，不重新设计话题系统。

## 0. 基线与隔离

- 仓库 `HebiIsHere/QIO`。受检分支 `fix/topic-review-20261110`。
- 受检提交（本轮起点）：`44d92fb38338bb81a315d528abbdcad5eef79f69`。
  已 fetch 核实 `origin/fix/topic-review-20261110` 同 SHA（0/0）；工作树干净；无更新的远端修复。
- 主工作树：`D:\qio-dev\qio-r14`（所有实现与测试改动只在这里）。
- 基线只读副本：`D:\qio-dev\r15-baseline`（`git worktree`，detached 于 44d92fb3；
  `backend/.venv` 与 `frontend/node_modules` 是 junction，只读复用，不要删除/重装）。
  仅用于「基线红」证据，**不得**在基线副本里改实现文件；F/E 的基线红用例放基线副本 tests 下，
  不提交，也不写回主工作树。
- 原始证据（日志/JUnit/截图/命令输出）写到 `D:\qio-dev\r15-evidence\<role>\`（a/b/c/d/e/f/lead），
  **不要**写进工作树（保持 `git status` 干净，冻结 SHA 与受测代码一致）。提交进仓库的只有
  契约/交付记录/architecture/status 文档与源代码/测试。

### 0.1 Git 纪律

- **只有 Lead 执行 git 写操作**（add/commit/branch/push/rebase/checkout/worktree）。
- 子智能体只读 git（status/diff/log/show）；不 push、不触发 CI、不建 worktree、不切换分支。
- 同一时刻同一文件只有一个编辑者；跨文件需要改别人文件 → 发消息给 Lead（Lead 裁决/转交）。

### 0.2 禁止事项（所有人）

- 不跑全量 pytest / 全量 vitest / `npm test` / 生产构建 / 离线评测；不触发 CI；不 push。
- 不用真实 API Key / 联网 / 真实用户数据 / 付费模型；模型一律 fake/mock provider；不写真实应用数据库。
- 不改历史迁移；不新增生产依赖；新增日志/事件/错误文本必须过 `agent/trace/redact.py`。
- 不新增关键词/正则/包含匹配/相似度阈值式的自然语言意图识别；不新增授权分类器或第二模型层。
- 不用 `sleep` 构造并发/时序结果：用事件、屏障、受控时钟/租约测试接口。
- 不放宽/删除必须断言来换通过；不通过扩大 skip 减少运行次数；不用清空草稿规避问题。

## 1. 不可变产品决定（验收底线，逐条保留）

1. 所有用户消息完整进入 QIO 主循环，由主模型判断「主动要求」或「QIO 建议」；程序只消费
   现有话题工具参数 `intent=user_request|suggestion`（缺省 suggestion）。
2. 不增加关键词、正则或另一个模型进行授权识别。
3. 切换与新建继续复用现有工具及导航服务（不新增第二套前端直建/直切执行器）。
4. QIO 建议在正式回答前确认；拒绝申请后仍回答原问题。
5. 关闭弹窗只是收起，保留申请和名称。
6. 等待期间允许写草稿；确认后跟随真实目标，不自动发送。
7. 不修改真实应用数据库，不调用真实或付费模型。
8. 不合并 main、不打标签、不发布。
9. 位置权威仍只有一份：`navigation_version`（后端单调版本）+ Anchor 单写者
   （`TopicNavigationService`）；前端不另造位置权威，只消费后端事件/快照。
10. 本轮不声称外部模型调用严格 exactly-once；只保证同一逻辑答案唯一持久化与执行权收口。

## 2. 文件所有权（同一文件唯一编辑者）

后端路径相对 `backend/src/agent/`，前端相对 `frontend/src/`。

| 角色 | 编号 | 独占实现文件 | 独占测试文件（既有 + 新建） |
| --- | --- | --- | --- |
| A | R1 | `services/turn_orchestrator.py`、`core/turn.py`、`services/topic_application.py`、`core/loop.py`（仅在确有必要时） | 新建 `backend/tests/test_r15_a_r1_snapshot.py`；既有 `backend/tests/test_r14_a_f05.py`、`test_p1r_a_mainloop_semantics.py`、`test_topic_application_flow.py`、`test_topic_decision_integration.py`、`test_topic_decision_wiring.py` |
| C | R2 | `stores/events.ts`、`stores/session.ts`、`stores/restore.ts`、`services/api.ts`（仅运行时状态类型）、后端 `services/app.py`（仅 `_publish_anchor_event` 区域） | 新建 `frontend/src/stores/__tests__/r15-c-position-events.test.ts`；既有前端 `stores/__tests__/events.test.ts`、`stores/__tests__/r14-d-f12.test.ts`、`stores/__tests__/rm-e-continuation.spec.ts`、`__tests__/r14-f-draft-follow.test.ts` |
| B | R3 | `api/server.py`（anchor 路由 + `/api/runtime/state` 的 `anchor` 字段）、`services/navigation.py`、`services/binding.py` | 新建 `backend/tests/test_r15_b_r3_anchor_replay.py`；既有 `backend/tests/test_anchor_*.py`、`test_r14_lead_anchor_event.py`、`test_topic_navigation.py`、`test_continue_intent.py`、`test_p1r_c_topic_identity.py` |
| D | R4 | `services/topic_recovery.py`、`api/recovery_routes.py`、新建 `storage/migration_r15_r4_recovery_lease.py`、`storage/schema.py`、`storage/migrate.py` | 新建 `backend/tests/test_r15_d_r4_recovery_lease.py`；既有 `backend/tests/test_r14_f_recovery.py`、`test_p1r_b_topic_recovery.py` 及既有 recovery 相关测试 |
| E | 集成 | 无实现文件 | 新建 `backend/tests/test_r15_e_integration.py`、`frontend/src/__tests__/r15-e-integration.test.ts` |
| F | 独立 | 无实现文件 | 新建 `backend/tests/test_r15_f_independent.py`、`frontend/src/__tests__/r15-f-independent.test.ts` |
| Lead | 集成/文档 | `docs/*`、`scripts/*`（仅本轮台账需要时）；不代写实现 | 无 |

**共享接线（明确分工）**：
- C 在 `services/app.py` 的 ANCHOR 事件里追加 `instance_id`（`self.instance_id`），不动其它逻辑；
- B 在 `api/server.py` 的 `/api/runtime/state` 响应里按 §4.2 追加 `anchor` 对象（同时改 anchor 路由）；
- D 追加迁移 81 + 补偿 82（不改既有迁移），同步 `REQUIRED_OBJECTS`；
- Lead 写契约/交付记录/architecture §21/status P104，做集成与最终验证。

## 3. R1 / P1：位置快照必须早于异步上下文装配（A）

### 3.1 已复现问题（baseline 44d92fb3）

`_execute_turn` 在 `await self.build_context(ctx, adapter)` **之后**才调用
`_snapshot_decision_context(ctx)`。用户在装配期间在同一话题改选历史片段会让导航版本推进；
快照随后取到**新版本**，但本轮绑定/上下文片段仍是旧选择。模型基于旧依据返回工具调用时，
`base_nav_version` 恰好等于当前版本，CAS 通过 → 覆盖用户的新选择。

### 3.2 冻结实现要求

1. **时机**：snapshot 紧跟在 `begin()` 成功返回后、`await build_context(...)` **之前**执行，
   一轮只写一次。任何情况下不得在装配完成后用「当前最新位置」补齐旧绑定。
2. **快照内容**（`TurnContext` 字段 + trace `decision_snapshot`）：
   - `decision_turn_id`（本轮 turn）、`decision_user_message_id`（原用户消息）；
   - `decision_topic_id` / `decision_fragment_id`（本轮真实绑定的话题与片段，来自 begin 的绑定）；
   - `decision_intent_id` / `decision_intent_version`（本轮采纳的待接续选择及版本；无则 None）；
   - `decision_nav_version`（真实导航版本；读不到如实 None，不猜）；
   - `decision_queued`（受理时是否排队）。
   保留既有字段名，新增字段用 `getattr(ctx, ..., None)` 兼容旧替身。
3. **装配后复核**：build_context 返回后、首次模型调用前调用
   `_verify_decision_basis(ctx)`：
   - 重新读取**当前权威导航版本**与当前 Anchor（仅用于复核，不写回快照）；
   - 版本相同 → `ctx.decision_basis_state="fresh"`；
   - 版本已变 → `ctx.decision_basis_state="superseded"`，trace 写
     `decision_basis_superseded`（只记录事实，**不停止整轮**）；
   - 版本读不到 → `"unknown"`（不把未知伪装成 fresh/superseded）。
4. **失效语义**：superseded 后所有话题操作仍只继承快照的 `base_nav_version`（旧值），
   由既有写入前 CAS 判 stale → 不执行、不弹申请、不跳回旧位置、不写拒绝证据。
   **禁止**在 `_verify_decision_basis` 里把快照更新为新版本（那等于再次用装配后的当前值补齐）。
5. **用户浏览 ≠ 取消原问题**：装配期间用户离开/返回/改选都只让「基于旧依据的新导航失效」；
   已绑定的普通回答仍按 `turn_bindings` 的原归属保存并完成，不整轮停止。
6. **同轮工具自己的合法导航**只通过 `note_decision_navigation` 的权威执行结果更新后续依据；
   不得在 await 之后重新读取「当前最新位置」冒充本次工具结果。因果更新后
   `decision_basis_state="causal"`。
7. **采用新依据必须显式重建**：本轮不新增「装配后自动重取新依据」的路径；需要新依据时由
   新一轮（或既有显式重建上下文路径）重新建立上下文与快照。代码注释与文档写明这一边界。
8. 保留现有写入前事务/CAS；正确的 CAS 不能弥补错误的快照形成时机。

### 3.3 验收（事件屏障，不依赖 sleep）

新建 `backend/tests/test_r15_a_r1_snapshot.py`，使用真实 `AppContext.run_turn` +
真实临时 SQLite + fake scripted adapter（第一响应发 `switch_topic`，第二响应正常回答）：
1. **装配中改选历史（主场景）**：装配阶段用 `asyncio.Event` 暂停（挂在 build_context 的
   真实内部步骤或受控 hook）；暂停期间同一话题内执行真实的「改选历史片段」导航（推进版本）；
   释放装配与模型响应；模型返回旧依据下的 `switch_topic` 调用；断言：
   旧操作未执行（目标 Anchor 未被覆盖）、用户新选择仍在、导航版本未被旧操作推进、
   本轮正式回答仍按原绑定归属保存。
2. **装配期间切走再返回**：真实导航离开 + 返回（版本推进）→ 旧操作 stale；回答原归属完成。
3. **装配期间待接续位置改变**：`register_continuation` 改选另一历史片段（版本推进）→ stale。
4. **正常无导航变化**：快照版本=当前版本，操作正常执行成功，快照因果更新（对照组）。
5. **顺序断言**：spy 记录 `_snapshot_decision_context` 与 build_context 完成顺序，证明快照早于装配；
   断言快照字段（turn/user_message/intent/topic/fragment/nav）真实写入。
6. 既有 `test_r14_a_f05.py` 全绿（快照字段/因果更新/失败不更新等语义不倒退）。

## 4. R2 / P1：旧位置事件必须整条拒绝，不能只保护草稿（C）

### 4.1 已复现问题（baseline 44d92fb3）

`events.ts` 的 ANCHOR 分支先调用 `session.applyAuthoritativeTopicAnchor`（只决定草稿是否迁移），
随后**无条件** `session.setAnchor(...)` 与 `setPendingContinuation(...)`。校验只保护草稿，
旧事件仍改变当前位置。已复现：手动位置事件 版本9/序号20 进入 manual-new；再收旧操作成功事件
版本8/序号19；草稿未搬错，界面仍跳回旧目标 t2。`navVersion` 没有形成完整位置顺序保护；
`seq` 依附一次性的草稿跟随对象（follow 不存在/已消费时就没有保护）。

### 4.2 冻结接口

**(a) 后端 ANCHOR 事件（C 改 `services/app.py`）**：在既有字段基础上追加
`instance_id: self.instance_id`（进程实例身份；跨重启/重连时 `seq` 会归零，靠它区分）。
其余字段（topic_id/topic_name/fragment_id/fragment_title/historic/pending_*/origin/
operation_id/param_version/turn_id/approval_id/nav_version/seq）保持不变。

**(b) 后端权威快照（B 改 `api/server.py` 的 `/api/runtime/state`）**：响应追加
`anchor` 对象（顶层已有 `instance_id`）：
```json
"anchor": {
  "topic_id": "...", "topic_name": "...", "fragment_id": null, "fragment_title": null,
  "historic": false, "nav_version": 12,
  "pending_intent_id": null, "pending_intent_version": null,
  "pending_source_fragment_id": null, "pending_source_title": null
}
```
数据来源与 `_publish_anchor_event` 完全一致（AnchorService + navigation_version.current +
bindings.peek_intent + fragment_title）。读不到 nav 版本时如实 `null`。

**(c) 前端 session 接口（C 实现，E/F 按此写独立用例）**：
- 新增 session state：
  - `_positionFacts: { topicId: string; fragmentId: string | null; historic: boolean;
     navVersion: number | null; instanceId: string | null; seq: number | null;
     source: "snapshot" | "anchor" } | null`（初始 null）；
  - `_invalidTopicOperationIds: string[]`（已知被拒绝/失效/被新导航替代的话题操作 id，
    有界，按操作进入终态时登记）。
- 新增方法：
  - `applyAuthoritativePositionSnapshot(anchor, instanceId)`：无条件采用服务器权威快照
    （设置 position facts + `setAnchor` + `setPendingContinuation`；旧 instance 的本地 seq 不复用）；
  - `acceptTopicPositionEvent(input): { accepted: boolean; reason: string; migrated: boolean }`：
    **先判定是否接受这条位置事件**；接受后才统一更新当前位置、历史位置/待接续提示、
    并在最后调用既有草稿跟随迁移（`applyAuthoritativeTopicAnchor`）。
- 既有 `applyAuthoritativeTopicAnchor` 只保留「草稿是否迁移」职责，不再承担位置接受判定；
  其 operation/approval/param_version/seq 校验继续用于真实操作关联。
- `events.ts` ANCHOR 分支必须改为：`acceptTopicPositionEvent(...)` → 仅当 accepted 才
  `setAnchor` / `setPendingContinuation` / `loadHistory`；rejected 事件整条丢弃（不迁移草稿、
  不改 pending）。

### 4.3 接受规则（冻结；E/F 独立断言这些行为）

位置事件输入字段：`topicId, fragmentId, historic, navVersion, instanceId, seq, origin,
operationId, approvalId, paramVersion, turnId, pending*`。

先做**操作/轮次关联拒绝**：
- `origin="topic_operation"` 且 `operationId` 在 `_invalidTopicOperationIds` 中 → 拒绝
  （reason=`invalidated_operation`）；拒绝/失效/被新导航替代的操作迟到事件不得重新导航或迁移草稿。
- `origin="topic_operation"` 且 `turnId` 属于另一个**已知活动轮**（`session.activeTurnId`
  非空且不等）→ 拒绝（`stale_turn`）。

再做**位置顺序判定**（`_positionFacts` 存在时）：
1. `incoming.navVersion != null && facts.navVersion != null`：
   - `incoming < facts` → 拒绝 `stale_nav_version`（旧位置事件，即使草稿跟随已消费/不存在）；
   - `incoming > facts` → 接受 `new_nav_version`（真实新导航）；
   - `incoming == facts`：只允许**同版本合法细化**：
     * 同 topic 且 `facts.fragmentId == null` 且 `incoming.fragmentId != null` → 接受
       `fragment_refinement`；
     * 同 topic、同 fragment、同 historic → 接受 `idempotent`（无可见变化）；
     * 同 topic、incoming.fragmentId 为 null 而 facts 非 null → 拒绝 `ambiguous_same_version`
       （不得用缺字段事件抹掉已知真实片段）；
     * topic 不同 → 拒绝 `topic_conflict_same_version`；
     * pending 字段变化不算位置变化：接受事件后按事件值更新 pending，但位置判定仍按上面规则。
2. `facts.navVersion == null || incoming.navVersion == null`（旧后端/读不到版本）：
   退回「实例身份 + 顺序」规则：
   - 若两边 instanceId 都有且不同：若 `incoming.instanceId !== session.instanceId`
     （当前已采用的权威实例）→ 拒绝 `stale_instance`；否则视为新实例基线，接受并重置本地 seq；
   - 同实例（或 instance 缺失）且两边 seq 都有：`incoming.seq <= facts.seq` → 拒绝
     `stale_seq`；否则接受；
   - 都无法比较（都缺）→ 接受 `no_order_facts`（不阻塞正常显示；但操作关联拒绝仍生效）。
3. `_positionFacts == null`：直接接受（`baseline`），写入 facts。
4. 接受后：`_positionFacts` 更新为事件值（`source="anchor"`）；`seq` 只在同实例有意义；
   实例变化时 seq 置为事件值（新实例基线）。
5. **快照更新**：`applyAuthoritativePositionSnapshot` 更新 facts；若 `facts.navVersion > 快照版本`
   说明本地已有更新的实时事实，保留更**新**者（不倒退），但快照仍是权威的 pending/position 来源
   用于补齐缺失字段；snapshot 应用后缓冲事件按规则补放。

**独立性要求**：位置接受不得依附 `_topicDraftFollow` 是否存在/是否已消费/草稿是否非空。
无草稿、跟随已消费、跟随从未武装时，旧事件同样必须被拒绝。

**合并更新要求**：accept 后统一更新——当前位置（topic/fragment/historic）、待接续提示
（pending_intent_*）、草稿跟随；reject 时三者都不动。

**必须保留**：目标话题已有草稿的既有保护（不静默覆盖、让位记录/恢复不变）、
不自动发送、不通过清空草稿规避问题。

### 4.4 验收（新建 `r15-c-position-events.test.ts`，真实 Pinia store + 事件路由）

每个场景同时断言：当前位置（currentTopicId/anchorFragmentId/anchorHistoric）、待接续
（pendingContinuation）、草稿跟随/草稿正文：
1. 新手动导航（v9/seq20 → manual-new）后收到旧操作成功事件（v8/seq19）→ 位置仍在 manual-new，
   pending 不被旧值覆盖，草稿不迁移；
2. 跟随标记已经消费后重放旧事件 → 位置不变、草稿不变；
3. 没有草稿时重放旧事件 → 位置不变（不只断言草稿）；
4. 同话题不同片段的乱序事件（旧 v < 新 v；同 v 细化规则）；
5. 拒绝/失效操作的迟到事件（operation 进入 invalid 集合）→ 位置与 pending 都不变；
6. 重连 / 权威快照更新 / 服务重启（instance 变化、seq 归零）后的事件：旧实例事件拒绝、
   新实例事件接受、seq 不再跨实例比较；
7. 同版本合法补充片段信息 → 接受并更新 fragment；同版本 topic 冲突 → 拒绝；
8. 目标已有草稿保护 + 确认后跟随真实目标 + 不自动发送（既有语义不倒退）。
另需 `npx vue-tsc --noEmit` exit 0，且既有前端事件/草稿/恢复用例（C 所有权内的）全绿。

## 5. R3 / P1：历史旧请求不能绕过服务层的失效结果（B）

### 5.1 已复现问题（baseline 44d92fb3）

`POST /api/anchor` 的 continue 分支调用 `register_continuation` 后忽略
`inactive / state=replaced / intent_id=null` 结果，仍用旧请求的 fragment_id 调用
`enter_topic`（`advance_nav_version=opens_current` → false），也不推进版本。
真实 HTTP 复现：A 选历史 A → B 改选历史 B → 网络重放 A：三次均 200，最后 Anchor=A、
pending 来源=B、导航版本保持 2。

### 5.2 冻结实现要求

1. 路由必须消费登记结果的有效性，不能只取目标片段继续执行。
2. 新增服务方法（`TopicNavigationService`）把「登记 + 有效性判断 + 需要的 Anchor 落位」
   放进**同一 SQLite 事务**（`transaction(conn)` 可嵌套复用）；失败整体回滚，
   不留下「显示 A、实际接续 B」的半截状态。
   方法名与返回（供 E/F 断言）：`register_and_position_continuation(topic_id, fragment_id,
   *, request_id=None, turn_id=None) -> dict`，返回至少：
   `{topic_id, fragment_id, fragment_title, historic, intent_id, intent_version,
     source_fragment_id, nav_version, changed: bool, replay: bool, inactive: bool,
     state: str | None, opens_current: bool}`。
3. 三种情况**必须分开**（不能仅以 `changed=false` 决定动作）：
   - **当前同一选择的幂等复用**（同 request_id 且参数一致，或重复点同一来源；intent 仍有效）：
     `changed=False, replay=True`；**零位置写入、零版本推进、零 ANCHOR 广播**；
     返回既有选择与当前 Anchor 事实（只读）。
   - **已失效旧选择的请求重放**（intent 被 replaced/cancelled）：`inactive=True`；
     零位置写入、零版本推进、零 ANCHOR 广播；路由返回 409
     `{ok:false, conflict:true, code:"continuation_request_inactive", state:...}`。
     consumed 的选择重放：若它仍是当前选择且无更新选择 → 按幂等复用回放既有结果（零写入）；
     若已被替换/取消 → 按失效拒绝。
   - **用户真正发起的新选择**（changed=True，或点当前开放片段 opens_current）：
     正常落位；真实改选推进一次版本；`changed=True`；广播一次 ANCHOR。
4. **request_id 参数一致性**：同一 request_id 已用于不同 (topic_id, fragment_id) 的重放 →
   409 `{code:"continuation_request_conflict"}`，零写入、零版本推进；
   不得返回既有旧意图冒充成功。
5. 真实选择改变正确推进版本（一次点击最多 +1）；幂等重放与被拒绝的旧请求零位置写入、
   零版本推进。
6. 保留「点击登记、发送时落实」两阶段接续：点击路径**不创建片段**、不落实意图；
   `apply_continuation` 仍是唯一创建/复用片段的位置；不重新引入已删除的组合便捷入口。
7. 既有响应字段保持向后兼容（ok/topic_id/fragment_id/fragment_title/historic/
   created_fragment_id/source_fragment_id/intent_id/intent_version/pending），可追加
   `changed/replay/inactive`。
8. 普通（非 continue）`POST /api/anchor` 行为不变：进入话题最新位置、恢复位置规则不变。

### 5.3 验收（真实 FastAPI `create_app` + 临时 SQLite，遵守仓库测试认证约定）

新建 `backend/tests/test_r15_b_r3_anchor_replay.py`，用 `TestClient` 走真实 HTTP：
- A→重复 A（同 request_id）：第二次 200 replay、零写入零推进；Anchor A、pending 来源 A、
  intent 状态 registered、导航版本等于首次；
- A→B→重放 A（request_id A）：第三次 409 inactive；Anchor 仍 B、pending 来源 B、
  intent A=replaced / B=registered、导航版本保持（只被真实改选推进一次）；
- A 取消后重放 A：409 inactive；Anchor/pending/版本均不变；
- A 已消费后重放 A（无更新选择）：200 replay；零写入零推进；Anchor/pending/版本不变；
- 用户重新主动选择 A（新 request_id，选 B 之后）：200 changed；Anchor A、pending A、
  版本 +1；
- 请求参数与已使用 request_id 不一致：409 continuation_request_conflict；零写入。
每个场景断言 Anchor 行（AnchorService.get_active）、pending 来源（bindings.peek_intent）、
意图状态（intent_by_request/by_id）与 navigation_version.current；
不能只检查 HTTP 状态码或服务层返回值。另断言点击路径不创建片段（fragments 计数不变）。

## 6. R4 / P2：逻辑答案执行权、租约续期与写入权收口（D）

### 6.1 已复现问题（baseline 44d92fb3）

租约按 recovery record 领取；虽唯一答案约束防重复落库，但不防重复生成：
A. 同一原问题两条恢复记录（answer_key 相同）并发：各自领取 record 租约 → 模型调用两次，
   最终仅一份答案；
B. 第一任务仍在等模型但 600s 租约已到期：新请求接管并再次调用模型，最终仍一份答案。
本轮必须补齐，不能继续靠延长超时。

### 6.2 冻结实现要求

1. **按逻辑答案身份协调执行权**：新增持久表（迁移 81，见 §6.3）
   `topic_answer_runs`，主键 `answer_key`；同一 `answer_key` 的多个恢复记录共享同一个
   生成执行权，不能分别发起模型调用。第二个请求在别的 record 上到达时：
   - 现有执行有效 → 结构化 202 `recovery_in_progress`（不调用模型）；
   - 无执行/可接管 → 接管（世代 +1）后由它生成。
2. **有效执行期间续租（心跳）**：模型调用期间起一个心跳任务按租约间隔续租
   （条件 UPDATE：`answer_key + generation + owner`）；**不持有 SQLite 写事务**、
   不用全局锁串行所有恢复（不同 answer_key 可并行）。
3. **执行世代/写入权令牌**：每次领取/接管 generation+1，写入权 = `(answer_key, generation, owner)`。
   答案暂存、保存、成功收口、失败更新、租约释放都必须校验当前执行权；
   失去执行权的旧任务不得覆盖新任务的状态、暂存结果或租约（CAS 失败即放弃写入，
   返回 `{ok:false, code:"recovery_execution_superseded", superseded:true}`）。
4. **接管策略**：租约到期且能证明旧实例已失效（复用 `InstanceRegistry.owner_alive`：
   `False` 可接管；`True` 不得接管，返回 202 处理中）才允许接管；
   判不出来（None）时按既有「unknown 不改状态」约定不接管；但心跳保证活任务租约不会
   仅因固定时间到期而失效。真正失效（exited/pid 不存在）的进程必须能被接管。
   不得引入第二套存活机制；优先复用现有 `ctx.instances`。
5. **可控时间/租约测试接口**（F/E 与 D 共用，冻结）：
   - 模块级 `_now_utc() -> datetime` 作为租约时间唯一来源（测试 monkeypatch 它模拟时间）；
   - `retry_topic_answer(ctx, record_id, *, expected_class=None, expected_status=None,
     lease_seconds=None, heartbeat_interval=None, heartbeat_wait=None)`：后两个参数是
     测试缝（生产不传）；`heartbeat_wait` 是 `async def () -> None`，每次续租前调用，
     测试用事件控制节奏，避免 sleep。
6. **保留**：同一逻辑答案唯一保存（`messages.answer_key` 唯一索引不变）、答案与成功状态
   原子提交、已生成但保存失败只补存不重生成、无关恢复任务可并行、封存后的原轮次归属、
   恢复不覆盖当前导航（现有 nav 版本复核保留）。
7. **事务收口**：保存事务内再校验一次执行权（当前 generation/owner）、写答案行、
   把**所有**同 answer_key 的 pending 恢复记录置 answered（含 answer_id）、
   `topic_answer_runs` 置 answered；一切原子。旧执行者迟到保存/迟到失败只允许读结果或
   无副作用返回，不得改任何行。
8. **不声称**外部模型调用严格 exactly-once；崩溃在模型返回与结果持久化之间仍可能再次调用，
   在文档与结果文案中如实说明。

### 6.3 迁移（只追加；D 执行）

- 迁移 81：`topic_answer_runs`（`answer_key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT
  'pending', generation INTEGER NOT NULL DEFAULT 0, owner TEXT, owner_instance TEXT,
  claim_until TEXT, record_id TEXT, answer_id TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL`）+ 必要部分索引；
  `ALTER TABLE topic_recovery_records ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0`、
  `execution_owner TEXT`、`last_heartbeat_at TEXT`。
- 补偿 82：重放 80 全部语句 + 迁移 81 的建对象语句；`COMPENSATION_VERSION=82`。
- `migrate.REQUIRED_OBJECTS` 追加新表/新列/索引（按既有「补偿语句必须能补齐」判据）。
- 不改历史迁移；新模块 `storage/migration_r15_r4_recovery_lease.py`。

### 6.4 验收（新建 `test_r15_d_r4_recovery_lease.py`，fake 模型 + 真实 SQLite + 受控时钟）

每个场景分别检查：模型调用次数、正式答案行数、answer_id、执行权（generation/owner）、
记录状态更新；不只检查唯一索引：
1. 同 answer_key、不同 record 并发：只有一次模型调用；等待方 202 处理中；最终一份答案、
   两条记录都 answered 且 answer_id 相同；
2. 同 record 并发：同上；
3. 活任务运行超过原租约周期（受控时钟推进 + 心跳续租）：第二个请求仍 202，模型调用仍 1；
4. 真正失效实例接管（InstanceRegistry 判 False / 租约到期且实例已退出）：接管成功，
   generation 递增；旧执行权作废；
5. 旧执行者迟到成功：接管后旧任务恢复继续 → 不写答案、不改记录/暂存/租约，返回 superseded；
6. 旧执行者迟到失败：不得把新任务记录改成失败、不得清新租约；
7. 保存失败后补存：只补存、不再次调用模型（既有语义）；
8. 接管与保存竞争：一轮保存与新执行权竞争，只产生一份答案，保存事务不会覆盖新执行权状态。
另：既有恢复测试（`test_r14_f_recovery.py`、`test_p1r_b_topic_recovery.py` 等）全绿；
迁移对象校验/补偿测试（含新表/列）按仓库既有测试通过。

## 7. 跨模块集成（Lead 负责最终调度，E 负责测试）

1. 上下文装配期间改选历史：旧操作不能执行；后续迟到 ANCHOR 也不能让前端跳回
   （R1 后端 + R2 前端）。
2. A→B→重放 A 不改变后端位置（R3）；相应事件不改变前端的 B（R2）。
3. 恢复旧问题不抢占新轮次、不改变当前导航和草稿（R4 + R2）。
4. 正常切换、新建、改名、冲突确认、拒绝后回答、收起重开、草稿跟随、封存后恢复仍可用
   （用受影响的既有用例回归，不重写实现）。
5. 不引入第二套状态权威：前端位置只由 ANCHOR/权威快照更新，后端位置只由
   `TopicNavigationService` 写；不让前后端各自推测执行结果。

## 8. 验证纪律与命令

- 后端（主工作树）：`cd D:\qio-dev\qio-r14\backend`；
  `$env:PYTHONPATH='D:\qio-dev\qio-r14\backend\src'`；
  `.\.venv\Scripts\python.exe -m pytest tests/<file> -q -p no:cacheprovider`。
- 基线红证据（只读副本）：`cd D:\qio-dev\r15-baseline\backend`；同样用
  `D:\qio-dev\qio-r14\backend\.venv\Scripts\python.exe`，`PYTHONPATH` 指向基线 `src`。
  基线副本里只放 F/E 的临时测试文件；不要改基线实现。
- 前端：`cd D:\qio-dev\qio-r14\frontend`；`npx vitest run <spec>`；
  类型检查 `npx vue-tsc --noEmit`。
- 并发：同一时刻最多 3 个 pytest 进程，单文件定向优先，禁止全量；前端只跑相关 spec。
- 每个实现者：先建立最小失败场景（能红）→ 改 → 定向测试（含新增边界）→ 发消息报告 Lead。
- 事件/时序用事件、屏障、可控时钟/租约测试接口，不用任意 sleep。
- 所有新增行为必须同时有「前一刻失败、修复后通过」的证据（F 独立复现补齐基线红）。

## 9. 汇报格式（子智能体 → Lead，发消息）

1. 角色/编号/状态（完成/受阻/部分）；
2. 改动/新增文件精确清单（绝对或仓库相对路径）；
3. 逐项行为变化 + 最小复现（测试名或命令）；
4. 定向命令、通过/失败数、exit code、日志路径（`D:\qio-dev\r15-evidence\<role>\*`）；
5. 与契约的偏差（必须显式说明）；
6. 未完成/已知限制/需要 Lead 集成的接缝。

## 10. 本轮编号

- `R1/R2/R3/R4` 是**本轮**编号（对应审查四项补修）；不要与 `F01—F14`、`P1-01…P1-05` 混写。
- 提交信息、测试名、交付台账统一用 R1—R4。

## 11. 实施期修订与 Lead 裁决（随交付记录同步）

1. **R2 invalid 集合边界（F 交叉评审后收窄）**：`TOPIC_PHASE.recoverable_failure` **不**是
   「被拒绝 / 已失效 / 已被新导航替代」的终态证据（恢复重试可能成功，且其导航可能已真实发生）。
   因此它不再把 operation 登记进 `_invalidTopicOperationIds`；它的迟到位置事件仍交给
   `nav_version` 顺序规则判定，草稿跟随按失败收口。拒绝 / 失效仍以 `APPROVAL_RESULT`
   非 executed 终态为登记来源（契约 §4.3）。
2. **R1 原用户消息身份**：快照时刻（begin 后 / build_context 前）本轮 user 消息尚未落库，
   `decision_user_message_id` 如实为 None；build_context 写入消息后**只补记这一条身份事实**，
   位置字段（topic/fragment/nav_version）绝不在装配后回填（F 交叉评审确认的边界）。
3. **R4 旧版本残留**：执行权以 `topic_answer_runs` 为权威；旧版本残留的 record.claim_owner
   若没有对应 run 行，按「无执行/可接管」处理（单一进程升级假设）。该边界已在交付记录与
   architecture §21 写明；不新增第二套存活判定。
4. **E 集成用例收窄**：装配期间用户登记更新的接续选择时，`advance_anchor` 按 P1-04 既有保护
   不推进 Anchor；E 的集成断言只要求「旧操作 stale、目标未被覆盖、pending 仍在、回答原归属」，
   不得断言位置细化必须落在本轮开放片段（Lead 裁决，未改实现语义）。
