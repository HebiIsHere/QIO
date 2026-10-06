"""上传失败 / 取消 / 断连收敛（round 4 问题三）+ 轮次绑定的回执接线（问题二路由侧）。

修复前的真缺陷（plan §0 第 3 条）：api/server.py 的 _upload_queue_put 只等队列空位、
不看工作线程是否已经结束；结束/中止哨兵还走同一个队列 —— 工作线程一死，
「队列满 + 没有消费者」就让请求永远等下去，附件停在 prepared。

本文件的时序全部用**闸门 / 事件**控制（sleep 只作为有界超时判定失败，不作为证据）：
* 写盘错误用「附件副本的目标父路径是普通文件」制造真实 OSError（mkdir 必失败）；
* 上传分块数 > 队列容量（UPLOAD_QUEUE_DEPTH），确定性暴露「消费者已退出」；
* 取消用真实 DELETE；断连让请求体生成器抛错；服务关闭用取消请求任务模拟。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 分块数必须超过队列容量，才能确定性地制造「工作线程已退出、队列没有消费者」
CHUNKS = 20
CHUNK_BYTES = 8192
#: 有限超时只用于判定失败（出现即说明收敛没做到）
DEADLINE = 5.0


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
def async_app(tmp_path: Path):
    conn = connect(tmp_path / "upload_convergence.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    _pin_attachment_data_dir(app, tmp_path)
    return app


@asynccontextmanager
async def _live(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
            yield ac


def _break_target_dir(ctx) -> Path:
    """把附件副本的目标父目录占成**普通文件**：工作线程的 mkdir 立刻抛真实 OSError。

    目录名与 AttachmentService.copy_path 一致（root/YYYY/MM）：用同一个 _local_month 取。
    """
    year, month = attachments_mod._local_month()
    parent = ctx.attachments.root / year / month
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("我不是目录", "utf-8")
    return parent


def _chunked_body(
    chunks: int,
    *,
    sent: dict | None = None,
    gate: asyncio.Event | None = None,
    fail_after: int | None = None,
):
    """分块请求体：块数远大于队列容量。

    * gate：发完第一块后挂住（模拟慢发送端），由测试决定何时继续；
    * fail_after：发到第 N 块后抛错（模拟客户端断开）。
    """

    async def body():
        for index in range(chunks):
            if sent is not None:
                sent["count"] = index + 1
            yield b"x" * CHUNK_BYTES
            if fail_after is not None and index + 1 >= fail_after:
                raise RuntimeError("客户端断开（测试模拟）")
            if gate is not None and index == 0:
                await gate.wait()

    return body()


async def _wait_for_row(ctx, *, timeout: float = DEADLINE):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = ctx.attachments.list(limit=10, check=False)
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError("上传没有登记出附件行（begin_upload 没走到）")


async def _wait_until(predicate, *, timeout: float = DEADLINE, what: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有在 {timeout}s 内满足：{what}")


def _leftovers(ctx, *ignore: Path) -> list[Path]:
    """附件根目录下的文件（可排除测试自己放的占位文件）。"""
    root = ctx.attachments.root
    if not root.is_dir():
        return []
    ignored = {str(path) for path in ignore}
    return [p for p in root.rglob("*") if p.is_file() and str(p) not in ignored]


def _active_jobs():
    """上传作业注册表（只在实现落地后存在；测试内导入，红的时候直接报缺模块）。"""
    from agent.services.attachment_upload import active_jobs

    return active_jobs()


async def test_upload_write_failure_finishes_within_the_deadline(async_app):
    """写盘失败（建目录失败）必须让请求在有限时间内结束 —— 修复前它会永远等一个没人消费的队列。"""
    ctx = async_app.state.ctx
    placeholder = _break_target_dir(ctx)
    sent = {"count": 0}

    async with _live(async_app) as ac:
        try:
            resp = await asyncio.wait_for(
                ac.post(
                    "/api/attachments/upload",
                    content=_chunked_body(CHUNKS, sent=sent),
                    headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("写盘失败.bin")},
                ),
                timeout=DEADLINE,
            )
        except asyncio.TimeoutError:  # pragma: no cover - 修复前走这里
            raise AssertionError(
                "上传没有在 %.0fs 内结束：接收端在等一个已经没有消费者的满队列（问题三）" % DEADLINE
            ) from None

    assert resp.status_code >= 400, resp.text
    detail = str(resp.json().get("detail") or "")
    assert detail, "失败必须带人话原因"
    # 接收端应当**提前**停止（工作线程一失败就不该继续灌块）
    assert sent["count"] < CHUNKS, f"接收端没有提前结束：已发送 {sent['count']}/{CHUNKS} 块"

    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"上传失败后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx, placeholder) == [], f"失败后留下了文件：{_leftovers(ctx, placeholder)}"


async def test_worker_failure_releases_a_blocked_receiver(async_app):
    """工作线程失败 → 接收端的排队等待必须被解除（不是等下一个空位）。"""
    ctx = async_app.state.ctx
    placeholder = _break_target_dir(ctx)
    sent = {"count": 0}

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, sent=sent),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("释放.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code >= 400, resp.text
    assert sent["count"] < CHUNKS, "工作线程已经失败，接收端还在继续入队"
    assert _leftovers(ctx, placeholder) == [], f"失败后留下了文件：{_leftovers(ctx, placeholder)}"
    await _wait_until(lambda: not _active_jobs(), what="上传作业没有收尾（还有等待中的任务）")


async def test_write_failure_mid_stream_converges(async_app, monkeypatch):
    """写入途中的真实失败（ENOSPC）：行转 failed、不留临时文件、请求及时结束。"""
    ctx = async_app.state.ctx
    real_open = open
    state = {"writes": 0}

    class _NoSpaceWriter:
        def __init__(self, handle) -> None:
            self._handle = handle

        def write(self, data) -> int:
            state["writes"] += 1
            if state["writes"] > 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self._handle.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            self._handle.close()
            return False

    def fake_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        return _NoSpaceWriter(handle) if "w" in str(mode) else handle

    monkeypatch.setattr(attachments_mod, "open", fake_open, raising=False)

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("途中失败.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code >= 400, resp.text
    assert "磁盘空间不足" in str(resp.json().get("detail") or "")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"失败后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"失败后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="上传作业没有收尾")


async def test_cancel_unblocks_the_worker_blocked_on_the_queue(async_app):
    """用户取消（DELETE）必须解除工作线程在队列上的阻塞读取，并且不留临时文件。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, gate=gate),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("取消我.bin")},
            )
        )
        row = await _wait_for_row(ctx)
        # 第一块已经发出去：工作线程消费它之后会阻塞在队列读取上，直到取消把它唤醒
        removed = await ac.delete(f"/api/attachments/{row.id}")
        assert removed.status_code == 200, removed.text

        await _wait_until(
            lambda: all(job.terminal for job in _active_jobs()) or not _active_jobs(),
            what="取消之后工作线程没有从阻塞读取里退出",
        )
        assert _leftovers(ctx) == [], f"取消之后留下了文件：{_leftovers(ctx)}"

        gate.set()
        done = await asyncio.wait_for(task, timeout=DEADLINE)

    assert done.status_code in (400, 404, 409), done.text
    assert ctx.attachments.get(row.id, check=False) is None
    await _wait_until(lambda: not _active_jobs(), what="上传作业没有收尾")


