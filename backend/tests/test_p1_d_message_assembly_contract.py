# -*- coding: utf-8 -*-
"""D 独立契约测试：统一消息组装（docs/qio-refactor-contract.md §3）。

口径：只按契约措辞断言，不从实现反推。阶段 1（A 的实现未合并）允许失败；
失败消息写明契约条款与期望。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_message_assembly_contract.py -q
"""

from __future__ import annotations

import json

import pytest

from agent import prompts
from agent.adapters.base import AudioPart, ChatMessage, ImagePart, VideoPart
from agent.core import message_assembly as ma

# 契约 §3 硬性边界：不得再用中文标题把注入上下文与当前用户消息拼成一条。
FORBIDDEN_USER_HEADINGS = ("【用户消息】", "用户消息】", "【用户消息")


def _plain_user_text() -> str:
    return (
        "帮我核对季度预算：含 {花括号}、100%、%(name)s、"
        "反斜杠\\、换行\n与【伪标题】。原文必须一字不改。"
    )


def test_answer_protocol_and_fallback_messages_use_frozen_source():
    """§3：回答协议 / 补救提示的唯一入口，文本来自 prompts.py（三档一致）。"""
    protocol = ma.answer_protocol_message()
    assert isinstance(protocol, ChatMessage)
    assert protocol.role == "system"
    assert protocol.content == prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"], (
        "契约 §3：回答协议消息内容必须等于 PROMPTS['CONTENT_ROLE_PROTOCOL']"
    )
    fallback = ma.fallback_hint_message()
    assert fallback.role == "system"
    assert fallback.content == prompts.PROMPTS["ANSWER_FALLBACK_HINT"], (
        "契约 §3：补救提示消息内容必须等于 PROMPTS['ANSWER_FALLBACK_HINT']"
    )


def test_build_initial_messages_keeps_current_user_text_verbatim():
    """§3.6：当前用户原文单独一条 user 消息，不得与注入上下文拼成一条。"""
    raw = _plain_user_text()
    messages = ma.build_initial_messages(user_text=raw, mode_rules="模式规则-SENTINEL")
    assert len(messages) == 2, (
        f"契约 §3：无上下文/无媒体时初始消息应恰好是 [system, user]；实际={len(messages)}"
    )
    system_msg, user_msg = messages
    assert system_msg.role == "system"
    assert user_msg.role == "user"
    assert user_msg.content == raw, (
        "契约 §3.6：user 消息内容必须是用户原文本身，不得拼接上下文/标题；"
        f"实际开头={str(user_msg.content)[:120]!r}"
    )
    for heading in FORBIDDEN_USER_HEADINGS:
        assert heading not in (system_msg.content or ""), f"契约 §3：system 中不得出现 {heading}"
        assert heading not in (user_msg.content or ""), f"契约 §3：user 中不得出现 {heading}"
    assert prompts.PROMPTS["QIO_CORE"] in system_msg.content, (
        "契约 §3.1：system 消息必须包含 QIO_CORE 系统规则"
    )
    assert "模式规则-SENTINEL" in system_msg.content, "契约 §3.2：当前模式规则必须进入 system"


def test_context_blocks_render_as_labeled_json_data_not_user_text():
    """§3.4：结构化记忆/材料是带固定标签的 JSON 数据块，与用户原文分开。"""
    raw = "本轮用户原文-UNIQUE-8f3a"
    data = {
        "记忆条目": [{"title": "预算会", "summary": "含 {括号} 与 100%"}],
        "来源": "memory",
    }
    messages = ma.build_initial_messages(
        user_text=raw,
        blocks=[ma.ContextBlock(key="memory", data=data)],
    )
    system_text = messages[0].content or ""
    user_text = messages[1].content or ""
    assert user_text == raw, "契约 §3：注入上下文不得改变 user 消息正文"
    heading = "【" + prompts.CONTEXT_LABELS["memory"] + "】"
    assert heading in system_text, (
        "契约 §3.4：上下文块标题必须来自 prompts.CONTEXT_LABELS"
    )
    parsed = json.JSONDecoder().raw_decode(system_text.split(heading, 1)[1].lstrip())[0]
    assert parsed == data, (
        "契约 §2.2/§3.4：data 必须整体 json.dumps 进入 system，且不得被 format/截断"
    )
    assert json.dumps(raw, ensure_ascii=False) not in system_text, (
        "契约 §3：用户原文不应同时被塞进 system 数据块"
    )


