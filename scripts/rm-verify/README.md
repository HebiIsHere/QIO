# F 组独立验证（scripts/rm-verify）

本目录是第二轮修复任务里 **F 组（独立验证组）** 的交付物：为 R01、R04、R06、R02、R03、
M04、M05、M07、M09、M10、C01、M12 各写**能抓到原缺陷**的受控用例，并在基线
（`origin/main = 6e073e92aa6cacd976455dc5acd1a4189054eb50`）上先跑出「反例成立」的原始证据。

F 组**不改任何生产实现**：只新建 `backend/tests/test_rm_verify_*.py`、
`scripts/rm-verify/`、`frontend/src/**/__tests__/rm-f-*.spec.ts`。

## 1. 一键运行

```powershell
# 后端 + 前端（原始输出同时落到 scripts/rm-verify/output/）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\rm-verify\run_all.ps1 -Label baseline

# 集成后重跑（同一批用例，前后对照）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\rm-verify\run_all.ps1 -Label after

# 只要一部分
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\rm-verify\run_all.ps1 -BackendOnly
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\rm-verify\run_all.ps1 -FrontendOnly
```

- 退出码：`0` = 全部通过（集成后应当是 0）；`1` = 存在失败用例（基线就是这个状态）。
- 环境准备（干净 worktree 首次）：后端 `cd backend; uv sync --frozen --extra dev`；
  前端把主仓的 `frontend/node_modules` 整目录复制到本 worktree 的 `frontend/` 下。
- 前端 `.spec.ts` 说明：仓库自带的 `frontend/vitest.config.ts` 只收集 `src/**/*.test.ts`，
  F 组按约定新建的是 `rm-f-*.spec.ts`，因此用本目录的
  `vitest.rmf.config.mjs` 显式收集（不改仓库自带配置）：
  `cd frontend; npx vitest run --config ../scripts/rm-verify/vitest.rmf.config.mjs`。

## 2. 用例设计原则

- **只依据可观察行为**：HTTP 状态码/响应体、SQLite 行与计数、active 版本数量、
  模型调用次数、asyncio 任务是否结束、前端 store 状态字段。
  不 import 任何「本轮新出现」的模块名作为断言前提，修复后仍可长期回归。
- **受控手段**：模型一律本地假 provider / 假 SDK 客户端（不联网、不用真实 Key）；
  `asyncio.Event` 当闸门；SQLite 连接子类在指定写入处注入一次真实 `OperationalError`；
  有界轮询（每 10ms 一次、上限约 3s）代替随机 sleep 与长时间等待。
- **不删失败用例、不降预期、不 skip**：基线红的 15 条就是「原缺陷仍然成立」的证据。
- 基线证据：`output/baseline-backend-*.txt`、`output/baseline-frontend-*.txt`
  （`output/baseline-summary.txt` 是 `--tb=no` 的汇总视图）。

## 3. 对照清单：问题 → 用例 → 基线观察结果

后端：`16 tests collected` → **15 failed / 1 passed**（那条 pass 是「不许退化」的守卫）。
前端：`2 tests` → **1 failed / 1 passed**（同理）。

