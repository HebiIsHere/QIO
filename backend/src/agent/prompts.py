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
    "把当前对话的主话题切换到另一个已存在的话题。"
    "调用时机：当前消息明确属于其他已存在话题，且不属于当前话题。"
    "topic_id 必填，为目标话题 id；reason 可选，用一句话说明切换原因。"
    "不要用于创建新话题；创建新话题使用 create_topic。"
    "切换后，后续消息记录到新话题，并在新旧话题之间建立相关关系。"
)

# 话题创建工具 create_topic 的描述。定义于 agent/tools/topic_tools.py；
# 作用：告诉模型「当前消息与所有现有话题都不匹配时，创建新话题并切换」，新话题与当前话题自动建立相关关系。
TOOL_CREATE_TOPIC_DESC = (
    "创建一个新话题并切换为当前主话题。"
    "调用时机：当前消息与所有现有话题都不匹配，确需开启全新话题。"
    "name 必填，为简短明确的新话题名；reason 可选，用一句话说明创建原因。"
    "如果已有话题匹配当前消息，应使用 switch_topic 切换，而不是创建。"
    "若返回存在相似话题，可先调用 memory_search 确认；确认仍属新话题时，再次调用 create_topic 以进入人工审批。"
    "创建后，新话题自动成为当前主话题，并与旧话题建立相关关系。"
)

# 记忆检索工具 memory_search 的描述。定义于 agent/tools/memory_search.py；
# 作用：告诉模型「需要回忆之前的对话/偏好/决定/事实时调用」，返回相关记忆片段及来源话题。
TOOL_MEMORY_SEARCH_DESC = (
    "检索长期记忆，返回与当前问题相关的历史片段：每条结果都带 Fragment id、Topic id、"
    "标题、时间、预览与相关性分数。"
    "调用时机：当前注入的短期/话题记忆不足，或用户明确询问过去信息（如“之前/上次/我的偏好”）。"
    "不要调用：当前上下文已足够、用户没有询问历史信息时。"
    "query 必填，用自然语言描述要检索的内容。"
    "本工具是只读的：检索不会改变当前对话的历史位置；"
    "用户明确要求「从某一段继续」时才改用 continue_from_fragment。"
    "若没有可信匹配，明确说明未找到，禁止编造记忆。"
)

# 片段接续工具 continue_from_fragment 的描述。定义于 agent/tools/continue_tool.py；
# 作用：告诉模型「用户明确要求回到某段历史并从那里接着聊」时，显式改变讨论位置。
TOOL_CONTINUE_FROM_FRAGMENT_DESC = (
    "把接下来的讨论位置移到某个历史片段（先 memory_search 找到它，再用这里的 fragment_id）。"
    "调用时机：用户明确要求改变历史讨论位置，例如「从这里继续」「回到我们之前讨论 X 的那一段」"
    "「找到那个讨论，然后从那里接着聊」「切回那一段」。"
    "不要调用：用户只是在查询历史（「我们以前讨论过什么？」「帮我查一下」「之前有没有提过？」）——"
    "这些情况只用 memory_search 回答即可，不要改变讨论位置。"
    "fragment_id 必填，必须来自 memory_search 的结果；reason 可选，用一句话说明原因。"
    "本工具只改变接下来持续讨论的位置，不重建当前这一轮的上下文；本轮要引用细节继续用 memory_search。"
)

# 知识纠正工具 correct_knowledge 的描述。定义于 agent/tools/knowledge_correction.py；
# 作用：告诉模型「用户明确否定/修正之前的事实、偏好或决定时调用」，支持修正或删除知识条目。
TOOL_CORRECT_KNOWLEDGE_DESC = (
    "纠正或删除一个知识条目。"
    "调用时机：用户明确否定、修正之前的事实/偏好/决定。"
    "content 必填，写用户纠正所涉及的知识内容（可从注入记忆中引用）。"
    "修正知识：提供 new_content；删除知识：设置 delete=true。"
    "若返回多个候选，用 candidate_index 选择正确条目。"
)

