# -*- coding: utf-8 -*-
"""V 组反例证据（A02）：旧 schema 人工值被自动候选覆盖、且不登记候选。

现场（基线 `da0436b`）：卡上的 summary / kind / attributes 都有人工值，但
`field_meta` 是空的（升级上来的旧行）。保护判断只看 `source == "user"`，
`None != "user"` → 直接覆盖，一个候选都不登记。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from agent.entities.cards import (  # noqa: E402
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

NOW = "2026-10-10T00:00:00+00:00"
CARD = "card_legacy"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-a02-"))
    conn = connect(tmp / "e.db")
    apply_migrations(conn)

    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        " state, created_at, updated_at, revision, field_meta) "
        "VALUES (?, NULL, '我家的鹅', ?, '动物', '人工写的摘要：这只鹅会咬人', ?, "
        "'active', ?, ?, 0, '{}')",
        (
            CARD,
            json.dumps(["大白"], ensure_ascii=False),
            json.dumps([{"key": "年龄", "value": "2 岁", "confidence": 0.9}], ensure_ascii=False),
            NOW,
            NOW,
        ),
    )

    service = EntityCardService(conn)
    before = service.get(CARD)
    print("[升级后] field_meta      =", before.field_meta)
    print("[升级后] summary         =", before.summary)
    print("[升级后] kind            =", before.kind)
    print("[升级后] attributes      =", {a["key"]: a["value"] for a in before.attributes})
    print("[升级后] aliases         =", before.aliases)

    service.apply_auto_candidate(
        EntityCardCandidate(
            name="我家的鹅",
            aliases=["新别名"],
            kind="家禽",
            summary="自动摘要：鹅会看门",
            attributes=[
                EntityAttribute(key="年龄", value="3 岁"),
                EntityAttribute(key="健康状况", value="良好"),
            ],
        )
    )

    after = service.get(CARD)
    print("--- 提交自动候选之后 ---")
    print("[结果] summary           =", after.summary)
    print("[结果] kind              =", after.kind)
    print("[结果] attributes        =", {a["key"]: a["value"] for a in after.attributes})
    print("[结果] aliases           =", after.aliases)
    print("[结果] pending 候选条数  =", len(service.pending_candidates(CARD)),
          service.pending_candidates(CARD))
    print("[结论] 三个字段的人工值被自动候选覆盖；冲突一条候选都没登记。")
    conn.close()


if __name__ == "__main__":
    main()