async def test_client_disconnect_converges(async_app):
    """客户端断开：请求体生成器抛错 → 不留临时文件、行不落成 prepared/ready。"""
    ctx = async_app.state.ctx

    async with _live(async_app) as ac:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                ac.post(
                    "/api/attachments/upload",
                    content=_chunked_body(CHUNKS, fail_after=1),
                    headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("断连.bin")},
                ),
                timeout=DEADLINE,
            )

    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"断连后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"断连后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="断连后上传作业没有收尾")


async def test_request_cancellation_converges(async_app):
    """服务关闭 / 请求被取消：工作线程要被解除阻塞并清理，行不落成 prepared/ready。"""
    ctx = async_app.state.ctx
    gate = asyncio.Event()

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS, gate=gate),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("关闭.bin")},
            )
        )
        await _wait_for_row(ctx)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    await _wait_until(lambda: not _active_jobs(), what="取消请求后上传作业没有收尾")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"取消后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"取消后留下了文件：{_leftovers(ctx)}"


async def test_write_permission_failure_converges(async_app, monkeypatch):
    """权限不足（EACCES）也是真实写盘失败：如实报错、行 failed、不留文件。"""
    ctx = async_app.state.ctx
    real_open = open

    def fake_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(attachments_mod, "open", fake_open, raising=False)

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("没权限.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code >= 400, resp.text
    assert "权限" in str(resp.json().get("detail") or "")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"失败后仍有附件停在 prepared/ready：{[(r.id, r.state) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"失败后留下了文件：{_leftovers(ctx)}"
    await _wait_until(lambda: not _active_jobs(), what="权限失败后上传作业没有收尾")


async def test_shutdown_during_commit_leaves_no_orphan_copy(async_app, monkeypatch):
    """关闭竞态：工作线程已经动手提交（os.replace 被闸门卡住）时取消请求。

    必须在**不留下孤儿副本**的前提下收敛：服务侧取消标志让最后一道闸失效，
    已经提交的副本由收尾清理，行不得落成 prepared/ready。
    """
    ctx = async_app.state.ctx
    # 闸门在**工作线程**里等：必须是 threading 语义，不能占住事件循环
    gate = threading.Event()
    release = threading.Event()
    real_replace = attachments_mod.os.replace

    def gated_replace(src, dst):
        # 工作线程里执行：告诉测试「已经走到提交这一步」，等测试取消请求后再放行
        gate.set()
        assert release.wait(5), "测试没有放行 os.replace"
        return real_replace(src, dst)

    monkeypatch.setattr(attachments_mod.os, "replace", gated_replace)

    async with _live(async_app) as ac:
        task = asyncio.create_task(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(CHUNKS),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("提交竞态.bin")},
            )
        )
        await _wait_for_row(ctx)
        # 等**工作线程**走到提交（闸门是 threading 语义：用 to_thread 等，不占事件循环）
        assert await asyncio.to_thread(lambda: gate.wait(DEADLINE)), "工作线程没有走到提交"
        task.cancel()
        await asyncio.sleep(0)  # 让取消投递到路由（它会先置取消标志、再等收尾）
        release.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    await _wait_until(lambda: not _active_jobs(), what="取消后上传作业没有收尾")
    rows = ctx.attachments.list(limit=10, check=False)
    assert all(row.state not in ("prepared", "ready") for row in rows), (
        f"取消后仍有附件停在 prepared/ready：{[(r.id, r.state, r.error) for r in rows]}"
    )
    assert _leftovers(ctx) == [], f"取消后留下了孤儿副本：{_leftovers(ctx)}"


