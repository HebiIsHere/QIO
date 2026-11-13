# -*- coding: utf-8 -*-
"""PM-QIO032 #80 / #81 后端回归：同 clientRequestId 幂等重放 + 按身份查证不编造。

本轮 E2E 复核要求（Lead 第三轮）：
- #80：重复提交保留原受理记录与状态、可查原进度；**同一请求不得再次执行**。
  这里用真实 HTTP 路由 + 记录 turns.submit 调用次数来证明：同 clientRequestId 重放
  返回原记录事实（replayed=true、同一 submissionId/status/turnId），board_submissions
  不增行，也不会再受理一次 Turn；同身份不同内容 409；换身份才新增记录。
- #81：按原请求身份查证；查不到的保持未知（404 + acceptance=null + 可读原因），
  不用「最近一条 / 时间接近」推导成功，也不写任何记录。

本文件是**活动**用例（此前 test_irm_b_acceptance_api.py 因旧理解载荷退役被整文件 skip，
本轮补上不依赖旧链路的等价覆盖）。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.interactive import models

BOARD = "board_pm032_replay"
CRID = "crid-pm032-replay-0001"


@pytest.fixture()
def client(db_conn, settings):
    from agent.storage import migration_p3_a

    migration_p3_a.apply(db_conn)
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()

    async def _fake_runner(turn_ctx) -> None:  # noqa: ANN001 - 模型替身
        turn_ctx.result = {"ok": True}
        turn_ctx.final_content = "fake answer"

    app.state.ctx.turns.set_runner(_fake_runner)
    with TestClient(app) as test_client:
        yield test_client


def _state(cards) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": [],
        "links": [],
        "selection": [],
    }


def _prepare(client: TestClient, cards) -> int:
    current = int(client.get(f"/api/interactive/boards/{BOARD}/state").json()["seq"])
    state = _state(cards)
    state["seq"] = current
    saved = client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": state}
    )
    assert saved.status_code == 200, saved.text
    return int(saved.json()["seq"])


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, status, client_request_id FROM board_submissions"
        " WHERE board_id = ? ORDER BY seq",
        (BOARD,),
    ).fetchall()


def _post(client: TestClient, **body):
    return client.post(f"/api/interactive/boards/{BOARD}/submissions", json=body)


def _acceptance(client: TestClient, client_request_id: str | None = CRID):
    params = {} if client_request_id is None else {"clientRequestId": client_request_id}
    return client.get(
        f"/api/interactive/boards/{BOARD}/submissions/acceptance", params=params
    )


def _participating_note(content: str) -> dict:
    return models.new_card(
        "text", content, participation=models.PARTICIPATION_INCLUDED
    )


def test_repeat_submit_same_identity_replays_without_new_row_or_turn(client, db_conn):
    note = _participating_note("同身份重放的说明")
    seq = _prepare(client, [note])

    submitted: list[object] = []
    original_submit = client.app.state.ctx.turns.submit

    def spying_submit(*args, **kwargs):
        submitted.append((args, kwargs))
        return original_submit(*args, **kwargs)

    client.app.state.ctx.turns.submit = spying_submit  # type: ignore[assignment]

    body = {"baseStateVersion": seq, "clientRequestId": CRID, "note": "第一版备注"}
    first = _post(client, **body)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "succeeded"
    assert first_body.get("replayed") in (None, False)
    submission_id = first_body["submission"]["id"]
    assert first_body["submission"]["clientRequestId"] == CRID
    rows = _rows(db_conn)
    assert [row["id"] for row in rows] == [submission_id]
    assert len(submitted) == 1, "首次提交应受理一次 Turn"

    # 同身份同内容重放：返回原记录事实，不落新行、不再执行。
    second = _post(client, **body)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["replayed"] is True
    assert second_body["submission"]["id"] == submission_id
    assert second_body["status"] == first_body["status"] == "succeeded"
    assert second_body["turnId"] == first_body["turnId"]
    assert second_body["delivery"]["turnId"] == first_body["delivery"]["turnId"]
    assert second_body["submission"]["clientRequestId"] == CRID
    assert _rows(db_conn) == rows, "重放不得新增 board_submissions 行"
    assert len(submitted) == 1, "同一请求不得再次执行（不得再受理 Turn）"

    # 查原进度：按精确身份查证仍指向同一受理记录。
    looked_up = _acceptance(client)
    assert looked_up.status_code == 200, looked_up.text
    acceptance = looked_up.json()["acceptance"]
    assert acceptance["clientRequestId"] == CRID
    assert acceptance["submissionId"] == submission_id
    assert acceptance["status"] == "succeeded"
    assert acceptance["replayed"] is False

    # 同身份不同内容 → 409，不落新行、不执行。
    conflict = _post(
        client, baseStateVersion=seq, clientRequestId=CRID, note="换了备注"
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["error"] == "request_identity_conflict"
    assert _rows(db_conn) == rows
    assert len(submitted) == 1

    # 换一个身份 + 真实内容变化才是新的提交（计数自证）：新增一行并真的受理一次。
    # （同内容换身份只会是 duplicate，不会新增记录——这本身也是幂等的一部分。）
    note2 = _participating_note("第二张说明")
    seq2 = _prepare(client, [note, note2])
    other = _post(
        client, baseStateVersion=seq2, clientRequestId=CRID + "-other", note="另一请求"
    )
    assert other.status_code == 200, other.text
    other_body = other.json()
    assert other_body["status"] == "succeeded"
    assert other_body["submission"]["id"] != submission_id
    assert len(_rows(db_conn)) == 2
    assert len(submitted) == 2


def test_acceptance_unknown_identity_stays_unknown_without_writing(client, db_conn):
    unknown = _acceptance(client, "crid-pm032-never-submitted")
    assert unknown.status_code == 404
    payload = unknown.json()
    assert payload["acceptance"] is None
    reason = str(payload.get("reason") or "")
    assert reason, "查不到时必须给出可读原因，而不是空"
    assert "没有" in reason or "未知" in reason
    assert _rows(db_conn) == [], "查证是只读的：不得因为一次查询写任何提交记录"

    # 连身份都没带：同样是未知，不猜。
    missing = _acceptance(client, None)
    assert missing.status_code == 404
    assert missing.json()["acceptance"] is None
    assert _rows(db_conn) == []

    # 正常提交后：未知身份仍未知（不拿「最近一条提交」冒充）。
    seq = _prepare(client, [_participating_note("已提交说明")])
    submitted = _post(client, baseStateVersion=seq, clientRequestId=CRID)
    assert submitted.status_code == 200, submitted.text
    still_unknown = _acceptance(client, "crid-pm032-still-unknown")
    assert still_unknown.status_code == 404
    assert still_unknown.json()["acceptance"] is None
    assert len(_rows(db_conn)) == 1
