# R9-H3 A 报告：已知限制优先级与执行前核对

> 基线：`8d97904f`（r9h3/main 起点：冻结补修契约 + 迁移 63/补偿 64）
> 分支：`r9h3/a-limits`；worktree：`D:\qio-dev\qio-r9h3-a`
> 本轮**未触发完整 CI / 全量矩阵**（累计预算 2/2 已用尽）；只做定向测试。
> 模型调用全部 fake/mock；未 push、未 merge main、未改历史迁移、未删除/skip/xfail 既有测试。
> 未改 review.py/app.py/internal_data_identity.py（B/C 负责）。

## 0. 结论摘要

| 项 | 结果 |
| --- | --- |
| 问题 1：unresolved/legacy 已保存可靠限制时，先命中"限制未恢复"分支，模型审查 ALLOW 后可修改 B | **已修复**：`_verdict` 先取要求+有效限制（含 legacy/unresolved 的 known limits、context resolution_limits）并先做 `_limit_conflict`；命中即 DENY，先于内部数据/范围/审批/模式/审查 |
| 问题 2：unrestricted 仍调用 targeted 针对性审查 | **已移除**：authorize/authorize_many 删除 unresolved_review 分支；`_apply_review` 删除 targeted 参数与 request 字段调用；unrestricted unresolved 最终只返回具体确认（APPROVE），不调用 ModelAuthorizationReviewer |
| 执行前核对 | `recheck_approved` 先最新有效限制（命中 DENY），再 unresolved/内部数据/永久删除/模式；旧审批/旧 allow/内部数据确认不能覆盖有效禁止 |
| RecheckService | 重建 AuthContext 时把**已知限制**（含 legacy/unresolved）传入 `resolution_limits`，执行前重核对读取最新持久化限制 |
| 内部数据 effect 绕过 | `_LIMIT_OPERATION_NAMES` 增加 `EffectKind.INTERNAL_DATA -> "write"`：已知"禁止修改 B"命中内部数据 target 时 DENY；未命中的内部数据仍走三次确认（APPROVE） |
| 批量 | authorize_many/authorize_batch 共用 _verdict/_limit_conflict，逐项独立；只有 B 子项 DENY |
| 定向测试 | A 新文件 **11 passed**；相关既有回归 **122 passed**（含 2 个经 Lead 授权的 H2 过期断言适配，见 §5.2） |
| 跨范围修改 | 经 Lead 明确授权，更新 2 个因 H3 契约过期的 H2 断言（`tests/test_r9h2_parse_state.py` 1 个、`tests/test_r9h2_real_flow.py` 1 个）为 unrestricted unresolved → APPROVE/reviewer=0；不删除测试、不放宽其它断言 |

## 1. 交付文件

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| `backend/src/agent/security/engine.py` | 修改 | _verdict 限制优先重排；删除 unrestricted unresolved_review/targeted 路径；_apply_review 去掉 targeted 参数/请求字段；recheck_approved 限制优先；INTERNAL_DATA 限制操作映射 |
| `backend/src/agent/security/recheck.py` | 修改 | 执行前重建上下文时传入最新已知限制（legacy/unresolved 不丢弃） |
| `backend/tests/test_r9h3_limits_priority.py` | 新增 | 11 个用例：真实 ledger/engine/RecheckService，fake reviewer 计数 |
| `backend/tests/test_r9h2_parse_state.py` | 跨范围适配（Lead 授权） | 仅更新 1 个 H2 过期断言：unrestricted unresolved → APPROVE、reviewer.calls==0；旧 allow 不能复用/状态不丢等断言保留 |
| `backend/tests/test_r9h2_real_flow.py` | 跨范围适配（Lead 授权） | 更新同名 H2 测试（用例名改为 H3 specific_confirmation_not_independent_review）：B/A 均 APPROVE、read ALLOW、reviewer.calls==0；unresolved/source 等有效断言保留 |
| `docs/r9h3-reports/A.md` | 新增 | 本报告 |