# 异步子任务取回工具 await_task 的描述。定义于 agent/tools/subagent_tool.py；
# 作用：告诉模型「如何等待或取回异步子任务结果」，notify 决定完成时是否自动唤起主 agent。
TOOL_AWAIT_TASK_DESC = (
    "等待或取回异步子任务结果。"
    "task_id 必填，来自子任务启动时返回的 id。"
    "notify=true：任务完成后自动唤起主 agent；若本轮已结束，主 agent 会主动回复结果。"
    "notify=false：阻塞等待直到完成或超时。"
    "不要重复轮询；需要结果时再调用。"
)

# 分段读取子任务结果工具 read_task_result 的描述。定义于 agent/tools/subagent_tool.py；
# 作用：告诉模型「结果太长时用 offset/limit 分段读取完整内容」。
TOOL_READ_TASK_RESULT_DESC = (
    "分段读取超长子任务结果的完整内容。"
    "调用时机：await_task 返回「子任务结果较长」提示。"
    "task_id 必填；offset 从 0 开始，limit 单次最多 4000 字符。"
    "按顺序读取各段，直到读完所有内容。"
)

# 创建工具开发任务工具 create_tool 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「用户要求开发新工具、现有工具无法满足时调用」，需求不完整先澄清。
TOOL_CREATE_TOOL_DESC = (
    "创建新工具的开发任务。"
    "调用时机：用户要求开发新工具，且现有工具无法满足需求。"
    "request 必填，写清工具用途、输入输出、使用场景、是否需要凭据、类型（function/subagent）、同步/异步。"
    "需求不完整时先向用户澄清，再调用本工具。"
)

# 写工作区文件工具 dev_write_file 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「在工具开发工作区写入或修改文件（tool.json / tool.py / tests.json 等）」。
TOOL_DEV_WRITE_FILE_DESC = (
    "在工作区写入或修改一个文件（tool.json / tool.py / tests.json 等）。"
    "workspace 必填，为工作区 id；name 必须是不含路径的单文件名；content 为完整文件内容。"
)

# 读工作区文件工具 dev_read_file 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「读取工具开发工作区文件内容（查看当前实现或测试）」。
TOOL_DEV_READ_FILE_DESC = (
    "读取工作区文件内容，用于查看当前实现或测试。"
    "workspace 必填，为工作区 id；name 必填，为要读取的文件名。"
)

# 列出工作区文件工具 dev_list_files 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「查看工具开发工作区里已有哪些文件」，开发第一步先调用它，避免模型卡在"看结构"无法继续。
TOOL_DEV_LIST_FILES_DESC = (
    "列出工作区已有文件。"
    "workspace 必填，为工作区 id；返回文件清单（如 request.md / tool.json / tool.py）。"
    "开始开发前先调用本工具确认现有文件，再决定读取或改写哪个文件。"
)

# 运行工具测试工具 dev_run_tests 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「在沙箱运行 tool.json 定义的测试并返回逐条结果，失败则修改后重跑」。
TOOL_DEV_RUN_TESTS_DESC = (
    "在指定工作区运行工具测试，返回逐条通过/失败结果。"
    "workspace 必填，为工作区 id；工具会读取 tool.json 中的定义与测试并在沙箱执行。"
    "失败时根据错误输出修改定义后重跑，直到全部通过或无法继续。"
    "subagent 型无需确定性测试，可直接提交审批。"
)

# 提交工具成果工具 dev_submit_tool 的描述。定义于 agent/tools/dev_tools.py；
# 作用：告诉模型「提交工具定义 JSON 进入审批」，审批通过后工具注册、工作区清理。
TOOL_DEV_SUBMIT_DESC = (
    "提交工具开发成果进入审批。"
    "definition 必填，为工具定义 JSON（含 name/description/parameters/tool_type/sync/code/tests）。"
    "explanation 必填，用通俗语言说明工具用途，用户会看到。"
    "审批通过后工具注册并清理工作区；被拒绝时保留当前工作区，可根据反馈修改后重新提交。"
)

