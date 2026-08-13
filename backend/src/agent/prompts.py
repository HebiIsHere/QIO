# -*- coding: utf-8 -*-
"""
agent/prompts.py —— QIO 全部提示词的唯一集中管理文件。

约定：
- 本模块只存放「给模型看」的文本（工具描述、注入模板、system prompt、维护/自动化指令、
  子任务运行时文案）。业务返回值（如错误消息）不属于提示词，留在各自模块。
- 每个命名常量必须带自然语言注释：说明「用在哪里 + 作用是什么」。
- 带占位符的模板统一用 str.format() 风格（{name}），JSON 花括号用 {{ }} 转义。
"""

# ============================================================================
# 一、工具描述（Tool.description）——注入给模型的功能说明
# ============================================================================

# 话题切换工具 switch_topic 的描述。定义于 agent/tools/topic_tools.py；
# 作用：告诉模型「把当前对话主话题切换到指定已有话题」，切换后消息记录到新话题并建立新旧话题相关关系。
TOOL_SWITCH_TOPIC_DESC = (
    "把当前对话的主话题切换到指定话题。topic_id 必须是已存在的话题。"
    "切换后后续对话将记录到新话题，并在新旧话题间建立相关关系。"
)

# 话题创建工具 create_topic 的描述。定义于 agent/tools/topic_tools.py；
# 作用：告诉模型「当前消息与所有现有话题都不匹配时，创建新话题并切换」，新话题与当前话题自动建立相关关系。
TOOL_CREATE_TOPIC_DESC = (
    "创建新话题并切换为当前主话题。适用于当前消息与所有现有话题都不匹配时。"
    "新话题与当前话题自动建立相关关系。"
)

# 记忆检索工具 memory_search 的描述。定义于 agent/tools/memory_search.py；
# 作用：告诉模型「需要回忆之前的对话/偏好/决定/事实时调用」，返回相关记忆片段及来源话题。
TOOL_MEMORY_SEARCH_DESC = (
    "检索历史记忆。当需要回忆之前的对话、用户偏好、决定或事实时使用；"
    "返回相关记忆片段及其来源话题。"
)

# 知识纠正工具 correct_knowledge 的描述。定义于 agent/tools/knowledge_correction.py；
# 作用：告诉模型「用户明确否定/修正之前的事实、偏好或决定时调用」，支持修正或删除知识条目。
TOOL_CORRECT_KNOWLEDGE_DESC = (
    "纠正或删除知识条目。当用户明确否定、修正之前的事实/偏好/决定时调用。"
    "content 填用户纠正所涉及的知识内容（可从注入的记忆中引用）；"
    "修正用 new_content 给出新内容；否定该知识用 delete=true；"
    "返回多个候选时用 candidate_index 选择。"
)

# 异步子任务取回工具 await_task 的描述。定义于 agent/tools/subagent_tool.py；
# 作用：告诉模型「如何等待或取回异步子任务结果」，notify 决定完成时是否自动唤起主 agent。
TOOL_AWAIT_TASK_DESC = (
    "等待或取回异步子任务的结果。task_id 来自子工具调用的返回。"
    "notify=true 时：任务完成会自动唤起主 agent（若本轮已结束，主 agent 会主动回复你任务结果）；"
    "notify=false 时：阻塞等待直到任务完成或超时。"
)

# 分段读取子任务结果工具 read_task_result 的描述。定义于 agent/tools/subagent_tool.py；
# 作用：告诉模型「结果太长时用 offset/limit 分段读取完整内容」。
TOOL_READ_TASK_RESULT_DESC = (
    "分段读取超长子任务结果的完整内容。"
    "当 await_task 返回的结果包含「子任务结果较长」提示时使用；"
    "offset/limit 控制读取区间，单次最多读取 4000 字符。"
)

# 创建工具开发任务工具 create_tool 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「用户要求开发新工具、现有工具无法满足时调用」，需求不完整先澄清。
TOOL_CREATE_TOOL_DESC = (
    "创建新工具的开发任务。当用户要求开发新工具、现有工具无法满足需求时使用。"
    "需求不完整时先向用户澄清，再调用本工具创建开发任务。"
)

