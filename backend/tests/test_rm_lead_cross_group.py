"""Lead 亲自复验：跨组端到端路径（只按可观察行为断言，不依赖各组内部实现）。

为什么单独写这一组：各组只能验自己那一摊。**真正会出事的是接缝**——实例归属 ×
台账 × 审批 × 派生队列、受理 × 重发、知识版本链 × 管理 API、预算闸门 × 统一记账、
运行快照。这些路径只有集成之后才跑得起来，所以由 Lead 在这里钉住。

覆盖：
  L-V1 多实例恢复：第二个实例启动不动活跃实例的记录；确认退出后才恢复
  L-V2 可靠接受：台账写失败 → HTTP 503、无假接受、台账不留痕
  L-V3 重发与孤儿出口：抢占后崩溃不永久隐藏；修复后可重发一次、再重发 409
  L-V4 知识原子替换：连续纠正只有一个当前版本；激活失败整体回滚
  L-V5 预算与记账：已确认耗尽后真实 provider 请求不再发生；总账等于真实响应之和
  L-V6 运行快照：新连接（无游标）能拿到待确认事项与全部必需字段

纪律：受控时钟 / 一次性故障注入；不联网、不用真实 Key、不长时间等待。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.adapters.native import NativeAdapter
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.policy import BudgetExhausted, remaining_budget
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import accounting_snapshot, bind_request_accounting
from agent.services import derived_tasks as dt
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.instance_registry import HEARTBEAT_TTL_SECONDS
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import JournalWriteError, TurnJournal

GHOST_PID = 999_999_999
FAKE_SECRET = "sk-lv-not-a-real-key"


class _FixedUsageAdapter:
    mode = "native"
    model = "m"

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        return Completion(
            message=ChatMessage(role="assistant", content="好的"),
            usage={"prompt_tokens": 30, "completion_tokens": 15},
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open(tmp_path, name: str = "app.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    return conn


def _ctx(tmp_path, conn: sqlite3.Connection) -> AppContext:
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _app(tmp_path, conn: sqlite3.Connection):
    app = create_app(Settings(data_dir=tmp_path), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    return app, app.state.ctx


def _snapshot_rows(conn: sqlite3.Connection) -> dict:
    return {
        "turns": sorted(
            (r["turn_id"], r["status"], r["owner_instance_id"], r["recovered_by"])
            for r in conn.execute(
                "SELECT turn_id, status, owner_instance_id, recovered_by FROM turn_journal"
            )
        ),
        "approvals": sorted(
            (r["approval_id"], r["status"])
            for r in conn.execute("SELECT approval_id, status FROM pending_approvals")
        ),
        "tasks": sorted(
            (r["id"], r["state"], r["owner_instance_id"])
            for r in conn.execute("SELECT id, state, owner_instance_id FROM derived_tasks")
        ),
    }


# ---------------------------------------------------------------------------
# L-V1 多实例恢复：第二个实例不动活跃实例的记录
# ---------------------------------------------------------------------------


def test_lv1_second_instance_does_not_touch_a_live_instances_work(tmp_path):
    conn = _open(tmp_path)
    ctx_a = _ctx(tmp_path, conn)
    a = ctx_a.instance_id

    ctx_a.turn_journal.accepted(turn_id="turn_a", message="A 正在跑的消息", topic_id=None)
    conn.execute(
        "UPDATE turn_journal SET status = 'running', owner_instance_id = ? WHERE turn_id = 'turn_a'",
        (a,),
    )
    conn.execute(
        "INSERT INTO pending_approvals (approval_id, kind, payload, turn_id, session_id, "
        "created_at, status, owner_instance_id) "
        "VALUES ('appr_a', 'tool_execution', '{}', 'turn_a', 's1', ?, 'pending', ?)",
        (_now().isoformat(), a),
    )
    task_id, created = dt.enqueue(conn, dt.KIND_SUMMARY, "frag_lv1", 1)
    assert created is True
    # 用生产同一条认领路径（同时写 owner 列与 record_owners 归属表），
    # 不要用裸 SQL 只写列：启动恢复是按归属表驱动的。
    claimed = dt.claim_due(conn, instance_id=a)
    assert [t.id for t in claimed] == [task_id]
    assert dt.task_for(conn, dt.KIND_SUMMARY, "frag_lv1", 1).state == dt.STATE_RUNNING

    before = _snapshot_rows(conn)

    ctx_b = _ctx(tmp_path, conn)  # B 启动（同一份库），A 仍是活实例
    assert ctx_b.instance_id != a

    assert _snapshot_rows(conn) == before, "第二个实例启动不得改动活跃实例的记录"
    row_a = conn.execute(
        "SELECT status, owner_instance_id FROM turn_journal WHERE turn_id = 'turn_a'"
    ).fetchone()
    assert row_a["status"] == "running" and row_a["owner_instance_id"] == a, (
        "活跃实例正在跑的那一轮不得被改成 interrupted"
    )
    approvals = {
        r["approval_id"]: r["status"]
        for r in conn.execute("SELECT approval_id, status FROM pending_approvals")
    }
    assert approvals["appr_a"] == "pending", "活跃实例的待确认事项不得被判成中断"
    assert dt.task_for(conn, dt.KIND_SUMMARY, "frag_lv1", 1).state == dt.STATE_RUNNING

    # unknown（心跳过期、但 pid 确实存在）也不得改状态：契约只在
    # 「心跳过期 **且** pid 不存在」时才判死，其余一律是未知。
    conn.execute(
        "UPDATE instances SET last_heartbeat = ?, pid = ? WHERE instance_id = ?",
        ((_now() - timedelta(seconds=HEARTBEAT_TTL_SECONDS * 4)).isoformat(), os.getpid(), a),
    )
    ctx_u = _ctx(tmp_path, conn)
    assert ctx_u.instance_id not in {a, ctx_b.instance_id}
    assert dt.task_for(conn, dt.KIND_SUMMARY, "frag_lv1", 1).state == dt.STATE_RUNNING, (
        "判不出来时不得把 running 任务当成可恢复"
    )

    # A 真正退出之后，重启才恢复
    ctx_a.instances.mark_clean_exit()
    _ctx(tmp_path, conn)
    assert dt.task_for(conn, dt.KIND_SUMMARY, "frag_lv1", 1).state == dt.STATE_PENDING, (
        "确认已退出实例的派生任务应当可恢复"
    )


# ---------------------------------------------------------------------------
# L-V2 可靠接受：台账写失败不得返回假成功
# ---------------------------------------------------------------------------


def test_lv2_journal_write_failure_is_never_reported_as_accepted(tmp_path):
    conn = _open(tmp_path)
    app, ctx = _app(tmp_path, conn)

    async def _adapter():
        return _FixedUsageAdapter()

    ctx.build_adapter = _adapter  # type: ignore[assignment]

    with TestClient(app) as client:
        real_accepted = ctx.turn_journal.accepted
        state = {"n": 0}

        def _flaky(**kwargs):
            state["n"] += 1
            if state["n"] == 2:
                raise JournalWriteError("injected: 第二条消息的台账写失败")
            return real_accepted(**kwargs)

        ctx.turn_journal.accepted = _flaky  # type: ignore[assignment]

        first = client.post("/api/turns", json={"message": "第一条消息"})
        assert first.status_code == 200, first.text

        second = client.post("/api/turns", json={"message": "第二条消息"})
        assert second.status_code == 503, (
            f"持久化失败必须以非 200 反馈，实际 {second.status_code} / {second.text}"
        )
        assert second.json().get("accepted") is False
        queued = ctx.turns.snapshot()["queued"]
        assert all("第二条消息" != q.get("message") for q in queued), "不得有假接受项"
        rows = [r["message"] for r in conn.execute("SELECT message FROM turn_journal")]
        assert "第二条消息" not in rows, "未被接受的消息不该在台账里留下痕迹"
        assert rows.count("第一条消息") == 1


# ---------------------------------------------------------------------------
# L-V3 重发：孤儿不永久隐藏 + 只有一个后继
# ---------------------------------------------------------------------------


def test_lv3_orphan_is_visible_repairable_and_resend_is_one_shot(tmp_path):
    conn = _open(tmp_path)
    app, ctx = _app(tmp_path, conn)
    with TestClient(app) as client:
        journal = ctx.turn_journal
        journal.accepted(turn_id="turn_crash", message="抢占后崩溃的消息", topic_id=None)
        # 孤儿 = 用户可见的 interrupted 行 + 抢占已提交(recovered_at 有值) + 无后继
        conn.execute(
            "UPDATE turn_journal SET status = 'interrupted', recovered_at = ? WHERE turn_id = ?",
            (_now().isoformat(), "turn_crash"),
        )

        state = client.get("/api/runtime/state").json()
        visible = {r["turn_id"] for r in state.get("orphaned_turns", [])} | {
            r["turn_id"] for r in state.get("interrupted_turns", [])
        }
        assert "turn_crash" in visible, (
            f"孤儿记录永久不可见：orphaned={state.get('orphaned_turns')} "
            f"interrupted={state.get('interrupted_turns')}"
        )

        assert journal.repair_orphan("turn_crash", ctx.instance_id) is True
        fresh = client.get("/api/runtime/state").json()
        assert "turn_crash" in {r["turn_id"] for r in fresh.get("interrupted_turns", [])}, (
            "修复后必须重新变成可操作的恢复记录"
        )

        first = client.post("/api/turns/turn_crash/resend")
        assert first.status_code == 200, first.text
        new_id = first.json()["turn_id"]
        link = conn.execute(
            "SELECT recovered_by, recovered_at FROM turn_journal WHERE turn_id = 'turn_crash'"
        ).fetchone()
        assert link["recovered_by"] == new_id and link["recovered_at"]
        assert (
            conn.execute("SELECT 1 FROM turn_journal WHERE turn_id = ?", (new_id,)).fetchone()
            is not None
        )
        for _ in range(2):
            assert client.post("/api/turns/turn_crash/resend").status_code == 409
        assert journal.orphaned_claims() == []


# ---------------------------------------------------------------------------
# L-V4 知识原子替换：只有一个当前版本
# ---------------------------------------------------------------------------


def _chain_members(conn: sqlite3.Connection, any_id: str) -> set[str]:
    ids = {any_id}
    frontier = [any_id]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for row in conn.execute(
                "SELECT id, supersedes_id FROM knowledge WHERE supersedes_id = ? OR id = ?",
                (node, node),
            ).fetchall():
                if row["supersedes_id"]:
                    ids.add(str(row["supersedes_id"]))
                if str(row["id"]) not in ids:
                    ids.add(str(row["id"]))
                    nxt.append(str(row["id"]))
        frontier = nxt
    return ids


def _chain_actives(conn: sqlite3.Connection, any_id: str) -> list[str]:
    ids = _chain_members(conn, any_id)
    rows = conn.execute(
        "SELECT id FROM knowledge WHERE state = 'active' AND id IN "
        f"({','.join('?' for _ in ids)})",
        tuple(ids),
    ).fetchall()
    return sorted(str(r["id"]) for r in rows)


def test_lv4_revise_keeps_exactly_one_active_and_rolls_back_on_failure(tmp_path):
    conn = _open(tmp_path)
    app, ctx = _app(tmp_path, conn)
    with TestClient(app) as client:
        created = client.post(
            "/api/knowledge", json={"category": "general_fact", "content": "第一版事实"}
        )
        assert created.status_code == 200, created.text
        first_id = created.json()["knowledge"]["id"]

        first = client.post(f"/api/knowledge/{first_id}/revise", json={"content": "第二版事实"})
        assert first.status_code == 200, first.text
        second_id = first.json()["knowledge_id"]

        # 第二次纠正必须作用在**当前版本**上（拿已被取代的旧行当目标是陈旧纠正，
        # 那条路径由下面的 409 断言单独验证）
        second = client.post(f"/api/knowledge/{second_id}/revise", json={"content": "第三版事实"})
        assert second.status_code == 200, second.text
        third_id = second.json()["knowledge_id"]

        actives = _chain_actives(conn, first_id)
        assert len(actives) == 1, f"同一版本链出现多个当前版本：{actives}"
        assert actives == [third_id], f"当前版本应为最新一版，实际 {actives}"
        assert first_id not in actives and second_id not in actives

        # 陈旧纠正（拿已被取代的旧行当目标，并声明它当时的版本号）必须明确冲突，
        # 而不是悄悄再建一个 current
        stale = client.post(
            f"/api/knowledge/{first_id}/revise",
            json={"content": "拿旧版本再改一次", "expected_version": 1},
        )
        assert stale.status_code == 409, stale.text
        assert _chain_actives(conn, first_id) == [third_id]


def test_lv4_failed_activation_leaves_the_old_version_valid(tmp_path):
    conn = _open(tmp_path)
    app, ctx = _app(tmp_path, conn)
    # 真实 HTTP 客户端看到的是 5xx（不是崩溃）。TestClient 默认会把服务端异常
    # 直接抛回测试进程，所以这里显式关掉，才能观察到「客户端到底收到什么」。
    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post(
            "/api/knowledge", json={"category": "general_fact", "content": "原始事实"}
        )
        old_id = created.json()["knowledge"]["id"]

        # 在「把新行变成 active」这一步注入一次真实写失败：INSERT 与 UPDATE 两条
        # 激活路径都挡住，确保无论实现用哪种写法，激活都不会成功。
        conn.execute(
            "CREATE TRIGGER lv4_block_insert BEFORE INSERT ON knowledge "
            "WHEN NEW.state = 'active' "
            "BEGIN SELECT RAISE(ABORT, 'injected activation failure'); END"
        )
        conn.execute(
            "CREATE TRIGGER lv4_block_update BEFORE UPDATE ON knowledge "
            "WHEN NEW.state = 'active' AND OLD.state <> 'active' "
            "BEGIN SELECT RAISE(ABORT, 'injected activation failure'); END"
        )
        resp = client.post(f"/api/knowledge/{old_id}/revise", json={"content": "注入失败的新事实"})
        assert resp.status_code >= 500, (
            f"写失败必须以 5xx 反馈给客户端，实际 {resp.status_code} / {resp.text}"
        )

        old_state = conn.execute(
            "SELECT state FROM knowledge WHERE id = ?", (old_id,)
        ).fetchone()["state"]
        assert old_state == "active", "替换失败后旧版本必须仍是当前有效版本"
        assert _chain_actives(conn, old_id) == [old_id], "有效版本数不得变成 0"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge WHERE content = '注入失败的新事实'"
        ).fetchone()["n"] == 0
        conn.execute("DROP TRIGGER lv4_block_insert")
        conn.execute("DROP TRIGGER lv4_block_update")

        ok = client.post(f"/api/knowledge/{old_id}/revise", json={"content": "解除失败后的新事实"})
        assert ok.status_code == 200, ok.text
        assert _chain_actives(conn, old_id) == [ok.json()["knowledge_id"]]


# ---------------------------------------------------------------------------
# L-V5 预算与记账：耗尽后真实请求不再发生
# ---------------------------------------------------------------------------


class _FakeCompletion:
    def __init__(self, content: str, prompt: int, completion: int) -> None:
        class _Msg:
            def __init__(self) -> None:
                self.content = content
                self.tool_calls = None

        class _Choice:
            def __init__(self) -> None:
                self.message = _Msg()

        class _Usage:
            """与真实 SDK 一致：用量经 model_dump() 暴露（这是记账读的口子）。"""

            def __init__(self) -> None:
                self._payload = {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                }

            def model_dump(self) -> dict[str, int]:
                return dict(self._payload)

        self.choices = [_Choice()]
        self.usage = _Usage()


class _ScriptedClient:
    """假 SDK 客户端：真实 NativeAdapter 的预算闸门就在它之前。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def chat(self) -> "_ScriptedClient":
        return self

    @property
    def completions(self) -> "_ScriptedClient":
        return self

    async def create(self, **kwargs: Any) -> _FakeCompletion:
        self.calls.append(kwargs)
        return _FakeCompletion("（假响应）", prompt=30, completion=15)


