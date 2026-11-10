"""单调导航版本（P1-04 契约 §5）：对话位置的唯一版本源 + 原子 CAS。

为什么需要它
------------
话题申请是「用户看到某个位置（话题 / 片段）时做出的决定」。用户等待期间可能
手动切走、切回、换片段，或者本轮工具自己落实了别的导航。只比较 topic_id
不够（同一话题换片段、离开再返回都要让旧申请失效），所以引入**单调递增**的
版本号：

* 每一次**真实位置变化**推进一次版本（用户手动进入 / 切回话题、选历史片段、
  改接续位置、工具真实导航成功）；
* 渲染、重复事件、草稿编辑、仅收起 / 打开弹窗、无关后台状态**不**推进；
* 申请建立时快照 `base_nav_version`，确认时与实际写入时都用它做 CAS：
  版本对不上 → 不发生任何写入（`NavigationVersionConflict`）。

实现边界
--------
* 版本存在 SQLite 单行表 `navigation_version`（迁移 77 追加；禁止改历史迁移）；
* `current()` 只读、缺表/缺行按 0（未迁移的旧库不假装有版本）；
* `advance()` / `compare_and_advance()` 在事务内做原子读改写：调用方把它们和
  Anchor / 片段写入放进**同一个** `transaction(conn)`，CAS 失败时抛异常让整个
  事务回滚 —— 这是「导航写入与版本校验无竞态窗口」的唯一做法；
* 本模块不做任何自然语言判断，也不决定「哪次用户动作算真实导航」——那由
  `services/navigation.py` 的写入口在调用点声明 `source`。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from agent.storage.db import transaction

#: 表名 / 单行主键（一个数据库只有一条全局导航版本）。
TABLE = "navigation_version"
ROW_ID = 1

_COLUMNS = ("version", "source", "topic_id", "fragment_id", "turn_id", "updated_at")


class NavigationVersionConflict(RuntimeError):
    """导航位置已经改变：绑定的 `base_nav_version` 与当前版本不一致。

    这是**申请失效**的机器事实，不是用户拒绝，也不是代码异常。调用方据此：
    * 工具侧 → 返回真实失败（`nav_version_conflict`），绝不执行导航写入；
    * API 侧 → 409 `topic_application_stale`（带 expected / actual 字段）。
    """

    code = "nav_version_conflict"
    status_code = 409

    def __init__(self, expected: int, actual: int, *, source: str = "") -> None:
        self.expected = int(expected)
        self.actual = int(actual)
        self.source = str(source or "")
        super().__init__(
            f"导航位置已改变（申请基于版本 {self.expected}，当前版本 {self.actual}），"
            "这次操作没有执行"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "expected": self.expected,
            "actual": self.actual,
            "source": self.source,
            "message": str(self),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def _row(conn: sqlite3.Connection) -> Any:
    return conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} WHERE id = ?", (ROW_ID,)
    ).fetchone()


def _read_version(conn: sqlite3.Connection) -> int:
    row = _row(conn)
    if row is None:
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):  # pragma: no cover - 损坏行按 0（不造假版本）
        return 0


def _ensure_row(conn: sqlite3.Connection) -> None:
    """保证单行存在（迁移会种入 0；这里是给被单独建表的测试库兜底）。"""
    conn.execute(
        f"INSERT OR IGNORE INTO {TABLE} (id, version, source, updated_at) "
        "VALUES (?, 0, '', '')",
        (ROW_ID,),
    )


def current(conn: sqlite3.Connection) -> int:
    """当前导航版本。只读；表不存在（未迁移的旧库 / 单元测试）按 0。"""
    if not _table_exists(conn):
        return 0
    return _read_version(conn)


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """当前版本的完整事实快照（诊断 / 事件 / 恢复记录用）。只读。"""
    data: dict[str, Any] = {
        "version": 0,
        "source": "",
        "topic_id": None,
        "fragment_id": None,
        "turn_id": None,
        "updated_at": "",
    }
    if not _table_exists(conn):
        return data
    row = _row(conn)
    if row is None:
        return data
    for index, name in enumerate(_COLUMNS):
        data[name] = row[index]
    data["version"] = int(data["version"] or 0)
    return data


def advance(
    conn: sqlite3.Connection,
    *,
    source: str,
    topic_id: str | None = None,
    fragment_id: str | None = None,
    turn_id: str | None = None,
) -> int:
    """无绑定推进：一次真实导航写入成功后调用。返回新版本。

    **原子读改写**：调用方应把它和 Anchor / 片段写入放进同一个
    `transaction(conn)`；本函数内部也包了 `transaction()`（外层已有事务时复用，
    由最外层决定提交/回滚），所以单独调用也安全。
    """
    with transaction(conn):
        _ensure_row(conn)
        conn.execute(
            f"UPDATE {TABLE} SET version = version + 1, source = ?, topic_id = ?, "
            "fragment_id = ?, turn_id = ?, updated_at = ? WHERE id = ?",
            (
                str(source or ""),
                topic_id,
                fragment_id,
                turn_id,
                _now(),
                ROW_ID,
            ),
        )
        return _read_version(conn)


def compare_and_advance(
    conn: sqlite3.Connection,
    expected: int,
    *,
    source: str = "",
    topic_id: str | None = None,
    fragment_id: str | None = None,
    turn_id: str | None = None,
) -> bool:
    """CAS：只有当前版本 == `expected` 才推进一次并返回 True。

    条件不满足 → 不写任何数据、返回 False（调用方据此抛
    `NavigationVersionConflict` 或返回真实失败）。同一 expected 只能成功一次：
    重复确认 / 网络重放拿到 False。
    """
    expected = int(expected)
    with transaction(conn):
        _ensure_row(conn)
        cursor = conn.execute(
            f"UPDATE {TABLE} SET version = version + 1, source = ?, topic_id = ?, "
            "fragment_id = ?, turn_id = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (
                str(source or ""),
                topic_id,
                fragment_id,
                turn_id,
                _now(),
                ROW_ID,
                expected,
            ),
        )
        return cursor.rowcount == 1


def cas_or_advance(
    conn: sqlite3.Connection,
    expected: int | None,
    *,
    source: str,
    topic_id: str | None = None,
    fragment_id: str | None = None,
    turn_id: str | None = None,
) -> int:
    """导航写入口的统一版本动作。

    * `expected is None`：这次是**用户自己的新选择**（或没有申请绑定）→ 无条件推进；
    * 否则：条件推进；版本对不上 → 抛 :class:`NavigationVersionConflict`
      （调用方在事务里让 Anchor 写入一起回滚）。

    返回**本次写入后的真实版本**；冲突时数据零变化。
    """
    if expected is None:
        return advance(
            conn,
            source=source,
            topic_id=topic_id,
            fragment_id=fragment_id,
            turn_id=turn_id,
        )
    expected = int(expected)
    actual = 0
    with transaction(conn):
        _ensure_row(conn)
        cursor = conn.execute(
            f"UPDATE {TABLE} SET version = version + 1, source = ?, topic_id = ?, "
            "fragment_id = ?, turn_id = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (
                str(source or ""),
                topic_id,
                fragment_id,
                turn_id,
                _now(),
                ROW_ID,
                expected,
            ),
        )
        if cursor.rowcount == 1:
            return _read_version(conn)
        actual = _read_version(conn)
    raise NavigationVersionConflict(expected, actual, source=source)


def matches(conn: sqlite3.Connection, expected: int | None) -> bool:
    """只读判断：当前版本是否仍等于申请快照（None = 没有绑定，视为匹配）。"""
    if expected is None:
        return True
    return current(conn) == int(expected)


def require_current(
    conn: sqlite3.Connection, expected: int | None, *, source: str = ""
) -> int:
    """只读复核：版本已变 → 抛 `NavigationVersionConflict`；否则返回当前版本。

    恢复入口（异步恢复 / 刷新恢复）用它复核「用户后来的选择没有被覆盖」，
    本身不推进版本、不写任何数据。
    """
    actual = current(conn)
    if expected is not None and actual != int(expected):
        raise NavigationVersionConflict(int(expected), actual, source=source)
    return actual


__all__ = [
    "TABLE",
    "NavigationVersionConflict",
    "current",
    "snapshot",
    "advance",
    "compare_and_advance",
    "cas_or_advance",
    "matches",
    "require_current",
]
