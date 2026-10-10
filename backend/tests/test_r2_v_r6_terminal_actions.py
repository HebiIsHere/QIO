"""V 组独立验证：R6（成功重发后原记录不再暴露可用的 resend）。

两层证据：
  A. 数据库级：TurnJournal.facts() 的读时投影全矩阵（唯一实现 = project_terminal_actions）；
  B. 真实 API：插入一条 interrupted + actions=['resend'] 的台账行 → 快照能看到 resend
     → POST /api/turns/{id}/resend 首次 200 → 刷新后再读看不到 resend → 重复 POST 409。

不使用 r2-w4 / fb_* / acc_* 的用例。B 里只为「不真的调模型」把 turns.activate 换成 no-op，
被验证的 claim / recovered_at / 读时投影链路完全是真的。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.storage.turn_journal import TurnJournal, project_terminal_actions

TURN = "turn-v-r6-1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _journal_row(
    conn,
    turn_id: str,
    *,
    status: str,
    actions: list[str] | None,
    notify: int = 0,
    recovered_at: str | None = None,
    topic_id: str | None = None,
    reason: str | None = None,
) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status,"
        " created_at, updated_at, reason, actions, recovered_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            turn_id,
            "用户的原话",
            topic_id,
            int(notify),
            status,
            now,
            now,
            reason,
            None if actions is None else json.dumps(actions),
            recovered_at,
        ),
    )
    conn.commit()


def _raw_actions(conn, turn_id: str) -> str | None:
    row = conn.execute(
        "SELECT actions FROM turn_journal WHERE turn_id = ?", (turn_id,)
    ).fetchone()
    return None if row is None else row["actions"]


# ---------------------------------------------------------------- A. 数据库级


def test_v_r6a_facts_projection_matrix(db_conn):
    """读时投影的准入谓词：recoverable_now = interrupted and notify == 0 and recovered_at IS NULL。"""
    db_conn.execute("DELETE FROM turn_journal")
    cases = [
        # turn_id, status, actions, notify, recovered_at, 期望 actions
        ("t-rec", "interrupted", ["resend"], 0, None, ["resend"]),
        ("t-claimed", "interrupted", ["resend"], 0, _now(), []),
        ("t-notify", "interrupted", ["resend"], 1, None, []),
        ("t-empty-recoverable", "interrupted", [], 0, None, ["resend"]),
        ("t-empty-claimed", "interrupted", [], 0, _now(), []),
        ("t-empty-notify", "interrupted", [], 1, None, []),
        ("t-cancelled", "cancelled", ["resend"], 0, None, ["retry"]),
        ("t-cancelled-claimed", "cancelled", ["resend"], 0, _now(), ["retry"]),
        ("t-completed", "completed", ["retry"], 0, None, ["retry"]),
        ("t-failed", "failed", ["resend"], 0, None, []),
        ("t-queued", "queued", ["resend"], 0, None, ["resend"]),
    ]
    for turn_id, status, actions, notify, recovered_at, _expected in cases:
        _journal_row(
            db_conn,
            turn_id,
            status=status,
            actions=actions,
            notify=notify,
            recovered_at=recovered_at,
        )

    journal = TurnJournal(db_conn)
    facts = journal.facts([c[0] for c in cases])

    for turn_id, status, actions, notify, recovered_at, expected in cases:
        got = facts[turn_id]["actions"]
        label = f"{turn_id} status={status} notify={notify} recovered={'yes' if recovered_at else 'no'} stored={actions}"
        print(label, "->", got)
        assert got == expected, label
        # 读时投影绝不改写执行事实
        assert json.loads(_raw_actions(db_conn, turn_id)) == actions
        # 通知轮 / 已领取轮不再暴露"可点的" resend。
        # queued / running 不在投影的作用域内（事实尚未定稿，原样读回）。
        if status != "queued" and not (
            status == "interrupted" and notify == 0 and recovered_at is None
        ):
            assert "resend" not in got


def test_v_r6b_cancelled_resend_normalises_to_retry(db_conn):
    """取消轮的 resend 必须归一成真正可用的 retry（回归）。"""
    db_conn.execute("DELETE FROM turn_journal")
    _journal_row(db_conn, "t-cxl", status="cancelled", actions=["resend", "dismiss"])
    facts = TurnJournal(db_conn).facts(["t-cxl"])
    print("cancelled actions:", facts["t-cxl"]["actions"])
    assert facts["t-cxl"]["actions"] == ["retry", "dismiss"]
    assert project_terminal_actions("cancelled", ["resend"]) == ["retry"]


# ---------------------------------------------------------------- B. 真实 API


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _seed_message_with_turn(client, db_conn, turn_id: str) -> str:
    """插一条带 turn_id 的历史消息，让 /api/session/context 这一页覆盖到该轮。"""
    topic_id = client.app.state.ctx.current_topic()
    frag_id = f"frag-v-r6-{turn_id}"
    now = _now()
    db_conn.execute(
        "INSERT INTO fragments (id, topic_id, created_at) VALUES (?,?,?)",
        (frag_id, topic_id, now),
    )
    msg_id = f"msg-v-r6-{turn_id}"
    db_conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, raw,"
        " created_at, storage_tier, turn_id) VALUES (?,?,?,?,?,?,?, 'hot', ?)",
        (msg_id, frag_id, "user", "用户的原话", "text", "{}", now, turn_id),
    )
    db_conn.commit()
    return topic_id


def _facts_for(client, turn_id: str) -> dict | None:
    resp = client.get("/api/session/context")
    assert resp.status_code == 200, resp.text
    for item in resp.json().get("turn_facts") or []:
        if item.get("turn_id") == turn_id:
            return item
    return None


def test_v_r6c_real_api_resend_once_then_hidden_then_409(client, db_conn, monkeypatch):
    turn_id = TURN
    topic_id = _seed_message_with_turn(client, db_conn, turn_id)
    _journal_row(
        db_conn,
        turn_id,
        status="interrupted",
        actions=["resend"],
        topic_id=topic_id,
        reason="running_at_restart",
    )

    # 不真的执行这一轮模型（被验证的投影 / claim / 409 链路不受影响）
    calls: list[str] = []
    monkeypatch.setattr(
        client.app.state.ctx.turns,
        "activate",
        lambda turn: calls.append(str(getattr(turn, "turn_id", turn))),
        raising=False,
    )

    # 1) 刷新读到的历史事实里必须**有** resend（这一条现在确实可重发）
    first = _facts_for(client, turn_id)
    print("before POST turn_fact:", first)
    assert first is not None and first.get("actions") == ["resend"]

    # 2) 首次重发成功
    resp = client.post(f"/api/turns/{turn_id}/resend")
    print("POST resend ->", resp.status_code, resp.text[:300])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    assert calls, "activate 没有被调用（说明重发没有走完受理链路）"

    # 3) 台账事实：recovered_at 已写上，status 仍是 interrupted（不是被改写执行事实）
    row = db_conn.execute(
        "SELECT status, recovered_at, actions FROM turn_journal WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    print("journal after:", dict(row))
    assert row["status"] == "interrupted"
    assert row["recovered_at"]
    assert json.loads(row["actions"]) == ["resend"]  # 执行事实未被改写
    assert TurnJournal(db_conn).facts([turn_id])[turn_id]["actions"] == []

    # 4) 刷新后再读：不得再暴露 resend（要么这一条根本不再出现在可用操作里）
    second = _facts_for(client, turn_id)
    print("after POST turn_fact:", second)
    if second is not None:
        assert "resend" not in (second.get("actions") or [])

    # 5) 重复 POST 必须是 409（死按钮不存在）
    again = client.post(f"/api/turns/{turn_id}/resend")
    print("POST resend again ->", again.status_code, again.text[:200])
    assert again.status_code == 409