def test_lv5_exhausted_budget_stops_real_provider_requests(tmp_path):
    conn = _open(tmp_path)
    store = CredentialStore(conn, keyring_backend=MemoryKeyring())
    store.create("k1", FAKE_SECRET, tags=["main-loop"], verify_state="verified", budget=45)

    client = _ScriptedClient()
    adapter = NativeAdapter(client=client, model="fake-model")
    adapter.key_id = "k1"
    bind_request_accounting(adapter, store)

    async def _one():
        return await adapter.complete(
            [ChatMessage(role="user", content="你好")], []
        )

    asyncio.run(_one())
    assert len(client.calls) == 1
    assert accounting_snapshot(adapter)["recorded"] == 1
    assert remaining_budget(store, "k1") == 0.0

    with pytest.raises(BudgetExhausted):
        asyncio.run(_one())
    assert len(client.calls) == 1, (
        f"用量已确认耗尽后不得再发真实请求，实际发了 {len(client.calls)} 次"
    )


# ---------------------------------------------------------------------------
# L-V6 运行快照：新连接（无游标）能拿到待确认事项与全部必需字段
# ---------------------------------------------------------------------------


def test_lv6_runtime_state_carries_approvals_and_all_required_fields(tmp_path):
    """新连接（无游标）必须能 discover 到「还在等用户」的事项与全部必需字段。

    待确认事项的**活体**住在内存里（`approvals.pending()` 只返回真的有人等的），
    所以恢复入口是「上一个实例留下的、没人回答的审批」——启动时按归属把它标成
    interrupted，客户端据此把「那一次操作没有执行」显示出来。
    """
    conn = _open(tmp_path)
    # 造一个确认已退出的前实例，留下一条没人回答的审批
    conn.execute(
        "INSERT OR REPLACE INTO instances "
        "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
        "VALUES ('prior', 1, 'h', ?, ?, ?)",
        (
            (_now() - timedelta(seconds=HEARTBEAT_TTL_SECONDS * 4)).isoformat(),
            (_now() - timedelta(seconds=HEARTBEAT_TTL_SECONDS * 4)).isoformat(),
            _now().isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO pending_approvals (approval_id, kind, payload, turn_id, session_id, "
        "created_at, status, owner_instance_id) "
        "VALUES ('appr_lv6', 'tool_execution', '{}', 'turn_lv6', 's1', ?, 'pending', 'prior')",
        (_now().isoformat(),),
    )

    app, ctx = _app(tmp_path, conn)  # 新实例启动 = 新连接看到的那一份权威状态
    with TestClient(app) as client:
        state = client.get("/api/runtime/state").json()
        interrupted = {
            a.get("approval_id") for a in state.get("interrupted_approvals", [])
        }
        live = {a.get("approval_id") for a in state.get("approvals", [])}
        assert "appr_lv6" in interrupted | live, (
            f"没人回答的待确认事项必须可发现（入口不能消失）："
            f"live={state.get('approvals')} interrupted={state.get('interrupted_approvals')}"
        )
        for field in (
            "instance_id",
            "revision",
            "turn_queue",
            "approvals",
            "interrupted_approvals",
            "interrupted_turns",
            "orphaned_turns",
            "tasks",
            "tools",
            "narratives",
        ):
            assert field in state, f"运行快照缺少字段 {field}"
