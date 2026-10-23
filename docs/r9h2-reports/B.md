# R9-H2 B 报告：解析失败状态 / 台账状态 / 授权判定一致性

- 分支：`r9h2/b-state`（worktree `D:/qio-dev/qio-r9h2-b`）
- 基线：`a6732f4c`（R9-H2 契约 + 迁移 61/补偿 62）
- 已按 Lead 指示合并 `r9h2/main`（C 分支身份模块/迁移纠正）到本分支；合并后基线 `1943baf4`。
- 日期：2026-10-23
- 范围：契约 §4（解析失败状态一致性 + 引擎/recheck 一致性）+ §5.2 app/engine 接线；不改 A/C 独占模块。

## 1. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/services/app.py` | 解析失败 fallback 显式 unresolved/source；不再立即历史重解释；`_ensure_legacy_structured_limits` 重写（A 链接口 + 兼容路径、条件写回、返回本轮新限制）；内部数据身份登记创建/sync/refresh/引擎注入 |
| `backend/src/agent/security/engine.py` | `_apply_review` 与 `_permanent_delete_decision` 在 legacy/unresolved 时禁止复用旧 allow 记录；新增内部数据身份 callable 注入并用于 `_verdict`/`recheck_approved` |
| `backend/src/agent/security/recheck.py` | `RecheckResult.requirement_state`（从台账重新加载的状态）+ 各结论回填；重建 AuthContext 继续携带持久化限制/解析状态 |
| `backend/tests/test_r9h2_parse_state.py` | 新增 16 项真实 `_execute_turn`/真实台账/真实引擎用例（fake 解析器/审查器） |
| `docs/r9h2-reports/B.md` | 本报告 |

## 2. 契约 §4 实现

### 2.1 解析失败状态

- `app._prepare_requirement_binding`：解析器缺失 → `resolution_missing`；resolve 抛错（超时/非法输出等）→ 异常类名；resolve 返回 None（输出非法/为空）→ `invalid_output`。均记录 notice/审计。
- fallback 调用 `_record_unresolved_requirement`：
  - A 分支 `record_user_message(..., structured_state=unresolved, structured_source=resolution_unavailable:<类>)` 存在时直接写入；
  - A 未合入时退回 `record_user_message` + `set_structured_limits(state=unresolved, source=...)`，再不行才兼容直写台账状态（绝不留下 structured/[]）。
- **不再**在失败 fallback 立即调用 `_ensure_legacy_structured_limits`，避免把失败消息马上重解释成 structured；返回 `status=unavailable`。
- 成功解析 new/amend：apply_resolution 已是 structured/unresolved；只有 legacy/unresolved 才进入历史恢复。

### 2.2 历史恢复（A 接口 + 兼容）

- A 接口存在（`RequirementLedger.recovery_context` + resolver `extract_limits_from_context`）：
  1. `complete=False` → 不调模型，保持原状态（已有 known limits 保留），notice；
  2. 调 `extract_limits_from_context(context, requirement_id=..., version=...)`（同步/异步兼容）；
  3. 返回后重新读 requirement/recovery_context，按 id/version/status/parent_id/chain_digest 核对，任一变化丢弃、不写；
  4. setter 传 `expected_version/expected_status=active/expected_chain_digest`（A setter 支持时），source=`prompt;chain=<digest>;version=<n>`；
  5. 成功：重读 requirement 得到新 limits 返回本轮使用；失败：保留 known limits 写 unresolved，不覆盖；
- A 未合入（当前合并树）：兼容路径只喂当前要求原话（行为与 R9-H 相同），成功 source=原 prompt version，失败 source 以 `extract_limits_failed:<类>` 开头并写 unresolved/保留 known。

### 2.3 engine / recheck

- `_apply_review`：`_limits_unresolved(requirement)` 为真时跳过 `authorizations.reuse`，即使 fingerprint 命中也不复用；随后走当前审查/审批。
- `_permanent_delete_decision`：同样跳过旧 `delete_permanent` allow 记录复用，返回 APPROVE 要求明确确认。
- `_verdict` 原有规则保留：unrestricted + unresolved + 相关操作 → APPROVE（不自动放行）；restricted 正常审查；read_only 固定规则不变。
- `recheck.py`：`_bound_requirement` 从台账重新加载 requirement；`RecheckResult.requirement_state` 返回 structured/legacy/unresolved；重建 AuthContext 继续带 structured limits/解析状态，unresolved 不会因重建上下文变成无限制。

### 2.4 内部数据身份接线（C 已合入）

- app 创建 `InternalDataIdentityStore(conn)`，启动 `sync(self._internal_data_paths(), source=app_startup)`；失败只告警并记录 `internal_data_identity_error`（路径判定兜底，不静默降级）。
- engine 新增 `internal_data_identity_keys` / `internal_data_known_paths` callable；`_verdict` 与 `recheck_approved` 经 `_effect_is_internal` 传给 `internal_data.effect_targets_internal_data(identity_keys/known_paths)`；C 未合入时 TypeError 退回路径判定。
- app 提供 `_internal_identity_keys()`、`_internal_identity_known_paths()`、`refresh_internal_data_identities(path=None)`（刷新失败保留已有登记）。
- **未改 C 模块**；身份读取/登记/退役语义由 C 负责。

