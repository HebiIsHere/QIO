# -*- coding: utf-8 -*-
"""R14-E / F11：只读真实话题对象解析（lookup_topic）定向测试。

用真实临时 SQLite（AppContext + 真实迁移）与 fake adapter 证明**接线与行为边界**，
不声称真实模型自然语言准确率：

* 初始候选前五之外的已有话题（排名第 6 及更后）可以由 lookup_topic 解析出真实 topic_id；
* 只有名称、没有消息/片段的「空话题」能解析（memory_search 的摘要/原文通道都覆盖不到）；
* 重名 / 归一化同名 / 包含匹配返回**全部真实候选**，不擅自挑选；
* 不存在 / 已删除目标明确 not_found，不编 ID、不自动新建；
* 已结束话题如实返回 status；是否可切换按现有产品规则（switch_topic 只校验话题存在）；
* 查询零副作用：Anchor / 片段 / 消息 / 记忆索引 / 接续意图 / 导航版本 / 审批都不变；
* 查询之后同一轮仍可以调用 switch_topic（F01：查询不关闭后续话题决策）；
* 只查询不触发切换、不建申请、不新建话题；
* 工具说明 / 参数说明单源在 prompts.py（工具模块只引用）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from agent import prompts
from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.core.turn import TurnContext
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.memory.fragment import new_id
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.predict import TopicPrediction
from agent.services.topic_application import TopicApplicationBridge
from agent.services.topic_decision import build_candidates
from agent.services.tool_router import CORE_TOOLS, ToolRouter
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import ToolResult
from agent.tools.topic_lookup import LookupTopicTool

ANSWER_MARKER = "[[QIO:ANSWER]]"


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14e_lookup.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path / "data"), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _seed_history(
    app: AppContext,
    topic_id: str,
    *,
    texts: tuple[str, ...] = ("用户之前在这里讨论过",),
    summary: str | None = None,
) -> str:
    """真实写入一个片段（+ 消息），用于「非空话题」的区分信息。"""
    frag_id = new_id("frag")
    now = _now()
    app.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (frag_id, topic_id, summary, now),
    )
    for text in texts:
        app.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, 'user', ?, 'text', ?)",
            (new_id("msg"), frag_id, text, now),
        )
    app.conn.commit()
    return frag_id


def _ensure_tool(app: AppContext) -> LookupTopicTool:
    """注册到真实 registry（Lead 的 services/app.py 接线之前，测试自己装配）。"""
    tool = app.registry.get("lookup_topic")
    if tool is None:
        tool = LookupTopicTool(app.conn)
        app.registry.register(tool)
    assert isinstance(tool, LookupTopicTool)
    return tool


async def _lookup(app: AppContext, **kwargs) -> ToolResult:
    _ensure_tool(app)
    call = ToolCall(id="call_" + new_id("c")[2:], name="lookup_topic", arguments=kwargs)
    return await app.registry.execute(call)


def _data(result: ToolResult) -> dict:
    return dict(result.data or {})


def _snapshot(app: AppContext) -> dict:
    """查询前后的零副作用快照（全部是真实库内状态）。"""
    active = AnchorService(app.conn).get_active()
    counts = {}
    for table in ("nodes", "fragments", "messages", "memory_index", "pending_approvals"):
        counts[table] = app.conn.execute(
            "SELECT COUNT(*) AS c FROM " + table
        ).fetchone()["c"]
    return {
        "active_topic": getattr(active, "topic_id", None),
        "active_fragment": getattr(active, "fragment_id", None),
        "nav_version": navigation_version.current(app.conn),
        "legacy_pending_switch_retired": not hasattr(app.navigation, "pending_switch"),
        "intent": app.bindings.peek_intent(),
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 1. 工具契约：说明单源、参数形状、可被 registry 调用
# ---------------------------------------------------------------------------


async def test_tool_contract_and_single_source_description(app: AppContext):
    tool = _ensure_tool(app)
    assert tool.name == "lookup_topic"
    assert tool.description == prompts.TOOL_DESCRIPTIONS["lookup_topic"]
    assert str(prompts.TOOL_DESCRIPTIONS["lookup_topic"]).strip()

    props = tool.parameters["properties"]
    assert set(props) == {"name", "topic_id", "include_ended"}
    assert list(tool.parameters.get("required") or []) == []
    assert props["name"]["description"] == prompts.argument_description(
        "lookup_topic", "name"
    )
    assert props["topic_id"]["description"] == prompts.argument_description(
        "lookup_topic", "topic_id"
    )
    assert props["include_ended"]["description"] == prompts.argument_description(
        "lookup_topic", "include_ended"
    )
    # 只读工具：可和其他并发安全工具并行
    assert tool.is_concurrency_safe is True
    assert "lookup_topic" in {spec.name for spec in app.registry.specs()}

    # 结构化结果协议（模型拿到的 envelope；完整证据在 data 里）
    result = await _lookup(app, name="契约测试")
    assert result.protocol_version == 1
    assert result.status == "completed"
    assert result.code == "not_found"


# ---------------------------------------------------------------------------
# 2. 排名第 6 及更后的真实目标：初始候选看不到，lookup 能解析
# ---------------------------------------------------------------------------


async def test_lookup_finds_target_outside_initial_candidate_limit(app: AppContext):
    _topic(app, "当前话题")
    candidates_names = [f"高频候选{i}" for i in range(1, 6)]
    candidate_ids = {name: _topic(app, name) for name in candidates_names}
    target_name = "排名第六的真实目标"
    target_id = _topic(app, target_name)
    scores = {candidate_ids[name]: 0.9 - 0.05 * i for i, name in enumerate(candidates_names)}
    scores[target_id] = 0.01
    prediction = TopicPrediction(
        main_topic_id=candidate_ids[candidates_names[0]], scores=scores
    )

    # 现状（F11 问题）：初始候选上限 5，排名第 6 的目标不在提示词里
    initial = build_candidates(prediction, app.topics)
    assert len(initial) == 5
    assert target_id not in [c.topic_id for c in initial]
    # 也不扩大初始提示词：硬上限仍是 5（相似度前五继续作为自动建议证据）
    assert len(build_candidates(prediction, app.topics, limit=99)) == 5

    result = await _lookup(app, name=target_name)
    data = _data(result)
    assert result.ok is True and result.code == "ok"
    assert [t["topic_id"] for t in data["topics"]] == [target_id]
    assert data["topics"][0]["name"] == target_name
    assert data["topics"][0]["match"] == "exact"
    assert data["topics"][0]["status"] == "active"


# ---------------------------------------------------------------------------
# 3. 空话题（只有名称、没有消息/片段）：解析得到真实可执行目标
# ---------------------------------------------------------------------------


async def test_lookup_finds_empty_topic_that_memory_search_cannot_reach(app: AppContext):
    current = _topic(app, "当前话题")
    AnchorService(app.conn).set_active(current)
    empty_name = "只有名称的空话题"
    empty_id = _topic(app, empty_name)

    # 空话题在 memory_search 能读到的两张表里都完全没有数据（摘要 + 原文两条通道都覆盖不到）
    assert app.conn.execute(
        "SELECT COUNT(*) AS c FROM fragments WHERE topic_id = ?", (empty_id,)
    ).fetchone()["c"] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) AS c FROM memory_index WHERE topic_id = ?", (empty_id,)
    ).fetchone()["c"] == 0
    assert app.conn.execute(
        "SELECT COUNT(*) AS c FROM messages m JOIN fragments f ON f.id = m.fragment_id "
        "WHERE f.topic_id = ?",
        (empty_id,),
    ).fetchone()["c"] == 0

    result = await _lookup(app, name=empty_name)
    topic = _data(result)["topics"][0]
    assert topic["topic_id"] == empty_id
    assert topic["empty"] is True
    assert topic["fragment_count"] == 0
    assert topic["message_count"] == 0
    assert topic["last_activity"] is None

    # 解析出来的 ID 就是现有执行器可用的真实目标（不执行切换：这里显式验证工具可接受它）
    from agent.tools.topic_tools import SwitchTopicTool

    switched = await SwitchTopicTool(app.conn).run(
        topic_id=topic["topic_id"], reason="验证解析结果可被现有执行器使用"
    )
    assert switched.ok is True
    assert AnchorService(app.conn).get_active().topic_id == empty_id


# ---------------------------------------------------------------------------
# 4. 重名：全部真实候选都返回，不擅自挑选
# ---------------------------------------------------------------------------


async def test_lookup_lists_all_duplicate_names_without_picking(app: AppContext):
    first = _topic(app, "读书笔记")
    second = _topic(app, "读书笔记")
    result = await _lookup(app, name="读书笔记")
    data = _data(result)
    topics = data["topics"]
    assert data["found"] is True
    assert data["matched_total"] >= 2
    assert {t["topic_id"] for t in topics} == {first, second}
    assert all(t["name"] == "读书笔记" for t in topics)
    assert all(t["match"] == "exact" for t in topics)
    # 没有「挑一个」：两个都真实给出，决定交给模型按话题规则继续处理
    assert len({t["topic_id"] for t in topics}) == 2


# ---------------------------------------------------------------------------
# 5. 归一化同名 / 包含匹配：清楚标注匹配类型与顺序
# ---------------------------------------------------------------------------


async def test_lookup_returns_normalized_and_contains_matches(app: AppContext):
    exact = _topic(app, "原神")
    contains = _topic(app, "原神资料整理")
    spaced = _topic(app, "Data Science")

    result = await _lookup(app, name="原神")
    topics = _data(result)["topics"]
    ids = [t["topic_id"] for t in topics]
    assert ids[:2] == [exact, contains], "精确同名排在包含匹配之前"
    by_id = {t["topic_id"]: t for t in topics}
    assert by_id[exact]["match"] == "exact"
    assert by_id[contains]["match"] == "contains"

    normalized = await _lookup(app, name="DataScience")
    norm_topics = _data(normalized)["topics"]
    assert [t["match"] for t in norm_topics] == ["normalized"]
    assert norm_topics[0]["topic_id"] == spaced
    assert norm_topics[0]["name"] == "Data Science"


# ---------------------------------------------------------------------------
# 6. 不存在：明确 not_found，不编 ID、不自动新建
# ---------------------------------------------------------------------------


async def test_lookup_missing_name_is_explicit_not_found_and_creates_nothing(
    app: AppContext,
):
    existing = _topic(app, "已有话题")
    AnchorService(app.conn).set_active(existing)
    before_nodes = {node.id for node in app.topics.nodes.list_topics()}
    before = _snapshot(app)

    result = await _lookup(app, name="从来没有存在过的话题")
    data = _data(result)
    assert result.ok is True
    assert result.code == "not_found"
    assert data["found"] is False
    assert data["topics"] == []
    # 不自动新建同名/相似话题，也不改当前位置
    assert {node.id for node in app.topics.nodes.list_topics()} == before_nodes
    assert _snapshot(app) == before
    notes = "\n".join(str(note) for note in data.get("notes") or [])
    assert "不要编造" in notes
    assert "新建" in notes


# ---------------------------------------------------------------------------
# 7. 目标已删除（节点不存在）：明确 not_found，不编 ID、不新建
# ---------------------------------------------------------------------------


async def test_lookup_missing_or_deleted_topic_id_is_not_found(app: AppContext):
    missing = await _lookup(app, topic_id="topic_does_not_exist")
    assert missing.ok is True
    assert missing.code == "not_found"
    assert _data(missing)["topics"] == []

    doomed = _topic(app, "即将被删除的话题")
    app.conn.execute("DELETE FROM nodes WHERE id = ?", (doomed,))
    app.conn.commit()
    deleted = await _lookup(app, topic_id=doomed)
    assert deleted.code == "not_found"
    assert _data(deleted)["topics"] == []
    assert app.conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type = 'topic' AND id = ?", (doomed,)
    ).fetchone()["c"] == 0
    # 没有为被查询的 ID 补建任何节点
    assert app.conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE id = ?", (doomed,)
    ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# 8. 已结束话题：如实返回状态；按现有产品规则告知是否可切换
# ---------------------------------------------------------------------------


async def test_lookup_ended_topic_reports_status_and_current_navigation_rules(
    app: AppContext,
):
    open_topic = _topic(app, "仍在进行的话题")
    AnchorService(app.conn).set_active(open_topic)
    ended = _topic(app, "已经结束的真实话题")
    _seed_history(app, ended, texts=("这段讨论已经结束",), summary="已结束话题的摘要")
    app.topics.nodes.mark_topic_ended(ended, reason="user_confirmed")

    # 默认 include_ended=false：精确同名是权威身份事实，仍要返回并标注 ended
    result = await _lookup(app, name="已经结束的真实话题")
    topic = _data(result)["topics"][0]
    assert topic["topic_id"] == ended
    assert topic["status"] == "ended"
    assert topic["ended"] is True
    assert topic["switchable"] is True, "现有产品规则下已结束话题仍可按 ID 切换"
    assert topic["empty"] is False
    assert topic["fragment_count"] == 1
    assert topic["message_count"] == 1

    # 「可切换」不是自称：用现有 switch_topic 真实执行一次验证（记忆保留、按 ID 可达）
    from agent.tools.topic_tools import SwitchTopicTool

    switched = await SwitchTopicTool(app.conn).run(
        topic_id=ended, reason="继续已结束话题"
    )
    assert switched.ok is True
    assert AnchorService(app.conn).get_active().topic_id == ended
    assert len(switched.content) > 0

    by_id = await _lookup(app, topic_id=ended)
    assert _data(by_id)["topics"][0]["status"] == "ended"


# ---------------------------------------------------------------------------
# 9. 已结束话题的「包含匹配」默认隐藏，但如实报告存在并可显式包含
# ---------------------------------------------------------------------------


async def test_lookup_hides_ended_contains_matches_by_default_and_reports_count(
    app: AppContext,
):
    ended = _topic(app, "旧项目归档")
    app.topics.nodes.mark_topic_ended(ended)

    hidden = await _lookup(app, name="项目")
    data = _data(hidden)
    assert data["topics"] == []
    assert data["found"] is False
    assert data["ended_hidden"] >= 1
    notes = "\n".join(str(note) for note in data.get("notes") or [])
    assert "include_ended" in notes

    shown = await _lookup(app, name="项目", include_ended=True)
    topics = _data(shown)["topics"]
    assert any(t["topic_id"] == ended for t in topics)
    assert next(t for t in topics if t["topic_id"] == ended)["status"] == "ended"


# ---------------------------------------------------------------------------
# 10. 零副作用：查询不改 Anchor、不建片段、不登记接续、不推进导航版本、不审批
# ---------------------------------------------------------------------------


async def test_lookup_has_zero_navigation_side_effects(app: AppContext):
    current = _topic(app, "当前话题")
    other = _topic(app, "另一个真实话题")
    _seed_history(app, current, texts=("当前话题的真实消息",), summary="当前摘要")
    AnchorService(app.conn).set_active(current)
    before = _snapshot(app)
    assert before["active_topic"] == current

    for kwargs in (
        {"name": "另一个真实话题"},
        {"topic_id": other},
        {"name": "根本不存在"},
        {"name": "另一个", "include_ended": True},
    ):
        result = await _lookup(app, **kwargs)
        assert result.ok is True

    assert _snapshot(app) == before, "lookup_topic 必须零导航副作用"


# ---------------------------------------------------------------------------
# 11. 参数边界：至少一个；topic_id 权威优先
# ---------------------------------------------------------------------------


async def test_lookup_rejects_missing_arguments_without_side_effects(app: AppContext):
    before = _snapshot(app)
    result = await _lookup(app)
    assert result.ok is False
    assert result.status == "failed"
    assert result.code == "invalid_arguments"
    assert _snapshot(app) == before

    blank = await _lookup(app, name="   ", topic_id="  ")
    assert blank.ok is False
    assert blank.code == "invalid_arguments"
    assert _snapshot(app) == before


async def test_lookup_prefers_topic_id_when_both_given(app: AppContext):
    by_id_topic = _topic(app, "话题A")
    _topic(app, "话题B")
    result = await _lookup(app, topic_id=by_id_topic, name="话题B")
    topics = _data(result)["topics"]
    assert [t["topic_id"] for t in topics] == [by_id_topic]
    assert topics[0]["match"] == "id"


# ---------------------------------------------------------------------------
# 12. 集中提示词：lookup 使用规则补进唯一维护点，不扩大初始候选
# ---------------------------------------------------------------------------


def test_prompt_rules_describe_lookup_usage_without_expanding_candidates():
    desc = prompts.TOOL_DESCRIPTIONS["lookup_topic"]
    assert "只读" in desc
    assert "编造" in desc
    assert "新建" in desc

    switch_desc = prompts.TOOL_DESCRIPTIONS["switch_topic"]
    assert "lookup_topic" in switch_desc
    # 旧限制「topic_id 只能来自本轮话题候选」必须更新：否则排名第 6 的目标无解
    assert "或 lookup_topic 查到的真实 topic_id" in switch_desc

    policy = prompts.TOPIC_SEMANTIC_POLICY
    assert "lookup_topic" in policy
    assert "只读" in policy
    # 查询本身不等于切换决定（F01：查询不关闭后续话题决策）
    assert "不等于" in policy
    # 原有语义与反例必须保留（同一区块仍是唯一规则来源）
    for item in (
        "不要切换到原神话题，继续回答这里的问题",
        "请不要新建名为「原神」的话题",
        "请解释「新建话题」是什么意思，不要执行操作",
        "把「切换到原神话题」翻译成英文",
        "等我确认以后再切过去",
        "切到原神。不，还是留在这里",
        "我想玩会原神",
        "user_request",
        "suggestion",
        "缺省或拿不准一律按 suggestion",
        "解释文字、过程说明不等于用户确认",
    ):
        assert item in policy, f"语义规则区块缺少既有内容：{item}"


# ---------------------------------------------------------------------------
# 13. 路由可见性：lookup 必须和话题工具一样始终可见（Lead 接线）
# ---------------------------------------------------------------------------


def test_lookup_topic_is_always_visible_to_the_tool_router():
    from agent.adapters.base import ToolSpec

    assert "lookup_topic" in CORE_TOOLS, (
        "F11：lookup_topic 必须在 CORE_TOOLS 中始终可见，"
        "否则模型拿不到「名称 → 真实 topic_id」的解析路径"
    )
    specs = [ToolSpec(name=name, description="core", parameters={}) for name in CORE_TOOLS]
    routed = ToolRouter(embedding=None).route("换个话题继续聊", specs)
    assert "lookup_topic" in [spec.name for spec in routed]


# ---------------------------------------------------------------------------
# 14. 主循环接线（F11 + F01）：先查询再调用现有 switch_topic 仍会执行
# ---------------------------------------------------------------------------


class ScriptedAdapter:
    """非流式假 adapter：按脚本返回（正文, 工具调用）。"""

    mode = "native"
    model = "fake-r14e-scripted"
    supports_stream = False

    def __init__(self, script: list[tuple[str | None, list[ToolCall] | None]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.script:
            content, calls = self.script.pop(0)
        else:
            content, calls = (ANSWER_MARKER + "\n收尾回答", None)
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


def _prepare_turn(app: AppContext, *, message: str, topic_id: str):
    fragment = app.fragments.get_or_create_open(topic_id)
    app.navigation.enter_topic(topic_id)
    turn_id = "turn_r14e_lookup"
    ctx = TurnContext(turn_id=turn_id, message=message)
    message_id, _closed = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=message,
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    ctx.user_message_id = message_id
    ctx.current_topic = topic_id
    ctx.bound_topic = topic_id
    ctx.bound_fragment_id = fragment.id
    return ctx


def _make_bridge(app: AppContext, ctx: TurnContext) -> TopicApplicationBridge:
    async def _rebind(topic_id: str) -> str:
        binding = app.bindings.binding_for(ctx.turn_id)
        target = app.fragments.get_or_create_open(topic_id)
        app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, topic_id)
        if ctx.user_message_id:
            app.memory.move_message(ctx.user_message_id, to_fragment_id=target.id)
        app.bindings.record_binding(
            ctx.turn_id,
            topic_id,
            fragment_id=target.id,
            intent_id=getattr(binding, "intent_id", None),
            intent_version=getattr(ctx, "bound_intent_version", None),
        )
        ctx.bound_topic = topic_id
        ctx.bound_fragment_id = target.id
        app.navigation.enter_topic(topic_id)
        return topic_id

    async def _rebuild(topic_id: str) -> str:
        return f"SYS_FOR:{topic_id}"

    return TopicApplicationBridge(app, ctx, rebind_turn=_rebind, rebuild_system=_rebuild)


def _initial_messages(message: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="SYS_FOR:old"),
        ChatMessage(role="user", content=message),
    ]


async def test_lookup_then_switch_topic_in_same_turn_still_executes(
    app: AppContext, monkeypatch
):
    """F11 的接线目标：模型先 lookup 到真实 topic_id，再用现有 switch_topic 执行。

    F01 保证「普通工具调用（查询）不打开可回答阶段」：这次查询之后的话题工具
    仍然是决策期内的合法调用，不能被当成 ANSWERABLE 之后的迟到调用丢弃。
    """
    old_topic = _topic(app, "原话题")
    target_topic = _topic(app, "排名第六的真实话题")
    question = "把我换到排名第六的真实话题去聊"
    ctx = _prepare_turn(app, message=question, topic_id=old_topic)

    adapter = ScriptedAdapter(
        [
            (
                "我先查一下这个真实话题。",
                [
                    ToolCall(
                        id="c_lookup",
                        name="lookup_topic",
                        arguments={"name": "排名第六的真实话题"},
                    )
                ],
            ),
            (
                "查到了，切过去。",
                [
                    ToolCall(
                        id="c_switch",
                        name="switch_topic",
                        arguments={"topic_id": target_topic, "intent": "user_request"},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n换到目标话题后的正式回答", None),
        ]
    )
    calls: list[tuple[str, dict]] = []

    async def _forbid_approval(kind, payload, **kwargs):  # noqa: ANN001, ANN003
        calls.append((str(kind), dict(payload or {})))
        raise AssertionError(f"user_request 不应建立普通话题申请：{kind}")

    monkeypatch.setattr(app.approvals, "request", _forbid_approval)
    _ensure_tool(app)
    loop = AgentLoop(
        adapter,
        app.registry,
        app.bus,
        approvals=app.approvals,
        topic_application=_make_bridge(app, ctx),
        turn_id=ctx.turn_id,
    )

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=20
    )

    assert result.final_content == "换到目标话题后的正式回答"
    assert calls == [], "话题工具走 user_request 直接执行，不应弹普通申请"
    # 查询之后的 switch_topic 真的执行了（不是被当成迟到调用丢弃）
    assert AnchorService(app.conn).get_active().topic_id == target_topic
    assert app.bindings.binding_for(ctx.turn_id).topic_id == target_topic
    # lookup 的真实结果进入了下一轮模型输入（模型看到的是真实 topic_id，不是编的）
    second_messages = adapter.requests[1]["messages"]
    tool_texts = [
        str(getattr(message, "content", "") or "")
        for message in second_messages
        if getattr(message, "role", "") == "tool"
    ]
    assert any(target_topic in text for text in tool_texts), "查询结果没有进入模型输入"
    assert any('"code": "ok"' in text for text in tool_texts)


async def test_lookup_alone_does_not_switch_or_create_application(
    app: AppContext, monkeypatch
):
    """只查询不改讨论位置：主模型没有要求操作时，查询本身不触发切换/申请/新建。"""
    old_topic = _topic(app, "稳定原话题")
    target_topic = _topic(app, "只是查一下的话题")
    question = "先确认一下有没有这个话题，暂时不用切"
    ctx = _prepare_turn(app, message=question, topic_id=old_topic)
    adapter = ScriptedAdapter(
        [
            (
                "我查一下。",
                [
                    ToolCall(
                        id="c_lookup",
                        name="lookup_topic",
                        arguments={"name": "只是查一下的话题"},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n好的，只回答原问题。", None),
        ]
    )

    async def _forbid_approval(kind, payload, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError(f"只查询不应建立申请：{kind}")

    monkeypatch.setattr(app.approvals, "request", _forbid_approval)
    _ensure_tool(app)
    loop = AgentLoop(
        adapter,
        app.registry,
        app.bus,
        approvals=app.approvals,
        topic_application=_make_bridge(app, ctx),
        turn_id=ctx.turn_id,
    )

    result = await asyncio.wait_for(
        loop.run(question, initial_messages=_initial_messages(question)), timeout=20
    )

    assert result.final_content == "好的，只回答原问题。"
    assert AnchorService(app.conn).get_active().topic_id == old_topic
    assert app.bindings.binding_for(ctx.turn_id).topic_id == old_topic
    topic_count = app.conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type = 'topic'"
    ).fetchone()["c"]
    assert topic_count == 2, "查询不得新建话题"
    assert app.conn.execute(
        "SELECT COUNT(*) AS c FROM pending_approvals"
    ).fetchone()["c"] == 0
    # 查询真的发生过并返回了目标（只是没有触发导航）
    tool_texts = [
        str(getattr(message, "content", "") or "")
        for message in adapter.requests[1]["messages"]
        if getattr(message, "role", "") == "tool"
    ]
    assert any(target_topic in text for text in tool_texts)
