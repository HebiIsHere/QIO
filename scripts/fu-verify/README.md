# V 组 · 独立验证（QIO 补充修复轮 2026-10-10 · A01–A06 / B01–B02）

这个目录是**独立验证组**的交付物：为 8 项修复各写能**抓到原缺陷**的受控用例，
并先在基线上跑出「反例成立」的原始证据。

* 基线：`da0436b84058aa7c1011a3870e6e745e1472f61b`（= `origin/main`）
* worktree：`D:\qio-dev\qio-fu-v`，分支 `wt/fu-v`
* **只新建**：`backend/tests/test_fu_verify_*.py`、`scripts/fu-verify/`、
  `frontend/src/**/__tests__/fu-v-*.spec.ts`。不改生产实现、不改他人测试、不改 docs。
* 断言只依据可观察行为：HTTP 状态码、SQLite 行、清单/report 对象的字段、版本推进、
  渲染出来的冻结文案。**不以 import 尚不存在的新模块作为断言前提**（否则基线会变成
  「收集错误」，就不是「先红」的证据了）。

## 一键运行

```powershell
# 基线（预期红 —— 那就是原缺陷仍在的证据）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fu-verify\run_all.ps1 -Label baseline

# 集成后重跑（预期全绿）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fu-verify\run_all.ps1 -Label after

# 只跑一段
powershell ... -File scripts\fu-verify\run_all.ps1 -Label after -BackendOnly
powershell ... -File scripts\fu-verify\run_all.ps1 -Label after -FrontendOnly
```

三段原始输出分别写入 `scripts/fu-verify/output/<label>-{backend,repro,frontend}-<时间戳>.txt`，
外加一份 `<label>-summary-<时间戳>.txt`。退出码 0 = 全绿，1 = 有用例红。

环境准备（首次）：

```powershell
cd D:\qio-dev\qio-fu-v\backend; uv sync --frozen --extra dev
robocopy D:\qio-dev\qio-wt-fixes\frontend\node_modules D:\qio-dev\qio-fu-v\frontend\node_modules /MIR /NFL /NDL /NJH /NJS /MT:16
```

前端用例沿用仓库自带的 `frontend/vitest.config.ts`（`include: src/**/*.spec.ts` 已经能收到
`fu-v-*.spec.ts`），所以运行器不额外造配置：

```powershell
cd D:\qio-dev\qio-fu-v\frontend
npx vitest run src/stores/__tests__/fu-v-a01-recovery-inbox.spec.ts src/components/planet/__tests__/fu-v-a04-candidates.spec.ts
```

## 基线跑分（本次，2026-10-10 11:15）

| 段 | 结果 | 原始输出 |
| --- | --- | --- |
| 后端受控用例 | **52 failed / 21 passed**（共 73 条） | `output/baseline-backend-20261010-111539.txt` |
| 反例现场（8 个只打印不断言的脚本） | 8/8 正常跑完，现场全部命中 | `output/baseline-repro-20261010-111539.txt` |
| 前端受控用例 | **14 failed / 0 passed**（2 个文件） | `output/baseline-frontend-20261010-111539.txt` |

红的是「原缺陷仍在」，绿的 21 条是**对照**（既有行为不许倒退的那些）。

## 用例对照表

