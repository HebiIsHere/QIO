# R9-H3 B 报告：移除完全无限制模式的独立授权审查

- 分支：`r9h3/b-unrestricted`（worktree `D:/qio-dev/qio-r9h3-b`）
- 基线：`8d97904f`（R9-H3 契约冻结 + 迁移 63/补偿 64）
- 日期：2026-10-23
- 范围：契约 §3（B：review.py 删除 targeted 例外 + 定向测试/报告）；未改 engine.py/app.py/migration/A/C 独占模块。

## 1. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/security/review.py` | 删除 `ReviewRequest.targeted_limits_review` 字段；`_validate_request_shape` 只允许 restricted；`ModelAuthorizationReviewer.review` 在模型调用前拒绝非 restricted 请求；旧 engine 传参用兼容构造忽略（不保存字段、不进入模型输入） |
| `backend/tests/test_r9h3_unrestricted_no_review.py` | 新增 14 项真实引擎/台账测试（fake 审查器/模型） |
| `docs/r9h3-reports/B.md` | 本报告 |

## 2. 实现说明

- `ReviewRequest` 不再有 `targeted_limits_review` 字段（`dataclasses.fields` 不含）；`build_review_payload` 不含任何 targeted 标记。
- 为兼容 A 分支尚未移除 engine 传参：`ReviewRequest.__init__` 只忽略该旧关键字，其它未知字段抛 TypeError；字段不落数据类、不进入模型输入。
- `_validate_request_shape`：`mode != REVIEW_MODE`（restricted）一律抛 `ReviewUnavailable`，恢复“普通独立授权审查只属于受限模式”。
- `ModelAuthorizationReviewer.review`：进入摘要/模型调用之前检查 mode，非 restricted 直接 `ReviewUnavailable`，因此即使旧 engine 仍调用 reviewer，也绝不调用模型。
- 恢复流程仍由既有专用语义理解流程承担（extract limits），不生成普通授权记录；固定查询/普通读取/内部数据/永久删除规则未改。

## 3. 测试与证据

`tests/test_r9h3_unrestricted_no_review.py`（14 项全部通过，真实 AuthorizationEngine/RequirementLedger/AuthorizationLedger，fake 模型）：

1. ReviewRequest 无 targeted 字段；旧关键字只兼容忽略；payload 无 targeted 标记。
2. `validate_outcome` 拒绝 unrestricted 请求；restricted 正常。
3. 真实 `ModelAuthorizationReviewer` 对 unrestricted 请求在模型调用前拒绝，fake model_call 次数 0。
4. unrestricted + unresolved（无已知限制/有已知限制）→ 模型调用 0、结论不为 ALLOW（B 基线为 APPROVE，A 合入后应为 APPROVE/DENY）。
5. unrestricted + structured → ALLOW 且 reviewer/model 调用 0。
6. unrestricted + structured 已知限制 → 写 B DENY、写 A ALLOW、模型调用 0。
7. unresolved 已知限制 → 模型调用 0、不静默 ALLOW（已知限制优先级 DENY 由 A 的 engine 修复，见 §5）。
8. 批量 + 重复 authorize/recheck（unresolved）→ 模型调用 0；structured 批量 ALLOW 不新增审查调用。
9. 历史恢复成功（真实台账写 structured limits）→ B DENY/A ALLOW，模型调用 0。
10. restricted 仍调用审查（fake reviewer 被调用 1 次）。
11. read_only 不新增审查（DENY、reviewer 0 次）。
12. 内部数据仍三次确认（APPROVE、reason 含三次确认）；unrestricted 永久删除仍 ALLOW；模型调用 0。
13. 迁移 63 撤销的旧 targeted 授权记录（mode=unrestricted/source=model_review/revoked_at 非空）不能复用；行/原因/review_id 保留；未撤销记录仍保留。

定向命令与结果（cwd=backend，`uv run --frozen pytest`；未触发完整 CI/全量矩阵）：

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_r9h3_unrestricted_no_review.py` | **14 passed** |
| 综合定向（本测试 + r9f_review/review_validation/engine_review + r9g_semantics/engine_core/binding + r9h_binding_flow/recheck_limits + authorization_engine + r9h2_chain_recovery） | **201 passed** |
| 早期回归（含 r9h2_parse_state） | 199 passed + 1 个被契约取代的 H2 断言红（见 §5） |

## 4. 与契约偏差 / 依赖 A

- **engine.py 仍保留 A 负责删除的 targeted 分支**（本分支基线 8d97904f）：unrestricted+unresolved 时 engine 仍会调用 reviewer。B 的两道防线保证：review.py 在模型调用前拒绝；不产生普通授权记录；结论不为 ALLOW。A 合入移除 engine 分支后，reviewer.review 本身也不会被调用。
- 因不能改 engine，测试对 unrestricted+unresolved 的断言是「模型调用 0 且不为 ALLOW」；「reviewer.review 0 次」需 A 合入后成立（本报告与测试注释已明确）。
- 未改 app.py/migration/A/C 独占文件；迁移 63 的实际撤销行为由 Lead 测试覆盖，B 只验证撤销记录不可复用且保留。

## 5. 需要集成时适配的既有测试（非本分支独占文件，我未改）

契约 §3 明确删除 targeted 例外，因此两条 H2 旧断言已被新契约取代，集成时需要 Lead/A 更新：

- `tests/test_r9h2_parse_state.py::test_restart_and_recheck_preserve_unresolved_state`：旧断言 unrestricted unresolved 经 reviewer 返回 ALLOW；H3 后应为 APPROVE、reviewer 不被调用。
- `tests/test_r9h2_real_flow.py::test_parse_failure_unresolved_uses_targeted_review_not_blanket_block`：目标行为是 targeted 审查，H3 已删除。

其余受影响定向套件均绿。

## 6. 未完成 / 未验证（诚实清单）

- 未触发完整 CI（累计预算 2/2 已用尽，按契约只做定向测试）；未跑本地全量矩阵。
- A 的 engine targeted 分支删除/已知限制优先级修复未合入本分支：unresolved+已知限制的 DENY 由 A 的 `test_r9h3_limits_priority` 验证；B 仅验证不静默放行且无模型审查。
- 「reviewer.review 0 次」的严格断言需 A 合入后由 Lead 定向测试确认；B 已用真实 ModelAuthorizationReviewer 证明模型调用 0。
- 迁移 63 的真实 DB 撤销由 Lead 迁移测试覆盖；未在 B 中伪造迁移结果。

## 7. 合并提示

- 本分支只改 review.py 与新增测试/报告；与 A 合并顺序无冲突（A 可先删 engine targeted 参数；review.py 的兼容构造可安全忽略旧参数）。
- 合并后请把 §5 两条旧测试按 H3 契约更新，并跑 A 的 limits priority 定向套件确认已知限制 DENY。
