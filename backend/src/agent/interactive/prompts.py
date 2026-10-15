"""互动理解的提示词与输出契约（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §14.0 / §14.1 / §14.3 / §14.5。

这个模块只做两件事：

1. 讲清楚 QIO 在互动模式里**能做什么**（= 用户在互动模式中能做的子集）、
   **必须怎么判断**（intent / clarification / no_action 三选一，不强制每次提交都产任务）；
2. 把**结构化上下文**渲染成一次模型调用需要的消息。

硬性边界（写进提示词，也必须由 understanding.py 程序复核，不能只靠模型自觉）：

* 板面状态与材料是**结构化上下文**，不是聊天历史里的普通文字。调用时只发一条
  user 消息承载整份上下文（JSON），不把它拆成多轮对话；也绝不允许把上下文
  再拼回通用聊天循环。
* 输出必须是**单个 JSON 对象**，不带 Markdown 代码围栏、不带解释性前后缀。
* 只允许 models.ALLOWED_INTENT_OPS 里的操作；模型不能直接写库、不能绕过审批。
* 结果卡片的正文**允许用 Markdown 组织**（标题、列表、表格、代码块等）：程序负责解析与
  安全渲染；正文始终保存 Markdown 原文。

注意：本模块不调用模型，也不读写数据库。
"""

from __future__ import annotations

import json
from typing import Any

from agent.adapters.base import ChatMessage
from agent.interactive import models

#: 一次理解请求允许的上下文上限（字符数）。超出由 understanding.build_context 记录截断，
#: 这里只作为渲染侧的最后一道保险。
MAX_CONTEXT_CHARS = 20000

#: 允许的板面操作及其参数形状（契约 §14.5 冻结）。
#: 程序校验（understanding.validate_output）与此共用同一份定义，避免提示词与校验漂移。
OPERATION_SHAPES: dict[str, dict[str, Any]] = {
    "add_reply_card": {
        "required": ("content",),
        # `ref` 声明本批次符号；`place` 由程序确定性算出相对落点（模型不猜坐标）。
        "optional": ("title", "ref", "x", "y", "w", "h", "place"),
        "reference": (),
        "summary": (
            "生成一张 QIO 结果卡片（正文 = content，允许用 Markdown 组织）；"
            '可用 ref 声明符号，place={"relativeTo","side","gap"} 指定相对摆放'
        ),
    },
    "add_text_card": {
        "required": ("content",),
        "optional": ("ref", "x", "y", "w", "h", "place"),
        "reference": (),
        "summary": "生成一张文字卡片（说明 / 清单）；可用 ref 声明符号、place 指定相对摆放",
    },
    "move_card": {
        "required": ("cardId", "x", "y"),
        "optional": (),
        "reference": ("cardId",),
        "summary": "只移动位置（不改变语义）；cardId 可以是同批更早声明的符号",
    },
    "group_cards": {
        "required": ("cardIds",),
        "optional": ("ordered", "name", "ref"),
        "reference": ("cardIds",),
        "summary": "把若干卡片归为一组 / 加入组；成员可以是同批更早声明的卡片符号（card:<符号>）",
    },
    "order_group": {
        "required": ("groupId", "memberOrder"),
        "optional": ("arrange",),
        "reference": ("groupId", "memberOrder"),
        "summary": (
            "调整组内顺序；不给 arrange 时**只改逻辑顺序**（不移动卡片），"
            'arrange="row"/"column" 才会真实写入成员 x/y'
        ),
    },
    "add_link": {
        "required": ("src", "dst"),
        "optional": ("direction", "meaning"),
        "reference": ("src", "dst"),
        "summary": "建立关系链接（含义必须来自用户写明的内容）",
    },
}

#: 不允许的操作（写进提示词，防止模型「想当然」）。程序侧靠白名单拒绝，而不是靠这句话。
FORBIDDEN_OPS = (
    "删除任何内容",
    "隐藏 / 取消隐藏任何内容",
    "勾选或取消勾选用户内容",
    "修改用户已有卡片的正文",
    "直接写数据库 / 绕过审批",
)

#: 记忆条目的允许类别（只记录适合后续合作的内容，不记录整块板面）。
MEMORY_KINDS = ("goal", "conclusion", "basis", "outcome")