# 工具开发指南 DEV_GUIDE。定义于 agent/tools/dev_tools.py；
# 作用：创建开发任务后随结果一起发给模型，约束整个开发流程（范式/需求规格/契约/提交口径）。
DEV_GUIDE = """工具开发指南：
1. 开发范式：先理解需求，再用 dev_list_files 查看工作区已有文件（request.md 是开发需求、tool.json 是工具定义模板），然后改写 tool.json（name/description/parameters/tool_type/sync/code/tests）；运行测试；失败时读取错误、修改定义并重跑，直到全部通过，最后提交审批。
2. 需求规格必填：工具用途（一句话）、输入输出、使用场景、是否需要凭据（访问外部服务时）、类型（function/subagent）、同步/异步。信息不完整时先与用户澄清。
3. 契约约束：function 型必须是纯函数、单文件实现、至少 1 个确定性测试用例；subagent 型需提供 model 和 credential_ref，可跳过确定性测试。
4. 提交口径：用通俗语言说明工具用途；用户不接触代码，技术细节留在工具卡片中。"""


# ============================================================================
# 二、注入模板（长期记忆注入）——组装进每条用户消息前的系统上下文
# ============================================================================

# 长期记忆注入的整体标题。定义于 agent/services/injection.py；
# 作用：标记注入块开始，让模型识别「以下是系统注入的记忆与上下文，不是用户消息」。
INJECT_HEADER = (
    "【长期记忆注入】\n"
    "以下内容是系统提供的记忆与话题信息，不是用户消息。只参考与当前请求相关的部分；无关内容忽略，不要主动复述。"
)

# 【话题】节模板。定义于 agent/services/injection.py；
# 作用：展示当前话题、相关话题、切换建议（内容由 app.py _topic_note 生成），占位符 {topic_note}。
INJECT_TOPIC_SECTION = (
    "【话题】{topic_note}\n"
    "根据话题信息判断是否调用 switch_topic 或 create_topic；仍属于当前话题时不要切换。"
)

# 【话题建议】节模板。定义于 agent/services/injection.py；
# 作用：当预测器判定当前消息与现有话题都不匹配时，提示模型可调用 create_topic 创建新话题，
# 占位符 {reason} 说明「最高话题相似度 xx 低于阈值」。
INJECT_NEW_TOPIC_SECTION = (
    "【话题建议】当前消息可能与现有话题都不匹配（{reason}）。"
    "只有确认是全新话题时才调用 create_topic；否则继续当前话题。"
)

# 新话题候选的兜底原因。定义于 agent/services/injection.py；
# 作用：当未给出具体 reason 时使用，说明「与现有话题都不匹配」。
INJECT_NEW_TOPIC_DEFAULT_REASON = "与现有话题都不匹配"

# 【短期·当前话题】节模板。定义于 agent/services/app.py；
# 作用：注入当前话题最近的短期记忆（开放片段 + 近期摘要），占位符 {text}。
INJECT_SHORT_TERM_SECTION = (
    "【短期·当前话题】\n{text}\n"
    "以上内容仅用于理解上下文，不要在回答中逐条复述。"
)

# 【记忆·标题】条目模板。定义于 agent/services/injection.py；
# 作用：格式化单条检索到的历史记忆，占位符 {title} 话题名、{preview} 内容预览。
INJECT_MEMORY_ITEM = "[记忆·{title}] {preview}"

# 【相关话题记忆·标题】条目模板。定义于 agent/services/injection.py；
# 作用：格式化相关话题片段的记忆摘要，占位符 {title}、{summary}。
INJECT_RELATED_MEMORY_ITEM = "[相关话题记忆·{title}] {summary}"