## 3. 测试与结果

`tests/test_r9h2_parse_state.py`（16 项，全部通过；fake resolver/reviewer，真实 `_execute_turn`/台账/引擎）：

1. 解析失败四类（异常/超时/非法输出 None/解析器缺失）→ state=unresolved、source=`resolution_unavailable:<类>`、临时 status=unavailable；unrestricted 写操作 APPROVE、读 ALLOW。
2. 成功解析明确无限制 → structured/[]，unrestricted 写 ALLOW。
3. 失败但用户原话明确禁止 B → unresolved 时 unrestrict 写 B 不自动允许；历史恢复成功写 structured 后 B DENY、A ALLOW。
4-6. A 链恢复：完整链条件写回（expected_version/status/chain_digest、source 含 prompt/chain/version）；返回后链变化丢弃且不写；失败保留 known limits 写 unresolved。
7. unresolved 禁止复用旧 write allow 记录（reviewer 被调用）；旧 delete_permanent allow 记录也不能绕过（APPROVE）。
8. 重启（新 RequirementLedger 实例）仍 unresolved；RecheckService 重建上下文返回 needs_confirmation 且 requirement_state=unresolved；重启后新引擎仍不复用旧 allow。
9. 无关要求/无依赖工作不受影响（structured 无限制 write ALLOW）。
10. A 分支 `record_user_message(structured_state=...)` shim：fallback 直接写 unresolved/source。
11. structured 明确限制仍可按 fingerprint 复用 allow 记录（证明只有 legacy/unresolved 禁止复用）。
12. app 已创建内部数据身份登记并注入引擎；只命中身份（移出注册路径）仍按内部数据三次确认（unrestricted 下 APPROVE）。

定向命令与结果（cwd=backend，`uv run --frozen pytest`；未跑全量/CI）：

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_r9h2_parse_state.py` | **16 passed** |
| 综合定向：parse_state + binding_flow + r9h2_internal_identity + r9h2_state_migration + internal_data_guard + r9h_internal_data + r9g_engine_core + r9h_engine_wiring + r9h_content_binding + approval_lifecycle/restart/persistence/binding + r9h_recheck_limits | **125 passed** |
| 早期定向：binding_flow/limits/state_migration/internal_identity/r9g_engine_core/approval_lifecycle/approval_restart | 72 passed + 1 基线既有红（`test_r9h_limits.py::test_migration_59_and_compensation_60_real_db`：断言 SCHEMA_VERSION==60，合并树迁移已到 62；属 Lead 迁移/测试集成范围，本分支未改） |
| 剩余 r9h 套件（content_binding/engine_wiring/fixed_queries_platform/recheck_limits/limits/r9g_fixed_queries） | 67 passed + 同上 1 个迁移版本红 |

模型调用全部 fake/mock；未触发全量 pytest/CI。

## 4. 与契约偏差 / 兼容说明

- **A 接口未合入当前合并树**：app 按 `getattr` 兼容：有 `recovery_context`+`extract_limits_from_context` 才走链恢复；否则走只喂当前原话的兼容路径。链恢复路径用 `ChainLedgerShim` 测试覆盖条件写回/竞态/失败保留；Lead 集成 A 后无需改 app。
- **旧测试断言 source 精确等于 prompt_version**（`tests/test_r9h_binding_flow.py::test_legacy_requirement_is_reunderstood_and_persisted`）：当前兼容路径保持旧 source 以不破坏既有测试；A 合入后新路径 source 含 `;chain=...;version=...`，该旧断言需 A/Lead 在集成时按新契约调整（非本分支独占测试，未修改）。
- `RecheckResult.requirement_state` 为只追加字段，不影响旧调用方。
- 内部数据身份接线只在 C 模块可导入时创建；C 模块不可用时只告警，路径判定兜底（不静默把已注册对象当普通文件之外的新放行）。

## 5. 未完成 / 未验证（诚实清单）

- 未跑本地全量 pytest/完整 CI（按纪律与预算由 Lead 统一）。
- A 真实分支合入后的端到端链恢复（真实 resolver 模型输出）未在本分支验证（仅 shim + fake resolver）；Lead 集成后用 A 测试覆盖。
- 内部数据身份的真实硬链接/移动/改名行为由 C 分支测试覆盖；B 只验证 app 创建/sync/注入与引擎按身份命中。
- fallback 兼容直写台账状态分支（A setter 与 record_user_message 新参数同时缺失）未在真实 DB 上强制触发（基线 setter 存在）；该分支仅作极端兼容兜底。
- 未修改 `tests/test_r9h_limits.py` 的迁移版本断言（Lead 迁移职责）；该红非本次代码引入。

## 6. 给 Lead 的合并提示

- 合并顺序：本分支已包含 r9h2/main 合并；直接合入 r9h2/main 即可。
- A 合入后请用真实 `recovery_context`/`extract_limits_from_context` 跑一遍本文件第 3 节的链恢复用例思路；兼容路径会自动让位（getattr 判定）。
- `tests/test_r9h_binding_flow.py` 的 source 精确断言与 `test_r9h_limits.py` 迁移版本断言需要集成时统一适配（契约已变更）。
