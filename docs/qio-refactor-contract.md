# QIO 整体改造 · 第一阶段冻结契约（提示词 / 消息组装 / 记忆记录基础）

- 基线提交：`aef3c43c0cf5c03a4c551b18970f226a745cc6fc`（origin/main）
- 本阶段只做第一阶段；后续阶段从本阶段整合后的提交继续。
- 本文是并行实现的**唯一公共契约**。实现与本文冲突时，以本文为准；确需修改契约，
  由整合负责人（Lead）在本文件更新后再通知各实现者。
- 交付：整合提交 + 定向验证结果 + 阶段交接。**不推送、不合并 main、不发布、不启动完整 CI。**

## 0. 角色、分支与 worktree

| 角色 | worktree | 分支 | 主要职责 |
| --- | --- | --- | --- |
| 整合负责人（Lead） | `D:\qio-dev\qio-p1-lead` | `feat/qio-refactor-p1` | 冻结契约、prompts.py 固定正文、共享入口、迁移号、整合提交、调度验证、文档 |
| 智能体 A | `D:\qio-dev\qio-p1-a` | `wt/p1-a-prompts` | 提示词集中管理、统一消息组装、适配器与回答协议、结构化工具结果与类型模型 |
| 智能体 B | `D:\qio-dev\qio-p1-b` | `wt/p1-b-memory` | 摘要覆盖、滚动摘要、来源与上下文使用、预算与 limits |
| 智能体 C | `D:\qio-dev\qio-p1-c` | `wt/p1-c-knowledge` | 知识新增审批、实体创建、明确纠正工具、证据校验 |
| 智能体 D | `D:\qio-dev\qio-p1-d` | `wt/p1-d-verify` | 独立审阅、契约检查、定向回归、独立测试 |

规则：

- 每名实现者只在自己的 worktree / 分支提交；**不 push**、不合并 main、不跑完整 CI。
- 共享文件（本表中同一路径出现多个角色时）由 Lead 集中整合；实现者不得同时改写。
- 沟通与提交信息用中文；汇报用“做了什么 / 证据（命令与结果）/ 未做与剩余问题 / 需要 Lead 处理”四段式。
- 模型调用测试一律用 fake/mock provider；不依赖真实 API Key 或外网。
- 迁移只追加，禁止修改历史迁移；迁移号见 §8。
- 不写会过期的硬编码数字；文档里的数量要跑命令确认。

## 1. 验证预算与方式

四阶段合计：完整验收最多两轮、完整 CI 最多两轮；本阶段 **不启动完整 CI、不跑完整测试集合**。

开发期间只允许：

- 受影响的定向 pytest（单文件 / 单用例集），命令模板（所有 worktree 共用主仓库 venv）：

```powershell
cd <自己的 worktree>\backend
& 'D:\qio-dev\qio\backend\.venv\Scripts\python.exe' -m pytest tests/test_xxx.py -q
```

- 必要静态检查：`python -m compileall`（改动文件）、`python scripts/check_docs.py`（改了文档时）。
- 前端只跑受影响的 vitest 文件（`npx vitest run <文件>`），不跑全量；不改无关视觉。
- 不为每段提示词写全文快照测试；不为简单可逆文案调整新增独立测试。
- 保留必要验证：授权、来源、版本、幂等、重启、取消、任务归属、完整内容保存。
- runtime / 预算 / 工具策略变化所需的离线评估集中执行，对比已有基线，不覆盖基线。

## 2. prompts.py 单文件契约（A 主责，Lead 冻结固定正文）

`backend/src/agent/prompts.py` 是**全部固定模型可见文案的唯一来源**，包括但不限于：
系统规则、回答协议与补救规则；记忆 / 话题 / 材料 / 多媒体说明；工具描述与参数说明；
开发指南；摘要、提炼、维护与独立回答规则；互动模式规则；子任务、通知、修复说明；固定上下文标签。

### 2.1 已冻结的公共符号（Lead 已写入，**不得改写文本内容**）

