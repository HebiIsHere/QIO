"""R14-C / F08：重复接续登记不推进导航版本。

最小失败场景（旧行为，修复前会红）：
* navigation.register_continuation 无论 register_intent 是否真的新建 / 替换都
  cas_or_advance —— 同一个 request_id 重放、重复点同一来源也会推进导航版本；
* 因此等待中的话题申请 / 恢复记录会把自己误判成「用户后来已经离开」而失效；
* 重放已被替换 / 取消的旧 request_id 还能重新激活旧选择。

修复口径：binding.register_intent 在同一事务里返回 changed 事实；只有 changed=True
才 cas_or_advance；重放返回既有选择与相同 nav_version；apply 旧意图不能重新激活。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.adapters.base import ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.credentials.store import MemoryKeyring
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.binding import IntentNotActive, TurnBindingService
from agent.services.navigation import (
    FragmentNotInTopic,
    NavigationVersionConflict,
    TopicNavigationService,
    TopicNotFound,
)
from agent.services.topic_application import TopicApplicationBridge
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "r14c_f08.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    yield ctx


class FakeApprovals:
    def __init__(self, *, decisions: list[dict] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.requests: list[dict] = []
        self.executions: list[dict] = []
        self.manual = False
        self.awaiting = asyncio.Event()
        self._release = asyncio.Event()

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        self.requests.append({"kind": kind, "payload": json.loads(json.dumps(payload))})
        if self.manual:
            self.awaiting.set()
            await self._release.wait()
            self._release.clear()
            self.awaiting.clear()
        item = self.decisions.pop(0) if self.decisions else {"decision": "rejected"}
        return ApprovalResult(
            str(item.get("approval_id") or f"appr_fake_{len(self.requests)}"),
            str(item.get("decision") or "rejected"),
            edits=item.get("edits"),
        )

    async def record_legacy_execution(
        self, approval_id, *, ok, error=None, summary=None
    ) -> dict | None:
        self.executions.append({"approval_id": approval_id, "ok": bool(ok)})
        return {"phase": "executed" if ok else "failed"}

    def release(self, decision: str = "approved", edits: dict | None = None) -> None:
        self.decisions.append({"decision": decision, "edits": edits})
        self._release.set()


def _topic(app: AppContext, name: str = "话题") -> str:
    return app.topics.nodes.create_topic(name).id


def _seed_fragment(
    app: AppContext, topic_id: str, *, closed: bool = True, marker: str = "a"
) -> str:
    fragment_id = f"frag_{str(topic_id)[-6:]}_{marker}"
    app.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary_version, created_at, closed_at) "
        "VALUES (?, ?, 0, ?, ?)",
        (
            fragment_id,
            topic_id,
            "2026-11-10T00:00:00+00:00",
            "2026-11-10T00:00:01+00:00" if closed else None,
        ),
    )
    app.conn.commit()
    return fragment_id


def _open_fragments(app: AppContext, topic_id: str) -> list[str]:
    return [
        row["id"]
        for row in app.conn.execute(
            "SELECT id FROM fragments WHERE topic_id = ? AND closed_at IS NULL", (topic_id,)
        )
    ]


def _make_turn(app: AppContext, *, topic_id: str, message: str = "问题"):
    fragment = app.fragments.get_or_create_open(topic_id)
    turn_id = "turn-r14c-f08"
    ctx = TurnContext(turn_id=turn_id, message=message)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=message,
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    app.navigation.enter_topic(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    ctx.user_message_id = message_id
    ctx.current_topic = topic_id
    ctx.bound_topic = topic_id
    ctx.bound_fragment_id = fragment.id
    ctx.decision_nav_version = navigation_version.current(app.conn)
    ctx.decision_topic_id = topic_id
    ctx.decision_fragment_id = fragment.id
    return ctx


def _make_bridge(app: AppContext, ctx: TurnContext, approvals: FakeApprovals):
    app.approvals = approvals

    async def _rebind(topic_id: str) -> str:
        app.bindings.rebind_topic_from_tool_nav(ctx.turn_id, topic_id)
        target_fragment = app.fragments.get_or_create_open(topic_id)
        if ctx.user_message_id:
            app.memory.move_message(ctx.user_message_id, to_fragment_id=target_fragment.id)
        app.bindings.record_binding(ctx.turn_id, topic_id, fragment_id=target_fragment.id)
        ctx.bound_topic = topic_id
        ctx.bound_fragment_id = target_fragment.id
        return topic_id

    async def _rebuild(topic_id: str) -> str:
        return f"SYS:{topic_id}"

    return TopicApplicationBridge(
        app, ctx, rebind_turn=_rebind, rebuild_system=_rebuild
    )


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    loop_now = asyncio.get_event_loop().time()
    deadline = loop_now + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 1) 重复点同一来源 / 同 request_id 重放：changed=False、版本不涨
# ---------------------------------------------------------------------------


def test_repeated_register_same_source_does_not_advance_version(app: AppContext):
    topic = _topic(app)
    source = _seed_fragment(app, topic, closed=True)
    nav = TopicNavigationService(app.conn)
    bindings = TurnBindingService(app.conn)
    v0 = navigation_version.current(app.conn)

    first = nav.register_continuation(topic, source, request_id="req-1")
    v1 = navigation_version.current(app.conn)
    assert first["changed"] is True
    assert v1 == v0 + 1, "首次真实选择改变推进一次版本"

    again = nav.register_continuation(topic, source, request_id="req-1")
    assert again["changed"] is False, "同 request_id 重放不推进版本"
    assert again["intent_id"] == first["intent_id"]
    assert again["nav_version"] == v1
    assert navigation_version.current(app.conn) == v1

    same_click = nav.register_continuation(topic, source)
    assert same_click["changed"] is False, "重复点同一来源不是新选择"
    assert same_click["nav_version"] == v1
    assert navigation_version.current(app.conn) == v1
    assert len(bindings.peek_intent().intent_id) > 0
    assert (
        app.conn.execute(
            "SELECT COUNT(*) c FROM continuation_intents WHERE state='registered'"
        ).fetchone()["c"]
        == 1
    )


# ---------------------------------------------------------------------------
# 2) 真正改选：changed=True 且版本推进；被替换的旧意图不能重新落实
# ---------------------------------------------------------------------------


def test_change_selection_advances_version_and_old_intent_cannot_apply(app: AppContext):
    topic = _topic(app)
    source_a = _seed_fragment(app, topic, closed=True)
    source_b = _seed_fragment(app, topic, closed=True, marker="b")
    nav = TopicNavigationService(app.conn)
    v0 = navigation_version.current(app.conn)

    first = nav.register_continuation(topic, source_a)
    v1 = navigation_version.current(app.conn)
    assert first["changed"] is True and v1 == v0 + 1

    second = nav.register_continuation(topic, source_b)
    v2 = navigation_version.current(app.conn)
    assert second["changed"] is True, "发送前改选是真实选择改变"
    assert v2 == v1 + 1
    assert second["intent_version"] == 2

    fragments_before = app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
    with pytest.raises(IntentNotActive):
        nav.apply_continuation(topic, first["intent_id"])
    assert (
        app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
        == fragments_before
    ), "旧意图不得建片段"
    # 当前选择仍是 B：落实 B 才创建片段。
    result = nav.apply_continuation(topic, second["intent_id"])
    assert result.source_fragment_id == source_b


# ---------------------------------------------------------------------------
# 3) 重放已取消的 request_id：不重新激活旧选择（intent_id=None）
# ---------------------------------------------------------------------------


def test_replay_cancelled_request_does_not_reactivate(app: AppContext):
    topic = _topic(app)
    source = _seed_fragment(app, topic, closed=True)
    nav = TopicNavigationService(app.conn)
    bindings = TurnBindingService(app.conn)

    first = nav.register_continuation(topic, source, request_id="req-cancel")
    bindings.cancel_intent(first["intent_id"])
    # 重新登记时 peek_intent 为空（取消的不算待落实）→ 新意图；但先用旧 request_id 重放。
    v_after_cancel = navigation_version.current(app.conn)
    replay = nav.register_continuation(topic, source, request_id="req-cancel")
    assert replay["changed"] is False
    assert replay["intent_id"] is None, "重放已取消的旧请求不得返回可落实的意图"
    assert replay["inactive"] is True
    assert navigation_version.current(app.conn) == v_after_cancel

    with pytest.raises(IntentNotActive):
        nav.apply_continuation(topic, first["intent_id"])
    assert (
        app.conn.execute(
            "SELECT COUNT(*) c FROM continuation_intents WHERE state='cancelled'"
        ).fetchone()["c"]
        == 1
    )


# ---------------------------------------------------------------------------
# 4) 重复登记不会让等待中的话题申请误失效（F08 的核心回归）
# ---------------------------------------------------------------------------


def test_duplicate_register_does_not_invalidate_pending_application(app: AppContext):
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    app.fragments.get_or_create_open(target)
    history = _seed_fragment(app, old, closed=True)
    nav = TopicNavigationService(app.conn)

    # 用户先点了一段历史（真实选择改变，版本推进一次）。
    nav.register_continuation(old, history)
    ctx = _make_turn(app, topic_id=old)
    approvals = FakeApprovals(decisions=[{"decision": "approved"}])
    approvals.manual = True
    bridge = _make_bridge(app, ctx, approvals)

    async def scenario():
        run = asyncio.create_task(
            bridge.before_tool(
                ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
            )
        )
        await _wait_until(lambda: bool(approvals.requests))
        base = approvals.requests[0]["payload"]["base_nav_version"]
        version_before = navigation_version.current(app.conn)

        # 前端重复点同一来源（重复事件）：不得推进版本，不得让申请失效。
        duplicate = nav.register_continuation(old, history)
        assert duplicate["changed"] is False
        assert navigation_version.current(app.conn) == version_before
        assert navigation_version.current(app.conn) == base

        approvals.release("approved")
        assert await asyncio.wait_for(run, timeout=5) is None, "重复登记后申请仍有效"
        call = ToolCall(id="c1", name="switch_topic", arguments={"topic_id": target})
        executed = await app.registry.execute(call)
        assert executed.ok is True, executed.error
        await bridge.after_tool(call, executed)
        assert ctx.bound_topic == target

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5) 陈旧版本零写入：apply 的 CAS 失败时不留登记 / 片段 / Anchor 变化
# ---------------------------------------------------------------------------


def test_apply_with_stale_expected_version_writes_nothing(app: AppContext):
    topic = _topic(app)
    source = _seed_fragment(app, topic, closed=True)
    nav = TopicNavigationService(app.conn)
    registered = nav.register_continuation(topic, source)
    version_at_register = registered["nav_version"]

    # 用户之后又真实导航（版本推进）：旧期望版本失效。
    other = _topic(app, "另一个话题")
    nav.enter_topic(other)
    anchor_before = app.navigation.anchors.get_active()
    fragments_before = app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
    version_before = navigation_version.current(app.conn)

    with pytest.raises(NavigationVersionConflict):
        nav.apply_continuation(
            topic, registered["intent_id"], expected_nav_version=version_at_register
        )

    assert app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == fragments_before
    assert app.navigation.anchors.get_active() == anchor_before
    assert navigation_version.current(app.conn) == version_before
    intent = TurnBindingService(app.conn).intent_by_id(registered["intent_id"])
    assert intent is not None and intent.state == "registered", "CAS 失败时意图仍未落实、可重试"


# ---------------------------------------------------------------------------
# 6) 开放片段的两阶段：登记只返回 opens_current，落实由调用方 enter_topic
# ---------------------------------------------------------------------------


def test_open_fragment_two_phase_plain_continue(app: AppContext):
    topic = _topic(app)
    open_fragment = app.fragments.get_or_create_open(topic)
    nav = TopicNavigationService(app.conn)
    v0 = navigation_version.current(app.conn)

    registered = nav.register_continuation(topic, open_fragment.id)
    assert registered["opens_current"] is True
    assert registered["intent_id"] is None
    assert registered["changed"] is False
    assert navigation_version.current(app.conn) == v0, "普通继续不推进版本"
    result = nav.enter_topic(topic, fragment_id=open_fragment.id)
    assert result.fragment_id == open_fragment.id
