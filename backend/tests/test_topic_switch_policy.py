"""Topic 切换策略（spec 第 27~32 / 73~74 条）。

* 用户明确要求切换 → 直接执行，不再确认；
* 模型/预测器推测切换 → 只产生「待确认切换」，Anchor 一动不动；
* 确认 → 切换；拒绝 → 保持原话题。
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
from agent.services.navigation import TopicNavigationService, detect_explicit_navigation
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

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        return Completion(message=ChatMessage(role="assistant", content="好的。"))


def _install_adapter(ctx: AppContext, monkeypatch) -> None:
    async def fake_build():
        return CapturingAdapter()

    monkeypatch.setattr(ctx, "build_adapter", fake_build)


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


# -- 明确切换：直接执行 -----------------------------------------------------


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


async def test_explicit_switch_executes_without_confirmation(ctx: AppContext, monkeypatch):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)

    result = await ctx.run_turn("切到 顺丁橡胶降解", topic_id=a)

    assert result["ok"] is True
    assert AnchorService(ctx.conn).get_active().topic_id == b
    assert ctx.navigation.pending_switch() is None, "明确切换不需要再确认"


# -- 推测切换：只产生待确认 --------------------------------------------------


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


async def test_inferred_switch_only_suggests_and_keeps_the_anchor(ctx: AppContext, monkeypatch):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    _force_prediction(ctx, monkeypatch, b)
    collected, finish = await _collect(ctx, "TOPIC_SWITCH_SUGGESTED")

    result = await ctx.run_turn("对了，之前那个橡胶实验还有件事", topic_id=a)
    await finish()

    assert result["ok"] is True
    assert AnchorService(ctx.conn).get_active().topic_id == a, "推测不得自行移动用户"
    pending = ctx.navigation.pending_switch()
    assert pending is not None and pending["topic_id"] == b
    assert collected and collected[0]["topic_id"] == b
    assert collected[0]["topic_name"] == "顺丁橡胶降解"
    assert collected[0]["from_topic_id"] == a


async def test_confirm_and_reject_drive_the_pending_switch(ctx: AppContext, monkeypatch):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    nav: TopicNavigationService = ctx.navigation
    nav.enter_topic(a)
    nav.request_switch(b, reason="看起来属于另一个话题")

    nav.reject_switch()
    assert AnchorService(ctx.conn).get_active().topic_id == a

    nav.request_switch(b, reason="看起来属于另一个话题")
    result = nav.confirm_switch()
    assert result is not None and result.topic_id == b
    assert AnchorService(ctx.conn).get_active().topic_id == b
    assert nav.pending_switch() is None


# -- HTTP 层 ----------------------------------------------------------------


def test_topic_switch_api_confirm_and_reject(db_conn, settings):
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
        rejected = client.post("/api/topic-switch/reject").json()
        assert rejected["ok"] is True
        assert AnchorService(ctx.conn).get_active().topic_id == a

        ctx.navigation.request_switch(b, reason="推测")
        confirmed = client.post("/api/topic-switch/confirm").json()
        assert confirmed["ok"] is True
        assert confirmed["topic_id"] == b
        assert AnchorService(ctx.conn).get_active().topic_id == b


def test_topic_switch_api_without_pending_is_a_no_op(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        ctx = client.app.state.ctx
        a = _topic(ctx, "QIO 前端")
        ctx.navigation.enter_topic(a)

        confirmed = client.post("/api/topic-switch/confirm").json()

        assert confirmed["ok"] is False
        assert AnchorService(ctx.conn).get_active().topic_id == a
