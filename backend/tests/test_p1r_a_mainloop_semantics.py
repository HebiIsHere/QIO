# -*- coding: utf-8 -*-
"""P1-01：主循环语义与入口（A 角色定向测试）。

只证明**接线与分支衔接**，不声称真实模型在自然语言场景中的准确率：

* 轮前位置不变：模型第一次被调用时 anchor 仍是本轮提交时的话题；
* 程序不再做轮前语义识别：否定 / 解释 / 引用 / 条件 / 改口 / 仅讨论 / 指代不清
  的句子不会触发程序导航或程序授权登记；
* 用户原话完整进入主循环（不截断、不改写）；程序不替模型执行明确请求；
* 直接 UI 导航入口（enter_topic / 两阶段续作 register_continuation→apply_continuation /
  默认话题）不受破坏；
* prompts.py 的唯一语义规则区块包含契约 §7 全部反例与两个工具的 intent 说明。
"""
from __future__ import annotations

import inspect

import pytest

from agent import prompts
from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.services.topic_decision import (
    TopicCandidate,
    build_topic_decision_state,
    render_topic_decision_note,
)
from agent.services.predict import TopicPrediction
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "p1r_a.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class RecordingAdapter:
    mode = "native"
    model = "fake-model"

    def __init__(self, reply: str = "好的。") -> None:
        self.reply = reply
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": tools})
        return Completion(message=ChatMessage(role="assistant", content=self.reply))


def _install_adapter(ctx: AppContext, monkeypatch, reply: str = "好的。") -> RecordingAdapter:
    adapter = RecordingAdapter(reply=reply)

    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    return adapter


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _anchor(ctx: AppContext) -> str | None:
    active = AnchorService(ctx.conn).get_active()
    return active.topic_id if active is not None else None


def _user_texts(adapter: RecordingAdapter) -> list[str]:
    assert adapter.requests, "本轮没有模型请求"
    return [
        str(getattr(message, "content", "") or "")
        for message in adapter.requests[-1]["messages"]
        if getattr(message, "role", "") == "user"
    ]


# -- 1. 轮前 anchor 不变（模型调用前没有位置变化）---------------------------


