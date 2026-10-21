"""子任务完成通知：可靠排队、去重、原话题续作事实（阶段四 A；契约 §14.3）。

为什么单独一层：TaskManager 的 notify 回调发生在任务收口的那一瞬间，而主循环
可能正在响应用户（不能打断当前交流）、也可能正在跑另一条通知轮。直接在回调里
「push 进当前轮或立刻 submit 一个系统轮」会有两个已知坏结果：

* 处理另一条通知时到达的通知被丢弃（真实缺陷：结果无声消失）；
* 迟到结果把已经被取消的任务重新复活。

这里把「任务收口」和「真正通知主 agent」拆开：收口时只**入队**（按 task_id 去重、
终态过滤、取消不通知），由接线方在合适的时机（没有 active 用户轮 / 轮末）取一条
（FIFO）交给系统通知轮；失败可 requeue，不丢。通知事实携带程序绑定的原任务
（origin: turn_id / topic_id / goal_version），供轮末在原话题续作、不切界面。
"""

from __future__ import annotations

from collections import deque
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

STATUS_LABELS: dict[str, str] = {
    OUTCOME_COMPLETED: "完成",
    OUTCOME_PARTIALLY_COMPLETED: "部分完成",
    OUTCOME_FAILED: "失败",
    OUTCOME_BUDGET_EXHAUSTED: "预算耗尽",
    OUTCOME_CANCELLED: "已取消",
}

#: 单次通知里给模型的预览上限（全文用 read_task_result 读取，不塞进通知正文）。
NOTICE_PREVIEW_CHARS = 500
MAX_PENDING_NOTIFICATIONS = 200


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
    """可靠、去重的通知队列（Lead 接线：task_manager 回调 → enqueue；空档 → take）。"""

    def __init__(self, manager: TaskManager, *, max_pending: int = MAX_PENDING_NOTIFICATIONS) -> None:
        self.manager = manager
        self._pending: deque[dict[str, Any]] = deque()
        self._task_ids: set[str] = set()
        self._max_pending = max(1, max_pending)

    def enqueue(self, task_id: str, record=None) -> bool:
        """收口回调入口：入队成功返回 True（重复 / 取消 / 非通知终态返回 False）。"""
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
        if task_id in self._task_ids:
            return False
        # TaskManager 级去重：同一条任务只通知一次（跨队列重建也成立）
        if not self.manager.mark_notified(task_id):
            return False
        item = build_notification(task_id, record)
        self._pending.append(item)
        self._task_ids.add(task_id)
        while len(self._pending) > self._max_pending:
            dropped = self._pending.popleft()
            self._task_ids.discard(str(dropped.get("task_id")))
        return True

    def pending(self) -> list[dict[str, Any]]:
        return list(self._pending)

    def pending_count(self) -> int:
        return len(self._pending)

    def has_pending(self) -> bool:
        return bool(self._pending)

    def take(self) -> dict[str, Any] | None:
        """取最早一条通知（FIFO）。处理失败可 requeue，不丢。"""
        if not self._pending:
            return None
        item = self._pending.popleft()
        self._task_ids.discard(str(item.get("task_id")))
        return item

    def peek(self) -> dict[str, Any] | None:
        return self._pending[0] if self._pending else None

    def requeue(self, item: dict[str, Any]) -> None:
        """通知轮未能启动 / 失败：放回队首（幂等，不产生重复）。"""
        task_id = str(item.get("task_id") or "")
        if not task_id or task_id in self._task_ids:
            return
        self._pending.appendleft(item)
        self._task_ids.add(task_id)

    def mark_processed(self, item: dict[str, Any]) -> bool:
        """显式确认这条通知已经交付（用于接线方不能用 take 的异步场景）。"""
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return False
        self._task_ids.discard(task_id)
        kept = [entry for entry in self._pending if str(entry.get("task_id")) != task_id]
        replaced = len(kept) != len(self._pending)
        self._pending = deque(kept)
        return replaced

    def clear(self) -> None:
        self._pending.clear()
        self._task_ids.clear()
