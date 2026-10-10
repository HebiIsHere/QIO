# 独立验证报告 · 阶段二：集成分支复跑、跨层组合与实机取证（acc）

- 计划：`docs/plans/2026-10-09-process-attachment-audit-consolidation.md`（§二 C1—C8、§六 验收、§七/§八集成期裁定）
- 基线审计 SHA：`e3d1205eafef28f37db33c0c43a48a32b102de1b`（fix/attachment-send-cancel-integrity，= origin 同分支）
- 集成分支：`fix/process-attachment-audit-consolidation`；本报告对应的验证运行 SHA：`b947e14`（其后只追加文档提交；产品代码不再变化）
- 阶段一报告：`docs/verification-acc-phase1.md`（基线反例，独立验证者 acc-f2）
- 实机取证：`docs/verification-shots-acc/`（10 张截图、`summary.json`、`visual-report.md`、原始日志 `acc-phase2-raw/`）
- 装置脚本：`scripts/verify-acc-phase2.ps1` + `scripts/verify-acc-phase2-shots.mjs`（可复跑）

## 〇、角色与诚实边界

1. 独立验证者 acc-f2 完成阶段一（基线反例与报告）与阶段二的反例/组合用例/实机脚本；其阶段二回合两次异常终止（无收尾消息）。
2. 实机取证由 acc-vis 收口：4 项 FAIL 全部用 DOM/文本级证据定性为**装置问题**（不是产品缺陷），修装置后 **11/11 PASS**（`docs/verification-shots-acc/visual-report.md`）。
3. 本报告的最终复跑（后端全量、前端全量、类型检查、文档检查、关键反例集）由 Lead 在集成树上执行，命令与真实结果在 §二；acc-f2 在 1d1a7b6 上的原始日志保留在 `docs/verification-shots-acc/acc-phase2-raw/`，二者 SHA 不同，不混用。
4. 模型调用一律本机假 provider（真 SSE 分片、真 500、真断流）；没有使用真实 API Key，没有联网依赖。
5. 未实测项见 §六，不用组件/编译通过冒充实机，也不用浏览器证据冒充原生壳。

## 一、基线反例 → 本轮结果（F01—F25）

口径：**已有修复** = 基线之前已落地且反例通过；**本轮修复** = 本轮分支内的提交；**新增 F25** = 集成期相邻路径发现。

