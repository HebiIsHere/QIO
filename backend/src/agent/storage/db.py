"""SQLite connection management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL, foreign keys, and busy timeout.

    check_same_thread=False: FastAPI serves requests on a thread pool while
    the agent loop may also touch storage; SQLite serializes access through
    WAL + busy_timeout.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # autocommit: every write persists immediately (no implicit transactions)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def close(conn: sqlite3.Connection) -> None:
    conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """把逻辑上属于同一个动作的多次写入包成一个原子操作。

    连接是 autocommit（`isolation_level = None`）——单条语句天然立即落盘，
    但「关闭 fragment + 写 memory index」「建节点 + 建边」这类多步业务动作
    必须要么全成功、要么全失败，否则中途失败会留下半截状态。

    用法：

        with transaction(conn):
            conn.execute(...)
            conn.execute(...)

    已在外层事务中时不再嵌套 BEGIN（SQLite 不支持嵌套事务），
    直接复用外层事务，由最外层决定提交/回滚。
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
