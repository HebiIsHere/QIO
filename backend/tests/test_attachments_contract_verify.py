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
import os
import sys
import time

import pytest

from agent.adapters.base import ToolCall

# R9：旧的「≤100MB 自动复制 / >100MB 只记路径」副本策略已删除。
# 任意大小的桌面路径都只登记源文件引用、不复制；上传大小限制是**传输资源限制**。
LEGACY_COPY_THRESHOLD = 100_000_000
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


def test_small_file_is_registered_without_copy(app_client, tmp_path):
    client, _app = app_client
    source = tmp_path / "笔记.txt"
    source.write_text("真实读入的内容：附件验证第一行。", encoding="utf-8")
    before = source.stat()

    created = _post_source(client, source)
    assert str(created["kind"]) == "reference", created
    assert "源文件引用" in str(created.get("display") or ""), created
    assert int(created["size_bytes"]) == source.stat().st_size, created

    meta = _wait_terminal(client, str(created["id"]))
    assert meta["state"] == "ready", meta
    assert meta.get("stored_path") in (None, ""), "新入口不得产生副本"
    assert str(meta.get("source_path")) == str(source), meta
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert not (tmp_path / "data" / "attachments").exists()


def test_any_size_path_registration_never_copies(app_client, tmp_path):
    """替代旧的阈值用例：任意大小（<、=、> 100MB）都只登记源引用、不复制。"""
    client, _app = app_client
    sizes = [1, LEGACY_COPY_THRESHOLD - 1, LEGACY_COPY_THRESHOLD, LEGACY_COPY_THRESHOLD + 1]
    for index, size in enumerate(sizes):
        source = _sparse(tmp_path / f"size-{index}.bin", size)
        created = _post_source(client, source)
        assert str(created["kind"]) == "reference", (size, created)
        assert created.get("stored_path") in (None, ""), (
            f"{size} 字节也不得复制",
            created,
        )
        assert int(created["size_bytes"]) == size, (size, created)
        assert str(created.get("source_path")) == str(source), (size, created)
    assert not (tmp_path / "data" / "attachments").exists(), "任何大小都不得产生 QIO 副本"


def test_upload_size_limit_is_a_transport_limit_not_copy_rule(app_client, tmp_path):
    """上传大小限制是传输资源限制，与「副本策略」无关（独立说明与独立拒绝）。"""
    client, app = app_client
    service = app.state.ctx.attachments
    service.max_upload_bytes = 16
    resp = client.post(
        "/api/attachments/upload",
        content=b"x" * 32,
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": "too-big.bin"},
    )
    assert resp.status_code == 413, (resp.status_code, resp.text)
    materials = service.materials_root
    leftovers = [p for p in materials.rglob("*") if p.is_file()] if materials.is_dir() else []
    assert leftovers == [], f"超限上传不得留下材料文件：{leftovers}"


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
    assert meta_one.get("stored_path") in (None, "") and meta_two.get("stored_path") in (None, ""), (
        "同名文件都不得产生副本",
    )
    assert meta_one.get("source_path") != meta_two.get("source_path"), (meta_one, meta_two)
    # 同名不同文件：文件身份不同，绝不能被当成同一份
    assert meta_one.get("file_id") != meta_two.get("file_id"), (meta_one, meta_two)


# ---- 2. 删除与重定位（契约 §4.2） ------------------------------------------------


def test_delete_removes_only_reference(app_client, tmp_path):
    client, _app = app_client
    source = tmp_path / "原件.txt"
    source.write_text("用户原件", encoding="utf-8")
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))
    assert source.exists()

    resp = client.delete(f"/api/attachments/{created['id']}")
    assert resp.status_code in (200, 204), (resp.status_code, resp.text)
    body = resp.json()
    assert body.get("deleted_copy") in (None, ""), "新语义不删任何文件"
    assert body.get("kept_source") == str(source)
    assert source.exists(), "删除附件绝不能动用户原文件"
    assert source.read_text(encoding="utf-8") == "用户原件", "原文件内容也不得被改"


def test_missing_and_changed_reference_are_reported(app_client, tmp_path):
    client, _app = app_client
    source = _sparse(tmp_path / "被引用.bin", LEGACY_COPY_THRESHOLD + 1)
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    assert meta["state"] == "ready", meta

    # POSIX 上 unlink 后立刻在**同一路径**重建文件，文件系统可能复用同一个 inode
    # （ext4 实测会），于是「换了一份文件」在身份判据上看起来仍是同一个文件 —— 这会把
    # 平台行为差异误读成产品缺陷。这里先用一个打开的文件描述符**钉住旧 inode**，
    # 保证重建得到的是不同身份；Windows 不允许删除已打开的文件，所以只在 POSIX 上这样做。
    hold_open = None
    if sys.platform != "win32":
        hold_open = os.open(str(source), os.O_RDONLY)
    source.unlink()
    missing = _unwrap(client.get(f"/api/attachments/{created['id']}").json())
    assert missing["state"] == "missing", ("源文件不在了必须如实说 missing", missing)
    assert missing.get("missing_reason"), missing

    # 新文件要在**旧 inode 仍被打开时**创建（上面钉住），否则释放后 inode 可能被复用。
    source = _sparse(tmp_path / "被引用.bin", LEGACY_COPY_THRESHOLD + 2)
    if hold_open is not None:
        os.close(hold_open)
    changed = _unwrap(client.get(f"/api/attachments/{created['id']}").json())
    assert changed["state"] == "changed", (
        "路径上的文件换了一份必须如实说 changed（不得继续冒充 ready）",
        changed,
    )
    assert changed.get("reference_state") == "ambiguous", changed


def test_relocate_after_move_keeps_identity(app_client, tmp_path):
    client, _app = app_client
    source = _sparse(tmp_path / "移动前.bin", LEGACY_COPY_THRESHOLD + 1)
    created = _post_source(client, source)
    _wait_terminal(client, str(created["id"]))

    moved = tmp_path / "移动后.bin"
    source.rename(moved)  # 同卷移动/改名：文件身份不变

    resp = client.post(
        f"/api/attachments/{created['id']}/relocate", json={"source_path": str(moved)}
    )
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    meta = _wait_terminal(client, str(created["id"]))
    assert str(meta.get("source_path") or "") == str(moved), meta
    assert meta["state"] == "ready", meta
    assert meta["reference_state"] == "relocated", meta


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
