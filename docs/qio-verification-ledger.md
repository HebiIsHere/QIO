# QIO 整体改造 · 阶段一验证台账

- 基线提交：`aef3c43c0cf5c03a4c551b18970f226a745cc6fc`（origin/main）；公共契约冻结提交：`d265d033`。
- 整合分支：`feat/qio-refactor-p1`（Lead 工作区 `D:\qio-dev\qio-p1-lead`，未推送）。
- 契约：`docs/qio-refactor-contract.md`。
- 口径：只记录**实际执行过**的定向验证；不记录推测。完整测试集合 / 完整 CI 不在本阶段执行。
- 平台：Windows 开发机（PowerShell）；后端 pytest 使用共享 venv
  `D:\qio-dev\qio\backend\.venv\Scripts\python.exe`；模型调用全部 fake / mock，不联网、不使用真实 Key。

## 轮次说明

四阶段合计预算：完整验收最多两轮、完整 CI 最多两轮。本阶段只做定向验证，
**不计入完整验收 / 完整 CI 轮次**；后续阶段若复用本阶段结论，在对应阶段台账中引用本文件。

## 定向验证记录（按执行顺序）

| 编号 | 范围 | 命令 / 方式 | 结果 | 备注 |
| --- | --- | --- | --- | --- |
| P1-预检-01 | 基线导入 / 适配器 | `pytest tests/test_import_smoke.py tests/test_adapters.py -q` | 通过（12 项） | 确认共享 venv 可用 |
| P1-预检-02 | 模块拆分（摘要 / 知识抽取） | `pytest tests/test_import_smoke.py tests/test_extraction.py tests/test_memory.py -q` | 通过（25 项） | 契约冻结提交内验证 |
| P1-Lead-01 | 历史接续当轮上下文 + 话题工具描述单源 | `pytest tests/test_p1_lead_continue_context.py tests/test_continue_fragment.py -q` | 通过（14 项） | 摘要/头尾原文进工具结果与结构化 data；预算与 omit 如实 |
| P1-Lead-02 | 基线定向集合（整合前对照） | 21 个相关测试文件（adapters / tool_feedback / loop / memory / knowledge / entity / injection / context） | 通过（exit 0） | 冻结提交 + Lead 接线的整合前对照 |
| P1-A-01 | 提示词 / 适配器 / 工具反馈 | `pytest tests/test_import_smoke.py tests/test_adapters.py tests/test_anthropic_adapter.py tests/test_adapter_contract.py tests/test_text_adapter_health.py tests/test_tool_feedback.py tests/test_tool_schema_present.py -q` | 通过（50 项） | A 分支 |
| P1-A-02 | 循环 / 回答重复 / text 协议 | `pytest tests/test_loop.py tests/test_r5_answer_duplication_verify.py tests/test_rm_d_c01_text_protocol.py -q` | 通过（25 项） | A 分支 |
| P1-A-03 | A 新增契约测试 | `pytest tests/test_p1_a_{prompts,message_assembly,protocol,structured_results}.py -q` | 通过（30 项） | 三档规则 / 协议一次 / 补救 / 特殊字符 / schema 同源 / 结构化 data 进消息 |
| P1-A-04 | 受迁移影响既有测试 | `pytest tests/test_streaming_deltas.py tests/test_context_budget_accuracy.py tests/test_m12_maintenance.py tests/test_interactive_understanding.py tests/test_dev_guide_sections.py tests/test_tool_display.py -q` | 通过（131 项） | A 分支 |
| P1-A-05 | 组装 / 媒体 / 生命周期 | `pytest tests/test_app_integration.py tests/test_media_turn_e2e.py tests/test_media_transcription.py tests/test_chat_image_delivery.py tests/test_turn_lifecycle_protocol.py tests/test_trace_phases.py tests/test_acc_b_answer_annotation.py tests/test_rm_d_m09_budget.py tests/test_interactive_during_heavy_work.py -q` | 通过（72 项，6 skipped：ffmpeg/docker 环境门槛） | A 分支 |
| P1-B-01 | 摘要 / 滚动 / 上下文 / 迁移 44 | `pytest tests/test_import_smoke.py tests/test_memory.py tests/test_extraction.py tests/test_derivation_recovery.py tests/test_fragment_capacity.py tests/test_service_injection.py tests/test_injection_short_term.py tests/test_path_context.py tests/test_turn_short_term.py tests/test_p1_b_{summary_coverage,rolling_state,context_blocks,migration_and_storage}.py -q` | 通过（113 项） | B 分支 |
| P1-B-02 | 派生 / 失败 / 容量回归 | 15 个受影响文件（f05 / m05 / 派生恢复 / 故障注入 / 预算台账 / 锚点等） | 通过（89 项） | B 分支 |
| P1-C-01 | 知识 / 实体定向 | 简报 11 文件（含 test_import_smoke） | 通过（61 项，exit 0） | C 分支 |
| P1-C-02 | C 新增契约测试 | `pytest tests/test_p1_c_{evidence,knowledge_flow,entity_flow}.py -q` | 通过（29 项） | 审批 / 证据 / 纠错 / 实体决策 |
| P1-C-03 | 邻近风险回归 | test_derivation_recovery + test_rm_verify_m04_m05_memory → 49 项；另 11 文件（memory / entity provenance / f05 / app_integration 等）→ 76 项 | 通过 | C 分支 |
| P1-C-04 | 前端知识 / 实体面板（受影响文件） | `npx vitest run src/components/planet/__tests__/KnowledgePanel.test.ts src/components/planet/__tests__/EntityPanel.test.ts` | 通过（21 项） | 只跑受影响文件，未跑全量 |
| P1-D-01 | D 独立契约测试（未整合基线） | `pytest tests/test_p1_d_*.py -q`（7 文件） | 40 通过 / 27 失败（失败全部对应实现尚未合入，测试可收集、失败含契约条款） | 阶段 1；既有相关 11 文件基线对照 84 通过 |
| P1-整合-01 | 整合分支定向集合（首次） | 约 60 个受影响文件（A/B/C 测试 + 适配器 / 组装 / 记忆 / 知识 / 实体 / 接续 / 迁移 / 流式 / 预算 / 媒体） | 3 失败（见下） | 失败均为整合前提变化或收口缺口 |
| P1-整合-02 | 迁移纪律（迁移 44 登记 + 补偿 45） | `pytest tests/test_migrations_append_only_verify.py tests/test_fu_w3_b01_schema.py tests/test_storage.py tests/test_db_invariants.py tests/test_p1_b_migration_and_storage.py tests/test_import_smoke.py -q` | 1 失败（B 迁移测试仍假设未登记 schema.py） | 已适配测试前提 |
| P1-整合-03 | 迁移 / 通知 / 展示名修复复跑 | `pytest tests/test_turn_short_term.py tests/test_p1_b_migration_and_storage.py tests/test_tool_display.py -q` | 通过（10 项） | 结构化上下文标签 / 迁移登记 / 新工具中文展示名 |
| P1-整合-04 | 通知轮统一组装 + 实体参数说明 | `pytest tests/test_import_smoke.py tests/test_subagent_integration.py tests/test_app_integration.py tests/test_fragment_capacity.py tests/test_turn_short_term.py -q`；实体/知识工具相关 7 文件 | 通过（22 项；52 项） | 通知轮不再拼「【系统通知】」；create_entity 参数说明入 prompts |
| P1-整合-05 | 整合分支定向集合（复跑） | 同 P1-整合-01 文件集合 | 通过（exit 0） | 无失败 |
| P1-D-02 | D 独立契约测试（整合结果） | `pytest tests/test_p1_d_*.py -q`（7 文件） | 通过（67 项，全 PASS） | D 阶段 2；两处仅修测试装置前提，断言未变 |
| P1-D-03 | D 受影响定向回归 | 核心集 18 文件 → 129 项；扩展集 40 文件 → 395 项；turn 族 29 文件 → 217 项 + 6 skipped；A/B/C/Lead 契约测试 12 文件 → 78 项 | 通过 | D 阶段 2；均为受影响文件，非全量 |
| P1-整合-06 | F1 / F2 / F3 收口验证 | `pytest tests/test_import_smoke.py tests/test_p7_memory_authority.py tests/test_p7_restart_trace_resources.py tests/test_fu_w3_b01_schema.py tests/test_p1_b_migration_and_storage.py tests/test_onboarding_followups.py tests/test_attachment_context.py tests/test_interactive_execution.py -q` | 通过（66 项） | trace 注入项从结构化块派生；3 处固定文案入 prompts；rolling_* 进 REQUIRED_OBJECTS |
| P1-整合-07 | 最终整合定向集合 | 72 个受影响测试文件（D / A / B / C / Lead 契约测试 + 适配器 / 组装 / 记忆 / 知识 / 实体 / 接续 / 通知 / 迁移 / 流式 / 预算 / 媒体 / trace / 并发） | 通过（720 项，exit 0） | 交付前最终一条；非完整集合 |
| P1-前端 | 前端类型检查与受影响用例 | `npx vue-tsc --noEmit`；`npx vitest run src/components/planet/__tests__/KnowledgePanel.test.ts src/components/planet/__tests__/EntityPanel.test.ts` | 通过（类型 0 错误；21 项） | 只跑受影响文件，未跑全量 |
| P1-docs | 文档一致性 | `python scripts/check_docs.py` | 通过（78 个里程碑条目） | 同步 status P69 / architecture §16 / 契约 §11 / 本台账 |