| 项 | 基线判定 | 本轮处置（提交） | 反例 / 测试（集成树） | 最终结果 |
| --- | --- | --- | --- | --- |
| F01 DOCX/XLSX 解压与解析无界 | 仍成立（A 负责人子进程资源探针红灯） | 本轮修复（3a65109） | backend/tests/test_acc_a_f01_parse_bounds.py | 通过（全量内） |
| F02 超长单行整行分配 | 仍成立（负责人探针：1 亿字节单行 → ~200MB 峰值） | 本轮修复（3a65109） | test_acc_a_f02_line_bounds.py | 通过 |
| F03 noopener 误判 / URL 过早撤销 | **独立红（2 失败）** | 本轮修复（79c81bc） | frontend/src/services/__tests__/acc_f_03_external_open.test.ts（2）+ acc_d_attachment_open.test.ts | 通过 |
| F04 历史重传结果不可见 | 仍成立（D 负责人反例） | 本轮修复（79c81bc） | acc_d_reupload_result.test.ts | 通过 |
| F05 排队改变活动 turn 归属 | **已有修复且反例通过**（祖先 2b204d7）+ 本轮前端按 turn 身份强化 | 本轮修复（d7df19e） | backend/tests/test_acc_f_05_turn_ownership.py（后端跨层）+ acc_f_05_active_turn_ownership.test.ts + acc_c_turn_identity.test.ts | 通过 |
| F06 SSE 无结束标记仍算完成 | **独立红**（status=completed / reason_code=none） | 本轮修复（db0afba + 39ad19d：新增终态 incomplete） | test_acc_f_06_incomplete_stream.py + test_acc_b2_incomplete_status.py | 通过 |
| F07 流式事件绕过脱敏（含跨分块） | **独立红**（完整 [0,1]、跨块 [1,2] 泄漏） | 本轮修复（db0afba） | test_acc_f_07_stream_redaction.py + test_acc_b_redact_stream.py | 通过 |
| F08 异步结果跨话题写入 | 仍成立（D 负责人反例） | 本轮修复（79c81bc） | acc_d_composer_topic_scope.test.ts | 通过 |
| F09 重传失败仍删旧附件 | 仍成立 | 本轮修复（79c81bc） | acc_d_composer_reupload_replace.test.ts | 通过 |
| F10 暂时恢复失败被当永久无效 | 仍成立 | 本轮修复（79c81bc） | acc_d_restore_pending.test.ts | 通过 |
| F11 附系统注释导致整段回答重复 | **前端独立红（正文 2 条）** | 本轮修复（db0afba 后端独立字段 + 48e2126/ee2e798 前端按 turn 身份校准与独立「系统事实」区域） | test_acc_f_11_annotation_final_answer.py + acc_f_11_final_answer_annotation.test.ts + acc_c_final_answer.test.ts + acc_c2_incomplete_ui.test.ts | 通过 |
| F12 排队轮取消无结束事实 | **独立红**（B 没有 TURN_END） | 本轮修复（48e2126 前端 + 39ad19d 后端补发恰好一条 TURN_END，actions=retry） | test_acc_f_12_queued_cancel_facts.py + test_acc_b2_queued_cancel_end.py + acc_c_queued_cancel.test.ts + acc_c2_incomplete_turn.test.ts | 通过 |
| F13 列表内代码块/嵌套/表格被压平 | **独立红（3 失败 + 1 对照绿）** | 本轮修复（6b5e5ba） | acc_c_markdown.test.ts + acc_f_13_markdown_lists.test.ts；实机 S8 结构保留 | 通过 |
| F14 折叠耗时与明细口径冲突 | 仍成立 | 本轮修复（47de6b7） | acc_c_timing.test.ts；实机 S2「总耗时 = 排队 + 执行」 | 通过 |
| F15 绑定跨 await 使用失效归属 | 仍成立 | 本轮修复（ad26095，Lead 接手） | test_acc_e_f15_binding_boundary.py | 通过 |
| F16 多附件重试前序克隆缺回滚 | 仍成立 | 本轮修复（ad26095） | test_acc_e_f16_clone_rollback.py | 通过（负载下曾出现一次 flake，单跑与最终全量均绿，见 §二.4） |
| F17 副本存在但不可读仍放行 | 仍成立 | 本轮修复（76e5c72） | test_acc_e_f17_readability.py | 通过 |
| F18 大文件引用消失仍可重试 | 仍成立 | 本轮修复（b55efa2） | test_acc_e_f18_reference_retry.py | 通过 |
| F19 未声明前缀中断清空文字 | 仍成立 | 本轮修复（db0afba） | test_acc_b_prefix_interrupt.py | 通过 |
| F20 重定位旧后台结果覆盖新内容 | 仍成立 | 本轮修复（ad26095，Lead 接手） | test_acc_e_f20_relocate_version.py | 通过 |
| F21 同步读取阻塞事件循环 | 仍成立（注入 350ms 延迟） | 本轮修复（3a65109） | test_acc_a_f21_scheduling.py | 通过 |
| F22 嗅探截断 UTF-8 误判 GBK | 仍成立 | 本轮修复（3a65109） | test_acc_a_f22_encoding.py | 通过 |
| F23 分页事实与实际不符 | 仍成立 | 本轮修复（3a65109） | test_acc_a_f23_paging.py | 通过 |
| F24 missing 重试致前端放弃 | 仍成立 | 本轮修复（ad26095 后端「已受理且正在准备」+ 前端 track 等待规则） | test_acc_e_f24_preparing_semantics.py + acc_d_wait_settled.test.ts | 通过 |
| **F25（新增·相邻路径）** TOOL_END 出口未脱敏 | **独立红**（合成敏感值原样进 SSE） | 本轮修复（59c3766，Lead 接手） | test_acc_f_25_tool_end_redaction.py + test_acc_b_redact_stream.py | 通过 |

### 说明（不把「测试改了」当「产品修了」）

- F05 的基线红灯在**后端跨层**已是绿的（祖先提交 2b204d7 的修复），阶段一据此判「已有修复且反例通过」；本轮前端仍按 turn 身份重构归属（d7df19e），并不把旧修复重复宣称。
- F06/F11/F12 的验证者断言在阶段一做过一次**契约对齐**（F11 注释改独立字段、F06 status 允许 incomplete、F12 可恢复动作 resend → retry）。对齐不是放宽：正文唯一性、注释完整性、结束事实与落库断言都保留；理由与真实基线结果写在 `docs/verification-acc-phase1.md`。
- F01/F02/F21/F22/F23、F04/F08/F09/F10、F14、F15—F20/F24 的**基线红灯**来自各负责人自己的反例运行（原始记录见 `_acc-evidence/` 与各自提交内的测试）；Lead 本轮复跑的是**修复后**的集成树，未逐项重放这些项的基线红灯——这一点如实标注，不当作独立复现。