async def test_upload_over_limit_is_413_and_leaves_nothing(async_app):
    """超限仍然 413、不留行/文件（保留原有语义，别被收敛改造破坏）。"""
    ctx = async_app.state.ctx
    ctx.attachments.max_upload_bytes = 64

    async with _live(async_app) as ac:
        resp = await asyncio.wait_for(
            ac.post(
                "/api/attachments/upload",
                content=_chunked_body(4, sent={"count": 0}),
                headers={"Content-Type": "application/octet-stream", "X-QIO-Name": quote("超标.bin")},
            ),
            timeout=DEADLINE,
        )

    assert resp.status_code == 413, resp.text
    assert ctx.attachments.list(limit=10, check=False) == []
    assert _leftovers(ctx) == []
    await _wait_until(lambda: not _active_jobs(), what="超限后上传作业没有收尾")


# -- 问题二路由接线：按 B 的冻结回执行事 -------------------------------------------------


async def _make_ready_attachment(ac, ctx, tmp_path, name="回执.txt") -> dict:
    """在**调用方已经打开的 lifespan** 里备好一个 ready 附件（TurnManager 不能被提前关掉）。"""
    topic = ctx.topics.nodes.create_topic("回执话题").id
    source = tmp_path / name
    source.write_bytes(b"receipt")
    created = (
        await ac.post("/api/attachments", json={"source_path": str(source), "topic_id": topic})
    ).json()["attachment"]
    meta = created
    for _ in range(200):
        meta = (await ac.get(f"/api/attachments/{created['id']}")).json()["attachment"]
        if meta["state"] != "prepared":
            break
        await asyncio.sleep(0.02)
    assert meta["state"] == "ready", meta
    return {**meta, "topic_id": topic}


