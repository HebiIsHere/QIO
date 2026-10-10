"""契约 4（D）：事件闸门 —— 路由/文件 I/O 被闸门卡住时，事件循环必须仍然可响应。

验收点（契约 4）：
- 闸门未释放时（Future/Event 手动控制），经**真实事件循环**并发跑 fastapi 健康
  请求（asgi 直连）+ 取消请求能推进；证据 = 推进完成顺序与时间戳（不以毫秒阈值
  为唯一判据）；
- 释放后路由结果、缓存全部正确。
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import suppress

import httpx
import pytest
from fastapi import FastAPI

from agent.adapters.base import ToolSpec
from agent.services.tool_router import CORE_TOOLS, ToolRouter
from agent.tools import fs_tools
from agent.tools.fs_tools import FsReadTool


class RecordingEmbedding:
    """确定性嵌入后端：记录每次调用的批量大小与执行线程（与 route_async 测试一致）。"""

    def __init__(
        self,
        name: str = "fake",
        model_name: str = "m1",
        dims: int = 32,
        delay: float = 0.0,
        gate: threading.Event | None = None,
    ) -> None:
        import hashlib

        import numpy as np

        self._np = np
        self._hashlib = hashlib
        self.name = name
        self.model_name = model_name
        self.dims = dims
        self.delay = delay
        self.gate = gate
        self.calls: list[tuple[int, int]] = []

    def available(self) -> bool:
        return True

    def embed_texts(self, texts):
        self.calls.append((len(texts), threading.get_ident()))
        if self.gate is not None:
            if not self.gate.wait(15):
                raise RuntimeError("gate timeout in test (should not happen)")
        if self.delay:
            time.sleep(self.delay)
        out = []
        np = self._np
        for t in texts:
            v = np.zeros(self.dims, dtype=np.float32)
            for tok in (t or "").split():
                b = int(self._hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dims
                v[b] += 1.0
            n = float(np.linalg.norm(v)) or 1e-9
            out.append(v / n)
        return np.stack(out)


def _specs(n: int = 12) -> list[ToolSpec]:
    specs = [
        ToolSpec(name="memory_search", description="检索历史记忆", parameters={}),
        ToolSpec(name="switch_topic", description="切换话题", parameters={}),
        ToolSpec(name="create_topic", description="创建话题", parameters={}),
        ToolSpec(name="continue_from_fragment", description="从片段继续", parameters={}),
    ]
    for i in range(n):
        specs.append(
            ToolSpec(name=f"tool_{i}", description=f"does thing {i}", parameters={})
        )
    return specs


def _health_app(timeline: list[str]) -> FastAPI:
    app = FastAPI()

    @app.get("/api/health")
    async def health() -> dict:
        timeline.append(f"health-done@{time.perf_counter():.4f}")
        return {"ok": True}

    return app


async def test_gated_route_async_does_not_block_event_loop():
    gate = threading.Event()
    emb = RecordingEmbedding(gate=gate)
    router = ToolRouter(top_n=40, embedding=emb)
    specs = _specs(10)

    route_task = asyncio.ensure_future(router.route_async("天气查询", specs))
    await asyncio.sleep(0.1)  # embed 任务已进入线程并被闸门卡住
    assert not route_task.done(), "gate closed → route_async should still be pending"

    timeline: list[str] = []
    app = _health_app(timeline)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # 取消请求能推进：被取消的协程立刻结束（不排队等路由）
    cancel_target = asyncio.ensure_future(asyncio.sleep(50))
    timeline.append(f"cancel-requested@{time.perf_counter():.4f}")
    cancel_target.cancel()
    cancelled = False
    with suppress(asyncio.CancelledError):
        await cancel_target
    cancelled = cancel_target.cancelled()
    assert cancelled
    timeline.append(f"cancel-done@{time.perf_counter():.4f}")

    assert not route_task.done(), "gate still closed; route must not have finished"

    gate.set()
    result = await asyncio.wait_for(route_task, timeout=10)
    timeline.append(f"route-done@{time.perf_counter():.4f}")

    # 释放后路由结果与缓存全部正确
    names = [s.name for s in result]
    for core in CORE_TOOLS:
        assert core in names
    assert len(router._tool_vecs) == 10

    # 证据（完成顺序）：健康请求与取消都在路由完成之前推进
    idx_health = next(i for i, t in enumerate(timeline) if t.startswith("health-done"))
    idx_cancel = next(i for i, t in enumerate(timeline) if t.startswith("cancel-done"))
    idx_route = next(i for i, t in enumerate(timeline) if t.startswith("route-done"))
    assert idx_health < idx_route and idx_cancel < idx_route, timeline
    print("[event-gate 证据] 顺序与时间戳：")
    for entry in timeline:
        print("  " + entry)


async def test_gated_fs_read_does_not_block_event_loop():
    gate = threading.Event()
    original = fs_tools._read_file_text

    def gated_read(path) -> str:
        gate.wait(15)
        return original(path)

    fs_tools._read_file_text = gated_read
    try:
        from pathlib import Path

        root = Path.cwd()
        target = root / "gate_probe.txt"
        target.write_text("probe", encoding="utf-8")

        class _Sandbox:
            def root(self):
                return root

            def resolve_in_root(self, path: str) -> Path:
                return target.resolve()

            def read_verdict(self, path: str) -> str:
                return "auto"

            def contains(self, path) -> bool:
                return True

            @property
            def mode(self) -> str:
                return "default"

        class _Approval:
            async def request(self, kind, payload):
                return type("R", (), {"decision": "approved"})()

        tool = FsReadTool()
        tool.computer = _Sandbox()
        tool.approvals = _Approval()

        read_task = asyncio.ensure_future(tool.run(path=str(target)))
        await asyncio.sleep(0.1)
        assert not read_task.done(), "gate closed → fs_read should still be pending"

        timeline: list[str] = []
        app = _health_app(timeline)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert not read_task.done()

        gate.set()
        result = await asyncio.wait_for(read_task, timeout=10)
        timeline.append(f"read-done@{time.perf_counter():.4f}")
        assert result.ok and result.content == "probe"

        idx_health = next(i for i, t in enumerate(timeline) if t.startswith("health-done"))
        idx_read = next(i for i, t in enumerate(timeline) if t.startswith("read-done"))
        assert idx_health < idx_read, timeline
    finally:
        fs_tools._read_file_text = original
        (Path.cwd() / "gate_probe.txt").unlink(missing_ok=True)


async def test_concurrent_read_only_fs_ops_finish(tmp_path):
    """并发只读 fs（超过 executor 线程数）：全部完成、内容正确（不丢不串）。"""
    files = []
    for i in range(6):
        p = tmp_path / f"c{i}.txt"
        p.write_text(f"content-{i}", encoding="utf-8")
        files.append(p)

    from pathlib import Path

    class _Sandbox:
        def __init__(self, root: Path) -> None:
            self._root = root

        def root(self):
            return self._root

        def resolve_in_root(self, path: str) -> Path:
            return Path(path).resolve()

        def read_verdict(self, path: str) -> str:
            return "auto"

        def contains(self, path) -> bool:
            return True

        @property
        def mode(self) -> str:
            return "default"

    class _Approval:
        async def request(self, kind, payload):
            return type("R", (), {"decision": "approved"})()

    tools_ = []
    for i in range(6):
        t = FsReadTool()
        t.computer = _Sandbox(tmp_path)
        t.approvals = _Approval()
        tools_.append(t)

    results = await asyncio.gather(
        *(t.run(path=str(p)) for t, p in zip(tools_, files))
    )
    for i, res in enumerate(results):
        assert res.ok
        assert res.content == f"content-{i}", f"tool {i} got {res.content!r}"
