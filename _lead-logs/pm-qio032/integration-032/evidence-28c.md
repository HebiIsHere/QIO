# #28b 返修：取消核验时清理临时文件（路径交接前的归属）

日期：2026-11-14
worktree：D:\qio-dev\pm-qio032-integration（分支 fix/pm-qio032-integration，HEAD acdb671c）
写入范围：backend/src/agent/tools/attachment_tools.py、backend/tests/pm_qio032_material_scope_test.py。
**未改前端、未改 docs、未改迁移、未改 board_tools.py、未改 materials.py。未 git commit。**

---

## 1. 已证实的残留缺陷（返修前）

`_load_verified_submission_material` 在工作线程里创建临时目录（`_verified_material_dir`）
并写入 `verified.<ext>`，然后 `return (temp_dir, path, "")`。若此时任务被取消，
`_run_worker` 会 `cancel.set()` 并等待线程（有界 `WORKER_STOP_WAIT_SECONDS`），但
**调用方拿不到 temp_dir** —— `_read_content` 的 `finally: _cleanup_temp_dir(temp_dir)`
根本不会执行 → 残留 `verified.txt`。

既有取消用例（`test_verified_temp_dir_is_cleaned_on_cancel`）只覆盖「解析已经开始」的阶段：
那时目录已经交接给调用方，`temp_dir` 已经赋值，所以它一直是绿的 —— 缺陷在交接**之前**。

**红证据**（`evidence-28c-red-proof.log`，把源码修复 stash 掉、只留新用例）：

```
FAILED ...::test_verified_temp_dir_is_cleaned_when_cancelled_before_handoff
FAILED ...::test_verified_temp_dir_is_cleaned_when_cancelled_while_writing
FAILED ...::test_verified_temp_dir_is_cleaned_when_worker_raises
FAILED ...::test_verified_temp_dir_is_swept_after_parser_releases_file
FAILED ...::test_run_worker_discards_result_when_waiter_already_cancelled
FAILED ...::test_abandoned_worker_result_is_cleaned_after_late_finish
FAILED ...::test_temp_dir_cleanup_defers_while_parser_still_holds_file
```

（`test_verified_temp_dir_is_cleaned_when_write_fails` 在旧实现下是绿的：旧代码在
`except OSError` 里显式 `_cleanup_temp_dir`。它保留为**回归护栏**，防止新写法把
写盘失败路径的清理弄丢 —— 见 §4 的变异验证。）

---

## 2. 修法：明确临时文件归属（交接前归线程，交接后归解析流程）

### 2.1 归属规则（attachment_tools.py）

| 阶段 | 谁负责清理 | 实现 |
| --- | --- | --- |
| 目录已创建、**尚未交接**（写盘中 / 交接前被取消 / 线程内任何异常） | **工作线程自己** | `work()` 里 `try/finally` + `handed_off` 标记：`finally` 中 `if not handed_off: _cleanup_temp_dir(temp_dir)` |
| 交接成功（`return temp_dir, path, ""`） | **解析流程**（调用方） | `_read_content` 的 `finally: _cleanup_temp_dir(temp_dir)`（原有行为，未改） |
| 等待方先退出、线程**稍后**才交接 | **`_run_worker` 收尾** | 新增 `on_abandoned` 回调；`_load_verified_submission_material` 传入 `discard`，清理 `result[0]` |
| 解析仍持有文件（Windows 删不掉） | **延迟清理**，线程退出后再删 | `_DEFERRED_TEMP_DIRS` 登记 + `_sweep_deferred_temp_dirs()` |

关键点：`_run_worker` 在取消时 `cancel.set()` 并**等待线程**（有界），所以线程内的
`finally` 一定会跑；即使等待方放弃等待（超 `WORKER_STOP_WAIT_SECONDS`），
`future.add_done_callback(_abandon_later)` 也会在线程真正结束时收尾 —— 不会留下
无人认领的临时文件。

### 2.2 不提前删除（Windows 打开文件行为）

`_cleanup_temp_dir` 不再用 `shutil.rmtree(ignore_errors=True)` 假装删掉：

* `_remove_temp_dir(path)` 返回 False（`OSError`，Windows 上文件仍被打开）时，
  目录登记进 `_DEFERRED_TEMP_DIRS`；
* `_sweep_deferred_temp_dirs()` 在三个时机重试：`_run_worker` 起线程前、
  取消收尾时、`_cleanup_temp_dir` 入口；
* 登记表有上限（`_DEFERRED_TEMP_DIRS_MAX = 64`），极端情况下不无限增长。

清理范围**仅限本模块创建的核验材料目录**（`verified-material-*` / `qio-verified-material-*`），
原附件一律不动（用例逐条断言 `source.read_bytes() == body`）。

### 2.3 未改动的既有语义（逐条核对）

