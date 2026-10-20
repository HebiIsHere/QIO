# -*- coding: utf-8 -*-
"""D 独立契约测试：主对话真实请求形状 + 三档适配器一致性（契约 §3）。

链路：AppContext.run_turn（真实服务层 + fake provider）→ adapter 收到的
messages 就是「模型最终看到的形状」。断言按契约 §3 的固定顺序与边界：
用户原文单独一条消息、上下文是带固定标签的 JSON 数据块、回答协议只出现一次。

口径：全部 fake provider，不联网、不读密钥。阶段 1（A/B 的实现未合并）允许失败。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_turn_assembly_contract.py -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent import prompts
from agent.adapters.base import ChatMessage
from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.adapters.text import TextAdapter
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"
USER_HEADING = "【用户消息】"


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "turn.db")
    apply_migrations(conn)
    application = AppContext(Settings(data_dir=tmp_path / "data"), conn, EventBus())
    application.credentials._kr = MemoryKeyring()
    return application


def _seed_topic_history(ctx: AppContext, topic: str, rounds: int = 3) -> None:
    for index in range(rounds):
        ctx.memory.append_message(
            topic_id=topic, role="user", content=f"历史用户消息 {index}：HISTORY_USER_{index}"
        )
        ctx.memory.append_message(
            topic_id=topic,
            role="assistant",
            content=f"历史助手回答 {index}：HISTORY_ASSISTANT_{index}",
        )


async def _run_one_turn(ctx: AppContext, message: str) -> tuple[FakeStreamAdapter, str]:
    topic = ctx.topics.nodes.create_topic("消息组装契约话题").id
    _seed_topic_history(ctx, topic)
    adapter = FakeStreamAdapter([StreamScript(text=ANSWER + "\n收到，已处理。")])
    ctx.build_adapter = _fake_build_adapter(adapter)
    await ctx.run_turn(message, topic_id=topic)
    assert adapter.requests, "装置失效：adapter 没有收到任何请求"
    return adapter, topic


def _fake_build_adapter(adapter):
    async def _build(*args, **kwargs):
        return adapter

    return _build


def _all_contents(adapter: FakeStreamAdapter, request_index: int = 0) -> list[ChatMessage]:
    return list(adapter.requests[request_index]["messages"])


async def test_user_message_stays_verbatim_and_separate(ctx: AppContext):
    """§3.6：当前用户原文单独一条 user 消息；不得用【用户消息】标题拼接上下文。"""
    raw = "本轮用户原文-UNIQUE-a7c：含 {花括号} 与 100% 与【伪标题】"
    adapter, _ = await _run_one_turn(ctx, raw)
    messages = _all_contents(adapter)
    user_messages = [m for m in messages if m.role == "user"]
    assert len(user_messages) == 1, (
        f"契约 §3.6：首个请求应恰好一条当前用户消息；实际={[m.content for m in user_messages]}"
    )
    assert user_messages[0].content == raw, (
        "契约 §3：当前用户消息被注入上下文/标题改写；"
        f"实际开头={str(user_messages[0].content)[:160]!r}"
    )
    for message in messages:
        assert USER_HEADING not in (message.content or ""), (
            "契约 §3：仍在使用【用户消息】标题把上下文与用户原文拼成一条"
        )


async def test_context_is_labeled_json_data_in_system_messages(ctx: AppContext):
    """§3.4：结构化记忆/材料进入 system 侧，标题只能来自 CONTEXT_LABELS，data 必须是 JSON。"""
    raw = "请结合历史回答-UNIQUE-b31"
    adapter, _ = await _run_one_turn(ctx, raw)
    messages = _all_contents(adapter)
    protocol = prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"]
    # 回答协议 / 补救提示是独立固定消息（不属于 CONTEXT_LABELS 的上下文块体系）：
    # 扫描上下文标题时排除它们，本文件的协议断言在 test_system_rules_first_... 单独做。
    def _is_dedicated_fixed_message(text: str) -> bool:
        return text.startswith("【回答协议】") or text.startswith("【最终回答】")

    system_texts = [
        m.content or ""
        for m in messages
        if m.role == "system" and not _is_dedicated_fixed_message(m.content or "")
    ]
    assert system_texts, (
        "契约 §3.1：主对话请求缺少 system 规则/上下文消息（基线把规则拼进了 user 消息）"
    )
    joined = "\n".join(system_texts)
    allowed = set(prompts.CONTEXT_LABELS.values())
    illegal = sorted(
        heading
        for heading in set(re.findall(r"【([^】\n]{1,40})】", joined))
        if heading not in allowed
    )
    assert not illegal, (
        "契约 §3：system 中出现非 CONTEXT_LABELS 的固定标题（自造中文上下文标题）："
        f"{illegal}"
    )
    # 至少一个冻结 key 的上下文块真的出现，且其 data 是可解析 JSON、包含已注入的历史内容。
    found = {}
    for key in ("short_term", "memory", "history_focus", "current_topic"):
        heading = "【" + prompts.CONTEXT_LABELS[key] + "】"
        if heading in joined:
            tail = joined.split(heading, 1)[1].lstrip()
            try:
                found[key] = json.JSONDecoder().raw_decode(tail)[0]
            except json.JSONDecodeError:
                continue
    assert found, (
        "契约 §3.4：结构化上下文块没有以 CONTEXT_LABELS 标签 + json.dumps 数据的形式进入 system"
    )
    payload_text = json.dumps(found, ensure_ascii=False)
    assert "HISTORY_USER_0" in payload_text or "历史用户消息 0" in payload_text, (
        "契约 §3.4：已注入的短期/历史上下文没有作为结构化数据进入上下文块"
    )


async def test_system_rules_first_and_answer_protocol_last_once(ctx: AppContext):
    """§3.1/§3.3：系统规则在最前；回答协议作为每次调用的最后一条 system 消息且只注入一次。"""
    adapter, _ = await _run_one_turn(ctx, "协议注入检查-UNIQUE-c42")
    protocol = prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"]
    for request_index, request in enumerate(adapter.requests):
        messages = list(request["messages"])
        system_messages = [m for m in messages if m.role == "system"]
        assert system_messages, (
            f"契约 §3.1：第 {request_index + 1} 次模型调用没有 system 规则消息"
        )
        first_system = system_messages[0]
        assert prompts.PROMPTS["QIO_CORE"] in (first_system.content or ""), (
            "契约 §3.1：第一条 system 消息必须包含 QIO_CORE 系统规则；"
            f"实际开头={str(first_system.content)[:160]!r}"
        )
        assert messages[-1].role == "system" and messages[-1].content == protocol, (
            "契约 §3.3：native 档回答协议必须是本次调用的最后一条 system 消息，"
            f"且内容等于 PROMPTS['CONTENT_ROLE_PROTOCOL']；实际最后一条="
            f"{messages[-1].role}:{str(messages[-1].content)[:80]!r}"
        )
        occurrences = sum((m.content or "").count(protocol) for m in messages)
        assert occurrences == 1, (
            f"契约 §3.3：回答协议在第 {request_index + 1} 次调用出现 {occurrences} 次（应恰好 1 次）"
        )


# ---------------------------------------------------------------------------
# text 兼容档：同一份固定文案，工具目录只出现一次（§3.3）
# ---------------------------------------------------------------------------


def _text_adapter() -> TextAdapter:
    return TextAdapter(client=None, model="fake-text", endpoint=None)


def test_text_adapter_system_prompt_uses_frozen_prompts_only():
    """§2.1/§3.3：text 档固定文案来自 prompts.py（旧副本必须消失），协议只出现一次。"""
    from agent.adapters.base import ToolSpec

    tools = [
        ToolSpec(
            name="probe_tool",
            description="测试工具：含 {花括号} 与 100% 与 %(name)s",
            parameters={
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "含 {json} 与 %s 的说明"}
                },
                "required": ["q"],
            },
        )
    ]
    system = _text_adapter().build_system_prompt(tools)
    for key in ("TEXT_MODE_PROTOCOL", "CONTENT_ROLE_PROTOCOL", "TEXT_OBSERVATION_RULES", "AVAILABLE_TOOLS_HEADER"):
        assert prompts.PROMPTS[key] in system, (
            f"契约 §2.1/§3.3：text 档 system prompt 缺少 PROMPTS[{key!r}]（固定文案唯一来源）"
        )
    assert system.count(prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"]) == 1, (
        "契约 §3.3：回答协议在三档都只能出现一次（text 档重复注入）"
    )
    # 旧的另一份固定文案（冻结前 text.py 自带的那段）必须已被移除。
    old_text_mode = (
        "You are a tool-calling assistant in text mode, where tool calls are expressed as JSON."
    )
    assert old_text_mode not in system, (
        "契约 §2.1：text 档仍在用旧的 SYSTEM_PROMPT_TEXT_MODE 副本（应改为 prompts.py 的唯一来源）"
    )
    # 工具目录：名称/描述只出现一次，参数 schema 是完整 JSON。
    assert system.count("probe_tool") == 1, "契约 §3.3：text 档工具定义应只出现一次"
    marker = "arguments (JSON Schema): "
    assert marker in system, "契约 §3.3：text 档工具目录必须带 JSON Schema"
    parsed = json.JSONDecoder().raw_decode(system.split(marker, 1)[1].lstrip())[0]
    assert parsed == tools[0].parameters, (
        "契约 §2.2/§3.3：text 档工具参数 schema 被改写/截断"
    )


def test_text_and_native_see_same_frozen_protocol_text():
    """§3.3：三档适配器看到的回答协议是同一份措辞（同一常量）。"""
    system = _text_adapter().build_system_prompt([])
    assert prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"] in system
    # native 档由 loop 注入的内容在 test_system_rules_first_and_answer_protocol_last_once 覆盖；
    # 这里确认两档引用的都是同一个冻结常量，不是各自写一份。
    assert system.count(prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"]) == 1

async def test_fallback_call_keeps_general_rules_and_adds_frozen_hint(ctx: AppContext):
    """§3：补救调用（每轮最多一次）保留通用规则，再追加 prompts.answer_fallback_hint()。"""
    topic = ctx.topics.nodes.create_topic("补救调用契约话题").id
    adapter = FakeStreamAdapter(
        [StreamScript(text=""), StreamScript(text=ANSWER + "\n兜底回答")]
    )

    async def _build(*args, **kwargs):
        return adapter

    ctx.build_adapter = _build
    await ctx.run_turn("请给一个最终回答-UNIQUE-f01", topic_id=topic)
    assert len(adapter.requests) == 2, (
        "装置失效：整轮没有回答内容时应恰好触发一次补救调用；实际请求数="
        f"{len(adapter.requests)}"
    )
    fallback = adapter.requests[1]
    assert fallback["tools"] == [], "契约 §3：补救调用不提供任何工具（tools=[]）"
    system_text = "\n".join(
        str(m.content or "") for m in fallback["messages"] if m.role == "system"
    )
    assert prompts.PROMPTS["QIO_CORE"] in system_text, (
        "契约 §3：补救调用必须保留通用系统规则（不得用 hint 取代整个 system）"
    )
    assert prompts.PROMPTS["ANSWER_FALLBACK_HINT"] in system_text, (
        "契约 §3：补救调用必须追加 PROMPTS['ANSWER_FALLBACK_HINT']"
    )
    assert prompts.PROMPTS["ANSWER_FALLBACK_HINT"] not in (
        "\n".join(str(m.content or "") for m in adapter.requests[0]["messages"])
    ), "契约 §3：补救提示不应出现在第一次（正常）调用里"