- `ANSWER_MARKER = "[[QIO:ANSWER]]"`（解析器保留原值）。
- `PROMPTS: dict[str, str]`：固定文案，key 至少 21 个（QIO_CORE / CONTENT_ROLE_PROTOCOL /
  ANSWER_FALLBACK_HINT / TEXT_MODE_PROTOCOL / TEXT_OBSERVATION_RULES / AVAILABLE_TOOLS_HEADER /
  MEMORY_POLICY / MEMORY_CONTEXT_RULES / TOPIC_POLICY / MATERIAL_POLICY / TOOL_RESULT_POLICY /
  NARRATIVE_FIELD_DESCRIPTION / TOOL_VALIDATION_FEEDBACK_SYSTEM / STRUCTURED_JSON_POLICY /
  OUTPUT_SCHEMA_PREFIX / CLOSED_SUMMARY_SYSTEM / ROLLING_SUMMARY_SYSTEM / MERGE_SUMMARY_SYSTEM /
  KNOWLEDGE_EXTRACTION_SYSTEM / ENTITY_EXTRACTION_SYSTEM / STRUCTURED_REPAIR_SYSTEM）。
- `TOOL_DESCRIPTIONS: dict[str, str]`：工具描述唯一来源（含 propose_knowledge / create_entity）。
- `ARGUMENT_DESCRIPTIONS: dict[str, str]`：通用参数说明唯一来源。
- `CONTEXT_LABELS: dict[str, str]`：固定上下文标签唯一来源（17 个 key，见 §3）。
- 纯组装函数：`system_rules()` / `answer_protocol()` / `answer_fallback_hint()` /
  `text_mode_protocol()` / `text_observation_rules()` / `text_tool_catalog()` /
  `context_heading()` / `render_context_blocks()` / `structured_task_system()`。

### 2.2 硬性规则

- 其他模块**只能引用常量或调用上述统一组装入口**；不得复制、另写或覆盖一套提示词。
- 动态消息、材料、来源、工具结果、运行时工具定义作为**数据**传入；动态数据统一用
  `json.dumps` 序列化。禁止对包含 JSON / 用户内容 / 代码的整段文本调用 `format`、
  百分号替换或递归 `replace`。
- 先按完整对象控制大小，再序列化；不得截断序列化后的 JSON（预算不足时在数据层裁剪条目）。
- 新增文案只在所属功能实际可用后启用；旧常量需要兼容时指向同一来源，不保留另一份旧正文。
- 工具参数的自然语言说明迁移到本文件（可省略不必要的说明，只保留真实类型、枚举与约束）。
- 类型模型可单独管理，但向模型展示的自然语言说明必须引用 prompts.py。

### 2.3 A 需要完成的迁移