## 契约冻结产物（Lead，提交 d265d033）

- `backend/src/agent/prompts.py`：PROMPTS / TOOL_DESCRIPTIONS / ARGUMENT_DESCRIPTIONS / CONTEXT_LABELS
  与纯组装入口（文本逐字来自冻结稿，不自行改写）。
- `backend/src/agent/tools/base.py`：ToolStatus 枚举 + 结构化结果字段。
- `backend/src/agent/tools/contracts.py`：ToolEnvelope / json_schema_for / validate_model。
- `backend/src/agent/core/message_assembly.py`：ContextBlock 与统一组装入口。
- 模块拆分：`memory/limits.py`、`memory/contract.py`、`memory/rolling.py`、
  `knowledge/extraction.py`；`memory/summary.py` 保留兼容导出。
- `memory/__init__.py` 改为惰性导出，解除 memory ↔ knowledge 循环导入。

## 各智能体落地与验证（摘要）

- A：提示词单文件化（互动模式 / text 档 / 工具描述与参数说明）、统一消息组装与回答协议、
  结构化工具结果与类型化参数；新增 4 个 `test_p1_a_*` 文件。
- B：limits 单源化、完整覆盖摘要、滚动摘要覆盖位置（迁移 44）、结构化上下文块（滚动摘要 +
  未覆盖原文、仅 active 知识、实体卡优先、预算按完整条目裁剪）；新增 4 个 `test_p1_b_*` 文件。
