# -*- coding: utf-8 -*-
"""V 组反例证据（A05）：关闭结果不真实。

现场（基线 `da0436b`）：注册一个吞掉取消的后台协程，
`AppContext.aclose()` 的返回值是 `None`，却**照样**写了干净退出标记、照样释放
adapter；`create_app(..., close_db_on_shutdown=True)` 也照样关掉数据库。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from fastapi.testclient import TestClient  # noqa: E402

from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402


class FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _stuck(release: asyncio.Event) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass  # 吞掉取消
    await release.wait()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-a05-"))
    conn = connect(tmp / "j.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp / "data"), conn, close_db_on_shutdown=True)

    with TestClient(app) as client:
        ctx = client.app.state.ctx
        ctx.credentials._kr = MemoryKeyring()
        release = asyncio.Event()
        adapter = FakeAdapter()
        ctx._adapter_cache[("k", 1, "e", "m")] = adapter

        original = ctx.background.shutdown

        async def fast(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
            return await original(0.05, cancel_timeout=0.05)

        ctx.background.shutdown = fast  # type: ignore[method-assign]

        async def factory():  # noqa: ANN202
            return await _stuck(release)

        client.portal.call(lambda: ctx.background.register("stuck", factory))
        instance_id = ctx.instance_id
        result_holder: dict[str, object] = {}

        async def run_close():  # noqa: ANN202
            result_holder["report"] = await ctx.aclose()

        # 直接手动走一遍关闭（lifespan 也会走，但这里要拿到返回值）
        client.portal.call(run_close)
        report = result_holder["report"]
        print("[基线观察] aclose() 返回 =", type(report).__name__ if report is not None else None,
              repr(report))
        row = conn.execute(
            "SELECT exited_at FROM instances WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        print("[基线观察] instances.exited_at =", dict(row))
        print("[基线观察] adapter 是否被释放   =", adapter.closed)

    release.set()
    try:
        conn.execute("SELECT 1").fetchone()
        print("[基线观察] 关闭后数据库仍可用   = True")
    except sqlite3.ProgrammingError as exc:
        print("[基线观察] 关闭后数据库仍可用   = False（", exc, "）")
    print("[结论] 后台协程没结束，却写了干净退出标记、释放了 adapter、关了数据库。")
    conn.close()


if __name__ == "__main__":
    main()
