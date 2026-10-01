"""Turn 队列的持久化台账：被 API 接受过的消息不能在重启后静默消失。

产品语义（**不自动重放**）：

* `queued`    —— API 已经明确接受，但还没开始执行；
* `running`   —— 正在执行；
* `completed` / `cancelled` / `failed` / `unavailable` —— 终态；
* `interrupted` —— 进程结束（含崩溃）时还没走到终态的 turn。

重启后的规则（明确、可测）：

1. 台账里还写着 `queued` / `running` 的行一律标成 `interrupted`
   （原因分别记 `queued_at_restart` / `running_at_restart`）；
2. **不自动执行任何一条**：进程退出可能正是用户的意思，自动重放会重复花
   模型钱、重复产生回答，甚至在用户没看着的时候改动数据；
3. 消息原文留在台账里，通过 `/api/runtime/state` 的 `interrupted_turns`
   如实告诉用户「这条没有被执行」，由用户明确决定「重发」或「知道了」；
4. 只有 `interrupted` 的行能被重发；`completed` 等终态永远不可重发 ——
   这就是「不得重复执行已经完成的 turn」的落地方式；
5. 重发是**一次性**的：`claim()` 用一条带条件的 UPDATE 抢占，抢占成功才提交
   新 turn，所以同一条记录不可能被重发两次。

为什么队列里的消息会消失（缺陷）：排队中的 turn 只活在进程内存的
`asyncio.Queue` 里，用户消息甚至还没写进 `messages` 表 —— 进程一退，
它就没有任何痕迹。台账是这条消息唯一的落点。

保留策略：终态行保留 `DEFAULT_RETENTION_DAYS` 天（只为「不重复执行」作证），
到期清理；`interrupted` 行在用户处理（重发 / 知道了）之前**不清理**。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# 台账状态（至少覆盖 queued / running / completed / cancelled / interrupted）
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
UNAVAILABLE = "unavailable"
INTERRUPTED = "interrupted"

TERMINAL_STATUSES = (COMPLETED, CANCELLED, FAILED, UNAVAILABLE)
# 进程结束时会「没走到终态」的状态：重启后一律变成 interrupted
OPEN_STATUSES = (QUEUED, RUNNING)

DEFAULT_RETENTION_DAYS = 7
# 用户看到的文案（不含任何内部标识之外的东西）
REASON_TEXT = {
    "queued_at_restart": "这条消息当时还在排队，进程退出后没有开始执行",
    "running_at_restart": "这条消息执行到一半，进程退出后没有完成",
    "shutdown": "应用关闭时这条消息还没有执行",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TurnJournal:
    """`turn_journal` 表的读写。所有写入失败都只记日志 —— 台账不能挡住对话。"""

    def __init__(self, conn: sqlite3.Connection, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.conn = conn
        self.retention_days = int(retention_days)

    # -- 写入：生命周期 ---------------------------------------------------

    def accepted(
        self,
        *,
        turn_id: str,
        message: str,
        topic_id: str | None = None,
        notify: bool = False,
        status: str = QUEUED,
    ) -> None:
        """把一个刚被接受的 turn 落一行（排队中的消息从此有了痕迹）。"""
        self._execute(
            "INSERT OR IGNORE INTO turn_journal "
            "(turn_id, message, topic_id, notify, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(turn_id),
                str(message or ""),
                topic_id,
                1 if notify else 0,
                status if status in OPEN_STATUSES else QUEUED,
                _now(),
                _now(),
            ),
        )

    def running(self, turn_id: str) -> None:
        self._execute(
            "UPDATE turn_journal SET status = ?, started_at = COALESCE(started_at, ?), "
            "updated_at = ? WHERE turn_id = ? AND status = ?",
            (RUNNING, _now(), _now(), str(turn_id), QUEUED),
        )

    def terminal(self, turn_id: str, status: str, *, reason: str | None = None) -> None:
        """记终态。

        `cancelled` + `reason='shutdown'` 记成 `interrupted`：那是进程把这一轮
        掐断的，不是用户取消的 —— 重启后应该给用户一个「重发」的机会。
        """
        if status not in TERMINAL_STATUSES:
            status = FAILED
        journal_status = INTERRUPTED if (status == CANCELLED and reason == "shutdown") else status
        self._execute(
            "UPDATE turn_journal SET status = ?, ended_at = ?, updated_at = ?, "
            "reason = COALESCE(?, reason) WHERE turn_id = ? AND status IN (?, ?)",
            (journal_status, _now(), _now(), reason, str(turn_id), QUEUED, RUNNING),
        )

    def note_user_message(self, turn_id: str, message_id: str | None) -> None:
        """记下这一轮已经写进历史的用户消息 id（重启后能如实告诉用户）。"""
        if not message_id:
            return
        self._execute(
            "UPDATE turn_journal SET user_message_id = ?, updated_at = ? WHERE turn_id = ?",
            (str(message_id), _now(), str(turn_id)),
        )

    # -- 重启恢复 ---------------------------------------------------------

    def interrupt_stale(self) -> list[dict[str, Any]]:
        """启动时把上一个进程留下的 queued / running 标成 interrupted。

        只改状态、不动消息原文；返回被动过的行（供启动日志与界面提示）。
        """
        rows = self._query(
            "SELECT * FROM turn_journal WHERE status IN (?, ?) ORDER BY created_at ASC",
            (QUEUED, RUNNING),
        )
        recovered: list[dict[str, Any]] = []
        for row in rows:
            reason = (
                "running_at_restart" if row["status"] == RUNNING else "queued_at_restart"
            )
            self._execute(
                "UPDATE turn_journal SET status = ?, reason = ?, updated_at = ? "
                "WHERE turn_id = ? AND status IN (?, ?)",
                (INTERRUPTED, reason, _now(), row["turn_id"], QUEUED, RUNNING),
            )
            recovered.append(
                self._view({**dict(row), "status": INTERRUPTED, "reason": reason})
            )
        return recovered

    def unfinished(self) -> list[dict[str, Any]]:
        """还没被用户处理的 interrupted 行（系统通知轮不算：它不是用户的消息）。"""
        rows = self._query(
            "SELECT * FROM turn_journal WHERE status = ? AND recovered_at IS NULL "
            "AND notify = 0 ORDER BY created_at ASC",
            (INTERRUPTED,),
        )
        return [self._view(dict(r)) for r in rows]

    def recoverable(self, turn_id: str) -> dict[str, Any] | None:
        """这条记录还能不能重发：只有 interrupted 且没被处理过才算。"""
        row = self._query_one(
            "SELECT * FROM turn_journal WHERE turn_id = ? AND status = ? "
            "AND recovered_at IS NULL",
            (str(turn_id), INTERRUPTED),
        )
        return dict(row) if row is not None else None

    def claim(self, turn_id: str) -> bool:
        """抢占一条记录的重发权（原子、一次性）：抢到了才允许提交新 turn。"""
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET recovered_at = ?, updated_at = ? "
                "WHERE turn_id = ? AND status = ? AND recovered_at IS NULL",
                (_now(), _now(), str(turn_id), INTERRUPTED),
            )
        except sqlite3.Error as exc:  # noqa: BLE001 - 台账失败不得影响接口可用性
            logger.warning("turn journal claim failed: %s", exc)
            return False
        return int(cur.rowcount or 0) == 1

    def release_claim(self, turn_id: str) -> None:
        """提交失败时把抢占退回去（否则用户就再也重发不了这条消息了）。"""
        self._execute(
            "UPDATE turn_journal SET recovered_at = NULL, updated_at = ? WHERE turn_id = ?",
            (_now(), str(turn_id)),
        )

    def mark_recovered(self, turn_id: str, *, new_turn_id: str | None = None) -> bool:
        self._execute(
            "UPDATE turn_journal SET recovered_at = ?, recovered_by = ?, updated_at = ? "
            "WHERE turn_id = ? AND status = ?",
            (_now(), new_turn_id, _now(), str(turn_id), INTERRUPTED),
        )
        return True

    def dismiss(self, turn_id: str) -> bool:
        """用户选择「知道了」：不再提示，但仍然保留记录（不删用户消息）。"""
        if self.recoverable(turn_id) is None:
            return False
        self._execute(
            "UPDATE turn_journal SET recovered_at = ?, updated_at = ? WHERE turn_id = ?",
            (_now(), _now(), str(turn_id)),
        )
        return True

    # -- 清理 -------------------------------------------------------------

    def prune_terminal(self, days: int | None = None) -> int:
        """清掉过期终态行（只为「不重复执行已完成 turn」作证，不需要永久保留）。

        `interrupted` 行不会被清：它承载用户还没看到的消息原文。
        """
        retention = self.retention_days if days is None else int(days)
        if retention <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat()
        try:
            cur = self.conn.execute(
                "DELETE FROM turn_journal WHERE status IN (?, ?, ?, ?) AND ended_at IS NOT NULL "
                "AND ended_at < ?",
                (*TERMINAL_STATUSES, cutoff),
            )
        except sqlite3.Error as exc:  # noqa: BLE001 - 清理是维护动作
            logger.warning("turn journal prune failed: %s", exc)
            return 0
        return int(cur.rowcount or 0)

    # -- internals --------------------------------------------------------

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        """给界面的形状：消息原文照给（它就是用户自己发的那句话）。"""
        row.pop("notify", None)
        row["reason_text"] = REASON_TEXT.get(str(row.get("reason") or ""), "")
        return row

    def _execute(self, sql: str, params: tuple) -> None:
        try:
            self.conn.execute(sql, params)
        except sqlite3.Error as exc:  # noqa: BLE001 - 台账失败不得影响对话
            logger.warning("turn journal write failed: %s", exc)

    def _query(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        try:
            return list(self.conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:  # noqa: BLE001 - 读不到就当没有
            logger.warning("turn journal read failed: %s", exc)
            return []

    def _query_one(self, sql: str, params: tuple) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None
