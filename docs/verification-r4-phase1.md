# D 独立验收（R4 三项剩余问题）· 阶段一：修复前的红证据

- 验证方：子智能体 D（独立验收，**不改实现**）
- worktree：`D:\qio-dev\qio-r4-d`（分支 `wt/r4-d`）；基线 = `origin/fix/unified-process-audit` @ `ccb5734`
- 契约：`docs/plans/2026-10-07-three-remaining-fixes.md` §1.1/§1.2/§1.3（冻结）+ §3 验收计划
- 运行（离线 venv 不含项目包，必须给 PYTHONPATH）：
  ```
  cd D:\qio-dev\qio-r4-d\backend
  $env:PYTHONPATH='src'
  .\.venv\Scripts\python.exe -m pytest tests/test_r4_*.py tests/test_audit_stream_role_verify.py tests/test_audit_attachment_binding_verify.py -q --tb=line
  ```
- 本轮结果（基线现状）：**26 红 / 12 绿 / 1 跳过（装置受限，见 §5）**
  = 问题一 7 红、问题二 9 红、问题三 4 红、按新契约对齐的两个老文件 6 红。

## 0. 本轮新增/改写的验收资产（只动 D 名下文件）

| 文件 | 内容 | 本轮 |
| --- | --- | --- |
| `backend/tests/test_r4_answer_phase_verify.py` | 问题一主验收件（9 条） | 新增 |
| `backend/tests/test_r4_attachment_retry_verify.py` | 问题二（10 条） | 新增 |
| `backend/tests/test_r4_upload_convergence_verify.py` | 问题三（7 条） | 新增 |
| `backend/tests/test_audit_stream_role_verify.py` | round3 老文件，按 §1.1 新契约改写（7 条） | 改写 |
| `backend/tests/test_audit_attachment_binding_verify.py` | round1 老文件，2 条按 §1.2 严格语义改写 | 改写 |

## 1. 问题一（回答阶段协议）· 7 红

红证据（原始输出节选，全部来自基线实跑）：

```
[FAIL] test_answer_text_visible_in_answer_area_before_call_ends
AssertionError: ('provider 还在流式输出（调用未结束）时，正式回答区一个字都没有：
  回答不是边生成边进正式回答区，而是等调用结束才一次性出现', [])
[FAIL] test_first_answer_delta_is_interim_false_and_streaming
AssertionError: ('整轮没有任何正式回答事件', [])
[FAIL] test_closing_snapshot_is_calibration_not_first_display
AssertionError: ('没有任何流式回答事件：正式回答只由结束时的累计快照交付（契约 §1.1：校准不是首次展示来源）', [])
[FAIL] test_working_call_text_stays_in_process_area_then_answer_streams
AssertionError: ('工具后的正式回答没有流式增量（只在结束时一次性出现）',
  [{'content': '工具跑完了，这是正式回答。', 'interim': False, 'streaming': False, 'delta_id': 'dl_r4_answe_3'}])
[FAIL] test_multi_tool_rounds_then_answer_streams
AssertionError: ('多轮工具之后，正式回答仍然只在结束时一次性出现（没有流式增量）',
  [{'content': '两轮工具之后的正式回答。', 'interim': False, 'streaming': False, 'delta_id': 'dl_r4_answe_4'}])
[FAIL] test_cancel_mid_answer_keeps_published_text
AssertionError: ('取消之前（provider 还在流）回答区一个字都没有：无从谈「保留已显示文字」', [])
[FAIL] test_answer_call_failure_is_honest
AssertionError: ('回答调用 500，整轮却像正常完成一样交付了空回答',
  TurnResult(final_content='本轮没有产生回答，也没有给出原因。', phase=LoopPhase.DONE, ...))
```

绿守卫（基线就是对的，保留作回归）：过程区不留正式回答副本；迟到 320ms/1.2s 工具增量不把文字移回过程区。

老文件 `test_audit_stream_role_verify.py` 按 §1.1 改写后 4 红 3 绿（文件头已写明「旧语义 = 工作调用正文经提升进回答区，已被 plan §1.1 取代」）：

```
[FAIL] test_answer_area_has_text_before_answer_call_ends :: ('provider 还在流（回答调用未结束）时，正式回答区一个字都没有', [])
[FAIL] test_answer_is_streaming_single_delta_and_calibrated :: ('整轮没有任何正式回答事件', [])
[FAIL] test_multi_tool_rounds_then_answer :: ('多轮工具之后正式回答仍然只在结束时一次性出现', [{...'streaming': False...}])
[FAIL] test_cancel_mid_answer_keeps_published_text :: 取消之前回答区一个字都没有：无从谈「保留已显示文字」
```

**尚未覆盖（阶段二补）**：前端 DOM 断言（文字渲染在正式回答容器 `.message.assistant`、过程区 `[data-test="turn-process"]` 内不含该文字，断流前后各一次）—— 单独建 `*.r4.verify.test.ts`；实机截图与「首个正文显示时间早于 provider 结束时间」的端到端时间线。

## 2. 问题二（重试复用附件）· 9 红

