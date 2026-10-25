# R9-H A 报告：要求绑定、真实用户原话与主流程接线

> 基线：`39c0f48e`（r9h/main 起点：冻结契约 + 引擎内容绑定/名称清理/结构化限制读取预接线）
> 分支：`r9h/a-binding`；worktree：`D:\qio-dev\qio-r9h-a`
> 本轮只做定向测试；未跑全量 pytest / 完整 CI（按纪律由 Lead 统一各最多两次）。
> 模型调用全部为 FakeAdapter/mock，未使用真实 Key；未 push、未 merge main、未改历史迁移、未删除/跳过/放宽既有测试。
> 未改 requirements.py（C 负责）、engine.py（Lead）、review.py/工具（B）。

## 0. 结论摘要

| 项 | 结果 |
| --- | --- |
| 问题 1：要求解析成功后主流程拿不到绑定 | **已修复**：`AppliedResolution.requirement` 才是真实要求；new/amend 用 `requirements.binding_for(...)` 取得有效版本绑定；不再读不存在的 `applied.requirement_id/id` |
| 真实用户原话 | `apply_resolution(message=instruction.text, message_id=..., turn_id=..., topic_id=ctx.initial_topic)`；模型 corrections 不替代原话（单测用被改写的 corrections 验证） |
| withdraw | 不绑定已撤回要求（binding=None）；旧要求状态 withdrawn、旧授权 reuse 为 None |
| binding 建立失败 | 明确记录（日志 + 轮 notices + `requirement_resolutions` 审计）并置 `resolution_status=unavailable`；**不回退登记另一条要求**，不静默成功 |
| 解析失败 | 按新要求登记并标 unavailable；本轮绑定指向新要求，不扩大旧授权（既有行为保留） |
| §6 历史限制重新理解 | `resolution.py` 新增 `extract_limits()` + 提示词；app 对 legacy/unresolved 要求用真实原话重新理解并调用 C 的 `set_structured_limits` 持久化；失败标 unresolved、不阻塞绑定、不当无限制 |
| `removed_limits` | `ResolutionOutcome.removed_limits: tuple[dict,...] = ()`；解析/校验支持（仅 amend 可非空）；提示词明确"明确解除才输出、不得清空未解除限制" |
| 定向测试 | **218 passed, 1 failed**；唯一失败 `test_authorization_engine.py::test_read_only_still_denies_credential_reads` 经 git stash 验证为**基线既有红**（与 A 改动无关，见 §7.3） |
| 真实模型评估 | 本轮未运行（纪律：不使用真实 Key）；接口/提示词单测用 FakeAdapter 覆盖 |

## 1. 交付文件

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| `backend/src/agent/services/app.py` | 修改 | `_execute_turn` 要求绑定重写；`_prepare_requirement_binding` / `_ensure_legacy_structured_limits` 等辅助；真实原话/来源传递；失败显式化 |
| `backend/src/agent/security/resolution.py` | 修改 | `ResolutionOutcome.removed_limits`；limits/removed_limits 共用校验；`extract_limits()` + 渲染/解析函数；提示词版本属性 |
| `backend/src/agent/prompts.py` | 修改（仅要求解析段） | 解析提示词支持 removed_limits；新增 `LIMIT_EXTRACTION_*`（历史限制重新理解）；版本 `r9h-req-resolution-v1` / `r9h-limit-extraction-v1` |
| `backend/tests/test_r9h_binding_flow.py` | 新增 | 22 个用例：真实 `_execute_turn()` + 真实台账/版本绑定；new/amend/withdraw/失败/取消/历史限制/解析器 API |
| `docs/r9h-reports/A.md` | 新增 | 本报告 |

未改：`requirements.py`、`engine.py`、`review.py`、工具、`context.py`、`gateway.py`、schema/迁移。

## 2. 问题 1 根因与修复（契约 §3）

### 2.1 根因

基线 `_execute_turn` 的绑定逻辑（Lead 预接线）有三处错误：

1. 从 `applied` 读 `requirement_id` / `id` —— `AppliedResolution` 没有这两个字段，真实要求是 `applied.requirement`，所以成功路径永远拿不到 id；
2. 只有 `resolution is None`（解析失败）才回退登记新要求，成功路径没有绑定也不登记 → `requirement_binding=None`；
3. 在检查绑定**之前**就把 `resolution_status` 置为 `ok`，绑定失败/异常仍标成功；`apply_resolution` 调用也没传 `message/topic_id`，模型 corrections 可能替代真实原话。

