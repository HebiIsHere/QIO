"""附件 HTTP 接口：登记 / 上传 / 查询 / 重定位 / 删除 / 与轮次绑定。

这些用例走真实的 FastAPI 路由（TestClient），断言的是**磁盘上的事实**
（副本内容、原文件未被改动）与接口返回的状态，而不是「界面上出现了卡片」。
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

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
    conn = connect(tmp_path / "attachments_api.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _wait_terminal(client: TestClient, attachment_id: str, timeout: float = 20.0) -> dict:
    """等后台复制结束：只认终态，不把 prepared 当成功。"""
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        resp = client.get(f"/api/attachments/{attachment_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


def _wait_state(client: TestClient, attachment_id: str, want: str, timeout: float = 20.0) -> dict:
    """等一个**指定**状态（重试场景下「失败」本身就是终态，不能拿终态当完成信号）。"""
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] == want:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内变成 {want}：{last}")


def _drain_turns(client: TestClient, timeout: float = 10.0) -> None:
    """把测试里提交的轮次收干净（不让后台执行影响后续用例）。"""
    client.post("/api/turns/cancel")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/turns/queue").json()["running"] is None:
            return
        time.sleep(0.02)


def test_create_copies_small_file(client: TestClient, tmp_path: Path):
    source = tmp_path / "会议纪要.txt"
    source.write_bytes("第一行\n第二行\n".encode("utf-8"))

    resp = client.post("/api/attachments", json={"source_path": str(source), "topic_id": "t1"})
    assert resp.status_code == 200, resp.text
    created = resp.json()["attachment"]
    assert created["id"].startswith("att_")
    assert created["kind"] == "copy"
    assert created["display"] == "已保存副本"
    assert created["name"] == "会议纪要.txt"
    assert created["state"] in ("prepared", "ready")

    ready = _wait_terminal(client, created["id"])
    assert ready["state"] == "ready"
    assert ready["sha256"]
    stored = Path(ready["stored_path"])
    assert stored.read_bytes() == "第一行\n第二行\n".encode("utf-8")
    assert source.read_bytes() == "第一行\n第二行\n".encode("utf-8")  # 原文件没有被搬走


def test_create_rejects_fakepath(client: TestClient):
    resp = client.post("/api/attachments", json={"source_path": "C:\\fakepath\\假路径.txt"})
    assert resp.status_code == 400
    assert "找不到这个文件" in resp.json()["detail"]


def test_upload_raw_bytes_without_multipart(client: TestClient):
    """浏览器回退：原始字节体 + X-QIO-Name 头，不需要 multipart 依赖。"""
    payload = "上传的内容\n第二行\n".encode("utf-8")
    resp = client.post(
        "/api/attachments/upload",
        content=payload,
        headers={
            "Content-Type": "application/octet-stream",
            "X-QIO-Name": quote("中文名.txt"),
            "X-QIO-Topic-Id": "t_upload",
        },
    )
    assert resp.status_code == 200, resp.text
    att = resp.json()["attachment"]
    assert att["kind"] == "copy"
    assert att["state"] == "ready"
    assert att["name"] == "中文名.txt"
    assert att["topic_id"] == "t_upload"
    assert Path(att["stored_path"]).read_bytes() == payload


def test_get_and_list(client: TestClient, tmp_path: Path):
    source = tmp_path / "清单.csv"
    source.write_bytes(b"a,b\n1,2\n")
    created = client.post(
        "/api/attachments", json={"source_path": str(source), "topic_id": "t_list"}
    ).json()["attachment"]
    _wait_terminal(client, created["id"])

    single = client.get(f"/api/attachments/{created['id']}")
    assert single.status_code == 200
    assert single.json()["attachment"]["id"] == created["id"]
    assert single.json()["attachment"]["availability"]["readable_by_tool"] is True

    listed = client.get("/api/attachments", params={"topic_id": "t_list"}).json()["attachments"]
    assert [item["id"] for item in listed] == [created["id"]]
    other_topic = client.get("/api/attachments", params={"topic_id": "nope"}).json()["attachments"]
    assert other_topic == []
    unbound = client.get("/api/attachments", params={"unbound": True}).json()["attachments"]
    assert created["id"] in [item["id"] for item in unbound]

    assert client.get("/api/attachments/att_missing").status_code == 404


def test_delete_removes_copy_but_keeps_source(client: TestClient, tmp_path: Path):
    source = tmp_path / "原件.txt"
    source.write_bytes("原件内容".encode("utf-8"))
    created = client.post("/api/attachments", json={"source_path": str(source)}).json()["attachment"]
    ready = _wait_terminal(client, created["id"])
    stored = Path(ready["stored_path"])
    assert stored.is_file()

    deleted = client.delete(f"/api/attachments/{created['id']}")
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["ok"] is True and body["removed"] is True
    assert body["deleted_copy"] == str(stored)
    assert not stored.exists()
    assert source.read_bytes() == "原件内容".encode("utf-8")  # 用户原文件必须还在
    assert client.get(f"/api/attachments/{created['id']}").status_code == 404
    assert client.delete("/api/attachments/att_missing").status_code == 404


def test_relocate_after_user_moved_the_file(client: TestClient, tmp_path: Path):
    source = tmp_path / "会被移动.txt"
    source.write_bytes("移动前".encode("utf-8"))
    created = client.post("/api/attachments", json={"source_path": str(source)}).json()["attachment"]
    _wait_terminal(client, created["id"])

    moved = tmp_path / "新目录" / "改名后.txt"
    moved.parent.mkdir(parents=True, exist_ok=True)
    source.rename(moved)

    resp = client.post(
        f"/api/attachments/{created['id']}/relocate", json={"source_path": str(moved)}
    )
    assert resp.status_code == 200, resp.text
    # 重定位现在是**受理事实**（文件 I/O 在工作线程、状态回事件循环线程落库，见问题 6）：
    # prepared 不是成功，必须按 GET 跟到终态之后再断言内容。
    assert resp.json()["attachment"]["id"] == created["id"]
    relocated = _wait_terminal(client, created["id"])
    assert relocated["state"] == "ready"
    assert relocated["source_path"] == str(moved)
    assert Path(relocated["stored_path"]).read_bytes() == "移动前".encode("utf-8")


def test_reference_file_moved_away_is_missing_and_relocatable(client: TestClient, tmp_path: Path, monkeypatch):
    from agent.services import attachments as attachments_mod

    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 16)
    source = tmp_path / "大文件.bin"
    source.write_bytes(b"v" * 32)

    created = client.post("/api/attachments", json={"source_path": str(source)}).json()["attachment"]
    assert created["kind"] == "reference"
    assert created["display"] == "引用本地文件"
    assert created["caveat"] == "历史保留的是位置，不保证内容仍然存在"
    ready = _wait_terminal(client, created["id"])
    assert ready["state"] == "ready"
    assert ready["stored_path"] is None

    source.unlink()
    missing = client.get(f"/api/attachments/{created['id']}").json()["attachment"]
    assert missing["state"] == "missing"
    assert missing["retryable"] is True

    restored = tmp_path / "换了个地方" / "大文件.bin"
    restored.parent.mkdir(parents=True, exist_ok=True)
    restored.write_bytes(b"v" * 32)
    assert client.post(
        f"/api/attachments/{created['id']}/relocate", json={"source_path": str(restored)}
    ).status_code == 200
    fixed = _wait_terminal(client, created["id"])  # 受理事实 → 跟到终态（问题 6：后台化）
    assert fixed["state"] == "ready"
    assert fixed["source_path"] == str(restored)


def test_retry_failed_attachment_reuses_the_same_record(client: TestClient, tmp_path: Path):
    source = tmp_path / "重试.txt"
    source.write_bytes("重试内容".encode("utf-8"))
    created = client.post("/api/attachments", json={"source_path": str(source)}).json()["attachment"]
    _wait_terminal(client, created["id"])

    ctx = client.app.state.ctx
    # 造一个**真实的**失败：副本不在 + 记录 failed。
    # （只改状态列、副本还在且 sha256 一致时，按契约 §1.4 属于「可验证的恢复」，
    #   GET 会如实转 ready —— 那样就不是失败态了。）
    stored = Path(created["stored_path"]) if created.get("stored_path") else None
    ready_row = client.get(f"/api/attachments/{created['id']}").json()["attachment"]
    stored = Path(ready_row["stored_path"])
    stored.unlink()
    ctx.attachments._update(created["id"], state="failed", error="模拟失败")
    failed = client.get(f"/api/attachments/{created['id']}").json()["attachment"]
    assert failed["state"] == "failed" and failed["retryable"] is True

    resp = client.post(f"/api/attachments/{created['id']}/retry")
    assert resp.status_code == 200
    again = _wait_state(client, created["id"], "ready")
    assert again["id"] == created["id"]  # 同一行重做，不新建附件
    assert Path(again["stored_path"]).read_bytes() == "重试内容".encode("utf-8")


def test_turn_accepts_explicit_attachment_ids_and_binds_before_running(client: TestClient, tmp_path: Path):
    source = tmp_path / "绑定.txt"
    source.write_bytes("绑定内容".encode("utf-8"))
    created = client.post(
        "/api/attachments", json={"source_path": str(source), "topic_id": "t_bind"}
    ).json()["attachment"]
    _wait_terminal(client, created["id"])

    accepted = client.post(
        "/api/turns",
        json={"message": "看看这个附件", "topic_id": "t_bind", "attachment_ids": [created["id"]]},
    ).json()
    assert accepted["accepted"] is True
    assert [item["id"] for item in accepted["attachments"]] == [created["id"]]

    turn_id = accepted["turn_id"]
    row = client.app.state.ctx.conn.execute(
        "SELECT turn_id FROM attachments WHERE id = ?", (created["id"],)
    ).fetchone()
    assert row["turn_id"] == turn_id  # 受理时就已经绑上，不是执行完才补
    assert client.app.state.ctx.attachments.turn_note(turn_id) is not None
    _drain_turns(client)


def test_turn_fallback_binds_only_same_topic_unbound(client: TestClient, tmp_path: Path):
    first = tmp_path / "话题A.txt"
    first.write_bytes("A".encode("utf-8"))
    second = tmp_path / "话题B.txt"
    second.write_bytes("B".encode("utf-8"))
    att_a = client.post(
        "/api/attachments", json={"source_path": str(first), "topic_id": "t_a"}
    ).json()["attachment"]
    att_b = client.post(
        "/api/attachments", json={"source_path": str(second), "topic_id": "t_b"}
    ).json()["attachment"]
    _wait_terminal(client, att_a["id"])
    _wait_terminal(client, att_b["id"])

    # 在话题 A 里发消息：只兜底绑 A 的附件，不能吞掉 B 的
    accepted = client.post("/api/turns", json={"message": "在 A 里说话", "topic_id": "t_a"}).json()
    assert [item["id"] for item in accepted["attachments"]] == [att_a["id"]]
    _drain_turns(client)

    ctx = client.app.state.ctx
    assert ctx.attachments.get(att_b["id"]).turn_id is None


def test_turn_rejects_non_list_attachment_ids(client: TestClient):
    resp = client.post("/api/turns", json={"message": "hi", "attachment_ids": "att_1"})
    assert resp.status_code == 400
    assert "attachment_ids" in resp.json()["detail"]


def test_explicit_ids_do_not_pull_in_other_unbound_attachments(client: TestClient, tmp_path: Path):
    chosen = tmp_path / "选中的.txt"
    chosen.write_bytes("chosen".encode("utf-8"))
    other = tmp_path / "没选中的.txt"
    other.write_bytes("other".encode("utf-8"))
    att_chosen = client.post(
        "/api/attachments", json={"source_path": str(chosen), "topic_id": "t_pick"}
    ).json()["attachment"]
    att_other = client.post(
        "/api/attachments", json={"source_path": str(other), "topic_id": "t_pick"}
    ).json()["attachment"]
    _wait_terminal(client, att_chosen["id"])
    _wait_terminal(client, att_other["id"])

    accepted = client.post(
        "/api/turns",
        json={
            "message": "只带一个附件",
            "topic_id": "t_pick",
            "attachment_ids": [att_chosen["id"]],
        },
    ).json()
    assert [item["id"] for item in accepted["attachments"]] == [att_chosen["id"]]
    ctx = client.app.state.ctx
    assert ctx.attachments.get(att_other["id"]).turn_id is None
    _drain_turns(client)
async def test_read_attachment_is_registered_and_reads_through_the_real_pipeline(
    client: TestClient, tmp_path: Path
):
    """注册 + 真实管线读回内容（不是「卡片出现了」）。

    create_app 在构建 AppContext 之后注册附件服务与 read_attachment（不改 A 的 app.py），
    这里既断言工具真的在 spec 里，也让调用走 registry 的完整管线（审批/超时/校验都经过）。
    """
    from agent.adapters.base import ToolCall

    ctx = client.app.state.ctx
    assert "read_attachment" in [spec.name for spec in ctx.registry.specs()]

    source = tmp_path / "管线.txt"
    source.write_bytes("管线里的第一行\n第二行\n".encode("utf-8"))
    created = client.post(
        "/api/attachments", json={"source_path": str(source), "topic_id": "t_e2e"}
    ).json()["attachment"]
    _wait_terminal(client, created["id"])

    call = ToolCall(
        id="call_attach_1",
        name="read_attachment",
        arguments={"attachment_id": created["id"], "offset": 0, "limit": 5},
    )
    result = await ctx.registry.execute(call)
    assert result.ok is True, result.error
    assert "管线里的第一行" in result.content
    assert "第二行" in result.content
    assert '"total_units": 2' in result.content

    # 不存在的 id：如实说没有，并告诉模型怎么发现附件（不编造内容）
    missing = await ctx.registry.execute(
        ToolCall(id="call_attach_2", name="read_attachment", arguments={"attachment_id": "att_nope"})
    )
    assert missing.ok is False
    assert "没有这个附件 id" in (missing.error or "")