# 【聚焦片段·标题】节模板。定义于 agent/services/app.py _focus_block；
# 作用：注入「从这里开始」选中的锚点片段标题作为小节头，其后动态拼接片段摘要与消息，
# 占位符 {title}。
INJECT_FOCUS_SECTION = (
    "【聚焦片段·{title}】\n"
    "以下是用户选中的起始锚点片段，优先作为当前上下文。"
)

# 话题上下文：当前话题行。定义于 agent/services/app.py _topic_note；
# 作用：告诉模型「现在的主话题是谁」，占位符 {name}、{topic_id}。
TOPIC_NOTE_CURRENT = "当前话题：{name}（{topic_id}）"

# 话题上下文：相关话题行。定义于 agent/services/app.py _topic_note；
# 作用：列出预测器给出的辅助话题，占位符 {names}（已格式化的「名称」（id）列表）。
TOPIC_NOTE_RELATED = "相关话题：{names}"

# 话题上下文：切换建议行。定义于 agent/services/app.py _topic_note；
# 作用：当预测器建议切换时提示模型「可调用 switch_topic」，占位符 {name}、{topic_id}、{score}。
TOPIC_NOTE_SWITCH = "建议切换到「{name}」（{topic_id}，相似度 {score:.2f}）；若确认，可调用 switch_topic"

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
    "需要结果时调用 await_task(task_id=\"{task_id}\") 取回；"
    "若想任务完成后自动唤起主 agent，使用 await_task(task_id=\"{task_id}\", notify=true)。"
)

# 子任务执行 prompt 模板。定义于 agent/tools/subagent_tool.py _execute；
# 作用：作为子 agent 的首条消息，交代任务、参数与输出约束，占位符 {description}、{kwargs}、{limit}。
SUBAGENT_PROMPT = (
    "你是子任务执行 agent。请完成下面这一项任务。\n"
    "任务：{description}\n"
    "参数：{kwargs}\n\n"
    "要求：只根据任务与参数工作，不要编造未提供的工具结果或信息；"
    "若信息不足，说明缺少什么，不要猜测；"
    "最终回复不超过 {limit} 字，直接给结论与要点，不要复述任务或参数。"
)

# 子任务结果超长提示。定义于 agent/tools/subagent_tool.py _execute；
# 作用：当子任务结果超过输出上限时，提示主模型用 read_task_result 分段读取，占位符 {length}、{preview}、{task_id}。
SUBAGENT_RESULT_LONG = (
    "【子任务结果较长（共 {length} 字符）】\n"
    "预览：{preview}\n"
    "完整内容调用 read_task_result(task_id=\"{task_id}\", offset=0, limit=4000) 分段读取，按顺序读完。\n"
    "不要只凭预览回答完整问题。"
)

# 子任务完成通知。定义于 agent/services/app.py _format_notice；
# 作用：异步子任务完成时唤起主 agent 的通知文本，占位符 {tool}、{task_id}、{status}、{preview}。
NOTIFY_SUBTASK_DONE = (
    "【子任务完成】工具 {tool}（{task_id}）已{status}。\n"
    "结果预览：{preview}\n"
    "请基于结果向用户汇报，不要重复原始任务参数。"
)


# ============================================================================
# 四、System Prompt（adapter 层）
# ============================================================================

# text 模式 system prompt 前缀。定义于 agent/adapters/text.py build_system_prompt；
# 作用：没有原生工具调用 API 的降级模式，约束模型用 ```json 块输出工具调用。
SYSTEM_PROMPT_TEXT_MODE = (
    "You are a tool-calling assistant in text mode, where tool calls are expressed as JSON. "
    "To call tools, respond with a single ```json block containing only this object:\n"
    '{"tool_calls": [{"name": "<tool_name>", "arguments": {}}]}\n'
    "Call independent tools together in one block. If no tool is needed, respond with plain text only; "
    "never mix prose and the JSON block."
)