- C：知识新增审批（propose_knowledge）、后台提炼新字段与证据目录、拒绝墓碑、纠正稳定 ID / 版本 CAS、
  实体创建与 decision 语义、知识面板最小审批入口；新增 3 个 `test_p1_c_*` 文件。
- D：独立契约测试 7 文件 + 审阅清单 `D:\qio-dev\_P1-REVIEW-d.md` + 证据 `D:\qio-dev\_P1-EVIDENCE-d.md`；
  阶段 2 在整合结果上逐条判定。

## 整合收口记录（Lead）

- 迁移：B 的迁移 44 登记进 `storage/schema.py`，追加补偿迁移 45（= 43 的全部语句 + 44 新增列），
  `SCHEMA_VERSION = COMPENSATION_VERSION = 45`；历史 1..43 未改（append-only 回归通过）。
- 移除 B 分支中 C derivation 缺失时的两处 ImportError 临时回退与旧落库路径，统一委托冻结接口。
- 通知轮（系统驱动）改统一消息组装，去掉「【系统通知】」拼接；记忆上下文改为结构化块。
- 新工具补中文展示名与参数说明来源；create_entity 的 pydantic 参数说明迁入 prompts.py。
- 冲突处理：C 与 B 对 4 个既有 fake provider 的分派适配冲突按“单一提示词来源 + 不改弱断言”合并。

