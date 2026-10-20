"""Phase 1 A：统一消息组装（ContextBlock / build_initial_messages / loop 接线）。"""

from __future__ import annotations

import pytest

from agent import prompts
from agent.adapters.base import ImagePart
from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.core.message_assembly import (
    ContextBlock,
    MediaParts,
    answer_protocol_message,
    build_initial_messages,
)
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _Echo(Tool):
    name = "echo"
    description = "回显"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=str(kwargs.get("text") or "ok"))


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Echo())
    return registry


def test_build_initial_messages_shape_and_order():
    blocks = [
        ContextBlock("memory", {"items": [{"id": "m1", "text": "旧记忆"}]}),
        ContextBlock("attachments", {"note": "a.txt 可读"}),
    ]
    media = MediaParts(
        images=(
            ImagePart(
                data_url="data:image/png;base64,AAA",
                label="截图",
                media_type="image/png",
                bytes=3,
            ),
        )
    )
    messages = build_initial_messages(
        user_text="当前用户原文",
        media=media,
        mode_rules="【当前模式规则】只做文本判断。",
        blocks=blocks,
    )
    assert [m.role for m in messages] == ["system", "user"]
    system = messages[0].content or ""
    # 系统规则 / 模式规则 / 结构化上下文按固定顺序各出现一次
    assert prompts.PROMPTS["QIO_CORE"].splitlines()[0] in system
    assert "【当前模式规则】只做文本判断。" in system
    assert prompts.CONTEXT_LABELS["memory"] in system
    assert prompts.CONTEXT_LABELS["attachments"] in system
    assert prompts.context_heading("memory") in system
    # 当前用户消息正文只放用户原文，不拼上下文标题
    user = messages[1]
    assert user.content == "当前用户原文"
    assert user.images and user.images[0].label == "截图"
    assert prompts.CONTEXT_LABELS["memory"] not in (user.content or "")


def test_build_initial_messages_does_not_format_special_characters():
    special = '{name} $HOME ${PATH} `code` {"json": [1, 2]} %s'
    blocks = [ContextBlock("tool_feedback", {"raw": special, "obj": {"a": "{b}"}})]
    messages = build_initial_messages(
        user_text="用户内容里的 【用户消息】 不应变成标题",
        blocks=blocks,
    )
    system = messages[0].content or ""
    # 只序列化、不 format：危险字符原样保留在 JSON 值里
    assert "$HOME" in system
    assert "`code`" in system
    assert "%s" in system
    assert '{"a": "{b}"' in system
    # system 里没有「【用户消息】」这个拼接标题；它只出现在 user 原文里
    assert "【用户消息】" not in system
    assert "【用户消息】" in (messages[1].content or "")


def test_context_block_key_must_be_known():
    with pytest.raises(KeyError):
        build_initial_messages(
            user_text="x", blocks=[ContextBlock("not_a_real_key", {"a": 1})]
        )


def test_empty_blocks_are_skipped():
    messages = build_initial_messages(
        user_text="x",
        blocks=[
            ContextBlock("memory", None),
            ContextBlock("attachments", {}),
            ContextBlock("media", []),
        ],
    )
    system = messages[0].content or ""
    assert prompts.context_heading("memory") not in system
    assert prompts.context_heading("attachments") not in system


def test_answer_protocol_message_is_the_frozen_protocol():
    message = answer_protocol_message()
    assert message.role == "system"
    assert message.content == prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"]


async def test_loop_uses_assembled_initial_messages():
    adapter = FakeStreamAdapter([StreamScript(text="[[QIO:ANSWER]]\n完成")])
    loop = AgentLoop(adapter, _registry(), EventBus(), turn_id="turn_p1a")
    initial = build_initial_messages(
        user_text="原始用户消息",
        blocks=[ContextBlock("memory", {"legacy_text": "旧注入文本"})],
        mode_rules="",
    )
    result = await loop.run("这条不应生效", initial_messages=initial)

    assert result.final_content == "完成"
    first = adapter.requests[0]["messages"]
    assert first[0].role == "system"
    assert prompts.PROMPTS["QIO_CORE"].splitlines()[0] in (first[0].content or "")
    assert prompts.CONTEXT_LABELS["memory"] in (first[0].content or "")
    assert first[1].role == "user"
    assert first[1].content == "原始用户消息"
    assert "这条不应生效" not in (first[0].content or "")