# 写工作区文件工具 dev_write_file 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「在工具开发工作区写入或修改文件（tool.json / tool.py / tests.json 等）」。
TOOL_DEV_WRITE_FILE_DESC = "在工作区写入或修改文件（tool.json / tool.py / tests.json 等）。"

# 读工作区文件工具 dev_read_file 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「读取工具开发工作区文件内容（查看当前实现或测试）」。
TOOL_DEV_READ_FILE_DESC = "读取工作区文件内容（查看当前实现或测试）。"

# 运行工具测试工具 dev_run_tests 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「在沙箱运行 tool.json 定义的测试并返回逐条结果，失败则修改后重跑」。
TOOL_DEV_RUN_TESTS_DESC = (
    "在工作区运行工具测试（读取 tool.json 的定义与测试，沙箱执行），返回逐条结果。"
    "失败时根据错误输出修改后重跑。"
)

# 提交工具成果工具 dev_submit_tool 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「提交工具定义 JSON 进入审批」，审批通过后工具注册、工作区清理。
TOOL_DEV_SUBMIT_DESC = (
    "提交工具开发成果进入审批。definition 为工具定义 JSON（含 name/description/parameters/工具类型/实现/测试），"
    "explanation 用通俗语言说明工具用途（用户会看到）。审批通过后工具注册，工作区清理。"
)

# 工具开发指南 DEV_GUIDE。定义于 agent/tools/dev_tools.py；
# 作用：创建开发任务后随结果一起发给模型，约束整个开发流程（范式/需求规格/契约/提交口径）。
DEV_GUIDE = """工具开发指南：
1. 开发范式：理解需求 → 在 tool.json 写工具定义（name/description/parameters/tool_type/sync/code/tests）→ 运行测试 → 失败则读取错误、修改定义、重跑，直至全部通过 → 提交审批。
2. 需求规格必填项：工具用途（一句话）、输入输出、使用场景、是否需要凭据（访问外部服务时）、类型（function/subagent）、同步/异步。需求不完整时，先与用户澄清再开始开发。
3. 契约约束：纯函数、单文件实现、测试用例 ≥1 条且确定性断言；subagent 型需 model 与 credential_ref，可跳过确定性测试。
4. 提交时用通俗语言说明工具用途（用户不接触代码）。"""


# ============================================================================
# 二、注入模板（长期记忆注入）——组装进每条用户消息前的系统上下文
# ============================================================================

# 长期记忆注入的整体标题。定义于 agent/services/injection.py；
# 作用：标记注入块开始，让模型识别「以下是系统注入的记忆与上下文，不是用户消息」。
INJECT_HEADER = "【长期记忆注入】"

# 【话题】节模板。定义于 agent/services/injection.py；
# 作用：展示当前话题、相关话题、切换建议（内容由 app.py _topic_note 生成），占位符 {topic_note}。
INJECT_TOPIC_SECTION = "【话题】{topic_note}"

# 【话题建议】节模板。定义于 agent/services/injection.py；
# 作用：当预测器判定当前消息与现有话题都不匹配时，提示模型可调用 create_topic 创建新话题，
# 占位符 {reason} 说明「最高话题相似度 xx 低于阈值」。
INJECT_NEW_TOPIC_SECTION = (
    "【话题建议】当前消息可能与现有话题都不匹配（{reason}）。"
    "如需创建新话题，请调用 create_topic 工具。"
)

# 新话题候选的兜底原因。定义于 agent/services/injection.py；
# 作用：当未给出具体 reason 时使用，说明「与现有话题都不匹配」。
INJECT_NEW_TOPIC_DEFAULT_REASON = "与现有话题都不匹配"

# 【短期·当前话题】节模板。定义于 agent/services/app.py；
# 作用：注入当前话题最近的短期记忆（开放片段 + 近期摘要），占位符 {text}。
INJECT_SHORT_TERM_SECTION = "【短期·当前话题】\n{text}"

# 【记忆·标题】条目模板。定义于 agent/services/injection.py；
# 作用：格式化单条检索到的历史记忆，占位符 {title} 话题名、{preview} 内容预览。
INJECT_MEMORY_ITEM = "[记忆·{title}] {preview}"

