# R9-H B 报告：审查对象组装 / 内容绑定 / 授权复用

- 分支：`r9h/b-content`（worktree `D:/qio-dev/qio-r9h-b`）
- 基线：`39c0f48e`（R9-H 契约冻结 + 引擎字段读取预接线）
- 报告日期：2026-10-23
- 范围：契约 §4（内容绑定贯通真实组装路径）+ 相关 §9 真实接线验证；不改 `engine.py`。

## 1. 交付概览

| 提交 | 内容 |
| --- | --- |
| `384511c3` | 核心：effect.detail 字段生产、review 状态摘要 helper、审批 operation_binding、执行器重核对、初版测试 |
| 本次提交（见交付消息 SHA） | 按 Lead 集成回归反馈修正附件写回内容语义；`test_approved_operations` 改为契约形状；补批量子项/前置状态/cwd/永久删除/旧形状兼容用例；本报告 |

改动文件：

- `backend/src/agent/security/review.py`：新增 `object_state_digest` / `precondition_digest`（规范化前置状态摘要），并在 `__all__` 导出。原 ReviewOperation 字段/审查 payload/validate/operation_fingerprint 已在 R9-G 实现，本分支只补状态摘要与一致性使用。
- `backend/src/agent/tools/fs_tools.py`：`fs_write` 写入内容摘要 + 覆盖前状态；`fs_patch` 补丁结果摘要 + 打补丁前状态；`fs_delete` 逐项及批量删除前状态、`delete_mode=recycle_bin`；审批指纹新增 `operation_binding`（真实引擎 `_review_operation` + `operation_fingerprint`）。
- `backend/src/agent/tools/cmd_tools.py`：命令永久删除时 effect.detail 写 `delete_mode=permanent`（契约字段映射）；spec/script 摘要沿用真实执行事实。
- `backend/src/agent/services/attachments.py`：写回 `content_sha256` 修正为**新内容**摘要（原代码误传当前旧内容），补 `content_digest`/`precondition_digest`；附件回收逐项/批量补删除前状态与 `delete_mode`；审批指纹新增 `operation_binding`。
- `backend/src/agent/tools/approved_operations.py`：新增 `_review_binding_mismatch`（审批单绑定指纹 vs effect 快照重算）与 `_payload_content_mismatch`（拟写入内容摘要核对）；fs_write/patch/delete、附件写回/批量写回/回收执行前应用；删除重放按指纹逐项核对对象内容变化；失败摘要带具体失效原因。附件写回语义修复：旧形状 `content_sha256=覆盖前内容`、`new_sha256=新内容` 时按 new_sha256 比较，契约形状 `content_digest=新内容` 优先。
- `backend/tests/test_r9h_content_binding.py`：新增 16 个真实工具 + 真实引擎 `_review_operation` 用例（见 §3）。
- `backend/tests/test_approved_operations.py`：按 Lead 要求在 `test_attachment_write_back_really_writes_source` 使用契约形状指纹（`content_digest`=新内容、`precondition_digest`=覆盖前状态），原断言（写回成功、source 内容、attachment 登记 sha 更新）全部保留并继续验证。

## 2. 契约 §4 字段映射实现

| effect.detail 键（工具生产） | ReviewOperation 字段 | 实现位置 |
| --- | --- | --- |
| `content_digest` / `content_sha256` | `content_digest` | fs_write（写入内容）、fs_patch（补丁结果）、附件写回（新内容）；cmd 的脚本/内联代码/argv 由 `spec_digest` 覆盖 |
| `precondition_digest` / `base_content_sha256` | `precondition_digest` | fs_write/fs_patch/fs_delete 逐项与批量、附件写回/回收；新文件用 `exists=False` absent 状态 |
| `spec_digest` / `execution.spec_digest` | `spec_digest` | cmd run_program/run_shell 真实 LaunchSpec 摘要（程序身份/argv/cwd/脚本内容） |
| `script_sha256` | `script_sha256` | cmd 脚本参数文件内容摘要（同路径不同内容不同） |
| `delete_mode` | `delete_mode` | fs_delete/附件回收 `recycle_bin`；命令永久删除 `permanent` |
| `conditions` | `conditions` | 字段贯通与校验；本分支范围内没有产生结构化条件的真实工具（见偏差） |
| `network_hosts` / 目的地 | `network_hosts` / 对象 | 本分支独占文件无外发/上传工具；命令的 cwd（执行目的地）由 spec_digest 绑定，见测试 |

