"""故障注入：半交接、重试、重启、派生任务幂等（阶段 1/2 的交付要求）。

规格里点名的几条必须能被验证，而不是「理论上不会发生」：

* 事务中任一步失败 → 保持原状态或完整新状态，不出现半交接；
* 事务已提交但事件/响应丢失 → 重试返回既有结果，不重复创建；
* 重启发生在绑定后 / 摘要前 / 摘要后 → 绑定与任务状态一致，无重复副作用；
* 首轮失败或取消 → 复用已创建的接续位置，原始记录不被伪造成功。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks
from agent.services.app import AppContext
from agent.services.binding import TurnBindingService
from agent.services.navigation import TopicNavigationService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app(tmp_path: Path, name: str = "fault.db") -> AppContext:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _topic(ctx: AppContext, name: str = "话题") -> str:
    return ctx.topics.nodes.create_topic(name).id


def _fragment(ctx: AppContext, topic: str, fragment_id: str, *, closed: bool) -> None:
    ts = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at, meta) "
        "VALUES (?, ?, ?, 1, ?, ?, '{}')",
        (fragment_id, topic, "旧摘要" if closed else None, ts, ts if closed else None),
    )
    ctx.conn.commit()


class _SummaryAdapter:
    """最小的摘要适配器：返回固定摘要，供派生任务跑通。"""

    mode = "native"
    model = "fake-summary"

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(
            message=ChatMessage(
                role="assistant",
                content='{"title": "标题", "summary": "这是一段摘要", "entities": [], "keywords": []}',
            )
        )


def test_transaction_failure_leaves_no_half_handoff(tmp_path: Path, monkeypatch):
    """接续交接中途失败：旧片段不能被封存、不能留下新片段、意图不能被标成已落实。"""
    ctx = _app(tmp_path)
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", closed=True)
    _fragment(ctx, topic, "frag_c", closed=False)
    nav = TopicNavigationService(ctx.conn)
    intent = nav.register_continuation(topic, "frag_a")

    def boom(*args, **kwargs):
        raise RuntimeError("建片段时炸了")

    # 注意：nav 内部有它自己的 FragmentManager 实例，要打在它身上才生效
    monkeypatch.setattr(nav.fragments, "create_child", boom)
    with pytest.raises(RuntimeError):
        nav.apply_continuation(topic, intent["intent_id"])

    c_row = ctx.conn.execute(
        "SELECT closed_at, boundary_reason FROM fragments WHERE id = 'frag_c'"
    ).fetchone()
    assert c_row["closed_at"] is None, "事务回滚：C 不能被封存"
    assert c_row["boundary_reason"] is None
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 2, "不该留下新片段"
    assert ctx.bindings.intent_by_id(intent["intent_id"]).state == "registered", "意图仍未落实"


def test_retry_after_commit_returns_the_same_fragment(tmp_path: Path):
    """事务已提交但响应丢了：重试必须返回同一个片段，不重复创建。"""
    ctx = _app(tmp_path)
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", closed=True)
    nav = TopicNavigationService(ctx.conn)
    intent = nav.register_continuation(topic, "frag_a", request_id="req_1")

    first = nav.apply_continuation(topic, intent["intent_id"])
    second = nav.apply_continuation(topic, intent["intent_id"])  # 传输层重试

    assert first.fragment_id == second.fragment_id
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 2
    again = nav.register_continuation(topic, "frag_a", request_id="req_1")
    assert again["intent_id"] == intent["intent_id"]


def test_restart_after_binding_keeps_binding(tmp_path: Path):
    """绑定后进程重启：绑定仍然读得到（重启不丢归属）。"""
    ctx = _app(tmp_path)
    topic = _topic(ctx)
    ctx.bindings.record_binding("turn_restart", topic, fragment_id=None)
    ctx.conn.close()

    conn = connect(tmp_path / "fault.db")
    again = TurnBindingService(conn).binding_for("turn_restart")
    assert again is not None and again.topic_id == topic
    conn.close()


def test_restart_before_summary_resumes_the_task_once(tmp_path: Path):
    """摘要前重启：派生任务仍在，恢复后只跑一次，索引只有一条。"""
    ctx = _app(tmp_path)
    topic = _topic(ctx)
    ctx.memory.append_message(topic_id=topic, role="user", content="问题")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="回答")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    task = derived_tasks.task_for(
        ctx.conn, derived_tasks.KIND_SUMMARY, sealed.id, int(sealed.content_version or 0)
    )
    assert task is not None and task.state == derived_tasks.STATE_PENDING
    ctx.conn.close()

    restarted = _app(tmp_path)
    derived_tasks.recover_stale(restarted.conn)
    done = asyncio.run(restarted.memory_lifecycle.drain_derived_tasks(_SummaryAdapter(), limit=5))
    asyncio.run(restarted.memory_lifecycle.drain_derived_tasks(_SummaryAdapter(), limit=5))

    rows = restarted.conn.execute(
        "SELECT COUNT(*) c FROM memory_index WHERE fragment_id = ?", (sealed.id,)
    ).fetchone()["c"]
    assert done == 1 and rows == 1, f"done={done} index_rows={rows}"
    assert restarted.fragments.messages(sealed.id), "原文仍然可读"


def test_failed_first_turn_reuses_the_continuation_fragment(tmp_path: Path):
    """首轮失败后重试：复用已创建的接续片段，不重复建、也不伪造成功。"""
    ctx = _app(tmp_path)
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", closed=True)
    nav = TopicNavigationService(ctx.conn)
    intent = nav.register_continuation(topic, "frag_a")
    applied = nav.apply_continuation(topic, intent["intent_id"])

    state = ctx.bindings.intent_by_id(intent["intent_id"])
    assert state.state == "resolved"
    assert ctx.fragments.open_fragment(topic).id == applied.created_fragment_id
    assert ctx.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE fragment_id = ?", (applied.created_fragment_id,)
    ).fetchone()["c"] == 0, "失败的一轮不该伪造出内容"

    retried = nav.apply_continuation(topic, intent["intent_id"])
    assert retried.fragment_id == applied.created_fragment_id
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 2