| 项 | 原缺陷（基线可观察事实） | 用例文件 | 关键用例 | 修复后期望 |
| --- | --- | --- | --- | --- |
| A01 历史 queued/running 无归属行不可见 | `interrupted_turns=[]`；`POST /api/turns/{id}/resend → 409`；`GET /api/recovery/records → 404`；台账行状态原样不动 | `backend/tests/test_fu_verify_a01_a03_recovery.py` | `test_a01_legacy_unowned_rows_become_visible`、`test_a01_legacy_row_survives_restart_and_is_still_operable`、`test_a01_continue_yields_exactly_one_effective_execution`、`test_a01_continue_gives_new_turn_an_owner_that_is_traceable`、`test_a01_concurrent_continue_produces_one_successor`、`test_a01_second_continue_click_conflicts`、`test_a01_notify_rows_never_enter_the_inbox`、`test_a01_listing_truncation_is_honest` | 清单 200 且含该行（`state_class=legacy_unowned`、`owner_state=none`、消息原文）；`continue` 200 → 恰好 1 个后继 + 恰好 1 次执行 + 归属可追踪；重启后仍可见可继续；重复/并发第二次 409；notify 轮永不入清单 |
| A01 历史无归属 running 派生任务 | 既不在任何清单里，也不能放回队列 | 同上 | `test_a01_legacy_running_derived_task_is_listed_and_requeueable`、`test_a01_derived_requeue_conflicts_after_state_change` | `kinds=derived_task` 清单 200（`derived_legacy`，`attempts/last_error` 如实）；`requeue` 200 → `pending`、`run_after=NULL`、`claim_generation+1`、`attempts/last_error` 不重置；重复 409 |
| A02 旧 schema 人工值被自动候选覆盖 | `summary` `人工写的摘要：这只鹅会咬人` → `自动摘要：鹅会看门`；`kind` `动物` → `家禽`；`年龄 2 岁` → `3 岁`；`pending 候选条数 = 0` | `backend/tests/test_fu_verify_a02_provenance.py` | `test_a02_legacy_manual_values_are_not_overwritten`、`test_a02_conflicts_are_registered_as_candidates`、`test_a02_gaps_still_get_filled`、`test_a02_real_upgrade_path_protects_manual_values` | 原值全部保留；4 条冲突进候选（`summary` / `kind` / `aliases` / `attributes.年龄`）；空缺（新属性 `健康状况`）照常补；不得把 legacy 值写成 `source: "user"` |
| A03 真实形状孤立 claim 无修复入口 | `orphaned_turns` 只有**只读**出口；`unfinished` 看不见它；`repair` / `continue` 全 404 | 同上 | `test_a03_orphan_claim_gets_a_repair_entry`、`test_a03_repair_then_continue_restores_the_message`、`test_a03_repair_refuses_already_resent_record`、`test_a03_concurrent_continue_after_repair_has_one_successor`、`test_a03_ignore_keeps_the_message_and_creates_no_successor`、`test_a03_continue_with_wrong_expected_class_conflicts` | 清单里 `state_class=orphaned_claim` 且带 `repair`；`repair` 200 → `recovered_at` 清空、回到 `ready`、不新建 turn、不改消息原文；随后 `continue` 成功；**已重发过的行 repair 必须拒绝**且关联不断；并发只有 1 个后继；ignore 不删消息不产生后继；期望类别不符 409 且一行不改 |
| A04 候选无法被用户管理 | 后端已登记 4 条候选；`GET /api/entities/candidates`、`GET/POST /api/entities/{id}/candidates[/adopt|/dismiss]` 全 404 | `backend/tests/test_fu_verify_a04_candidates.py` | `test_a04_cross_card_listing_lists_candidates`、`test_a04_listing_exposes_the_fields_the_ui_needs`、`test_a04_adopt_writes_value_and_marks_user_source`、`test_a04_adopt_with_stale_revision_conflicts_and_changes_nothing`、`test_a04_duplicate_adopt_is_idempotent`、`test_a04_archived_card_is_not_adoptable`、`test_a04_dismiss_resolves_only_that_candidate`、`test_a04_duplicate_dismiss_is_idempotent`、`test_a04_listing_json_has_no_secret_material` | 两个读接口 200 且字段齐全（`candidate_id/field/current_value/candidate_value/reason_label/card_revision/adoptable`）；adopt 200 → 值写入 + `source=user` + revision 推进 + 候选消失；过期候选 409 + `conflict:true` + 值不变；重复点击不 500、不再改 revision；归档卡 `adoptable=false` + `blocked_reason=card_archived` + 不复活；dismiss 只解决一条；清单不含 `sk-` 之类密钥片段 |
| A04 前端入口 | `EntityPanel.vue` 完全不提候选；`services/entityCandidatesApi.ts` 不存在 | `frontend/src/components/planet/__tests__/fu-v-a04-candidates.spec.ts` | 全部 6 条（含「待处理候选（n）」区块、展开后当前值/候选值/采纳/丢弃、409 就地「已被改动」、500 不假装成功、归档卡按钮禁用 + 「已归档」、交付物存在性） | 区块默认收起、展开后文案与按钮齐全；冲突/失败就地说明且不抹掉候选；归档卡按钮禁用并说明 |
| A05 关闭结果不真实 | `aclose() → None`；却写了 `instances.exited_at`；adapter 被释放；`close_db_on_shutdown=True` 的库被关（`Cannot operate on a closed database`）；`shutdown(..., final_timeout=)` 直接 `TypeError` | `backend/tests/test_fu_verify_a05_close.py` | 四态矩阵 `test_a05_state_{normal,cooperative,delayed,stuck}_...`、`test_a05_delayed_cancel_gets_a_bounded_final_grace`、`test_a05_shutdown_is_idempotent`、`test_a05_state_normal_writes_nothing_after_close`、`test_a05_clean_close_lets_next_instance_recover_queued_rows`、`test_a05_unclean_close_leaves_rows_for_ownership_based_recovery`、`test_a05_lifespan_keeps_db_open_when_close_is_unclean` | `aclose()` 返回 `CloseReport`（`clean/unfinished/phases/detail` + `to_dict()`）；clean=True → 写干净退出 + 释放 adapter；clean=False → **不写干净退出、不释放 adapter、不关库**、如实列 `unfinished`；收尾顺序 后台 → 维护 → turn → 独立任务 → 重活 →（clean 才）干净退出 → adapter；重启后按归属恢复；关闭后不再写库 |
| A06 适配器记账归属被全局默认库接管 | 两个账本 A=0 / B=100，全局默认是 B；A 的适配器归属 = **B**；A=0 的首次直接调用**被放行**（剩余 100）；用量记进 `B.budget_used=10`，`A.budget_used=0` | `backend/tests/test_fu_verify_a06_accounting.py` | `test_a06_owning_ledger_wins_over_later_global_default`、`test_a06_first_direct_call_is_blocked_by_own_budget`、`test_a06_swapped_budgets_do_not_block_own_call`、`test_a06_usage_is_written_to_owning_ledger_exactly_once`（+2 条对照） | 显式绑定自己的账本；A=0 必须 `BudgetExhausted`；额度对调后 A=100 不得被 B 的 0 挡住；用量只进自己的账本且只进一次 |
| B01 高版本存量库漏对象 | 版本已记 29 → `apply_migrations` 返回 29、**对象一个都没补**；`InstanceRegistry.start()` 抛 `OperationalError: no such table: instances`；用户数据仍在 | `backend/tests/test_fu_verify_b01_migration.py` | `test_state03_version_29_without_objects_is_repaired`、`test_state03_compensation_is_repeatable_and_idempotent`（+4 条对照/守护） | 四种库状态（只到 25 / 25+26–28 / 29 但对象缺失 / 全新库）迁移后对象齐全（表 + 列 + 索引）、版本推进、数据不丢、重复幂等；版本号高不等于对象齐全 |
| B02 POSIX 低 PID 被当成判不出来 | 平台=posix 时 `_default_pid_alive(1/2/3/4)` 全 `None`，且替身记录 `os.kill` **一次都没被调用** | `backend/tests/test_fu_verify_b02_pid.py` | `test_posix_low_pids_are_probed_not_short_circuited`、`test_posix_low_pid_exists_returns_true`、`test_posix_low_pid_permission_error_means_alive`、`test_posix_low_pid_matrix_is_explicit[1..4]`（+ Windows 特殊 PID 对照） | POSIX 1/2/3/4 正常探测（信号 0）；不存在→False、权限不足→True、其它 OSError→None；Windows 0..4 仍 None 且绝不调 `os.kill` |

