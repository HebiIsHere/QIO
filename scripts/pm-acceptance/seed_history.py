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
NOW = datetime.now(timezone.utc)

# 固定的一组历史记录：覆盖「单条成功 / 单条失败 / 并发两条各自归属」，
# 供矩阵 2（0 待办有历史）与矩阵 4（刷新/重连后历史仍在）使用。
# 全部写进隔离数据目录；不碰任何用户数据。
SEEDS = [
    (APPROVAL_ID, "tool_execution", SUMMARY, "executed", {"decision": "approved", "label": "修改发布材料：第 1 章 摘要"}),
    ("e_seed_history_2", "tool_execution", "执行失败：目标文件被占用（未做任何修改）", "failed", {"decision": "approved", "label": "修改发布材料：第 1 章 附录"}),
    ("e_seed_concurrent_a", "tool_execution", "E-ACCEPT 并发 A 已执行：写了 3 个文件", "executed", {"decision": "approved", "label": "并发审批 A"}),
    ("e_seed_concurrent_b", "tool_execution", "E-ACCEPT 并发 B 执行失败：磁盘只读", "failed", {"decision": "approved", "label": "并发审批 B"}),
]


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
        for index, (approval_id, kind, summary, outcome, detail) in enumerate(SEEDS):
            detail = dict(detail)
            detail.setdefault("seeded_by", "E acceptance precondition")
            conn.execute(
                "INSERT OR REPLACE INTO approval_history"
                " (approval_id, kind, summary, outcome, detail, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    kind,
                    summary,
                    outcome,
                    json.dumps(detail, ensure_ascii=False),
                    (NOW.replace(microsecond=0) if index == 0 else NOW).isoformat(),
                ),
            )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM approval_history").fetchone()[0]
    finally:
        conn.close()
    print(f"seeded approval_history: {len(SEEDS)} 条 / 表内总条数 {count} / {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())