## 二、Lead 在集成树上的最终复跑（真实命令与结果）

工作树：`D:\qio-dev\qio-acc`（分支 fix/process-attachment-audit-consolidation，HEAD b947e14，复跑前后 `git status` 干净）。

1. **后端全量**：`cd backend; uv run --frozen pytest -q -p no:warnings`
   结果：**退出码 0，无 F/E（全绿，含 1 项 skip）**。收集数由 `uv run --frozen pytest --collect-only` 取到 **2611 tests collected**；汇总行未打印是因为 `pyproject.toml` 的 `addopts = "-q"` 与命令行的 `-q` 叠加成 `-qq`，pytest 会省掉计数行 —— 所以这里用收集数 + 退出码，而不是引用一个没出现的数字。原始日志：`_acc-evidence/lead-be2.txt`。
2. **前端全量**：`cd frontend; npx vitest run`
   结果：**Test Files 156 passed (156)；Tests 1281 passed (1281)**，退出码 0。
3. **类型检查**：`npx vue-tsc --noEmit` → 退出码 0（无输出）。
4. **文档一致性**：`python scripts/check_docs.py` → 「文档一致性检查通过（35 个里程碑条目）」，退出码 0。
5. **关键反例集（Lead 亲跑，含跨层）**：F05/F06/F07/F11/F12 的 acc-f 后端用例 + acc_b2 + acc_b + acc_e + acc_e2 + F25 + r8 回归 + turn_manager/state_sequences/turn_timing_facts —— 全绿（与第 1 项同一次全量内）。
6. **负载型 flake 记录**：`test_acc_e_f16_clone_rollback.py::test_cancel_during_second_clone_rolls_back_first_clone` 与 `test_timing_contract_verify.py::test_queued_turn_reports_queue_ms_but_not_inside_duration` 在并行重负载的两轮全量里各出现过一次，单跑即绿、第三轮与最终全量未复现；归因负载竞速，非本轮产品改动（两者涉及路径本轮未被修改）。

## 三、集成期两个新发现与处置

### F25 TOOL_END 事件出口未脱敏（相邻路径，独立验证者发现）

- 触发条件：工具失败，`ToolResult.error` 含已登记敏感值；`TOOL_END` 事件在 SSE 上原样发布（同源 `tool_state` / 工具事实 / 工具历史同样带原文）；对照：进程日志、`TURN_END.annotation`、`final_content`、ASSISTANT 流式正文都已脱敏。
- 基线实际结果（无原文泄漏的断言输出）：`leaked_indices [0]`。
- 修复：`59c3766` —— `core/loop.py` 的 `TOOL_END` `error`/`content_preview` 在**事件出口**先过 `agent/trace/redact.py::redact_text`，同源落库（`tool_state.finish` / `_tool_facts` / `_record_tool_call`）用同一份已脱敏值。
- 修复后：`test_acc_f_25_tool_end_redaction.py` 绿；`test_acc_b_redact_stream.py`、`test_acc_f_07_stream_redaction.py` 不回归。

### 回归修复：r8「兼容路径多附件任一失败整轮拒绝」在组合/负载下变红

- 现象：`test_r8_compat_path_reject_verify.py::test_legacy_multi_attachment_any_failure_rejects_all` 返回 200 accepted、`rejected=[]`（单文件运行 5 passed）——Lead 在集成树上先复现到，再交 acc-e2。
- 根因（探针钉死）：兼容兜底把「进入时就能带」当**快照过滤器**；bad 附件的失败若在兼容路径枚举前落库，就被过滤掉，于是只剩 ok 附件被绑定、模型被调用。
- 修复（2400899）：进入时有至少一条能带草稿 ⇒ 集合 = 进入时本话题**全部未绑定草稿**（含进入即 failed/missing/不可读）→ 任一不合格整轮拒绝、0 次调用、无半绑定；一条能带的都没有 ⇒ 纯文字发送（保住产品规则 7）。
- 装置：两个冻结用例补确定性同步点（`_gated_fail_prepare_copy`），**未改断言**。
- 修复后：组合命令 3 次连续全绿（acc-e2 原始输出 `combo_3x_final.txt`）、全量后端绿。

## 四、跨层组合验证（F05/F06/F07/F11/F12）