| 要求 | 现状 |
| --- | --- |
| 只解析已核验字节 | 未改：仍是 `read_verified_bytes_at_path` 一次读取 + 冻结 sha256 校验 → 临时文件 → 解析器只读这一份 |
| 提交范围 / 文件版本 / 读取授权 | 未改：`_submission_material_guard`、`submission_material_verified_path`、`attachment_access.authorize_read` 全部原样 |
| 旧快照 / 源文件无法核验 → 重新提交提示 | 未改：`_submission_read_failure` 原样（`code=material_source_changed`、`next_step=resubmit`、短文案） |
| 普通对话轮 / 当轮显式附件 | 未改：`guard["required"]` 为假时不走核验路径 |
| 媒体（图片 / 音频 / 视频）读取 | 未改：`_read_image` / `_read_audio` / `_read_video` 一行未动 |

---

## 3. 新增回归测试（backend/tests/pm_qio032_material_scope_test.py，+9 例）

全部直接走 `ReadAttachmentTool.run`（除两条纯单元用例），用可控阻塞分别取消三个阶段，
并**等工作线程真正结束**后再断言。

| 用例 | 覆盖 | 断言 |
| --- | --- | --- |
| `test_verified_temp_dir_is_cleaned_when_cancelled_before_handoff` | 阶段①：目录已创建、尚未交接 | 目录与文件清理干净；原附件字节不变 |
| `test_verified_temp_dir_is_cleaned_when_cancelled_while_writing` | 阶段②：临时文件写入中（已落盘、未交接） | 同上（先断言 `verified.txt` 真的落盘过） |
| `test_verified_temp_dir_is_cleaned_when_cancelled_during_parse` | 阶段③：解析已开始（已交接） | 同上 |
| `test_verified_temp_dir_is_cleaned_when_write_fails` | 写盘 OSError | 线程内清理；结果 `material_source_changed` + 「写临时文件失败」 |
| `test_verified_temp_dir_is_cleaned_when_worker_raises` | 线程内非 OSError 异常 | 线程内 `finally` 清理；结果 `material_source_changed` |
| `test_run_worker_discards_result_when_waiter_already_cancelled` | 等待方先退出、线程后交接 | `on_abandoned` 收到被丢弃的结果 |
| `test_abandoned_worker_result_is_cleaned_after_late_finish` | 端到端：`WORKER_STOP_WAIT_SECONDS` 压到 0.05s，等待方先放弃等待 | 线程晚到的交接结果被收尾，临时目录清理干净；原附件不变 |
| `test_temp_dir_cleanup_defers_while_parser_still_holds_file` | 解析仍持有文件（Windows） | 不假装删掉；登记延迟清理；句柄关闭后 sweep 删掉 |
| `test_verified_temp_dir_is_swept_after_parser_releases_file` | 端到端：解析线程开着临时文件、等待方先放弃等待 | 延迟清理登记清空、目录最终删掉；原附件不变 |

三条取消用例都断言「没有成功结果被交付」（`pytest.raises(asyncio.CancelledError)`），
并 `_join_threads` 确认工作线程真的跑完（线程池线程退出后仍存活，不能按 `is_alive` 判）。

---

## 4. 变异验证（确认用例不是恒真）

| 变异 | 结果 |
| --- | --- |
| 去掉线程内 `finally` 的 `_cleanup_temp_dir` | `write_fails` / `worker_raises` 变红（`qio-verified-material-*` 残留） |
| 去掉 `future.add_done_callback(_abandon_later)` | `late_finish` 变红（晚到交接的目录残留） |
| `_cleanup_temp_dir` 改回 `rmtree(ignore_errors=True)` | `defers_while_parser` / `parser_releases_file` 变红（假装删掉了） |
| 源码整体 stash 掉（旧实现） | 7 条新用例变红（见 §1） |

---

## 5. 实际跑了什么命令、结果

工作目录 `D:\qio-dev\pm-qio032-integration`，解释器 `backend\.venv\Scripts\python.exe`。

