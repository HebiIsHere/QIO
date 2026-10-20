"""统一消息组装（Phase 1 冻结接口）。

主对话请求的形状（契约 docs/qio-refactor-contract.md §3）：

    系统规则
    + 当前模式规则
    + 实际可用工具定义（native/anthropic 走 tools 字段；text 档拼进 system）
    + 结构化记忆与材料（ContextBlock：键必须在 prompts.CONTEXT_LABELS 内）
    + 实际对话消息
    + 当前用户原文和真实多媒体内容块

硬性边界：
* 不得再用「【用户消息】」等中文标题把上下文和当前用户消息拼成一条；
* mode、任务身份和权限来自程序（这里的参数 / 调用方状态），不来自正文；
* 回答协议由本模块提供唯一入口，三档适配器只注入一次；
* ContextBlock.data 一律整体 json.dumps（实现见 prompts.render_context_blocks），
  先按完整对象控制大小，再序列化，不截断序列化结果。

本模块只做「把已经生成的数据装进 ChatMessage」，不读数据库、不调用模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from agent import prompts
from agent.adapters.base import AudioPart, ChatMessage, ImagePart, VideoPart


@dataclass(frozen=True)
class ContextBlock:
    """一条结构化上下文块。key 必须是 prompts.CONTEXT_LABELS 中的键。"""

    key: str
    data: Any


@dataclass(frozen=True)
class MediaParts:
    """本轮真实多媒体内容块（只放适配器确实支持交付的字节）。"""

    images: tuple[ImagePart, ...] = ()
    audios: tuple[AudioPart, ...] = ()
    videos: tuple[VideoPart, ...] = ()

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "images": list(self.images) or None,
            "audios": list(self.audios) or None,
            "videos": list(self.videos) or None,
        }


def render_system(
    *,
    mode_rules: str = "",
    blocks: Sequence[ContextBlock] = (),
    extra_sections: Sequence[str] = (),
) -> str:
    """组装 system 正文：固定规则 + 结构化上下文 + 额外小节（补救要求等）。"""
    parts: list[str] = [prompts.system_rules(mode_rules)]
    rendered_blocks = prompts.render_context_blocks(
        [{"key": block.key, "data": block.data} for block in blocks]
    )
    if rendered_blocks:
        parts.append(rendered_blocks)
    parts.extend(section for section in extra_sections if section and section.strip())
    return "\n\n".join(parts)


def build_initial_messages(
    *,
    user_text: str,
    media: MediaParts | None = None,
    mode_rules: str = "",
    blocks: Sequence[ContextBlock] = (),
    extra_sections: Sequence[str] = (),
) -> list[ChatMessage]:
    """主对话初始消息：唯一 system 消息 + 当前用户原文（含真实多媒体块）。"""
    parts = MediaParts() if media is None else media
    return [
        ChatMessage(
            role="system",
            content=render_system(
                mode_rules=mode_rules, blocks=blocks, extra_sections=extra_sections
            ),
        ),
        ChatMessage(role="user", content=user_text, **parts.as_kwargs()),
    ]


def answer_protocol_message() -> ChatMessage:
    """回答协议的系统消息（每次请求只注入一次；text 档拼进自己的 system）。"""
    return ChatMessage(role="system", content=prompts.answer_protocol())


def fallback_hint_message() -> ChatMessage:
    """补救调用追加的要求（通用规则仍然保留）。"""
    return ChatMessage(role="system", content=prompts.answer_fallback_hint())