### 2.2 修复后行为（`_prepare_requirement_binding`）

- `new/amend`：取 `applied.requirement` → `requirements.binding_for(requirement.requirement_id)` → 校验 `binding.version == requirement.version`；通过才 `status=ok`。
- `withdraw`：不绑定；若 `applied.requirement.status` 仍为 active（台账异常）→ 明确失败；否则 `status=ok`、binding=None。
- 任何"应有绑定却失败"（requirement 为空 / binding_for None / 版本不一致 / 未知 relation）：
  - 写日志；向 `ctx.notices` 追加可见提示；
  - 调 `requirements.record_resolution_failure(...)` 写 `requirement_resolutions.outcome=unavailable`（含 `binding_unavailable` 错误码）；
  - 返回 `binding=None, status=unavailable`；**不登记另一条要求、不用"最近一条 active"**。
- `apply_resolution` 抛错（目标不存在/非 active/版本过期）：记录 `apply_resolution_failed`（apply 自身已写 rejected 审计，不重复写）→ binding=None、status=unavailable、不静默新建。
- 解析失败/解析器缺失：保留既有"按新要求登记 + status=unavailable"，binding 指向刚登记的新要求；不扩大旧授权。

### 2.3 真实流程验证（全部通过真实 `_execute_turn()`）

- `test_new_resolution_binds_real_requirement_and_keeps_user_words`：编排器内 contextvar 拿到 `RequirementBinding`（真实 id + version=1），台账行 text 与用户输入逐字一致。
- `test_amend_binds_new_version_and_invalidates_old_binding`：新版本 2、parent_id 正确、旧版本 `binding_for` 为 None、`validate_binding` False。
- `test_withdraw_does_not_bind_withdrawn_requirement_or_reuse_old_grant`：binding None、状态 withdrawn、旧授权 `reuse` None。
- `test_binding_version_mismatch_is_explicit_failure_without_fallback_registration`：只登记 1 条要求、无回退、审计 unavailable、notices 含 binding_unavailable。
- `test_missing_binding_is_explicit_failure_not_silent_success`：同上（binding_for 返回 None）。
- `test_apply_resolution_failure_does_not_register_fallback_requirement`：过期版本 → rejected 审计、无新要求、status unavailable。
- `test_resolution_failure_registers_new_requirement_without_expanding_old_grant`：旧要求保持 active 但本轮绑定是新要求；旧授权不能复用到新要求。

测试未使用"预先登记正确要求再手动 set requirement_context"；要求解析落实、台账、版本绑定、contextvar 都是真实实现。编排器用可观察替身（契约允许），不跳过 `_execute_turn`。

## 3. 真实用户原话与来源（契约 §3）

- `apply_resolution(message=instruction.text, message_id=..., turn_id=ctx.turn_id, topic_id=ctx.initial_topic)`。
- `instruction.text` 是 InstructionRegistry 收到的用户原文；`apply_resolution` 内 `message is not None` 时优先使用 message（new/amend 都是），corrections 只作为补充审计，不改写 `requirement.text`。
- `message_id` 来源：`ctx.user_message_id`（若调用方/恢复路径已提供），否则 `None`；**不拿 turn_id/topic_id 冒充**。
  - 说明：解析发生在 `TurnOrchestrator` 写用户消息之前（编排器在 `turn_orchestrator.py:576` 才 append_message），因此**新轮**此刻通常没有真实 message_id；A 不伪造。
  - 测试覆盖：`test_new_resolution...` 设 `ctx.user_message_id="msg_real_1"`，provenance 的 message_id/turn_id/topic_id 与来源逐项一致；`test_message_id_is_not_cross_used_when_unavailable` 验证缺失时为 None 且不等于 turn_id。
  - 需要 Lead 决策：若要求"每个新轮也有真实 message_id"，需编排器把用户消息落库提前到要求解析之前（或把 message_id 写入 `TurnContext` 后再解析）；A 不擅自改编排器。
- `topic_id=ctx.initial_topic`（不是 current_topic，避免中途切话题串用）。

## 4. `removed_limits`（契约 §3、§6）