```
[FAIL] test_retry_reuses_saved_copy_and_new_turn_can_read_the_content
AssertionError: ('重试没有为新轮绑定附件（契约 §1.2：必须新建一条记录、复用已保存的副本）',
  {'status': 200, 'body': '{"ok":true,"accepted":true,...,"attachments":[]}'})
[FAIL] test_stealing_attachment_bound_to_another_turn_is_rejected
AssertionError: ('契约 §1.2：请求里的 id 不满足条件时必须结构化失败（409/422），不得静默丢弃后照常受理', 200, ...)
[FAIL] test_cross_topic_attachment_is_rejected_with_reason :: 同上（200 + 静默空绑）
[FAIL] test_reference_clone_rechecks_availability_after_source_removed
AssertionError: ('引用型重试也要有新记录（并如实记 missing/changed）', ...)
[FAIL] test_missing_stored_copy_is_not_silently_accepted
AssertionError: ('副本已经不在磁盘上，重试却既不失败也不绑定（静默丢附件：用户以为带上了）', ...)
[FAIL] test_multi_attachment_retry_clones_every_item :: 3 个附件（2 copy + 1 reference）一个都没克隆
[FAIL] test_double_retry_does_not_error_or_corrupt :: 两次点击都没有克隆出新副本
[FAIL] test_retry_while_source_turn_still_open :: 源轮未结束时重试拿不到附件
[FAIL] test_resend_interrupted_turn_reuses_attachments :: resend 丢掉了附件
```

`test_retry_after_history_refresh_uses_message_attachment_ids`：**跳过（装置受限）** —— API 级夹具没有能跑通的 provider，turn 不写用户消息 → 历史里没有附件行；阶段二实机（假厂商 + 真实重试入口）覆盖，不把装置问题算成产品红。

老文件 `test_audit_attachment_binding_verify.py` 两条已按 §1.2 **严格语义**改写（旧断言 = 静默跳过 + 照常受理，已被取代）：断言 409/422 + 结构化 `rejected`（含 id 与人话原因）+ 不入队/不调模型 + 原归属与历史不变 + 原轮仍能打开自己的副本。基线仍是 200 → 这两条现在红，等 B/C 合入后应转绿。

## 3. 问题三（上传失败/取消收敛）· 4 红

四条同一根因签名（工作线程已死、接收端继续在满队列上等空位）：

```
[FAIL] test_write_failure_midway_unblocks_receiver
[FAIL] test_mkdir_failure_is_reported_and_converges
[FAIL] test_temp_open_failure_is_reported_and_converges
[FAIL] test_cancel_while_worker_blocked_ends_request_and_never_commits
AssertionError: ('写入途中失败：工作线程早已失败，上传请求却一直不返回（接收端在满队列上死等空位）',
  {'status': 'timeout', 'text': '上传请求在 12.0s 内没有返回', 'elapsed': 11.985})
```

装置与手法（全部事件/闸门驱动，有限超时只用于判定失败）：
- 请求体用 httpx `ASGITransport` + 异步分块生成器（每块真的是一次 `request.stream()`），分块数 = `UPLOAD_QUEUE_DEPTH*4+8`；
- 三类受控失败：写第 1 次 `write` 抛 `OSError(28)`（写入途中）／把附件根目录做成一个文件（创建目录）／`.part` 的 `open` 抛 `PermissionError`（打开临时文件）；
- 取消：工作线程进入写盘时置位闸门事件 → 调 `DELETE /api/attachments/{id}` → 断言请求结束 + 绝不 ready + 无 `.part`；
- 客户端断开：请求体生成器中途抛错；
- 绿守卫：闸门挂住上传时其它 API 仍能推进；受控写入确实被拦到（补丁自检）。

## 4. 环境发现（如实记录）

`Settings(data_dir=tmp_path/...)` 会被**全局默认覆盖**（实测 `Settings(data_dir=...)` 与 `ctx.settings.data_dir` 都仍是 `D:\QIO-data`）：只有 `create_app(settings, conn)` 里 `AttachmentService(conn, settings.data_dir)` 拿到的是传入值。**走 `create_app` 的测试如果不钉住目录，附件会写进开发者真实数据目录** `D:\QIO-data\attachments`。本文件对应的两个 API 级验收文件已显式 `app.state.ctx.attachments.data_dir = tmp_path/'data'` 隔离；并检查过泄漏产物（0 个残留）。

## 5. 阶段一红/绿计数（基线 ccb5734）

| 文件 | 红 | 绿 | 跳过 |
| --- | --- | --- | --- |
| test_r4_answer_phase_verify.py | 7 | 2 | 0 |
| test_r4_attachment_retry_verify.py | 9 | 0 | 1 |
| test_r4_upload_convergence_verify.py | 4 | 3 | 0 |
| test_audit_stream_role_verify.py（按 §1.1 改写） | 4 | 3 | 0 |
| test_audit_attachment_binding_verify.py（2 条按 §1.2 改写） | 2 | 4 | 0 |
| **合计** | **26** | **12** | **1** |

阶段二（Lead 集成后）：把上面红行改成 PASS，**保留本文件的 FAIL 行作对照**，并补实机（uvicorn + vite + 假厂商 + msedge/Playwright 截图）与全量复跑。
