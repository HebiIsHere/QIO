# -*- coding: utf-8 -*-
"""复现 3（R06）：抢占之后、mark_recovered 之前「进程退出」的遗留记录，
在集成后的可见性与可修复性（只读仓库源码）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from fastapi.testclient import TestClient  # noqa: E402

from agent.api.server import create_app  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.storage.turn_journal import TurnJournal  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rmf-repro-r06-"))
    conn = connect(tmp / "j.db")
    apply_migrations(conn)

    journal = TurnJournal(conn)
    journal.accepted(turn_id="turn_crash", message="抢占之后崩溃的消息")
    journal.interrupt_stale()
    print("崩溃窗口：claim 成功 =", journal.claim("turn_crash"))
    print("崩溃窗口：unfinished =", [r["turn_id"] for r in journal.unfinished()])
    print("崩溃窗口：orphaned_claims =", [r["turn_id"] for r in journal.orphaned_claims()])

    app = create_app(Settings(data_dir=tmp / "data"), conn)
    with TestClient(app) as client:
        ctx = client.app.state.ctx
        ran: list[str] = []

        async def runner(turn_ctx):  # noqa: ANN001
            ran.append(turn_ctx.turn_id)

        ctx.turns.set_runner(runner)

        state = client.get("/api/runtime/state").json()
        print("runtime/state 顶层字段:", sorted(state.keys()))
        print("interrupted_turns:", [t["turn_id"] for t in state["interrupted_turns"]])
        print(
            "orphaned_turns:",
            [(t["turn_id"], t.get("reason_text")) for t in state.get("orphaned_turns", [])],
        )

        direct = client.post("/api/turns/turn_crash/resend")
        print("未修复时直接 resend:", direct.status_code)

        print("repair_orphan:", journal.repair_orphan("turn_crash"))
        repaired = client.post("/api/turns/turn_crash/resend")
        print("修复后 resend:", repaired.status_code, repaired.json())
        row = dict(
            conn.execute(
                "SELECT recovered_at, recovered_by, status FROM turn_journal WHERE turn_id = ?",
                ("turn_crash",),
            ).fetchone()
        )
        print("修复后关联:", row)
        again = client.post("/api/turns/turn_crash/resend")
        print("再次 resend（必须 409）:", again.status_code)


if __name__ == "__main__":
    main()
