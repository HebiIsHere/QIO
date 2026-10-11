"""收尾轮反例测试（C）：09 长草稿静默截短 + 12 服务端草稿清除结果真实性。

正确行为期望（不是"断言异常发生"）：

- 09：20000 字（上限内）必须**完整**保存；20001 / 20008 字必须被**明确拒绝**
  （HTTP 400，{"error": "draft_too_long", "limit": ...}），不允许截短后返回成功；
  拒绝后服务器上既有的完整草稿原样保留（唯一完整副本不得被清掉）。
- 12：服务端对清除的处理必须如实返回——响应里能看到这次保存**真的清掉了哪些键**
  （cleared）、草稿的单调存簿版本号（rev）；清除后重读没有旧值；
  旧格式数据（缺 rev 包装的 plain dict）兼容，不自动覆盖或删除。
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

BOARD = "board_final_c_drafts"
LIMIT = board_store.DRAFT_MAX_CHARS  # 20000


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _put(client: TestClient, drafts: dict):
    return client.put(f"/api/interactive/drafts/{BOARD}", json={"drafts": drafts})


def _get(client: TestClient):
    resp = client.get(f"/api/interactive/drafts/{BOARD}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _padded(chars: int, tail: str) -> str:
    """长度恰为 chars 的正文，结尾带唯一标识（任何截短都会丢掉它）。"""
    fill = "写"
    result = fill * chars
    return result[: chars - len(tail)] + tail


# --- 09：上限内完整保存 ----------------------------------------------------


def test_full_save_keeps_exact_length(client: TestClient):
    chars = LIMIT  # 20000 恰好在上限内
    payload = _padded(chars, "[唯一结尾-20000]")

    resp = _put(client, {"note": payload})
    assert resp.status_code == 200, resp.text
    stored = resp.json()["drafts"]["note"]
    assert len(stored) == chars, "服务端不得截短：返回的就是实际存储的版本"
    assert stored.endswith("[唯一结尾-20000]"), "结尾唯一标识必须完整保留"

    again = _get(client)
    assert again["drafts"]["note"] == stored, "重开后再读：正文与保存结果一致"


# --- 09：超过上限必须明确拒绝，不许悄悄截短 -------------------------------


@pytest.mark.parametrize(
    ["chars", "marker"], [(LIMIT + 1, "[tail-20001]"), (LIMIT + 8, "[tail-20008]")]
)
def test_over_limit_is_rejected_not_truncated(client: TestClient, chars: int, marker: str):
    payload = _padded(chars, marker)

    resp = _put(client, {"note": payload})
    assert resp.status_code in (400, 422), f"超限必须拒绝，实际 {resp.status_code}"
    detail = resp.json()["detail"]
    assert detail["error"] == "draft_too_long"
    assert detail["limit"] == LIMIT
    assert "note" in detail["keys"]

    stored = _get(client)
    assert "note" not in stored["drafts"], "被拒绝的草稿不得以截短形式被写入"


def test_rejected_over_limit_keeps_previous_real_copy(client: TestClient):
    """先存一份完整 20000 字；再送超长的 20008 字：真实完整副本必须原样保留。"""
    good = "存" * (LIMIT - 6) + "[good]"
    first = _put(client, {"note": good})
    assert first.status_code == 200 and first.json()["drafts"]["note"] == good

    resp = _put(client, {"note": "长" * (LIMIT + 8)})
    assert resp.status_code in (400, 422)

    after = _get(client)
    assert after["drafts"]["note"] == good, "拒绝超限时不能把既有完整副本清掉或截短"


def test_multi_key_rejection_is_atomic(client: TestClient):
    """一个键超限 -> 整个请求拒绝，合法键也不得被写成一半。"""
    _put(client, {"keep_old": "旧值"})
    resp = _put(client, {"note_ok": "短稿", "note_bad": "长" * (LIMIT + 1)})
    assert resp.status_code in (400, 422)
    stored = _get(client)
    assert "note_ok" not in stored["drafts"], "部分成功会让界面误报已保存"


# --- 12：清除结果真实可见（rev / cleared） ---------------------------------


def test_clear_reports_what_it_really_cleared(client: TestClient):
    first = _put(client, {"a": "第一份", "b": "第二份"})
    assert first.status_code == 200
    rev1 = first.json()["rev"]

    second = _put(client, {"a": "第一份"})  # b 不再出现 = 清除
    body = second.json()
    assert body["cleared"] == ["b"], "响应必须如实说明这次真的清掉了哪些键"
    assert body["drafts"] == {"a": "第一份"}
    assert body["rev"] > rev1, "rev 是单调的存簿版本号"

    after = _get(client)
    assert after["drafts"] == {"a": "第一份"}, "重开 / 回读不得复活被清除的草稿"
    assert after["rev"] == body["rev"], "只读不动版本"


def test_clear_response_is_real_stored_data(client: TestClient):
    _put(client, {"a": "一"})
    cleared_body = _put(client, {}).json()
    assert cleared_body["drafts"] == {}, "发送空 drafts 后，服务端存下的就是空"
    assert cleared_body["cleared"] == ["a"]


# --- 12：旧格式数据兼容（缺新字段的 plain dict） --------------------------


def test_legacy_plain_dict_drafts_still_readable(client: TestClient, db_conn: sqlite3.Connection):
    """旧格式：drafts 列直接是 {key: text}（没有 rev 包装）。不得报错、不得清掉。"""
    board_store.ensure_board(db_conn, board_id=BOARD)
    db_conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)",
        (BOARD, json.dumps({"legacy_key": "旧格式草稿"}, ensure_ascii=False), models.now_iso()),
    )
    db_conn.commit()

    fetched = _get(client)
    assert fetched["drafts"]["legacy_key"] == "旧格式草稿"
    # 旧格式没有版本信息：rev 如实给 0，不能编一个"已保存过一次"的版本号
    assert fetched["rev"] == 0


def test_write_upgrades_legacy_format(client: TestClient, db_conn: sqlite3.Connection):
    board_store.ensure_board(db_conn, board_id=BOARD)
    db_conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)",
        (BOARD, json.dumps({"k": "旧值"}, ensure_ascii=False), models.now_iso()),
    )
    db_conn.commit()

    cleared = _put(client, {"k": "新值"}).json()
    assert cleared["rev"] == 1  # 旧格式基线 0 -> 第一次新写法保存 rev = 1
    raw = db_conn.execute("SELECT drafts FROM board_drafts WHERE board_id = ?", (BOARD,)).fetchone()
    assert json.loads(raw["drafts"]) == {"rev": 1, "drafts": {"k": "新值"}}


# --- 09 / 12：底层函数同样不得截短、必须报告真实结果 ------------------------


def test_direct_save_rejects_too_long_without_truncation(db_conn: sqlite3.Connection):
    with pytest.raises(ValueError):
        board_store.save_draft(db_conn, BOARD, {"note": "长" * (LIMIT + 1)})
    exc = None
    try:
        board_store.save_draft(db_conn, BOARD, {"note": "长" * (LIMIT + 1)})
    except ValueError as caught:  # noqa: BLE001 - 测试断言用
        exc = caught
    assert exc is not None
    assert getattr(exc, "keys", None) == ["note"]
    row = db_conn.execute("SELECT drafts FROM board_drafts WHERE board_id = ?", (BOARD,)).fetchone()
    assert row is None, "拒绝时不得落库半个草稿"
