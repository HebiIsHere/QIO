# -*- coding: utf-8 -*-
"""提交身份的两个路由（契约 §1.2，子智能体 B）。

    POST /api/interactive/boards/{board_id}/submissions   body 增加可选 clientRequestId
    GET  /api/interactive/boards/{board_id}/submissions/acceptance?clientRequestId=<id>
         200 {"acceptance": {...}} | 404 {"acceptance": null, "reason": "..."}

冻结形状断言（D/A 也按它写断言）：
    acceptance = {clientRequestId, submissionId, status, delivery{delivered,status,
    understandingId,reason,detail}, understandingId, understandingStatus,
    baseStateVersion, note, createdAt, replayed}

运行：cd backend; uv run --frozen pytest tests/test_irm_b_acceptance_api.py -q
"""

from __future__ import annotations

import sys
import time
import types

import pytest

pytestmark = pytest.mark.skip(
    reason="旧理解受理载荷已随 §13.3 退役；新受理事实由 turnId/主循环测试覆盖"
)
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.interactive import models

BOARD = "board_acc_api"
CRID = "crid-api-0001"
ACCEPTANCE_KEYS = {
    "clientRequestId",
    "submissionId",
    "status",
    "delivery",
    "understandingId",
    "understandingStatus",
    "baseStateVersion",
    "note",
    "createdAt",
    "replayed",
}
DELIVERY_KEYS = {"delivered", "status", "understandingId", "reason", "detail"}


