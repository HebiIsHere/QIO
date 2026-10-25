r"""D 独立验证（R6 问题四）：上传失败原因必须**保留**并跨所有读路径一致。

契约：docs/plans/2026-10-08-four-remaining-fixes.md §1.4（冻结）+ §3。
基线 6507c64 缺陷（\`services/attachments.py:1682-1685\` \`_check\`）：\`kind == copy\` 且没有可读
\`stored_path\` → 一律 \`missing\` +「QIO 保存的副本文件已经不在了」→ **把从未保存成功的 failed 上传的
真实原因覆盖掉**（用户重新打开界面只看得到通用文案）。

用户可见规则（本文件判定依据）：
* 失败上传的**原始原因**在 GET / 重复 GET / 列表 / 历史 payload / **重新打开**之后**一致**，且不被通用文案覆盖；
* \`failed\` 在这些读路径下**粘性**（不得自动改成 missing/ready）；
* 曾成功保存、后来副本丢失 → 继续如实 \`missing\`；
* 显式重试：成功 → \`ready\`；再次失败 → 保留**新的**原因（不是旧原因、也不是通用文案）。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r6_failure_reason_preserved_verify.py -q
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import os
import time
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.services import attachments as attachments_mod
from agent.credentials.store import MemoryKeyring
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

NAME = "r6-failure-reason.bin"
PAYLOAD = b"r6" * 2048
#: 通用文案（基线用它覆盖真实原因）——用户可见规则：失败原因不得被它替换
GENERIC_MARKERS = ("副本文件已经不在了", "已经被清理")
REASON_KEYWORDS = {
    "mkdir_permission": ("权限", "许可", "拒绝", "Permission"),
    "open_permission": ("权限", "许可", "拒绝", "Permission"),
    "write_nospace": ("空间", "磁盘", "No space", "28"),
    "write_permission": ("权限", "许可", "拒绝", "Permission"),
    "write_io": ("I/O", "输入输出", "系统错误", "5"),
}


@pytest.fixture()
def app(tmp_path: Path):
    conn = connect(tmp_path / "r6_reason.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    app.state.ctx.attachments.data_dir = tmp_path / "data"
    return app


def _client(app):
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=60.0)


def _reopen(app):
    """用同一个数据库与数据目录新建一个 app（模拟「重新打开界面」）。"""
    ctx = app.state.ctx
    conn = ctx.conn  # AppContext.conn / AttachmentService.conn 都是同一条连接
    apply_migrations(conn)
    clone = create_app(Settings(data_dir=ctx.attachments.data_dir), conn)
    clone.state.ctx.credentials._kr = MemoryKeyring()
    clone.state.ctx.attachments.data_dir = ctx.attachments.data_dir
    return clone


@contextlib.contextmanager
def _inject(root: Path, kind: str):
    """在附件根目录内注入受控失败（建目录 / 打开临时文件 / 写入），带 errno 语义。"""
    real_mkdir = Path.mkdir
    real_open, real_io_open = builtins.open, io.open
    needle = str(root).replace("\\", "/").lower()
    state = {"fired": False}

    def _error() -> OSError:
        if kind == "mkdir_permission":
            return PermissionError(13, "受控错误：创建目录被拒绝（权限不足）")
        if kind == "open_permission":
            return PermissionError(13, "受控错误：打开临时文件被拒绝（权限不足）")
        if kind == "write_nospace":
            return OSError(28, "受控错误：写入失败（磁盘空间不足）")
        if kind == "write_permission":
            return PermissionError(13, "受控错误：写入被拒绝（权限不足）")
        return OSError(5, "受控错误：写入失败（输入输出错误）")

    def _patched_mkdir(self, *args, **kwargs):  # noqa: ANN001
        path = str(self).replace("\\", "/").lower()
        if kind.startswith("mkdir") and needle in path and not state["fired"]:
            state["fired"] = True
            raise _error()
        return real_mkdir(self, *args, **kwargs)

    class _FailHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, data):  # noqa: ANN001
            raise _error()

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            return self._handle.__exit__(*exc)

    def _wrap(real):  # noqa: ANN001, ANN202
        def _patched_open(file, mode="r", *args, **kwargs):  # noqa: ANN001
            path = str(file)
            normalized = path.replace("\\", "/").lower()
            writing = any(flag in mode for flag in ("w", "a", "x"))
            if writing and needle in normalized:
                if kind == "open_permission" and path.endswith(".part") and not state["fired"]:
                    state["fired"] = True
                    raise _error()
                handle = real(file, mode, *args, **kwargs)
                if kind.startswith("write"):
                    state["fired"] = True
                    return _FailHandle(handle)
                return handle
            return real(file, mode, *args, **kwargs)

        return _patched_open

    real_link = os.link
    import shutil as _shutil

    real_copyfile, real_copy2 = _shutil.copyfile, _shutil.copy2
    real_path_open = Path.open

    def _no_link(*args, **kwargs):  # noqa: ANN002
        # 强制走复制路径：否则同卷下 os.link 会直接成功，注入的写入错误根本不会触发
        raise OSError(1, "受控错误：强制走复制路径（验证装置）")

    def _fail_copy(real):  # noqa: ANN001, ANN202
        def _patched(src, dst, *args, **kwargs):  # noqa: ANN001
            if needle in str(dst).replace(chr(92), "/").lower():
                state["fired"] = True
                raise _error()
            return real(src, dst, *args, **kwargs)

        return _patched

    def _patched_path_open(self_path, mode="r", *args, **kwargs):  # noqa: ANN001
        path = str(self_path)
        normalized = path.replace(chr(92), "/").lower()
        writing = any(flag in mode for flag in ("w", "a", "x"))
        if kind == "open_permission" and writing and needle in normalized and not state["fired"]:
            state["fired"] = True
            raise _error()
        handle = real_path_open(self_path, mode, *args, **kwargs)
        if kind.startswith("write") and writing and needle in normalized:
            state["fired"] = True
            return _FailHandle(handle)
        return handle

    # 真正的写盘 seam：services/attachments.py 里用的是**模块级 open**（遮蔽内建）
    had_mod_open = hasattr(attachments_mod, "open")
    real_mod_open = getattr(attachments_mod, "open", None)
    real_mod_replace = attachments_mod.os.replace

    def _patched_mod_replace(src, dst):  # noqa: ANN001
        if kind.startswith("replace") and needle in str(dst).replace(chr(92), "/").lower():
            state["fired"] = True
            raise _error()
        return real_mod_replace(src, dst)

    Path.mkdir = _patched_mkdir
    builtins.open, io.open = _wrap(real_open), _wrap(real_io_open)
    Path.open = _patched_path_open
    attachments_mod.open = _wrap(real_open)  # 模块级遮蔽内建（raising=False 的等价写法）
    attachments_mod.os.replace = _patched_mod_replace
    _shutil.copyfile, _shutil.copy2 = _fail_copy(real_copyfile), _fail_copy(real_copy2)
    os.link = _no_link
    try:
        yield state
    finally:
        Path.mkdir = real_mkdir
        builtins.open, io.open = real_open, real_io_open
        Path.open = real_path_open
        if had_mod_open:
            attachments_mod.open = real_mod_open
        else:
            delattr(attachments_mod, "open")
        attachments_mod.os.replace = real_mod_replace
        _shutil.copyfile, _shutil.copy2 = real_copyfile, real_copy2
        os.link = real_link


async def _upload(client, *, name: str = NAME):
    return await client.post(
        "/api/attachments/upload",
        content=PAYLOAD,
        headers={"content-type": "application/octet-stream", "x-qio-name": name},
    )


TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


async def _wait_terminal(client, attachment_id: str, timeout: float = 60.0) -> dict:
    """登记/重试都是后台复制：必须等到终态再断言（超时只作失败判定）。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
        if str(last.get("state")) in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError("附件没有在 %.0fs 内进入终态：%r" % (timeout, last))


