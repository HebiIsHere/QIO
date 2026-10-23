# R9-H4 B 报告：统一授权引擎、执行前核对、操作事实与子智能体范围

- 分支：`r9h4/b`（worktree `D:/qio-dev/qio-r9h4-b`）
- 基线：`67fa88c6`（R9-H4 契约 + 迁移 65/66 身份 birth 生命周期）
- 日期：2026-10-24
- 范围：契约 §2/§5/§6/§7/§9 + Lead 补充的 E 身份生命周期接口；独占 `security/engine.py`、`security/recheck.py`、`tests/test_r9h4_engine_binding.py`、本报告。未改 app/effects/tools/migration/其他角色模块。

## 1. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/security/engine.py` | 实际影响/限制匹配重写；绑定失效拒绝；审查后重读；子智能体范围 DENY；E 身份 lookup 接口与内部数据接线兼容 |
| `backend/src/agent/security/recheck.py` | `RecheckResult.verdict` 正确读取 decision；保留调用方 context/extra/scope；模式变化进入新确认；dict 绑定兼容 |
| `backend/tests/test_r9h4_engine_binding.py` | 27 项真实台账/引擎/重核对测试（fake reviewer） |
| `docs/r9h4-reports/B.md` | 本报告 |

## 2. 契约实现

### 2.1 实际影响与限制匹配（§2/§3/§7）

`_limit_conflict(effect, requirement, *, extra_limits=())` 现在：

- 展开 base effect 与 `effect.detail["limit_impacts"]` 的全部实际影响（每项 `{operation, objects:[{kind,value}]}`）；base 操作仍按 EffectKind 映射。
- 覆盖 `read/write/create/delete/execute/install/system_change/egress`；`program` 对象按 `program`/`execution.program`/`resolved_program`/`program_resolved` 实际值匹配（裸名与可执行名主干兼容，如 python vs python.exe）；`service` 按 host 匹配；delete 按 path。
- 目录限制：`directory`/`path_prefix` 命中目录自身及其下对象；目录递归删除的子对象由 C/D 的 `limit_impacts`（delete/path）提供，引擎逐项核对。
- `EffectKind.INTERNAL_DATA` 不再固定当 write：优先 `detail.internal_operation`（read/write/create/delete/execute/install/system_change）；缺失时动作不确定，对象命中任一限制即保守 DENY，不被当作无限制。
- 相对路径：限制值以 `workspace_root` 规范化，effect 相对路径以 `detail.cwd`/`execution.cwd` 规范化；基准不可确定时只做可证明的文本同一匹配，不当无限制。
- `detail.limit_impacts_complete is False`：剩余影响不能忽略；在已检查影响外返回明确 DENY（“实际影响无法完整确定”）。
- 任一实际影响命中有效限制即 DENY；`_verdict`/`authorize_many`/`authorize_batch`/`recheck_approved` 都在内部数据、scope、永久删除、固定查询、模式、审查之前先做限制核对。

### 2.2 绑定要求撤回/替换/版本失效（§5）

- 新增 `_requirement_binding_state(context, effect)`：返回 `ok/missing/invalid`；invalid 含要求不存在、非 active（撤回/替换）、版本不一致、缺 id、台账不可用、版本非法。
- 绑定来源：`context.requirement`/ContextVar，其次 `effect.detail["requirement_binding"]`（后端核实绑定，`requirement_version` 或 `version` 键均兼容）。不回退“最近 active”要求。
- `_verdict`/`recheck_approved`：invalid 且属于要求依赖操作 → 明确 DENY；普通读取、应用受管（internal. 工具 + app_managed）、固定查询保持可用。
- `_apply_review` 初始读要求同样区分 invalid；模型返回后再次读取要求 id/version/status 与当前限制，并重新计算操作指纹（对象身份/程序/argv/cwd/脚本摘要/内容/前置状态/接收方/模式）；任一变化 → APPROVE、不采用旧结果、不写授权记录。

### 2.3 RecheckService（§6）

- `RecheckResult.verdict` 为只读属性，从 `decision.verdict` 读取 allow/approve/deny；修复调用方读取不存在字段导致“需要新确认”被当普通失败。
- 重建 `AuthContext` 保留调用方 `instruction/agent_scope/origin/turn_id/session_id/extra`；额外保留 `untrusted_material` 等原内容依据；沿用调用方同一个 effect 对象。
- 模式变化（binding.mode 与当前模式不同）→ `needs_confirmation` + APPROVE decision，进入现有新确认流程；内容/前置状态变化 → `content_changed`，指纹变化 → `operation_changed`。
- 真实引擎判定仍优先：限制 DENY、unresolved 需要确认、固定查询/普通读取等按引擎结论；不再因字段错读伪造 allow。dict 形式 requirement 绑定兼容。

### 2.4 子智能体范围（§9）

- `_scope_limited` 不再因 unrestricted 跳过；read 也检查范围内路径，写/删除/执行/安装/系统改动/外发检查 path/program/service/limit_impacts 对象。
- 范围不足 → `_verdict` 返回 **DENY**（reason 说明需要主智能体统一申请）；`recheck_approved` 同样 DENY；普通审查/always-ALLOW 不能覆盖。无 scope 上下文时不改变原行为；引擎不自行扩权/不冒充主智能体授予新范围。

### 2.5 E 身份生命周期接口（Lead 补充）

构造函数新增（均可选，缺省 None）：

```python
AuthorizationEngine(
    internal_data_identity_lookup=store.match_identity,      # E: (identity, birth_key=None) -> dict
    internal_data_identity_tokens=store.protected_tokens,   # 或 store.active_tokens
    internal_data_identity_keys=..., internal_data_known_paths=...,
)
```