## 独立审阅发现与收口（D 阶段 2）

- F1（已修复）：新上下文装配下 turn trace 的 `injection.items` 为空 —— 现从结构化块派生真实的
  surface / item_id / tokens / score / preview；预算仍取 limits 块真实值（不再留空，也不编造）。
- F2（已修复）：3 处模型可见固定文案迁入 prompts.py（互动模式结果卡生成、首次引导追问、
  附件事实前缀），调用点改为引用常量。
- F3（已修复）：`REQUIRED_OBJECTS` 补 `fragments.rolling_*` 四列（补偿迁移 45 覆盖），版本 45 丢列可自愈。
- F4（已知边界，保留）：通知轮记忆 legacy_text 与旧 `build_injection` 文本路径仍在，见契约 §11.6。

## 剩余问题 / 未验证边界

- 只完成四阶段中的第一阶段；完整测试集合、完整前端测试、完整 CI 均未运行（预算保留给四阶段收口）。
- 平台仅 Windows；桌面壳实机 / 真实模型链路未复验。
- 兼容期保留：旧 `build_injection` / `short_term_items` 文本路径、通知轮记忆 legacy 文本块、
  prompts.py 中少量兼容别名 —— 后续阶段清理。
- 若后续阶段发现旧测试依赖旧提示词字面量，应更新测试而不是恢复旧正文（本阶段已按此处理少量既有测试）。
- 本阶段未推送、未创建 PR、未合并 main、未发布。


---

# 阶段二验证台账：独立后台维护 / 记忆待确认入口 / 安全恢复（2026-10-21）

- 起点提交：`5021a4d5`（阶段一整合）；契约冻结 `bcce3a2a`；整合分支 `feat/qio-refactor-p2`
  （Lead 工作区 `D:\qio-dev\qio-ph2-lead`，本地，未推送）。
- 契约：`docs/qio-refactor-contract.md` §12。
- 口径同阶段一：只记录实际执行的定向验证；不跑完整集合 / 完整 CI；模型一律 fake/mock；
  平台 Windows 开发机，后端使用共享 venv `D:\qio-dev\qio\backend\.venv\Scripts\python.exe`。

## 定向验证记录（按执行顺序）

