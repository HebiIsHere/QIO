# R9-H4 C 报告：文件/附件实际影响、目录范围、批量逐项

> 分支：`r9h4/c`（worktree `D:/qio-dev/qio-r9h4-c`，基线 `67fa88c6`）
> 契约：`docs/r9h4-contracts.md` §2/§3/§6/§7（冻结）。只做定向测试；完整 CI 预算 2/2 已用尽，本分支不触发。
> 结论：C 独占文件的实现与定向测试完成；engine/_limit_conflict、approved_operations 的目录影响执行前核对需要 Lead/B 接线（见 §5）。

## 1. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/src/agent/tools/fs_tools.py` | 新增实际影响/目录影响/去重 helper；fs_read/fs_list/fs_find/fs_info 补 read 影响；fs_write/fs_patch 补 write/create 影响与 identity；fs_delete 逐项补 delete 影响（目录有界枚举子对象）+ 目录影响依据；批量删除汇总与平台语义去重 |
| `backend/src/agent/tools/attachment_tools.py` | `read_attachment` 的 READ effect 补真实读取对象影响（读的是登记路径本身，不把目录子对象扩大成读取影响） |
| `backend/src/agent/services/attachments.py` | 附件写回（单个/批量）补 write 影响与 identity；材料回收（单个/批量）目录删除补有界子对象影响、目录影响摘要与内部数据子对象；批量前置/指纹逐项保留 |
| `backend/tests/test_r9h4_fs_batch.py`（新增） | 真实工具/真实文件操作/真实审批执行器 + 记录型 fake 网关 |
| `docs/r9h4-c-contract-report.md`（草稿，已删除）→ `docs/r9h4-reports/C.md` | 本报告 |

## 2. 实际影响 limit_impacts（契约 §2/§3）

effect.detail 统一字段（所有文件/附件真实操作）：
* `limit_impacts`: `[{"operation": "read|write|create|delete", "objects": [{"kind": "path|directory|path_prefix", "value": ...}]}, ...]`；
* `limit_impacts_complete`: bool；
* `limit_impacts_reason`: 不完整原因（达到上限/枚举失败）；
* `directory_impact` / `directory_impact_digest` / `internal_data_entries`（仅目录删除）。

覆盖：
* fs_read/fs_list/fs_find/fs_info：`read` + 真实对象（目录查询只报目录本身，**不**把子对象扩大成限制；普通目录查询不被扩大限制）；
* fs_write：已存在 = `write`，新建 = `create`；fs_patch = `write`；
* fs_delete：每个待删对象逐项 effect 有 `delete` 影响；**目录**枚举有界子对象（目录自身 + 文件/子目录 + 受保护/内部数据子对象），不是只查目录本身；
* 附件读取 = `read` 登记路径；附件写回 = `write` 源路径；材料回收 = `delete`（目录同样枚举子对象）；
* 批量 effect 汇总各子项影响（同一 operation 合并去重，不漏项）。

## 3. 目录影响依据与执行前比较（契约 §6/§7）

`fs_tools.directory_impact(root, operation="delete", is_protected=..., max_entries=500, max_depth=8)`：
* 有界枚举：目录自身 + 子文件/子目录；不跟随目录符号链接；排序固定；
* 每项：path、kind、身份键（volume:file_id）、内容摘要（仅 <=512KB 文件；大文件用 size+mtime 参与条目摘要）、size/mtime、protected（内部数据子对象）；
* `digest` 覆盖完整/部分条目；达到 max_entries/max_depth 时 `complete=False`、`reason` 明确，仍有 `path_prefix` 兜底对象；**不会假装完整**；
* fs_delete 目录项：effect.detail 与审批 fingerprint 都记录 `directory_impact`、`directory_impact_digest`、`limit_impacts_complete`；目录 precondition 用影响摘要参与；
* `fs_tools.verify_directory_impact(expected, root, ...)`：执行前重算并比较新增/移除/替换（身份/内容/尺寸/mtime/保护属性）；审批时影响不完整也明确返回不通过；
* 测试用真实等待场景：工具提交审批后新增目录子项，`verify_directory_impact` 返回 `ok=False` 且 `added` 含新对象（替换/移除同理）。

批量（fs_delete）：
* 逐项保留依据（identity/content/directory digest/precondition）、状态、结果；一次审批覆盖列明对象；
* 真实审批执行器逐项核对：B 在等待期间被原子替换 → B `invalidated` 保留文件，A 正常进回收站；结果 items 覆盖全部项（不漏项、不先整批判废）；
* 路径去重按平台语义：`path_dedupe_key(path, windows=...)`（Windows `normcase` 归一化；POSIX 大小写敏感）；fs_delete 使用该 helper（修掉原来一律 lower 的跨平台错误）；
* 批量影响汇总用 `aggregate_limit_impacts`（同 operation 对象合并去重）。