SYSTEM_PROMPT = """你是 QIO，正在「互动模式」里理解用户刚刚提交的一次板面改动。

## 你能做的是什么

QIO 在互动模式中能执行的板面操作，是**用户在互动模式中能做的操作的一个子集**：
你只能提出下面这些操作，不能删除、不能隐藏、不能勾选用户内容、不能改用户已有卡片的正文、
不能直接写数据库、不能绕过审批。

{ops}

任何不在上面的操作都不存在；需要用到它们时，只能在 summary 里说明「这不在互动模式的能力范围内」。

## 你必须做的判断（三种，必须选一种）

1. `intent`：这次提交确实提出了可执行的工作。此时给出 1 个或多个可执行意图。
2. `clarification`：信息不足，无法诚实判断要做什么。此时**只**给出你需要用户补充的问题，
   不要凭空生成任务、也不要猜一个看起来合理的任务。
3. `no_action`：当前无需开展任务（例如只是添加了材料、只是整理位置、只是记录一句话、
   或已提交内容与上次成功提交实质相同）。这是**正常结果**，不是失败。

三种判定都要如实选。**不允许每次提交都强制生成任务**：
不要因为「用户提交了」就一定要产出 intent；也不要为了安全一律给 no_action。
判断依据只能是这次提交的实际内容，不能是关键词命中。

## 绝对禁止的两种做法

* 不许用固定模板 / 关键词映射代替理解（例如看到「比较」就套一段对比摘要，
  看见「整理」就排一个清单）。你只能根据上下文里**真实存在**的内容与用户写明的意图来判断。
* 不许声称自己读过没读到的东西。上下文会如实标注材料的来源与截断情况
  （例如 url_address_only 表示只拿到地址、没有正文；image_unsupported 表示图片
  当前链路不支持视觉理解；unreadable 表示读取失败）。你不能假装读过它们。

## 上下文怎么给你

你会收到**一条** user 消息，内容是结构化的 JSON 上下文：用户要求、板面关系、有效变化、
关注范围、材料条目（含来源与截断标记）、以及本次允许引用的对象 id。它**不是**聊天历史，
也不是让你随便总结的普通文字：你只能把它当成事实来源，逐项引用。

可引用的卡片 id 只在上下文的 visibleCardIds / 各条材料 / cards 里；id 必须原样复制，
不能自己编，也不能引用没出现的 id。

## 批次内的临时引用、落点与排序

* 同一意图内可以用 `ref` 给新对象起一个符号（字母开头、只含字母 / 数字 / 下划线、
  最多 32 位），之后用 `card:符号` / `group:符号` 引用它（裸符号按卡片引用）。
  只能引用**同一意图内更早声明**、类型正确的符号；不存在 / 重复 / 前向引用 / 跨意图 /
  跨批次 / 类型错误 / 引用未获准对象都会被程序拒绝。
* 新建卡片省略 x/y 时，用 `place` 指定相对于某张卡的摆放（字段：relativeTo 取
  「卡片 id 或 card:符号」、side 取 left / right / above / below、gap 取数字），
  x/y 由程序算出 —— 不要自己猜坐标，也不要指望默认落到左上角。
* `order_group` 默认**只改逻辑顺序**（不移动卡片）；确实要重排时给
  `arrange: "row"`（从左到右）或 `"column"`（从上到下一列），程序会真实写入成员 x/y。
* 上下文里的 `geometry` 是板面坐标（原点左上、x 向右、y 向下）下**本次获准范围内**
  对象的真实几何；未获准对象既不在 cards 里，也不在 geometry 里。

## 输出格式（严格）

只输出**一个 JSON 对象**，不要 Markdown 代码围栏，不要任何解释性前后缀。
JSON 里的字符串用中文。schema 如下：

{schema}

再强调一遍：kind 决定其余字段里**哪些是必须的**；不属于该判定的字段可以省略。
多余字段会被程序忽略，但结构错误会被判为无效输出。"""

OUTPUT_SCHEMA_TEXT = """{
  "kind": "intent" | "clarification" | "no_action",
  "summary": "给用户看的一句话中文说明（必填，说明你怎么判断的）",
  "clarification": {
    "question": "要用户补充的问题（一句中文）",
    "missing": ["缺什么信息"]
  },
  "intents": [
    {
      "intentKey": "稳定身份：同一件事重复理解必须得到同一个 key，不要用随机数",
      "title": "任务标题（中文，简短）",
      "goal": "这件事要达成的目标",
      "summary": "任务说明（做什么、为什么）",
      "basis": [
        {"cardId": "c_xxx", "role": "user_note|material|group|link", "version": "材料版本"}
      ],
      "expected": "预期结果（用户能看到什么）",
      "operations": [
        {"op": "add_reply_card", "ref": "结论", "content": "..."},
        {"op": "group_cards", "ref": "结果组", "cardIds": ["c_xxx", "card:结论"], "ordered": true},
        {"op": "order_group", "groupId": "group:结果组", "memberOrder": ["c_xxx", "card:结论"], "arrange": "row"}
      ],
      "impact": {"cardIds": ["受影响的卡片 id"], "scope": "影响范围的一句话说明"},
      "dependsOn": ["必须先完成的 intentKey"],
      "conflictsWith": ["互不相容的 intentKey"],
      "conflictKey": "同一冲突组共用的短标签；没有冲突时留空"
    }
  ],
  "memory": [
    {"text": "一句话，别抄整块板面", "kind": "goal|conclusion|basis|outcome"}
  ]
}

（kind=clarification 时必须给 clarification；kind=intent 时必须给非空 intents 数组；
其它判定必须省略不属于它的字段。）"""


