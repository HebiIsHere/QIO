# -*- coding: utf-8 -*-
"""稳定提交身份与服务端幂等（契约 §1.1 / §1.2，子智能体 B）。

覆盖语义（全部用 fake 理解模块，不联网、不读密钥、不调真实模型）：

1. 同身份重试 → 返回原事实（原 submission / 原 delivery / 原理解请求），replayed=True，
   不落新行、不新建理解请求、不启动后台理解、不调用模型，基准与勾选不变；
2. 同身份但 base_state_version 或 note 不一致 → 409 request_identity_conflict，
   不落新提交、不改基准、不调模型；
3. 空串 = 旧客户端：行为与今天一致（不参与幂等）；
4. 查证只认精确匹配：时间接近 / 最近一条理解记录 / 板面出现新任务都不是受理证据；
5. 重启（新连接）后仍按身份查得到；同身份重试跨连接仍然不重复调用模型。

运行：cd backend; uv run --frozen pytest tests/test_irm_b_submission_identity.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

from agent.interactive import board_store, models
from agent.interactive import submission as submission_module

BOARD = "board_idn_test"
OTHER_BOARD = "board_idn_other"
CRID = "crid-window-1"
CRID_ACCEPT = "11111111-2222-4333-8444-555555555555"


# --- 工具 -----------------------------------------------------------------


def _state(cards=(), groups=(), links=(), selection=()) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups),
        "links": list(links),
        "selection": list(selection),
    }


def _note(content: str, *, checked: bool = True, **fields) -> dict:
    return models.new_card("text", content, checked=checked, **fields)


def _material(kind: str = "file", content: str = "", **fields) -> dict:
    return models.new_card(kind, content, **fields)


def _save(conn: sqlite3.Connection, state: dict, board_id: str = BOARD) -> dict:
    return board_store.save_board(conn, board_id, state, reason="op")


async def _submit_async(conn: sqlite3.Connection, board_id: str = BOARD, **kwargs) -> dict:
    """提交并把默认（无 adapter）的后台理解任务停掉，避免它在断言期间改写理解行。

    后台理解的启动本身仍然发生（delivery.background.started=True），只是不在这里真跑模型。
    """
    result = await submission_module.submit_board(conn, board_id, **kwargs)
    uid = (result.get("delivery") or {}).get("understandingId")
    if uid:
        await _stop_background(str(uid))
    return result


def _submit(conn: sqlite3.Connection, board_id: str = BOARD, **kwargs) -> dict:
    return asyncio.run(_submit_async(conn, board_id, **kwargs))


async def _stop_background(understanding_id: str) -> None:
    """取消提交路径排下的后台任务（用例自己用 fake adapter 驱动执行）。"""
    target = "interactive-understanding:" + str(understanding_id)
    for task in asyncio.all_tasks():
        if task.get_name() != target:
            continue
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


def _submissions(conn: sqlite3.Connection, board_id: str = BOARD) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM board_submissions WHERE board_id = ? ORDER BY seq", (board_id,)
        )
    ]


def _understandings(conn: sqlite3.Connection, board_id: str = BOARD) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM board_understanding WHERE board_id = ? ORDER BY created_at, rowid",
            (board_id,),
        )
    ]


def _client_request_id(conn: sqlite3.Connection, submission_id: str) -> str:
    row = conn.execute(
        "SELECT client_request_id FROM board_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    return str(row["client_request_id"]) if row is not None else "<missing>"


@pytest.fixture()
def understanding(monkeypatch):
    """fake 理解模块：**真实写** board_understanding 行，并记录受理 / 执行次数。"""
    import agent.interactive as interactive_pkg

    calls: dict = {"requests": [], "runs": [], "adapters": [], "module": None}
    module = types.ModuleType("agent.interactive.understanding")

    def request_understanding(conn, *, board_id, submission_id, snapshot=None):  # noqa: ANN001
        calls["requests"].append(
            {"boardId": board_id, "submissionId": submission_id, "snapshot": dict(snapshot or {})}
        )
        rid = "sub:" + str(board_id) + ":" + str(submission_id)
        existing = conn.execute(
            "SELECT * FROM board_understanding WHERE request_id = ?", (rid,)
        ).fetchone()
        if existing is not None:
            result = dict(existing)
            result["reused"] = True
            return result
        uid = models.new_understanding_id()
        stamp = models.now_iso()
        conn.execute(
            "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status,"
            " snapshot, created_at, updated_at) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?)",
            (uid, board_id, submission_id, rid, models.dumps(snapshot or {}), stamp, stamp),
        )
        return {"id": uid, "status": "accepted", "boardId": board_id, "submissionId": submission_id}

    async def run_understanding(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        adapter = await adapter_provider() if callable(adapter_provider) else None
        calls["runs"].append(understanding_id)
        calls["adapters"].append(adapter)
        stamp = models.now_iso()
        conn.execute(
            "UPDATE board_understanding SET status = 'understood', output = ?, updated_at = ?,"
            " finished_at = ? WHERE id = ?",
            (models.dumps({"kind": "no_action", "summary": "当前无需开展任务"}), stamp, stamp, understanding_id),
        )
        return {"id": understanding_id, "status": "understood"}

    module.request_understanding = request_understanding
    module.run_understanding = run_understanding
    monkeypatch.setitem(sys.modules, "agent.interactive.understanding", module)
    monkeypatch.setattr(interactive_pkg, "understanding", module, raising=False)
    calls["module"] = module
    return calls


def _prepare(conn: sqlite3.Connection, cards: list[dict], board_id: str = BOARD) -> int:
    board_store.ensure_board(conn, board_id=board_id)
    saved = _save(conn, _state(cards), board_id=board_id)
    return int(saved["seq"])


# --- 1. 同身份重试返回原事实 ----------------------------------------------


def test_same_identity_replays_original_facts(db_conn, understanding):
    note = _note("第一次提交的说明")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="第一次提交", base_state_version=seq, client_request_id=CRID)
    assert first["status"] == "succeeded"
    assert first.get("replayed") is not True
    first_id = first["submission"]["id"]
    first_uid = first["delivery"]["understandingId"]
    assert first_uid
    assert first["checkedCleared"] == [note["id"]]

    baseline_after_first = submission_module.last_success_baseline(db_conn, BOARD)
    rows_before = len(_submissions(db_conn))
    requests_before = len(understanding["requests"])

    second = _submit(db_conn, note="第一次提交", base_state_version=seq, client_request_id=CRID)
    assert second["status"] == "succeeded"
    assert second["replayed"] is True
    assert second["submission"]["id"] == first_id, "重放必须返回原提交记录"
    assert second["submission"]["seq"] == first["submission"]["seq"]
    assert second["submission"]["status"] == "succeeded"
    assert second["delivery"] == first["delivery"], "重放返回原 delivery 事实"
    assert second["delivery"]["understandingId"] == first_uid
    assert second["checkedCleared"] == first["checkedCleared"]

    assert len(_submissions(db_conn)) == rows_before, "重放不得落新行"
    assert len(understanding["requests"]) == requests_before, "重放不得新建理解请求"
    assert len(_understandings(db_conn)) == 1
    assert understanding["runs"] == [], "重放不得进入后台理解 / 调用模型"
    after = submission_module.last_success_baseline(db_conn, BOARD)
    assert after["submissionId"] == baseline_after_first["submissionId"]
    assert after["seq"] == baseline_after_first["seq"], "重放不得改基准"


def test_replay_ignores_stale_board_version(db_conn, understanding):
    """提交之后服务端板面已经前进（清勾选 / 其它窗口保存）：重放不受版本漂移影响。

    身份重放发生在**版本门之前**：同身份同内容的请求哪怕带着旧版本号，也必须返回原事实，
    而不是被当成一次新的（过期的）提交拒绝。
    """
    note = _note("稳定身份下的说明")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
    first_id = first["submission"]["id"]

    loaded = board_store.load_board(db_conn, BOARD)
    assert int(loaded["seq"]) > seq, "场景前提：成功提交后板面版本已经前进"

    replay = _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
    assert replay["replayed"] is True
    assert replay["submission"]["id"] == first_id
    assert replay["checkedCleared"] == first["checkedCleared"]
    assert len(_submissions(db_conn)) == 1


def test_identity_is_persisted_and_survives_reconnect(db_conn, understanding, tmp_path):
    """重启（新连接）后仍按身份查得到；同身份重试跨连接仍不重复调用模型。"""
    from agent.storage.db import connect

    row = db_conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(row[2]) if row is not None else tmp_path / "test.db"
    note = _note("重启后仍应认得出的提交")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="重启前", base_state_version=seq, client_request_id=CRID)
    assert first["status"] == "succeeded"
    assert _client_request_id(db_conn, first["submission"]["id"]) == CRID

    other = connect(db_path)
    try:
        found = submission_module.acceptance_lookup(other, BOARD, CRID)
        assert found is not None, "身份必须落库：新连接（重启）后照样查得到"
        assert found["submissionId"] == first["submission"]["id"]
        replay = asyncio.run(
            _submit_async(
                other, BOARD, note="重启前", base_state_version=seq, client_request_id=CRID
            )
        )
        assert replay["replayed"] is True
        assert replay["submission"]["id"] == first["submission"]["id"]
        assert len(_understandings(other)) == 1
    finally:
        other.close()
    assert understanding["runs"] == []


# --- 2. 同身份不同内容必须拒绝 --------------------------------------------


def test_same_identity_different_note_is_conflict(db_conn, understanding):
    note = _note("冲突用例的说明")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="原本的备注", base_state_version=seq, client_request_id=CRID)
    rows_before = len(_submissions(db_conn))
    baseline_before = submission_module.last_success_baseline(db_conn, BOARD)
    requests_before = len(understanding["requests"])

    with pytest.raises(submission_module.RequestIdentityConflict) as excinfo:
        _submit(db_conn, note="另一个窗口的备注", base_state_version=seq, client_request_id=CRID)
    message = str(excinfo.value)
    assert "request_identity_conflict" in message or "身份" in message

    assert len(_submissions(db_conn)) == rows_before, "冲突不得落新提交"
    assert len(understanding["requests"]) == requests_before, "冲突不得新建理解请求"
    assert understanding["runs"] == [], "冲突不得调用模型"
    baseline_after = submission_module.last_success_baseline(db_conn, BOARD)
    assert baseline_after["submissionId"] == baseline_before["submissionId"], "冲突不得改基准"
    assert baseline_after["seq"] == baseline_before["seq"]
    # 原记录事实不变
    stored = _submissions(db_conn)[0]
    assert stored["id"] == first["submission"]["id"]
    assert stored["client_request_id"] == CRID


def test_same_identity_different_base_version_is_conflict(db_conn, understanding):
    note = _note("基准不一致的说明")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
    rows_before = len(_submissions(db_conn))
    requests_before = len(understanding["requests"])

    with pytest.raises(submission_module.RequestIdentityConflict):
        _submit(db_conn, note="", base_state_version=seq + 1, client_request_id=CRID)

    assert len(_submissions(db_conn)) == rows_before
    assert len(understanding["requests"]) == requests_before
    assert understanding["runs"] == []
    assert _submissions(db_conn)[0]["id"] == first["submission"]["id"]


def test_two_windows_same_identity_second_one_loses(db_conn, understanding):
    """两个窗口几乎同时提交：第一个成功，第二个带着同身份但内容不同 → 冲突收口。"""
    note = _note("两窗口竞争")
    seq = _prepare(db_conn, [note])
    window_a = _submit(db_conn, note="窗口 A 的备注", base_state_version=seq, client_request_id=CRID)
    assert window_a["status"] == "succeeded"
    with pytest.raises(submission_module.RequestIdentityConflict):
        _submit(db_conn, note="窗口 B 的备注", base_state_version=seq, client_request_id=CRID)
    assert len(_submissions(db_conn)) == 1
    assert len(_understandings(db_conn)) == 1
    assert understanding["runs"] == []


def test_conflict_does_not_touch_checked_or_state(db_conn, understanding):
    note = _note("冲突不改板面")
    seq = _prepare(db_conn, [note])
    _submit(db_conn, note="A", base_state_version=seq, client_request_id=CRID)
    state_before = board_store.load_board(db_conn, BOARD)
    with pytest.raises(submission_module.RequestIdentityConflict):
        _submit(db_conn, note="B", base_state_version=seq, client_request_id=CRID)
    state_after = board_store.load_board(db_conn, BOARD)
    assert state_before["seq"] == state_after["seq"]
    assert state_before["state"] == state_after["state"]


# --- 3. 空串 = 旧客户端 ----------------------------------------------------


def test_empty_identity_keeps_legacy_behaviour(db_conn, understanding):
    note = _note("旧客户端第一次")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="旧客户端", base_state_version=seq)
    assert first["status"] == "succeeded"
    assert first.get("replayed") is not True
    assert _client_request_id(db_conn, first["submission"]["id"]) == ""

    # 旧客户端重复点击：用户重新勾选同一条注释（内容与上次成功提交一致）→ 沿用既有
    # duplicate 语义（不是身份重放）。成功提交会取消勾选并保存，板面版本已经前进，
    # 旧客户端每次点击前都会先读板面拿最新版本。
    current = board_store.load_board(db_conn, BOARD)
    assert int(current["seq"]) > seq
    rechecked = dict(current["state"]["cards"][0])
    rechecked["checked"] = True
    _save(db_conn, _state([rechecked]))
    latest = int(board_store.load_board(db_conn, BOARD)["seq"])
    second = _submit(db_conn, note="旧客户端", base_state_version=latest)
    assert second["status"] == "duplicate"
    assert second.get("replayed") is not True
    assert len(_submissions(db_conn)) == 2, "旧客户端每次点击都如实落一行（与今天一致）"


def test_empty_identity_second_success_is_not_blocked_by_unique_index(db_conn, understanding):
    """空串不参与唯一索引：旧客户端连续两次不同内容的成功提交都要落得下去。"""
    note_a = _note("旧客户端内容 A")
    seq = _prepare(db_conn, [note_a])
    first = _submit(db_conn, note="", base_state_version=seq)
    assert first["status"] == "succeeded"

    note_b = _note("旧客户端内容 B")
    seq2 = int(_save(db_conn, _state([note_a, note_b]))["seq"])
    second = _submit(db_conn, note="", base_state_version=seq2)
    assert second["status"] == "succeeded", second
    rows = _submissions(db_conn)
    assert len(rows) == 2
    assert {row["client_request_id"] for row in rows} == {""}


def test_identity_must_be_a_string(db_conn, understanding):
    _prepare(db_conn, [_note("身份类型校验")])
    with pytest.raises(ValueError):
        _submit(db_conn, client_request_id=12345)  # type: ignore[arg-type]
    assert _submissions(db_conn) == []


# --- 4. 查证只认精确匹配 ---------------------------------------------------


def test_acceptance_lookup_returns_exact_record(db_conn, understanding):
    note = _note("查证用例")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="查证备注", base_state_version=seq, client_request_id=CRID_ACCEPT)

    found = submission_module.acceptance_lookup(db_conn, BOARD, CRID_ACCEPT)
    assert found is not None
    assert found["clientRequestId"] == CRID_ACCEPT
    assert found["submissionId"] == first["submission"]["id"]
    assert found["status"] == "succeeded"
    assert found["baseStateVersion"] == seq
    assert found["note"] == "查证备注"
    assert found["createdAt"] == first["submission"]["createdAt"]
    assert found["understandingId"] == first["delivery"]["understandingId"]
    assert found["understandingStatus"] == "accepted"
    assert found["delivery"]["delivered"] is True
    assert found["delivery"]["status"] == "accepted"
    assert found["delivery"]["understandingId"] == first["delivery"]["understandingId"]


def test_acceptance_lookup_requires_exact_identity(db_conn, understanding):
    note = _note("精确匹配")
    seq = _prepare(db_conn, [note])
    _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
    assert submission_module.acceptance_lookup(db_conn, BOARD, "crid-not-mine") is None
    assert submission_module.acceptance_lookup(db_conn, BOARD, "") is None
    assert submission_module.acceptance_lookup(db_conn, OTHER_BOARD, CRID) is None
    assert submission_module.acceptance_lookup(db_conn, BOARD, CRID) is not None


def test_acceptance_lookup_ignores_time_proximity_and_other_submissions(db_conn, understanding):
    """时钟偏差不误判：另一块板面在同一毫秒提交、板面又出现新任务，都不能当受理证据。"""
    note = _note("时间接近不算证据")
    seq = _prepare(db_conn, [note])
    _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)

    board_store.ensure_board(db_conn, board_id=OTHER_BOARD)
    other_note = models.new_card("text", "另一块板面的提交", checked=True)
    _save(db_conn, _state([other_note]), board_id=OTHER_BOARD)
    _submit(db_conn, OTHER_BOARD, note="另一块板面", client_request_id="crid-other")

    # 板面上出现新任务 / 新意图
    db_conn.execute(
        "INSERT INTO board_intents (id, board_id, submission_id, title, summary, status, preview,"
        " impact, depends_on, conflicts_with, conflict_key, material_refs, progress, applied,"
        " revert, reason, demo, created_at, updated_at) VALUES ('intent_new', ?, 'sub_x', '新任务',"
        " '', 'pending', '{}', '{}', '[]', '[]', '', '[]', '{}', '{}', '{}', '', 0, ?, ?)",
        (BOARD, models.now_iso(), models.now_iso()),
    )
    assert submission_module.acceptance_lookup(db_conn, BOARD, "crid-nonexistent") is None
    # 另一块板面的身份也不能被这块板面认领
    assert submission_module.acceptance_lookup(db_conn, BOARD, "crid-other") is None
    assert submission_module.acceptance_lookup(db_conn, OTHER_BOARD, CRID) is None


def test_acceptance_lookup_reports_failed_and_settled_understanding(db_conn, understanding):
    note = _note("理解终态")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
    uid = first["delivery"]["understandingId"]
    db_conn.execute(
        "UPDATE board_understanding SET status = 'failed', error = '模型失败（可重试）' WHERE id = ?",
        (uid,),
    )
    found = submission_module.acceptance_lookup(db_conn, BOARD, CRID)
    assert found["understandingStatus"] == "failed"
    assert found["delivery"]["delivered"] is True, "理解失败不改受理事实"


# --- 5. 同身份重试不重复调用模型（反例） ----------------------------------


def test_replay_never_reinvokes_model_even_after_settle(db_conn, understanding):
    note = _note("不重复调用模型")
    seq = _prepare(db_conn, [note])
    first = _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
    uid = first["delivery"]["understandingId"]
    db_conn.execute(
        "UPDATE board_understanding SET status = 'understood', finished_at = ?, updated_at = ?"
        " WHERE id = ?",
        (models.now_iso(), models.now_iso(), uid),
    )
    for _ in range(3):
        replay = _submit(db_conn, note="", base_state_version=seq, client_request_id=CRID)
        assert replay["replayed"] is True
    assert len(_submissions(db_conn)) == 1
    assert len(_understandings(db_conn)) == 1
    assert understanding["runs"] == [], "重放绝不重新调用模型"
    assert understanding["adapters"] == []


def test_no_identity_request_second_submit_after_pause_still_works(db_conn, understanding):
    """空串路径没有被幂等改动破坏：继续用既有 duplicate / empty 判定。"""
    note = _note("无身份语义")
    seq = _prepare(db_conn, [note])
    assert _submit(db_conn, note="", base_state_version=seq)["status"] == "succeeded"
    current = board_store.load_board(db_conn, BOARD)
    rechecked = dict(current["state"]["cards"][0])
    rechecked["checked"] = True
    _save(db_conn, _state([rechecked]))
    latest = int(board_store.load_board(db_conn, BOARD)["seq"])
    assert _submit(db_conn, note="", base_state_version=latest)["status"] == "duplicate"
    assert len(_understandings(db_conn)) == 1, "duplicate 不新建理解请求"
