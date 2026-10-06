"""附件显式绑定（问题 3）：`attachment_ids` 的**存在性**即语义。

契约（docs/plans/2026-10-06-audit-seven-fixes.md §1.4）：

* 字段**出现**（含 `[]`）= 显式：只绑列出的这些，`[]` = 这一轮**没有附件**；
* 字段**缺失**才走旧客户端兜底（把本话题下还没绑定任何轮次的附件绑上来）；
* 绑定前校验：存在、属于当前话题/无归属、状态有效、
  已被别的 turn 绑定的 id 不得重复绑。

本文件先于实现落地：修复前 `body.get("attachment_ids") or []` 把显式空列表变成 falsy，
后端的兜底分支照常执行 → 「用户清空附件后发纯文字」仍会把遗留附件绑到这一轮（用例红）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "explicit_binding.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _wait_terminal(client: TestClient, attachment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


def _drain_turns(client: TestClient, timeout: float = 10.0) -> None:
    client.post("/api/turns/cancel")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/turns/queue").json()["running"] is None:
            return
        time.sleep(0.02)


def _create(
    client: TestClient, tmp_path: Path, name: str, *, topic_id: str | None = "t_exp", body: bytes = b"x"
) -> dict:
    source = tmp_path / name
    source.write_bytes(body)
    payload: dict = {"source_path": str(source)}
    if topic_id is not None:
        payload["topic_id"] = topic_id
    created = client.post("/api/attachments", json=payload).json()["attachment"]
    return _wait_terminal(client, created["id"])


def _turn_row(client: TestClient, attachment_id: str) -> str | None:
    return client.app.state.ctx.attachments.get(attachment_id, check=False).turn_id


def test_explicit_empty_list_binds_nothing(client: TestClient, tmp_path: Path):
    """显式 `[]` = 这一轮没有附件：不得兜底绑上遗留附件（修复前红）。"""
    leftover = _create(client, tmp_path, "遗留.txt", topic_id="t_empty")
    assert _turn_row(client, leftover["id"]) is None

    accepted = client.post(
        "/api/turns",
        json={"message": "这条消息不带附件", "topic_id": "t_empty", "attachment_ids": []},
    ).json()

    assert accepted["accepted"] is True
    assert [item["id"] for item in accepted["attachments"]] == []
    assert _turn_row(client, leftover["id"]) is None  # 遗留附件仍未被绑定
    assert client.app.state.ctx.attachments.turn_note(accepted["turn_id"]) is None
    _drain_turns(client)


def test_missing_field_still_falls_back_for_old_clients(client: TestClient, tmp_path: Path):
    """缺字段才是旧客户端兜底路径：这条语义不能被收紧掉。"""
    leftover = _create(client, tmp_path, "旧客户端兜底.txt", topic_id="t_legacy")

    accepted = client.post(
        "/api/turns", json={"message": "老前端不带这个字段", "topic_id": "t_legacy"}
    ).json()

    assert [item["id"] for item in accepted["attachments"]] == [leftover["id"]]
    assert _turn_row(client, leftover["id"]) == accepted["turn_id"]
    _drain_turns(client)


def test_explicit_ids_do_not_steal_attachment_bound_to_another_turn(
    client: TestClient, tmp_path: Path
):
    """已被别的 turn 绑定的 id 不得重复绑（第二轮不能把它抢走）。"""
    att = _create(client, tmp_path, "先到先得.txt", topic_id="t_owner")
    first = client.post(
        "/api/turns",
        json={"message": "第一轮", "topic_id": "t_owner", "attachment_ids": [att["id"]]},
    ).json()
    assert [item["id"] for item in first["attachments"]] == [att["id"]]
    assert _turn_row(client, att["id"]) == first["turn_id"]
    _drain_turns(client)

    second = client.post(
        "/api/turns",
        json={"message": "第二轮不该抢", "topic_id": "t_owner", "attachment_ids": [att["id"]]},
    ).json()
    assert [item["id"] for item in second["attachments"]] == []
    assert _turn_row(client, att["id"]) == first["turn_id"]
    _drain_turns(client)


def test_explicit_ids_skip_attachment_from_another_topic(client: TestClient, tmp_path: Path):
    """别的话题的附件不能借显式 id 串到本话题的轮次里。"""
    other = _create(client, tmp_path, "别的话题.txt", topic_id="t_other")

    accepted = client.post(
        "/api/turns",
        json={"message": "本话题", "topic_id": "t_here", "attachment_ids": [other["id"]]},
    ).json()

    assert [item["id"] for item in accepted["attachments"]] == []
    assert _turn_row(client, other["id"]) is None
    _drain_turns(client)


def test_explicit_ids_skip_failed_and_missing(client: TestClient, tmp_path: Path):
    """failed / missing 明确不绑（状态无效的附件不该被当成「这一轮的附件」）。"""
    failed = _create(client, tmp_path, "失败.txt", topic_id="t_bad")
    missing = _create(client, tmp_path, "丢了副本.txt", topic_id="t_bad")
    ctx = client.app.state.ctx
    ctx.attachments._update(failed["id"], state="failed", error="模拟失败")
    # missing 必须是**文件世界**的事实：把 QIO 的副本删掉，GET 之后状态就是 missing
    Path(missing["stored_path"]).unlink()
    assert client.get(f"/api/attachments/{missing['id']}").json()["attachment"]["state"] == "missing"

    accepted = client.post(
        "/api/turns",
        json={
            "message": "这些都没准备好",
            "topic_id": "t_bad",
            "attachment_ids": [failed["id"], missing["id"]],
        },
    ).json()

    assert [item["id"] for item in accepted["attachments"]] == []
    assert _turn_row(client, failed["id"]) is None
    assert _turn_row(client, missing["id"]) is None
    _drain_turns(client)


def test_explicit_ids_bind_ready_and_prepared_only(client: TestClient, tmp_path: Path):
    """ready / prepared 是有效状态：显式给出时必须绑上。"""
    ready = _create(client, tmp_path, "就绪.txt", topic_id="t_ok")
    prepared = _create(client, tmp_path, "准备中.txt", topic_id="t_ok")
    client.app.state.ctx.attachments._update(prepared["id"], state="prepared", error=None)

    accepted = client.post(
        "/api/turns",
        json={
            "message": "带两个",
            "topic_id": "t_ok",
            "attachment_ids": [ready["id"], prepared["id"]],
        },
    ).json()

    assert [item["id"] for item in accepted["attachments"]] == [ready["id"], prepared["id"]]
    assert _turn_row(client, ready["id"]) == accepted["turn_id"]
    assert _turn_row(client, prepared["id"]) == accepted["turn_id"]
    _drain_turns(client)


def test_explicit_unknown_id_is_ignored_not_a_crash(client: TestClient, tmp_path: Path):
    """不存在的 id 静默跳过（不 500、不绑），真附件照常绑。"""
    ready = _create(client, tmp_path, "真附件.txt", topic_id="t_unknown")

    accepted = client.post(
        "/api/turns",
        json={
            "message": "混合 id",
            "topic_id": "t_unknown",
            "attachment_ids": ["att_does_not_exist", ready["id"]],
        },
    ).json()

    assert [item["id"] for item in accepted["attachments"]] == [ready["id"]]
    _drain_turns(client)


def test_unowned_attachment_gets_bound_and_adopts_topic(client: TestClient, tmp_path: Path):
    """无归属附件（topic_id 为空）在显式请求里可以绑，并补上当前话题。"""
    unowned = _create(client, tmp_path, "无归属.txt", topic_id=None)
    assert unowned["topic_id"] is None

    accepted = client.post(
        "/api/turns",
        json={"message": "收编", "topic_id": "t_adopt", "attachment_ids": [unowned["id"]]},
    ).json()

    assert [item["id"] for item in accepted["attachments"]] == [unowned["id"]]
    row = client.app.state.ctx.attachments.get(unowned["id"], check=False)
    assert row.turn_id == accepted["turn_id"]
    assert row.topic_id == "t_adopt"
    _drain_turns(client)
