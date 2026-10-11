# 第二段验收对照表（task-5 第 4 条 → 具体定向入口）

只列**定向**入口（指定文件 / 指定用例名），不列全量。行号/用例名以候选 SHA 为准，跑之前先 `git grep` 确认还在。
后端统一：`cd backend; uv run --frozen pytest -q <文件或 ::用例名>`；前端统一：`cd frontend; npm test -- <文件>`。

| 验收项（task-5 第 4 条） | 定向入口 | 备注 |
| --- | --- | --- |
| 提交→受理→材料→理解→意图→审批→真实执行→结果卡 | `tests/test_interactive_real_model.py::test_real_path_submission_understanding_approval_execution` + `test_result_changes_with_material_and_requirement` | Lead 的端到端验收；我另外用 `verify_submission_identity.py` 补身份维度 |
| 未获准材料不进入上下文 | `tests/test_interactive_real_model.py::test_invisible_never_reaches_model_context`；`tests/test_interactive_materials.py`；`tests/test_interactive_submission.py::test_unchecked_note_and_its_links_stay_out_of_before_and_after` | 上下文与「before/after 范围」两侧都看 |
| 聊天与板面提交分离 | `tests/test_interactive_submission.py::test_save_path_never_calls_qio`、`::test_draft_is_not_submission_content`；前端 `src/interactive/__tests__/chat.test.ts`、`src/components/__tests__/b-chatDockPendingSend.test.ts` | 保存不调 QIO、草稿不是提交内容 |
| 模型操作是用户可做操作的子集 | `tests/test_interactive_execution.py::test_forbidden_operations_are_never_created`；`test_interactive_real_model.py`（批准后真实执行） | ALLOWED_INTENT_OPS |
| 两窗口草稿互不覆盖 | `src/acceptance-state-recovery/f01-drafts-two-windows.test.ts`；`src/stores/__tests__/d4CardDraftRace.verify.test.ts` | 逐键三态 + 版本守卫 |
| 本地删除不被无关服务器更新复活 | `src/acceptance-state-recovery/f07-local-removal-merge.test.ts`、`f2-legacy-local-removal-replace.test.ts`；`src/components/interactive/__tests__/f07-removal-conflict-entry.test.ts` | 删除冲突二选一入口 |
| 回执丢失 / 结果未知 | `src/acceptance-state-recovery/f10-submit-refresh.test.ts`；**我的探针** `probes/e-submit-read-once.probe.test.ts`；`src/components/interactive/__tests__/f10f11-refresh-failure.test.ts` | 我的探针用计数断言，不看文案 |
| 两窗口并发 | `f01-drafts-two-windows.test.ts`；`f10-submit-refresh.test.ts`；后端 `tests/test_interactive_submission.py::test_duplicate_submission_is_idempotent` | 并发窗口的草稿与提交 |
| 时钟偏差 | **我的** `verify_submission_identity.py` S6（「时间接近」诱饵）；`src/acceptance-state-recovery/f08-stale-read-version.test.ts` | 时间接近不得单独作为受理证据 |
| 重启查证 | 前端 `src/acceptance-state-recovery/n7-acceptance-identity.test.ts`（A 新增，候选上有）；`f1-cancel-recovery-independent-changes.test.ts`、`n2-reconfirm-result.test.ts` | 身份落本机台账、重开继续查证 |
| 同身份重试不认错提交、不重复调用模型 | **我的** `verify_submission_identity.py` S1/S2/S3；后端 `tests/test_interactive_submission.py::test_duplicate_submission_is_idempotent` | 我数库行与模型调用次数 |
| 查证只认精确匹配（最近一条 / 时间接近是诱饵） | **我的** `verify_submission_identity.py` S4/S5/S6 | 命中返回自己的事实；未用身份 404 |
| 旧理解结果不覆盖新提交 | `src/interactive/__tests__/irm-d-understanding-store.test.ts`、`a-understanding-ownership.test.ts`（A 新增）；后端 `tests/test_interactive_store_understanding.py`、`tests/test_interactive_understanding.py` | 迟到结果不得写当前记录 |
| 任务读取失败不显示「没有任务」 | `src/acceptance-state-recovery/f11-intents-load-failure.test.ts`、`src/components/interactive/__tests__/f10f11-refresh-failure.test.ts` | 保留最后成功清单 + 过期风险 |
| 重复单项 / 批量审批不重复执行 | `tests/test_interactive_real_model.py::test_repeated_approval_does_not_duplicate_execution_or_result`；`tests/test_interactive_execution.py`；前端 `src/interactive/__tests__/approval.test.ts` | 锁在核验之前、finally 释放 |
| 核验失败不放行 | `f11-intents-load-failure.test.ts`；`src/interactive/__tests__/approval.test.ts`；`tests/test_interactive_submission.py::test_acceptance_failure_keeps_changes_checks_and_baseline` | 旧快照不许放行 |
| 迁移 39/40 与历史迁移未改写 | **我的** `verify_migrations.py`（不变式 I3 自动核对 40 == 38 + 39）；`tests/test_interactive_migration_integration.py`、`tests/test_fu_w3_b01_schema.py` | 见报告 §5 |
| 提交成功路径只读一次 | **我的** `probes/e-submit-read-once.probe.test.ts` | 基线实测 board/intents/batches 各 2 次 |

一条命令跑完全部（含 SHA 绑定）：`backend/.venv/Scripts/python.exe scripts/irm-int-verify/run_stage2.py --sha <候选SHA>`。
