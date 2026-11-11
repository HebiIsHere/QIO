# R19 · 复核补修交付记录（2026-11-11 · 第八轮）

> 本文件记录本轮**实际做了什么、证据在哪、边界是什么**，数据均来自本轮实际命令，不是推断。
> 唯一共享契约是 `docs/topic-unify-r19-contract-20261111.md`（在 R16 v2 / R17 / R18 之上只补两处）；
> 结构语义补充见 `docs/architecture.md` §22.11。本轮不合并 main、不打标签、不发布。

## 1. 基线、分支与提交

- 仓库 `HebiIsHere/QIO`；受检分支 `fix/topic-review-20261110`；基线 `9de91bb`（= origin，工作树干净）。
- 开发隔离：Lead 在 `D:\qio-dev\qio-r14` 集成；A/B 两条角色 worktree（`r19-a` / `r19-b`）从脚手架提交拉出；
  `backend/.venv` 与 `frontend/node_modules` 是共享 junction；原始证据在 `D:\qio-dev\r19-evidence\<role>\`。
- 提交序列（相对 `9de91bb`）：

| 提交 | 内容 |
| --- | --- |
| `d1f8376d` | 冻结 R19 共享契约（脚手架基线） |
| `94188f86` / `3e1bdd70` | A：首次互动板提交受理时解析默认话题并冻结归属（P1，含基线红→绿用例） |
| `e7fb5a0c` / `e328f38e` / `fe7d7e77` | B：保存失败记录提交后发布 RECOVERY_INBOX_CHANGED + 前端收件箱刷新与在途补拉（P2，含基线红→绿用例） |
| `dda64fb0` / `2059e5aa` | Lead：分别合并 r19/a、r19/b |
| 本文件所在提交 | Lead：集成验收用例 + 交付记录 + 架构 §22.11 + 状态 P108（`[skip ci]`） |

- **集成代码受测 SHA：`2059e5aa`**（A/B 全部合并后、本文件所在 docs 提交之前）。
  最终推送 SHA 以交付回复为准；本文件所在提交之后只改文档，不再改生产代码。

## 2. 两项问题的复现与修复

### 2.1 P1 首次互动板提交归错话题

- 复现（基线 `9de91bb`，Lead 真实 HTTP 用例 `test_r19_lead_integration.py::test_r19_p1_first_interactive_submission_uses_default_topic`）：
  DB 无任何当前位置（无 active Anchor）→ 创建互动板并勾选一张说明 → `POST /api/interactive/boards/{board}/submissions`
  受理（本轮开始执行，测试把 `build_adapter` 挂在闸门上 = 暂停模型准备）→ 用户手动 `POST /api/anchor` 切到 B →
  放行。基线：R18 的 `accept_turn` 在快照无 topic 时传 `turn_topic=None`，轮次到 `begin` 才用实时
  `app.current_topic()` 兜底，于是本轮归属被解析成 B，提问与回答写入 B（基线红：用户消息片段 topic = B，
  期望默认话题）。
- 修复：`interactive_store.submit` 的 `accept_turn` 在受理这一刻（`ctx.turns.submit` 之前）发现
  `basis["topic_id"]` 为空时，按既有兜底语义 `AppContext.current_topic()` **只读解析**默认话题
  （无 active Anchor 时取默认话题、必要时创建默认话题节点），写回**同一份**受理快照
  `basis["topic_id"]`，并作为 `ctx.turns.submit(message, topic, basis=basis)` 的 topic 与
  `turn.board_topic_id`。解析不 `enter_topic`、不写 Anchor/cursor、不推进任何版本；解析失败如实告警并按
  旧路径降级（不伪造话题）。位置版本沿用同一份快照（未额外读取），后续轮首补锚/轮末推进仍按 R17 依据链
  CAS —— 用户后来导航到 B 时旧依据失效，旧轮零位置副作用零广播，回答仍写回默认话题。
- 验证：A 新用例 `test_r19_a_default_topic.py` 2 passed（A1 无位置→暂停→切 B→放行；A2 无导航正常补锚）；
  Lead 集成验收 `test_r19_p1_...` 2 passed（含「无导航正常路径」）；相邻回归 69 passed（见 §3）。

### 2.2 P2 当前页面看不到回答重试入口

- 复现（基线 `9de91bb`）：普通回答保存失败时，R18 已在**同一事务**里写共享暂存 + pending 恢复记录，
  但没有任何通知；前端 `RecoveryInbox.vue` 只渲染 `GET /api/recovery/records` 拉到的清单，
  `TURN_END` 不会自动拉清单 —— 已打开页面在没有刷新/重连时看不到新记录，也就没有「重试回答」入口。
- 修复（后端通知 + 前端刷新）：
  - 新增事件类型 `EventType.RECOVERY_INBOX_CHANGED`（载荷 `record_id / turn_id / topic_id / kind /
    failed_stage / action`），并在 `api/bus.py` 归入 `CRITICAL_EVENTS`（Lead 授权；背压时不静默丢，
    溢出触发 RESYNC 时前端仍会走 restore → loadRecoveryInbox）。
  - `turn_orchestrator.persist` 在 `save_normal_answer` 抛错的路径上，用
    `answer_key_for(turn_id, user_message_id)` 调 `topic_recovery.find_pending_by_answer_key` 查
    **已提交**的 pending 记录：查到才 `await app.bus.publish(...)`，随后原异常照抛；查不到（暂存/记录未写成、
    被另一活跃执行者持有）不发事件。通知任何异常只记日志，不掩盖原异常、不改变轮次结局。
  - 前端 `services/events.ts` 事件枚举、`stores/events.ts` dispatch 新增 case →
    `session.requestRecoveryInboxRefresh()`：空闲时复用既有 `loadRecoveryInbox()`；同实例已有清单请求
    在途时**不并发第二个请求**，只登记「完成后补拉一轮」，由那次请求 finally 在成功取得结果并解锁后补拉；
    既有普通并发调用的单飞语义与既有测试保持不变。点击「重试回答」继续走既有
    `session.retryTopicAnswer` → `POST /api/recovery/records/{id}/retry-answer`（不改协议、不加乐观成功）。
- 验证：B 后端 `test_r19_b_recovery_notify.py` 6 passed（事件到真实 record_id、事务提交后发、
  查不到不发、通知不掩盖原异常）；B 前端 `r19-b-recovery-refresh.test.ts` 6 passed（事件刷新、在途补拉后
  新记录最终可见、重复事件幂等、普通单飞不变）；Lead 集成验收 `test_r19_p2_...` 1 passed（真实 HTTP +
  fake 模型注入一次写失败：事件带真实 pending record_id，解除故障后既有入口只补存一条正式回答、
  模型调用不增、原问题不重复、记录 answered、重复重试同一 answer_id）。

## 3. 本轮定向验证（命令、次数、受测 SHA）

全部命令在集成工作树 `D:\qio-dev\qio-r14`，后端使用共享 `.venv` + 临时 SQLite + fake provider；
前端使用共享 `node_modules`。受测代码 SHA = `2059e5aa`。

```
cd D:\qio-dev\qio-r14\backend
$env:PYTHONPATH='D:\qio-dev\qio-r14\backend\src'
& .\.venv\Scripts\python.exe -m pytest <files> -o addopts= -q -p no:cacheprovider