- `ResolutionOutcome` 追加 `removed_limits: tuple[dict, ...] = ()`（默认空，向后兼容）；`as_dict()` 输出。
- `parse_resolution_outcome` 解析 `removed_limits`（与 limits 同一结构/上限/规范化）；
  `validate_resolution` 用同一 `_clean_limit_entries` 校验操作名/对象 kind，且**仅 amend 允许非空**（new/withdraw 带 removed_limits 直接 Unavailable）；removed_limits 的 objects **只做结构校验**，不要求命中当前候选/后端对象（解除限制只是放宽，解除对象可能来自旧要求；写错也不会产生许可）。
- 提示词（`r9h-req-resolution-v1`）：明确"用户明确解除限制时输出 removed_limits；不得借 amend 清空未解除的限制；无法确定时不要输出、在 reason 说明"。
- 测试：`test_resolution_outcome_supports_removed_limits_only_for_amend`。
- C 侧 `apply_resolution` 可用 `getattr(resolution, "removed_limits", ())` 读取并落实；A 未改 requirements.py。

## 5. §6 历史限制重新理解（Lead 补充任务）

### 5.1 resolution.py / prompts.py

- `ModelRequirementResolver.extract_limits(text, *, requirement_id=None, version=None) -> tuple[dict,...]`：
  - 单次 `adapter.complete(messages, tools=[])`；不暴露工具/文件/设置；不写库；
  - 输入只含**用户原话**与 requirement_id/version（`build_limit_extraction_payload` 唯一入口，不喂 R9-G 展平文字、不做关键词判断）；
  - 输出 JSON `{"limits":[...]}`，结构/操作名/对象 kind 与 `ResolutionOutcome.limits` 条目一致；
  - 结构非法/超时/异常 → `RequirementResolutionUnavailable`；空数组是合法结果（模型判断"原话没有明确限制"）。
  - 对象来源**只做结构校验**（不要求命中当前后端对象清单）：旧要求里的对象可能已不在本轮可解析集合，且限制只收紧、不产生授权；写错对象不会放行任何操作。
- 提示词：`LIMIT_EXTRACTION_SCHEMA_VERSION="r9h-limit-extraction-v1"`、`LIMIT_EXTRACTION_SYSTEM/INSTRUCTION`（prompts.py 解析段）。
- 解析器实例属性 `limit_extraction_prompt_version` / `prompt_version`，供 app/审计写 source（不猜版本）。

### 5.2 app 接线（`_ensure_legacy_structured_limits`）

取得有效绑定后：

1. `requirements.by_id(binding.requirement_id)` 取当前要求；
2. `getattr(requirement, "structured_limits_state", None)` 为 `legacy` / `unresolved` 时：
   - 调 `extract_limits(requirement.text, requirement_id=..., version=...)`（真实用户原话）；
   - 成功 → `requirements.set_structured_limits(id, limits, source=提示词版本, state="structured")`；
   - 失败 → `set_structured_limits(id, (), source="extract_limits_failed:<异常类>", state="unresolved")`，日志 + notices；
   - setter/extractor 不存在（C 未合入）→ 只告警跳过，**不**把 legacy 当无限制；
   - 整个过程捕获异常，**绝不阻塞本轮绑定**；引擎侧（R9-H 预接线）对 legacy/unresolved 要求按"需确认"处理。
3. setter/extractor 兼容同步/异步（`inspect.isawaitable`），C 接口形式变化不会崩。

### 5.3 测试（因 C 分支不在本 worktree，用前向兼容替身）

`StructuredStateLedgerShim` 只补 C 的 `structured_limits_state` / `set_structured_limits` 两个接口，其余全部委托真实 `RequirementLedger`（apply_resolution/binding_for/record_user_message/candidates 都是真实写库）：

- `test_legacy_requirement_is_reunderstood_and_persisted`：extract 收到逐字原话 + 真实 id/version；set 调用 state=structured、source=提示词版本、limits 一致。
- `test_legacy_extraction_failure_marks_unresolved_without_blocking_binding`：绑定仍有效、状态 ok；set state=unresolved、source 以 `extract_limits_failed:` 开头、notices 有提示。
- `test_legacy_text_is_passed_verbatim_not_keyword_parsed`：含"不要/如果/「」"的原话逐字交给模型，无"限制："展平前缀。
- `test_structured_requirement_does_not_trigger_extra_extraction`：structured 要求不触发额外模型调用。
- `test_real_resolver_extract_limits_is_wired_through_execute_turn`：app + 真实 `ModelRequirementResolver` + FakeAdapter（两次响应：解析 + 提取），验证 2 次 tools=[] 调用、提取输入含原话、持久化 state=structured。
- `test_extract_limits_uses_single_tools_free_model_call` / `test_extract_limits_payload_and_render_use_user_words_only` / `test_parse_extracted_limits_rejects_illegal_structure`：解析器 API 与非法结构。

