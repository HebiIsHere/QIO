# R9-H2 A 报告：历史要求修正链的结构化限制恢复

> 基线：`a6732f4c`（r9h2/main 起点：冻结补修契约 + 迁移 61/补偿 62）
> 分支：`r9h2/a-chain`；worktree：`D:\qio-dev\qio-r9h2-a`
> 本轮只做定向测试；未跑全量 pytest / 完整 CI（Lead 统一预算）。
> 模型调用全部 fake/mock，不使用真实 Key；未 push、未 merge main、未删除/跳过/放宽既有测试。
> 未改 app.py（B）、engine.py/recheck.py（B）、internal_data.py（C）。

## 0. 结论摘要

| 项 | 结果 |
| --- | --- |
| 缺口 1：legacy 补充后原限制丢失 | **A 侧已修复接口**：新增 `RequirementRecoveryContext/RequirementChainNode` 与 `recovery_context()`，按真实 parent_id 链组装根->当前完整原话/顺序/父子/版本/已知限制/来源；新增 `extract_limits_from_context()` 输出目标版本**完整有效限制**；旧单句 `extract_limits(text)` 保留兼容 |
| 缺口 1：条件写回 | `set_structured_limits` 增加 `expected_version/expected_status/expected_chain_digest`，同事务重新读取+重算链摘要，版本/状态/链不匹配抛 `RequirementVersionMismatch`，绝不覆盖新版本/已撤回要求 |
| 缺口 2 接口部分 | `record_user_message` 增加 `structured_state/structured_limits/structured_source`（默认结构化兼容；生产 fallback 由 B 显式传 unresolved） |
| 链完整性 | 缺父/循环/版本不连续/当前非 active → `complete=False + reason`，不抛异常；不完整上下文不调用模型、不标 structured |
| 竞态 | 模型等待期间 amend/withdraw：重新读 requirement 与 recovery_context，链/版本/状态变化 → 丢弃旧结果；条件写回也拒绝旧版本 |
| 失败保留 | 条件写入失败不修改行（known limits/source 原样保留）；`limits=None` 时 setter 保留已有 known limits，避免空数组误清空 |
| 定向测试 | A 新文件 **17 passed**；相关既有回归 **178 passed, 1 failed** 与 **76 passed, 1 failed**；唯一失败 `test_r9h_limits.py::test_migration_59_and_compensation_60_real_db` 为**基线既有红**（断言版本 60，而 r9h2/main 基线已有迁移 61/62；A 未改迁移），见 §7.2 |
| 真实模型 | 未运行（纪律：fake/mock；B 集成后由 Lead 决定是否真实评估） |

## 1. 交付文件

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| `backend/src/agent/security/requirements.py` | 修改 | `RequirementChainNode / RequirementRecoveryContext`、`_requirement_chain_digest`、`RequirementLedger.recovery_context`、`set_structured_limits` 条件写入、`record_user_message` 状态参数 |
| `backend/src/agent/security/resolution.py` | 修改 | `build_limit_extraction_context_payload / render_limit_extraction_context_messages / parse_extracted_context_limits`、`ModelRequirementResolver.extract_limits_from_context`、新 prompt 版本属性；旧 `extract_limits` 兼容保留 |
| `backend/src/agent/prompts.py` | 修改（仅解析段） | `LIMIT_EXTRACTION_CHAIN_SCHEMA_VERSION="r9h2-limit-extraction-chain-v1"`、`LIMIT_EXTRACTION_CHAIN_SYSTEM/INSTRUCTION` |
| `backend/tests/test_r9h2_chain_recovery.py` | 新增 | 17 个用例：真实 `_execute_turn()` + 真实 ledger/apply_resolution + fake resolver/闸门 |
| `docs/r9h2-reports/A.md` | 新增 | 本报告 |

未改：`services/app.py`、`security/engine.py`、`security/recheck.py`、`security/internal_data.py`、schema/迁移/其他测试。

