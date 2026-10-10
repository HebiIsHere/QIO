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

## 补充修复轮 · 四处判据修正（**不是放宽期望**）

集成后重跑本组用例时，有 10 条红。Lead 用独立探针逐条定性后确认：**实现是对的，
是这 4 处的判据本身写错了**（3 处判据写反/写错 + 1 处设计裁定）。修正只改判据，
**没有删任何用例、没有降低任何期望**；其中 A04 的修正还把断言换成了**更强**的行为
断言。逐条依据如下。

### 修正 1（A01/A03，5 条）· 后继关联的方向写反了

* 原判据：`successors_of()` 查 `WHERE recovered_by = <老 id>`。
* 依据：既有 R06 契约 C3（`TurnJournal.claim_for_resend()`，已合并进 main 并在用）
  在**老记录**上写关联 —— 事务里三件事是「条件 UPDATE 老记录写 `recovered_at` →
  INSERT 新记录 → `UPDATE turn_journal SET recovered_by = <新 turn_id> WHERE
  turn_id = <老 id>`」。所以真实方向是 `老记录.recovered_by ──▶ 新记录`。
* 探针实测（可复现）：

  ```
  CONTINUE 200 {"ok":true,"record_id":"turn_legacy","turn_id":"turn_b6217b154946","status":"accepted"}
  journal: turn_legacy          → status=interrupted, reason=user_confirmed_takeover,
                                   recovered_at=有值, recovered_by=turn_b6217b154946, owner_instance_id=qio_xxx
  journal: turn_b6217b154946    → status=completed, owner_instance_id=qio_xxx
  ```

  可见关联确实写在老记录上；按原方向查永远是 `[]`，红的是用例而不是实现。
* 修正：`successors_of(conn, record_id)` 顺着**老记录的 `recovered_by`** 找后继行
  （`recovered_by` 为空 → `[]`）。**断言一字未放宽**：仍然要求「恰好一个后继」
  `len(successors) == 1`、「并发只产生一个」、以及「同一个 turn_id」。

### 修正 2（A01 派生任务，2 条）· 「卡住」用例的时间戳太旧，被正常兜底重排了

* 原判据：`seed_derived_running()` 用固定字面量 `updated_at = 2026-10-10T00:00:00+00:00`
  播种，然后要求它作为「卡住的 running」出现在恢复清单里。
* 依据：`derived_tasks.recover_stale()`（`_STALE_RUNNING_SECONDS = 300`，应用
  lifespan 启动时调用一次）对「无归属 + 已超期」的 running 行会**合法地**放回
  `pending`。真实当前时间比那个字面量晚 3 小时以上，所以这行启动后已经是 `pending`
  —— 它**不是**卡住的 running，`_derived_records()` 只列 `running`，当然不进清单。
* 探针实测（双向对照）：

  ```
  updated_at 新鲜（now） → 清单里有 task_legacy derived_task derived_legacy owner=none actions=['requeue']，行仍是 running
  updated_at 旧值        → 启动后被重排成 pending，不进清单
  ```
* 修正：需要「卡住」语义的用例改用**当前时间**播种（`_utc_now_iso()`）；并**额外加断言**
  （已并入 `test_a01_legacy_running_derived_task_is_listed_and_requeueable`）：同一场景里
  再播一条**已超期**（`now - 1h`，`content_version` 不同以避开唯一约束）的无归属 running
  任务，断言它被启动恢复放回 `pending`、`run_after IS NULL`、且**不占着恢复清单**
  —— 也就是「它不是永久卡住，不需要用户操作」。这是真实行为，值得钉住。

### 修正 3（B02，1 条）· `OSError(13, ...)` 其实就是 `PermissionError`

* 原判据：`raise OSError(13, "unexpected probe failure")` 用来表达「其它 OSError」，
  断言 `_default_pid_alive(3) is None`。
* 依据：Python 3.3+ 的 `OSError(errno, ...)` 会按 errno **映射出子类**：
  `OSError(13, ...)` == `PermissionError`（EACCES）。而「权限不足」的既定分支是
  **`True`**（进程在、只是没权限），所以实现返回 `True` 是正确的，原判据表达不出
  「其它 OSError」。实测：`E assert True is None`。
