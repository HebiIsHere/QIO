# -*- coding: utf-8 -*-
"""复现 1（R04）：把 F 组用例里的注入点修正为『任何 INSERT ... turn_journal』后，
集成后的 POST /api/turns 到底返回什么。

只读仓库源码，不改任何文件；只在自己的临时目录建库。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from fastapi.testclient import TestClient  # noqa: E402

from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402


class Flaky(sqlite3.Connection):
    armed = False

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        text = " ".join(str(sql).split()).lower()
        if self.armed and text.startswith("insert") and "turn_journal" in text:
            self.armed = False
            raise sqlite3.OperationalError("disk I/O error (injected by rm-f repro)")
        return super().execute(sql, *args, **kwargs)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rmf-repro-r04-"))
    conn = sqlite3.connect(str(tmp / "t.db"), check_same_thread=False, factory=Flaky)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    apply_migrations(conn)

    app = create_app(Settings(data_dir=tmp / "data"), conn)
    with TestClient(app) as client:
        ctx = client.app.state.ctx
        ran: list[str] = []

        async def runner(turn_ctx):  # noqa: ANN001
            ran.append(turn_ctx.turn_id)

        ctx.turns.set_runner(runner)

        first = client.post("/api/turns", json={"message": "第一条消息"})
        print("第一条:", first.status_code, first.json())

        conn.armed = True
        second = client.post("/api/turns", json={"message": "第二条消息"})
        print("注入 journal 写失败后第二条:", second.status_code, second.json())
        print("运行器执行次数:", len(ran))
        print("journal 行数:", conn.execute("SELECT COUNT(*) c FROM turn_journal").fetchone()["c"])


if __name__ == "__main__":
    main()