def test_system_label_headings_only_come_from_context_labels():
    """§3：固定上下文标签唯一来源 = prompts.CONTEXT_LABELS（不得自造中文标题）。"""
    messages = ma.build_initial_messages(
        user_text="原样消息",
        blocks=[
            ma.ContextBlock(key="current_topic", data={"topic_id": "t1"}),
            ma.ContextBlock(key="short_term", data={"messages": ["a", "b"]}),
            ma.ContextBlock(key="limits", data={"max": 1}),
        ],
    )
    system_text = system_msg = messages[0].content or ""
    import re

    headings = set(re.findall(r"【([^】\n]{1,40})】", system_text))
    allowed = set(prompts.CONTEXT_LABELS.values())
    illegal = sorted(h for h in headings if h not in allowed)
    assert not illegal, (
        "契约 §3：system 中出现非 CONTEXT_LABELS 的固定标题（自造另一套上下文标签）："
        f"{illegal}"
    )
    assert system_msg, "装置失效"


def test_media_parts_are_real_content_blocks_not_text():
    """§3.6：真实多媒体内容块挂在 user 消息上，不拼进文本。"""
    image = ImagePart(data_url="data:image/png;base64,AAA", label="图1", media_type="image/png", bytes=3)
    audio = AudioPart(data_url="data:audio/wav;base64,BBB", label="音1", media_type="audio/wav", bytes=3)
    video = VideoPart(data_url="data:video/mp4;base64,CCC", label="视1", media_type="video/mp4", bytes=3)
    raw = "请看这些材料"
    messages = ma.build_initial_messages(
        user_text=raw,
        media=ma.MediaParts(images=(image,), audios=(audio,), videos=(video,)),
    )
    system_text = messages[0].content or ""
    user_msg = messages[1]
    assert user_msg.content == raw, "契约 §3.6：媒体不能改写用户原文"
    assert user_msg.images == [image], "契约 §3.6：图片必须是真实内容块（ImagePart）"
    assert user_msg.audios == [audio], "契约 §3.6：音频必须是真实内容块（AudioPart）"
    assert user_msg.videos == [video], "契约 §3.6：视频必须是真实内容块（VideoPart）"
    for part in (image, audio, video):
        assert part.data_url not in system_text, (
            "契约 §3.6：媒体字节不得退化成文本塞进 system"
        )


def test_render_system_order_and_single_occurrence():
    """§3 顺序：系统规则 → 上下文块 → 额外小节；每部分只出现一次。"""
    block_heading = "【" + prompts.CONTEXT_LABELS["knowledge"] + "】"
    extra = "EXTRA_SECTION_FALLBACK_SENTINEL"
    text = ma.render_system(
        mode_rules="MODE_SENTINEL",
        blocks=[ma.ContextBlock(key="knowledge", data={"items": ["k1"]})],
        extra_sections=["", extra, "   "],
    )
    core_at = text.index(prompts.PROMPTS["QIO_CORE"])
    block_at = text.index(block_heading)
    extra_at = text.index(extra)
    assert core_at < block_at < extra_at, (
        "契约 §3：组装顺序必须是 系统规则 → 结构化上下文 → 额外小节"
    )
    assert text.count("MODE_SENTINEL") == 1
    assert text.count(block_heading) == 1
    assert text.count(extra) == 1
    assert "   " not in text.split(extra)[0][-5:], "空/空白小节应被跳过"


def test_unknown_block_key_is_rejected():
    """§3：key 必须来自 CONTEXT_LABELS；未知 key 不许静默渲染。"""
    with pytest.raises(KeyError):
        ma.build_initial_messages(
            user_text="x",
            blocks=[ma.ContextBlock(key="self_invented_block", data={"a": 1})],
        )


def test_serialized_context_is_not_truncated():
    """§2.2：先按完整对象控制大小，再序列化；不得截断序列化后的 JSON。"""
    payload = {"docs": ["材料" * 5000]}
    messages = ma.build_initial_messages(
        user_text="查材料",
        blocks=[ma.ContextBlock(key="attachments", data=payload)],
    )
    system_text = messages[0].content or ""
    heading = "【" + prompts.CONTEXT_LABELS["attachments"] + "】"
    parsed = json.JSONDecoder().raw_decode(system_text.split(heading, 1)[1].lstrip())[0]
    assert parsed == payload, "契约 §2.2：上下文 JSON 被截断或改写"


def test_user_text_is_not_used_as_format_template():
    """§2.2：不得对含用户内容/JSON 的整段文本调用 format / 百分号替换 / 递归 replace。"""
    raw = "100% 确定：{answer} 与 %(x)s 与 {0}"
    messages = ma.build_initial_messages(user_text=raw, blocks=[ma.ContextBlock(key="memory", data={"raw": raw})])
    assert messages[1].content == raw, "契约 §2.2：用户原文被当模板处理了"
    system_text = messages[0].content or ""
    assert raw in system_text, "契约 §2.2：data 中的用户内容必须原样保留（json.dumps 后）"