async def test_turn_rejects_and_does_not_enqueue_when_receipt_has_rejects(async_app, tmp_path, monkeypatch):
    """预检通过、但绑定回执 rejected 非空（受理瞬间竞态）→ 409 结构化 detail 且**不入队**。"""
    ctx = async_app.state.ctx
    from agent.services.attachments import BindOutcome

    async with _live(async_app) as ac:
        att = await _make_ready_attachment(ac, ctx, tmp_path)
        monkeypatch.setattr(
            ctx.attachments,
            "bind_for_turn",
            lambda **kwargs: BindOutcome(
                bound=[], rejected=[(att["id"], "附件已经绑到别的轮次（请移除后重发）")]
            ),
        )

        before = ctx.turns.snapshot()
        resp = await ac.post(
            "/api/turns",
            json={"message": "重试", "topic_id": att["topic_id"], "attachment_ids": [att["id"]]},
        )
        after = ctx.turns.snapshot()

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [row["id"] for row in detail["rejected"]] == [att["id"]]
    assert "别的轮次" in detail["rejected"][0]["reason"]
    assert detail["message"], "结构化失败必须带一句人话"
    assert detail["bound_attachment_ids"] == []
    assert after["running"] == before["running"], "被拒绝的请求不得开始执行"
    assert after["queued"] == before["queued"], "被拒绝的请求不得入队"


async def test_turn_response_carries_the_real_binding_receipt(async_app, tmp_path, monkeypatch):
    """受理成功必须带**实际绑定回执**（bound_attachment_ids / rejected），前端以它为准。"""
    ctx = async_app.state.ctx

    async with _live(async_app) as ac:
        att = await _make_ready_attachment(ac, ctx, tmp_path, name="回执2.txt")
        resp = await ac.post(
            "/api/turns",
            json={"message": "带上它", "topic_id": att["topic_id"], "attachment_ids": [att["id"]]},
        )
        body = resp.json()
        ctx.turns.cancel(body.get("turn_id") or "")

    assert resp.status_code == 200, resp.text
    assert body["accepted"] is True
    # 回执来自 B 的真实 bind_for_turn（不是路由自己拼的）
    assert body["bound_attachment_ids"] == [att["id"]]
    assert body["rejected"] == []
    assert [item["id"] for item in body["attachments"]] == [att["id"]]
    assert ctx.attachments.get(att["id"], check=False).turn_id == body["turn_id"]


async def test_turn_rejects_attachment_owned_by_another_turn_without_enqueue(async_app, tmp_path):
    """已被别的轮次绑定的 id（且不是 retry_of_turn_id）→ 直接拒绝，不入队、不静默丢弃。

    这条在 B 的冻结实现落地前也必须成立：路由侧的受理前校验（镜像冻结规则）先挡住。
    """
    ctx = async_app.state.ctx

    async with _live(async_app) as ac:
        att = await _make_ready_attachment(ac, ctx, tmp_path, name="先到先得.txt")
        ctx.attachments.bind_for_turn("turn_owner", [att["id"]], topic_id=att["topic_id"])
        assert ctx.attachments.get(att["id"], check=False).turn_id == "turn_owner"

        before = ctx.turns.snapshot()
        resp = await ac.post(
            "/api/turns",
            json={"message": "第二轮不该抢", "topic_id": att["topic_id"], "attachment_ids": [att["id"]]},
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "attachment_binding_failed"
    assert [row["id"] for row in detail["rejected"]] == [att["id"]]
    assert "别的一轮" in detail["rejected"][0]["reason"]
    after = ctx.turns.snapshot()
    assert after["queued"] == before["queued"] and after["running"] == before["running"]
    assert ctx.attachments.get(att["id"], check=False).turn_id == "turn_owner", "原归属不得被改写"
