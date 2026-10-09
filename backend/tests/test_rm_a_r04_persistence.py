"""R04 受控验收：持久接受 —— 持久化失败必须**拒绝**，不许返回假的 200。

缺陷（基线实测）：`TurnJournal.accepted()` 走 `_execute()`，把所有
`sqlite3.Error` 吞成一条 warning。于是 `POST /api/turns` 在库写不进去时
仍然返回 `200 {"ok":true,"accepted":true}`，内存里留下一个永远执行不完的 turn，
而库里**什么都没有** —— 重启后这条消息没有任何痕迹，用户却以为发出去了。

修好之后（契约 C2）：
* `accepted` 写失败抛 `JournalWriteError`；
* `TurnManager.submit` 顺序固定 persist → dispatch，失败抛 `TurnAcceptError`
  （既不入队，也不会有 worker 去跑它）；
* `POST /api/turns` → HTTP 503 + `{ok:false, accepted:false, error:"消息未被接受：持久化失败"}`。

故障注入是受控的：不制造真实崩溃，只在**第二条消息**的台账写入处注入一次失败。
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
from agent.core.turn import TURN_START, TurnAcceptError, TurnManager
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import JournalWriteError, TurnJournal


class _AnswerAdapter:
    mode = "native"
    model = "m"

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        return Completion(
            message=ChatMessage(role="assistant", content="好的"),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


class _FailNTimes:
    """在真实台账外面包一层：第 N 次 `accepted` 注入一次写失败。

    只影响台账这一个方法，其它行为完全走真实实现（不 mock 数据库本身）。
    """

    def __init__(self, journal, fail_on: int) -> None:
        self.journal = journal
        self.fail_on = int(fail_on)
        self.calls = 0
        self.attempts: list[str] = []

    def accepted(self, **kwargs):
        self.calls += 1
        self.attempts.append(str(kwargs.get("message")))
        if self.calls == self.fail_on:
            raise JournalWriteError("injected: journal write failed")
        return self.journal.accepted(**kwargs)

    def __getattr__(self, name):  # 其余方法透传（running / terminal / ...）
        return getattr(self.journal, name)


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


# -- 单元层：顺序与异常语义 ---------------------------------------------------


def test_accepted_write_failure_raises_journal_write_error(db_conn: sqlite3.Connection):
    journal = TurnJournal(db_conn)
    db_conn.execute("DROP TABLE turn_journal")  # 写入必然失败
    with pytest.raises(JournalWriteError):
        journal.accepted(turn_id="turn_x", message="这条不会被接受")


async def test_submit_persists_before_dispatch_and_refuses_when_write_fails():
    order: list[str] = []

    class _Journal:
        def accepted(self, **kwargs):
            order.append("persist")
            raise JournalWriteError("boom")

    ran: list[str] = []

    async def runner(ctx):  # pragma: no cover - 不该被调用
        ran.append(ctx.turn_id)

    tm = TurnManager(runner, journal=_Journal())
    with pytest.raises(TurnAcceptError):
        tm.submit("库写不进去的消息")
    assert order == ["persist"]
    # 没有入队、没有 worker、没有执行
    assert tm.queued_count() == 0
    assert tm.active is None
    await asyncio.sleep(0)
    assert ran == []


async def test_submit_records_the_row_before_the_runner_runs():
    """成功路径的顺序固定为 persist → dispatch：runner 开跑时库里已经有这一行。

    观察点放在 **runner 内部**（= 派发那一刻），而不是台账调用参数上：
    要证明的正是「派发时这一行已经落库」。
    """
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    journal = TurnJournal(conn)
    journal.registry = None
    order: list[str] = []
    observed: list[object] = []

    class _TracingJournal:
        """真实台账 + 顺序记录（不改变任何写入行为）。"""

        def accepted(self, **kwargs):
            order.append("persist")
            return journal.accepted(**kwargs)

        def __getattr__(self, name):
            return getattr(journal, name)

    async def runner(ctx):
        order.append("dispatch")
        observed.append(
            conn.execute(
                "SELECT status FROM turn_journal WHERE turn_id = ?", (ctx.turn_id,)
            ).fetchone()
        )

    tm = TurnManager(runner, journal=_TracingJournal())
    tm.submit("第一条")
    for _ in range(50):  # 让出控制权等 worker 跑完，不长时间 sleep
        if observed:
            break
        await asyncio.sleep(0.01)
    assert order[:2] == ["persist", "dispatch"], order
    assert observed and observed[0] is not None, "派发时这一行必须已经在库里"
    # 行已经存在（status 此刻可能已被 worker 推进到 running）；
    # 「存在」这一点就是 persist → dispatch 顺序的证据。
    assert observed[0]["status"] in ("accepted", "queued", "running")
    await tm.shutdown()
    conn.close()


async def test_runner_never_runs_when_the_second_write_fails():
    """第二条消息注入失败：第一条照常跑完，第二条连跑都没跑。"""
    ran: list[str] = []

    class _Journal:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def accepted(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise JournalWriteError("injected")
            return self.inner.accepted(**kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    from agent.storage.migrate import apply_migrations as _apply

    _apply(conn)
    journal = TurnJournal(conn)
    gate = asyncio.Event()

    async def runner(ctx):
        ran.append(ctx.message)
        if ctx.message == "第一条":
            await gate.wait()

    tm = TurnManager(runner, journal=_Journal(journal))
    first = tm.submit("第一条")
    await asyncio.sleep(0.05)
    with pytest.raises(TurnAcceptError):
        tm.submit("第二条")
    gate.set()
    await tm.wait(first.turn_id, timeout=5)
    await tm.shutdown()
    assert ran == ["第一条"], "失败的第二条绝不能被运行器执行"
    rows = conn.execute("SELECT turn_id FROM turn_journal").fetchall()
    assert len(rows) == 1
    conn.close()


# -- HTTP 层：503 + 无假接受 --------------------------------------------------


def test_http_turns_returns_503_and_does_not_accept_when_persist_fails(client):
    ctx = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("R04 话题").id
    executed: list[str] = []

    async def runner(turn_ctx):  # 替换真实 runner：只记录有没有被调用
        executed.append(turn_ctx.message)

    ctx.turns.set_runner(runner)
    failing = _FailNTimes(ctx.turn_journal, fail_on=2)
    ctx.turn_journal = failing
    ctx.turns.set_journal(failing)

    # 第一条：正常保存
    first = client.post("/api/turns", json={"message": "第一条消息", "topic_id": topic})
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] is True

    # 第二条：台账写入注入一次失败
    second = client.post("/api/turns", json={"message": "第二条消息", "topic_id": topic})
    assert second.status_code == 503, second.text
    body = second.json()
    assert body["ok"] is False
    assert body["accepted"] is False
    assert body["error"] == "消息未被接受：持久化失败"

    # 无内存假接受：队列里没有它，运行器也没有执行过它
    snap = ctx.turns.snapshot()
    assert snap["queued"] == []
    assert all("第二条消息" != item["message"] for item in [snap["running"]] if item)
    assert "第二条消息" not in executed
    # 库里也没有这条消息（它是被拒绝的，不是「接受了但没执行」）
    rows = [
        r["message"]
        for r in ctx.conn.execute("SELECT message FROM turn_journal").fetchall()
    ]
    assert "第二条消息" not in rows
    assert "第一条消息" in rows


def test_http_turns_failure_emits_no_turn_start(client):
    """503 那条消息不许发 TURN_START（TURN_START 只在 worker 真正开跑时发）。"""
    ctx = client.app.state.ctx
    events: list[dict] = []

    async def emitter(name, data):
        if name == TURN_START:
            events.append(data)

    ctx.turns.set_emitter(emitter)
    failing = _FailNTimes(ctx.turn_journal, fail_on=1)
    ctx.turn_journal = failing
    ctx.turns.set_journal(failing)

    resp = client.post("/api/turns", json={"message": "这条不会被接受"})
    assert resp.status_code == 503
    assert events == []


def test_accepted_message_survives_an_immediate_restart(tmp_path):
    """正常保存后立即退出，重启仍能找回这条消息（并可恢复）。"""
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ctx1 = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx1.credentials._kr = MemoryKeyring()

    async def _build():
        return _AnswerAdapter()

    ctx1.build_adapter = _build  # type: ignore[assignment]

    async def _accept_only():
        # 只提交、不等执行完；队列里那条消息只活在台账里
        ctx1.turn_journal.accepted(
            turn_id="turn_saved", message="保存后立刻退出", topic_id=None
        )
        await ctx1.turns.shutdown()  # 模拟立刻退出（真实关闭路径）

    asyncio.run(_accept_only())
    ctx1.instances.mark_clean_exit()

    conn2 = connect(tmp_path / "app.db")
    apply_migrations(conn2)
    ctx2 = AppContext(Settings(data_dir=tmp_path), conn2, EventBus())
    lost = {row["turn_id"]: row for row in ctx2.turn_journal.unfinished()}
    assert "turn_saved" in lost, "正常保存过的消息重启后必须还在"
    assert lost["turn_saved"]["message"] == "保存后立刻退出"
    assert lost["turn_saved"]["status"] == "interrupted"


def test_accept_write_failure_leaves_no_partial_row(db_conn: sqlite3.Connection):
    """受理写入失败不留半截行（写入用 `transaction()` 而不是 `with conn:`）。

    这里注入的是「同一个 turn_id 已经存在、但用的是普通 INSERT」这种真实会抛
    IntegrityError 的写入：它必须变成 `JournalWriteError`，且库里那一行保持原样。
    """
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_ok", message="这条是好的")

    class _StrictInsert:
        """把 INSERT OR IGNORE 换成普通 INSERT：重复 id 就真的报错。"""

        def __init__(self, conn):
            self._conn = conn

        @property
        def in_transaction(self) -> bool:
            return bool(self._conn.in_transaction)

        def execute(self, sql, params=()):
            return self._conn.execute(sql.replace("INSERT OR IGNORE", "INSERT"), params)

    journal.conn = _StrictInsert(db_conn)  # type: ignore[assignment]
    with pytest.raises(JournalWriteError):
        journal.accepted(turn_id="turn_ok", message="重复 id")

    row = db_conn.execute(
        "SELECT message FROM turn_journal WHERE turn_id = 'turn_ok'"
    ).fetchone()
    assert row["message"] == "这条是好的", "失败的那次写入不得改到已有行"
    assert db_conn.execute("SELECT COUNT(*) c FROM turn_journal").fetchone()["c"] == 1