1. 把仓库里散落的、模型可见的固定文案迁入 prompts.py（`grep -rn "【"、tool description、
   `interactive/prompts.py::SYSTEM_PROMPT` / OUTPUT_SCHEMA_TEXT、`tools/spec.py::TOOL_PROPOSAL_PROMPT`、
   维护 / 开发指南 / 子任务文案等），并删除原处的重复正文字面量。
2. 所有工具类改为引用 `TOOL_DESCRIPTIONS[name]` / `ARGUMENT_DESCRIPTIONS[key]`；旧 `TOOL_*_DESC`
   常量改为指向同一来源或删除（A 只能删除自己拥有文件的引用点；C / Lead 拥有文件里的引用由
   他们自己迁移，A 不越界）。
3. 更新模块 docstring 中的“提示词在哪”的说明；`docs/qio-refactor-contract.md` 的目录由 Lead 维护。

## 3. 统一消息组装契约（A 主责）

新模块：`backend/src/agent/core/message_assembly.py`（Lead 已冻结接口与 `ContextBlock`）。

主对话请求形状（顺序固定）：

1. **系统规则**：`prompts.system_rules(mode_rules)`（QIO_CORE + 记忆 / 上下文 / 话题 / 材料 / 工具结果政策）；
2. **当前模式规则**：由程序决定（适配器能力位 / 互动模式枚举），不来自正文；
3. **实际可用工具定义**：native / anthropic 走 `tools` 字段；text 档用
   `prompts.text_tool_catalog()` 拼进 system，三档规则一致且只出现一次；
4. **结构化记忆与材料**：`ContextBlock` 列表，key 必须来自 `CONTEXT_LABELS`，data 整体
   json.dumps（`prompts.render_context_blocks`）；
5. **实际对话消息**：本轮已累积的 assistant / tool 消息（按原顺序）；
6. **当前用户原文 + 真实多媒体内容块**：`ChatMessage(role="user", content=用户原文, images/audios/videos=真实字节)`。

硬性边界：

- **不得**再使用“【用户消息】”等中文标题把注入上下文与当前用户消息拼成一条（删除
  `turn_orchestrator.py` 的 `f"{payload.text}\n\n【用户消息】\n{message}"` 路径）。
- mode / 任务身份 / 权限来自程序；正文里自写的标题不改变身份或权限。
- 回答协议（CONTENT_ROLE_PROTOCOL）**只注入一次**：native / anthropic 作为每次调用的最后一条
  system 消息（现有 `_call_hint` 机制可复用）；text 档拼进自己的 system prompt（`protocol_in_prompt`）。
- 补救调用（每轮最多一次）**保留通用规则**，再追加 `prompts.answer_fallback_hint()`。
- 固定规则、工具、上下文及媒体预算**不漏算、不重复计算**（text 档工具在 prompt 里时只算一次；
  media bytes 不计入文本 token 预算但需如实记录）。
- `[[QIO:ANSWER]]` 原值保留；文本工具协议保留 `tool_calls` / `name` / `arguments` 键名。
- 结构化上下文块建议 key：`memory` / `current_topic` / `short_term` / `rolling_summary` /
  `history_focus` / `path_premises` / `related_memory` / `knowledge` / `entity` / `attachments` /
  `media` / `interactive` / `submission_snapshot` / `latest_materials` / `subtask` /
  `tool_feedback` / `limits`。

B 负责提供“结构化上下文的**内容**”（`services/context.py` 产出 `list[ContextBlock]` 或
等价 data），A 负责把它装进消息；两边以 `ContextBlock(key, data)` 为唯一交接形状。

## 4. 结构化工具结果契约（Lead 冻结类型，A 实现序列化）

`backend/src/agent/tools/base.py` 已加字段：`protocol_version` / `status` / `code` / `data` /
`model_payload`；`backend/src/agent/tools/contracts.py` 已提供 `ToolStatus`、
`ToolEnvelope`、`build_envelope`、`envelope_json`、`json_schema_for`、`validate_model`。

模型可见形状（成功）：

```json
{"protocol_version": 1, "ok": true, "status": "completed", "code": "稳定机器代码", "data": {}, "error": null}
```

- status 只允许：queued / running / completed / partially_completed / awaiting_approval /
  awaiting_input / conflict / failed / cancelled / unknown。
- **识别结构化结果只认程序字段**（`protocol_version == 1` / `ToolEnvelope`），不从任意文件正文
  或旧工具正文里猜同名 JSON 字段。
- 完整证据**必须真正进入模型消息**（`data` 随 envelope 序列化），不能只放进不传给模型的 facts。
- 旧工具成功正文兼容保留（无结构化字段时仍按旧文本路径，不强行改造全部工具）。
- 错误类别 / 可恢复性 / 状态由字段表示，不匹配中文句子。
- 新工具与关键改造路径的参数模型用 pydantic（`extra="forbid"`、enum、discriminated union），
  `parameters = json_schema_for(Model)`，本地校验用 `validate_model(Model, arguments)`；
  不得再声称 `tools/schema.py` 的轻量检查已经严格校验 enum / oneOf / additionalProperties。
- 新增字段必须贯穿生成 → 解析 → 修正 → 存储 → 重建 → API → 前端，不被旧 `repair_output` 丢弃。

## 5. 记忆契约（B 主责）

- 封块摘要**覆盖完整片段**：按“完整消息 + 可追溯分段”切块，逐段摘要后用
  `MERGE_SUMMARY_SYSTEM` 合成；不得只取尾部（删除 `[-12_000:]` 一类尾部截断）。
- 长输入分段必须保留消息身份（消息 id / 序号），合并时保留开头、中间、结尾信息。
- 滚动摘要只处理**未覆盖的新消息**；持久保存覆盖位置与输入版本（迁移 44），后续主对话使用
  **滚动摘要 + 未覆盖原文**（不得把已覆盖原文重复注入，也不得跳过未覆盖原文）。
- 长度随预算调整：生成提示词、schema、修正层、存取路径共用 `memory/limits.py` 的同一组常量；
  **移除存取路径按旧 2000 字上限再次截断**（`_summary_for_fragment` 里的 `text[:MAX_SUMMARY]`）。
- 原始对话保留，摘要失败可回查；摘要失败不得回退封块或卡住对话。
- 摘要输出保留 `title / summary / entities[] / keywords[]`。

## 6. 知识 / 实体契约（C 主责）

### 6.1 主对话新增知识

新工具 `propose_knowledge`：

- 必填：`content`、`category`（五类枚举）、`scope`（`{type: user|topic|entity, node_ids: string[]}`）、
  `epistemic_status`（user_explicit / shared_understanding / qio_observed / qio_inference）、
  `evidence_ids`（输入中实际存在的来源）。
- 主对话新增知识**全部须用户审批**，不能通过选择 general_fact 绕过；待审批不作为有效知识使用
  （不得进入注入面）。
- 复用现有知识面板形成最小可用审批入口：pending_review 条目可见、可通过现有 verify / reject API
  处理；不留下无法处理的提议。
- 来源必须实际存在：主对话证据目录 = 本轮实际提供的消息 / 附件 / 已注入知识 / 实体 / 工具调用等
  程序拥有的 id；后台证据目录 = 输入给模型的 fragment / summary / 实体 / 知识 id。
  `qio_observed` 必须有实际工具结果或已读取材料等依据，不能只依据 assistant 自述。
- 审批状态与事实确定程度分开：`epistemic_status` 不是审批状态。

### 6.2 后台提炼

保持原分类规则：`user_profile` / `agent_self` / `goal` 需用户确认；`general_fact` /
`tool_experience` 可自动生效。新增 `epistemic_status` 与 `evidence_ids`（必须真实存在），
输出结构：

```json
{"candidates": [{"content": "...", "category": "...", "attach": null, "entity": null,
                 "epistemic_status": "...", "evidence_ids": []}]}
