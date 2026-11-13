# #28 返修：附件工具必须解析「提交时核验过的那一份内容」（A→B→A 竞态）

日期：2026-11-14
worktree：D:\qio-dev\pm-qio032-integration（分支 fix/pm-qio032-integration，HEAD 551a2e99）
写入范围：backend/src/agent/tools/attachment_tools.py、backend/src/agent/interactive/materials.py、
backend/tests/pm_qio032_material_scope_test.py。**未改前端、未改 docs、未改迁移、未改 board_tools.py。**

---

## 1. 已证实的残留缺陷（返修前）

上一轮 #28 的 read_attachment 采用「读前核对一次 + 读后核对一次」：

1. `_submission_material_guard` 用 `submission_attachment_scope` 核对提交范围与冻结 sha256；
2. `_read_content` 让**解析器按路径**打开源文件（`_extract_text(att, path, ...)`）；
3. `_submission_material_changed_after_read` 再算一次同一路径的哈希。

验收构造的 A→B→A：源文件在解析前是 A、解析时变成同大小 B、解析后恢复 A。
两次核对都通过（读前 A、读后 A），但第 2 步解析器读到的是 B —— 工具把 B 正文交给了模型。

**已用探针实测复现**（临时文件，跑完即删，未提交）：
把 `_load_verified_submission_material` 换成「直接返回源路径」的旧行为桩，
在 `_extract_text` 入口把源文件换成同大小 B，输出：

```
PROBE-LEAKED-B: True
```

即旧行为确实会把 B 交付出去 —— 新增的回归用例因此是**有意义的**（不是恒真断言）。

---

## 2. 修法：从「绑定提交的附件读取入口」修，核验与解析用同一份字节

### 2.1 materials.py（核验读取的唯一事实源）

新增三个函数，把「判定 / 路径解析」与「纯文件 I/O」分开（线程纪律：附件服务与数据库
连接都不是线程安全的，只能在事件循环线程上用）：

| 函数 | 线程 | 职责 |
| --- | --- | --- |
| `submission_material_verified_path(service, att, *, recorded)` | 事件循环 | 前置判定（附件不存在 / 非 ready / 没冻结 sha256 → 不读）+ 读取授权 + 解析真实路径 |
| `read_verified_bytes_at_path(path, recorded_sha, *, cancel)` | 工作线程 | **一次读取 + 增量算哈希**；分块（1MB）推进，每块检查协作取消；超 `MATERIAL_HASH_MAX_BYTES` 边读边停 |
| `read_verified_submission_material_bytes(...)` | 事件循环 | 上面两步的组合入口（保留给同步调用方） |

`verified_submission_material_bytes` 保留为兼容入口（`submission_materials` 仍在用），
内部改为调用上面两个函数 —— 判定与措辞只有一份。

新增常量：
- `SUBMISSION_ATTACHMENT_READ_FAILED_NOTICE = "读不到提交时的附件，请重新提交。"`
  （与板面 / 材料入口的 `SUBMISSION_READ_FAILED_NOTICE` 分开：这里说的是「附件」）；
- `_MATERIAL_UNREADABLE_REASON` / `_MATERIAL_CHANGED_REASON` / `_MATERIAL_TOO_LARGE_REASON`
  （核验失败的三类可读原因，附件读取与材料解析共用同一份措辞）；
- `MaterialReadCancelled`（工作线程在块边界检查到取消标志）。

### 2.2 attachment_tools.py（ReadAttachmentTool）

- `_read()`：范围核对通过且 `guard["required"]` 时，走**核验字节**路径；否则原样走
  `_read_content()`（普通对话轮 / 当轮显式附件的行为完全不变）。
- `_load_verified_submission_material()`：事件循环线程上解析路径 + 核对冻结版本 →
  工作线程里一次读取 + 校验 + 落**受控临时文件**（保留原扩展名，解析器据此判断文件类型）。
- `_read_content(..., verified_loader=, guard=)`：拿到临时路径后**只解析这一份**；
  `try/finally` 保证成功、异常、取消三条路径都 `_cleanup_temp_dir`。