public 方法（供 Lead 修改 `security_gate.match_internal_data` 复用）：

```python
engine.internal_data_identity_lookup(identity_key, birth_key=None) -> Any   # 透传 store.match_identity 结果
engine.internal_data_identity_tokens() -> frozenset[str]                    # 注入 provider 优先，否则退回 active keys
engine.internal_data_identity_known_paths() -> frozenset[str]               # 历史登记路径
```

- 内部数据判定 `_internal_identity_kwargs()` 传 `identity_keys/known_paths/identity_lookup` 给 `internal_data.effect_targets_internal_data`；E 未合入（旧签名不接受 identity_lookup）时 TypeError 回退**保留 keys/known** 重试，不丢身份登记。
- `effect.detail` 消费键：`limit_impacts`、`limit_impacts_complete`、`internal_operation`、`program`/`resolved_program`/`program_resolved`、`execution`、`requirement_binding`。

## 3. 测试与结果

`tests/test_r9h4_engine_binding.py`（27 passed，真实 RequirementLedger/AuthorizationLedger/AuthorizationEngine/RecheckService；fake reviewer；未联网、未真实 Key）：

- 限制优先：limit_impacts 多影响、程序限制、命令删除对象、目录/子目录、read、INTERNAL_DATA internal_operation（write 不被 read 限制误杀；read 命中；未知动作保守拒）、相对路径 workspace/cwd、limit_impacts_complete=False、resolved_program。
- 绑定失效：撤回/版本不一致/不存在/缺 id → unrestricted/restricted 明确 DENY；普通读取保持可用；reviewer 0 次；D 形状 effect（limit_impacts + effect.detail requirement_binding）DENY；无绑定不套最近 active。
- 审查后变化：等待期间要求撤回/amend 版本变化/限制更新/内容摘要变化/对象身份变化 → APPROVE、无授权记录；正常 ALLOW 仍保存记录。
- Recheck：deny/approve/allow 的 `verdict` 正确读取；保留 scope/extra/turn/session/effect 对象；模式变化 needs_confirmation；content_changed 不误报 allow。
- 批量：已知限制只 DENY 命中项、A 仍可执行；scope 批量逐项；scope 不足 restricted/unrestricted 都 DENY 且不进入审查。
- E 接口：lookup/tokens/known_paths public 方法；identity_lookup 传给 internal_data；旧签名回退保留 keys。

定向命令（cwd=backend，`uv run --frozen pytest`；完整 CI 未触发）：

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_r9h4_engine_binding.py` | **27 passed** |
| 核心定向（本测试 + r9h_limits + r9h3_limits_priority + r9h_recheck_limits + r9h2_internal_identity + r9h_internal_data + internal_data_guard + r9g_engine_core + authorization_engine + r9f_review） | 136 passed + 2 个既有断言需按 H4 更新（见 §5） |
| 宽定向（再加 binding_flow/r9h2_parse_state/chain_recovery/subagent_scope/r9g_deletion/cmd_binding/approval_lifecycle/restart） | 249 passed + 上述同类 3 个既有断言需更新 |

## 4. 与契约差异/边界

- **A 的 read 限制规范化当前 B 基线未合入**：测试 read/internal_operation 用真实引擎 + 真实台账 id/version 视图承载已保存限制；A 合入后建议 Lead 用真实台账补一条端到端（A 已合入 main，集成后可直接复验）。
- **D 两条 xfail 用例现状**：当前 D 测试构造的 effect 没有 `requirement_binding`（也没有 ctx），引擎按契约 §2/§5 不回退“最近 active”，因此不会套用 `_LimitLedger.current()`；需要 D/Lead 在执行入口或测试中把真实绑定放入 effect.detail/ctx（B 已支持 effect.detail 绑定，并有用例证明 D 形状 DENY）。这是集成时需要明确对齐的一点，不是 B 引擎可安全猜测的。
- 未改 app/effects/security_gate/approved_operations 等共享文件；app 接线参数见 §2.5。
- `limit_impacts_complete=False` 时对任何存在限制的操作保守 DENY；如产品希望仅对可确定覆盖的影响拒绝，需要契约进一步明确（当前按“不能忽略剩余影响”处理）。

## 5. 需要集成时更新的既有测试（非本分支独占文件，我未改）

以下旧断言编码的是 H4 之前的行为，按 H4 §5/§9 契约应更新：

- `tests/test_r9g_engine_core.py::test_withdrawn_requirement_binding_is_invalid`：旧断言 APPROVE+review；H4 要求绑定失效明确拒绝（DENY）。
- `tests/test_authorization_engine.py::test_subagent_cannot_expand_parent_scope`：旧断言 scope 不足 APPROVE；H4 要求 DENY。
- `tests/test_subagent_scope.py::test_engine_child_scope_really_bounds_subagent_effects`：旧断言项目外写 APPROVE；H4 要求 DENY。

## 6. 未完成/未验证

- 未触发完整 CI/全量矩阵（累计预算 2/2 已用尽，按契约只做定向）；未跑本地全量。
- 未在 POSIX 真机跑（定向测试在本机 Windows；路径规范化/身份接口平台行为由 E/C 与集成 CI 覆盖）。
- E 的 birth 生命周期端到端（真实 InternalDataIdentityStore.match_identity 注入 app）由 Lead 接线后验证；B 已提供接口与旧签名兼容回退。
- app/approved_operations/security_gate 接线未由 B 修改；需要 Lead 按 §2.5 注入。

## 7. 合并准备

- 只提交本 worktree 分支，不 push/不 merge main；等待 Lead 集成与定向 CI。
- 合并后重点跑：D 的 cmd_program 两条（绑定来源修正后）、A 的 read 限制端到端、E 身份生命周期套件、F 审批恢复套件。