```
# 1) 本文件全量（37 → 38 例；新增 9 例，原 29 例）
python -m pytest backend\tests\pm_qio032_material_scope_test.py -p no:cacheprovider -q
→ 38 passed in 4.45s        （连跑 3 次均 38 passed，无抖动）

# 2) pm_qio032 八个文件一起跑（含本文件）
python -m pytest backend\tests\pm_qio032_material_scope_test.py backend\tests\pm_qio032_submit_version_basis.py backend\tests\pm_qio032_submit_material_versions.py backend\tests\pm_qio032_submit_first_round_materials.py backend\tests\pm_qio032_submit_authorization_facts.py backend\tests\pm_qio032_submit_empty_semantics.py backend\tests\pm_qio032_submit_intents_event.py backend\tests\pm_qio032_submit_replay_lookup.py -p no:cacheprovider -q
→ 71 passed, 1 warning in 33.85s

# 3) 附件读取 / 解析 / 媒体 / 事实（#28 波及面）
python -m pytest backend\tests\test_attachment_tools.py backend\tests\test_attachment_context.py backend\tests\test_attachment_content_and_background.py backend\tests\test_acc_a_f21_scheduling.py backend\tests\test_acc_a_f01_parse_bounds.py backend\tests\test_acc_a_f02_line_bounds.py backend\tests\test_acc_a_f22_encoding.py backend\tests\test_acc_a_f23_paging.py backend\tests\test_attachment_source_refs.py backend\tests\test_attachment_retry_reuse.py backend\tests\test_records_facts_file_read.py backend\tests\test_attachments_contract_verify.py -p no:cacheprovider -q
→ 137 passed, 1 warning in 47.22s

# 4) 互动材料 / 板面工具（#28 契约波及面）
python -m pytest backend\tests\test_interactive_materials.py backend\tests\test_interactive_understanding.py backend\tests\test_interactive_store_understanding.py backend\tests\test_irm_c_understanding_lifecycle.py backend\tests\test_p3_b_tools.py backend\tests\test_p3_d_tools_registry.py backend\tests\test_p2_d_participation_endpoints_display_version.py -p no:cacheprovider -q
→ 99 passed, 14 skipped in 11.63s

# 5) 静态边界 / 导入
python -m pytest backend\tests\test_p2_d_static_boundaries.py backend\tests\test_p3_d_static_boundaries.py backend\tests\test_p4_c_legacy_cleanup.py backend\tests\test_import_smoke.py backend\tests\test_import_order.py -p no:cacheprovider -q
→ 36 passed in 44.22s

# 6) 媒体 / 预览 / 重试 / 附件 API
python -m pytest backend\tests\test_media_delivery.py backend\tests\test_preview_real_files.py backend\tests\test_preview_service.py backend\tests\test_r4_attachment_retry_verify.py backend\tests\test_r9h4_fs_batch.py -p no:cacheprovider -q
→ 107 passed, 1 skipped

python -m pytest backend\tests\test_attachments_api.py backend\tests\test_attachments_failure_reason.py backend\tests\test_attachment_upload_convergence.py backend\tests\test_attachment_identity.py backend\tests\test_attachment_history.py -p no:cacheprovider -q
→ 54 passed

# 7) 工作线程 / 取消相关
python -m pytest backend\tests\test_tool_worker.py backend\tests\test_tool_parallel_cancel.py backend\tests\test_sr_d_fs_threading.py backend\tests\test_sr_d_fs_cancel.py backend\tests\test_attachments_service.py backend\tests\test_attachments_contract_verify.py backend\tests\test_attachment_source_refs.py backend\tests\test_attachment_retry_reuse.py backend\tests\test_records_facts_file_read.py -p no:cacheprovider -q
→ 136 passed

# 8) 语法 / 空白
python -m py_compile src/agent/tools/attachment_tools.py tests/pm_qio032_material_scope_test.py → ok
git diff --check -- backend/ → 无输出（干净）

# 9) 端到端残留检查（系统临时目录）
#    先清空 %TEMP%\qio-verified-material-*（红证据 / 变异验证留下的），再跑一遍本文件：
Get-ChildItem $env:TEMP -Directory -Filter "qio-verified-material-*" | Remove-Item -Recurse -Force
python -m pytest backend\tests\pm_qio032_material_scope_test.py -p no:cacheprovider -q
→ 38 passed in 19.50s
Get-ChildItem $env:TEMP -Directory -Filter "qio-verified-material-*"
→ count=0   （跑完一个临时目录都不剩）
```

> 注：红证据 / 变异验证跑完后 `%TEMP%` 下确实留下了 15 个 `qio-verified-material-*`
> 目录 —— 那正是旧实现（以及被变异的实现）的残留，反过来印证了缺陷真实存在；
> 修复后的干净跑法一个都不剩。

**未跑**：全量 pytest（纪律：只做本地定向验证，由 lead 统一跑）；CI；前端测试（未改前端）。

---

## 6. 仍未验证 / 已知边界

1. **真实 OS 级取消时序**未覆盖：用例用 `threading.Event` 在确定的位置阻塞，是可控的
   确定性时序；真实机器上「取消恰好落在 `mkdtemp` 与 `write_bytes` 之间」的窗口没有
   逐指令覆盖（但归属规则对整段 `try` 生效，不依赖具体指令位置）。
2. **进程被强杀**（`kill -9` / 断电）时线程内 `finally` 不会跑：临时目录仍会残留。
   目录名带固定前缀（`verified-material-` / `qio-verified-material-`）便于按前缀回收，
   但**本次没有实现启动时的前缀回收**（不在本任务范围，且需要先确认 QIO 数据目录的
   清理策略）。
3. **延迟清理登记表是进程内内存态**：进程退出时未清空的登记项会随进程消失（目录残留），
   与第 2 点同一类边界。
4. **Windows 专属行为**：`test_temp_dir_cleanup_defers_while_parser_still_holds_file` 的
   「删不掉」断言只在 `sys.platform == "win32"` 下检查；POSIX 上打开的文件可以删，
   该用例退化为「清理成功」的弱断言。
5. **超时路径**（`timeout_ms` 触发的取消）与显式 `task.cancel()` 走的是同一条
   `_run_worker` 取消分支，本次只测了后者。
6. 全量 pytest 未跑（由 lead 统一跑）。