同一份事实进入：审查请求（`build_review_payload` 已含 operation 的新字段）、授权记录（`operation_fingerprint` 含内容/前置/spec/删除方式）、审批 payload/fingerprint（`operation_binding`）、执行前重核对（绑定指纹/内容摘要/对象身份/前置内容）。

## 3. 测试与结果

新增 `tests/test_r9h_content_binding.py`（16 项，全部通过）：

1. fs_write：真实工具的 effect → 真实引擎组装；content A 允许后 content B 不复用（授权记录指纹不同；审查 payload 也含 content_digest/precondition_digest）。
2. fs_patch：补丁结果内容摘要 + 打补丁前状态进入审查对象。
3. 相同 effect（同内容/同前置状态/同对象）在有效期内复用已确认记录（不重复调用模型）。
4. fs_delete：`delete_mode=recycle_bin` + 删除前状态；审批指纹与引擎重算 operation_fingerprint 一致。
5. fs_delete 审批后文件内容变化 → 重放 invalidated，文件保留。
6. 同路径脚本文件内容变化 → 不复用旧授权（script_sha256/spec_digest 变化）。
7. 内联代码/argv 变化 → 不复用；审查 payload 同步变化。
8. 附件写回：新内容摘要 + 覆盖前状态进入审查；不同写回内容不复用。
9. 附件回收：`delete_mode=recycle_bin` + 前置状态；审批指纹一致。
10. fs_write 审批恢复执行绑定的内容；payload 内容漂移 → 不执行。
11. 前置状态变化（同内容）→ 不复用旧记录。
12. cwd（执行目的地）变化 → 不复用。
13. 批量子项独立绑定：两项内容摘要/前置摘要独立，批量 effect 带 delete_mode 与组合前置摘要。
14. 命令永久删除 effect `delete_mode=permanent`，审查 payload 暴露该字段。
15. 附件写回审批后源文件变化 → 重放 invalidated，不写回。
16. 旧审批形状（content_sha256=覆盖前内容、new_sha256=新内容）成功写回且登记 sha 更新（兼容回归；新内容语义比较，不把覆盖前摘要当新内容）。