@pytest.fixture(autouse=True)
def understanding(monkeypatch):
    """fake 理解模块：真实写 board_understanding 行，记录调用次数。"""
    import agent.interactive as interactive_pkg

    calls: dict = {"requests": [], "runs": [], "module": None}
    module = types.ModuleType("agent.interactive.understanding")

    def request_understanding(conn, *, board_id, submission_id, snapshot=None):  # noqa: ANN001
        calls["requests"].append(submission_id)
        rid = "sub:" + str(board_id) + ":" + str(submission_id)
        existing = conn.execute(
            "SELECT * FROM board_understanding WHERE request_id = ?", (rid,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        uid = models.new_understanding_id()
        stamp = models.now_iso()
        conn.execute(
            "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status,"
            " snapshot, created_at, updated_at) VALUES (?, ?, ?, ?, 'accepted', ?, ?, ?)",
            (uid, board_id, submission_id, rid, models.dumps(snapshot or {}), stamp, stamp),
        )
        return {"id": uid, "status": "accepted", "boardId": board_id, "submissionId": submission_id}

    async def run_understanding(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        calls["runs"].append(understanding_id)
        return {"id": understanding_id, "status": "accepted"}

    module.request_understanding = request_understanding
    module.run_understanding = run_understanding
    monkeypatch.setitem(sys.modules, "agent.interactive.understanding", module)
    monkeypatch.setattr(interactive_pkg, "understanding", module, raising=False)
    calls["module"] = module
    return calls


@pytest.fixture()
def client(db_conn, settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as test_client:
        yield test_client


def _state(cards, *, seq: int = 0) -> dict:
    return {
        "boardId": BOARD,
        "seq": int(seq),
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": [],
        "links": [],
        "selection": [],
    }


def _note(content: str, **fields) -> dict:
    return models.new_card("text", content, checked=True, **fields)


def _prepare(client: TestClient, cards) -> int:
    """按**当前版本**保存一次板面（并发安全：基于过期版本的保存会被服务端拒绝）。"""
    current = int(client.get(f"/api/interactive/boards/{BOARD}/state").json()["seq"])
    saved = client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": _state(cards, seq=current)}
    )
    assert saved.status_code == 200, saved.text
    return int(saved.json()["seq"])


def _post(client: TestClient, **body):
    return client.post(f"/api/interactive/boards/{BOARD}/submissions", json=body)


def _get(client: TestClient, client_request_id: str | None = CRID):
    params = {} if client_request_id is None else {"clientRequestId": client_request_id}
    return client.get(f"/api/interactive/boards/{BOARD}/submissions/acceptance", params=params)


# --- POST：接受 clientRequestId -------------------------------------------


def test_submit_accepts_client_request_id_and_persists_it(client, db_conn):
    note = _note("接口带身份提交")
    seq = _prepare(client, [note])
    response = _post(
        client, note="接口备注", baseStateVersion=seq, clientRequestId=CRID
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "succeeded"
    assert result.get("replayed") is not True
    row = db_conn.execute(
        "SELECT client_request_id FROM board_submissions WHERE id = ?",
        (result["submission"]["id"],),
    ).fetchone()
    assert row["client_request_id"] == CRID


def test_submit_without_client_request_id_keeps_legacy_behaviour(client, db_conn):
    note = _note("旧客户端接口提交")
    seq = _prepare(client, [note])
    result = _post(client, note="", baseStateVersion=seq).json()
    assert result["status"] == "succeeded"
    row = db_conn.execute(
        "SELECT client_request_id FROM board_submissions WHERE id = ?",
        (result["submission"]["id"],),
    ).fetchone()
    assert row["client_request_id"] == ""


def test_submit_replay_returns_original_facts(client, understanding):  # noqa: D401
    note = _note("接口重放")
    seq = _prepare(client, [note])
    first = _post(client, note="同一份备注", baseStateVersion=seq, clientRequestId=CRID).json()
    assert first["delivery"]["background"]["started"] is True, "首次提交照常启动后台理解"
    requests_after_first = len(understanding["requests"])
    time.sleep(0.3)  # 让首次提交的后台任务真的跑起来，计数才是稳定的
    runs_settled = len(understanding["runs"])
    assert runs_settled >= 1, "首次提交的后台理解应当已经启动"

    second_response = _post(
        client, note="同一份备注", baseStateVersion=seq, clientRequestId=CRID
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["replayed"] is True
    assert second["submission"]["id"] == first["submission"]["id"]
    assert second["delivery"] == first["delivery"]
    time.sleep(0.3)
    assert len(understanding["requests"]) == requests_after_first, "重放不得新建理解请求"
    assert len(understanding["runs"]) == runs_settled, "重放不得启动后台理解"


def test_submit_same_identity_different_content_is_409(client, understanding):
    note = _note("接口冲突")
    seq = _prepare(client, [note])
    first = _post(client, note="A 窗口", baseStateVersion=seq, clientRequestId=CRID)
    assert first.status_code == 200
    conflict = _post(client, note="B 窗口", baseStateVersion=seq, clientRequestId=CRID)
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["error"] == "request_identity_conflict"
    assert detail.get("reason")
    assert len(understanding["requests"]) == 1


def test_submit_bad_client_request_id_is_400(client):
    _prepare(client, [_note("身份校验")])
    bad_type = _post(client, clientRequestId=12345)
    assert bad_type.status_code == 400, bad_type.text
    too_long = _post(client, clientRequestId="x" * 5000)
    assert too_long.status_code == 400, too_long.text


# --- GET：精确查证 --------------------------------------------------------


def test_acceptance_lookup_returns_frozen_shape(client):
    note = _note("查证形状")
    seq = _prepare(client, [note])
    submitted = _post(
        client, note="查证备注", baseStateVersion=seq, clientRequestId=CRID
    ).json()

    response = _get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body.keys()) == {"acceptance"}
    acceptance = body["acceptance"]
    assert set(acceptance.keys()) == ACCEPTANCE_KEYS, acceptance.keys()
    assert set(acceptance["delivery"].keys()) >= DELIVERY_KEYS
    assert acceptance["clientRequestId"] == CRID
    assert acceptance["submissionId"] == submitted["submission"]["id"]
    assert acceptance["status"] == "succeeded"
    assert acceptance["baseStateVersion"] == seq
    assert acceptance["note"] == "查证备注"
    assert acceptance["createdAt"] == submitted["submission"]["createdAt"]
    assert acceptance["understandingId"] == submitted["delivery"]["understandingId"]
    assert acceptance["understandingStatus"] == "accepted"
    assert acceptance["replayed"] is False


def test_acceptance_lookup_404_returns_null_not_guessed_success(client):
    note = _note("查不到")
    seq = _prepare(client, [note])
    _post(client, note="", baseStateVersion=seq, clientRequestId=CRID)

    missing = _get(client, "crid-never-used")
    assert missing.status_code == 404, missing.text
    body = missing.json()
    assert body["acceptance"] is None
    assert body["reason"]
    assert "crid-never-used" in body["reason"] or "受理" in body["reason"]

    no_param = _get(client, None)
    assert no_param.status_code == 404
    assert no_param.json()["acceptance"] is None

    empty_param = _get(client, "")
    assert empty_param.status_code == 404
    assert empty_param.json()["acceptance"] is None


def test_acceptance_lookup_is_exact_not_time_based(client, db_conn):
    """板面出现新任务、另一块板面刚提交：都不能被当成本次受理证据。"""
    note = _note("时间接近不算证据")
    seq = _prepare(client, [note])
    _post(client, note="", baseStateVersion=seq, clientRequestId=CRID)

    other_board = "board_acc_other"
    client.post("/api/interactive/boards", json={"boardId": other_board, "title": "另一块板面"})
    client.put(
        f"/api/interactive/boards/{other_board}/state",
        json={"state": {"boardId": other_board, "seq": 0, "cards": [_note("别的提交")]}},
    )
    other = client.post(
        f"/api/interactive/boards/{other_board}/submissions",
        json={"clientRequestId": "crid-other-board"},
    )
    assert other.status_code == 200, other.text

    unknown = _get(client, "crid-unknown")
    assert unknown.status_code == 404
    assert unknown.json()["acceptance"] is None
    # 另一块板面的身份在这块板面上查不到
    cross = _get(client, "crid-other-board")
    assert cross.status_code == 404


def test_acceptance_lookup_after_restart_is_by_identity(client, db_conn, tmp_path, settings):
    """重启后（新连接 / 新应用实例）按身份查证仍然命中。"""
    from agent.api.server import create_app

    note = _note("重启后查证")
    seq = _prepare(client, [note])
    submitted = _post(
        client, note="重启备注", baseStateVersion=seq, clientRequestId=CRID
    ).json()

    restarted_app = create_app(settings, db_conn)
    restarted_app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(restarted_app) as restarted:
        response = restarted.get(
            f"/api/interactive/boards/{BOARD}/submissions/acceptance",
            params={"clientRequestId": CRID},
        )
        assert response.status_code == 200, response.text
        acceptance = response.json()["acceptance"]
        assert acceptance["submissionId"] == submitted["submission"]["id"]
        assert acceptance["clientRequestId"] == CRID


def test_acceptance_lookup_reports_understanding_terminal_state(client, db_conn):
    note = _note("终态如实返回")
    seq = _prepare(client, [note])
    submitted = _post(client, note="", baseStateVersion=seq, clientRequestId=CRID).json()
    uid = submitted["delivery"]["understandingId"]
    db_conn.execute(
        "UPDATE board_understanding SET status = 'needs_clarification' WHERE id = ?", (uid,)
    )
    acceptance = _get(client).json()["acceptance"]
    assert acceptance["understandingStatus"] == "needs_clarification"
    assert acceptance["delivery"]["delivered"] is True

# --- A 的 acceptanceMatchesIdentity 六条硬约束 ---------------------------------


def test_acceptance_echoes_identity_verbatim_and_fills_frozen_fields(client, db_conn):
    """逐条满足前端 acceptanceMatchesIdentity 的假设（Lead 冻结的 6 条）。

    1) clientRequestId 原样回显（不规范化 / 不截断 / 不改大小写）
    2) submissionId 是非空字符串
    3) note 原样回显提交时的 note（前端恒为空串）
    4) baseStateVersion 等于该次请求里的 baseStateVersion（number）
    5) delivery 是原提交记录的 delivery 对象（delivered/status/understandingId/reason/detail）
    6) understandingId / understandingStatus 能取到就填
    """
    identity = "creq_AbC-123_xY-9"          # 大小写 / 连字符 / 下划线都原样保留
    note = _note("六条约束")
    seq = _prepare(client, [note])
    submitted = _post(
        client, note="", baseStateVersion=seq, clientRequestId=identity
    ).json()
    assert submitted["status"] == "succeeded"

    body = _get(client, identity).json()
    acceptance = body["acceptance"]

    # 1) 原样回显
    assert acceptance["clientRequestId"] == identity
    # 2) submissionId 非空
    assert isinstance(acceptance["submissionId"], str) and acceptance["submissionId"]
    assert acceptance["submissionId"] == submitted["submission"]["id"]
    # 3) note 原样回显（前端恒为空串）
    assert acceptance["note"] == ""
    # 4) baseStateVersion 是 number 且等于请求值
    assert isinstance(acceptance["baseStateVersion"], int)
    assert acceptance["baseStateVersion"] == seq
    # 5) delivery 是原提交记录的 delivery 对象，status 是原记录的 status
    delivery = acceptance["delivery"]
    assert set(DELIVERY_KEYS) <= set(delivery.keys()), delivery.keys()
    assert delivery == submitted["delivery"] or (
        delivery["delivered"] == submitted["delivery"]["delivered"]
        and delivery["status"] == submitted["delivery"]["status"]
        and delivery["understandingId"] == submitted["delivery"]["understandingId"]
    )
    assert acceptance["status"] == submitted["status"] == "succeeded"
    # 6) 理解请求事实能取到
    assert acceptance["understandingId"] == submitted["delivery"]["understandingId"]
    assert acceptance["understandingStatus"] == "accepted"

    # 形状完整（JSON 样例即契约）
    assert set(acceptance.keys()) == ACCEPTANCE_KEYS


def test_acceptance_note_echoes_nonempty_note_verbatim(client):
    """非空备注也要**逐字**回显：不折叠空格、不截断、不替换换行。"""
    identity = "creq_note-echo-0001"
    note = _note("备注原样")
    seq = _prepare(client, [note])
    raw_note = "备注：保留  两个空格\n以及换行"
    submitted = _post(
        client, note=raw_note, baseStateVersion=seq, clientRequestId=identity
    ).json()
    assert submitted["status"] == "succeeded"
    acceptance = _get(client, identity).json()["acceptance"]
    assert acceptance["note"] == raw_note
    assert acceptance["clientRequestId"] == identity


def test_acceptance_understanding_fields_null_when_missing(client, db_conn):
    """取不到理解请求时 understandingId / understandingStatus 如实为 null（前端保守回退）。"""
    identity = "creq_no-understanding-1"
    note = _note("理解记录缺失")
    seq = _prepare(client, [note])
    submitted = _post(
        client, note="", baseStateVersion=seq, clientRequestId=identity
    ).json()
    db_conn.execute(
        "DELETE FROM board_understanding WHERE submission_id = ?",
        (submitted["submission"]["id"],),
    )
    acceptance = _get(client, identity).json()["acceptance"]
    assert acceptance["understandingId"] is None
    assert acceptance["understandingStatus"] is None
    assert acceptance["delivery"]["delivered"] is True, "受理事实不因理解记录缺失而改变"


def test_replay_with_original_base_state_version_is_200_not_stale(client):
    """Lead 明确要求：用**原 baseStateVersion** 同身份重试 → 200 + replayed=true。

    身份命中判定必须排在 stale_state 之前：同身份重试带的是**原请求那个版本**，
    只要板面在那之后前进过（用户又改了一点东西），裸判就会先吃到 409 stale_state，
    永远进不到幂等分支。

    2026-10-12 起「许可持续有效」，提交本身**不再**改板面（不再清勾选），
    所以「提交后版本自然前进」这个旧前提不成立；场景改成**用户在提交后又改了一次板面**
    —— 这是更常见也更严格的前提（版本确实比原请求新）。
    """
    identity = "creq_stale-order-0001"
    note = _note("顺序要求")
    seq = _prepare(client, [note])
    first = _post(
        client, note="", baseStateVersion=seq, clientRequestId=identity
    )
    assert first.status_code == 200, first.text

    later = _prepare(client, [note, _note("提交之后用户又写的一条")])
    current = client.get(f"/api/interactive/boards/{BOARD}/state").json()["seq"]
    assert current > seq, "场景前提：板面版本已经比原请求新"
    assert later >= current

    retry = _post(client, note="", baseStateVersion=seq, clientRequestId=identity)
    assert retry.status_code == 200, retry.text
    result = retry.json()
    assert result["replayed"] is True
    assert result["submission"]["id"] == first.json()["submission"]["id"]


def test_acceptance_samples_dump(client):
    """把 acceptance 的真实返回打出来（报告用样例；同时保证它是可序列化的 JSON）。"""
    identity = "creq_sample-00000001"
    note = _note("样例")
    seq = _prepare(client, [note])
    _post(client, note="", baseStateVersion=seq, clientRequestId=identity)
    acceptance = _get(client, identity).json()["acceptance"]
    import json as _json

    dumped = _json.loads(_json.dumps(acceptance, ensure_ascii=False))
    assert dumped == acceptance
    print("ACCEPTANCE_SAMPLE=" + _json.dumps(dumped, ensure_ascii=False, sort_keys=True))
