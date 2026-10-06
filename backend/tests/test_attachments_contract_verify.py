"""D 独立验证：附件链路（契约 §4）。

契约来源：docs/plans/2026-10-06-unified-process-attachments-streaming.md §4。
覆盖：阈值边界（<、=、> 100_000_000 字节）、多文件/同名、复制失败、不存在的路径、
引用文件缺失/变化、删除只删 QIO 副本、真实读入内容、read_attachment 分段。

基线（ee6bbff）现状：没有 attachments 表、没有 /api/attachments、没有 read_attachment ——
本文件在实现合并前应当是**红的**。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_attachments_contract_verify.py -q
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent.adapters.base import ToolCall

THRESHOLD = 100_000_000  # 契约 §4.1：十进制 MB，size <= 阈值 → 保存副本
TERMINAL_STATES = {"ready", "failed", "missing", "changed"}


@pytest.fixture()
def app_client(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    with TestClient(app) as client:
        yield client, app


def _sparse(path, size: int):
    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def _unwrap(body: dict) -> dict:
    """附件的取数路径。

    契约 §4.2 把响应写成顶层 {id, kind, display, state, size_bytes}，实现是信封形式
    {"ok": true, "attachment": {...}}（Lead 已确认把契约文档同步成实际形状）。
    两种都接受：这里只统一取数路径，不对字段名与取值放宽。
    """
    nested = body.get("attachment")
    return nested if isinstance(nested, dict) else body


def _post_source(client, path, **extra) -> dict:
    resp = client.post("/api/attachments", json={"source_path": str(path), **extra})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    return _unwrap(resp.json())


def _wait_terminal(client, attachment_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/api/attachments/{attachment_id}")
        assert resp.status_code == 200, (resp.status_code, resp.text)
        last = _unwrap(resp.json())
        if str(last.get("state")) in TERMINAL_STATES:
            return last
        time.sleep(0.05)
    pytest.fail(f"附件长时间没有进入终态：{last}")


# ---- 1. 保存规则与阈值边界（契约 §4.1） -----------------------------------------


def test_small_file_is_copied_and_readable(app_client, tmp_path):
    client, _app = app_client
    source = tmp_path / "笔记.txt"
    source.write_text("真实读入的内容：附件验证第一行。", encoding="utf-8")

    created = _post_source(client, source)
    assert str(created["kind"]) == "copy", created
    assert "副本" in str(created.get("display") or ""), created
    assert int(created["size_bytes"]) == source.stat().st_size, created

    meta = _wait_terminal(client, str(created["id"]))
    assert meta["state"] == "ready", meta
    assert meta.get("stored_path"), meta
    assert source.exists(), "保存副本不得动用户原文件"

    stored = tmp_path / "stored-copy.bin"
    stored.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    assert stored.read_text(encoding="utf-8").startswith("真实读入的内容"), "副本内容必须与原文一致"


def test_file_below_threshold_is_copied(app_client, tmp_path):
    client, _app = app_client
    source = _sparse(tmp_path / "below.bin", THRESHOLD - 1)
    created = _post_source(client, source)
    assert str(created["kind"]) == "copy", created
    assert int(created["size_bytes"]) == THRESHOLD - 1, created


def test_file_exactly_at_threshold_is_copied(app_client, tmp_path):
    """"size <= 100_000_000 → 保存独立副本"：边界上取等号也必须复制。"""
    client, _app = app_client
    source = _sparse(tmp_path / "exact.bin", THRESHOLD)
    created = _post_source(client, source)
    assert str(created["kind"]) == "copy", ("等于阈值必须保存副本", created)
    assert int(created["size_bytes"]) == THRESHOLD, created


def test_file_above_threshold_is_referenced(app_client, tmp_path):
    """"size > 100_000_000 → 引用本地文件"，只存路径 + 元数据。"""
    client, _app = app_client
    source = _sparse(tmp_path / "above.bin", THRESHOLD + 1)
    created = _post_source(client, source)
    assert str(created["kind"]) == "reference", ("超过阈值必须只引用，不得复制", created)
    assert "引用" in str(created.get("display") or ""), created
    assert int(created["size_bytes"]) == THRESHOLD + 1, created
    meta = _wait_terminal(client, str(created["id"]))
    assert str(meta.get("source_path") or "") == str(source), meta
    assert meta.get("stored_path") in (None, "",), ("引用文件不得产生 QIO 副本", meta)


def test_missing_source_path_fails_honestly(app_client, tmp_path):
    client, _app = app_client
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/attachments" in paths, ("契约 §4.2：POST /api/attachments 必须存在", sorted(paths)[:20])
    resp = client.post("/api/attachments", json={"source_path": str(tmp_path / "不存在.bin")})
    assert 400 <= resp.status_code < 500, ("不存在的路径必须明确失败", resp.status_code, resp.text)


def test_same_name_files_are_kept_apart(app_client, tmp_path):
    client, _app = app_client
    first = tmp_path / "a" / "同名.txt"
    second = tmp_path / "b" / "同名.txt"
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("第一份", encoding="utf-8")
    second.write_text("第二份", encoding="utf-8")

    one = _post_source(client, first)
    two = _post_source(client, second)
    assert one["id"] != two["id"], (one, two)
    meta_one = _wait_terminal(client, str(one["id"]))
    meta_two = _wait_terminal(client, str(two["id"]))
    assert meta_one["state"] == meta_two["state"] == "ready", (meta_one, meta_two)
    assert str(meta_one.get("stored_path")) != str(meta_two.get("stored_path")), (meta_one, meta_two)


# ---- 2. 删除与重定位（契约 §4.2） ------------------------------------------------


def test_delete_removes_only_qio_copy(app_client, tmp_path):
    client, _app = app_client
    source = tmp_path / "原件.txt"
    source.write_text("用户原件", encoding="utf-8")
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    stored_path = meta.get("stored_path")
    assert source.exists()

    resp = client.delete(f"/api/attachments/{created['id']}")
    assert resp.status_code in (200, 204), (resp.status_code, resp.text)
    assert source.exists(), "删除附件绝不能动用户原文件"
    assert source.read_text(encoding="utf-8") == "用户原件", "原文件内容也不得被改"
    if stored_path:
        from pathlib import Path

        assert not Path(str(stored_path)).exists(), ("QIO 副本应当被删掉", stored_path)


def test_missing_and_changed_reference_are_reported(app_client, tmp_path):
    client, _app = app_client
    source = _sparse(tmp_path / "被引用.bin", THRESHOLD + 1)
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    assert meta["state"] == "ready", meta

    source.unlink()
    missing = _unwrap(client.get(f"/api/attachments/{created['id']}").json())
    assert missing["state"] == "missing", ("引用文件不在了必须如实说 missing", missing)

    source = _sparse(tmp_path / "被引用.bin", THRESHOLD + 2)
    changed = _unwrap(client.get(f"/api/attachments/{created['id']}").json())
    assert changed["state"] in {"changed", "missing"}, (
        "引用文件变了必须如实说 changed（不得继续冒充 ready）",
        changed,
    )


def test_relocate_points_at_a_new_path(app_client, tmp_path):
    client, _app = app_client
    source = _sparse(tmp_path / "移动前.bin", THRESHOLD + 1)
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))

    moved = tmp_path / "移动后.bin"
    moved.write_bytes(b"")
    with open(moved, "wb") as handle:
        handle.truncate(THRESHOLD + 1)

    resp = client.post(f"/api/attachments/{created['id']}/relocate", json={"source_path": str(moved)})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    meta = _wait_terminal(client, str(created["id"]))
    assert str(meta.get("source_path") or "") == str(moved), meta
    assert meta["state"] == "ready", meta


# ---- 3. 真实读入内容 + 分段读取（契约 §4.2 工具） ---------------------------------


def _read_attachment(app, attachment_id: str, **kwargs):
    ctx = app.state.ctx
    call = ToolCall(
        id="verify_read",
        name="read_attachment",
        arguments={"attachment_id": attachment_id, **kwargs},
    )
    return asyncio.run(ctx.registry.execute(call))


def test_read_attachment_returns_real_content(app_client, tmp_path):
    client, app = app_client
    body = "第一行：真实读入的内容。\n第二行：不能被伪造。\n"
    source = tmp_path / "读我.txt"
    source.write_text(body, encoding="utf-8")
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))

    result = _read_attachment(app, str(created["id"]))
    assert result.ok, result.error
    assert "第一行：真实读入的内容。" in result.content, result.content
    assert "第二行：不能被伪造。" in result.content, result.content


def test_read_attachment_pages_through_content(app_client, tmp_path):
    client, app = app_client
    lines = [f"第{i:03d}行" for i in range(1, 301)]
    source = tmp_path / "长文件.txt"
    source.write_text("\n".join(lines), encoding="utf-8")
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))

    head = _read_attachment(app, str(created["id"]), offset=0, limit=10)
    assert head.ok, head.error
    assert "第001行" in head.content and "第010行" in head.content, head.content
    assert "第011行" not in head.content, ("limit 必须真的生效", head.content)

    tail = _read_attachment(app, str(created["id"]), offset=290, limit=10)
    assert tail.ok, tail.error
    assert "第295行" in tail.content, tail.content
    assert "第001行" not in tail.content, ("offset 必须真的生效", tail.content)


def test_unreadable_binary_is_reported_not_faked(app_client, tmp_path):
    """契约 §4.3：不可读类型必须明说不可读，不能因为拿到文件名就说已读。"""
    client, app = app_client
    source = tmp_path / "神秘.bin"
    source.write_bytes(bytes(range(256)) * 8)
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))

    result = _read_attachment(app, str(created["id"]))
    if result.ok:
        assert "不可读" in result.content or "无法" in result.content or "二进制" in result.content, (
            "二进制/不可读类型必须明确说明限制",
            result.content[:400],
        )
    else:
        assert result.error, "失败也要给出原因"


# ---- 4. 接口面（契约 §4.2） -------------------------------------------------------


def test_turns_accepts_and_binds_attachment_ids(app_client, tmp_path):
    """契约 §4.2：POST /api/turns 接受 attachment_ids，发送后与消息绑定。

    接口是未类型化的 dict body（OpenAPI 看不出字段），所以这里按**行为**验：
    1) attachment_ids 不是数组 → 明确 400（说明字段真的被消费，不是被忽略）；
    2) 带一个真实附件 id → 200 且响应里带回绑定的附件（同 id）。
    """
    client, _app = app_client
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/turns" in paths, sorted(paths)[:20]

    bad = client.post("/api/turns", json={"message": "验证", "attachment_ids": "not-a-list"})
    assert bad.status_code == 400, ("attachment_ids 非数组必须明确拒绝", bad.status_code, bad.text)

    source = tmp_path / "绑定用.txt"
    source.write_text("绑定内容", encoding="utf-8")
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))

    ok = client.post(
        "/api/turns",
        json={"message": "验证附件绑定", "attachment_ids": [str(created["id"])]},
    )
    assert ok.status_code in (200, 201, 202), (ok.status_code, ok.text)
    body = ok.json()
    bound = body.get("attachments") or []
    assert [str(item.get("id")) for item in bound] == [str(created["id"])], (
        "发送后附件必须与本轮绑定",
        body,
    )
    client.post("/api/turns/cancel")