定向命令与结果（cwd=backend，`uv run --frozen pytest`）：

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_r9h_content_binding.py` | **16 passed** |
| `pytest tests/test_approved_operations.py` | **23 passed** |
| `pytest tests/test_r9h_content_binding.py tests/test_approved_operations.py tests/test_fs_tools.py tests/test_r9g_fs_binding.py tests/test_fs_delete_recycle.py tests/test_r9f_engine_review.py tests/test_r9f_review.py tests/test_r9f_review_validation.py tests/test_r9g_cmd_binding.py tests/test_attachments_service.py tests/test_attachment_source_refs.py tests/test_r9f_origin_attachments.py tests/test_r9g_batch.py` | **241 passed** |
| `pytest tests/test_approval_lifecycle.py tests/test_approval_persistence.py tests/test_approval_restart.py tests/test_approval_binding.py tests/test_approval_api.py tests/test_attachments_api.py tests/test_security_modes.py tests/test_tool_restore.py` | **58 passed** |
| `pytest tests/test_fs_tools.py ... tests/test_sandbox_readonly.py`（fs/review/cmd 批次） | **190 passed** |
| `pytest tests/test_attachments_service.py ... tests/test_approval_binding.py tests/test_r9g_engine_core.py` | 113 passed；**2 个基线既有红**：`test_r9g_engine_core.py::test_structured_limit_denies_matching_write_in_unrestricted`、`::test_limit_matches_only_structured_objects`（结构化限制持久化，属 C/Lead 范围，本分支未改） |

模型调用全部为 FakeReviewer（不联网、无真实 Key）；未运行全量 pytest/完整 CI（按纪律留给 Lead）。

## 4. 集成回归反馈修复（Lead 报告）

问题：`tests/test_approved_operations.py::test_attachment_write_back_really_writes_source` 在合并树失败：`invalidated: operation_binding`。根因：该测试指纹沿用旧形状 `content_sha256=覆盖前内容`、`new_sha256=新内容`，而我的 `_payload_content_mismatch` 通用分支把 `content_sha256` 当拟写入内容比较。

修复：

- 执行器按契约语义与操作类型取「拟写入内容」：附件写回优先 `operation_binding.content_digest` → 指纹 `content_digest` → `new_sha256`；fs.write/fs.patch 才回退 `content_sha256`。覆盖前内容仍由已有的 `content_sha256`/`precondition_digest` + 身份核对负责。
- `test_approved_operations.py` 该用例改为真实契约形状（同时保留旧字段兼容），原断言不削弱并继续验证写回成功后 source 内容与 attachment 登记 sha 更新。
- 新增旧形状兼容用例（本文件第 16 项）证明两种形状都不会被误判，也不会把覆盖前摘要当新内容。

## 5. 与契约偏差 / 未完成 / 未验证（诚实清单）

- **修改了独占清单外的 `backend/tests/test_approved_operations.py`**：属 Lead 明确指示的集成回归修复；只改该用例指纹为契约形状，未删除/放宽任何断言。
- **conditions**：字段已贯通审查 payload/指纹/重核对；本分支独占文件（fs/attachment/cmd）当前没有产生结构化条件的真实工具，故没有「条件满足/不成立」的真实接线用例；重核对逻辑会拒绝 satisfied 非 True 或缺少 evidence 的条件。
- **来源/目的地**：本分支无移动/复制/上传/网络外发工具（fs 工具没有 move/copy 入口；网络工具不在独占文件内）。已覆盖命令 cwd（执行目的地）变化；附件写回/回收绑定源文件路径与身份，未伪造不存在的目的地字段。
- **批量子项失效隔离**：新增了批量子项独立内容/前置摘要测试与逐项 approval 指纹；未写「批量子项部分对象变化 → 只失效该项并继续其余项」的执行级用例（fs_delete 执行器会逐项 invalidated；附件回收逐项同理，但回收进系统回收站会移动真实文件，未在测试中制造系统副作用）。
- 附件 `write_back_many` 的逐项内容核对依赖 payload/operation_digest + 指纹 `new_sha256`；审批恢复执行器已按 `fingerprint.items.new_sha256` 做写出前摘要校验，但未写批量恢复的端到端用例。
- 未运行全量 pytest/完整 CI；未做真实模型网络验证（按要求 FakeReviewer）。
- `security/review.py` 的 ReviewOperation 字段与 payload/validate/operation_fingerprint 在 R9-G 已存在，本分支验证它们与真实 effect 事实一致；只新增了状态摘要 helper，没有重写审查语义。

## 6. 给 Lead 的接线/合并提示

- 引擎从 `effect.detail` 读取字段的映射已按 `docs/r9h-contracts.md` §4 表对齐；工具字段名见 §2 表。
- 审批指纹新增 `operation_binding`（含 fingerprint/mode/content_digest/precondition_digest/spec_digest/conditions/delete_mode）；历史审批没有此字段时 `_review_binding_mismatch` 返回 None，由既有要求/对象/内容核对兜底，不默认放行。
- 附件写回旧形状兼容：`content_sha256`=覆盖前内容、`new_sha256`=新内容；新工具已同时写契约字段（`content_digest`/`precondition_digest`）。
- `review.object_state_digest` / `precondition_digest` 可供其他模块复用（规范化路径、排序无关、只存摘要）。