```

`attach` 只允许 user / topic / entity / null；`user_profile` / `agent_self` / `goal` 的
attach 缺省时按现有规则处理。明确拒绝的内容不能被另一入口无理由再次保存
（复用/扩展 `knowledge.provenance.ignored_at` 的拒绝事实，主对话与后台都检查）。

### 6.3 纠正工具

- `correct_knowledge`：保留原有参数 `content` / `new_content` / `delete` / `candidate_index`；
  补充稳定 `knowledge_id` 与 `expected_version` 定位；旧候选序号仅在仍有效时兼容。
- `correct_entity`：保留原有参数；补充稳定 `entity_id` 与 `expected_revision`；身份不明确时
  不得选择同名对象猜测。
- 明确纠正当场生效（同一轮工具调用返回真实结果），不需要用户再次确认同一明确纠正。

### 6.4 实体创建

新工具 `create_entity`：必填 `name` / `identity` / `creation_reason` / `evidence_ids`；
可选 `kind` / `aliases` / `summary` / `attributes` / `relations`。

- 允许 QIO 自主创建身份明确、具有持续记录价值的对象，不要求每次明确说“记住”。
- 先查重；**同名不代表同一对象**；不覆盖已有受保护信息（用户值 / 来源未知字段按现状保护）。
- 后台实体输出（替换现有实体提炼 schema）：

```json
{"entities": [{"entity_id": null, "name": "...", "identity": "...", "aliases": [],
  "kind": null, "summary": null, "decision": "observe", "reason": "...",
  "evidence_ids": [], "attributes": [{"key": "...", "value": "...", "evidence_ids": []}],
  "relations": [{"target": "...", "target_entity_id": null, "type": "...", "evidence_ids": []}]}]}