- 用例：`backend/tests/test_acc_f_20_cross_layer_combination.py`（真实假 provider → adapter → loop → TurnManager → 事件总线；2 例）。
  - 正常路径：A 流式 + B 排队取消 + A 工具失败注记（独立 `annotation` 字段）+ C 正常完成 —— 断言 turn 归属、正文唯一、结束事实与落库。
  - 异常路径：A 真 SSE 缺 `finish_reason` → `incomplete/incomplete_stream/retry` + B 排队取消 + 敏感值跨分块无完整/半个泄漏。
- 结果：2 passed（原始日志 `docs/verification-shots-acc/acc-phase2-raw/backend-combination-1d1a7b6.txt`，该次为 1d1a7b6；最终 b947e14 全量复跑包含同两例）。

## 五、实机取证（msedge 无头 + Playwright）

- 命令：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-acc-phase2.ps1`（假厂商 SSE → uvicorn → vite → 无头浏览器）。
- 结果：**11/11 PASS，consoleErrors=0**；截图 10 张（执行+排队共存、结束折叠与耗时、未完成、失败+重试、排队取消、系统事实注记、附件恢复、Markdown 列表内代码块/表格、1440 宽、420 窄）。
- 关键实测：排队 chip「1 运行中 · 1 排队中」；过程区 `data-state="incomplete"` 且显示「未完成 + 原因 + 重试」；系统核对注记不在任何 `.markdown-body` 内、恰好 1 个，正文恰好 1 份；刷新后历史附件行存在，点「打开」→ `GET /api/attachments/{id}/content` = 200；`li .code-block=1`、`li .table-wrap table=1`，`pre code` 文本 `const a = 1;`；窄窗口横向溢出 0px。
- 产品侧裁定（低危，未改产品）：`incomplete_stream` 同时发 WARNING，`ConversationView.vue` 的通知链是 `v-if/v-else-if`，警告横幅先命中 → 那条安静「未完成」提示在真机路径不可达。Lead 裁定**保持现状**：同一事实不重复两条横幅，过程区已如实显示未完成 + 原因 + retry，历史恢复路径仍走安静提示。该行为记入 `docs/status.md` P23 与本节，不隐藏。

## 六、未实测项（明确留白）

1. 真实厂商端点：所有流式/结束语义验证都用本机假 provider（真 SSE/真 500/真断流），不能证明任何真实厂商的兼容性、限流、鉴权与配额行为。
2. Windows 原生窗口 / Tauri 安装包：原生选文件、路径拖入、原生中止提示、多显示器 DPI 均未验证；实机取证只在 msedge 无头内做。
3. 浏览器矩阵：只覆盖 msedge 无头；未在 Chrome/Firefox/Safari 或真实有头窗口复核 `openAttachment` 的 blob URL 生命周期。
4. 视觉：只有 1440×900 与 420×820 两档，无 200% DPI、键盘滚动、触屏。
5. 排队取消的「结束事实」在实机只取到可操作证据（B 移出队列、A 不受影响），语义由后端契约与前端 store 用例钉住。
6. eval：见 §七。

## 七、eval 对比

- 本轮的改动集中在对话过程区/流式/附件读取与一致性，未引入新的模型判断路径；`agent.eval.run` 的对照值（`_lead-logs/eval-baseline.json` 与 `eval-fixed.json`）在本轮开工前后已记录为**逐字段一致**。
- 本轮在最终集成树上复跑 `cd backend; uv run --frozen python -m agent.eval.run`，输出与 `_lead-logs/eval-baseline.json` **逐字段一致**（脚本 `ConvertTo-Json` 比对结果 IDENTICAL，原始输出 `_acc-evidence/lead-eval-final.json`）。即：本轮修复没有改变 eval 结果。

## 八、已知限制与遗留

1. 兼容路径（旧客户端不传 `attachment_ids`）下，话题里**未绑定**的陈旧失败草稿会阻断带附件发送，直到用户删除或重试成功；显式 `attachment_ids: []` 的纯文字发送不受影响。
2. `turn_orchestrator.finish()` 仍把 `turn_bindings.status` 写成 `completed`（incomplete 轮也一样）。该字段不是 turn 生命周期状态、当前无读取方（消费方只用 topic_id/fragment_id/write_state），本轮为控制范围未改，已登记为已知限制。
3. 关闭流程中仍在排队的 turn 不发 TURN_END、台账记 `interrupted`（既有语义，本轮未改）。
4. 旧历史记录不追溯补写结束事实（与既有「不补写旧记录」一致）。
5. active 取消路径的 `resend` 动作在台账为 `cancelled` 时会被 `/api/turns/{id}/resend` 拒绝（该端点只认 `interrupted`）——既有行为，本轮只把**排队取消**路径改为真实可用的 `retry`，未扩大范围改 active 路径。