## 2. 缺口 1 根因与 A 侧修复（契约 §3.1/§3.2）

### 2.1 根因（A 负责的接口部分）

R9-H 的 `_ensure_legacy_structured_limits` 只把 `requirement.text`（当前补充句）交给 `extract_limits(text)`：
* 补充后的子要求 text 只是"把标题改成 X"，原要求"不要修改 B"不在输入里；
* 子要求被标 structured/空限制后，unrestricted 下原限制丢失；
* 失败恢复时若写 `()`，会把已知限制覆盖成空数组。

### 2.2 新增台账接口（requirements.py）

```python
@dataclass(frozen=True)
class RequirementChainNode:
    requirement_id: str
    version: int
    parent_id: str | None
    text: str
    status: str
    provenance: dict
    structured_limits: tuple[dict, ...] | None
    structured_limits_state: str
    structured_limits_source: str | None

@dataclass(frozen=True)
class RequirementRecoveryContext:
    requirement_id: str
    version: int
    status: str
    parent_id: str | None
    chain: tuple[RequirementChainNode, ...]   # 根 -> 当前
    complete: bool
    reason: str
    chain_digest: str                          # sha256((id, version, parent_id, text, status) 有序)

RequirementLedger.recovery_context(requirement_id) -> RequirementRecoveryContext
RequirementLedger.set_structured_limits(
    requirement_id, limits, *, source, state,
    expected_version=None, expected_status=None, expected_chain_digest=None,
) -> UserRequirement
RequirementLedger.record_user_message(
    *, text, message_id=None, turn_id=None, topic_id=None,
    structured_state=STATE_STRUCTURED,
    structured_limits=(),
    structured_source="user_message",
) -> UserRequirement
```

行为：
* `recovery_context` 只用台账真实 `parent_id` 逐级回溯，再反转根->当前；不看 candidates/最近 active/全部历史消息；
* 父行缺失、id 循环、子 `version != parent.version+1`、当前非 active → `complete=False` + 中文 `reason`，**不抛异常**；
* `text/provenance/state/source` 原样；`structured_limits` 读该行反序列化值（None=未知），不拼接模型摘要；
* `chain_digest` 只覆盖契约指定的 `(requirement_id, version, parent_id, text, status)` 有序序列（不含模型输出/Reason 全文/密钥）。

### 2.3 条件写入（set_structured_limits）

* 三者都在**同一事务**内重新读取当前行：`expected_version` / `expected_status` 直接比较；`expected_chain_digest` 用 `recovery_context` 现场重算后比较；
* UPDATE 语句带 `WHERE id=? AND version=? AND status=?`，并核对 `rowcount==1`；任一失败抛 `RequirementVersionMismatch`（`ValueError` 子类），不写任何列；
* `limits=None && state!=legacy` → 保留已有 `structured_limits`（避免"恢复失败写空数组"）；显式 `()` 仍表示"已知集合为空"；
* state=legacy 清空结构化列（NULL）；source 截断 300；返回 reloaded requirement。

### 2.4 record_user_message 状态参数

* 默认 `structured / [] / "user_message"`，兼容旧调用；
* B 的生产 fallback 必须显式传 `STATE_UNRESOLVED`（解析失败不是"确认无限制"），实现支持；
* 非法 state 抛 `ValueError`；legacy 不写结构化内容（NULL）。

## 3. 要求链模型恢复（resolution.py + prompts.py，契约 §3.2）

```python
def build_limit_extraction_context_payload(context, *, requirement_id=None, version=None) -> dict
def render_limit_extraction_context_messages(context, *, requirement_id=None, version=None) -> list[ChatMessage]
def parse_extracted_context_limits(raw_text) -> tuple[dict, ...]

class ModelRequirementResolver:
    limit_extraction_chain_prompt_version = "r9h2-limit-extraction-chain-v1"
    async def extract_limits_from_context(
        self, context, *, requirement_id: str, version: int
    ) -> tuple[dict, ...]
    async def extract_limits(self, text, *, requirement_id=None, version=None)  # 旧兼容包装
```