## 基线原始片段（摘自 `output/baseline-repro-*.txt`）

```
[基线观察] /api/runtime/state.interrupted_turns = []
[基线观察] POST /api/turns/{legacy}/resend  -> 409 {"detail":"这一条不在「未执行」状态…"}
[基线观察] GET  /api/recovery/records      -> 404 {"detail":"Not Found"}

[升级后] summary         = 人工写的摘要：这只鹅会咬人
[结果]   summary           = 自动摘要：鹅会看门
[结果]   attributes        = {'年龄': '3 岁', '健康状况': '良好'}
[结果]   pending 候选条数  = 0 []

[基线观察] orphaned_turns    = ['turn_orphan_claim'] （只有只读出口）
[基线观察] POST /api/recovery/records/{id}/repair      -> 404

[基线观察] GET  /api/entities/{id}/candidates                -> 404
[基线观察] POST /api/entities/{id}/candidates/{cid}/adopt   -> 404

[基线观察] aclose() 返回 = None None
[基线观察] instances.exited_at = {'exited_at': '2026-10-10T03:16:23.657756+00:00'}
[基线观察] adapter 是否被释放   = True
[基线观察] 关闭后数据库仍可用   = False（ Cannot operate on a closed database. ）

[基线观察] 这条 adapter 实际归属的账本是 = B
[基线观察] A=0 的首次直接调用核对结果 = 放行，剩余额度 100.0
[基线观察] 记完一次用量之后：A.budget_used = 0.0  B.budget_used = 10.0

[基线观察] apply_migrations 返回 = 29
[基线观察] 迁移后缺失对象 = ['table:instances', 'table:record_owners', 'column:turn_journal.owner_instance_id', …]
[基线观察] InstanceRegistry.start() 抛 = OperationalError no such table: instances

[基线观察] 平台=posix，os.kill 替身记录 = []
[基线观察] _default_pid_alive(1/2/3/4) = {1: None, 2: None, 3: None, 4: None}
```

## 修复后重跑时要知道的依赖面

* **A01/A03/A04 的 4 组端点必须真的挂上**。契约 §1 说 `api/recovery_routes.py` /
  `api/entity_pending_routes.py` 各由 Lead 在 `api/server.py` 挂一行；没挂就仍然 404。
  这是本组要抓的东西，不是用例的问题。