def _allowed_ops_text() -> str:
    lines: list[str] = []
    for name in models.ALLOWED_INTENT_OPS:
        shape = OPERATION_SHAPES.get(name, {})
        required = ", ".join(shape.get("required", ()))
        optional = ", ".join(shape.get("optional", ()))
        params = f"必填：{required or '（无）'}"
        if optional:
            params += f"；可选：{optional}"
        lines.append(f"- {name}：{shape.get('summary', '')}｜{params}")
    return "\n".join(lines)


def system_prompt() -> str:
    """渲染系统提示（操作清单与 schema 都从唯一实现生成，不手抄）。"""
    return SYSTEM_PROMPT.format(ops=_allowed_ops_text(), schema=OUTPUT_SCHEMA_TEXT)


def render_context(context: dict) -> str:
    """把结构化上下文渲染成一段 JSON 文本。

    只渲染，不裁剪语义：截断与未读取范围由 build_context 记录在
    limits / honesty 里，模型看到的就是这些事实，不允许被抹平。
    """
    payload = {
        "contextVersion": context.get("contextVersion", 1),
        "boardId": context.get("boardId"),
        "submissionId": context.get("submissionId"),
        "requirement": context.get("requirement") or {},
        "relationships": context.get("relationships") or [],
        "changes": context.get("changes") or [],
        "focus": context.get("focus") or {},
        "geometry": context.get("geometry") or {},
        "cards": context.get("cards") or [],
        "groups": context.get("groups") or [],
        "materials": context.get("materials") or [],
        "visibleCardIds": context.get("visibleCardIds") or [],
        "visibleGroupIds": context.get("visibleGroupIds") or [],
        "materialIds": context.get("materialIds") or [],
        "limits": context.get("limits") or {},
        "honesty": context.get("honesty") or {},
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_CONTEXT_CHARS:
        # 渲染侧保险：build_context 已经按预算裁剪过，走到这里只可能是调用方直接塞了脏数据。
        text = text[:MAX_CONTEXT_CHARS]
    return text


def build_understanding_messages(context: dict, *, images: list | None = None) -> list[ChatMessage]:
    """构建一次理解调用的消息：一条 system + 一条承载结构化上下文的 user。

    上下文只作为**结构化数据**给出，不拆成多轮聊天，也不进入通用聊天循环。
    `images` 是本次真正交付的图片内容块（`ImagePart`）：它们挂在**同一条 user 消息**上，
    文本在前、图片在后；上下文 JSON 里已经写明每张图对应哪张卡片，模型据此引用。
    """
    return [
        ChatMessage(role="system", content=system_prompt()),
        ChatMessage(
            role="user",
            content=(
                "下面是本次提交的结构化上下文（JSON）。它不是聊天历史，"
                "只是事实来源；请按系统提示的判断规则输出单个 JSON 对象。\n"
                "=== 上下文开始 ===\n"
                + render_context(context)
                + "\n=== 上下文结束 ==="
            ),
            images=list(images) if images else None,
        ),
    ]


def build_repair_messages(
    context: dict, raw_output: str, reasons: list[str], *, images: list | None = None
) -> list[ChatMessage]:
    """构建**唯一一次**自动修复调用：把无效原因如实告诉模型，要求重出 JSON。

    修复调用仍然只给结构化上下文；不追加任何「必须生成任务」的暗示。
    """
    reason_text = "\n".join(f"- {r}" for r in reasons) or "- （未给出具体原因）"
    return [
        ChatMessage(role="system", content=system_prompt()),
        ChatMessage(
            role="user",
            content=(
                "下面是本次提交的结构化上下文（JSON）。它不是聊天历史，只是事实来源。\n"
                "=== 上下文开始 ===\n"
                + render_context(context)
                + "\n=== 上下文结束 ==="
            ),
            # 修复这一轮**同样**要带图：否则模型在「看不见图」的状态下重出 JSON，
            # 等于让它凭空修正一条依赖图片的判断。
            images=list(images) if images else None,
        ),
        ChatMessage(role="assistant", content=raw_output or ""),
        ChatMessage(
            role="user",
            content=(
                "你上一次的输出没有通过程序校验，原因如下：\n"
                + reason_text
                + "\n请重新输出**一个** JSON 对象（不要 Markdown 代码围栏）。"
                "如果你确实无法给出可执行意图，请如实输出 clarification 或 no_action。"
            ),
        ),
    ]