未改：`review.py`、`app.py`、`internal_data.py/internal_data_identity.py`、schema/迁移、其他测试。

## 2. 判定顺序（契约 §2.1，冻结）

### 2.1 AuthorizationEngine._verdict（authorize/authorize_many/authorize_batch 共用）

```text
0) requirement = _requirement_for(context)
   extra_limits = context.extra["resolution_limits"]
1) conflict = _limit_conflict(effect, requirement, extra_limits)
   → 命中：DENY（不调用模型审查、不复用旧 allow、不进入内部数据确认/普通审批）
2) 内部运行数据特殊规则（读/写三次确认）
3) 子智能体范围
4) 永久删除政策（已知限制已在 1 先 DENY）
5) 已验证固定查询
6) 未恢复部分：unrestricted + legacy/unresolved + 相关操作 → APPROVE（具体确认），
   不调用 ModelAuthorizationReviewer；restricted 继续走原审查/审批
7) 三种模式（read_only/unrestricted/restricted）
```

要点：
- `_limit_conflict` 读取 `requirement.structured_limits`（不因 state=legacy/unresolved 跳过）并合并 context.extra resolution_limits；
- 限制只覆盖其 operations/objects：范围外对象不会因局部限制变 DENY；
- 不再有 targeted/unresolved_review 分支；普通独立授权审查只在 restricted（_apply_review 只在 restricted 调用）。

### 2.2 recheck_approved（执行前核对）

顺序：最新有效限制（命中 DENY）→ 未恢复部分（unrestricted 相关操作 APPROVE）→ 子智能体范围 → 内部数据确认次数 → 永久删除（旧 allow 复用已在限制之后，且 unresolved 时仍禁用复用）→ 固定查询 → 模式。
旧审批/旧 allow/模式/内部数据确认都不能覆盖有效禁止。

### 2.3 RecheckService

`RecheckService.recheck` 重建 `AuthContext` 时：
* 从台账最近一次 `by_id` 读取当前要求（含最新 structured_limits）；
* `resolution_limits` 传入**已知限制**（legacy/unresolved 也不丢弃），`requirement_resolution_status` 仍按 structured/unavailable 标注；
* 之后调用 engine.authorize（其 _verdict 先限制后其他），执行前新增/更新限制会立即 DENY。

### 2.4 内部数据 effect 的限制覆盖（Lead 补充要求）

`_LIMIT_OPERATION_NAMES` 增加 `EffectKind.INTERNAL_DATA: "write"`：
- `INTERNAL_DATA` effect 的 path target 按 write 限制核对；"禁止修改 B"命中即 DENY（reviewer 0），先于内部数据三次确认；
- 未命中的内部数据 effect 仍走原三次确认（unrestricted APPROVE）；
- 不按 detail 猜测操作类型，固定映射 write（内部数据维护语义）。

## 3. 真实流程验证（A 新测试）

`tests/test_r9h3_limits_priority.py`（11 用例，全部真实 RequirementLedger/AuthorizationLedger/AuthorizationEngine/RecheckService，FakeReviewer 计数）：

1. `test_unresolved_known_limit_denies_before_internal_and_review`：unresolved+known"禁止修改 B"：写 B DENY、内部数据 B DENY、reviewer 0；范围外普通写 A APPROVE、内部数据 A 三次确认 APPROVE。
2. `test_structured_known_limit_denies_even_always_allow_reviewer`：restricted + always-ALLOW reviewer，写 B 仍 DENY，reviewer 0。
3. `test_unrestricted_unresolved_has_no_reviewer_but_restricted_does`：unrestricted 重复判定 APPROVE、reviewer 0；restricted 原审查被调用 1 次。
4. `test_old_allow_and_manual_approval_cannot_override`：旧 allow 记录 + 人工批准 recheck 均不能覆盖，DENY。
5. `test_permanent_delete_old_allow_cannot_override`：已知 delete 限制 + 旧永久删除 allow 记录/审批恢复 → DENY。
6. `test_internal_data_confirmation_cannot_override_known_limit`：内部数据三次确认（execution recheck）仍 DENY。
7. `test_apply_resolution_removes_limit_then_allows`：真实 apply_resolution amend 明确解除后新版本 structured/[]，写 B ALLOW，reviewer 0。
8. `test_authorize_batch_only_b_denied`：批量 [写 A, 写 B] 仅 B DENY，A 非 DENY；authorize_many 整体 DENY 且 reason 为 B 限制。
9. `test_recheck_service_uses_latest_limits_after_update`：绑定后新增限制，第一次 recheck ok，第二次 DENY。
10. `test_recheck_uses_legacy_known_limits`：legacy state + known limits（历史行）→ recheck DENY，requirement_state=legacy。
11. `test_engine_source_has_no_targeted_review_branch`：源码无 targeted_limits_review/unresolved_review，含 INTERNAL_DATA→write 映射。

