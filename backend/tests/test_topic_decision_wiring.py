"""话题判断接线（Lead 集成面，2026-11-10）。

验证：统一判断证据确实进入**本轮**主模型请求；相似度不再单独产生可执行动作；
延续/纠正/追问不会触发话题申请；用户明确新建只登记授权依据、不在这里创建。

这些用例只验证程序接线与契约边界，用 fake adapter，不声称真实模型在自然语言
场景中的准确率。
"""
from __future__ import annotations

import json

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.affinity import TopicMode
from agent.services.app import AppContext
from agent.services import topic_decision as decision_module
from agent.services.predict import TopicPrediction
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

# 本文件验证话题申请的真实审批交互：关闭 conftest 的旧测试自动批准兜底。
pytestmark = pytest.mark.topic_application


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "wiring.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class RecordingAdapter:
    mode = "native"
    model = "fake-model"

    def __init__(self, reply: str = "好的。") -> None:
        self.reply = reply
        self.messages: list[list] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.messages.append(list(messages))
        return Completion(message=ChatMessage(role="assistant", content=self.reply))


def _install_adapter(ctx: AppContext, monkeypatch, reply: str = "好的。") -> RecordingAdapter:
    adapter = RecordingAdapter(reply=reply)

    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    return adapter


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _system_text(adapter: RecordingAdapter) -> str:
    assert adapter.messages, "本轮没有模型请求"
    parts = [
        str(getattr(m, "content", "") or "")
        for m in adapter.messages[-1]
        if getattr(m, "role", "") == "system"
    ]
    return "\n".join(parts)


async def test_decision_evidence_note_is_injected_into_the_turn_system(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    adapter = _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)

    sentinel = "【Sentinel】统一话题判断证据已注入"
    monkeypatch.setattr(
        decision_module, "build_topic_decision_state", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        decision_module,
        "render_topic_decision_note",
        lambda state, candidates=None, **kwargs: sentinel,
    )

    result = await ctx.run_turn("继续看一下刚才的界面问题", topic_id=a)

    assert result["ok"] is True
    assert sentinel in _system_text(adapter), "证据必须进入本轮 system，而不是另一套判题请求"


async def test_evidence_builder_receives_refusal_scope(ctx: AppContext, monkeypatch):
    a = _topic(ctx, "QIO 前端")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    seen: dict = {}

    def _capture(app, turn_ctx, topic, prediction, refuse_scope=None):
        seen["refuse_scope"] = refuse_scope
        seen["ctx"] = turn_ctx
        return object()

    monkeypatch.setattr(decision_module, "build_topic_decision_state", _capture)
    monkeypatch.setattr(
        decision_module, "render_topic_decision_note", lambda state, **kwargs: ""
    )

    result = await ctx.run_turn("这段继续说", topic_id=a)

    assert result["ok"] is True
    assert seen.get("refuse_scope", "missing") is None
    assert seen["ctx"] is not None


@pytest.mark.parametrize(
    "message",
    [
        "刚才那个数据再补充一个维度",
        "不对，应该是 2024 年的那份",
        "继续刚才的问题",
        "把刚才的结论重做一遍，按实验组分开",
    ],
)
async def test_continuation_and_correction_do_not_request_a_topic_application(
    ctx: AppContext, monkeypatch, message
):
    a = _topic(ctx, "实验数据分析")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    requested: list[str] = []

    async def _no_request(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        requested.append(str(kind))
        raise AssertionError(f"延续/纠正/追问轮不应建立任何话题申请：{kind}")

    monkeypatch.setattr(ctx.approvals, "request", _no_request)

    result = await ctx.run_turn(message, topic_id=a)

    assert result["ok"] is True
    assert requested == []
    assert AnchorService(ctx.conn).get_active().topic_id == a


async def test_high_similarity_alone_never_emits_an_executable_suggestion(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    monkeypatch.setattr(
        ctx.predictor,
        "predict",
        lambda message, current_topic_id=None: TopicPrediction(
            main_topic_id=b,
            aux_topic_ids=[b],
            is_new_topic_candidate=False,
            suggested_switch=True,
            scores={b: 0.93, a: 0.2},
            backend_used="onnx",
        ),
    )
    published: list[dict] = []

    async def _capture(event) -> None:  # noqa: ANN001
        published.append(
            {
                "type": str(getattr(event, "type", "")),
                "data": dict(getattr(event, "data", {}) or {}),
            }
        )

    monkeypatch.setattr(ctx.bus, "publish", _capture)

    result = await ctx.run_turn("这个界面上的按钮为什么没有对齐", topic_id=a)

    assert result["ok"] is True
    assert AnchorService(ctx.conn).get_active().topic_id == a
    assert ctx.navigation.pending_switch() is None
    suggestions = [e for e in published if e["type"].endswith("TOPIC_SWITCH_SUGGESTED")]
    assert suggestions == [], f"相似度不得单独产生可执行建议：{suggestions}"


async def test_create_request_no_longer_registers_program_authorization(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    before = {n.id for n in ctx.topics.nodes.list_topics()}

    result = await ctx.run_turn("帮我新建一个话题叫量子计算", topic_id=a)

    assert result["ok"] is True
    # P1-01：进入主循环前不再解析 / 登记「明确新建」授权；创建只由本轮模型
    # 调用 create_topic（intent=user_request|suggestion）执行一次。
    after = {n.id for n in ctx.topics.nodes.list_topics()}
    assert after == before, "程序不得先建一次话题（创建只由本轮模型调用 create_topic 执行）"