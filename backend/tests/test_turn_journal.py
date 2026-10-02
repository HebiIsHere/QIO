"""F2: turn 队列持久化 —— 被接受过的消息不能因为重启静默消失。

缺陷：排队中的用户消息只活在进程内存的 `asyncio.Queue` 里（连 `messages` 表都
还没写），进程一退就毫无痕迹。修复后的产品语义（明确、可测，见
storage/turn_journal.py）：

* 重启时把留着的 queued / running 标成 `interrupted`（区分原因），**不自动重放**；
* 消息原文留下来，通过 `/api/runtime/state` 的 `interrupted_turns` 如实告诉用户，
  由用户明确选择「重发」或「知道了」；
* 只有 `interrupted` 能被重发，且重发是一次性的 —— 已完成的 turn 永远不可重复执行。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnManager
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import INTERRUPTED, TurnJournal


class _AnswerAdapter:
    mode = "native"
    model = "m"

    async def complete(self, messages, tools, **kwargs):
        return Completion(
            message=ChatMessage(role="assistant", content="好的"),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class _SlowAdapter:
    """第一次调用卡住：用来制造「A 在跑、B 排队」的现场。"""

    mode = "native"
    model = "m"

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(self, messages, tools, **kwargs):
        await self.release.wait()
        return Completion(
            message=ChatMessage(role="assistant", content="第一条回答"),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


def _ctx(tmp_path, name: str = "app.db") -> AppContext:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


# -- 状态机：queued / running / 终态 / interrupted ---------------------------


async def test_journal_records_lifecycle(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_1", message="排队中", topic_id="t1")
    row = db_conn.execute("SELECT * FROM turn_journal WHERE turn_id='turn_1'").fetchone()
    assert row["status"] == "queued"
    assert row["ended_at"] is None

    journal.running("turn_1")
    journal.note_user_message("turn_1", "msg_1")
    journal.terminal("turn_1", "completed")
    row = db_conn.execute("SELECT * FROM turn_journal WHERE turn_id='turn_1'").fetchone()
    assert row["status"] == "completed"
    assert row["started_at"] and row["ended_at"]
    assert row["user_message_id"] == "msg_1"


def test_running_rows_become_interrupted_on_restart(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_q", message="排队中的消息", topic_id="t1")
    journal.accepted(turn_id="turn_r", message="执行中的消息", topic_id="t1")
    journal.running("turn_r")

    # 重启：新的 TurnJournal 在同一份库上做启动归一化
    recovered = TurnJournal(db_conn).interrupt_stale()
    rows = {r["turn_id"]: r for r in recovered}
    assert rows["turn_q"]["reason"] == "queued_at_restart"
    assert rows["turn_r"]["reason"] == "running_at_restart"
    assert rows["turn_r"]["message"] == "执行中的消息"

    unfinished = {r["turn_id"]: r for r in TurnJournal(db_conn).unfinished()}
    assert set(unfinished) == {"turn_q", "turn_r"}
    assert unfinished["turn_q"]["status"] == INTERRUPTED
    assert "排队" in unfinished["turn_q"]["reason_text"]


def test_completed_rows_are_never_recoverable(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_done", message="已经执行完", topic_id="t1")
    journal.running("turn_done")
    journal.terminal("turn_done", "completed")

    assert TurnJournal(db_conn).recoverable("turn_done") is None
    assert TurnJournal(db_conn).unfinished() == []
    assert TurnJournal(db_conn).interrupt_stale() == []  # 终态不会被改成 interrupted


def test_prune_keeps_interrupted_rows(db_conn: sqlite3.Connection):
    """终态行只为「不重复执行」作证，可以按天清理；未处理的 interrupted 行不行。"""
    journal = TurnJournal(db_conn, retention_days=7)
    journal.accepted(turn_id="turn_old", message="老的完成消息", topic_id="t1")
    journal.terminal("turn_old", "completed")
    db_conn.execute(
        "UPDATE turn_journal SET ended_at = '2020-01-01T00:00:00+00:00' WHERE turn_id='turn_old'"
    )

    journal.accepted(turn_id="turn_lost", message="没执行的消息", topic_id="t1")
    journal.interrupt_stale()

    assert journal.prune_terminal() == 1
    assert db_conn.execute(
        "SELECT COUNT(*) FROM turn_journal WHERE turn_id='turn_old'"
    ).fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM turn_journal WHERE turn_id='turn_lost'"
    ).fetchone()[0] == 1


# -- 端到端：真实 TurnManager + 真实库 ---------------------------------------


async def test_shutdown_keeps_queued_message_and_does_not_execute_it(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    journal = TurnJournal(conn)
    started = asyncio.Event()
    adapter = _SlowAdapter()

    async def runner(ctx):
        if ctx.message == "第一条":
            started.set()
            await adapter.complete([], [])
        else:  # pragma: no cover - 第二条不该被执行
            raise AssertionError("排队中的消息被执行了")

    tm = TurnManager(runner)
    tm.set_journal(journal)
    tm.submit("第一条")
    await started.wait()
    tm.submit("第二条（排队）")
    assert tm.queued_count() == 1

    await tm.shutdown()

    rows = {r["message"]: r for r in journal.unfinished()}
    assert set(rows) == {"第一条", "第二条（排队）"}
    assert rows["第二条（排队）"]["reason"] == "shutdown"
    assert rows["第二条（排队）"]["status"] == INTERRUPTED
    conn.close()


async def test_restart_does_not_auto_execute_queued_message(tmp_path):
    """重启后的核心断言：消息还在、但**没有**被自动执行，也没有假回答落库。"""
    ctx1 = _ctx(tmp_path)
    topic = ctx1.topics.nodes.create_topic("重启话题").id
    adapter = _SlowAdapter()

    async def _build():
        return adapter

    ctx1.build_adapter = _build  # type: ignore[assignment]
    first = asyncio.create_task(ctx1.run_turn("第一条", topic_id=topic))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(ctx1.run_turn("第二条（排队）", topic_id=topic))
    await asyncio.sleep(0.05)
    assert ctx1.turns.queued_count() == 1

    await ctx1.turns.shutdown()
    await asyncio.gather(first, second, return_exceptions=True)

    # 重启：同一份库上重建 AppContext（它会做启动归一化）
    ctx2 = _ctx(tmp_path)
    assert ctx2.turns.active is None
    assert ctx2.turns.queued_count() == 0, "重启后不得自动恢复执行"

    lost = {r["message"]: r for r in ctx2.turn_journal.unfinished()}
    assert "第二条（排队）" in lost, lost
    assert lost["第二条（排队）"]["reason"] == "shutdown"

    # 没有假回答：只有第一条（被掐断）可能留下东西，第二条绝不会
    rows = ctx2.conn.execute("SELECT content FROM messages WHERE content = ?", ("第一条回答",)).fetchall()
    assert rows == []


async def test_completed_turn_is_recorded_as_completed(tmp_path):
    ctx = _ctx(tmp_path)

    async def _build():
        return _AnswerAdapter()

    ctx.build_adapter = _build  # type: ignore[assignment]
    topic = ctx.topics.nodes.create_topic("完成话题").id
    result = await ctx.run_turn("会正常完成的消息", topic_id=topic)
    assert result["ok"] is True

    row = ctx.conn.execute(
        "SELECT * FROM turn_journal WHERE message = ?", ("会正常完成的消息",)
    ).fetchone()
    assert row is not None
    assert row["status"] == "completed"
    assert row["ended_at"]
    assert ctx.turn_journal.recoverable(row["turn_id"]) is None


# -- HTTP：如实告诉用户 + 一次性重发 -----------------------------------------


@pytest.fixture()
def client(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()

    async def _build():
        return _AnswerAdapter()

    ctx.build_adapter = _build  # type: ignore[assignment]

    with TestClient(app) as c:
        yield c


def _seed_interrupted(
    ctx: AppContext, turn_id: str, message: str, topic_id: str, *, notify: bool = False
) -> None:
    ctx.turn_journal.accepted(
        turn_id=turn_id, message=message, topic_id=topic_id, notify=notify
    )
    ctx.turn_journal.running(turn_id)


def test_runtime_state_surfaces_unexecuted_messages(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("恢复话题").id
    _seed_interrupted(ctx, "turn_lost", "这条没有执行", topic)
    ctx.turn_journal.interrupt_stale()

    state = client.get("/api/runtime/state").json()
    lost = {r["turn_id"]: r for r in state["interrupted_turns"]}
    assert lost["turn_lost"]["message"] == "这条没有执行"
    assert lost["turn_lost"]["status"] == INTERRUPTED
    assert lost["turn_lost"]["reason"] == "running_at_restart"


def test_resend_is_one_shot_and_completed_turns_are_refused(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("重发话题").id
    _seed_interrupted(ctx, "turn_lost", "这条没有执行", topic)
    ctx.turn_journal.interrupt_stale()
    ctx.turn_journal.accepted(turn_id="turn_done", message="已经完成", topic_id=topic)
    ctx.turn_journal.terminal("turn_done", "completed")

    first = client.post("/api/turns/turn_lost/resend")
    assert first.status_code == 200, first.text
    assert first.json()["recovered_turn_id"] == "turn_lost"
    assert first.json()["turn_id"] != "turn_lost"

    again = client.post("/api/turns/turn_lost/resend")
    assert again.status_code == 409, again.text

    done = client.post("/api/turns/turn_done/resend")
    assert done.status_code == 409, done.text

    # 重发之后就不再提示；消息原文仍留在台账里（没删用户数据）
    unfinished = {r["turn_id"] for r in ctx.turn_journal.unfinished()}
    assert "turn_lost" not in unfinished
    row = ctx.conn.execute(
        "SELECT message, recovered_by FROM turn_journal WHERE turn_id='turn_lost'"
    ).fetchone()
    assert row["message"] == "这条没有执行"
    assert row["recovered_by"]


def test_dismiss_acknowledges_without_executing(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("忽略话题").id
    _seed_interrupted(ctx, "turn_lost2", "不想重发", topic)
    ctx.turn_journal.interrupt_stale()

    assert client.post("/api/turns/turn_lost2/dismiss").status_code == 200
    assert client.post("/api/turns/turn_lost2/dismiss").status_code == 409
    assert ctx.turn_journal.unfinished() == []
    assert ctx.turns.snapshot()["queued"] == []


# -- 系统通知轮（notify=1）：入口、重发、知道了都不放行（第二阶段修复） --------
#
# 缺陷：unfinished() 带了 notify = 0，而 recoverable() / claim() 没带 —— 同一个概念
# 两份口径。对系统通知轮直接调 resend 会 200 并且真的再提交一条系统消息（实测
# {"ok":true,...,"status":"accepted"}），dismiss 也会被放行。现在四处共用一个权威谓词
# （turn_journal._RECOVERABLE_CLAUSE）。


def test_notify_rows_are_not_recoverable_and_cannot_be_claimed(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_notify", message="系统通知", topic_id="t1", notify=True)
    journal.running("turn_notify")
    journal.accepted(turn_id="turn_user", message="用户的消息", topic_id="t1")
    journal.running("turn_user")
    journal.interrupt_stale()

    assert journal.recoverable("turn_notify") is None
    # 就算有人绕过 recoverable() 直接抢，也抢不到
    assert journal.claim("turn_notify") is False
    assert journal.mark_recovered("turn_notify", new_turn_id="turn_x") is False
    row = db_conn.execute(
        "SELECT status, recovered_at, recovered_by FROM turn_journal WHERE turn_id='turn_notify'"
    ).fetchone()
    assert row["status"] == INTERRUPTED
    assert row["recovered_at"] is None and row["recovered_by"] is None

    # 对照组：notify=0 的用户消息照旧可恢复、可抢占（行为不变）
    assert journal.recoverable("turn_user") is not None
    assert journal.claim("turn_user") is True


def test_unfinished_still_lists_only_user_rows(db_conn: sqlite3.Connection):
    """入口的返回不变：通知轮仍然不出现。"""
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_notify", message="系统通知", topic_id="t1", notify=True)
    journal.running("turn_notify")
    journal.accepted(turn_id="turn_user", message="用户的消息", topic_id="t1")
    journal.running("turn_user")
    journal.interrupt_stale()

    assert [row["turn_id"] for row in journal.unfinished()] == ["turn_user"]


def test_notify_rows_cannot_be_resent_or_dismissed_over_http(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("通知话题").id
    _seed_interrupted(ctx, "turn_notify", "系统通知轮", topic, notify=True)
    _seed_interrupted(ctx, "turn_notify2", "系统通知轮二", topic, notify=True)
    _seed_interrupted(ctx, "turn_user", "用户的消息", topic)
    ctx.turn_journal.interrupt_stale()

    # 入口里没有它们（现状保留）
    state = client.get("/api/runtime/state").json()
    assert {row["turn_id"] for row in state["interrupted_turns"]} == {"turn_user"}

    resend = client.post("/api/turns/turn_notify/resend")
    assert resend.status_code == 409, resend.text
    dismiss = client.post("/api/turns/turn_notify2/dismiss")
    assert dismiss.status_code == 409, dismiss.text
    # 没有偷偷提交新 turn，行也没有被标成「已处理」
    assert ctx.turns.snapshot()["queued"] == []
    row = ctx.conn.execute(
        "SELECT recovered_at, recovered_by FROM turn_journal WHERE turn_id='turn_notify'"
    ).fetchone()
    assert row["recovered_at"] is None and row["recovered_by"] is None

    # 对照组：notify=0 的用户消息行为完全一样（一次性重发 / 一次性知道了）
    assert client.post("/api/turns/turn_user/resend").status_code == 200
    assert client.post("/api/turns/turn_user/resend").status_code == 409


def test_user_rows_are_still_dismissable_once(client):
    """notify=0 的回归：知道了仍然只成功一次，且不再出现在入口。"""
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("忽略话题二").id
    _seed_interrupted(ctx, "turn_user2", "用户的消息二", topic)
    ctx.turn_journal.interrupt_stale()

    assert client.post("/api/turns/turn_user2/dismiss").status_code == 200
    assert client.post("/api/turns/turn_user2/dismiss").status_code == 409
    assert ctx.turn_journal.unfinished() == []

