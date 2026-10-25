#!/usr/bin/env python
"""E 独立验收 · 真实界面「结果与历史」的种子记录（隔离数据目录，不碰用户数据）。

为什么需要：验收「无待审批、有历史时仍能进入历史」必须真的有一条后端历史。
产品只提供 GET /api/approvals/history，没有公开的写入口；这里在**隔离的 QIO_DATA_DIR**
数据库里直接写一条 approval_history（等价于「用户昨天处理过一条审批」的既有事实），
之后所有断言仍走产品自己的 HTTP + 页面。

用法：
  $env:QIO_DATA_DIR="D:\\qio-dev\\.pm-e-cb63"
  python scripts/pm-acceptance/seed_history.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("QIO_DATA_DIR") or "")
SUMMARY = os.environ.get("QIO_SEED_HISTORY_SUMMARY") or "已执行：E-ACCEPT 种子历史（写入 2 个文件）"
APPROVAL_ID = os.environ.get("QIO_SEED_HISTORY_ID") or "e_seed_history_1"


def main() -> int:
    if not str(DATA_DIR) or not DATA_DIR.exists():
        print(f"QIO_DATA_DIR 无效：{DATA_DIR!r}（必须是隔离数据目录）", file=sys.stderr)
        return 2
    db = DATA_DIR / "app.db"
    if not db.exists():
        print(f"隔离数据库不存在：{db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO approval_history"
            " (approval_id, kind, summary, outcome, detail, at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                APPROVAL_ID,
                "tool_execution",
                SUMMARY,
                "executed",
                json.dumps(
                    {
                        "decision": "approved",
                        "label": "修改发布材料：第 1 章 摘要",
                        "seeded_by": "E acceptance precondition",
                    },
                    ensure_ascii=False,
                ),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM approval_history").fetchone()[0]
    finally:
        conn.close()
    print(f"seeded approval_history: {APPROVAL_ID} / 总条数 {count} / {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