* **A05 要 Lead 改 `api/server.py` 的 lifespan**（`close_db_on_shutdown=True` 时只有
  `report.clean` 才关库）与 `AppContext.aclose()` 的返回类型。只改 `services/background.py`
  不够：`test_a05_lifespan_keeps_db_open_when_close_is_unclean` 会继续红。
* **A06 要 Lead 在 `AppContext._create_adapter` 成功后调用
  `bind_request_accounting(adapter, self.credentials)`**。只改 `credentials/usage.py` 不够：
  本组走的是真实 `build_adapter_for_credential()` 路径。
* **B01 的补偿迁移必须能被 `apply_migrations()` 自动跑到**（编号 > 29），并且要在
  `verify_required_objects` 之前出现；本组按 `sqlite_master` / `PRAGMA table_info` 校验，
  不看函数名。
* 前端 A04 的 DOM 断言依赖契约冻结的中文文案：「待处理候选（n）」「采纳」「丢弃」
  「已被改动」「已归档」。换文案会红 —— 换之前请先改契约。

## 诚实边界（受控环境里**无法**验证的部分）

1. **A01/A03「继续」之后的真实执行**不在受控范围：真实执行会走模型调用。本组把
   `TurnManager` 的 runner 换成记录型替身，只断言「恰好产生一个有效执行（恰好 1 个后继、
   runner 恰好被调 1 次）」。真实模型链路一律 fake provider，不付费、不联网。
2. **A05 的「真实多进程并发 / 真被杀掉的进程」**：用同一进程内的受控 `InstanceRegistry`
   （`heartbeat_ttl=0` + `pid_alive=lambda _: False`）替代；真实 pid 复用、跨机 host 判定
   不在受控范围内。
3. **A02 的「候选落在哪个存储」**：断言取「冻结的候选读接口 + 既有 `pending_candidates()`」
   的**并集**。如果实现把候选搬到新存储而两条读法都读不到，用例会红（按设计）。
4. **A04 的「保存失败」只在后端**：注入 sqlite 写失败需要替换连接对象，属于实现内部，
   不作为契约面。这一条在前端（adopt 接口 500）覆盖：候选仍在、不显示成功。
5. **B01 的 26–28 三个兄弟分支没有集成到本分支**：它们的迁移代码不在这个仓库里，所以
   `test_state02_*` 里的 26–28 是**受控模拟**（只有形状与版本号），不是真实迁移定义；
   真实迁移定义只用在 1–25。**它们自己的对象本轮无法补偿，本组不做任何「任意合并顺序都
   兼容」的声明**；那些分支合并前必须把迁移号抬到 30 以上（见契约 §6 的诚实边界）。
6. **B02 的 Windows 分支**用受控 `os` 替身判定（`os.name = "nt"` + 记录型 `kill`），
   并断言 Windows 分支**绝不调用 `os.kill`**；真实 `_windows_pid_alive` 的 ctypes 调用
   不在本组注入（会污染 `sys.modules`）。全程不结束任何真实进程：POSIX 路径只用信号 0。
7. **前端组件级细节**（`RecoveryInbox.vue` 的内部结构、EntityPanel 候选区的 DOM 形状）
   只按契约冻结的文案与「按钮是否禁用」断言；不依赖任何 class 名或变量名。
8. **A04 前端「归档卡按钮禁用」**依赖实现真的渲染出 `采纳` 按钮（而不是直接不渲染）。
   契约 §3.4 说「按钮禁用 + 说明」，所以本组按「存在且 disabled」断言。

## 文件清单

```
backend/tests/test_fu_verify_a01_a03_recovery.py   A01 + A03（16 条，基线全红）
backend/tests/test_fu_verify_a02_provenance.py     A02（8 条，基线红 4）
backend/tests/test_fu_verify_a04_candidates.py     A04 后端（9 条，基线全红）
backend/tests/test_fu_verify_a05_close.py          A05（13 条，基线红 10）
backend/tests/test_fu_verify_a06_accounting.py     A06（6 条，基线红 4）
backend/tests/test_fu_verify_b01_migration.py      B01（7 条，基线红 2）
backend/tests/test_fu_verify_b02_pid.py            B02（14 条，基线红 7）
frontend/src/stores/__tests__/fu-v-a01-recovery-inbox.spec.ts       前端 A01/A03（8 条，全红）
frontend/src/components/planet/__tests__/fu-v-a04-candidates.spec.ts 前端 A04（6 条，全红）
scripts/fu-verify/run_all.ps1                      一键运行（-Label baseline|after）
scripts/fu-verify/output/repro/repro_*.py          8 个只打印不断言的反例现场脚本
scripts/fu-verify/output/<label>-*.txt             运行原始输出（前后对照）
```
