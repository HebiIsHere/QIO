"""子任务完成通知：持久化队列、可靠投递、去重、原话题续作事实（阶段四 A；契约 §14.3）。

为什么单独一层：TaskManager 的 notify 回调发生在任务收口的那一瞬间，而主循环
可能正在响应用户（不能打断当前交流）、也可能正在跑另一条通知轮。直接在回调里
「push 进当前轮或立刻 submit 一个系统轮」会有两个已知坏结果：

* 处理另一条通知时到达的通知被丢弃（真实缺陷：结果无声消失）；
* 迟到结果把已经被取消的任务重新复活。

这里把「任务收口」和「真正通知主 agent」拆开：收口时只**入队**（按 task_id 去重、
终态过滤、取消不通知），由接线方在合适的时机（没有 active 用户轮 / 轮末）取一条
（FIFO）交给系统通知轮；失败保留待重试，不丢。通知事实携带程序绑定的原任务
（origin: turn_id / topic_id / goal_version），供轮末在原话题续作、不切界面。

2026-10-26（后续修复轮 FU2-D）：基线是**纯内存 deque + 200 条上限**，有三个真实缺陷：

1. 超过 MAX_PENDING_NOTIFICATIONS 时 popleft **静默丢弃**最早通知 —— 结果无声消失；
2. enqueue 里调用 TaskManager.mark_notified —— **把「入队」当成「已通知/已交付」**；
3. 没有持久化：重启后队列清空；take 之后进程崩溃就丢这条（已标 notified，无法补投）。

现在的口径（迁移 71 起）：
* 传入 conn 时走**持久化**队列（表 subtask_notifications，task_id 主键 = 幂等去重）；
* **交付的唯一判据是 delivered_at**：只有通知轮被 TurnManager 接受（turns.submit 成功）
  才写它；失败写 attempts/last_error/last_attempt_at 并保留待重试；
* 队列超过 MAX_PENDING_NOTIFICATIONS 只**告警**，绝不丢弃；
* 重启时未送达的记录仍在库里，由接线方继续投递；已交付的永不重投；
* 诚实边界：turn 提交（TurnManager）与这里落库没有共享事务，所以「submit 成功但写
  delivered_at 之前进程崩溃」这一极窄窗口下同一条通知可能被再投一次（at-least-once）。
  除此之外同一 task_id 最多投递一次。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from agent import prompts
from agent.tools.task_manager import (
    NOTIFY_OUTCOMES,
    OUTCOME_BUDGET_EXHAUSTED,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_PARTIALLY_COMPLETED,
    TaskManager,
)

logger = logging.getLogger(__name__)

STATUS_LABELS: dict[str, str] = {
    OUTCOME_COMPLETED: "完成",
    OUTCOME_PARTIALLY_COMPLETED: "部分完成",
    OUTCOME_FAILED: "失败",
    OUTCOME_BUDGET_EXHAUSTED: "预算耗尽",
    OUTCOME_CANCELLED: "已取消",
}

#: 单次通知里给模型的预览上限（全文用 read_task_result 读取，不塞进通知正文）。
NOTICE_PREVIEW_CHARS = 500
#: 待投递通知的**告警阈值**（不是容量上限）：超过只告警，绝不丢弃（基线会丢最早）。
MAX_PENDING_NOTIFICATIONS = 200

#: 持久化表与必需列（与迁移 71 一一对应；缺对象由 migrate.REQUIRED_OBJECTS 兜底）。
NOTIFICATION_TABLE = "subtask_notifications"
NOTIFICATION_COLUMNS: tuple[str, ...] = (
    "task_id",
    "payload",
    "origin",
    "created_at",
    "delivered_at",
    "attempts",
    "last_error",
    "last_attempt_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def completion_system_hint() -> str:
    """通知轮要用的固定指令（唯一来源 prompts.py，不在业务模块复制正文）。"""
    return (str(prompts.PROMPTS.get("SUBTASK_COMPLETION_SYSTEM") or "").strip()).strip() + chr(10) + chr(10) + __import__("agent.prompts", fromlist=["PROMPTS"]).PROMPTS["SUBTASK_RETURN_POLICY"]


def build_notification(task_id: str, record) -> dict[str, Any]:
    """把 TaskRecord 收口成一条通知事实（不含完整正文，只给可读取引用）。"""
    outcome = str(getattr(record, "outcome", "") or "").strip()
    preview = str(getattr(record, "full_content", None) or "")
    if not preview and getattr(record, "result", None) is not None:
        preview = str(record.result.content or "")
    return {
        "task_id": task_id,
        "tool": str(getattr(record, "tool", "") or ""),
        "status": str(getattr(record, "status", "") or ""),
        "outcome": outcome,
        "status_label": STATUS_LABELS.get(outcome, outcome or "未知"),
        "ok": bool(record.result.ok) if getattr(record, "result", None) is not None else None,
        "error": (
            str(record.result.error or "")
            if getattr(record, "result", None) is not None and not record.result.ok
            else None
        ),
        "preview": preview[:NOTICE_PREVIEW_CHARS],
        "total_chars": int(getattr(record, "total_chars", 0) or 0),
        "result_ref": {
            "tool": "read_task_result",
            "task_id": task_id,
            "offset": 0,
            "limit": 4000,
            "total_chars": int(getattr(record, "total_chars", 0) or 0),
        },
        "origin": dict(getattr(record, "origin", {}) or {}),
        "finished_at": getattr(record, "finished_at", None),
        "cancelled": bool(getattr(record, "cancelled", False)),
    }


def format_completion_notice(item: dict[str, Any]) -> str:
    """通知轮的用户消息正文：只陈述真实事实与接下来该做什么，不复述全文。"""
    outcome = str(item.get("outcome") or "")
    label = STATUS_LABELS.get(outcome, outcome or "未知")
    lines = [
        "【子任务执行结果】",
        f"task_id: {item.get('task_id')}",
        f"工具: {item.get('tool')}",
        f"真实终态: {outcome or '未知'}（{label}）",
    ]
    if item.get("error"):
        lines.append(f"失败原因: {item['error']}")
    lines.append(f"完整结果: {int(item.get('total_chars') or 0)} 字（用 read_task_result 分段读取）")
    preview = str(item.get("preview") or "").strip()
    if preview:
        lines.append(f"结果预览: {preview}")
    origin = item.get("origin") or {}
    if origin.get("topic_id"):
        lines.append(f"原话题: {origin['topic_id']}")
    if origin.get("turn_id"):
        lines.append(f"原任务轮次: {origin['turn_id']}")
    lines.append(
        "请按原任务目标继续：先检查目标是否仍有效，再读取继续工作所需的完整结果；"
        "原目标有效时在原话题续作，已取消/改目标时不因迟到结果复活旧工作。"
    )
    return "\n".join(lines)


class SubtaskNotificationQueue:
    """可靠、去重的通知队列（接线：task_manager 回调 → enqueue；空档 → take）。

    conn 为 None 时保留**纯内存**形态（兼容既有单测 / 无库场景）：同样不再因超过
    阈值丢弃；但没有重启恢复能力（要持久化就必须传 conn）。
    """

    def __init__(
        self,
        manager: TaskManager,
        *,
        conn: Any | None = None,
        max_pending: int = MAX_PENDING_NOTIFICATIONS,
    ) -> None:
        self.manager = manager
        self.conn = conn
        self._max_pending = max(1, int(max_pending))
        # 内存形态（conn is None）
        self._pending: deque[dict[str, Any]] = deque()
        self._task_ids: set[str] = set()
        if conn is not None:
            self._assert_table_shape()
        self._warned_at = 0

    # -- 表结构自检（缺表 / 缺列如实报错，不静默把通知投进坏表）----------------

    def _assert_table_shape(self) -> None:
        conn = self.conn
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (NOTIFICATION_TABLE,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "subtask_notifications 表不存在：通知队列需要持久化（请先执行迁移 71/72）"
            )
        columns = {
            str(info[1]) for info in conn.execute(f"PRAGMA table_info({NOTIFICATION_TABLE})")
        }
        missing = [name for name in NOTIFICATION_COLUMNS if name not in columns]
        if missing:
            raise RuntimeError(
                "subtask_notifications 表结构不完整，缺少列："
                + "、".join(missing)
                + "（补偿迁移 72 应补齐；请检查迁移是否真正执行）"
            )

    # -- 入队（只持久化去重，不是交付）------------------------------------------

    def enqueue(self, task_id: str, record=None) -> bool:
        """收口回调入口：入队成功返回 True（重复 / 取消 / 非通知终态返回 False）。"""
        task_id = str(task_id or "")
        if not task_id:
            return False
        record = record if record is not None else self.manager.record_info(task_id)
        if record is None:
            return False
        if bool(getattr(record, "cancelled", False)):
            return False
        outcome = str(getattr(record, "outcome", "") or "")
        if not outcome:
            # 旧调用点 / 测试替身可能没有精确终态：按 result.ok 如实回退，不包装成功。
            result = getattr(record, "result", None)
            outcome = OUTCOME_COMPLETED if (result is not None and result.ok) else OUTCOME_FAILED
            try:
                record.outcome = outcome
            except Exception:  # noqa: BLE001 - 只读替身记录不阻断入队判断
                pass
        if outcome not in NOTIFY_OUTCOMES:
            return False
        item = build_notification(task_id, record)

        if self.conn is None:
            if task_id in self._task_ids:
                return False
            self._task_ids.add(task_id)
            self._pending.append(item)
            self._note_queue_depth(len(self._pending))
            return True

        payload = json.dumps(item, ensure_ascii=False, default=str)
        origin = json.dumps(item.get("origin") or {}, ensure_ascii=False, default=str)
        try:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO subtask_notifications"
                " (task_id, payload, origin, created_at, delivered_at, attempts,"
                "  last_error, last_attempt_at)"
                " VALUES (?, ?, ?, ?, NULL, 0, NULL, NULL)",
                (task_id, payload, origin, _now()),
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败必须显式报错，不假装入队成功
            logger.warning("子任务通知入队失败（库写入）：%s", exc, exc_info=True)
            raise
        inserted = int(getattr(cursor, "rowcount", 0) or 0) > 0
        if inserted:
            self._note_queue_depth(self.pending_count())
        return inserted

    def _note_queue_depth(self, count: int) -> None:
        """超过告警阈值只提示（绝不丢弃）；每跨过 100 条再提醒一次。"""
        if count <= self._max_pending:
            return
        if count == self._max_pending + 1 or count - self._warned_at >= 100:
            self._warned_at = count
            logger.warning(
                "子任务通知待投递 %s 条，超过告警阈值 %s（不会丢弃，按 FIFO 继续排队）",
                count,
                self._max_pending,
            )

    # -- 读取 -------------------------------------------------------------------

    def _item_from_row(self, row: Any) -> dict[str, Any]:
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except (TypeError, ValueError):
            payload = {}
        item = dict(payload) if isinstance(payload, dict) else {}
        item["task_id"] = str(row["task_id"])
        item["created_at"] = row["created_at"]
        item["attempts"] = int(row["attempts"] or 0)
        try:
            origin = json.loads(str(row["origin"] or "{}"))
        except (TypeError, ValueError):
            origin = {}
        item["origin"] = origin if isinstance(origin, dict) else {}
        return item

    def _oldest_row(self):
        return self.conn.execute(
            "SELECT task_id, payload, origin, created_at, attempts"
            " FROM subtask_notifications WHERE delivered_at IS NULL"
            " ORDER BY created_at ASC, rowid ASC LIMIT 1"
        ).fetchone()

    def pending(self) -> list[dict[str, Any]]:
        if self.conn is None:
            return list(self._pending)
        rows = self.conn.execute(
            "SELECT task_id, payload, origin, created_at, attempts"
            " FROM subtask_notifications WHERE delivered_at IS NULL"
            " ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def pending_count(self) -> int:
        if self.conn is None:
            return len(self._pending)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM subtask_notifications WHERE delivered_at IS NULL"
        ).fetchone()
        return int(row["n"] or 0)

    def has_pending(self) -> bool:
        return bool(self.pending_count())

    def peek(self) -> dict[str, Any] | None:
        if self.conn is None:
            return dict(self._pending[0]) if self._pending else None
        row = self._oldest_row()
        return None if row is None else self._item_from_row(row)

    # -- 投递状态机 -------------------------------------------------------------

    def take(self) -> dict[str, Any] | None:
        """取最早一条未送达通知（FIFO），并登记一次投递尝试（attempts+1）。

        取出 **不等于** 已交付：只有 mark_delivered 写的 delivered_at 才是交付事实；
        失败用 mark_failed 保留待重试。
        """
        if self.conn is None:
            if not self._pending:
                return None
            item = self._pending.popleft()
            self._task_ids.discard(str(item.get("task_id")))
            return item
        row = self._oldest_row()
        if row is None:
            return None
        item = self._item_from_row(row)
        now = _now()
        self.conn.execute(
            "UPDATE subtask_notifications"
            " SET attempts = attempts + 1, last_attempt_at = ?"
            " WHERE task_id = ? AND delivered_at IS NULL",
            (now, item["task_id"]),
        )
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_attempt_at"] = now
        return item

    def mark_delivered(self, item: dict[str, Any] | None) -> bool:
        """显式确认交付（唯一判据）：只有通知轮被 TurnManager 接受后才允许调用。幂等。"""
        task_id = str((item or {}).get("task_id") or "")
        if not task_id:
            return False
        if self.conn is None:
            return True
        cursor = self.conn.execute(
            "UPDATE subtask_notifications SET delivered_at = ?"
            " WHERE task_id = ? AND delivered_at IS NULL",
            (_now(), task_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def mark_failed(self, item: dict[str, Any] | None, error: str) -> bool:
        """记录一次真实的投递失败：保留记录待重试（不写 delivered_at）。"""
        task_id = str((item or {}).get("task_id") or "")
        if not task_id:
            return False
        if self.conn is None:
            return True
        cursor = self.conn.execute(
            "UPDATE subtask_notifications SET last_error = ?, last_attempt_at = ?"
            " WHERE task_id = ? AND delivered_at IS NULL",
            (str(error or "")[:500], _now(), task_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    # -- 兼容旧调用点 -----------------------------------------------------------

    def requeue(self, item: dict[str, Any]) -> None:
        """通知轮未能启动 / 失败：保留待重试（幂等，不产生重复）。"""
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return
        if self.conn is None:
            if task_id in self._task_ids:
                return
            self._pending.appendleft(item)
            self._task_ids.add(task_id)
            return
        self.mark_failed(item, "requeue: 通知轮未能启动")

    def mark_processed(self, item: dict[str, Any]) -> bool:
        """显式确认这条通知已经交付（用于接线方不能用 take 的异步场景）。"""
        if self.conn is None:
            task_id = str(item.get("task_id") or "")
            if not task_id:
                return False
            self._task_ids.discard(task_id)
            kept = [entry for entry in self._pending if str(entry.get("task_id")) != task_id]
            removed = len(kept) != len(self._pending)
            self._pending = deque(kept)
            return removed
        return self.mark_delivered(item)

    def clear(self) -> None:
        """清空**未送达**通知（测试 / 显式放弃用；已交付的历史事实不删）。"""
        if self.conn is None:
            self._pending.clear()
            self._task_ids.clear()
            return
        self.conn.execute("DELETE FROM subtask_notifications WHERE delivered_at IS NULL")
        self._warned_at = 0
