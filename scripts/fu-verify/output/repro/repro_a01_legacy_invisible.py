# -*- coding: utf-8 -*-
"""V 组反例证据（A01）：升级前「无归属」的历史 queued/running 用户行。

现场（基线 `da0436b`）：
  1. 启动恢复对「没有归属」的行只做保守保留（不改状态）；
  2. 于是它既不在 `interrupted_turns`（那个入口要求 status='interrupted'）里；
  3. 也不能被「重发」；
  4. 也没有第 3 条出口（`/api/recovery/records` 在基线 404）。

结论：用户消息既看不见、也无法继续 —— 永久卡死。

这个脚本在基线/修复后都能跑：只打印**可观察结果**，不做任何断言。
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

NOW = "2026-10-10T00:00:00+00:00"
QUEUED = "turn_legacy_queued"
RUNNING = "turn_legacy_running"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-a01-"))
    conn = connect(tmp / "j.db")
    apply_migrations(conn)

    for turn_id, status, message in (
        (QUEUED, "queued", "这条消息当时还在排队，进程退出后没有开始执行"),
        (RUNNING, "running", "这条消息执行到一半，进程退出后没有完成"),
    ):
        conn.execute(
            "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, "
            " created_at, updated_at, owner_instance_id) VALUES (?, ?, NULL, 0, ?, ?, ?, NULL)",
            (turn_id, message, status, NOW, NOW),
        )

    app = create_app(Settings(data_dir=tmp / "data"), conn)
    with TestClient(app) as client:
        state = client.get("/api/runtime/state").json()
        print("[基线观察] /api/runtime/state.interrupted_turns =",
              [t["turn_id"] for t in state["interrupted_turns"]])
        print("[基线观察] /api/runtime/state.orphaned_turns   =",
              [t["turn_id"] for t in state.get("orphaned_turns", [])])

        resend = client.post(f"/api/turns/{QUEUED}/resend")
        print("[基线观察] POST /api/turns/{legacy}/resend  ->", resend.status_code,
              str(resend.text)[:120])

        listing = client.get("/api/recovery/records")
        print("[基线观察] GET  /api/recovery/records      ->", listing.status_code,
              str(listing.text)[:120])

    rows = conn.execute(
        "SELECT turn_id, status, owner_instance_id, recovered_at, recovered_by "
        "FROM turn_journal ORDER BY created_at"
    ).fetchall()
    print("[台账真值] ", [dict(r) for r in rows])
    print("[结论] 两条历史行状态未变、不在任何用户可见入口里、也不能继续操作。")
    conn.close()


if __name__ == "__main__":
    main()