- `_parse_content()`：原解析分支整体搬出（行为不变），只认传进来的那一个路径。
- 删除 `_submission_material_changed_after_read()`（读后按路径复核 —— 正是竞态窗口本身）。
- `_submission_read_failure()`：统一失败结果 —— 用户侧一句短文案
  「读不到提交时的附件，请重新提交。」，具体原因进 `data.diagnostics`，
  `next_step=resubmit`，`code=material_source_changed`，`recoverable=False`。
- 提交范围不可用（快照读不到）与 stale（源内容已变化 / 无法核对）两路都改用同一句短文案。
- 绑定提交的轮次里源文件读不到时，也按「读不到提交时的附件」失败（不退回通用读取失败）。

### 2.3 保留的既有行为（逐条核对）

| 要求 | 现状 |
| --- | --- |
| 读取授权 `attachment_access.authorize_read` / `read_allowed` | 未改：仍在 `_read_content` 里，核验路径也先过 `_submission_material_guard`（含 `read_allowed`） |
| 提交范围 `submission_attachment_scope` | 未改：`_submission_material_guard` 原样调用 |
| 大小上限 `MATERIAL_HASH_MAX_BYTES` | 保留并**加严**：分块读取时边读边判，超限立即停（不再先整份读进内存） |
| 分段读取 offset / limit / fragment_offset | 未改：核验路径最终仍走 `_parse_content` 的同一套分页 |
| 隐藏 / 排除 / 提交后新增 → `not_in_submission_scope` | 未改：仍由 `submission_attachment_scope` 的 allowed 判定 |
| 普通对话轮 / 当轮显式附件 | 未改：`guard["required"]` 为假时不走核验路径 |
| 板面快照行为（board_tools 的 `submission_snapshot_unavailable`） | **未动**（本文件不在写入范围） |

---

## 3. 新增回归测试（backend/tests/pm_qio032_material_scope_test.py）

| 用例 | 断言 |
| --- | --- |
| `test_parse_reads_verified_bytes_not_source_path` | **A→B→A 竞态**：解析器入口把源文件换成同大小 B、解析后换回 A。断言结果里**不含 B**；成功时含 A 正文；失败时必须是 `material_source_changed` + 短文案 + `next_step=resubmit`；解析器读的路径**不是源路径**；核验临时目录已清理 |
| `test_stable_same_size_change_is_rejected` | 稳定变化的同大小文件（读的时候就是 B）→ 明确失败，不交付 B |
| `test_verified_temp_dir_is_cleaned_on_parse_failure` | 解析抛 OSError → 临时目录清理干净（异常路径） |
| `test_verified_temp_dir_is_cleaned_on_cancel` | 解析期间取消 → 临时目录清理干净（取消路径） |

A→B→A 用例的换入换出点：`attachment_tools._extract_text` 的入口（解析器真正开始读文件的
那一刻），`finally` 里换回 A —— 正好落在「读前核对之后、读后核对之前」的窗口里。

---

## 4. 实际跑了什么命令、结果

工作目录 `D:\qio-dev\pm-qio032-integration`，解释器 `backend\.venv\Scripts\python.exe`。