cd D:\qio-dev\qio-r14\frontend
npx vitest run <specs>
```

| # | 范围 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | Lead 集成验收（P1 两条 + P2 一条）+ A 新用例 + B 后端新用例（3 文件） | **11 passed，exit 0**（9.20s） | `r19-evidence\lead\FINAL-acceptance.log` |
| 2 | A 直接相邻回归（R18 互动依据/集成、R17 依据/重发、互动提交，6 文件） | **69 passed，exit 0**（24.53s） | `r19-evidence\lead\FINAL-adjacent-a.log` |
| 3 | B/事件直接相邻回归（R17-D、事件背压/协议/总线重放、R14 恢复，6 文件） | **45 passed，exit 0**（21.03s） | `r19-evidence\lead\FINAL-adjacent-b.log` |
| 4 | 前端定向（新 spec + 收件箱单飞/实例竞态/单源/r14-d/r14-f/r15-e，8 文件） | **72 passed / 8 files，exit 0**（4.73s） | `r19-evidence\lead\FINAL-frontend.log` |
| 5 | 前端类型检查 `npx vue-tsc --noEmit` | **exit 0，0 错误** | `r19-evidence\lead\FINAL-vue-tsc.log` |
| 6 | Lead 基线红（同一集成用例 @ `9de91bb`） | **2 failed / 1 passed**（P1-A1 问题回答被归到 B；P2 新事件类型不存在；P1-A2 无导航路径基线即通过） | `r19-evidence\lead\baseline-red.log` |

- 各角色自身证据：A 新用例绿 `r19-evidence\a\green\r19_a_green.log`（2 passed）与基线红
  `r19-evidence\a\baseline-red\`；B 后端 45 passed、前端 34 passed / 4 files（提交后重跑）与基线红
  （后端 4 failed/2 passed、前端 6 failed/6）见 `r19-evidence\b\README.md` 与 `r19-evidence\b\final\`。
- **本轮未运行**：全量 pytest、全量 vitest、生产构建、CI workflow；未调用真实/付费模型、未联网。

## 4. 已知限制与剩余验证范围

- 真实模型中文语义与真实浏览器界面未验证：fake/scripted provider + 前端 store 单测只证明接线、
  事件顺序、事务、归属与刷新合并逻辑；未启动浏览器/端到端。
- 补修提交**未跑全量批次与 CI**（按任务书）；不把定向通过写成全量通过。
- P1 只修互动板提交入口；`/api/turns`、重发、`start_interactive_turn`、`app.run_turn`、notify 轮按契约
  边界保持现行为（无当前位置 + 排队时理论上同类问题仍在，不在本轮范围）。
- P2 通知只覆盖 `persist` 普通正式回答保存失败路径（R18 冻结触发点）；`retry-answer` 自身补存失败
  不新增通知（此时记录已经可见）。新事件对旧前端是未知类型（忽略，保持现状）；新前端 + 旧后端仍靠
  连接/RESYNC 的既有 `loadRecoveryInbox`，不伪造记录。
- 事件在总线背压溢出时会触发 RESYNC（既有协议）：前端走完整 restore（含 `loadRecoveryInbox`），
  保证清单最终可见，但不保证这一条通知事件本身不丢。
- 普通回答外部模型调用不保证严格 exactly-once（沿用既有边界）。

## 5. 明确声明

**未合并 main、未打标签、未发布、未改变生产更新源、未操作真实应用数据库、未调用真实或付费模型。**
补修提交供复核；合并与发布留待后续决定。
