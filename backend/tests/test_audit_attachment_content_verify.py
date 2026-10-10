"""D 独立验证：历史附件打开（审计问题 5 / plan §1.6 的后端一半）。

契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 5 条 + §1.6。
只依据产品规则：

* 新增 GET /api/attachments/{id}/content：**只读 QIO 管理的副本**（kind=copy 且 state=ready）；
* 按 id 取路径，**绝不接受任意路径**；
* 借用型（引用型）只记位置、不复制内容，因此不得由这个入口提供内容（不得放开任意路径读取）；
* 删除用户原文件之后，副本必须仍然可打开（这正是「打开副本」的意义）。

基线（e428bb9）现状：没有这个端点（只有元数据 / relocate / retry / delete）—— 因此
本文件在修复前应当是**红的**。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_audit_attachment_content_verify.py -q
"""

from __future__ import annotations

import time

import pytest

COPY_MAX = 100_000_000
TERMINAL_STATES = {"ready", "failed", "missing", "changed"}


@pytest.fixture()
def app_client(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    with TestClient(app) as client:
        yield client, app


def _unwrap(body: dict) -> dict:
    nested = body.get("attachment")
    return nested if isinstance(nested, dict) else body


def _post_source(client, path) -> dict:
    resp = client.post("/api/attachments", json={"source_path": str(path)})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    return _unwrap(resp.json())


def _wait_terminal(client, attachment_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = _unwrap(client.get(f"/api/attachments/{attachment_id}").json())
        if str(last.get("state")) in TERMINAL_STATES:
            return last
        time.sleep(0.05)
    pytest.fail(f"附件长时间没有进入终态：{last}")


def _route_declared(client) -> bool:
    paths = client.get("/openapi.json").json()["paths"]
    return any(str(p).endswith("/content") and "/attachments/" in str(p) for p in paths)


# ---- 1. 副本内容可读，且读的是 QIO 自己的副本 ---------------------------------------


def test_copy_content_is_served_after_the_source_file_is_gone(app_client, tmp_path):
    client, _app = app_client
    if not _route_declared(client):
        pytest.fail(
            "契约 §1.6：缺 GET /api/attachments/{id}/content（历史附件没有可用的打开链路）"
        )
    body_bytes = "副本内容：这一行必须一字不差地读回来。\n".encode("utf-8")
    source = tmp_path / "原件.txt"
    source.write_bytes(body_bytes)
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    assert meta["state"] == "ready", meta

    # 用户把原文件删掉：副本仍然必须能打开（否则「打开副本」毫无意义）
    source.unlink()
    resp = client.get(f"/api/attachments/{created['id']}/content")
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    assert resp.content == body_bytes, ("打开入口必须给出真实副本内容", resp.content[:120])
    ctype = str(resp.headers.get("content-type") or "")
    assert "text/html" not in ctype, ("不得把文件内容当网页返回", ctype)


# ---- 2. 绝不接受任意路径 -----------------------------------------------------------


def test_arbitrary_paths_are_never_served(app_client, tmp_path):
    client, _app = app_client
    secret = tmp_path / "不该被读到.txt"
    secret.write_text("这不是附件，绝不能通过附件入口读出来", encoding="utf-8")

    for candidate in (
        str(secret),
        str(secret).replace("\\", "%5C"),
        "../../../../etc/passwd",
        "C:\\Windows\\win.ini",
    ):
        resp = client.get(f"/api/attachments/{candidate}/content")
        assert resp.status_code >= 400, (
            "附件入口只按 id 取 QIO 管理副本，不得接受任意路径",
            candidate,
            resp.status_code,
        )
        assert "这不是附件" not in resp.text, ("任意路径被读出来了", candidate)


# ---- 3. 引用型 / 非 ready 不提供内容 -----------------------------------------------


def test_reference_attachment_is_not_served_directly(app_client, tmp_path):
    """引用型只记位置（>100MB），不得由这个入口把任意路径的内容吐出来。"""
    client, _app = app_client
    if not _route_declared(client):
        pytest.fail("契约 §1.6：缺 GET /api/attachments/{id}/content")
    source = tmp_path / "被引用的.bin"
    with open(source, "wb") as handle:
        handle.truncate(COPY_MAX + 1)
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    assert meta["kind"] == "reference", meta

    resp = client.get(f"/api/attachments/{created['id']}/content")
    assert resp.status_code >= 400, (
        "引用型不得由内容入口直接提供（只读 QIO 管理的副本）",
        resp.status_code,
    )


def test_content_is_not_served_when_the_copy_is_gone(app_client, tmp_path):
    client, _app = app_client
    if not _route_declared(client):
        pytest.fail("契约 §1.6：缺 GET /api/attachments/{id}/content")
    source = tmp_path / "副本会消失.txt"
    source.write_text("副本马上被删掉", encoding="utf-8")
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    stored = str(meta.get("stored_path") or "")
    assert stored, meta

    from pathlib import Path

    Path(stored).unlink(missing_ok=True)
    now = _unwrap(client.get(f"/api/attachments/{created['id']}").json())
    assert str(now.get("state")) != "ready", ("副本不在了就不能继续冒充 ready", now)
    resp = client.get(f"/api/attachments/{created['id']}/content")
    assert resp.status_code >= 400, (
        "副本不在时必须如实失败，不能假装还能打开",
        resp.status_code,
    )