* 输入是 `RequirementRecoveryContext`：目标 id/version/status/parent_id/chain_digest + 根->当前每个节点的原话（逐字）、顺序、父子、版本、状态、已知 structured_limits/state/source；
* 渲染前核对：目标 id/version 与 context 一致、`complete=True`、chain 非空、节点数 ≤64、digest 非空；不满足直接 `RequirementResolutionUnavailable`，**不调用模型**；
* 单次 `complete(messages, tools=[])`；不暴露工具/文件/设置，不写库；
* 输出 JSON `{"limits":[...]}` → `_parse_limits`（结构/上限）→ `requirement_limits.normalize_limits`（冻结 schema）→ 任一非法抛 Unavailable；空数组合法（=完整理解后确认无限制）；
* 提示词 `r9h2-limit-extraction-chain-v1`：要求"把整条链按顺序理解、后续补充默认 append、只有明确解除/替换才移除、legacy/unresolved 的已知限制只是片段、不发明、不把摘要当原话"；operations/kinds 用 `requirement_limits` 冻结白名单；
* 旧 `extract_limits(text)` 原样保留（R9-H 单句接口，供旧调用/测试），app 历史恢复必须走新方法（B 负责）。

## 4. 测试覆盖（契约 §3.4）

文件 `backend/tests/test_r9h2_chain_recovery.py`（17 用例）：

1. `test_legacy_root_plus_title_only_keeps_b_restriction`：legacy 原要求"修复项目但不要修改 B" + 只补充"把标题改成 X" → 恢复输入含两条原话且顺序/版本/父子正确；恢复后 B 仍被真实引擎（unrestricted）拒绝、A 允许。
2. `test_multiple_amendments_keep_original_restriction`：连续两次补充 v3，链为三段原话；恢复后原限制仍有效。
3. `test_explicit_remove_one_limit_keeps_other`：明确解除 B、保留"不要删除 data 目录" → 恢复后 B 允许、删除 data 拒绝。
4. `test_explicit_remove_all_limits_marks_structured_empty`：明确全部解除 → structured/[]，B 允许。
5. `test_unrelated_requirement_not_in_chain`：不相关要求不进入链/payload。
6. `test_missing_parent...`：缺父 → complete=False，不调用模型、不标 structured。
7. `test_version_discontinuity_and_cycle_are_incomplete`：版本不连续 / 环 → complete=False+reason。
8. `test_non_active_current_is_incomplete`：当前 withdrawn → incomplete。
9. `test_conditional_write_rejects_stale_version_and_preserves_known_limits`：恢复等待期间真实 amend 到 v3；旧 v2/旧摘要写回被拒绝，旧行 known limits/source 不被覆盖，新行仍未解决。
10. `test_conditional_write_recomputes_chain_digest_inside_transaction`：只改祖先原话（版本/状态不变）→ 旧 digest 写回被拒绝。
11. `test_amend_during_recovery_discards_stale_result`：闸门等待期间真实 amend → 旧恢复结果丢弃（StaleRecovery），新版本不写 structured。
12. `test_withdraw_during_recovery_discards_stale_result`：等待期间 withdraw → 旧结果丢弃，要求保持 withdrawn。
13. `test_recovery_failure_keeps_known_limits_and_source`：模型失败 → B 侧失败路径保留 known limits（不写空数组），状态 unresolved。
14. `test_chain_digest_stable_and_changes_after_amend`：digest 稳定、amend 后变化。
15. `test_record_user_message_structured_parameters`：默认/显式 unresolved/legacy/非法状态。
16. `test_chain_payload_contains_full_ordered_chain`：payload 逐字原话/顺序/版本/父子/state/digest；目标不一致/非法结构拒绝。
17. `test_extract_limits_from_context_model_call_and_failures`：真实 `ModelRequirementResolver` + fake model_call：有效 JSON 成功且只调用一次；非法 JSON → Unavailable；不完整上下文在调用前拒绝。

