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
| P1-D-02 | D 独立契约测试 + 定向回归（整合结果） | 待回填 | 待回填 | D 阶段 2 |
| P1-整合-06 | 合并 D 测试后的最终定向集合 | 待回填 | 待回填 | 交付前最后一条 |
| P1-docs | 文档一致性 | `python scripts/check_docs.py` | 待回填 | 同步 status / architecture / 本台账 |

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

## 剩余问题 / 未验证边界

- 只完成四阶段中的第一阶段；完整测试集合、完整前端测试、完整 CI 均未运行（预算保留给四阶段收口）。
- 平台仅 Windows；桌面壳实机 / 真实模型链路未复验。
- 兼容期保留：旧 `build_injection` / `short_term_items` 文本路径、通知轮记忆 legacy 文本块、
  prompts.py 中少量兼容别名 —— 后续阶段清理。
- 若后续阶段发现旧测试依赖旧提示词字面量，应更新测试而不是恢复旧正文（本阶段已按此处理少量既有测试）。