## 4. 契约要求对照

| 契约 | 实现/证据 |
| --- | --- |
| §2.1 先计算要求+有效限制、先 _limit_conflict | _verdict 第 0/1 步；测试 1/2/4/5/6 |
| legacy/unresolved 使用 known limits、context resolution_limits 同 | _limit_conflict 直接读 structured_limits；recheck.py 传 known；测试 1/10 |
| 命中 DENY 不调模型审查/不复用旧 allow/不进内部数据确认/普通审批 | 测试 1/2/4/5/6；reviewer 计数 0 |
| 内部数据/范围/永久删除/固定查询/模式之后处理 | _verdict 步骤 2-7 |
| 未恢复部分 unrestricted APPROVE 具体确认、不调普通独立授权审查 | 测试 1/3；authorize/authorize_many 删除 unresolved_review |
| recheck_approved 先最新限制 | 实现步骤 1；测试 4/5/6 |
| authorize_many/authorize_batch 同一顺序逐项 | 测试 8 |
| 永久删除旧 allow 复用不得绕过限制 | 测试 5（且 _permanent_delete_decision 在 unresolved 时本来禁复用） |
| 内部数据 effect 不被特殊确认绕过（Lead 补充） | 映射 INTERNAL_DATA→write；测试 1/6 |
| 不新增关键词/名称判断 | 未新增；限制匹配沿用结构化 operations/objects |
| 旧 targeted 授权记录迁移撤销 | Lead 迁移 63/64 范围，A 未改；迁移后记录不可复用由 AuthorizationLedger 保证 |

## 5. 测试与验证记录

### 5.1 命令与结果

```powershell
cd D:/qio-dev/qio-r9h3-a/backend
uv sync --frozen --extra dev

# A 新测试
uv run --frozen python -m pytest tests/test_r9h3_limits_priority.py -W ignore
# => 11 passed in 3.41s

# 相关既有定向回归
uv run --frozen python -m pytest tests/test_r9h3_limits_priority.py tests/test_r9h2_parse_state.py tests/test_authorization_engine.py tests/test_r9h_recheck_limits.py tests/test_r9h_limits.py tests/test_security_modes.py tests/test_approval_lifecycle.py tests/test_approval_restart.py tests/test_approved_operations.py tests/test_r9h2_real_flow.py tests/test_r9g_engine_core.py -W ignore
# => 122 passed in 48.19s

# 编译
uv run --frozen python -m py_compile src/agent/security/engine.py src/agent/security/recheck.py tests/test_r9h3_limits_priority.py
# => OK
```

### 5.2 跨范围测试适配（Lead 明确授权，非失败）

H3 删除 unrestricted targeted 审查后，两个 H2 旧用例的行为预期与冻结契约冲突。经 Lead 明确授权，A 只更新旧行为断言，不删除测试、不放宽其他断言：

1. `tests/test_r9h2_parse_state.py::test_restart_and_recheck_preserve_unresolved_state`：
   - 旧：unrestricted + unresolved 经针对性审查 ALLOW、reviewer.calls==1；
   - 新：APPROVE（具体确认）、reviewer.calls==0；重启/状态不丢/旧 allow 不能复用等断言保留。