说明：A 分支的 app.py 仍是 R9-H 旧接线（B 尚未修改），旧 hook 会调用单句 `extract_limits`；测试中 fake 的旧接口故意抛 Unavailable（模拟"单句恢复不适用于历史链"），因此 amend 子要求状态可能为 unresolved 而非 legacy；核心断言是"未解决、不得当无限制"，链恢复仍走 A 的真实接口。B 合入后应用新链路做 E2E（见 §8）。

## 5. 契约要求对照

| 契约 | 实现 |
| --- | --- |
| §3.1 两个不可变数据类字段 | 字段/类型/默认与契约一致（链节点另含 provenance/state/source） |
| 根->当前按真实 parent_id；不用 candidates/最近 active/全部历史 | `recovery_context` 只按 parent_id 回溯 |
| 缺父/循环/版本不连续/当前非 active → complete=False+reason，不抛异常 | 已实现并在缺失/环/版本/withdrawn 测试覆盖 |
| text/provenance/state/source 原样 | `_chain_node` 直接读 UserRequirement |
| chain_digest 有序摘要 | sha256(JSON [(id,version,parent_id,text,status)]) |
| setter 条件写入 + WHERE 重算 + 不覆盖新版本 | 事务内重读/重算/rowcount 检查；测试覆盖 |
| 失败保留 known limits | 条件失败不写；`limits=None` 保留；显式 () 才清空 |
| record_user_message 状态参数/默认兼容/非法 state | 已实现 |
| §3.2 完整链模型恢复/失败 Unavailable/空数组合法/旧包装保留/版本常量 | 已实现（`r9h2-limit-extraction-chain-v1`） |
| §3.4 测试真实主流程/真实台账/不得绕过 context 组装/闸门竞态 | 场景 1-4、9-12 走真实 `_execute_turn`；恢复始终通过 `recovery_context` 组装；闸门用 asyncio.Event |

## 6. 与冻结契约的偏差 / 需 Lead 确认

1. **`recovery_context` 对"要求本身不存在/id 为空"也返回 complete=False 的 context**（version=0/status=""/chain=()），不抛异常；契约只写"链中任一父行缺失..."，此处补充防御行为，便于 B 统一处理。
2. **环检测优先于版本连续性**：若数据同时构成环且版本不连续，`reason` 报"父链存在循环"（先检查 `parent in seen`）。两类都 complete=False，不影响安全。
3. **`set_structured_limits(limits=None)` 会保留已有 known limits**（契约要求"调用方不得用 () 覆盖"；A 额外用 None 表示"保留"，显式 () 仍写空数组）。B 失败路径可传 `requirement.structured_limits` 或 None。
4. **旧 `extract_limits(text)` 保持 H 版行为不变**（解析用 resolution 通用结构校验），新 `extract_limits_from_context` 用 `requirement_limits.normalize_limits` 冻结 schema。契约"两者共用结构校验"在实现上共享 `_parse_limits` 的 JSON/字段解析，但旧包装未切到更窄 schema（为兼容旧测试/调用）；app 不再使用旧包装。
5. **不完整上下文在渲染阶段直接拒绝**（不调用模型）：契约要求 B 在 app 侧先检查 complete；A 在解析器侧再兜底一次，避免任何调用方绕过。
6. **历史原话提取的对象不做 allowed_objects 白名单校验**：旧对象可能不在当前可解析集合；限制只收紧、不产生许可，与 R9-H 口径一致。
7. **链节点上限 64**：超过视为上下文异常（拒绝恢复），不静默截断后声称完整。
8. **`record_user_message` 的 structured_source 对 legacy 不做特殊置空**：调用方显式传 None 才为 None；默认 "user_message" 保持旧行为。

## 7. 测试与验证记录

### 7.1 命令与结果

