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


def _pin_attachment_data_dir(app, tmp_path: Path) -> None:
    """把附件的真实落点钉在 tmp_path。

    已知陷阱（Lead 2026-10-07 确认的代码事实）：config.Settings.__post_init__ 会用环境变量
    QIO_DATA_DIR **覆盖**构造时显式传入的 data_dir。tests/conftest.py 会 pop 掉它，但把用例
    放在仓外跑（或 conftest 没被加载）时，Settings(data_dir=tmp_path) 就会写进用户真实数据目录。
    所以走 create_app 的附件测试必须再钉一次服务自己的 data_dir（root 由它派生）。
    """
    data_dir = tmp_path / "data"
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)
    app.state.ctx.attachments.data_dir = data_dir


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "explicit_binding.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    _pin_attachment_data_dir(app, tmp_path)
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
    """已被别的 turn 绑定的 id 不得重复绑。

    round4 §1.2 冻结语义（取代 round1 的「静默跳过」）：这种情况必须**结构化拒绝**
    （409 + 每个附件的人话原因）、不入队、不改原归属 —— 不允许静默丢弃，
    也不允许「后端已经开始执行后才发现附件丢了」。
    """
    att = _create(client, tmp_path, "先到先得.txt", topic_id="t_owner")
    first = client.post(
        "/api/turns",
        json={"message": "第一轮", "topic_id": "t_owner", "attachment_ids": [att["id"]]},
    ).json()
    assert [item["id"] for item in first["attachments"]] == [att["id"]]
    assert _turn_row(client, att["id"]) == first["turn_id"]
    _drain_turns(client)

    before = client.app.state.ctx.turns.snapshot()
    refused = client.post(
        "/api/turns",
        json={"message": "第二轮不该抢", "topic_id": "t_owner", "attachment_ids": [att["id"]]},
    )
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [item["id"] for item in detail["rejected"]] == [att["id"]]
    assert "别的一轮" in detail["rejected"][0]["reason"]
    assert detail["bound_attachment_ids"] == []
    after = client.app.state.ctx.turns.snapshot()
    assert after["queued"] == before["queued"], "被拒绝的请求不得入队"
    assert _turn_row(client, att["id"]) == first["turn_id"], "原归属不得被改写"
    _drain_turns(client)


def test_explicit_ids_skip_attachment_from_another_topic(client: TestClient, tmp_path: Path):
    """别的话题的附件不能借显式 id 串到本话题的轮次里（round4 §1.2：结构化拒绝）。"""
    other = _create(client, tmp_path, "别的话题.txt", topic_id="t_other")

    before = client.app.state.ctx.turns.snapshot()
    refused = client.post(
        "/api/turns",
        json={"message": "本话题", "topic_id": "t_here", "attachment_ids": [other["id"]]},
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [item["id"] for item in detail["rejected"]] == [other["id"]]
    assert "另一个话题" in detail["rejected"][0]["reason"]
    assert client.app.state.ctx.turns.snapshot()["queued"] == before["queued"]
    assert _turn_row(client, other["id"]) is None
    _drain_turns(client)


def test_explicit_ids_skip_failed_and_missing(client: TestClient, tmp_path: Path):
    """failed / missing 不许当成「这一轮的附件」：round4 §1.2 也是结构化拒绝。

    状态校验按**现在的事实**（GET 之后 missing/changed），拒绝要让用户看到是哪一条、为什么。
    """
    failed = _create(client, tmp_path, "失败.txt", topic_id="t_bad")
    missing = _create(client, tmp_path, "丢了副本.txt", topic_id="t_bad")
    ctx = client.app.state.ctx
    # 造一个**真实的**失败：副本不在 + 记录 failed。
    # （只改状态列、副本还在且 sha256 一致时，按契约 §1.4 属于「可验证的恢复」，
    #   GET 会如实转 ready —— 那样它就不是 failed，用例的前提也不成立。）
    Path(failed["stored_path"]).unlink()
    ctx.attachments._update(failed["id"], state="failed", error="模拟失败")
    # missing 必须是**文件世界**的事实：把 QIO 的副本删掉，GET 之后状态就是 missing
    Path(missing["stored_path"]).unlink()
    assert client.get(f"/api/attachments/{missing['id']}").json()["attachment"]["state"] == "missing"

    refused = client.post(
        "/api/turns",
        json={
            "message": "这些都没准备好",
            "topic_id": "t_bad",
            "attachment_ids": [failed["id"], missing["id"]],
        },
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert [item["id"] for item in detail["rejected"]] == [failed["id"], missing["id"]]
    assert all(item["reason"] for item in detail["rejected"]), "每个附件都要有人话原因"
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


def test_explicit_unknown_id_is_refused_not_a_crash(client: TestClient, tmp_path: Path):
    """不存在的 id 不 500、不静默丢：409 + 逐条原因，真附件也不能被「顺带绑上」。

    round4 §1.2：任何 id 不满足「存在 / 同话题 / 状态允许 / 未绑定或绑定在 retry_of_turn_id」
    都进 rejected；rejected 非空 → 整条请求不入队。
    """
    ready = _create(client, tmp_path, "真附件.txt", topic_id="t_unknown")

    before = client.app.state.ctx.turns.snapshot()
    refused = client.post(
        "/api/turns",
        json={
            "message": "混合 id",
            "topic_id": "t_unknown",
            "attachment_ids": ["att_does_not_exist", ready["id"]],
        },
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [item["id"] for item in detail["rejected"]] == ["att_does_not_exist"]
    assert "没有这个附件" in detail["rejected"][0]["reason"]
    assert client.app.state.ctx.turns.snapshot()["queued"] == before["queued"]
    # 混合请求整体被拒：真附件也不得被绑上（不允许半绑定状态）
    assert _turn_row(client, ready["id"]) is None
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