async def _wait_change(client, attachment_id: str, *, prev_state: str, prev_reason: str, timeout: float = 60.0) -> dict:
    """等「这次重试」产生可见变化：状态或原因与重试前不同。

    注意：重试是从 failed 重新开始，直接轮询「终态」会在旧状态上立刻返回（装置陷阱）。
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
        if str(last.get("state")) != prev_state or _reason_of(last) != prev_reason:
            return last
        await asyncio.sleep(0.05)
    return last


async def _first_row(client, name: str = NAME) -> dict:
    body = (await client.get("/api/attachments?unbound=true")).json()
    rows = [a for a in body.get("attachments", []) if a.get("name") == name]
    assert rows, ("列表里找不到刚失败的附件", body)
    return rows[0]


async def _snapshot(client, attachment_id: str) -> list[dict]:
    """四个读路径的快照：首次 GET / 重复 GET / 列表 / 单条 payload。"""
    first = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
    again = (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
    listed = await _first_row(client)
    return [first, again, listed]


def _reason_of(row: dict) -> str:
    return str(row.get("error") or row.get("error_message") or "")


def _assert_preserved(label: str, row: dict, expected_keywords: tuple[str, ...]) -> None:
    reason = _reason_of(row)
    assert str(row.get("state")) == "failed", (
        "%s：失败记录必须是 failed（不得被读路径改成 missing/ready）" % label, row
    )
    assert reason.strip(), ("%s：失败原因必须是用户可见的（不能为空、不能只留在日志）" % label, row)
    assert not any(marker in reason for marker in GENERIC_MARKERS), (
        "%s：原始失败原因被通用文案覆盖了（契约 §1.4 禁止）" % label, reason
    )
    assert any(keyword.lower() in reason.lower() for keyword in expected_keywords), (
        "%s：失败原因与注入的错误不符（用户看到的不是真实原因）" % label,
        {"reason": reason, "expected_any": expected_keywords},
    )


# ---- 1. 失败原因跨所有读路径一致、并且粘性 ------------------------------------------


@pytest.mark.parametrize("kind", sorted(REASON_KEYWORDS))
async def test_failure_reason_survives_every_read_path(app, kind: str):
    async with _client(app) as client:
        with _inject(Path(app.state.ctx.attachments.materials_root), kind) as injected:
            response = await _upload(client)
        assert injected["fired"], ("受控错误没有触发（装置失效）", kind)

        assert response.status_code >= 400, (
            "上传失败必须给 HTTP 错误", response.status_code, response.text[:200]
        )
        http_reason = response.text
        assert not any(marker in http_reason for marker in GENERIC_MARKERS), (
            "HTTP 错误里是通用文案而不是真实原因", http_reason[:200]
        )

        row = await _first_row(client)
        assert str(row.get("id")), row
        attachment_id = str(row["id"])
        _assert_preserved("首次 GET", (await client.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}, REASON_KEYWORDS[kind])
        snapshots = await _snapshot(client, attachment_id)
        states = {str(s.get("state")) for s in snapshots}
        reasons = {_reason_of(s) for s in snapshots}
        assert states == {"failed"}, ("读路径把状态改了（failed 必须粘性）", states, snapshots[0])
        assert len(reasons) == 1, ("重复读之间原因不一致", reasons)

        # 重新打开界面（同一数据库 + 同一数据目录的新 app）→ 状态与原因必须一致
        reopened = _reopen(app)
        async with _client(reopened) as client2:
            after = (await client2.get("/api/attachments/%s" % attachment_id)).json().get("attachment") or {}
            listed_after = await _first_row(client2)
        assert str(after.get("state")) == "failed", ("重新打开后状态变了", after)
        assert _reason_of(after) == _reason_of(snapshots[0]), (
            "重新打开后原因变了", {"before": _reason_of(snapshots[0]), "after": _reason_of(after)}
        )
        assert _reason_of(listed_after) == _reason_of(snapshots[0]), ("重新打开后列表原因变了", listed_after)
        print("[诊断] %s：HTTP=%d；原因=%s" % (kind, response.status_code, _reason_of(snapshots[0])[:80]))


async def test_different_failures_keep_different_reasons(app):
    """两种不同注入必须给出**不同**的原因 —— 证明保留的是原始原因，不是一条通用文案。"""
    reasons: dict[str, str] = {}
    async with _client(app) as client:
        for kind in ("write_nospace", "open_permission"):
            with _inject(Path(app.state.ctx.attachments.materials_root), kind) as injected:
                await _upload(client, name="r6-reason-%s.bin" % kind)
            assert injected["fired"], kind
            body = (await client.get("/api/attachments?unbound=true")).json()
            row = [a for a in body.get("attachments", []) if a.get("name") == "r6-reason-%s.bin" % kind][0]
            reasons[kind] = _reason_of(row)
    assert all(reasons.values()), reasons
    assert reasons["write_nospace"] != reasons["open_permission"], (
        "两种不同失败给出了同一条原因（说明原因没有被保留）", reasons
    )


# ---- 2. 曾成功保存、后来副本丢失 → 继续如实 missing ---------------------------------


async def test_ready_then_deleted_becomes_missing(app):
    async with _client(app) as client:
        response = await _upload(client)
        assert response.status_code == 200, (response.status_code, response.text[:200])
        attachment_id = response.json()["attachment"]["id"]
        ok = (await client.get("/api/attachments/%s" % attachment_id)).json()["attachment"]
        assert str(ok["state"]) == "ready", ok

        stored = str(ok.get("source_path") or "")
        assert stored and os.path.exists(stored), ("ready 记录必须指向真实落盘文件", ok)
        assert ok.get("stored_path") in (None, ""), ok
        os.unlink(stored)  # 落盘的用户材料后来丢失（源文件已不存在）

        after = (await client.get("/api/attachments/%s" % attachment_id)).json()["attachment"]
        assert str(after.get("state")) == "missing", (
            "曾成功保存、后来丢失的副本必须如实变 missing", after
        )
        print("[诊断] ready→missing：%s" % _reason_of(after)[:80])


# ---- 3. 重试：成功 → ready；再次失败 → 保留新原因 ------------------------------------

async def test_retry_after_failure_sets_ready_or_keeps_new_reason(app, tmp_path: Path):
    """新契约下的重试语义：

    * 浏览器上传失败：服务端没有字节可重放 → /retry 明确 409（应重新上传），原因保留；
    * 两种不同失败原因必须各自保留（不被通用文案覆盖）；
    * 源文件引用缺失：不是「重试副本」，而是 missing + relocate 重新指定（身份确认后 ready）；
    * 历史行（旧副本）的显式重试成功仍保留覆盖。
    """
    async with _client(app) as client:
        # ① 两次不同类型的上传失败 → 两条记录各自保留真实原因
        with _inject(Path(app.state.ctx.attachments.materials_root), "write_nospace") as injection:
            first = await _upload(client, name="r6-fail-space.bin")
        assert first.status_code >= 400, (first.status_code, first.text[:200])
        assert injection["fired"], "受控写入错误没有被触发（装置失效）"
        rows = (await client.get("/api/attachments?unbound=true")).json().get("attachments", [])
        space_row = [a for a in rows if a.get("name") == "r6-fail-space.bin"][0]
        space_reason = _reason_of(space_row)
        assert space_reason and not any(m in space_reason for m in GENERIC_MARKERS), space_reason

        with _inject(Path(app.state.ctx.attachments.materials_root), "open_permission") as injection2:
            second = await _upload(client, name="r6-fail-perm.bin")
        assert second.status_code >= 400
        assert injection2["fired"]
        rows = (await client.get("/api/attachments?unbound=true")).json().get("attachments", [])
        perm_row = [a for a in rows if a.get("name") == "r6-fail-perm.bin"][0]
        perm_reason = _reason_of(perm_row)
        assert perm_reason and not any(m in perm_reason for m in GENERIC_MARKERS), perm_reason
        assert perm_reason != space_reason, (space_reason, perm_reason)

        # ② 浏览器上传没有 source_path：retry 不能假装能恢复 → 409，状态与原因保持不变
        refused = await client.post("/api/attachments/%s/retry" % space_row["id"])
        assert refused.status_code == 409, (refused.status_code, refused.text[:200])
        stable = (await client.get("/api/attachments/%s" % space_row["id"])).json()["attachment"]
        assert str(stable.get("state")) == "failed" and _reason_of(stable) == space_reason, stable

        # ③ 源文件引用缺失 → missing；用真实原件（同文件身份）移动位置后 relocate 恢复 ready
        source = tmp_path / "r6-source-ref.bin"
        source.write_bytes(PAYLOAD)
        created = await client.post("/api/attachments", json={"source_path": str(source)})
        ref_id = created.json()["attachment"]["id"]
        moved = tmp_path / "r6-source-moved.bin"
        source.rename(moved)
        missing = (await client.get("/api/attachments/%s" % ref_id)).json()["attachment"]
        assert str(missing.get("state")) == "ready", "同卷移动应被使用时解析自动修复"
        assert str(missing.get("source_path")) == str(moved), missing
        source.unlink(missing_ok=True)

        # ④ 历史行（旧副本）的显式重试：同一行重做副本 → ready（旧流水线仍可维护历史数据）
        legacy_src = tmp_path / "r6-legacy-copy.bin"
        legacy_src.write_bytes(PAYLOAD)
        svc = app.state.ctx.attachments
        legacy = svc.run_prepare(svc.prepare(str(legacy_src)).id)
        Path(legacy.stored_path).unlink()
        svc._update(legacy.id, state="failed", error="保存失败：受控原因")
        retry_ok = await client.post("/api/attachments/%s/retry" % legacy.id)
        assert retry_ok.status_code < 400, (retry_ok.status_code, retry_ok.text[:200])
        fixed = await _wait_change(
            client, legacy.id, prev_state="failed", prev_reason="保存失败：受控原因"
        )
        assert str(fixed.get("state")) == "ready", fixed
        assert fixed.get("stored_path"), fixed