```powershell
cd D:/qio-dev/qio-r9h2-a/backend
uv sync --frozen --extra dev

# A 新测试
uv run --frozen python -m pytest tests/test_r9h2_chain_recovery.py -W ignore
# => 17 passed in 11.90s

# 相关既有回归（要求/解析/引擎/模式/app）
uv run --frozen python -m pytest tests/test_r9h2_chain_recovery.py tests/test_r9h_binding_flow.py tests/test_r9h_limits.py tests/test_r9h_recheck_limits.py tests/test_r9g_resolution.py tests/test_r9g_binding.py tests/test_r9f_requirements.py tests/test_r9f_review_validation.py tests/test_authorization_engine.py tests/test_security_modes.py -W ignore
# => 178 passed, 1 failed

uv run --frozen python -m pytest tests/test_app_integration.py tests/test_r9h_engine_wiring.py tests/test_r9h_content_binding.py tests/test_r9h_limits.py tests/test_r9g_acceptance_e2e.py -W ignore
# => 76 passed, 1 failed

# 编译
uv run --frozen python -m py_compile src/agent/security/requirements.py src/agent/security/resolution.py src/agent/prompts.py tests/test_r9h2_chain_recovery.py
# => OK
```

### 7.2 唯一失败

`tests/test_r9h_limits.py::test_migration_59_and_compensation_60_real_db`：
`assert SCHEMA_VERSION == COMPENSATION_VERSION == 60` 实际为 `62`（r9h2/main 基线已加入迁移 61/补偿 62，该旧测试断言未更新）。
**A 未改 schema/迁移/migrate.py**，该红为基线既有，按纪律不由 A 修改别人范围内的测试；需 Lead/B 统一更新旧断言或调整测试。

## 8. 未完成 / 未验证 & B 接线注意

| 项 | 状态 |
| --- | --- |
| app.py 消费（B §3.3） | **未做**（B 负责）：B 应改为 recovery_context → extract_limits_from_context → 重新读校验 → 条件 setter；本轮 A 测试用最小消费者替身验证 A 接口 |
| 真实 app E2E（真实 _execute_turn + 新链路 + 引擎） | 未验证（B/Lead） |
| 真实模型调用/提示词效果 | 未运行（纪律：fake/mock） |
| 全量 pytest / 完整 CI | 未运行（Lead 统一预算） |
| 迁移 61 回填与 legacy/unresolved 状态交互 | 未验证（Lead 迁移；A 只读现有列） |
| 非 Windows / SQLite 并发写 | 未验证（本机 Windows；条件写在同一连接事务内） |
| 长链/多分支用户历史 | 上限 64，未做大规模真实样本 |

B 接线要点（契约 §3.3 对应 A 接口）：
1. `context = requirements.recovery_context(binding.requirement_id)`；complete=False → 不调用模型、保持原 state（有 known limits 保留）、notice/日志返回。
2. `limits = await resolver.extract_limits_from_context(context, requirement_id=..., version=...)`（同步/异步兼容）。
3. 返回后重新 `by_id + recovery_context`；id/version/status/parent_id/chain_digest 任一变化 → 丢弃结果、不写。
4. `set_structured_limits(..., state=structured, source=prompt_version+";chain="+digest+";version="+version, expected_version=version, expected_status="active", expected_chain_digest=digest)`。
5. 异常/RequirementVersionMismatch：保留已有 known limits（传 `requirement.structured_limits` 或 None），state=unresolved，source=失败码+chain/version，notice；不阻塞本轮。
6. 成功：persist structured；本轮 resolution_limits 使用新限制（返回前重新读 binding/limits）。

## 9. 复现命令

```powershell
cd D:/qio-dev/qio-r9h2-a/backend
uv run --frozen python -m pytest tests/test_r9h2_chain_recovery.py -W ignore
uv run --frozen python -m pytest tests/test_r9h_binding_flow.py tests/test_r9g_resolution.py tests/test_r9g_binding.py tests/test_r9f_requirements.py tests/test_r9h_limits.py -W ignore
```
