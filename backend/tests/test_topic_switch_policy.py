"""Topic 切换策略（2026-11-10 统一判断改造后的回归）。

本轮口径（见 docs/topic-application-contract-20261110.md §1）：

* 用户明确要求切换 → 直接执行，不再确认（既有行为保留）；
* 预测器/分类器**不再**单独发出可执行的切换建议，也不再写待确认切换：
  相似度只是证据，唯一可执行判断来自本轮主模型（调用原工具 → 程序建申请 → 用户确认）；
* 旧「待确认切换」HTTP 入口退役（410），不得保留可独立提交的旧确认路径；
* 拒绝或取消话题申请后，仍在原话题回答原问题（由话题申请流程测试覆盖）。

P1-01（2026-11-10 第二轮）：程序在进入主循环前的关键词 / 正则「明确授权识别」
与轮前直切已删除。是否换题只由本轮主模型在开关工具参数 intent 里声明。
本文件只保留：**轮前位置不变**（模型调用前 anchor 不变）、否定/解释/引用等
不触发程序导航、导航原语、旧入口退役与「不再自动建议」。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.services.affinity import TopicMode
from agent.services.navigation import TopicNavigationService
from agent.services.predict import TopicPrediction
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "switch.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class CapturingAdapter:
    mode = "native"
    model = "fake-model"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": tools})
        return Completion(message=ChatMessage(role="assistant", content="好的。"))


def _install_adapter(ctx: AppContext, monkeypatch) -> CapturingAdapter:
    adapter = CapturingAdapter()

    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    return adapter


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


# -- 程序识别路径已退役：不允许在进入主循环前做语义授权判断 ------------------


def test_navigation_service_has_no_programmatic_authorization_detectors():
    import agent.services.navigation as navigation_module

    assert not hasattr(navigation_module, "detect_explicit_navigation")
    assert not hasattr(navigation_module, "detect_explicit_topic_creation")
    for name in (
        "_NAV_VERBS",
        "_CREATE_NAME_PATTERNS",
    ):
        assert not hasattr(navigation_module, name), name


# -- 轮前位置不变：模型调用前 anchor 不被用户原话移动 ------------------------


async def test_switch_request_does_not_move_the_anchor_before_the_model(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    adapter = _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    seen: dict = {}

    async def _capture(messages, tools, **kwargs):  # noqa: ANN001, ANN003
        active = AnchorService(ctx.conn).get_active()
        seen["anchor_at_model"] = active.topic_id if active is not None else None
        seen["user_text"] = [
            str(getattr(m, "content", "") or "")
            for m in messages
            if getattr(m, "role", "") == "user"
        ][-1]
        return Completion(message=ChatMessage(role="assistant", content="好的。"))

    monkeypatch.setattr(adapter, "complete", _capture)
    asked: list[str] = []

    async def _no_application(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        asked.append(str(kind))
        raise AssertionError(f"模型没有调用话题工具，程序不应建立申请：{kind} {payload}")

    monkeypatch.setattr(ctx.approvals, "request", _no_application)

    result = await ctx.run_turn("切到 顺丁橡胶降解", topic_id=a)

    assert result["ok"] is True
    # 本文件的核心断言（P1-01 验收）：模型调用前位置没有任何变化。
    assert seen["anchor_at_model"] == a
    # 用户原话完整进入主循环，程序不截断、不改写、不先替模型执行。
    assert seen["user_text"] == "切到 顺丁橡胶降解"
    assert "anchor_at_model" in seen, "本轮确实调用了模型"
    # fake 模型没有请求工具：程序不得自己把用户挪到 b。
    assert AnchorService(ctx.conn).get_active().topic_id == a
    assert ctx.navigation.pending_switch() is None
    assert asked == [], "没有工具调用就不应建立话题申请"


# -- 否定 / 解释 / 引用 / 条件 / 改口 / 指代：程序不导航 ---------------------


_NON_NAVIGATION_MESSAGES = [
    "不要切换到原神话题，继续回答这里的问题",
    "请不要新建名为「原神」的话题",
    "请解释「新建话题」是什么意思，不要执行操作",
    "把「切换到原神话题」翻译成英文",
    "等我确认以后再切过去",
    "切到原神。不，还是留在这里",
    "我想玩会原神",
    "那个话题的数据怎么样",
    "顺丁橡胶降解的数据不错",
]


@pytest.mark.parametrize("message", _NON_NAVIGATION_MESSAGES)
async def test_non_requests_never_navigate_programmatically(
    ctx: AppContext, monkeypatch, message: str
):
    from agent.services.navigation import TopicNotFound  # noqa: F401 - 只为保持导入面一致

    a = _topic(ctx, "原神")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    before = {n.id for n in ctx.topics.nodes.list_topics()}
    asked: list[str] = []

    async def _no_application(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        asked.append(str(kind))
        raise AssertionError(f"非请求不应建立申请：{kind} {payload}")

    monkeypatch.setattr(ctx.approvals, "request", _no_application)

    result = await ctx.run_turn(message, topic_id=a)

    assert result["ok"] is True
    assert AnchorService(ctx.conn).get_active().topic_id == a
    assert ctx.navigation.pending_switch() is None
    assert {n.id for n in ctx.topics.nodes.list_topics()} == before, "程序不得新建话题"
    assert asked == [], "程序不得在模型调用前判断「明确请求」"


# -- 预测器不再单独产生可执行建议 ------------------------------------------


def _force_prediction(ctx: AppContext, monkeypatch, topic_id: str) -> None:
    """让预测器与分类器都认为「这段内容明显属于另一个话题」。"""
    monkeypatch.setattr(
        ctx.predictor,
        "predict",
        lambda message, current_topic_id=None: TopicPrediction(
            main_topic_id=topic_id,
            aux_topic_ids=[topic_id],
            is_new_topic_candidate=False,
            suggested_switch=True,
            scores={topic_id: 0.9},
            backend_used="onnx",
        ),
    )

    class _Decision:
        mode = TopicMode.SWITCH
        switch_to = topic_id
        closest_topic = topic_id
        closest_score = 0.9
        entity_hints: list[str] = []

    monkeypatch.setattr("agent.services.affinity.classify", lambda *a, **k: _Decision())


async def _collect(ctx: AppContext, event_type: str):
    collected: list[dict] = []

    async def consumer():
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] == event_type:
                        collected.append(evt["data"])

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish():
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return collected, finish


async def test_predictor_switch_signal_no_longer_produces_an_executable_suggestion(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    _force_prediction(ctx, monkeypatch, b)
    collected, finish = await _collect(ctx, "TOPIC_SWITCH_SUGGESTED")

    result = await ctx.run_turn("对了，之前那个橡胶实验还有件事", topic_id=a)
    await finish()

    assert result["ok"] is True
    # 相似度只是证据：既不能移动用户，也不能自行产生「待确认切换」或可执行建议。
    assert AnchorService(ctx.conn).get_active().topic_id == a, "推测不得自行移动用户"
    assert ctx.navigation.pending_switch() is None
    assert collected == [], "旧的可执行切换建议事件必须退役"


async def test_create_request_is_not_registered_or_executed_by_the_program(
    ctx: AppContext, monkeypatch
):
    adapter = _install_adapter(ctx, monkeypatch)
    a = _topic(ctx, "QIO 前端")
    ctx.navigation.enter_topic(a)
    seen: dict = {}

    async def _capture(messages, tools, **kwargs):  # noqa: ANN001, ANN003
        seen["user_text"] = [
            str(getattr(m, "content", "") or "")
            for m in messages
            if getattr(m, "role", "") == "user"
        ][-1]
        return Completion(message=ChatMessage(role="assistant", content="好的。"))

    monkeypatch.setattr(adapter, "complete", _capture)
    before = {n.id for n in ctx.topics.nodes.list_topics()}

    result = await ctx.run_turn("帮我新建一个话题叫量子计算", topic_id=a)

    assert result["ok"] is True
    # P1-01：程序不再在轮前解析出「明确新建」并写进本轮上下文（授权只来自
    # 本轮模型在 create_topic 参数 intent 里声明的值）。
    assert seen["user_text"] == "帮我新建一个话题叫量子计算"
    after = {n.id for n in ctx.topics.nodes.list_topics()}
    assert after == before, "程序不得先建一次话题（创建只由本轮模型调用 create_topic 执行）"


# -- 导航原语（内部）：待确认切换仍只登记、不移动 Anchor -------------------


def test_pending_switch_never_moves_the_anchor(ctx: AppContext):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    nav: TopicNavigationService = ctx.navigation
    nav.enter_topic(a)

    nav.request_switch(b, reason="看起来属于另一个话题")
    assert AnchorService(ctx.conn).get_active().topic_id == a
    assert nav.pending_switch()["topic_id"] == b

    nav.reject_switch()
    assert nav.pending_switch() is None
    assert AnchorService(ctx.conn).get_active().topic_id == a


# -- 旧 HTTP 入口退役：不得保留可独立提交的旧确认路径 ----------------------


def test_legacy_topic_switch_http_entries_are_retired(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        ctx = client.app.state.ctx
        a = _topic(ctx, "QIO 前端")
        b = _topic(ctx, "顺丁橡胶降解")
        ctx.navigation.enter_topic(a)
        ctx.navigation.request_switch(b, reason="推测")

        for path in ("/api/topic-switch/confirm", "/api/topic-switch/reject"):
            resp = client.post(path)
            assert resp.status_code == 410, path
            body = resp.json()
            detail = body.get("detail") or {}
            assert detail.get("code") == "topic_switch_legacy_retired", (path, body)

        # 旧入口即使被调用也不能动 Anchor：话题申请统一走 /api/approvals/{id}/respond。
        assert AnchorService(ctx.conn).get_active().topic_id == a
