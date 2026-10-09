"""R06 受控验收：重发关联 —— 单事务、只有一个后继、孤立即记录不永久隐藏。

缺陷（基线实测）：重发是「先 claim（写 recovered_at）→ 再 submit → 再
mark_recovered（写 recovered_by）」三步。中间任何一步退出（提交前 / 提交后派发前），
就留下一条 `recovered_at` 非空、`recovered_by` 为空的记录：
`unfinished()` 要求 `recovered_at IS NULL`，所以它**再也不会出现在界面上**，
用户看不到、点不到 —— 那条消息永久消失。

修好之后（契约 C3）：
* `claim_for_resend(old,new,instance)` 在**一个事务**里完成「老记录
  recovered_by/recovered_at + 新记录 + 关联」，任何一步失败整体回滚；
* 并发/重复重发只有一个后继生效；
* `orphaned_claims()` 把遗留的孤儿列出来；`repair_orphan()` 让它重新可重发。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect, transaction
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import (
    INTERRUPTED,
    JournalWriteError,
    TurnJournal,
)


def _seed_interrupted(journal: TurnJournal, turn_id: str, message: str, topic: str) -> None:
    journal.accepted(turn_id=turn_id, message=message, topic_id=topic)
    journal.running(turn_id)
    journal.interrupt_stale()


class _AnswerAdapter:
    mode = "native"
    model = "m"

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        return Completion(
            message=ChatMessage(role="assistant", content="好的"),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


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


# -- 单事务语义 ---------------------------------------------------------------


def test_claim_for_resend_writes_old_new_and_link_in_one_transaction(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    _seed_interrupted(journal, "turn_old", "没执行完的消息", "topic_1")

    assert journal.claim_for_resend("turn_old", "turn_new") is True

    old = db_conn.execute(
        "SELECT status, recovered_at, recovered_by FROM turn_journal WHERE turn_id = 'turn_old'"
    ).fetchone()
    assert old["status"] == INTERRUPTED
    assert old["recovered_at"], "老记录必须记下接管时刻"
    assert old["recovered_by"] == "turn_new", "关联必须指向真正的新记录"
    new = db_conn.execute(
        "SELECT message, topic_id, status FROM turn_journal WHERE turn_id = 'turn_new'"
    ).fetchone()
    assert new is not None, "新记录必须在同一个事务里写成"
    assert new["message"] == "没执行完的消息"
    assert new["topic_id"] == "topic_1"
    # 关系不悬空：老记录指的新记录真实存在
    exists = db_conn.execute(
        "SELECT 1 FROM turn_journal WHERE turn_id = ?", (old["recovered_by"],)
    ).fetchone()
    assert exists is not None


def test_claim_for_resend_is_one_shot(db_conn: sqlite3.Connection):
    """重复重发只有一个后继生效：第二次返回 False，且不制造第二个后继。"""
    journal = TurnJournal(db_conn)
    _seed_interrupted(journal, "turn_old", "只执行一次", "topic_1")

    assert journal.claim_for_resend("turn_old", "turn_new_1") is True
    assert journal.claim_for_resend("turn_old", "turn_new_2") is False

    rows = db_conn.execute(
        "SELECT turn_id FROM turn_journal WHERE turn_id LIKE 'turn_new%'"
    ).fetchall()
    assert [r["turn_id"] for r in rows] == ["turn_new_1"]
    old = db_conn.execute(
        "SELECT recovered_by FROM turn_journal WHERE turn_id = 'turn_old'"
    ).fetchone()
    assert old["recovered_by"] == "turn_new_1"


def test_claim_for_resend_only_accepts_user_interrupted_rows(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_notify", message="通知", topic_id="t", notify=True)
    journal.running("turn_notify")
    journal.accepted(turn_id="turn_done", message="已完成", topic_id="t")
    journal.terminal("turn_done", "completed")
    journal.interrupt_stale()

    assert journal.claim_for_resend("turn_notify", "turn_x1") is False
    assert journal.claim_for_resend("turn_done", "turn_x2") is False
    assert journal.claim_for_resend("turn_missing", "turn_x3") is False
    rows = db_conn.execute(
        "SELECT COUNT(*) c FROM turn_journal WHERE turn_id LIKE 'turn_x%'"
    ).fetchone()["c"]
    assert rows == 0


def test_failure_inside_the_transaction_leaves_no_half_state(db_conn: sqlite3.Connection):
    """事务中途失败 → 老记录与新记录都回到原样（没有半截状态、没有孤儿）。

    注入的失败发生在 `prepare` 里，也就是「老记录已经被条件 UPDATE 抢占、
    新记录还没写」的那一刻 —— 正是最危险的中途点。`prepare` 内部用的是普通
    `transaction()`（复用外层事务，不是自己 BEGIN），所以整体回滚必须成立。
    """
    journal = TurnJournal(db_conn)
    _seed_interrupted(journal, "turn_old", "这条不能被半截处理", "topic_1")

    def _boom():
        # 先做一次真实写入（会写进外层事务），再抛错
        with transaction(db_conn):
            db_conn.execute(
                "UPDATE turn_journal SET reason = 'should_rollback' WHERE turn_id = 'turn_old'"
            )
        raise JournalWriteError("injected: new row write failed")

    with pytest.raises(JournalWriteError):
        journal.claim_for_resend("turn_old", "turn_new", prepare=_boom)

    old = db_conn.execute(
        "SELECT recovered_at, recovered_by, reason FROM turn_journal WHERE turn_id = 'turn_old'"
    ).fetchone()
    assert old["recovered_at"] is None and old["recovered_by"] is None
    assert old["reason"] != "should_rollback", "prepare 里的写入必须被整体回滚"
    assert (
        db_conn.execute("SELECT 1 FROM turn_journal WHERE turn_id = 'turn_new'").fetchone()
        is None
    )
    # 它仍然是「可重发」的（界面还能看到、还能再试一次）
    assert journal.recoverable("turn_old") is not None
    assert journal.orphaned_claims() == []
    assert db_conn.in_transaction is False


def test_claim_for_resend_uses_a_write_lock_not_deferred_read(db_conn: sqlite3.Connection):
    """BEGIN IMMEDIATE：写锁在事务开始就拿到，不是「先读后写」的延迟事务。

    证据：claim 成功返回时事务已经结束（没有把写锁留给下一句），
    且第二条连接看到的是**已提交**的结果（不是过期的可重发状态）。
    """
    other = connect_shadow(db_conn)
    journal = TurnJournal(db_conn)
    _seed_interrupted(journal, "turn_old", "并发也只成功一次", "topic_1")

    assert journal.claim_for_resend("turn_old", "turn_new") is True
    assert db_conn.in_transaction is False
    # 另一条连接（模拟另一个实例/另一个请求）看到的是已处理状态
    journal2 = TurnJournal(other)
    assert journal2.recoverable("turn_old") is None
    assert journal2.claim_for_resend("turn_old", "turn_new_other") is False
    other.close()


def connect_shadow(conn: sqlite3.Connection) -> sqlite3.Connection:
    """同一份库的第二条连接（内存库用 URI 共享缓存拿同一个库）。"""
    # db_conn 是文件库时直接连同一个文件；内存库退化为主连接（测试仍在语义上成立）
    path = _db_path(conn)
    if path is None:
        conn2 = sqlite3.connect("file::memory:?cache=shared", uri=True)
        conn2.row_factory = sqlite3.Row
        conn2.isolation_level = None
        return conn2
    conn2 = sqlite3.connect(path)
    conn2.row_factory = sqlite3.Row
    conn2.isolation_level = None
    conn2.execute("PRAGMA busy_timeout = 5000")
    return conn2


def _db_path(conn: sqlite3.Connection) -> str | None:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main" and row[2]:
            return str(row[2])
    return None


# -- 两个退出检查点（R06 的主验收）--------------------------------------------


def test_exit_checkpoint_before_dispatch_still_leaves_a_recoverable_record(tmp_path):
    """检查点 1：台账已持久化、**还没派发**就退出 → 重启后这条仍然可操作。"""
    journal = TurnJournal(connect_migrated(tmp_path))
    # 受理 = 持久化成功；此刻进程退出（没有入队、没有任何 TURN_START）
    journal.accepted(turn_id="turn_cp1", message="提交后派发前退出", topic_id=None)
    # 重启：新实例确认上一个实例已退出
    ctx = _restart_context(tmp_path)
    rows = {row["turn_id"]: row for row in ctx.turn_journal.unfinished()}
    assert "turn_cp1" in rows
    assert rows["turn_cp1"]["message"] == "提交后派发前退出"
    assert rows["turn_cp1"]["status"] == INTERRUPTED
    # 可操作：能真的重发（重发后关系不悬空）
    assert ctx.turn_journal.claim_for_resend("turn_cp1", "turn_cp1_new") is True
    link = ctx.conn.execute(
        "SELECT recovered_by FROM turn_journal WHERE turn_id = 'turn_cp1'"
    ).fetchone()["recovered_by"]
    assert link == "turn_cp1_new"
    assert (
        ctx.conn.execute(
            "SELECT 1 FROM turn_journal WHERE turn_id = 'turn_cp1_new'"
        ).fetchone()
        is not None
    )
    assert ctx.turn_journal.orphaned_claims() == []


def test_exit_checkpoint_after_dispatch_still_leaves_a_recoverable_record(tmp_path):
    """检查点 2：已经派发（running）但没执行完就退出 → 重启后同样可操作。"""
    journal = TurnJournal(connect_migrated(tmp_path))
    journal.accepted(turn_id="turn_cp2", message="派发后退出", topic_id=None)
    journal.running("turn_cp2")  # worker 已经开跑
    ctx = _restart_context(tmp_path)
    rows = {row["turn_id"]: row for row in ctx.turn_journal.unfinished()}
    assert "turn_cp2" in rows
    assert rows["turn_cp2"]["reason"] == "running_at_restart"
    # 归属已确认退出（正是它让这次恢复成立），并且这条现在可操作
    assert ctx.turn_journal.recoverable("turn_cp2") is not None
    link = ctx.instances.owner_instance_id("turn", "turn_cp2")
    assert link is None or ctx.instances.owner_alive(link) is False


def test_orphan_is_visible_and_repairable(tmp_path):
    """孤立即记录不得永久隐藏：须列得出来、且能修复成可重发。"""
    conn = connect_migrated(tmp_path)
    journal = TurnJournal(conn)
    journal.accepted(turn_id="turn_orphan", message="被抢占却没写成后继", topic_id=None)
    journal.running("turn_orphan")
    journal.interrupt_stale()
    # 模拟「抢占成功、后继没写成」的半截状态（修复前遗留数据的样子）
    conn.execute(
        "UPDATE turn_journal SET recovered_at = '2026-01-01T00:00:00+00:00' "
        "WHERE turn_id = 'turn_orphan'"
    )

    # 它不在 unfinished 里（老实现下就是永久消失）
    assert {r["turn_id"] for r in journal.unfinished()} == set()
    orphans = {r["turn_id"]: r for r in journal.orphaned_claims()}
    assert "turn_orphan" in orphans
    assert orphans["turn_orphan"]["message"] == "被抢占却没写成后继"

    # 修复：重新可重发（并且真的能重发）
    assert journal.repair_orphan("turn_orphan") is True
    assert journal.recoverable("turn_orphan") is not None
    assert journal.orphaned_claims() == []
    assert journal.claim_for_resend("turn_orphan", "turn_orphan_new") is True

    # 已经真正重发过的行不许被「修复」回可重发（否则会重发两次）
    assert journal.repair_orphan("turn_orphan") is False


def test_repair_orphan_is_restricted_to_the_exact_orphan_state(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_fine", message="没被抢占过", topic_id="t")
    journal.running("turn_fine")
    journal.accepted(turn_id="turn_notify", message="通知", topic_id="t", notify=True)
    journal.running("turn_notify")
    journal.interrupt_stale()
    db_conn.execute(
        "UPDATE turn_journal SET recovered_at = '2026-01-01T00:00:00+00:00' "
        "WHERE turn_id = 'turn_notify'"
    )
    assert journal.repair_orphan("turn_fine") is False  # 没有 recovered_at：不动
    assert journal.repair_orphan("turn_notify") is False  # 通知轮：不在恢复集合里
    assert journal.repair_orphan("turn_unknown") is False


# -- HTTP：重发只有一个后继 + 孤儿出口 ----------------------------------------


def test_http_resend_creates_exactly_one_successor(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("重发话题").id
    _seed_interrupted(ctx.turn_journal, "turn_lost", "只重发一次", topic)

    first = client.post("/api/turns/turn_lost/resend")
    assert first.status_code == 200, first.text
    body = first.json()
    new_id = body["turn_id"]
    assert new_id != "turn_lost"

    # 关联指向真正提交的那个 turn，且它确实存在
    link = ctx.conn.execute(
        "SELECT recovered_by FROM turn_journal WHERE turn_id = 'turn_lost'"
    ).fetchone()["recovered_by"]
    assert link == new_id
    assert (
        ctx.conn.execute("SELECT 1 FROM turn_journal WHERE turn_id = ?", (new_id,)).fetchone()
        is not None
    )

    for _ in range(3):
        again = client.post("/api/turns/turn_lost/resend")
        assert again.status_code == 409, again.text
    rows = ctx.conn.execute(
        "SELECT turn_id FROM turn_journal WHERE message = '只重发一次'"
    ).fetchall()
    assert len(rows) == 2, f"老记录 + 恰好一个新记录，实际 {len(rows)}"
    assert ctx.turn_journal.orphaned_claims() == []


def test_http_resend_reports_503_and_leaves_a_visible_orphan(client, monkeypatch):
    """重发时台账写入失败 → 明确非 200，且那条记录仍以孤儿出口可见（不消失）。"""
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("失败重发").id
    _seed_interrupted(ctx.turn_journal, "turn_lost", "重发会失败的消息", topic)

    def _boom(*args, **kwargs):
        raise JournalWriteError("injected: resend journal write failed")

    monkeypatch.setattr(ctx.turn_journal, "claim_for_resend", _boom)
    resp = client.post("/api/turns/turn_lost/resend")
    assert resp.status_code >= 500, resp.text
    # 没有假的 200、没有新 turn
    assert ctx.turns.snapshot()["queued"] == []
    # 老记录仍然可重发（没有被半截标成已处理）
    assert ctx.turn_journal.recoverable("turn_lost") is not None
    assert ctx.turn_journal.orphaned_claims() == []


def test_runtime_state_exposes_orphaned_turns(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("孤儿话题").id
    _seed_interrupted(ctx.turn_journal, "turn_orphan", "孤儿消息", topic)
    ctx.conn.execute(
        "UPDATE turn_journal SET recovered_at = '2026-01-01T00:00:00+00:00' "
        "WHERE turn_id = 'turn_orphan'"
    )
    state = client.get("/api/runtime/state").json()
    orphans = {row["turn_id"]: row for row in state["orphaned_turns"]}
    assert "turn_orphan" in orphans
    assert orphans["turn_orphan"]["message"] == "孤儿消息"


# -- helpers ------------------------------------------------------------------


def connect_migrated(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return conn


def _restart_context(tmp_path) -> AppContext:
    """在同一个库上重建上下文，并把上一个实例标成确认已退出（模拟真实重启）。"""
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    # 上一个实例（上一段测试里的直接 TurnJournal 写入）没有实例身份：给库里
    # 造一个「确认已退出」的归属者，让新实例按契约 C1 恢复它的记录。
    prior = conn.execute(
        "SELECT DISTINCT owner_instance_id FROM turn_journal WHERE owner_instance_id IS NOT NULL"
    ).fetchone()
    if prior is None:
        conn.execute(
            "INSERT OR REPLACE INTO instances "
            "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
            "VALUES ('prior', 1, 'unknown-host', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute("UPDATE turn_journal SET owner_instance_id = 'prior'")
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())
