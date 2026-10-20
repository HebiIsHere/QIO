"""Phase 1 负责人接线：continue_from_fragment 当轮即可获得相关上下文。

覆盖点：

* 登记成功的同一轮，工具结果里真实带出片段摘要与头尾原文（模型当轮可用）；
* 结构化 data 与正文一致、来源字段完整；
* 不改变当前位置、不创建接续片段、不影响已排队的导航意图；
* 大片段按完整条目收敛（omitted_messages 如实、块大小受 FocusPolicy.max_tokens 约束）；
* 话题工具的 description 来自 prompts 单一来源。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.memory.index import estimate_tokens
from agent.services.app import AppContext
from agent.services.params import FOCUS
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.continue_tool import ContinueFromFragmentTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _seed_fragment(
    ctx: AppContext,
    topic_id: str,
    *,
    title: str = "历史片段的标题",
    summary: str = "SUMMARY-CONTEXT-XYZ：这一段讨论的是接续上下文必须当轮可用",
    messages: list[tuple[str, str]] | None = None,
) -> str:
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (frag_id, topic_id, summary, now, now),
    )
    rows = messages or [
        ("user", "MESSAGE-HEAD-XYZ 开头：我们为什么讨论接续"),
        ("assistant", "MESSAGE-MIDDLE-XYZ 中间：已经把历史位置和检索分开"),
        ("user", "MESSAGE-TAIL-XYZ 结尾：用户最后要求从这段继续"),
    ]
    ids: list[str] = []
    for index, (role, text) in enumerate(rows):
        message_id = f"{frag_id}_m{index}"
        ids.append(message_id)
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?, 'text', ?)",
            (message_id, frag_id, role, text, now),
        )
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (ids[0], ids[-1], frag_id),
    )
    ctx.conn.execute(
        "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
        "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
        (new_id("idx"), frag_id, topic_id, title, now),
    )
    ctx.conn.commit()
    ctx._refresh_selector()
    return frag_id


def test_continue_tool_returns_focus_context_in_the_same_turn(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag = _seed_fragment(ctx, b)
    AnchorService(ctx.conn).set_active(a)

    result = ContinueFromFragmentTool(ctx.conn).run_sync(fragment_id=frag, reason="从这里继续")

    assert result.ok is True
    # 同一轮即可用：摘要与真实原文都出现在工具结果正文里
    assert "SUMMARY-CONTEXT-XYZ" in result.content
    assert "MESSAGE-HEAD-XYZ" in result.content
    assert "MESSAGE-TAIL-XYZ" in result.content
    assert "history_focus" not in result.content  # 正文是数据（标题由程序标签渲染）
    # 结构化字段与正文同源
    assert result.protocol_version == 1
    assert result.status == "completed"
    assert result.code in {"continuation_registered", "continuation_already_current"}
    data = result.data or {}
    assert data["fragment_id"] == frag
    assert data["topic_id"] == b
    context = data["context"]
    assert context["summary"].startswith("SUMMARY-CONTEXT-XYZ")
    roles = [item["role"] for item in context["head"] + context["tail"]]
    assert "user" in roles
    # 位置与排队意图语义不变：登记只对之后提交的消息生效
    assert AnchorService(ctx.conn).get_active().topic_id == a
    assert ctx.bindings.peek_intent().source_fragment_id == frag
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1


def test_continue_tool_context_is_bounded_and_reports_omitted(ctx: AppContext):
    topic = _topic(ctx, "长片段")
    messages = [("user" if i % 2 == 0 else "assistant", f"LONG-{i:03d} " + "内容" * 50) for i in range(80)]
    frag = _seed_fragment(ctx, topic, messages=messages)

    result = ContinueFromFragmentTool(ctx.conn).run_sync(fragment_id=frag)

    assert result.ok is True
    context = (result.data or {})["context"]
    # 完整条目收敛：头尾都保留、被省略的数量如实
    assert context["head"] and context["tail"]
    assert context["omitted_messages"] > 0
    assert context["head"][0]["content"].startswith("LONG-000")
    assert len(context["head"]) + len(context["tail"]) <= FOCUS.head_messages + FOCUS.tail_messages
    assert estimate_tokens(json.dumps(context, ensure_ascii=False)) <= FOCUS.max_tokens


def test_topic_tool_descriptions_come_from_prompts_single_source(ctx: AppContext):
    from agent.prompts import TOOL_DESCRIPTIONS
    from agent.tools.topic_tools import CreateTopicTool, SwitchTopicTool

    assert SwitchTopicTool(ctx.conn).description == TOOL_DESCRIPTIONS["switch_topic"]
    assert CreateTopicTool(ctx.conn).description == TOOL_DESCRIPTIONS["create_topic"]
    # 参数说明已迁到 prompts.py；工具里只留真实类型与约束
    for tool in (SwitchTopicTool(ctx.conn), CreateTopicTool(ctx.conn)):
        for schema in tool.parameters["properties"].values():
            assert set(schema) <= {"type", "enum"}