| 项 | 用例 | 基线观察结果（原始片段） |
| --- | --- | --- |
| R01 | `test_r01_second_context_does_not_interrupt_a_live_instances_turn` | `AssertionError: A 仍然是活跃实例，B 启动不得把 A 的运行中记录标成 interrupted；实际 'interrupted'` —— A 的 turn 还在跑时，B 的 `interrupt_stale()` 已把它改写成 interrupted |
| R04 | `test_r04_journal_write_failure_is_not_reported_as_accepted` | `AssertionError: journal 写失败时不得装作已经受理：HTTP 必须是 503（非 200）；实际 200 / {"ok":true,"accepted":true,"turn_id":"turn_...","status":"accepted",...}` |
| R06 | `test_r06_claimed_but_undispatched_record_is_not_permanently_hidden` | `AssertionError: 抢占后崩溃的遗留记录不得被永久隐藏：必须可重发，或至少出现在权威状态里（resend=409, interrupted_turns=[]）` |
| R06（守卫） | `test_r06_accepted_but_never_dispatched_record_is_recoverable` | PASS（「受理后未派发就退出」这条既有恢复路径本来是对的，修复后不许退化） |
| R02 | `test_r02_second_correction_of_the_same_old_version_never_yields_two_actives` | `第二个 ToolResult(ok=True)…；actives=['kn_…','kn_…']` —— 同一链出现 2 个 active |
| R02 | `test_r02_two_connections_correcting_the_same_version_keep_one_active` | `first/second 都 ok=True；actives=['kn_da5…','kn_f4e…']`（2 个 active） |
| R03 | `test_r03_failed_activation_keeps_the_old_version_active` | `failed=OperationalError('disk I/O error (injected by rm-f)'), actives=[]` —— 旧版本已被撤销、新版本没激活，链上 0 个 active |
| M04 | `test_m04_extraction_keeps_attributes_the_candidate_did_not_mention` | `AssertionError: 候选没提到的属性不得被删掉：{'状态': '康复'}`（`颜色` 整条丢了） |
| M04 | `test_m04_manual_edit_via_management_api_survives_released_extraction` | `AssertionError: 人工纠正过的值不得被放行的旧提炼结果覆盖回来：{'状态': '生病'}` |
| M04 | `test_m04_user_deleted_attribute_is_not_revived_by_extraction` | `AssertionError: 用户删除的属性和提炼不得被反转回来：{'状态': '生病', '颜色': '白'}` |
| M05 | `test_m05_entity_failure_leaves_a_retryable_task_and_only_it_retries` | `AssertionError: 实体提炼失败必须留下一条可重试的实体任务，否则永远不会再试；task=None`（摘要已 completed，实体没有任何任务行） |
| M07 | `test_m07_outer_cancel_cancels_the_inflight_request_and_leaves_nothing` | `assert False is True`（`_GatedAdapter.cancelled`）＋ 取消后仍有未结束的在途请求任务 |
| M09 | `test_m09_exhausted_credential_budget_stops_the_next_model_call` | `AssertionError: 余额在第一次调用之后已经耗尽，不得再发起第二次模型调用；实际调用 2 次` |
| M10 | `test_m10_parse_retry_responses_are_all_accounted` | `budget_used: 55.0`（期望 165）—— 被解析重试丢弃的那次真实响应（110）没有入账 |
| C01 | `test_c01_text_mode_second_request_has_no_tool_role_and_carries_the_result` | `带 role=tool 但没有 tool_call_id 的消息不得下发：[… {'role':'tool','content':'回显结果:你好'}]`（roles=`['system','user','assistant','tool']`） |
| C01 | `test_c01_text_mode_multi_tool_and_failed_result_keep_a_stable_order` | 同样出现 `role=tool`（多工具/失败结果顺序断言因此也红） |
| M12 | `frontend/.../rm-f-m12-restore.spec.ts` | `AssertionError: expected [] to include 'appr_1'` —— 新连接（`onopen`）只拉了 turn 队列，快照里的待确认事项 / 独立任务 / 工具记录 / 叙事都没被应用 |

每条用例的完整断言理由与调用栈都在 `output/baseline-*.txt` 里（原文保留）。

## 4. 基线无法在受控环境验证的部分（如实说明）

1. **R01 的「unknown 一律不改状态」与「心跳过期 + pid 不存在才算真退出」**：
   构造「心跳过期但 pid 仍在」的第三态需要直接操纵实例注册表（本轮新模块），
   会违反「不 import 新模块作为断言前提」。本组验证的是最强可观察命题：
   活跃实例的运行中记录不被第二个上下文改写，且该轮之后仍能正常收口成 `completed`。
2. **R01 的「B 不能重复启动 A 的任务」**：需要两个真实进程并发认领派生任务；
   受控环境不做多进程编排与长时间等待，未覆盖（由 A 组自测 + Lead 集成验证）。
3. **M09 的「后台 / 子任务 / 内部重试同一规则」**：本组只覆盖主循环端到端路径
   （`ctx.run_turn` → `build_adapter` → 真实 `AgentLoop` + 真实 `CredentialStore`）。
   其余接线点需要 D 组/Lead 的新入口，未以新模块名断言。
4. **M10 的「失败但有已知用量也要记」「无用量失败标 incomplete」「消除双记」**：
   前两者要拿到 D 组新记账入口才能构造；本组用既有公开入口验证了
   「解析重试的每一次真实响应都必须入账（110+55=165，不多不少）」这一可观察命题。
5. **M12 的「同步期间事项结束不复活」「旧快照 / 旧实例迟到不覆盖」**：
   基线已有实现，且既有 `resyncProtocol.test.ts`、`historyRace.verify.test.ts` 已覆盖；
   本组新增用例聚焦基线缺口「新连接（无历史游标）的完整恢复」。
   其中「工具记录」以「快照必须交给 `reconcileTools`」为可观察断言，
   而不是断言「凭空新建工具卡」——避免对 E 组实现形状做过度约束。
