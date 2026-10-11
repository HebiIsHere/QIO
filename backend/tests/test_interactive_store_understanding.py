"""理解过程的三个新路由（契约 docs/interactive-mode-contract.md §14.4）。

- GET  /api/interactive/boards/{board_id}/understanding（?submissionId= 过滤）
- POST /api/interactive/understanding/{understanding_id}/retry（只从保存的快照重试）
- POST /api/interactive/boards/{board_id}/understanding/{understanding_id}/cancel

B 的 understanding 模块用 fake（不调真实模型）；adapter provider 走
app.state.ctx.build_adapter 的同一形状（async () -> adapter | None）。
没有理解模块时必须给出明确 503，而不是让路由崩掉。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from agent.api import interactive_store
from agent.interactive import board_store, models, submission

BOARD = "board_u_test"
OTHER = "board_u_other"


async def _build_adapter():
    return "fake-adapter"


@pytest.fixture()
def understanding(monkeypatch):
    """fake understanding 模块：记录后台执行；默认落到 understood。"""
    import agent.interactive as interactive_pkg

    calls = {"runs": [], "adapters": [], "module": None}
    module = types.ModuleType("agent.interactive.understanding")

    async def run_understanding(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        adapter = await adapter_provider() if callable(adapter_provider) else None
        calls["runs"].append(understanding_id)
        calls["adapters"].append(adapter)
        conn.execute(
            "UPDATE board_understanding SET status = 'understood', updated_at = ? WHERE id = ?",
            (models.now_iso(), understanding_id),
        )
        return {"id": understanding_id, "status": "understood"}

    module.run_understanding = run_understanding
    monkeypatch.setitem(sys.modules, "agent.interactive.understanding", module)
    monkeypatch.setattr(interactive_pkg, "understanding", module, raising=False)
    calls["module"] = module
    return calls


@pytest.fixture()
async def client(db_conn: sqlite3.Connection, understanding):
    board_store.ensure_board(db_conn, board_id=BOARD)
    board_store.ensure_board(db_conn, board_id=OTHER)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(conn=db_conn, build_adapter=_build_adapter)
    app.include_router(interactive_store.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _insert(
    conn: sqlite3.Connection,
    understanding_id: str,
    *,
    board_id: str = BOARD,
    submission_id: str | None = "sub-1",
    status: str = "failed",
    snapshot: object = None,
    attempt: int = 1,
    created_at: str = "2026-10-11T00:00:00+00:00",
) -> dict:
    stamp = models.now_iso()
    if snapshot is None:
        snapshot = {"cards": [{"id": "c1", "kind": "file"}], "groups": [], "links": [], "selection": []}
    output = (
        {"kind": "no_action", "summary": "当前无需开展任务"}
        if status in ("understood", "no_action", "needs_clarification")
        else {}
    )
    conn.execute(
        "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status, stage,"
        " progress, snapshot, context, materials, output, validation, memory, error, attempt,"
        " adapter, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            understanding_id,
            board_id,
            submission_id,
            f"req:{understanding_id}",
            status,
            "读取材料",
            models.dumps({"steps": [{"key": "materials", "status": "running"}]}),
            models.dumps(snapshot),
            models.dumps({}),
            models.dumps([{"cardId": "c1", "kind": "file", "source": "unreadable", "read": False}]),
            models.dumps(output),
            models.dumps({"ok": True, "reasons": []}),
            models.dumps([]),
            "上次失败的真实原因" if status == "failed" else None,
            attempt,
            "text",
            created_at,
            stamp,
        ),
    )
    return snapshot


def _row(conn: sqlite3.Connection, understanding_id: str) -> dict:
    return dict(
        conn.execute(
            "SELECT * FROM board_understanding WHERE id = ?", (understanding_id,)
        ).fetchone()
    )


async def _settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


# --- GET 列表 --------------------------------------------------------------


async def test_list_returns_rows_with_real_shape_and_filters_by_submission(
    client, db_conn, understanding
):
    _insert(db_conn, "u_old", submission_id="sub-1", status="failed", created_at="2026-10-11T00:00:01+00:00")
    _insert(db_conn, "u_new", submission_id="sub-2", status="understood", created_at="2026-10-11T00:00:02+00:00")
    _insert(db_conn, "u_other", board_id=OTHER, submission_id="sub-9")

    response = await client.get(f"/api/interactive/boards/{BOARD}/understanding")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["understandings"]] == ["u_new", "u_old"]
    assert body["understanding"]["id"] == "u_new"

    newest = body["understandings"][0]
    assert newest["boardId"] == BOARD
    assert newest["submissionId"] == "sub-2"
    assert newest["status"] == "understood"
    assert newest["kind"] == "no_action"
    assert newest["summary"] == "当前无需开展任务"
    assert newest["attempt"] == 1
    assert newest["validation"] == {"ok": True, "reasons": []}
    assert newest["hasSnapshot"] is True
    assert newest["materials"][0]["source"] == "unreadable"
    assert newest["steps"][0]["key"] == "materials"

    filtered = await client.get(
        f"/api/interactive/boards/{BOARD}/understanding", params={"submissionId": "sub-1"}
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["understandings"]] == ["u_old"]

    empty = await client.get(
        f"/api/interactive/boards/{BOARD}/understanding", params={"submissionId": "sub-none"}
    )
    assert empty.json() == {"understanding": None, "understandings": []}


async def test_list_without_understanding_module_is_a_clear_503(client, monkeypatch):
    import agent.interactive as interactive_pkg

    monkeypatch.delitem(sys.modules, "agent.interactive.understanding", raising=False)
    monkeypatch.delattr(interactive_pkg, "understanding", raising=False)

    response = await client.get(f"/api/interactive/boards/{BOARD}/understanding")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "understanding_unavailable"
    assert "尚未接入" in detail["reason"]


async def test_list_rejects_bad_board_id(client):
    response = await client.get("/api/interactive/boards/bad!id/understanding")
    assert response.status_code == 400


# --- POST 重试 -------------------------------------------------------------


async def test_retry_failed_uses_saved_snapshot_and_runs_in_background(
    client, db_conn, understanding
):
    snapshot = {
        "cards": [{"id": "c1", "kind": "file", "meta": {"file": {"name": "材料.txt"}}}],
        "groups": [],
        "links": [],
        "selection": [],
    }
    _insert(db_conn, "u_retry", status="failed", snapshot=snapshot)

    response = await client.post("/api/interactive/understanding/u_retry/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["background"]["started"] is True, body["background"]
    assert body["understanding"]["attempt"] == 2
    assert body["understanding"]["status"] in ("accepted", "understanding", "understood")

    await _settle()
    assert understanding["runs"] == ["u_retry"]
    assert understanding["adapters"] == ["fake-adapter"], "provider 来自 ctx.build_adapter"
    assert _row(db_conn, "u_retry")["status"] == "understood"
    assert models.loads(_row(db_conn, "u_retry")["snapshot"], {}) == snapshot, (
        "重试只从保存的快照恢复：不重读当前板面"
    )


async def test_adapter_override_takes_precedence_over_ctx(db_conn, understanding):
    """app.state.interactive_adapter_provider 是统一注入点（与 C 的 intents 路由一致）。"""
    sentinel = object()

    async def override():
        return sentinel

    board_store.ensure_board(db_conn, board_id=BOARD)
    app = FastAPI()
    app.state.interactive_adapter_provider = override
    app.state.ctx = SimpleNamespace(conn=db_conn, build_adapter=_build_adapter)
    app.include_router(interactive_store.router)
    _insert(db_conn, "u_override", status="failed")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/interactive/understanding/u_override/retry")
    assert response.status_code == 200, response.text
    await _settle()
    assert understanding["adapters"] == [sentinel], "必须先认 app.state 上的注入点"


async def test_retry_rejects_active_settled_missing_and_snapshotless(client, db_conn):
    _insert(db_conn, "u_active", status="understanding")
    _insert(db_conn, "u_settled", status="understood")
    _insert(db_conn, "u_nosnap", status="failed", snapshot={})

    active = await client.post("/api/interactive/understanding/u_active/retry")
    assert active.status_code == 409
    assert active.json()["detail"]["error"] == "understanding_active"

    settled = await client.post("/api/interactive/understanding/u_settled/retry")
    assert settled.status_code == 409
    assert settled.json()["detail"]["error"] == "not_retryable"

    nosnap = await client.post("/api/interactive/understanding/u_nosnap/retry")
    assert nosnap.status_code == 409
    assert nosnap.json()["detail"]["error"] == "no_snapshot"

    missing = await client.post("/api/interactive/understanding/u_gone/retry")
    assert missing.status_code == 404

    bad_id = await client.post("/api/interactive/understanding/bad!id/retry")
    assert bad_id.status_code == 400


# --- POST 取消 -------------------------------------------------------------


async def test_cancel_active_marks_failed_with_readable_reason(client, db_conn):
    _insert(db_conn, "u_cancel", status="accepted")

    response = await client.post(f"/api/interactive/boards/{BOARD}/understanding/u_cancel/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["cancelled"] is True
    assert body["understanding"]["status"] == "failed"
    assert "取消" in body["understanding"]["error"]
    assert "重试" in body["understanding"]["error"]
    assert body["understanding"]["hasSnapshot"] is True

    again = await client.post(f"/api/interactive/boards/{BOARD}/understanding/u_cancel/cancel")
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "not_cancellable"

    wrong_board = await client.post(
        f"/api/interactive/boards/{OTHER}/understanding/u_cancel/cancel"
    )
    assert wrong_board.status_code == 404

    missing = await client.post(f"/api/interactive/boards/{BOARD}/understanding/u_gone/cancel")
    assert missing.status_code == 404


async def test_cancel_stops_a_running_background_understanding(client, db_conn, understanding):
    started = asyncio.Event()

    async def slow(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        started.set()
        await asyncio.sleep(30)
        conn.execute(
            "UPDATE board_understanding SET status = 'understood' WHERE id = ?",
            (understanding_id,),
        )
        return {"id": understanding_id, "status": "understood"}

    understanding["module"].run_understanding = slow
    _insert(db_conn, "u_slow", status="failed")

    retry = await client.post("/api/interactive/understanding/u_slow/retry")
    assert retry.status_code == 200
    await _settle()
    assert started.is_set(), "后台理解必须真的跑起来"

    cancelled = await client.post(f"/api/interactive/boards/{BOARD}/understanding/u_slow/cancel")
    assert cancelled.status_code == 200
    body = cancelled.json()
    assert body["stoppedBackground"] is True
    assert body["understanding"]["status"] == "failed"
    assert "取消" in body["understanding"]["error"]

    await _settle(10)
    assert _row(db_conn, "u_slow")["status"] == "failed", (
        "取消之后迟到的后台结果不得把状态改回 understood"
    )


async def test_cancel_without_understanding_module_is_a_clear_503(client, db_conn, monkeypatch):
    import agent.interactive as interactive_pkg

    _insert(db_conn, "u_alone", status="accepted")
    monkeypatch.delitem(sys.modules, "agent.interactive.understanding", raising=False)
    monkeypatch.delattr(interactive_pkg, "understanding", raising=False)

    response = await client.post(f"/api/interactive/boards/{BOARD}/understanding/u_alone/cancel")
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "understanding_unavailable"
