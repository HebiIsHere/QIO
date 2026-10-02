"""Lightweight ordered migrations with a schema_version table.

**每条迁移是一个真实事务**：它的全部语句 + 写 schema_version 要么一起生效、要么一起不生效。
这一点在本项目里极易被改坏，所以写清楚：

* `storage/db.py` 把连接设成 autocommit（`isolation_level = None`）；
* autocommit 下 `with conn:` **不会隐式开事务** —— 上下文管理器没有东西可提交/回滚，
  每条语句各自落盘。旧写法正是这样：一次中途失败（或两步之间进程死掉）会留下**半截 DDL**，
  而 schema_version 还停在旧版本。migration 24 是单条 `ALTER TABLE ... ADD COLUMN phases`，
  ALTER 生效、版本没写 → 下次启动重放它 → "duplicate column name: phases" → 应用再也起不来。

所以要显式 BEGIN/COMMIT（`agent.storage.db.transaction()` 就是它）。

另外对**存量库**的半截迁移做窄口径自愈：只有「对象已经存在」（duplicate column / already
exists）才当作「这条已经应用过」，其它任何错误照旧抛 —— 不能拿自愈掩盖真正的迁移错误。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone

from agent.storage.db import transaction
from agent.storage.schema import MIGRATIONS

logger = logging.getLogger(__name__)

# 只有「对象已经存在」这一类信号才当作「已经应用过」：
# 半截迁移的存量库靠它自愈；其它错误（no such table / 约束失败 / 语法错）一律照旧抛。
_ALREADY_APPLIED_PATTERNS = (
    re.compile(r"duplicate column name", re.IGNORECASE),
    re.compile(r"already exists", re.IGNORECASE),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _already_applied(exc: sqlite3.Error) -> bool:
    """这条语句要建的对象是不是已经存在（= 这条已经应用过）。"""
    return any(pattern.search(str(exc)) for pattern in _ALREADY_APPLIED_PATTERNS)


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return int(row["v"])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the new schema version.

    每条迁移一个事务：语句 + 版本行原子落盘（见模块 docstring 里为什么不能只写 `with conn:`）。
    遇到「对象已存在」的语句时跳过并记一条 warning（存量库的半截迁移自愈），其它错误直接抛。
    """
    version = current_version(conn)
    for target, statements in MIGRATIONS:
        if target <= version:
            continue
        with transaction(conn):
            for stmt in statements:
                try:
                    conn.execute(stmt)
                except sqlite3.Error as exc:
                    if not _already_applied(exc):
                        raise
                    # 上一次运行（旧版本代码）已经把这条语句生效过、只是版本号没来得及写：
                    # 跳过它，剩下的语句与版本行在同一个事务里补齐 —— 库因此能自愈。
                    logger.warning("迁移 %s：语句已应用过，跳过（%s）", target, exc)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (target, _now()),
            )
        version = target
    return version