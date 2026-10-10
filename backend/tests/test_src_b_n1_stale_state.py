"""【子智能体 B · N1】服务端过期写入保护（契约 §1.4 / docs/interactive-state-recovery-contract.md §1.4）。

分层：
- ① 单元：版本比较口径（board_store.parse_state_seq / state_version_error）；
- ③ ASGI + 真实临时 sqlite：完整路由 + 直接 SELECT 核对「真正存储结果」；
- ④ 真实 HTTP 由 scripts 级证据（uvicorn + 临时 QIO_DATA_DIR）覆盖，见交付报告。

基线（b3245e5）上这些用例是红的：服务端接受旧整板状态，新正文被覆盖。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import board_store, models

BOARD = "board_src_b_n1"


# --- 脚手架 -------------------------------------------------------------


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _meta(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _candidate(meta: dict, cards: list[dict], *, seq: int | None = None) -> dict:
    """以「读到的已保存版本」为基准构造候选（真实客户端就是这么做的）。"""
    state = json.loads(json.dumps(meta["state"]))
    state["cards"] = cards
    if seq is not None:
        state["seq"] = seq
    return state


def _put(client: TestClient, state: dict, **extra):
    return client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test", **extra}
    )


def _stored(conn: sqlite3.Connection) -> tuple[int, dict]:
    row = conn.execute(
        "SELECT seq, state FROM board_states WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert row is not None, "板面状态行必须存在"
    return int(row["seq"]), json.loads(row["state"])


def _snapshot_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM board_state_snapshots WHERE board_id = ?", (BOARD,)
    ).fetchone()
    return int(row["n"])


def _content_of(state: dict, card_id: str) -> str | None:
    for card in state.get("cards") or []:
        if str(card.get("id")) == card_id:
            return str(card.get("content") or "")
    return None


def _statuses(client: TestClient) -> dict[str, str]:
    listed = client.get(f"/api/interactive/boards/{BOARD}/intents")
    assert listed.status_code == 200, listed.text
    return {item["id"]: item["status"] for item in listed.json()["intents"]}


# --- ① 单元：版本比较口径 ------------------------------------------------


def test_parse_state_seq_accepts_real_versions_only():
    assert board_store.parse_state_seq({"seq": 3}) == 3
    assert board_store.parse_state_seq({"seq": 0}) == 0
    assert board_store.parse_state_seq({"seq": 3.0}) == 3
    assert board_store.parse_state_seq({"seq": "3"}) == 3
    # 未知版本（缺失 / 非数字 / 布尔）不得被当成某个已保存版本
    assert board_store.parse_state_seq({}) is None
    assert board_store.parse_state_seq({"seq": None}) is None
    assert board_store.parse_state_seq({"seq": "abc"}) is None
    assert board_store.parse_state_seq({"seq": True}) is None


def test_state_version_error_rules():
    # 一致 → 放行
    assert board_store.state_version_error(claimed=4, current=4) is None
    # 新板（还没有任何已保存版本）第一次保存：没有可被覆盖的旧版本
    assert board_store.state_version_error(claimed=0, current=0) is None
    assert board_store.state_version_error(claimed=None, current=0) is None
    # 一旦有已保存版本，旧版 / 未知版本都必须被拒绝，并给出可读原因
    old = board_store.state_version_error(claimed=1, current=2)
    assert old and "旧" in old and "2" in old
    unknown = board_store.state_version_error(claimed=None, current=2)
    assert unknown and "版本" in unknown
    future = board_store.state_version_error(claimed=9, current=2)
    assert future and "9" in future


# --- ③ ASGI + 真实临时库 -------------------------------------------------


def test_new_board_first_save_is_allowed(client: TestClient, db_conn: sqlite3.Connection):
    first = _put(client, _candidate({"state": models.empty_state(BOARD)}, [_card("n1", "text", "第一版")], seq=0))
    assert first.status_code == 200, first.text
    assert first.json()["seq"] == 1
    assert _stored(db_conn)[0] == 1


def test_new_board_first_save_without_seq_is_allowed_for_compat(
    client: TestClient, db_conn: sqlite3.Connection
):
    """新板没有任何已保存版本：缺失 seq 也允许（没有可被覆盖的旧版本）。"""
    state = models.empty_state(BOARD)
    state.pop("seq", None)
    state["cards"] = [_card("n1", "text", "第一版")]
    resp = _put(client, state)
    assert resp.status_code == 200, resp.text
    assert _stored(db_conn)[0] == 1


def test_late_old_whole_board_write_cannot_overwrite_newer_version(
    client: TestClient, db_conn: sqlite3.Connection
):
    """乱序：第二版先保存成功，迟到的第一版不得覆盖它（契约 §1.4）。"""
    seed = _put(client, _candidate({"state": models.empty_state(BOARD)}, [_card("n1", "text", "初始")], seq=0))
    assert seed.status_code == 200, seed.text
    meta = _meta(client)
    assert meta["seq"] == 1

    # 两次检查都基于同一个已保存版本 1：第一版 A 先发出但迟到，第二版 B 先落地
    candidate_a = _candidate(meta, [_card("n1", "text", "第一版（迟到）")])
    candidate_b = _candidate(meta, [_card("n1", "text", "第二版（新正文）")])
    assert candidate_a["seq"] == 1 and candidate_b["seq"] == 1

    saved_b = _put(client, candidate_b)
    assert saved_b.status_code == 200, saved_b.text
    assert saved_b.json()["seq"] == 2

    snapshots_before = _snapshot_count(db_conn)
    late_a = _put(client, candidate_a)
    assert late_a.status_code == 409, late_a.text
    detail = late_a.json()["detail"]
    assert detail["error"] == "stale_state"
    assert detail["currentSeq"] == 2
    assert isinstance(detail["reason"], str) and detail["reason"].strip()

    # 真正存储结果：新版没有被覆盖；旧写入没有落库、没有推进快照
    meta_after = _meta(client)
    assert meta_after["seq"] == 2
    stored_seq, stored_state = _stored(db_conn)
    assert stored_seq == 2
    assert _content_of(stored_state, "n1") == "第二版（新正文）"
    assert _snapshot_count(db_conn) == snapshots_before, "被拒绝的写入不得追加快照"


def test_missing_or_unknown_version_is_rejected_on_saved_board(
    client: TestClient, db_conn: sqlite3.Connection
):
    seed = _put(client, _candidate({"state": models.empty_state(BOARD)}, [_card("n1", "text", "初始")], seq=0))
    assert seed.status_code == 200, seed.text
    meta = _meta(client)

    without_seq = _candidate(meta, [_card("n1", "text", "没有版本声明")])
    without_seq.pop("seq", None)
    resp_missing = _put(client, without_seq)
    assert resp_missing.status_code == 409, resp_missing.text
    assert resp_missing.json()["detail"]["currentSeq"] == 1

    resp_future = _put(client, _candidate(meta, [_card("n1", "text", "未知版本")], seq=99))
    assert resp_future.status_code == 409, resp_future.text
    assert resp_future.json()["detail"]["error"] == "stale_state"
    assert resp_future.json()["detail"]["currentSeq"] == 1

    # 被拒绝的写入都没有落库
    assert _content_of(_stored(db_conn)[1], "n1") == "初始"

    # 用真实版本重试 → 落库
    ok = _put(client, _candidate(meta, [_card("n1", "text", "用真实版本重试")], seq=1))
    assert ok.status_code == 200, ok.text
    assert _content_of(_stored(db_conn)[1], "n1") == "用真实版本重试"


def test_write_time_edit_keeps_newer_candidate_and_rejects_old_receipt_write(
    client: TestClient, db_conn: sqlite3.Connection
):
    """写入期间再编辑：第二版必须能继续存下去；基于旧版本的迟到写入必须被拒绝。"""
    seed = _put(client, _candidate({"state": models.empty_state(BOARD)}, [_card("n1", "text", "初始")], seq=0))
    assert seed.status_code == 200, seed.text

    first = _put(client, _candidate(_meta(client), [_card("n1", "text", "第一版")]))
    assert first.status_code == 200, first.text
    first_seq = first.json()["seq"]

    # 客户端已吸收新版本事实，继续保存第二版
    second = _put(client, _candidate(_meta(client), [_card("n1", "text", "第二版")], seq=first_seq))
    assert second.status_code == 200, second.text
    assert second.json()["seq"] == first_seq + 1
    assert _content_of(_stored(db_conn)[1], "n1") == "第二版"

    # 另一个页面还停留在第一版：不得回退
    stale_page = _put(client, _candidate(_meta(client), [_card("n1", "text", "另一页面的旧正文")], seq=first_seq))
    assert stale_page.status_code == 409, stale_page.text
    assert _content_of(_stored(db_conn)[1], "n1") == "第二版"


def test_confirm_path_cannot_bypass_version_guard(
    client: TestClient, db_conn: sqlite3.Connection
):
    """带 confirm 的保存同样先过版本门（兼容路径不是绕过版本保护的入口）。"""
    seed_state = {
        "boardId": BOARD,
        "seq": 0,
        "cards": [
            _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
            _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
            _card("n1", "text", "整理说明", checked=True),
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    assert _put(client, seed_state).status_code == 200
    created = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert created.status_code == 200, created.text
    combine = next(
        item for item in created.json()["created"] if "归为一组" in str(item.get("title"))
    )
    approved = client.post(f"/api/interactive/intents/{combine['id']}/approve", json={})
    assert approved.status_code == 200 and approved.json()["intent"]["status"] == "running"

    meta = _meta(client)
    base_seq = meta["seq"]
    material_candidate = _candidate(
        meta, [_card("m1", "file", "材料一（替换版）", meta={"name": "a.pdf"}),
               _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
               _card("n1", "text", "整理说明", checked=True)]
    )
    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": base_seq, "changeSet": {"state": material_candidate}},
    ).json()
    assert check.get("checkId"), check

    # 等待确认期间，板面被另一次保存推进了一版
    other = _candidate(_meta(client), [
        _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
        _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
        _card("n1", "text", "整理说明（等待期间的新编辑）", checked=True),
    ])
    advanced = _put(client, other)
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["seq"] == base_seq + 1

    snapshots_before = _snapshot_count(db_conn)
    late_confirm = _put(
        client,
        material_candidate,
        confirm={"checkId": check["checkId"], "stateVersion": base_seq},
    )
    assert late_confirm.status_code == 409, late_confirm.text
    detail = late_confirm.json()["detail"]
    assert detail["error"] == "stale_state", detail
    assert detail["currentSeq"] == base_seq + 1
    # 不落库、不推进快照、不暂停任务
    assert _content_of(_stored(db_conn)[1], "m1") == "材料一"
    assert _snapshot_count(db_conn) == snapshots_before
    assert _statuses(client)[combine["id"]] == "running"


def test_impact_check_reports_current_version_and_cancel_changes_nothing(
    client: TestClient, db_conn: sqlite3.Connection
):
    """检查期间又编辑 / 取消：迟到的影响检查如实报告当前版本，取消不改动任何状态。"""
    seed = _put(client, _candidate({"state": models.empty_state(BOARD)}, [_card("n1", "text", "初始")], seq=0))
    assert seed.status_code == 200, seed.text
    meta = _meta(client)

    stale_check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"] - 1, "changeSet": {"state": meta["state"]}},
    ).json()
    assert stale_check["ok"] is False
    assert stale_check["currentSeq"] == meta["seq"]

    # 用户取消：没有任何写入
    seq_before, state_before = _stored(db_conn)
    snapshots_before = _snapshot_count(db_conn)
    assert _meta(client)["seq"] == seq_before
    assert _content_of(_stored(db_conn)[1], "n1") == _content_of(state_before, "n1")
    assert _snapshot_count(db_conn) == snapshots_before
