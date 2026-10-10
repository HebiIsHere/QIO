# -*- coding: utf-8 -*-
"""V 组反例证据（A04）：候选没有任何用户管理入口。

现场（基线 `da0436b`）：后端把冲突候选登记进 `field_meta.pending`，
但冻结的候选端点全部不存在（404）→ 界面上没有任何入口能采纳或丢弃它们。
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
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.entities.cards import (  # noqa: E402
    SOURCE_USER,
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-a04-"))
    conn = connect(tmp / "e.db")
    apply_migrations(conn)

    service = EntityCardService(conn)
    service.upsert(
        EntityCardCandidate(
            name="我家的鹅",
            aliases=["大白"],
            kind="动物",
            summary="用户写下的摘要",
            attributes=[EntityAttribute(key="年龄", value="2 岁")],
        ),
        source=SOURCE_USER,
    )
    card = service.find_by_name("我家的鹅")
    assert card is not None
    service.apply_auto_candidate(
        EntityCardCandidate(
            name="我家的鹅",
            aliases=["新别名"],
            kind="家禽",
            summary="自动提炼的摘要",
            attributes=[EntityAttribute(key="年龄", value="3 岁")],
        )
    )
    pending = service.pending_candidates(card.id)
    print("[基线观察] 后端已登记的候选条数 =", len(pending))
    for item in pending:
        print("           -", item.get("field"), "->", item.get("value"), "(", item.get("reason"), ")")

    app = create_app(Settings(data_dir=tmp / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        cross = client.get("/api/entities/candidates?include_archived=true")
        print("[基线观察] GET  /api/entities/candidates                     ->", cross.status_code)
        one = client.get(f"/api/entities/{card.id}/candidates")
        print("[基线观察] GET  /api/entities/{id}/candidates                ->", one.status_code)
        adopt = client.post(f"/api/entities/{card.id}/candidates/pc_x/adopt",
                            json={"expected_revision": 1})
        print("[基线观察] POST /api/entities/{id}/candidates/{cid}/adopt   ->", adopt.status_code)
        dismiss = client.post(f"/api/entities/{card.id}/candidates/pc_x/dismiss",
                              json={"expected_revision": 1})
        print("[基线观察] POST /api/entities/{id}/candidates/{cid}/dismiss ->", dismiss.status_code)

    print("[结论] 候选在后端登记着，但用户没有任何可观察的入口去采纳或丢弃。")
    conn.close()


if __name__ == "__main__":
    main()