* 修正：改用不会被映射成子类的 `errno.EIO`（EIO → 基础 `OSError`），并在用例里先
  自证 `not isinstance(OSError(errno.EIO, ...), (PermissionError, ProcessLookupError))`。
  既有的 `PermissionError → True` 与 `ProcessLookupError → False` 两条对照**原样保留**，
  期望值仍是 `None`。

### 修正 4（A04，2 条）· 设计裁定：dismiss **会**递增卡级 `revision`

* 原判据：`assert card.revision == revision`（「丢弃只解决候选，不该改卡的内容版本」）。
* 依据（Lead 裁定，**保留实现**）：`revision` 是**卡级 CAS 令牌**，不是纯内容版本。
  `_commit_card()` 对任何 `field_meta` 写入都递增它；丢弃要落
  `field_meta.pending/resolved`，递增它才能让并发的「丢弃 / 采纳 / 自动提交」**互相以
  409 暴露**而不是静默覆盖；提示词明确要求「不能静默覆盖较新的决定；提供刷新与冲突
  反馈」。实测：丢弃后 `3 == 2`（原断言红），而内容值 `summary` 仍是用户值 —— 说明
  红的只是令牌断言。
* 修正（**换成更强的行为断言，不是删掉**）：

  1. 丢弃后**内容值一字不变**：`summary` / `kind` / `aliases` / `attributes` 四项
     全部等于丢弃前（`_content_snapshot()`）；
  2. **其它候选仍在 pending**：数量与内容（field / candidate_value / reason）逐条不变；
  3. 用**丢弃之前**的 `expected_revision` 对另一条候选发 `adopt` → 必须 **409**
     （`conflict=true`、`current_revision` = 丢弃后的令牌），且**一行不改、不改值**、
     其它候选不动 —— 这正是 revision 递增提供的并发保护；
  4. 重复丢弃仍幂等：第二次调用不 500，且 `revision` 不再推进、内容不再变
     （`test_a04_duplicate_dismiss_is_idempotent` 保留）。

  丢弃**不改内容值**这一条没有放宽，反而被断言得更细。

### 本轮集成后实测（2026-10-10，`D:\qio-dev\qio-fu-main`）

后端 V 组共 73 条：**69 绿 / 4 红**，4 条红全部在 `test_fu_verify_a05_close.py`
（A05 不在上述四处修正范围内，本轮未改该文件）。经核实这 4 条**与本次判据修正无关**
（只收集未改动文件 `a02/a05/a06/b01` 时同样红，单独跑 `a05` 也红），且都指向**用例
自身的探针写法**，不是实现缺口：

* `_adapter_of(ctx)`（3 条：`..._normal_reports_clean_and_persists_clean_exit` /
  `..._cooperative_cancel_is_clean` / `..._delayed_cancel_is_clean`）读的是
  `next(iter(ctx._adapter_cache.values()))`，但 `AppContext.aclose()` 在
  `release_adapters` 时执行 `adapters, self._adapter_cache = list(...), {}` ——
  释放后缓存**已经清空**（这正是「释放过的不许再被发出去」的正确行为），于是
  探针在 `aclose()` **之后**读到空缓存 → `StopIteration`。应改成持有
  `_tap()` 播种的那个 `_FakeAdapter` 引用再断言 `.closed is True`。
* `test_a05_state_normal_writes_nothing_after_close`（第 218 行
  `conn.execute = _observe`）→ `AttributeError: 'sqlite3.Connection' object attribute
  'execute' is read-only`：`sqlite3.Connection` 是 C 类型，实例属性不可赋值
  （已单独验证：`sqlite3.connect(':memory:').execute = f` 同样报错），该探针**在本
  Python 上从未可能通过**。要用代理对象/包装连接（或换一个可注入的写库观察点），
  不是改实现。

以上 4 条属 A05 用例的探针缺陷，按分工未在本轮修改（不在允许改动的文件清单内），
已上报 Lead 决定。（→ 这 4 条与前端那 3 条已在 **V3 维护轮**按 Lead 的确定结论修完，
逐条依据见下一节。）

## V3 维护轮 · 五处判据修正（**不是放宽期望**）