# 【相关话题记忆·标题】条目模板。定义于 agent/services/injection.py；
# 作用：格式化相关话题片段的记忆摘要，占位符 {title}、{summary}。
INJECT_RELATED_MEMORY_ITEM = "[相关话题记忆·{title}] {summary}"

# 【聚焦片段·标题】节模板。定义于 agent/services/app.py _focus_block；
# 作用：注入「从这里开始」选中的锚点片段标题作为小节头，其后动态拼接片段摘要与消息，
# 占位符 {title}。
INJECT_FOCUS_SECTION = "【聚焦片段·{title}】"

# 话题上下文：当前话题行。定义于 agent/services/app.py _topic_note；
# 作用：告诉模型「现在的主话题是谁」，占位符 {name}、{topic_id}。
TOPIC_NOTE_CURRENT = "当前话题：{name}（{topic_id}）"

# 话题上下文：相关话题行。定义于 agent/services/app.py _topic_note；
# 作用：列出预测器给出的辅助话题，占位符 {names}（已格式化的「名称」（id）列表）。
TOPIC_NOTE_RELATED = "相关话题：{names}"

# 话题上下文：切换建议行。定义于 agent/services/app.py _topic_note；
# 作用：当预测器建议切换时提示模型「可调用 switch_topic」，占位符 {name}、{topic_id}、{score}。
TOPIC_NOTE_SWITCH = "建议切换到「{name}」（{topic_id}，相似度 {score:.2f}），可调用 switch_topic"

# 话题上下文：新话题候选原因。定义于 agent/services/app.py run_turn；
# 作用：当预测器标记新话题候选时，说明「最高相似度低于阈值、无匹配话题」，占位符 {top}。
TOPIC_NOTE_NEW_REASON = "最高话题相似度 {top:.2f} 低于阈值，无匹配话题"


# ============================================================================
# 三、子任务运行时提示词（subagent）
# ============================================================================

# 子任务启动回执。定义于 agent/tools/subagent_tool.py；
# 作用：异步启动子任务后返回给主模型的指引（如何取回结果），占位符 {task_id}。
SUBAGENT_STARTED = (
    "子任务已启动，task_id={task_id}。"
    "可调用 await_task(task_id=..., notify=false) 取回结果，"
    "或 await_task(task_id=..., notify=true) 在完成时自动唤起。"
)

# 子任务执行 prompt 模板。定义于 agent/tools/subagent_tool.py _execute；
# 作用：作为子 agent 的首条消息，交代任务、参数与输出约束，占位符 {description}、{kwargs}、{limit}。
SUBAGENT_PROMPT = (
    "任务：{description}\n"
    "参数：{kwargs}\n\n"
    "输出要求：最终回复不超过 {limit} 字，直接给出结论与要点，不要复述任务或参数。"
)

# 子任务结果超长提示。定义于 agent/tools/subagent_tool.py _execute；
# 作用：当子任务结果超过输出上限时，提示主模型用 read_task_result 分段读取，占位符 {length}、{preview}、{task_id}。
SUBAGENT_RESULT_LONG = (
    "【子任务结果较长（共 {length} 字符）】\n"
    "预览：{preview}\n"
    "完整内容请调用 read_task_result(task_id=\"{task_id}\", offset=0, limit=4000) 分段读取。"
)

# 子任务完成通知。定义于 agent/services/app.py _format_notice；
# 作用：异步子任务完成时唤起主 agent 的通知文本，占位符 {tool}、{task_id}、{status}、{preview}。
NOTIFY_SUBTASK_DONE = "【子任务完成】工具 {tool}（{task_id}）已{status}。\n结果预览：{preview}"


# ============================================================================
# 四、System Prompt（adapter 层）
# ============================================================================

# text 模式 system prompt 前缀。定义于 agent/adapters/text.py build_system_prompt；
# 作用：没有原生工具调用 API 的降级模式，约束模型用 ```json 块输出工具调用。
SYSTEM_PROMPT_TEXT_MODE = (
    "You are a tool-calling assistant without a native tool API. "
    "You MUST respond with a single JSON object in a ```json block "
    "using exactly this shape when you want to call a tool:\n"
    '{"tool_calls": [{"name": "<tool>", "arguments": {}}]}\n'
    "If no tool is needed, respond with plain text only."
)