2. `tests/test_r9h2_real_flow.py::test_parse_failure_unresolved_uses_specific_confirmation_not_independent_review`：
   - 用例名/说明改为 H3"不再独立审查"；旧：B DENY / A ALLOW / reviewer>=2；
   - 新：B/A 均 APPROVE（具体确认）、read ALLOW、reviewer.calls==0；
   - requirement unresolved/source、解析失败 status 等断言保留。

原因：H3 §3.1 冻结"完全无限制模式不再调用 ModelAuthorizationReviewer；unresolved 未恢复部分返回具体确认"，旧 H2 断言描述的是被删除的行为。

### 5.3 完整 CI 预算

累计完整 CI 2/2 已用尽；本轮**未触发** ci.yml、未触发任何全量矩阵；只跑上述文件级定向测试。

## 6. 与冻结契约的偏差 / 边界

1. **INTERNAL_DATA 固定映射 write**：按 Lead 补充要求"映射为 write（或依据 detail 明确操作选择，不猜测）"，A 选择固定 write，不读取/猜测 detail 操作类型；若后续需要区分内部数据 delete/execute 限制，需要契约明确 detail 字段并由 Lead 重新冻结。
2. **unresolved 具体确认放在固定查询/永久删除之后**：按 §2.1 冻结顺序"最后处理尚未恢复部分"；因此 unrestricted + unresolved 的永久删除/固定查询仍按既有专项规则返回（固定查询 ALLOW、永久删除 ALLOW），已知限制已先 DENY。若要求"未知限制一律阻止永久删除"，需要契约进一步明确（A 未擅自扩大）。
3. **authorize_many 的 restricted 审查未额外加 permanent-delete pending 判定**：保持既有实现（本次只按要求统一限制优先/移除 unrestricted 审查）；永久删除专项在 _verdict 已返回 APPROVE，authorize 路径已有 pending 防护，authorize_many 的既有差异未改，避免扩大变更面。
4. **跨范围测试适配**：`tests/test_r9h2_parse_state.py` 与 `tests/test_r9h2_real_flow.py` 各 1 个 H2 过期断言经 Lead 明确授权改为 H3 行为（APPROVE/reviewer=0），未删除测试、未放宽其他断言；real_flow 用例名改为反映"H3 不再独立审查"。
5. **不新增关键词/名称判断**：内部数据映射只做 kind→"write" 语义映射，不做文字/名称判断。

## 7. 未完成 / 未验证

| 项 | 状态 |
| --- | --- |
| H2 旧断言适配 | 已完成（Lead 授权）：`test_r9h2_parse_state.py` 1 处、`test_r9h2_real_flow.py` 1 处；其余断言保留，定向回归 122 passed |
| B 的 review.py 字段删除（targeted_limits_review 定义/校验） | B 负责；A 已删除 engine 调用，兼容现有字段默认值；B 删除后仍应通过 |
| app.py 消费/真实 E2E | B/Lead 集成验证（A 未改 app） |
| 完整 CI / 全量 pytest | **禁止**（预算 2/2 用尽）；未运行 |
| 真实模型 | 未运行（fake/mock） |
| 迁移 63 撤销 targeted 记录后的旧记录复用 | Lead 迁移范围；A 的旧 allow 不可覆盖测试使用真实 AuthorizationLedger，迁移后可复测 |
| 非 Windows / 并发 | 未验证 |
| 审批恢复/重启的完整端到端 | 仅 engine 方法级 + 定向既有测试；E2E 由 Lead/B |

## 8. 复现命令

```powershell
cd D:/qio-dev/qio-r9h3-a/backend
uv run --frozen python -m pytest tests/test_r9h3_limits_priority.py -W ignore
uv run --frozen python -m pytest tests/test_r9h2_parse_state.py tests/test_authorization_engine.py tests/test_r9h_recheck_limits.py -W ignore
```