```

- decision：create / update / observe / identity_uncertain；create 对应空 entity_id；
  update 要求真实已有 ID；observe / identity_uncertain 不自动创建。
- 关系只有名称、身份不明确时保留候选，不直接写入同名对象。
- 预选依据：持续关系、实际使用需要、信息价值；次数和时间跨度只作辅助，不设统一必须出现次数。
- 新增字段必须贯穿生成 → 解析 → 修正 → 存储 → 重建 → API → 前端（不被 `repair_output` 丢弃）。

## 7. 类型模型与 schema 契约（A 主责，C 使用）

- 新工具及关键改造路径使用真正执行约束的 pydantic 类型模型；展示 schema 与本地校验同源。
- `json_schema_for` 负责 $defs 内联；工具 `parameters` 必须是可下发的 JSON Schema。
- 本地校验失败返回结构化失败结果（`ToolEnvelope` + 字段错误），不靠中文错误句匹配。
- 旧轻量 `tools/schema.py` 仅保留给旧工具输出校验；新路径不得以它作为“严格校验”的依据。

## 8. 迁移号分配（只追加）

| 编号 | 归属 | 用途 |
| --- | --- | --- |
| 44 | B | 滚动摘要覆盖位置 / 输入版本持久化 |
| 45 | C | 知识提议 / 实体创建 / 证据与修订定位所需字段（如需要；优先复用现有列与 JSON） |
| 46 | A | 结构化工具结果持久化 / 重建所需字段（仅当必须） |
| 47 | Lead | 负责人接线所需（仅当必须） |

- 只允许追加，不得改历史迁移语义。
- **实现分支不改 `storage/schema.py`**：各自在自己的 worktree 新建
  `backend/src/agent/storage/migration_p1_<a|b|c>.py`，导出 `MIGRATION = (编号, ["SQL", ...])`
  （或 `MIGRATIONS_EXTRA = [(编号, [...])] )；分支内测试自行应用该 SQL。
  Lead 在整合时把它们登记进 `schema.py` 的 `MIGRATIONS` 末尾并统一收敛 `SCHEMA_VERSION`。
- 编号冲突时由 Lead 重新分配；合并顺序由 Lead 在整合分支处理。
- 迁移必须有对应定向测试（升级路径 / 幂等 / 对象校验至少一项）。

## 9. 负责人（Lead）接线清单

1. `continue_from_fragment`：当轮调用后即可获得相关上下文（把片段摘要 + 头尾原文作为该次工具结果
   的真实内容交给模型，受预算约束）；不连带改写其他排队消息；下一轮锚点语义保持不变。
2. 话题切换：用户明确要求时执行；仅 QIO 自主提议时先确认（TOPIC_POLICY 与工具描述一致）。
3. 实体完整卡片优先提供能力保留，受总预算约束（不破坏现有 reserved 顺序：Focus > 实体卡 > 短期记忆）。
4. 附件与多媒体读取范围保持真实；不凭文件名推断正文；能力位仍以适配器声明为准。
5. 数据迁移只追加，不改历史迁移。
6. 维护 `docs/qio-refactor-contract.md`、`docs/qio-verification-ledger.md`，同步 `docs/status.md`。

## 10. 交付与汇报

每个实现者在本分支产出：

- 实现提交（可多个，中文 commit message，前缀 `feat/fix/test/docs(p1-<角色>)`）；
- 定向验证记录（命令 + 结果摘要）写在自己的汇报中，同时给 Lead 可直接复制的证据行；
- 变更清单（文件 → 改了什么）、契约偏差、剩余风险、需要 Lead 处理的事项。