上一轮（4 处修正）之后仍有 7 条红：后端 4 条全在 `test_fu_verify_a05_close.py`，
前端 3 条（2 条在 `fu-v-a01-recovery-inbox.spec.ts`、1 条在
`fu-v-a04-candidates.spec.ts`）。Lead 逐条定性为**探针自身的问题**（不是实现缺口）。
本轮的修改只改「怎么观察」：**没有删用例、没有降低任何期望**，其中两条还把断言换成了
更强/更完整的观察。逐条依据如下。

### 修正 5（A05，3 条）· 释放后缓存已清空，断言必须落在「关闭前播种的那个对象」上

* 原探针：`_adapter_of(ctx) → next(iter(ctx._adapter_cache.values()))`，却在
  `await ctx.aclose()` **之后**才去读缓存。
* 依据：`AppContext.aclose()` 在干净释放时执行
  `adapters, self._adapter_cache = list(self._adapter_cache.values()), {}` ——
  「释放过的不许再被发出去」，这是**正确行为**。于是关闭后缓存是空字典，探针读到
  `StopIteration`（实测 `RuntimeError: coroutine raised StopIteration`）。
  红的是探针，不是实现。
* 修正：`_tap()` 播种 adapter 时**返回引用**，在 `aclose()` 之前持有它，关闭后断言
  **那个对象**的 `.closed is True`（不 clean 时仍是 `.closed is False`）。
  断言强度不变；观察点从「缓存里现在有谁」换成「关闭前播种的那个 adapter 是否被关」——
  后者才是契约面（缓存被清空本身就是正确行为，不该被断言成「还留着」）。

### 修正 6（A05，1 条）· 实例属性赋值不可行：改用记账连接子类（选了做法 (a)）

* 原探针：`conn.execute = _observe` → `AttributeError: 'sqlite3.Connection' object
  attribute 'execute' is read-only`。`sqlite3.Connection` 是 C 类型，**实例属性不可赋值**
  （`sqlite3.connect(':memory:').execute = f` 同样报错），该探针在本 Python 上
  从未可能通过。
* 选定做法 **(a)**：`sqlite3.connect(path, factory=_CountingConnection)`，
  `_CountingConnection(sqlite3.Connection)` 覆写 `execute`，把
  INSERT/UPDATE/DELETE/REPLACE 记进 `conn.writes`。连接的既有设置与
  `agent.storage.db.connect()` **逐项一致**：`check_same_thread=False`、
  `isolation_level=None`、`row_factory=sqlite3.Row`、`PRAGMA foreign_keys=ON`、
  `PRAGMA journal_mode=WAL`、`PRAGMA busy_timeout=5000`。观察到的写因此与真实路径同源
  （storage 层只用 `conn.execute` 写，全仓库没有 `conn.cursor()` 写路径）。
* 为什么选 (a) 而不是 (b)（「干净关闭后尝试写入抛 `sqlite3.ProgrammingError`」+ 行数不变）：
  `aclose()` **不负责关数据库**（`close_db_on_shutdown=True` 的关库是 lifespan 的最后一件事，
  见 `api/server.py`），这条用例里 `aclose()` 返回后连接本来就是开着的。要让 (b) 成立，
  探针必须**自己**关掉连接 —— 那观察到的就不再是「关闭完成后还有没有人在写库」，
  而是「被关掉的连接会拒绝写入」。(a) 保留了原意：关闭完成后写语句条数不再增长。
* 保留原断言：`await asyncio.sleep(0.05)` 后 `len(writes) == baseline`；
  **新增一条防空跑断言** `baseline > 0`（关闭前必须真的记到过写），
  否则探针在「一条都没记到」时会静默通过。

### 修正 7（前端 A01，1 条）· 「无错误」的既有表示是空串，不是 null

* 原判据：`expect(store.recoveryError ?? null).toBeNull()`，实测收到 `''`。
* 依据：store 的既有约定就是「无错误 = 空串」——`stores/session.ts` 初始化
  `recoveryError: ""`、每次加载前重置为 `""`；已合并的 W5 用例按 `toBe("")` 断言。
  `'' ?? null` 仍然是 `''`，原判据要求了一个 store 从不使用的表示。
* 修正：断**语义**「没有错误」：`expect(store.recoveryError).toBeFalsy()`，并把约定写进注释。
  没有改生产代码去迎合 null。

### 修正 8（前端 A01，1 条）· 409 的可读原因走**返回值**，不是抛错也不是 recoveryError