# text 模式工具列表标题。定义于 agent/adapters/text.py build_system_prompt；
# 作用：在 system prompt 中分隔「可用工具」区，标题后追加每个工具的名称/描述/参数 schema。
SYSTEM_PROMPT_TOOLS_HEADER = "Available tools:"

# text 模式单条工具条目模板。定义于 agent/adapters/text.py build_text_tools；
# 作用：格式化单个工具为「- 名称: 描述 + 参数 JSON Schema」，占位符 {name}、{description}、{schema}。
TEXT_TOOL_ENTRY = "- {name}: {description}\n  parameters (JSON Schema): {schema}"


# ============================================================================
# 五、离线维护 / 自动化（maintenance）
# ============================================================================

# 记忆维护分析 prompt。定义于 agent/services/maintenance.py；
# 作用：驱动「Dreaming」离线维护，让模型分析知识与摘录，输出 correct/merge/expire 候选 JSON，
# 占位符 {knowledge}、{summaries}。
DREAMING_PROMPT = """你是记忆维护分析器。分析以下知识与记忆摘录，找出：
1. correct：内容冲突或已过时（给出修正后的新内容）；
2. merge：内容高度重复（合并后内容）；
3. expire：长期无价值/矛盾（直接过期）。
只输出 JSON：{{"candidates": [{{"action": "correct|merge|expire", "knowledge_id": "<id>", "new_content": "<修正/合并后内容，expire 可省略>", "reason": "<原因>"}}]}}
最多 10 条。没有需要处理的项目时输出 {{"candidates": []}}。

知识条目：
{knowledge}

最近片段摘要：
{summaries}"""

# 工具自动化草案 prompt。定义于 agent/services/maintenance.py；
# 作用：当检测到用户多次提出相似请求时，让模型生成一个工具定义草案（帮助自动化这类任务），
# 后面拼接请求列表。
TOOL_AUTOMATION_PROMPT = (
    "以下是用户多次提出的相似请求，请生成一个工具定义草案（帮助用户自动化这类任务）。"
    "只输出 JSON：{\"name\": \"snake_case 工具名\", \"description\": \"人话说明\", "
    "\"parameters\": {\"type\": \"object\", \"properties\": {}}}\n请求：\n"
)


# ============================================================================
# 六、实体卡（Entity Card）——经验性实体提炼与注入
# ============================================================================

# 实体提炼指令。定义于 agent/services/app.py _close_fragment 扩展；
# 作用：封块时让主模型从对话提炼「经验性实体卡」候选（name/aliases/kind/summary/attributes/relations），
# 本地 JSON Schema 校验后写入 entity_cards；只收私人化、带个人属性的对象，不收通用概念。
ENTITY_EXTRACT_PROMPT = (
    "你是实体提炼器。从这段对话里，找出用户私人世界里反复提及、带有个人属性的对象"
    "（如「我家的鹅」「我师哥」「我爱吃的五里关火锅」）。"
    "只输出 JSON：{\"entities\": [{\"name\": \"实体名\", \"aliases\": [\"别名\"], "
    "\"kind\": \"类型(可选)\", \"summary\": \"一句话(可选)\", "
    "\"attributes\": [{\"key\": \"属性名\", \"value\": \"属性值\"}], "
    "\"relations\": [{\"target\": \"关联实体名\", \"type\": \"关系类型\"}]}]}\n"
    "规则：不收通用概念（人/火锅/电脑）；属性必须是对话中明确提到的个人化信息；"
    "关系是实体间的真实联系（属于/母子/饲养…）。\n对话：\n{messages}"
)

# 实体卡注入模板。定义于 agent/entities/cards.py format_card；
# 作用：对话命中实体时高优注入的卡文本（【实体·名称】+ 属性 + 关系），
# 占位符 {name}、{summary}、{attrs}、{rels}。
ENTITY_CARD_INJECT = "【实体·{name}】{summary}\n属性：{attrs}\n关系：{rels}"