6. **R06 的「并发两次重发只有一个后继」**：基线 `claim()` 的条件 UPDATE 本身已是原子一次性，
   真并发在 `TestClient` 单门户下不可控；本组用「重发成功后再重发必须 409」等价覆盖
   「一次性」语义，并用「抢占后崩溃」检查点覆盖真正会丢记录的那条路径。
7. **不在 F 清单内的项**（M06 / M02 / M03 / M08 / M11 / L01 等）本组不建用例，
   按 Lead 的清单由对应组与集成阶段覆盖。

## 5. 集成后复跑：三个红的根因与用例修正（after 轮）

在集成树（`fix/main-reliability-memory-20261009-r2`，HEAD `2e14cf4`）复跑后，
后端 16 条中 13 绿、3 红（前端 2 条全绿）。三条红**全部是用例自身的问题、0 个修复缺口**，
已用只读最小复现坐实，并按根因修正（只改注入/边界/观察通道，期望一条不降）：

| 用例 | 红的原因（用例 bug） | 修正 | 复现脚本 |
| --- | --- | --- | --- |
| R04 | 注入谓词写的是 `startswith("insert into turn_journal")`，而真实 SQL 是 `INSERT OR IGNORE INTO turn_journal …` → **注入从未触发**，测不出东西 | 谓词改为 `startswith("insert") and "turn_journal" in text`，断言一字不动 | `output/repro/repro_r04_journal_failure.py` |
| M09 | 用整层覆盖 `complete()` 的假 adapter，而预算闸门装在**真实 adapter 的每次实际请求之前** → 绕开了闸门；数的也是 adapter 调用而非真实 provider 请求 | 改用真实 `NativeAdapter` + 假 SDK 客户端 + 生产同款 `usage_sink=credential_usage_sink(store, adapter)`，断言 `client.calls == 1`、`budget_used == 120`，并要求给用户一句真实停止原因 | `output/repro/repro_m09_real_adapter.py` |
| R06 | 观察通道写死成 `interrupted_turns`；契约 C3 的孤儿出口叫 `orphaned_turns`（正式修复出口是 `repair_orphan()`） | 可见性判定扩成 `interrupted_turns ∪ orphaned_turns`，修复后走 `repair_orphan()` → resend 200 → `recovered_by` 非空 → 再次 409 | `output/repro/repro_r06_orphan.py` |

三个复现脚本**只读仓库源码**、只在临时目录建库、不写仓库文件；它们需要在**集成树**上运行
（`orphaned_claims()` / 请求前预算核对都是本轮才有的行为），例如：

```powershell
cd D:\qio-dev\qio-rm-main\backend
uv run --frozen python ..\scripts\rm-verify\output\repro\repro_r04_journal_failure.py
uv run --frozen python ..\scripts\rm-verify\output\repro\repro_m09_real_adapter.py
uv run --frozen python ..\scripts\rm-verify\output\repro\repro_r06_orphan.py
```

集成树上的观察结果（原始输出见集成树 `scripts/rm-verify/output/after-repro-*.txt`）：
R04 → `503 {'ok': False, 'accepted': False, 'error': '消息未被接受：持久化失败'}`、运行器 1 次、journal 1 行；
M09 → `真实 provider 请求次数: 1`、`budget_used: 120.0`、停止原因含「用量上限已耗尽（剩余 0）」；
R06 → `orphaned_turns: [('turn_crash', …)]`、`repair_orphan: True` 后 `resend: 200`、`recovered_by` 非空、再次 `409`。

## 6. 文件清单

```
backend/tests/test_rm_verify_r01_r04_r06.py           R01 / R04 / R06（4 条）
backend/tests/test_rm_verify_r02_r03_knowledge.py     R02 / R03（3 条）
backend/tests/test_rm_verify_m04_m05_memory.py        M04 / M05（4 条）
backend/tests/test_rm_verify_m07_m09_m10_c01.py       M07 / M09 / M10 / C01（5 条）
frontend/src/stores/__tests__/rm-f-m12-restore.spec.ts M12（2 条）
scripts/rm-verify/run_all.ps1                         一键运行（后端 + 前端）
scripts/rm-verify/vitest.rmf.config.mjs               只收集 rm-f-*.spec.ts 的 vitest 配置
scripts/rm-verify/output/                             基线原始输出（证据）
scripts/rm-verify/output/repro/                       三个根因最小复现（只读、需在集成树运行）
```