* 原判据：只认「抛出的 Error」或 `store.recoveryError` 两条通道。
* 依据（已核对 `components/RecoveryInbox.vue`）：`session.continueRecovery()` 返回
  `RecoveryOutcome{ok, message}`；409 分支 `_failRecovery()` 给出
  「继续这条记录没有完成：这条记录已经被处理过（可能在别处继续或忽略了）…」，
  并且**立刻** `void loadRecoveryInbox()` 重新要一份权威状态 —— 那次刷新会把
  `recoveryError` 重置为 `""`，所以在 `recoveryError` 里读到的是永远空。
  组件 `run()` 在 `outcome.ok === false` 时把 `outcome.message` 写进该条记录的
  inline 错误（`<p class="inline-error">`）并渲染出来，**用户确实看得到原因**。
* 修正：改断返回值通道 —— `ok === false` + `String(res.message)` 匹配
  `/已经被处理过|没有完成/`；并把「记录不得从入口里抹掉」的断言**原样保留**
  （它的实测结果见下方未决项，本组未自行改判据、未改生产代码）。

### 修正 9（前端 A04，1 条）· 判据不能对着原始 html 串做正则

* 原判据：`expect(wrapper.html()).not.toMatch(/已采纳|采纳成功/)`。
* 依据：命中的是 **Vue 模板注释**（`<!-- …也不翻成「已采纳」 -->`，注释会留在
  `outerHTML` 里）。组件实际渲染的是「采纳没有成功：…」，并没有假装成功 —— 是判据假红。
* 修正：改看**可见文本** `wrapper.text()`：保留「候选仍在列表里」（可见文本含候选值），
  **新增正向断言「可见文本里出现『采纳没有成功』」**（原来只有否定断言，失败原因是否
  可读其实没被钉住），再断言可见文本不含「已采纳 / 采纳成功」。期望比原来更强，
  也不再对 `html()` 做那条正则。

### V3 维护轮实测（2026-10-10，`D:\qio-dev\qio-fu-main`，`fix/reliability-memory-followup-20261010-a7f3`）

* 后端 7 个 V 文件：**73 绿 / 0 红**（修正前 69 绿 / 4 红）。
* 前端 2 个 spec：**13 绿 / 1 红**（修正前 11 绿 / 3 红）。

### 裁定 10（前端 A01，1 条）· 409 未被本地遮掉 —— 这是**真实实现缺口**，修的是实现

修正 8 把 409 的观察通道改对之后，立刻暴露出下一个断言为红：

```
AssertionError: 失败不得把记录从入口里抹掉: expected false to be true
实测：ids=[] err="" listCalls=3 pending=false   # 记录已从 store.recoveryRecords 消失
```

* **为什么这不是判据问题**：后端的 409 **不等于「已经被处理过」**。
  `services/recovery.py` 会在好几种情况下回 409 —— 记录状态已经变化（现在是 X）、
  上次的写入者现在还在运行不能接管、孤立记录要先修复再继续。这些情况下这条记录
  **仍然在权威清单里、仍然可操作**。
* **缺口**：`session.ts::_failRecovery()` 对 409 一律 `noteRecoveryResolved(recordId)`
  再做权威刷新，而 `mergeRecoveryInbox()` 会用本地 `_recoveredResolvedIds` 过滤 ——
  于是「刚拉回来的权威结果」被本地猜测遮掉，界面还写着「清单正在按后端最新状态刷新」，
  与事实相反。这正是本轮要消灭的形态：**以不确定为由把记录藏起来**。
* **修法（实现侧，`frontend/src/stores/session.ts`）**：409 分支**不再本地标记 resolved**，
  只重新拉一次权威清单，由后端决定这条还不在不在；提示语改成**原样转述后端给的原因**，
  不再自己编「已经被处理过」。
* **连带更新**（不是放宽）：`fu-w5-recoveryInbox.spec.ts` 里那条用例原来冻结了旧文案
  `res.message` 含「已经被处理过」。现在改成**更强**的断言：mock 一个具体服务端原因
  （「上次的写入者现在还在运行，不能接管」），断言 `res.message` **原样包含它**、
  `res.ok === false`、清单仍要向服务端重新对齐，并且**这条记录不得被本地遮掉**。