async def test_anchor_is_unchanged_at_the_first_model_call(ctx: AppContext, monkeypatch):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    adapter = _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    seen: dict = {}

    async def _capture(messages, tools, **kwargs):  # noqa: ANN001, ANN003
        seen["anchor_at_model"] = _anchor(ctx)
        seen["user_text"] = [
            str(getattr(message, "content", "") or "")
            for message in messages
            if getattr(message, "role", "") == "user"
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
    assert seen["anchor_at_model"] == a, "轮前 anchor 在模型调用前就被移动了"
    assert seen["user_text"] == "切到 顺丁橡胶降解", "用户原话必须完整进入主循环"
    assert _anchor(ctx) == a, "fake 模型未请求工具，程序不得替用户切换"
    assert not hasattr(ctx.navigation, "pending_switch"), "旧待确认切换机制已退役"
    assert asked == []


# -- 2. 否定 / 解释 / 引用 / 条件 / 改口 / 仅讨论 / 指代：程序不导航 --------


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
async def test_negation_quotation_and_discussion_do_not_navigate(
    ctx: AppContext, monkeypatch, message: str
):
    a = _topic(ctx, "原神")
    adapter = _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    before = {node.id for node in ctx.topics.nodes.list_topics()}
    asked: list[str] = []

    async def _no_application(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        asked.append(str(kind))
        raise AssertionError(f"非请求不应建立申请：{kind} {payload}")

    monkeypatch.setattr(ctx.approvals, "request", _no_application)

    result = await ctx.run_turn(message, topic_id=a)

    assert result["ok"] is True
    assert _user_texts(adapter)[-1] == message
    assert _anchor(ctx) == a, f"程序被非请求句移动了位置：{message}"
    assert not hasattr(ctx.navigation, "pending_switch"), "旧待确认切换机制已退役"
    assert {node.id for node in ctx.topics.nodes.list_topics()} == before, "程序不得新建话题"
    assert asked == [], "程序不得在进入主循环前判断「明确请求」"


# -- 3. 明确请求：程序不再自行执行 / 不再写 explicit 授权 -------------------


async def test_explicit_create_request_is_not_registered_by_the_program(
    ctx: AppContext, monkeypatch
):
    a = _topic(ctx, "QIO 前端")
    adapter = _install_adapter(ctx, monkeypatch)
    ctx.navigation.enter_topic(a)
    before = {node.id for node in ctx.topics.nodes.list_topics()}

    result = await ctx.run_turn("帮我新建一个话题叫量子计算", topic_id=a)

    assert result["ok"] is True
    assert _user_texts(adapter)[-1] == "帮我新建一个话题叫量子计算"
    assert {node.id for node in ctx.topics.nodes.list_topics()} == before
    # 旧实现会在轮前解析名称并写 ctx.explicit_create_name；现在只来自模型工具参数。
    assert not hasattr(ctx, "explicit_create_name")
    assert getattr(ctx, "explicit_target", None) is None


def test_orchestrator_source_has_no_programmatic_navigation_detection():
    from agent.services import turn_orchestrator as module

    source = inspect.getsource(module)
    for forbidden in (
        "detect_explicit_navigation",
        "detect_explicit_topic_creation",
        "explicit_target",
        "explicit_create_name",
    ):
        assert forbidden not in source, f"turn_orchestrator 仍引用 {forbidden}"


# -- 4. 直接 UI 导航入口不受破坏 -------------------------------------------


async def test_direct_ui_navigation_entries_still_work(ctx: AppContext, monkeypatch):
    a = _topic(ctx, "QIO 前端")
    b = _topic(ctx, "顺丁橡胶降解")
    adapter = _install_adapter(ctx, monkeypatch)

    # 4.1 手动进入话题 / 默认话题：前端传 topic_id 的既有入口。
    ctx.navigation.enter_topic(a)
    assert _anchor(ctx) == a
    result = await ctx.run_turn("继续", topic_id=b)
    assert result["ok"] is True
    assert _anchor(ctx) == b, "默认话题入口仍应把本轮归属放到指定话题"

    # 4.2 从历史片段继续：F14 之后只有两阶段契约（登记 → 落实）。来源是开放
    # 片段时 register_continuation 返回 opens_current=True / intent_id=None，
    # 之后按普通进入落实（不创建新片段）。
    open_fragment = ctx.fragments.get_or_create_open(a)
    registered = ctx.navigation.register_continuation(a, open_fragment.id)
    assert registered["opens_current"] is True
    assert registered["intent_id"] is None
    applied = ctx.navigation.enter_topic(a, fragment_id=open_fragment.id)
    assert applied.topic_id == a
    assert _anchor(ctx) == a


# -- 5. 唯一语义规则区块包含契约 §7 全部反例与 intent 说明 ------------------


_CONTRACT_COUNTEREXAMPLES = [
    "不要切换到原神话题，继续回答这里的问题",
    "请不要新建名为「原神」的话题",
    "请解释「新建话题」是什么意思，不要执行操作",
    "把「切换到原神话题」翻译成英文",
    "等我确认以后再切过去",
    "切到原神。不，还是留在这里",
    "我想玩会原神",
    "同意，切过去",
    "那个",
]


def test_semantic_policy_block_covers_contract_counterexamples():
    policy = prompts.TOPIC_SEMANTIC_POLICY
    for item in _CONTRACT_COUNTEREXAMPLES:
        assert item in policy, f"语义规则区块缺少契约 §7 反例：{item}"
    assert "user_request" in policy
    assert "suggestion" in policy
    assert "缺省或拿不准一律按 suggestion" in policy
    assert "解释文字、过程说明不等于用户确认" in policy


def test_semantic_policy_is_the_only_rule_source_in_render_output():
    policy = prompts.TOPIC_SEMANTIC_POLICY
    state = build_topic_decision_state(
        type("_App", (), {"topics": None, "fragments": None})(),
        type("_Ctx", (), {"message": "继续", "bound_fragment_id": None})(),
        None,
        TopicPrediction(main_topic_id=None, backend_used="rules"),
    )
    note = render_topic_decision_note(state, [TopicCandidate("t1", "当前", 0.1)])
    assert policy in note, "每轮 system 小节必须携带唯一语义规则区块"
    assert "intent" in note