| 编号 | 范围 | 命令 / 方式 | 结果 | 备注 |
| --- | --- | --- | --- | --- |
| P2-预检-01 | 冻结提示词 / 类型 / 本地校验同源 | 导入 + `validate_maintenance_output` / `validate_resolution_output` / schema 生成 | 通过 | PROMPTS 23 键；维护 / 回答输出形状与本地校验同一类型 |
| P2-A-01 | A：变化计数 / 等待轮次 / 调度 | `pytest tests/test_import_smoke.py tests/test_p2_a_*.py -q`（A 分支） | 通过（45 项） | 跨重启 / 新变化不重置等待 / 未触发不调模型 / 20 或 30 轮 / 快照推进 / 退避 / 单轮互斥 / turn 去重 |
| P2-B-01 | B：维护动作 / 独立回答 / 恢复 | `pytest tests/test_p2_b_*.py -q`（B 分支） | 通过（43 项） | merge 多来源关系 / 跨类别拒绝 / normalize / ask 去重 / 回答语义 / CAS / 恢复保护 |
| P2-B-02 | B 简报定向集合 | import smoke + p2_b + knowledge + r02r03 + p1_c_knowledge_flow（B 分支） | 通过（74 项） | 既有维护回归 test_m12_maintenance 6 passed（B 分支未改共享文件） |
| P2-C-01 | C：API 读取模型 / 两类事项 / 动作 | `pytest tests/test_import_smoke.py tests/test_p2_c_memory_api.py tests/test_knowledge_mgmt_api.py -q`（C 分支） | 通过（23 项） | fake runner；runner 不可用 503「未执行」；迁移未建表时诚实降级 |
| P2-C-02 | C：前端受影响用例 + 类型 | `npx vitest run` 受影响 3 文件；`npx vue-tsc --noEmit`（C 分支） | 通过（34 项；类型 0 错误） | 两类事项区分 / 动作 / 状态 / 来源筛选用例 |
| P2-C-03 | C：真实界面检查（首次，fake runner） | 真实 uvicorn + vite + Playwright 无头浏览器，临时数据目录 | 通过（16/16 断言，8 张截图） | 待确认列表 / 回答后状态 / 维护记录 / 恢复预览与结果 / 后台整理筛选 / 窄窗口 |
| P2-D-01 | D：独立契约测试（未整合基线） | `pytest tests/test_p2_d_*.py -q`（9 文件，基线 bcce3a2a） | 98 收集；35 passed / 33 failed / 30 errors（预期：实现未合入） | 冻结面全绿；迁移 / 计数 / 调度 / 维护 / 回答 / 恢复 / API 边界为待翻正项 |
| P2-D-02 | D：独立契约测试（整合结果） | `pytest tests/test_p2_d_*.py -q`（整合分支） | 通过（101 项） | 5 处测试前提适配，未放宽断言 |
| P2-D-03 | D：定向回归 5 组 + 前端 + 真实后端启动 | 维护/关闭/工具记录 34；知识生命周期/纠正/API 38；记忆/注入/审批 + App 生命周期 47；导入/轮次/阶段一契约 40；迁移/schema 30 + 1 failed | 通过（除 DEFECT-1 外） | DEFECT-1 为基线遗留，见下 |
| P2-D-04 | D：前端受影响用例 + 类型 | `npx vue-tsc --noEmit`；受影响 vitest 4 文件 | 通过（37 项；类型 0 错误） | — |
| P2-D-05 | D：真实后端启动抽查 | 临时数据目录 + uvicorn，GET pending / status / batches / knowledge | 200，真实 scheduler 状态可见，进程清理 | 未调用模型 |
| P2-整合-01 | Lead：整合分支定向集合（首次） | A/B/C 定向 + app integration + 迁移纪律 + 知识生命周期 | 4 个 C 测试因整合前提失败（真实 schema / 真实 runner） | 已按整合前提适配测试（不放宽断言） |
| P2-整合-02 | Lead：迁移登记 / 补偿 48 / 维护表 | 迁移纪律 + closeout + storage + p2_a/p2_b 迁移测试 | 通过；发现 DEFECT-1（见下）后 26 项复跑通过 | SCHEMA_VERSION=COMPENSATION_VERSION=48 |
| P2-整合-03 | Lead：D 测试合入整合分支 | 9 文件 D 测试 + import smoke | 通过（104 项） | 阶段二独立回归资产进入整合分支 |
| P2-整合-04 | Lead：最终定向集合（阶段一 + 阶段二选受影响文件） | 适配器 / 组装 / 循环 / 记忆 / 知识 / 实体 / 维护 / 迁移 / 接续 / 媒体 / trace / 并发 / A+B+C+D 契约测试 | 通过（exit 0，非完整集合） | 交付前最终一条 |
| P2-修复-01 | DEFECT-1：补偿 48 缺 fragments 基础表 | `pytest tests/test_main_closeout_migration_objects.py tests/test_migrations_append_only_verify.py tests/test_fu_w3_b01_schema.py -q` | 通过（26 项） | 补偿 48 增加 fragments 现代形状 CREATE TABLE（基础表补建），D 迁移测试口径同步更新并记录契约 §12.7 |
| P2-前端-01 | Lead：整合后前端类型与受影响用例 | `npx vue-tsc --noEmit`；vitest 4 文件（含 PlanetView） | 通过（101 项；类型 0 错误） | 只跑受影响文件 / 受影响页面用例 |
| P2-UI-01 | C：整合分支真实界面检查 + 真实 runner 链路（fake 适配器） | 真实迁移 46-48 建表 + 真实 runner/recovery/scheduler + 真实 HTTP；`verify_real_runner_c.py` | 17/17 UI 断言；8/8 真实 runner 链路 | 真实模型未调用；18 张截图在 `D:\qio-dev\_P2R-SHOTS-c\` |
| P2-docs | 文档一致性 | `python scripts/check_docs.py` | 通过（79 个里程碑条目） | §12.10 / architecture §17 / status P70 / 本台账 |

## 独立审阅发现与收口

- **DEFECT-1（基线遗留，已修复）**：`REQUIRED_OBJECTS` 中的 `fragments.rolling_*` 要求补偿迁移同时有
  对应基础表的 CREATE TABLE；补偿 48 原先只有 ALTER，整表丢失时无法自愈。已在补偿 48 增加
  fragments 现代形状的 `CREATE TABLE IF NOT EXISTS`，并同步 D 的迁移测试口径与契约 §12.7；
  `test_main_closeout_migration_objects` 等复跑通过。
- 其余 D 判定 D-01..D-19 PASS；D-20（迁移口径）随 DEFECT-1 修复翻正；D-21（真实 runner 界面链路）
  由 C 在整合分支复跑 17/17 + 8/8 闭环（模型仍为 fake 适配器）；D-22（台账）由本文件回填闭环。

## 剩余问题 / 未验证边界

- 第三、四阶段未开始；后续从阶段二整合提交继续。
- 完整后端测试集合、完整前端测试集合、完整 CI 均未运行（四阶段共同预算留到最终收口）。
- 平台仅 Windows；桌面壳 / Tauri 实机与真实模型链路未验证（维护与回答均为 fake/mock 适配器）。
- 维护批次 / 事项 / 操作、`knowledge_maintenance_turn_log` 暂无归档清理策略（契约未要求），长期增长待后续评估。
- 通知轮记忆 legacy 文本块、旧 `build_injection` 文本路径仍在；第三阶段继续清理。
- 本阶段未推送、未创建 PR、未合并 main、未发布。


---

# 阶段三验证台账：互动模式接入完整主循环（2026-10-21）

- 起点：第二阶段整合提交 `6e71abbb`；本阶段契约 §13 冻结提交（见分支 `feat/qio-refactor-p3`）。
- 口径同前：只记录实际执行的定向验证；不跑完整集合 / 完整 CI；模型一律 fake/mock。
- 待回填：A / B / C / D / Lead 整合 / 前端 / 真实界面 / docs 各角色行（按实际命令与结果记录）。

| 编号 | 范围 | 命令 / 方式 | 结果 | 备注 |
| --- | --- | --- | --- | --- |
| P3-预检-01 | 冻结提示词 / 工具参数模型 / 可信运行上下文同源 | 导入 + schema 生成探针 | 通过 | PROMPTS 24 键；TOOL_DESCRIPTIONS 36（含 12 个 board_*）；12 个参数模型 schema 由 json_schema_for 生成；意图结构可校验 |
| P3-预检-02 | allowed_actions 非空校验（DEFECT-P3-1） | D 复现后 Lead 修复 + 定向探针 | 通过 | 修复 enum 二次解析；补充 create_card / update_card 等非空 allowed_actions 可校验回归 |
| P3-A-01 | A：提交接入主循环 / 等待续作 / 聊天绑定 / 退休旧理解 | pytest test_p3_a_* + interactive_submission + interactive_integration + irm_b + import_smoke | 通过（107 项） | 无 turn_acceptor 时如实 failed，不回退旧理解；旧测试 19 条前提变化逐条记录 |
| P3-A-02 | A：旧理解测试红项收口 | pytest test_interactive_understanding.py（17）+ 上述组合 | 通过（17 / 109 项） | 只改测试前提，不删不 skip；旧记录只读兼容 |
| P3-B-01 | B：迁移 50 / 授权账本 / 12 工具 / 版本 CAS | pytest test_p3_b_service.py test_p3_b_tools.py | 通过（26 项） | 普通模式拒绝 / 伪造板面 / scope / 幂等 / 依赖环 / finish 账本 / 迟到完成 / 部分失败 |
| P3-B-02 | B：简报定向 + 相关互动回归 | test_p3_b_* + board + intents + import_smoke；追加互动 11 文件 | 通过（127 / 275 项） | 合并 DEFECT-P3-1/2 修复后复跑 |
| P3-C-01 | C：意图 API / 门槛 / 等待 / 账本 / 结果 / 恢复 | pytest test_import_smoke + test_p3_c_intents_api + test_interactive_intents | 通过（54 项） | 三条降级前提显式 monkeypatch；新增真实 service 200 形状回归 |
| P3-C-02 | C：前端意图工作台 / 结果卡 / 长正文 | npx vitest run IntentWorkbench + intentWorkbench；npx vue-tsc --noEmit | 通过（21 项；类型 0 错误） | Markdown 表格/代码块完整、折叠展开、空正文不显示假卡 |
| P3-C-03 | C：真实界面检查（真实迁移 51 + 真实 intent_service） | uvicorn + vite + Playwright(msedge)，临时数据目录，模型未调用 | 通过（20/20 断言，截图 D:\qio-dev\_P3R-SHOTS-c\） | 批量 1 禁用 / 2 可批、结果 / 账本 / 恢复 / 窄窗口 |
| P3-D-01 | D：独立契约测试（阶段 1） | pytest test_p3_d_*.py（8 文件 78 条） | 收集 78；合入冻结修复后冻结面 15/15 | 实现未合入项预期失败，均带契约条款，无 skip |
| P3-D-02 | D：整合后独立测试与定向回归 | 9 文件 79 条 + C API 15 条；interactive* + p3_* + turn/loop/app/迁移纪律 | 通过（94/94 + 定向回归） | 阶段 2 复跑；发现 DEFECT-P3-1/2/3 |
| P3-D-03 | D：独立真实界面检查（自有装置） | 真实后端 + vite 5199 + Playwright(msedge)，种子用真实 A/B 服务 | 通过（32/32 断言） | 长文 6384 字完整、表格/代码块、窄窗口、批量 2-vs-1、恢复预览、等待区 |
| P3-整合-01 | Lead：A+B 合入后定向集合 | 导入 / 迁移纪律 / closeout / A+B+D 测试 / 互动提交与存储回归 | 通过（303 项） | 迁移 49/50/51 登记、补偿 51、REQUIRED_OBJECTS |
| P3-整合-02 | Lead：最终整合定向集合（A+B+C+D + 互动/循环/存储/知识回归） | pytest 选受影响文件，-p no:warnings | 通过（exit 0，非完整集合） | 含 6 个环境 skip；C 分支 5108e978、A 分支 aac2fe4f 合入后复跑 |
| P3-前端-01 | Lead：整合后前端类型 + 受影响用例 | npx vue-tsc --noEmit；vitest 5 文件（session / turnFacts / IntentWorkbench / intentWorkbench / approval） | 通过（92 项；类型 0 错误） | 修复普通聊天 sendTurn 参数形状；旧 approval 门槛统一为 2 |
| P3-修复-01 | DEFECT-P3-1/2（冻结模型） | D/B 复现 → Lead 修复 → 探针 + 冻结面 15/15 | 通过 | allowed_actions 枚举二次解析、list/set 求交 |
| P3-修复-02 | DEFECT-P3-3（等待响应形状） | D 独立界面 FAIL → C 修复 75f38b47 → D 复跑 32/32 | 通过 | waiting 固定为 {available, board_id, count, waiting:[...]} |
| P3-修复-03 | 阶段二遗留 stale 测试 + 旧批量门槛 | test_interactive_during_heavy_work（2 条）+ approval.test.ts / intents.py 门槛 4→2 | 通过 | 旧否定词扫描断言改为退休守卫；前后端批量门槛统一 2 |
| P3-docs | 文档一致性 | python scripts/check_docs.py | 通过（79 个里程碑条目） | 契约 §13.10 / architecture §18 / status P71 / 本台账 |