整合顺序：A/B/C 分支完成 → Lead 逐个合并到 `feat/qio-refactor-p1`（解决共享文件冲突）→
D 在整合结果上独立审阅与定向回归 → Lead 修复、更新文档、形成阶段交接提交。

---

## 11. 阶段交接（第一阶段完成，2026-10-21）

### 11.1 分支与提交

- 基线：`aef3c43c0cf5c03a4c551b18970f226a745cc6fc`（origin/main）。
- 公共契约冻结提交：`d265d033`（prompts.py 冻结正文、tools/base.py 结构化字段、
  tools/contracts.py、core/message_assembly.py、memory / knowledge 派生模块拆分、本文件与验证台账）。
- 实现分支（本地，均未 push）：
  - A：`wt/p1-a-prompts`（提示词单文件化 / 统一消息组装 / 适配器与回答协议 / 结构化工具结果）；
  - B：`wt/p1-b-memory`（摘要完整覆盖 / 滚动摘要覆盖位置 / 结构化上下文块）；
  - C：`wt/p1-c-knowledge`（知识新增审批 / 实体创建 / 纠正 / 证据与拒绝墓碑）；
  - D：`wt/p1-d-verify`（独立契约测试与审阅记录）。
- 整合分支：`feat/qio-refactor-p1`（Lead 工作区 `D:\qio-dev\qio-p1-lead`）。
  **后续阶段从本分支的整合提交继续**；不合并 main、不推送、不创建 PR。

### 11.2 实现映射（契约条目 → 落地位置）

| 契约 | 实现位置 | 交接说明 |
| --- | --- | --- |
| §2 提示词单文件 | `backend/src/agent/prompts.py`；`interactive/prompts.py` 变薄引用；各工具引用常量 | 冻结正文逐字保留；新增迁移文案在同文件；旧英文摘要 / 提炼提示词已删除 |
| §3 消息组装 | `core/message_assembly.py`；`services/turn_orchestrator.py`；`services/app.py`（通知轮） | 主对话与通知轮都走同一入口；`ContextBlock` 为与 B 的交接形状 |
| §3 适配器 / 回答协议 | `adapters/{base,native,anthropic,text,fake}.py`；`core/loop.py` | 三档规则一致；协议只注入一次；补救保留通用规则 + hint |
| §4 结构化工具结果 | `tools/base.py`、`tools/contracts.py`、`core/tool_feedback.py`、`tools/registry.py` | `protocol_version==1` 识别；`data` 进模型消息；旧正文兼容；`arg_model` 参数同源校验 |
| §5 记忆 | `memory/{limits,contract,summary,rolling}.py`；`services/memory_lifecycle.py`；`storage/migration_p1_b.py` → schema.py 44/45 | 完整覆盖 + 滚动游标；存取不二次截断 |
| §5 上下文 | `services/context.py::build_context_blocks` | 七类结构化块 + limits；仅 active 知识 |
| §6 知识 / 实体 | `knowledge/{lifecycle,verify,extraction,derivation,evidence,rejection,scope}.py`；`entities/{cards,extract}.py`；`tools/{knowledge_propose,knowledge_correction,entity_create,entity_tools}.py`；`services/app.py`；`api/server.py`；前端 KnowledgePanel / EntityPanel | 主对话一律审批；后台分类规则保持；拒绝墓碑；稳定 ID CAS；实体 decision 语义 |
| §7 类型模型 | `tools/contracts.py`；新工具 pydantic 参数模型 | `json_schema_for` / `validate_model` 同源；enum / additionalProperties 真正执行 |
| §8 迁移 | `storage/schema.py` 追加 44 + 补偿 45；`storage/migration_p1_b.py` 为 SQL 唯一来源 | 历史迁移未改；`COMPENSATION_VERSION = SCHEMA_VERSION = 45` |
| §9 负责人接线 | `tools/continue_tool.py`（当轮片段上下文）；`services/app.py`（通知轮）；`tools/topic_tools.py`（描述单源） | 不改本轮归属、不影响排队消息；不推送 / 不跑完整 CI |