**集成前提（必须由 C/Lead 合并后验证）**：真实 `user_requirements.structured_limits_state/structured_limits` 列与 `set_structured_limits` 由 C 提供；A 分支没有该迁移，因此测试用替身，未验证真实列持久化/重启恢复。

## 6. 契约要求对照

| 契约 §3 要求 | 实现 | 证据 |
| --- | --- | --- |
| new/amend 用 `binding_for` 取得有效版本绑定 | app `_prepare_requirement_binding` | 22 个流程测试 |
| withdraw 不绑定已撤回要求 | binding=None + status 校验 | withdraw 测试 |
| message=instruction.text / message_id / turn_id / topic_id=initial_topic | 传入真实 `apply_resolution` | 原话/来源测试 |
| corrections 不替代原话 | message 优先，corrections 仅审计 | new/amend 测试 |
| binding 失败明确记录、status unavailable、不回退登记 | 日志+notices+审计；无 fallback | 版本不符/缺失测试 |
| 解析失败不扩大旧授权 | 新要求登记 + 旧要求保持独立 | 解析失败测试 |
| 取消轮终态 | 解析期间取消 → 轮绑定 status=cancelled；编排器收到 cancelled=True | 取消测试 |
| `removed_limits`（§3 接口冻结允许） | dataclass + 解析 + 仅 amend 校验 | removed_limits 测试 |
| §6 legacy/unresolved 重新理解 | app 接线 + extract_limits + C setter | shim + 真实 resolver 测试 |

## 7. 测试记录

### 7.1 命令与结果

```powershell
cd D:/qio-dev/qio-r9h-a/backend
uv sync --frozen --extra dev   # 本 worktree 首次需要 dev 依赖（pytest/keyring）

# A 新测试（真实 _execute_turn）
uv run --frozen python -m pytest tests/test_r9h_binding_flow.py -W ignore
# => 22 passed

# 定向回归（A + R9-G/R9-F 相关 + 引擎/模式/提示词）
uv run --frozen python -m pytest tests/test_r9h_binding_flow.py tests/test_r9g_resolution.py tests/test_r9g_binding.py tests/test_r9g_semantics.py tests/test_r9g_acceptance_e2e.py tests/test_r9f_requirements.py tests/test_r9f_review.py tests/test_r9f_engine_review.py tests/test_turn_cancel_semantics.py tests/test_turn_binding.py tests/test_app_integration.py tests/test_security_modes.py tests/test_instruction_scope.py tests/test_authorization_engine.py tests/test_p1_a_prompts.py -W ignore
# => 218 passed, 1 failed

# 编译
uv run --frozen python -m py_compile src/agent/services/app.py src/agent/security/resolution.py src/agent/prompts.py tests/test_r9h_binding_flow.py
# => OK
```

### 7.2 唯一失败

`tests/test_authorization_engine.py::test_read_only_still_denies_credential_reads`：只读模式读 `.env` 期望 DENY，实际 ALLOW（名称判定凭据路径的基线行为，属契约 §1 问题 3，B/Lead 范围）。

### 7.3 基线既有红验证

为排除 A 改动影响，执行：

```powershell
cd D:/qio-dev/qio-r9h-a
git stash push -- backend/src/agent/services/app.py backend/src/agent/security/resolution.py backend/src/agent/prompts.py
cd backend
uv run --frozen python -m pytest tests/test_authorization_engine.py::test_read_only_still_denies_credential_reads -W ignore
# => 1 failed（与 A 改动前一致）
cd ..
git stash pop
```

结论：该失败是基线既有红，A 未改 internal_data/engine，也未放宽该测试。

## 8. 与冻结契约的偏差 / 需 Lead 确认

