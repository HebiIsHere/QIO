"""F01 定向验收：草稿逐键更新 / 明确删除 / 未修改三态与版本保护（契约 C1）。

覆盖任务书 F01 要求的五类场景：
1. 两页改不同卡片：另一页面的旧快照保存不得删掉本页已保存的草稿；
2. 同卡片冲突：不静默覆盖，双方文字都在响应里（保留输入并报告冲突）；
3. 删除与更新相撞：删除有身份与版本保护，迟到的更新不许复活、也不丢自己的文字；
4. 迟到回执：baseRev 过期的增量保存仍然只动它自己那些键；
5. 旧客户端 / 旧存储格式的明确兼容行为（未列出的键不再是无条件删除）。

全程只走真实 HTTP 路由 + 真实 board_store，不碰模型、不碰用户正式数据。
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

BOARD = "board_f01_patch"


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _patch(client: TestClient, **body) -> dict:
    payload = {"mode": "patch"}
    payload.update(body)
    resp = client.put(f"/api/interactive/drafts/{BOARD}", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _legacy(client: TestClient, drafts: dict, base_rev: int | None = None) -> dict:
    body: dict = {"drafts": drafts}
    if base_rev is not None:
        body["baseRev"] = base_rev
    resp = client.put(f"/api/interactive/drafts/{BOARD}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _get(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/drafts/{BOARD}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 1. 两页改不同卡片 ------------------------------------------------------


def test_other_page_patch_does_not_delete_saved_draft(client: TestClient):
    """A 保存 card:a（rev=1）→ B 用**旧快照**（baseRev=0）只保存 card:b：card:a 必须还在。"""
    a = _patch(client, baseRev=0, drafts={"card:a": "A 页写了一半"}, keyVersions={})
    assert a["drafts"] == {"card:a": "A 页写了一半"}
    rev_a = a["rev"]

    # B 页读到的是更早的快照（baseRev=0），而且它只改自己的卡片
    b = _patch(client, baseRev=0, drafts={"card:b": "B 页的问题"}, keyVersions={})
    assert b["drafts"] == {"card:a": "A 页写了一半", "card:b": "B 页的问题"}
    assert b["cleared"] == [], "未出现在请求里的键不构成删除"
    assert b["blockedRemovals"] == []
    assert b["rev"] > rev_a

    stored = _get(client)
    assert stored["drafts"] == {"card:a": "A 页写了一半", "card:b": "B 页的问题"}
    assert stored["keyVersions"]["card:a"] == 1
    assert stored["keyVersions"]["card:b"] == 1


def test_independent_keys_coordinate_across_stale_bases(client: TestClient):
    """两页各自基于同一旧版本改**不同**键：两边都要留下，不许按最后到达覆盖。"""
    seed = _patch(client, baseRev=0, drafts={"card:seed": "种子"})
    page_a = _patch(client, baseRev=0, drafts={"card:a": "甲"}, keyVersions={"card:a": seed["rev"]})
    page_b = _patch(client, baseRev=0, drafts={"card:b": "乙"})
    final = _get(client)
    assert final["drafts"] == {"card:seed": "种子", "card:a": "甲", "card:b": "乙"}
    assert page_a["conflicts"] == [] and page_b["conflicts"] == []


# --- 2. 同卡片冲突：保留双方文字 --------------------------------------------


def test_same_key_conflict_keeps_both_texts(client: TestClient):
    first = _patch(client, baseRev=0, drafts={"card:a": "甲的第一版"}, keyVersions={})
    version_a = first["keyVersions"]["card:a"]
    # 甲这一页又改了一次：服务器上的 card:a 前进到第二版
    second = _patch(
        client,
        baseRev=first["rev"],
        drafts={"card:a": "甲的第二版"},
        keyVersions={"card:a": version_a},
    )
    assert second["conflicts"] == []
    moved = second["keyVersions"]["card:a"]
    assert moved == version_a + 1

    # B 页手里还是「甲的第一版」那一版（keyVersions 写的是它），却要改成别的内容
    stale = _patch(
        client,
        baseRev=0,
        drafts={"card:a": "乙的版本"},
        keyVersions={"card:a": version_a},
    )
    assert stale["drafts"]["card:a"] == "甲的第二版", "冲突不得静默覆盖服务器上的文字"
    assert stale["conflicts"] == [
        {"key": "card:a", "local": "乙的版本", "server": "甲的第二版"}
    ], "冲突必须同时保留双方文字"
    assert stale["rev"] == second["rev"], "纯冲突不落库、不推进版本"

    again = _get(client)
    assert again["drafts"]["card:a"] == "甲的第二版"
    assert again["keyVersions"]["card:a"] == moved


def test_same_key_overwrite_allowed_on_matching_key_version(client: TestClient):
    """客户端拿着**当前**逐键版本再改同一个键：这是有依据的覆盖，不是冲突。"""
    first = _patch(client, baseRev=0, drafts={"card:a": "第一版"}, keyVersions={})
    version = first["keyVersions"]["card:a"]
    second = _patch(
        client,
        baseRev=0,
        drafts={"card:a": "第二版"},
        keyVersions={"card:a": version},
    )
    assert second["conflicts"] == []
    assert second["drafts"]["card:a"] == "第二版"
    assert second["keyVersions"]["card:a"] == version + 1


def test_same_key_overwrite_allowed_on_matching_base_rev(client: TestClient):
    first = _patch(client, baseRev=0, drafts={"card:a": "第一版"})
    second = _patch(client, baseRev=first["rev"], drafts={"card:a": "第二版"})
    assert second["conflicts"] == []
    assert second["drafts"]["card:a"] == "第二版"


# --- 3. 删除与更新相撞 ------------------------------------------------------


def test_removal_needs_identity_and_version(client: TestClient):
    first = _patch(client, baseRev=0, drafts={"card:a": "要删的草稿"})
    version = first["keyVersions"]["card:a"]

    # 不带 baseRev、也不带正确版本：无法证明这次删除基于当前版本 → 保留并报告
    blocked = _patch(client, drafts={}, removed=["card:a"])
    assert blocked["cleared"] == []
    assert blocked["blockedRemovals"] == ["card:a"]
    assert blocked["drafts"]["card:a"] == "要删的草稿"
    assert {"key": "card:a", "local": "", "server": "要删的草稿"} in blocked["conflicts"]

    # 带上正确版本：这次删除有身份与版本保护，允许落地
    removed = _patch(client, baseRev=first["rev"], drafts={}, removed=["card:a"])
    assert removed["cleared"] == ["card:a"]
    assert "card:a" not in removed["drafts"]
    assert _get(client)["drafts"] == {}


def test_stale_update_does_not_resurrect_explicitly_removed_key(client: TestClient):
    first = _patch(client, baseRev=0, drafts={"card:a": "旧稿"})
    version = first["keyVersions"]["card:a"]
    removed = _patch(client, baseRev=first["rev"], drafts={}, removed=["card:a"])
    assert removed["cleared"] == ["card:a"]

    # 另一个页面手里还是删除前那一版，迟到的更新不许把它复活
    late = _patch(client, baseRev=0, drafts={"card:a": "迟到的更新"}, keyVersions={"card:a": version})
    assert late["conflicts"] == [{"key": "card:a", "local": "迟到的更新", "server": "旧稿"}]
    assert "card:a" not in late["drafts"], "迟到更新不许把已删除的键复活"
    assert _get(client)["drafts"] == {}

    # 而知道这次删除（版本对齐）的客户端可以明确重新写回
    tombstone = removed["rev"]
    rewritten = _patch(client, baseRev=tombstone, drafts={"card:a": "重新写"}, keyVersions={})
    assert rewritten["drafts"]["card:a"] == "重新写"


# --- 4. 迟到回执 ------------------------------------------------------------


def test_late_receipt_only_touches_its_own_keys(client: TestClient):
    seed = _patch(client, baseRev=0, drafts={"card:seed": "种子"})
    newer = _patch(client, baseRev=seed["rev"], drafts={"card:new": "新的"})
    # 迟到的请求带着更早的 baseRev：它只能更新自己声明的键
    late = _patch(client, baseRev=seed["rev"], drafts={"card:a": "迟到但只改自己"})
    final = _get(client)
    assert final["drafts"] == {
        "card:seed": "种子",
        "card:new": "新的",
        "card:a": "迟到但只改自己",
    }
    assert late["cleared"] == [] and late["blockedRemovals"] == []
    assert newer["drafts"]["card:new"] == "新的"


# --- 5. 旧客户端 / 旧存储格式兼容 ------------------------------------------


def test_legacy_replace_never_deletes_unlisted_keys(client: TestClient):
    """旧请求体（无 mode/baseRev/removed）：未列出的键**不是**无条件删除。"""
    _legacy(client, {"card:a": "A 页的草稿"})
    body = _legacy(client, {"card:b": "B 页的草稿"})
    assert body["legacyReplace"] is True
    assert body["cleared"] == []
    assert body["blockedRemovals"] == ["card:a"], "没有安全基线时旧语义也不许隐式删除"
    assert body["drafts"] == {"card:a": "A 页的草稿", "card:b": "B 页的草稿"}
    assert _get(client)["drafts"] == {"card:a": "A 页的草稿", "card:b": "B 页的草稿"}


def test_legacy_replace_with_matching_base_rev_may_delete(client: TestClient):
    """旧客户端只要带上可判定的 baseRev（=当前 rev）且无并发改写，删除仍然允许。"""
    first = _legacy(client, {"card:a": "要清掉的", "card:b": "留着"})
    body = _legacy(client, {"card:b": "留着"}, base_rev=first["rev"])
    assert body["legacyReplace"] is True
    assert body["cleared"] == ["card:a"]
    assert body["drafts"] == {"card:b": "留着"}


def test_legacy_plain_dict_is_readable_and_protected(client: TestClient, db_conn: sqlite3.Connection):
    """旧存储（plain dict，无版本信息）：可读，且旧请求体不许隐式删掉它。"""
    board_store.ensure_board(db_conn, board_id=BOARD)
    db_conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)",
        (BOARD, json.dumps({"legacy_key": "旧格式草稿"}, ensure_ascii=False), models.now_iso()),
    )
    db_conn.commit()

    fetched = _get(client)
    assert fetched["drafts"] == {"legacy_key": "旧格式草稿"}
    assert fetched["rev"] == 0, "旧格式没有版本信息：如实给 0"

    body = _legacy(client, {"another": "新键"})
    assert body["drafts"]["legacy_key"] == "旧格式草稿", "旧格式的草稿同样不许被隐式删除"
    assert body["blockedRemovals"] == ["legacy_key"]
    assert body["legacyReplace"] is True


def test_patch_without_base_or_version_reports_conflict_instead_of_overwrite(client: TestClient):
    """patch 但既没有 baseRev 也没有可对齐版本：同键相撞按冲突处理，不覆盖。"""
    _legacy(client, {"card:a": "服务器上的"})
    body = _patch(client, drafts={"card:a": "客户端的"})
    assert body["conflicts"] == [
        {"key": "card:a", "local": "客户端的", "server": "服务器上的"}
    ]
    assert body["drafts"]["card:a"] == "服务器上的"


def test_patch_validation_and_atomicity(client: TestClient):
    _patch(client, baseRev=0, drafts={"keep": "旧值"})
    bad = client.put(
        f"/api/interactive/drafts/{BOARD}",
        json={"mode": "patch", "baseRev": "三", "drafts": {}},
    )
    assert bad.status_code == 400
    bad2 = client.put(
        f"/api/interactive/drafts/{BOARD}",
        json={"mode": "patch", "drafts": {}, "removed": "card:a"},
    )
    assert bad2.status_code == 400
    bad3 = client.put(
        f"/api/interactive/drafts/{BOARD}",
        json={"mode": "patch", "drafts": {"card:a": "x"}, "removed": ["card:a"]},
    )
    assert bad3.status_code == 400
    bad4 = client.put(
        f"/api/interactive/drafts/{BOARD}",
        json={"mode": "patch", "drafts": {}, "keyVersions": {"card:a": "1"}},
    )
    assert bad4.status_code == 400
    assert _get(client)["drafts"] == {"keep": "旧值"}, "校验失败不得部分写入"
