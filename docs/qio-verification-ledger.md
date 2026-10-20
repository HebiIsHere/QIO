# QIO 整体改造 · 阶段一验证台账

- 基线提交：`aef3c43c0cf5c03a4c551b18970f226a745cc6fc`
- 契约：`docs/qio-refactor-contract.md`
- 口径：只记录**实际执行过**的定向验证；不记录推测。完整 CI 不在本阶段执行。
- 平台：Windows 开发机（PowerShell）；后端 pytest 使用主仓库共享 venv：
  `D:\qio-dev\qio\backend\.venv\Scripts\python.exe`。各平台实际执行次数如实记录。

## 轮次说明

四阶段合计预算：完整验收最多两轮、完整 CI 最多两轮。本阶段只做定向验证，
**不计入完整验收 / 完整 CI 轮次**；若后续阶段复用本阶段结论，在对应阶段台账中引用本文件。

## 定向验证记录

| 轮次 | 范围 | 命令 / 方式 | 结果 | 证据位置 | 备注 |
| --- | --- | --- | --- | --- | --- |
| P1-预检-01 | 基线导入 / 适配器 | `pytest tests/test_import_smoke.py tests/test_adapters.py -q` | 通过（12 项） | 本台账 | 确认共享 venv 可用 |
| P1-预检-02 | 模块拆分（摘要 / 知识抽取） | `pytest tests/test_import_smoke.py tests/test_extraction.py tests/test_memory.py -q` | 通过（25 项） | 本台账 | 契约冻结提交内验证 |

## 契约冻结产物（Lead，提交见 git log）

- `backend/src/agent/prompts.py`：新增 PROMPTS / TOOL_DESCRIPTIONS / ARGUMENT_DESCRIPTIONS /
  CONTEXT_LABELS 与纯组装入口（文本逐字来自用户冻结稿，不自行改写）。
- `backend/src/agent/tools/base.py`：ToolStatus 枚举 + 结构化结果字段。
- `backend/src/agent/tools/contracts.py`：ToolEnvelope / json_schema_for / validate_model。
- `backend/src/agent/core/message_assembly.py`：ContextBlock 与统一组装入口。
- 模块拆分：`memory/limits.py`、`memory/contract.py`、`memory/rolling.py`、
  `knowledge/extraction.py`；`memory/summary.py` 保留兼容导出。
- `memory/__init__.py` 改为惰性导出，解除 memory ↔ knowledge 循环导入。

## 各智能体定向验证（完成后回填）

### A（提示词 / 消息组装 / 适配器 / 结构化工具结果）

- 待回填：命令、结果、覆盖点（三档适配器、回答标记、补救不重复、特殊字符与 JSON 不破坏组装、schema 与实际参数一致）。

### B（摘要覆盖 / 滚动摘要 / 上下文）

- 待回填：命令、结果、覆盖点（完整覆盖、滚动覆盖位置、来源与确定程度存取可读、移除二次截断）。

### C（知识审批 / 实体创建 / 纠正）

- 待回填：命令、结果、覆盖点（低影响分类不能绕过新增审批、待审不生效、明确纠正当场生效、同名实体不误合并、拒绝内容不再保存）。

### D（独立审阅 / 定向回归）

- 待回填：审阅范围、发现、复跑命令、结论。

## 整合验证（Lead）

- 待回填：整合分支定向验证、`python scripts/check_docs.py`、最终交接提交证据。

## 剩余问题 / 未验证边界

- 待回填。