### 11.3 冻结接口备注（后续阶段沿用）

- `ContextBlock(key, data)`：key 必须是 `prompts.CONTEXT_LABELS` 中的键；data 一律 JSON 数据
  （不预拼中文标题），由 `prompts.render_context_blocks` 渲染。
- `ContextAssembler.build_context_blocks(...) -> list[ContextBlock]`，并通过实例属性
  `last_budget` / `last_needs_consolidation` 暴露收尾压缩信号（主对话 trace 与压缩触发读取）。
- `knowledge/derivation.py`：`extract_candidates(adapter, *, summary, evidence_catalog, tracer=None)`、
  `commit_candidates(conn, extraction, *, topic_id, entity_ids, fragment_id, evidence_catalog, tracer=None)`。
- 证据目录条目：`{"id": 真实 id, "kind": message|fragment|summary|entity|knowledge|tool_result|
  attachment|material, "label": 简短说明}`（可带 `role`）。
- `ToolResult` 结构化字段与 `ToolEnvelope` 形状见 §4；`ToolStatus` 为唯一状态枚举。

### 11.4 本阶段确认的裁决（实现 / 验证沿用）

1. 工具描述单一来源：冻结词典内工具必须与 `TOOL_DESCRIPTIONS[name]` 逐字一致；其余工具正文必须
   出现在 prompts.py 源码中（不强制进词典，不得留在工具模块）。
2. `correct_knowledge` 提供 `expected_version` 时，版本不匹配必须返回 conflict/failure；
   只有未提供稳定 ID / 版本时才走 candidate_index / content 兼容匹配。
3. 上下文块 key 必须 ⊆ `CONTEXT_LABELS`；具体使用哪些键由实现决定，验证不限定子集。
4. 补救调用必须保留 QIO_CORE 等通用规则，再追加 `ANSWER_FALLBACK_HINT`（每轮最多一次）。
5. 并行分支不改 `storage/schema.py`；迁移以独立模块交付，由 Lead 登记并追加补偿迁移（见 §8）。
6. B 的分段摘要与知识抽取：所有路径使用 prompts.py 的固定正文；既有 fake provider 的**分派识别**
   可做最小适配，不得放宽断言。
7. 通知轮（系统驱动）同样使用统一消息组装；通知正文来自 prompts.py 的固定模板。

### 11.5 验证与未验证边界

- 定向验证记录见 `docs/qio-verification-ledger.md`（含 A / B / C / D 与 Lead 整合的实际命令与结果）。
- 未执行：完整后端测试集合、完整前端测试集合、完整 CI、真实模型 / 桌面壳实机验收；
  这些按四阶段共同预算留到最终收口。任何“已验证”结论仅覆盖台账中列出的命令。
- 平台：Windows 开发机（共享后端 venv）；前端只跑受影响 vitest 文件。

### 11.6 剩余问题（交给后续阶段）

1. 第二阶段及以后（本阶段未做）：按整体改造计划继续，从本整合提交衍生新分支。
2. 旧 `build_injection` / `short_term_items` 文本路径与 prompts.py 中兼容别名仍在；后续阶段清理时
   需同步替换旧测试，不要把两套路径继续扩散。
3. 通知轮的记忆块暂沿用旧渲染文本（`legacy_text`），后续阶段可切到更细的结构化块。
4. 工具参数说明迁移已覆盖模型可见部分；非模型可见的产品文案（审批弹窗等）仍留在各自模块。
5. 完整测试集合 / CI 未运行；若后续阶段发现旧测试依赖旧提示词字面量，应更新测试而不是恢复旧正文。
6. 本阶段未推送、未合并 main、未发布；集成与发布流程按整体计划在最终阶段执行。
