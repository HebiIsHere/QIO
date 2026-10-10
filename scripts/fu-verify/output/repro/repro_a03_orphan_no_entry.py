# -*- coding: utf-8 -*-
"""V 组反例证据（A03）：孤立 claim 没有任何修复入口。

现场（基线 `da0436b`）：`recovered_at` 非空、`recovered_by` 空 →
`unfinished()` 不再提示它（要求 recovered_at IS NULL），界面上没有任何出口；
基线唯一的出口是只读的 `orphaned_turns`，`/api/recovery/records` 404。
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
ORPHAN = "turn_orphan_claim"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-a03-"))
    conn = connect(tmp / "j.db")
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at, "
        " updated_at, owner_instance_id, recovered_at, recovered_by) "
        "VALUES (?, '这条消息抢占过重发，但后继从来没有写出来', NULL, 0, 'interrupted', ?, ?, "
        " NULL, ?, NULL)",
        (ORPHAN, NOW, NOW, NOW),
    )

    app = create_app(Settings(data_dir=tmp / "data"), conn)
    with TestClient(app) as client:
        state = client.get("/api/runtime/state").json()
        print("[基线观察] interrupted_turns =",
              [t["turn_id"] for t in state["interrupted_turns"]], "（孤儿不在其中）")
        print("[基线观察] orphaned_turns    =",
              [t["turn_id"] for t in state.get("orphaned_turns", [])], "（只有只读出口）")
        print("[基线观察] GET  /api/recovery/records                 ->",
              client.get("/api/recovery/records").status_code)
        print("[基线观察] POST /api/recovery/records/{id}/repair      ->",
              client.post(f"/api/recovery/records/{ORPHAN}/repair",
                          json={"expected_class": "orphaned_claim"}).status_code)
        print("[基线观察] POST /api/recovery/records/{id}/continue    ->",
              client.post(f"/api/recovery/records/{ORPHAN}/continue",
                          json={"expected_class": "orphaned_claim",
                                "expected_status": "interrupted"}).status_code)

    row = dict(conn.execute(
        "SELECT status, recovered_at, recovered_by FROM turn_journal WHERE turn_id = ?",
        (ORPHAN,),
    ).fetchone())
    print("[台账真值]", row)
    print("[结论] 这条消息在界面上彻底消失，也没有任何 HTTP 入口能修复它。")
    conn.close()


if __name__ == "__main__":
    main()
