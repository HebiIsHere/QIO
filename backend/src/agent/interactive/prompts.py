"""互动理解的提示词与输出契约（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §14.0 / §14.1 / §14.3 / §14.5。

这个模块只做两件事：

1. 讲清楚 QIO 在互动模式里**能做什么**（= 用户在互动模式中能做的子集）、
   **必须怎么判断**（intent / clarification / no_action 三选一，不强制每次提交都产任务）；
2. 把**结构化上下文**渲染成一次模型调用需要的消息。

提示词正文的**唯一来源**是 `agent/prompts.py`（Phase 1 契约 §2）：
本模块只保留程序侧的结构定义（OPERATION_SHAPES / MEMORY_KINDS 等）与薄组装，
不再保存任何一份提示词正文。系统提示模板与输出 schema 通过
`prompts.interactive_system_prompt()` / `prompts.INTERACTIVE_OUTPUT_SCHEMA` 引用。

阶段三退役说明（docs/qio-refactor-contract.md §13.3）：

* 本模块与 understanding.py 构成**旧独立理解链**：互动提交不再调用它，新流程把提交
  作为主循环的一个 Turn 受理，由板面工具在**同一个主循环**里提出意图 / 提问 / 执行。
* 这里不再保留「禁止接入通用主循环 / 必须一次 JSON 理解」这类旧约束；本模块保留
  只为旧 board_understanding 记录的只读兼容、既有导入路径与旧重试入口，不再作为
  新流程的一部分，也不产生新记录。

旧链的程序侧边界（保留说明，供读旧记录时理解）：

* 板面状态与材料曾是**结构化上下文**，一次 user 消息承载整份上下文（JSON）。
* 输出必须是**单个 JSON 对象**，不带 Markdown 代码围栏、不带解释性前后缀。
* 只允许 models.ALLOWED_INTENT_OPS 里的操作；模型不能直接写库、不能绕过审批。
* 结果卡片的正文**允许用 Markdown 组织**（标题、列表、表格、代码块等）：程序负责解析与
  安全渲染；正文始终保存 Markdown 原文。

注意：本模块不调用模型，也不读写数据库。
"""

from __future__ import annotations

import json
from typing import Any

from agent import prompts
from agent.adapters.base import ChatMessage
from agent.interactive import models

#: 一次理解请求允许的上下文上限（字符数）。超出由 understanding.build_context 记录截断，
#: 这里只作为渲染侧的最后一道保险。
MAX_CONTEXT_CHARS = 20000

#: 允许的板面操作及其参数形状（契约 §14.5 冻结）。
#: 程序校验（understanding.validate_output）与此共用同一份定义，避免提示词与校验漂移。
#: 自然语言 summary 会由 system_prompt() 渲染进提示词。
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

#: 旧符号兼容：正文不再在这里维护，只指向 prompts.py 的唯一来源。
SYSTEM_PROMPT = prompts.INTERACTIVE_UNDERSTANDING_SYSTEM
OUTPUT_SCHEMA_TEXT = prompts.INTERACTIVE_OUTPUT_SCHEMA


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
    return prompts.interactive_system_prompt(_allowed_ops_text(), OUTPUT_SCHEMA_TEXT)


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


def _context_user_text(context: dict, *, notice: str) -> str:
    """一次理解调用的 user 消息：固定说明 + 上下文 JSON 数据 + 结束标记。"""
    return (
        notice
        + "\n"
        + prompts.INTERACTIVE_CONTEXT_START
        + "\n"
        + render_context(context)
        + "\n"
        + prompts.INTERACTIVE_CONTEXT_END
    )


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
            content=_context_user_text(context, notice=prompts.INTERACTIVE_CONTEXT_NOTICE),
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
            content=_context_user_text(
                context, notice=prompts.INTERACTIVE_REPAIR_CONTEXT_NOTICE
            ),
            # 修复这一轮**同样**要带图：否则模型在「看不见图」的状态下重出 JSON，
            # 等于让它凭空修正一条依赖图片的判断。
            images=list(images) if images else None,
        ),
        ChatMessage(role="assistant", content=raw_output or ""),
        ChatMessage(
            role="user",
            content=prompts.INTERACTIVE_REPAIR_INSTRUCTION.format(reasons=reason_text),
        ),
    ]