## 4. 内容/前置状态/身份（契约 §6）

* fs_write/fs_patch/附件写回：effect.detail 已有/补充 `content_digest`、`precondition_digest`、`identity`，审批 fingerprint 记录 `identity`、`base_content_sha256`/`new_sha256`、`limit_impacts`；
* fs_delete/附件回收：目录项 precondition 使用目录影响摘要；文件项保留内容摘要与身份；
* 执行前真实状态比较由现有 `recheck_before_execute`（身份+内容）与真实审批执行器完成；目录子项变化需 Lead 在执行器调用 `verify_directory_impact`（见 §5）。

## 5. 接线需求（共享文件只由 Lead 改）

1. **approved_operations.execute_fs_delete**：每行删除前，若 `row["is_directory"]` 为真，从审批 fingerprint 取 `directory_impact`（或 `paths[i]["directory_impact"]`）调用 `fs_tools.verify_directory_impact(..., path, is_protected=lambda p: security_gate.match_internal_data(...) 或 engine 内部数据判断)`；不通过 → 该项 `invalidated`（保留文件），继续其它项（不要整批判废）。
2. **approved_operations.execute_attachment_recycle**（若在 Lead 文件）：同样对目录项比较 `directory_impact`。
3. **engine._limit_conflict（B）**：读取 `effect.detail["limit_impacts"]`，按 `operation` + `objects` 匹配要求限制；`limit_impacts_complete=False` 时不得忽略剩余影响（按契约保留不确定/具体确认，具体策略由 B 决定）；read/program 对象已在 A 的 normalize 支持。
4. **review/审批内容**：`operation_fingerprint` 已通过 effect.detail 的 `content_digest/precondition_digest` 覆盖；`limit_impacts` 在 detail 中随 effect 快照持久化，无需改 effects.py/security_gate.py。
5. **执行器对象身份**：`security_gate.with_target_identity`（R9-H2）仍适用于审批恢复重建的 Effect；本分支未改。

## 6. 测试命令与结果（真实运行，Python 3.11 venv）

依赖：`cd backend; uv sync --frozen --extra dev`。

    uv run --frozen pytest tests/test_r9h4_fs_batch.py \
      tests/test_fs_tools.py tests/test_fs_delete_recycle.py tests/test_r9g_fs_binding.py \
      tests/test_r9g_batch.py tests/test_r9f_origin_fs_tools.py tests/test_r9f_origin_attachments.py \
      tests/test_attachment_source_refs.py tests/test_r9h_content_binding.py \
      tests/test_fs_containment.py tests/test_computer_sandbox.py tests/test_r9f_approved_replay.py -q
    → EXIT=0（仅既有 DeprecationWarning，无 FAILED/ERROR）

新增 `tests/test_r9h4_fs_batch.py` 覆盖：
* 写/补丁/新建真实影响（write/create，真实 effect + 审批指纹）；
* 读取/目录查询只有 read 对象，不把子对象扩大成限制；
* 删除目录影响包含全部子文件/子目录 + 受保护内部数据子对象（不能只查目录）；
* 达到枚举上限 complete=False + reason + path_prefix 兜底；
* 目录影响在等待期间新增/移除/替换 → `verify_directory_impact` 不通过并列出 changed；未变化 → 通过；审批影响不完整 → 明确不通过；
* 真实审批执行器批量逐项：B 变化 invalidated 且保留、A 继续完成、结果汇总全部项；
* Windows 归一化/POSIX 大小写敏感去重 helper；重复路径批量去重；
* 附件读取/写回/材料目录回收的真实影响与指纹（含内部数据子对象）。

## 7. Windows/POSIX 与未验证

* 本轮在 Windows 运行：路径去重 Windows 语义真实覆盖；POSIX 语义用 `path_dedupe_key(windows=False)` 单测（真实 POSIX 工具运行未在本机执行，交 Lead Linux 定向 CI）；
* 目录影响枚举不跟随符号链接；符号链接特殊对象按文件记录（身份读不到时按不可确认）；
* 未改 engine.py/review.py/approved_operations.py/security_gate.py/effects.py；目录变化在执行器的强制失效、limit_impacts 的引擎匹配待 Lead/B 接线（本分支提供依据与 helper + 真实工具测试）；
* 未触发完整 CI/全量矩阵（预算已用尽）；未 push/merge。