def test_context_helpers_derive_from_blocks():
    from agent.services.turn_orchestrator import (
        _context_limits,
        _knowledge_snapshot_from_blocks,
    )

    blocks = [
        ContextBlock(
            "knowledge",
            {"entries": [{"id": "k1", "content": "知识一"}, {"item_id": "k2", "text": "知识二"}]},
        ),
        ContextBlock(
            "limits", {"hard_cap": 900, "used_tokens": 120, "dropped": ["x"]}
        ),
    ]
    assert _knowledge_snapshot_from_blocks(blocks) == [
        {"item_id": "k1", "content": "知识一"},
        {"item_id": "k2", "content": "知识二"},
    ]
    limits = _context_limits(blocks)
    assert limits["hard_cap"] == 900
    assert limits["used_tokens"] == 120
    assert limits["dropped"] == ["x"]
    assert _context_limits([]) == {}


async def test_assemble_context_uses_b_final_signature():
    """与 B 的 build_context_blocks 最终签名对齐：只传它接收的参数。"""
    from agent.services.turn_orchestrator import TurnOrchestrator

    class _Heavy:
        async def run(self, fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return result

    class _Assembler:
        last_needs_consolidation = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def build_context_blocks(self, query, **kwargs):
            self.calls.append((query, dict(kwargs)))
            return [
                ContextBlock(
                    "knowledge", {"entries": [{"id": "k1", "content": "知识"}]}
                )
            ]

    class _Registry:
        def specs(self):
            return []

    class _App:
        def __init__(self, assembler) -> None:
            self.context_assembler = assembler
            self.registry = _Registry()
            self.heavy = _Heavy()

        def _topic_entity_ids(self, topic):
            return ["ent1"]

        def _user_root_id(self):
            return "user1"

        def build_injection(self, *args, **kwargs):  # pragma: no cover - 不应走到
            raise AssertionError("B 的 build_context_blocks 存在时不应走旧桥")

    class _Ctx:
        bound_fragment_id = "frag1"
        user_message_id = "msg9"

    assembler = _Assembler()
    app = _App(assembler)
    orchestrator = TurnOrchestrator(app)
    adapter = FakeStreamAdapter([], model="fake-model")

    blocks, plan, needs = await orchestrator._assemble_context(
        app,
        adapter,
        _Ctx(),
        message="hi",
        topic="t1",
        aux_topic_ids=["t2"],
        fragment_id="frag1",
        focus_fragment_id="focus1",
        exclude_message_id="msg9",
        new_topic_candidate=True,
        reason="相似度低",
        short_term=[],
        topic_note="",
        focus_block="",
        focus_item_id=None,
        entity_cards=[],
    )

    assert plan is None
    assert needs is True
    assert blocks and blocks[0].key == "knowledge"
    query, kwargs = assembler.calls[0]
    assert query == "hi"
    allowed = {
        "topic_id",
        "aux_topic_ids",
        "entity_ids",
        "user_node_id",
        "model",
        "fragment_id",
        "focus_fragment_id",
        "exclude_message_id",
        "new_topic_candidate",
        "new_topic_reason",
        "system_prompt_tokens",
        "adapter_overhead_tokens",
        "tool_definitions_tokens",
    }
    assert set(kwargs) == allowed
    assert kwargs["model"] == "fake-model"
    assert kwargs["fragment_id"] == "frag1"
    assert kwargs["focus_fragment_id"] == "focus1"
    assert kwargs["exclude_message_id"] == "msg9"
    assert kwargs["topic_id"] == "t1"
    assert kwargs["aux_topic_ids"] == ["t2"]
    assert kwargs["entity_ids"] == ["ent1"]
    assert kwargs["user_node_id"] == "user1"
    assert kwargs["system_prompt_tokens"] > 0
    # B 不接收的旧参数不得出现
    assert "short_term" not in kwargs
    assert "entity_cards" not in kwargs
    assert "topic_note" not in kwargs


async def test_loop_old_call_shape_still_works():
    adapter = FakeStreamAdapter([StreamScript(text="[[QIO:ANSWER]]\n老调用")])
    loop = AgentLoop(adapter, _registry(), EventBus(), turn_id="turn_p1a_old")
    result = await loop.run("老调用用户原文")
    assert result.final_content == "老调用"
    first = adapter.requests[0]["messages"]
    # 旧调用没有组装 system 规则：只有 user 原文 + 每次调用的回答协议提示
    assert first[0].role == "user"
    assert first[0].content == "老调用用户原文"
    assert first[-1].role == "system"
    assert first[-1].content == prompts.answer_protocol()