* 复跑：`fu-v-a01-recovery-inbox.spec.ts` + `fu-v-a04-candidates.spec.ts` +
  `fu-w5-recoveryInbox/RecoveryRestore/RecoveryInbox` 共 **5 文件 / 50 条全绿**。

**未决的红（本组按「不许放宽期望」原样保留，已上报 Lead 裁定）**

`fu-v-a01-recovery-inbox.spec.ts`「continueRecovery 收到 409 时给出可读原因，并保留这条记录」
里的 `expect(stillPending(store, "turn_legacy_1")).toBe(true)`：

* 探针实测（临时日志，随后还原）：409 之后 `store.recoveryRecords` 里已经没有这条
  （`ids=[]`、`recoveryError=""`、权威清单请求已重新发出）。
* 机制：`session.ts::_failRecovery()` 在 conflict 分支先 `noteRecoveryResolved(recordId)`
  （本地判「已解决」，防止旧快照复活），再 `void loadRecoveryInbox()`；
  `mergeRecoveryInbox()` 用 `_recoveredResolvedIds` 过滤掉它（该集合只追加、仅在超过 200
  条时 shift）。所以「重新要一份真相」拿回来的那条，仍被本地这层过滤遮掉。
* 两种读法（需要 Lead 裁定）：
  1. **探针判据问题**：409 的语义是「已经被处理过」，记录本应离场；已合并的
     `fu-w5-recoveryInbox.spec.ts`「409（已经被处理过）→ 如实说明并重新对齐」只冻结了
     `ok === false` + 可读原因 + 重新拉权威清单，并没有要求记录留下。
     若按此裁定，等价改法是不弱于原断言的「409 之后必须已请求权威清单」。
  2. **实现缺口**：服务端 409 不只有「已经被处理过」——`services/recovery.py` 还有
     「记录状态已经变化（现在是 X），请刷新后重试」「上次的写入者现在还在运行，不能接管」
     「这条记录的重发关联不完整，请先修复再继续」等分支；这些情况下记录**仍在权威清单里**，
     前端却把本地 resolved 过滤套在刚拉回来的权威结果上，且提示语声称
     「清单正在按后端最新状态刷新」——与事实不符。
  这两种读法下的改法完全不同（改探针判据 vs 改 `stores/session.ts`），
  本组按分工不改生产代码，故保持红并上报。

前端 2 个 spec 共 14 条：**11 绿 / 3 红**，同样全部指向**用例探针**而不是前端实现：

* `fu-v-a01-recovery-inbox.spec.ts`「store 暴露收件箱状态与加载动作…」第 211 行
  `expect(store.recoveryError ?? null).toBeNull()` → 收到 `''`。store **既有约定**
  就是「无错误 = 空串」（`stores/session.ts` 初始化 `recoveryError: ""`、加载前
  重置为 `""`；已合并的 W5 用例直接断言 `toBe("")`），`'' ?? null` 仍是 `''`，
  该断言要求了一个 store 从不使用的表示。改成 `toBe("")` / 真值判断即可。
* 同文件「continueRecovery 收到 409 时给出可读原因」第 266–269 行只认「抛出的
  Error」或 `store.recoveryError` 两条通道，但冻结设计里 409 的可读原因走的是
  **返回值**：`session.continueRecovery()` 返回 `{ok:false, message:"…已经被处理过
  （可能在别处继续或忽略了）…"}`，并按注释「409：不猜本地状态，重新要一份真相」
  立刻 `loadRecoveryInbox()` —— 那次刷新会在第 1392 行把 `recoveryError` 重置为
  `""`（所以探针读到空）。已合并的 W5 store 用例正是断言 `res.ok === false` +
  `res.message` 含「已经被处理过」。探针补上返回值这条通道即可。
* `fu-v-a04-candidates.spec.ts`「采纳保存失败（500）不假装成功」第 225 行
  `expect(html).not.toMatch(/已采纳|采纳成功/)` 命中的是 Vue 模板注释
  `<!-- 409 就地提示：不是普通失败，也不翻成「已采纳」 -->`（注释会留在
  `outerHTML` 里）。组件实际渲染的是「采纳没有成功：… 500 …」，**确实没有假装
  成功**。断言应看可见文本（如 `wrapper.text()`）而不是原始 html 串。

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