```
# 1) 返修前基线（两个必跑文件）
python -m pytest backend\tests\pm_qio032_material_scope_test.py backend\tests\pm_qio032_submit_version_basis.py -q -p no:cacheprovider
→ 30 passed

# 2) 返修后（新增 4 例）
python -m pytest backend\tests\pm_qio032_material_scope_test.py backend\tests\pm_qio032_submit_version_basis.py -q -p no:cacheprovider
→ 34 passed

# 2b) 8 个 pm_qio032 文件一起跑（含 -v 明细，落盘 evidence-28b-pytest.txt）
python -m pytest backend\tests\pm_qio032_material_scope_test.py backend\tests\pm_qio032_submit_version_basis.py backend\tests\pm_qio032_submit_material_versions.py backend\tests\pm_qio032_submit_first_round_materials.py backend\tests\pm_qio032_submit_authorization_facts.py backend\tests\pm_qio032_submit_empty_semantics.py backend\tests\pm_qio032_submit_intents_event.py backend\tests\pm_qio032_submit_replay_lookup.py -q -p no:cacheprovider -v
→ 62 passed, 1 warning in 18.32s

# 3) 新增用例单独跑
python -m pytest backend\tests\pm_qio032_material_scope_test.py -q -p no:cacheprovider -k 'verified or same_size or parse_reads'
→ 5 passed

# 4) 其余 pm_qio032 提交类文件（显式列文件名：pm_qio032_submit_*.py 不匹配默认收集规则）
python -m pytest backend\tests\pm_qio032_submit_material_versions.py backend\tests\pm_qio032_submit_first_round_materials.py backend\tests\pm_qio032_submit_authorization_facts.py backend\tests\pm_qio032_submit_empty_semantics.py backend\tests\pm_qio032_submit_intents_event.py backend\tests\pm_qio032_submit_replay_lookup.py -q -p no:cacheprovider
→ 28 passed

# 5) 附件读取 / 解析 / 媒体 / 事实相关
python -m pytest backend\tests\test_attachment_tools.py backend\tests\test_acc_a_f21_scheduling.py backend\tests\test_attachment_context.py backend\tests\test_attachment_content_and_background.py -q -p no:cacheprovider
→ 42 passed

python -m pytest backend\tests\test_acc_a_f01_parse_bounds.py backend\tests\test_acc_a_f22_encoding.py backend\tests\test_acc_a_f02_line_bounds.py backend\tests\test_acc_a_f23_paging.py backend\tests\test_attachments_contract_verify.py backend\tests\test_attachment_source_refs.py backend\tests\test_attachment_retry_reuse.py backend\tests\test_media_delivery.py backend\tests\test_records_facts_file_read.py backend\tests\test_r9h4_fs_batch.py -q -p no:cacheprovider
→ 118 passed

python -m pytest backend\tests\test_attachments_api.py backend\tests\test_preview_service.py backend\tests\test_r4_attachment_retry_verify.py backend\tests\test_attachments_service.py backend\tests\test_attachments_failure_reason.py -q -p no:cacheprovider
→ 120 passed, 1 skipped

# 6) 互动材料 / 理解 / 板面工具（#28 契约波及面）
python -m pytest backend\tests\test_interactive_materials.py backend\tests\test_interactive_understanding.py backend\tests\test_interactive_store_understanding.py backend\tests\test_irm_c_understanding_lifecycle.py -q -p no:cacheprovider
→ 43 passed, 12 skipped

python -m pytest backend\tests\test_p3_b_tools.py backend\tests\test_p3_d_tools_registry.py backend\tests\test_p2_d_participation_endpoints_display_version.py backend\tests\test_p3_b_service.py backend\tests\test_p3_d_intent_resume_retry.py -q -p no:cacheprovider
→ 81 passed

# 7) 静态边界（新代码不得引入提示词 / 旧路径）
python -m pytest backend\tests\test_p2_d_static_boundaries.py backend\tests\test_p3_d_static_boundaries.py backend\tests\test_p4_c_legacy_cleanup.py -q -p no:cacheprovider
→ 30 passed
```

**未跑**：全量 pytest（纪律：只做本地定向验证，由 lead 统一跑）；CI；前端测试（未改前端）。

---

## 5. 仍未验证 / 已知边界

1. **真实并发替换**未验证：用例用 monkeypatch 在解析器入口同步换文件，是确定性的可控竞态；
   真实 OS 层面的「读一半被替换」没有覆盖（分块读取期间换文件会读到混合内容，但哈希校验
   会失败 → 明确失败，不会交付混合内容）。
2. **超过 64MB 的提交材料**仍不交付（`MATERIAL_HASH_MAX_BYTES`）：无法在核对后交付，
   如实要求重新提交。这是上一轮已声明的产品决定，本次未改。
3. **图片 / 音频 / 视频**在绑定提交轮次里同样走核验字节（临时文件保留扩展名），
   但本次没有为这三类单独加 A→B→A 用例（文本路径已覆盖同一段核验代码）。
4. **前端**未改：短文案通过工具失败 error / 交互块传递；若前端要按 code 做专门展示，
   需要前端组另接。
5. 全量 pytest 未跑（由 lead 统一跑）。
