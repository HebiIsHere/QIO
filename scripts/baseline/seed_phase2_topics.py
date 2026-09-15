"""第二阶段视觉验收数据：写入一批话题（默认 100 个）。

用途：验证「总话题数 100，但同屏可见数量仍有明确上限」以及「持续旋转会不断
遇到新话题」。只写隔离数据目录（默认 %TEMP%\\qio-e2e），不碰正式数据。

用法：
    $env:QIO_DATA_DIR = "$env:TEMP\\qio-e2e"
    backend\\.venv\\Scripts\\python.exe scripts\\baseline\\seed_phase2_topics.py --topics 100
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("QIO_DATA_DIR") or (Path(os.environ.get("TEMP", ".")) / "qio-e2e"))
PREFIX = "阶段二-"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUBJECTS = [
    "顺丁橡胶降解", "QIO 前端", "课程报告", "互动模式", "记忆检索打分",
    "锚点生命周期", "沙箱边界", "流式渲染", "知识纠错", "实体抽取",
    "评测基线", "凭据轮换", "桌面壳打包", "搜索降级", "预算规划",
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "src"))
    from agent.storage.migrate import apply_migrations

    conn = sqlite3.connect(DATA_DIR / "app.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    apply_migrations(conn)
    return conn


def clear(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name LIKE ?", (PREFIX + "%",)
    ).fetchall()
    for row in rows:
        tid = row["id"]
        for frag in conn.execute("SELECT id FROM fragments WHERE topic_id=?", (tid,)).fetchall():
            conn.execute("DELETE FROM messages WHERE fragment_id=?", (frag["id"],))
            conn.execute("DELETE FROM memory_index WHERE fragment_id=?", (frag["id"],))
        conn.execute("DELETE FROM cursor WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM fragments WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (tid, tid))
        conn.execute("DELETE FROM nodes WHERE id=?", (tid,))
    conn.commit()


def seed(conn: sqlite3.Connection, count: int) -> None:
    now = datetime.now(timezone.utc)
    for i in range(count):
        subject = SUBJECTS[i % len(SUBJECTS)]
        title = f"{PREFIX}{subject} {i + 1:03d}"
        topic_id = new_id("topic")
        created = (now - timedelta(hours=count - i)).isoformat()
        conn.execute(
            "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'topic', ?, '{}', ?, ?)",
            (topic_id, title, created, created),
        )
        for fi in range(1 + (i % 3)):
            frag_id = new_id("frag")
            start = (now - timedelta(hours=count - i, minutes=30 - fi)).isoformat()
            conn.execute(
                "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (frag_id, topic_id, f"{title} 的第 {fi + 1} 段讨论摘要", start, start),
            )
            for mi, role in enumerate(("user", "assistant")):
                conn.execute(
                    "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
                    "VALUES (?, ?, ?, ?, 'text', ?)",
                    (new_id("msg"), frag_id, role, f"{title} 的第 {fi + 1} 段第 {mi + 1} 条内容", start),
                )
        conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, "
            "token_estimate, created_at) "
            "SELECT ?, id, ?, '[]', '[\"?\"]', ?, 10, created_at FROM fragments "
            "WHERE topic_id = ? ORDER BY created_at DESC LIMIT 1",
            (new_id("idx"), topic_id, title, topic_id),
        )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topics", type=int, default=100)
    parser.add_argument("--reset", action="store_true", help="先删掉上一次写入的验收话题")
    args = parser.parse_args()

    conn = connect()
    if args.reset:
        clear(conn)
    seed(conn, max(1, args.topics))
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type='topic'"
    ).fetchone()["c"]
    print(f"已写入 {args.topics} 个验收话题；数据库中话题总数 = {total}")
    print(f"数据目录：{DATA_DIR}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