# text 模式工具列表标题。定义于 agent/adapters/text.py build_system_prompt；
# 作用：在 system prompt 中分隔「可用工具」区，标题后追加每个工具的名称/描述/参数 schema。
SYSTEM_PROMPT_TOOLS_HEADER = "Available tools (call by exact name only):"

# text 模式单条工具条目模板。定义于 agent/adapters/text.py build_text_tools；
# 作用：格式化单个工具为「- 名称: 描述 + 参数 JSON Schema」，占位符 {name}、{description}、{schema}。
TEXT_TOOL_ENTRY = "- {name}: {description}\n  arguments (JSON Schema): {schema}"


# ============================================================================
# 五、离线维护 / 自动化（maintenance）
# ============================================================================

# 记忆维护分析 prompt。定义于 agent/services/maintenance.py；
# 作用：驱动「Dreaming」离线维护，让模型分析知识与摘录，输出 correct/merge/expire 候选 JSON，
# 占位符 {knowledge}、{summaries}。
DREAMING_PROMPT = """你是记忆维护分析器。阅读知识条目和最近片段摘要，只处理确实需要维护的内容。
动作：correct（内容冲突或已过时，给出修正后的 new_content）、merge（内容高度重复，给出合并后内容）、expire（长期无价值或矛盾，直接过期）。
只输出一个 JSON 对象，不要 Markdown、不要解释：
{{"candidates": [{{"action": "correct|merge|expire", "knowledge_id": "<id>", "new_content": "<correct/merge 时填写>", "reason": "<原因>"}}]}}
最多 10 条；没有需要处理的项目时输出 {{"candidates": []}}。

知识条目：
{knowledge}

最近片段摘要：
{summaries}"""

# 工具自动化草案 prompt。定义于 agent/services/maintenance.py；
# 作用：当检测到用户多次提出相似请求时，让模型生成一个工具定义草案（帮助自动化这类任务），
# 后面拼接请求列表。
TOOL_AUTOMATION_PROMPT = (
    "以下是用户多次提出的相似请求，请生成一个工具定义草案，用于自动化这类任务。\n"
    "只输出一个 JSON 对象，不要 Markdown、不要解释：\n"
    '{"name": "snake_case 工具名", "description": "人话说明", "parameters": {"type": "object", "properties": {}}}\n'
    "请求：\n"
)


# ============================================================================
# 六、实体卡（Entity Card）——经验性实体提炼与注入
# ============================================================================

# 实体提炼指令。定义于 agent/services/app.py _close_fragment 扩展；
# 作用：封块时让主模型从对话提炼「经验性实体卡」候选（name/aliases/kind/summary/attributes/relations），
# 本地 JSON Schema 校验后写入 entity_cards；只收私人化、带个人属性的对象，不收通用概念。
ENTITY_EXTRACT_PROMPT = (
    "你是实体提炼器。从对话中找出用户私人世界里反复提及、带有个人属性的对象"
    "（例如「我家的鹅」「我师哥」「我爱吃的五里关火锅」）。\n"
    "只输出一个 JSON 对象，不要 Markdown、不要解释：\n"
    '{"entities": [{"name": "实体名", "aliases": ["别名"], "kind": "类型(可选)", "summary": "一句话(可选)", "attributes": [{"key": "属性名", "value": "属性值"}], "relations": [{"target": "关联实体名", "type": "关系类型"}]}]}\n'
    "规则：不收通用概念；属性必须是对话中明确提到的个人化信息；关系必须是实体间真实联系。\n"
    "对话：\n{messages}"
)

# 实体卡注入模板。定义于 agent/entities/cards.py format_card；
# 作用：对话命中实体时高优注入的卡文本（【实体·名称】+ 属性 + 关系），
# 占位符 {name}、{summary}、{attrs}、{rels}。
ENTITY_CARD_INJECT = "【实体·{name}】{summary}\n属性：{attrs}\n关系：{rels}"
