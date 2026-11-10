"""Topic 切换策略（2026-11-10 统一判断改造后的回归）。

本轮口径（见 docs/topic-application-contract-20261110.md §1）：

* 用户明确要求切换 → 直接执行，不再确认（既有行为保留）；
* 预测器/分类器**不再**单独发出可执行的切换建议，也不再写待确认切换：
  相似度只是证据，唯一可执行判断来自本轮主模型（调用原工具 → 程序建申请 → 用户确认）；
* 旧「待确认切换」HTTP 入口退役（410），不得保留可独立提交的旧确认路径；
* 拒绝或取消话题申请后，仍在原话题回答原问题（由话题申请流程测试覆盖）。

本文件只保留：明确导航识别、显式新建识别、导航原语、旧入口退役与「不再自动建议」。
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
from agent.services.navigation import (
    TopicNavigationService,
    detect_explicit_navigation,
    detect_explicit_topic_creation,
)
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


# -- 明确导航 / 明确新建：识别层 -------------------------------------------


def test_detect_explicit_navigation_matches_switch_verbs_and_titles():
    titles = [("t1", "顺丁橡胶降解"), ("t2", "QIO 前端")]

    assert detect_explicit_navigation("切到 顺丁橡胶降解", titles) == "t1"
    assert detect_explicit_navigation("切换到QIO 前端吧", titles) == "t2"
    assert detect_explicit_navigation("回到 顺丁橡胶降解 那个话题", titles) == "t1"
    assert detect_explicit_navigation("转到 QIO 前端", titles) == "t2"


def test_detect_explicit_navigation_ignores_plain_mention():
    titles = [("t1", "顺丁橡胶降解")]

    assert detect_explicit_navigation("顺丁橡胶降解的数据不错", titles) is None
    assert detect_explicit_navigation("切到 一个并不存在的话题", titles) is None


def test_detect_explicit_topic_creation_is_narrow():
    assert detect_explicit_topic_creation("帮我新建一个话题叫量子计算") == "量子计算"
    assert detect_explicit_topic_creation("创建话题「论文投稿计划」") == "论文投稿计划"
    # 不是新建话题的句子不能误判：没有「话题」二字 / 没有新建动词 / 只有名词提及
    assert detect_explicit_topic_creation("新建一个文件叫报告") is None
    assert detect_explicit_topic_creation("这段讨论换到新话题吧") is None
    assert detect_explicit_topic_creation("量子计算这个话题不错") is None


# -- 明确切换：直接执行，不弹申请 ------------------------------------------


async def test_explicit_switch_executes_without_confirmation(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    adapter = _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    asked: list[str] = []

    async def _no_application(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        asked.append(str(kind))
        raise AssertionError(f"明确切换不应建立申请：{kind} {payload}")

    monkeypatch.setattr(ctx.approvals, "request", _no_application)

    result = await ctx.run_turn("切到 顺丁橡胶降解", topic_id=a)

    assert result["ok"] is True
    assert AnchorService(ctx.conn).get_active().topic_id == b
    assert ctx.navigation.pending_switch() is None, "明确切换不需要再确认"
    assert asked == []
    assert adapter.requests, "本轮确实调用了模型"


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


async def test_explicit_create_name_reaches_the_turn_context(ctx: AppContext, monkeypatch):
    from agent.services import topic_decision as decision_module

    a = _topic(ctx, "QIO 前端")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    seen: dict = {}

    def _capture(app, turn_ctx, topic, prediction, refuse_scope=None):
        seen["ctx"] = turn_ctx
        return object()

    monkeypatch.setattr(decision_module, "build_topic_decision_state", _capture)
    monkeypatch.setattr(
        decision_module, "render_topic_decision_note", lambda state, **kwargs: ""
    )
    before = {n.id for n in ctx.topics.nodes.list_topics()}

    result = await ctx.run_turn("帮我新建一个话题叫量子计算", topic_id=a)

    assert result["ok"] is True
    # 明确新建只登记授权依据（供 bridge 判断不重复确认），不在这里创建。
    assert getattr(seen["ctx"], "explicit_create_name", None) == "量子计算"
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