1. **`message_id` 在新轮可能为 None**：解析发生在编排器写用户消息之前，A 只传可用来源（`ctx.user_message_id`），不伪造/不串用 turn_id/topic_id。若契约要求新轮必有真实 message_id，需 Lead 调整编排器写消息顺序或 TurnContext 字段（A 未改 orchestrator）。
2. **`ResolutionOutcome.removed_limits`**：契约 §3 明确允许追加；已按默认空向后兼容。
3. **`extract_limits()` 为新增接口**（Lead 补充任务），不在原冻结接口清单；A 在 resolution.py 实现、prompts.py 新增提示词段；`REQUIREMENT_RESOLUTION_SCHEMA_VERSION` 升至 `r9h-req-resolution-v1`（解析提示词新增 removed_limits），新增 `LIMIT_EXTRACTION_SCHEMA_VERSION`。
4. **C 接口依赖**：app 用 `getattr` 兼容 `structured_limits_state` / `set_structured_limits`；A 分支没有真实列，测试用前向兼容替身。若 C 的字段名/签名不同，需在合并时对齐（A 报告 §5.3 写了接线点）。
5. **binding 失败审计复用 `record_resolution_failure`**（outcome=unavailable + error=binding_unavailable:...）：C 若新增更专门的绑定失败审计接口，Lead 可替换；当前满足"台账审计"要求。
6. **`_ensure_legacy_structured_limits` 对解析失败回退登记的新要求也会检查一次状态**（通常 structured，不产生额外模型调用）；不改变既有解析失败行为。
7. **`requirement_error` 返回值**目前仅用于日志/notices/审计，未写入新的 contextvar（不新增接口）。
8. **removed_limits 与历史提取的 objects 只做结构校验**：不要求命中当前候选/后端对象清单。理由：解除限制只放宽、不产生许可；历史对象可能已不在当前可解析集合。若 Lead 认为必须严格匹配，可在 `_clean_limit_entries(require_known_objects=True)` 打开（已保留参数），但会增加合法历史限制被判 Unavailable 的风险。

## 9. 未完成 / 未验证

| 项 | 状态 |
| --- | --- |
| C 的真实结构化限制列/迁移、真实 DB 持久化、重启恢复 | 未验证（C 分支不在本 worktree；A 用替身验证接线） |
| 引擎在 unrestricted 下按 legacy/unresolved 需确认的端到端行为 | 未验证（Lead 预接线已有 `_limits_unresolved`；A 未改 engine） |
| 子智能体/后台/审批恢复携带同一有效绑定 | 未验证（contextvar 既有机制；A 未改） |
| 真实模型评估 | 未运行（本轮纪律：不使用真实 Key） |
| 全量 pytest / 完整 CI | 未运行（Lead 统一各最多两次） |
| 取消发生在 extract_limits 等待期间 | 未单测；`asyncio.CancelledError` 会向上传播（既有取消语义），未把取消当解析失败吞掉 |
| 非 Windows 平台 | 未跑 |

## 10. Lead 接线 / 合并注意

1. AppContext 必须使用**真实** `ModelRequirementResolver`（`adapter_factory=self.build_adapter`）：A 的 app 逻辑依赖其 `extract_limits` 与 `limit_extraction_prompt_version`；若测试/降级使用自定义 resolver，需要同样提供（缺失时只告警跳过历史限制重新理解）。
2. C 的 `RequirementLedger` 需提供 `set_structured_limits(requirement_id, limits, *, source, state)` 与 `UserRequirement.structured_limits_state/structured_limits`；A 用 `getattr` 兼容，缺失时不阻塞绑定。
3. `resolution_status` 取值：成功 new/amend/withdraw 为 `ok`；解析失败、绑定失败、apply 失败为 `unavailable`；engine 侧映射 resolved/unavailable 不变。
4. 若 Lead 希望新轮 message_id 一定存在：在 `_execute_turn` 前写用户消息并把 id 放入 `ctx.user_message_id`（或 TurnContext 新字段），A 的 `_turn_message_id` 会自动使用。
5. 审批恢复/后台/子智能体沿用父绑定：A 未改 context.py；合并时确认它们使用 `current_requirement`（Lead 既有预接线）。

## 11. 复现命令（同 §7.1）

```powershell
cd D:/qio-dev/qio-r9h-a/backend
uv run --frozen python -m pytest tests/test_r9h_binding_flow.py -W ignore
uv run --frozen python -m pytest tests/test_r9g_resolution.py tests/test_r9g_binding.py tests/test_r9g_semantics.py tests/test_r9g_acceptance_e2e.py tests/test_r9f_requirements.py tests/test_r9f_review.py tests/test_r9f_engine_review.py tests/test_turn_cancel_semantics.py tests/test_turn_binding.py tests/test_app_integration.py tests/test_security_modes.py tests/test_instruction_scope.py tests/test_authorization_engine.py tests/test_p1_a_prompts.py -W ignore
```
