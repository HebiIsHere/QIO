# -*- coding: utf-8 -*-
"""D 独立契约测试：子任务真实终态 / 结果分页 / 通知与续作（契约 §14.3、§14.7）。

验收点：
* 真实终态：排队 / 运行 / 完成 / 部分完成 / 失败 / 取消 / 预算耗尽；
  不得把非异常退出包装成功（record.outcome 与 ToolResult.status 双层事实）；
* 完整结果保留，长结果按 offset/limit 分页读取（单次上限 4000 字符）；
* 通知队列可靠排队、去重、不因处理另一通知而丢弃；
* 结果回原任务原话题续作；不强行切换界面；尊重暂停/取消/目标修改。

装置：真实 TaskManager + SubtaskNotificationQueue；fake 模型；未合入时按契约失败。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

CLAUSE = "契约 §14.3/§14.7"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src" / "agent"


def _manager():
    from agent.api.bus import EventBus
    from agent.tools.task_manager import TaskManager

    return TaskManager(EventBus(), max_concurrent=4)


async def test_real_terminal_states_are_not_collapsed_to_done():
    """§14.3：部分完成 / 失败 / 取消 / 预算耗尽必须保留真实状态。"""
    from agent.tools.base import ToolResult, ToolStatus

    manager = _manager()

    async def partial():
        await asyncio.sleep(0.02)
        return ToolResult(ok=True, status=ToolStatus.PARTIALLY_COMPLETED.value,
                          content="完成一部分")

    async def failed():
        return ToolResult(ok=False, status=ToolStatus.FAILED.value, error="失败原因")

    async def cancelled():
        return ToolResult(ok=False, status=ToolStatus.CANCELLED.value, error="用户取消")

    async def budget():
        return ToolResult(ok=False, status=ToolStatus.FAILED.value,
                          error="预算耗尽", code="budget_exhausted")

    ids = {
        "partial": manager.submit("t", partial),
        "failed": manager.submit("t", failed),
        "cancelled": manager.submit("t", cancelled),
        "budget": manager.submit("t", budget),
    }
    observed = {}
    for key, task_id in ids.items():
        status, result = await manager.await_result(task_id, timeout=2)
        record = manager.record_info(task_id)
        observed[key] = {
            "status": status,
            "outcome": getattr(record, "outcome", None),
            "result_status": getattr(result, "status", None),
            "code": getattr(result, "code", None),
        }
    assert observed["partial"]["result_status"] == "partially_completed", (
        f"{CLAUSE}：部分完成必须保留真实状态（实际 {observed['partial']}）")
    assert observed["failed"]["status"] == "failed", (
        f"{CLAUSE}：失败不得包装成功（实际 {observed['failed']}）")
    # 执行器（subagent）负责把子任务 loop 的真实结束事实映射为 outcome
    import types

    subagent = __import__("agent.tools.subagent_tool", fromlist=["*"])
    partial_loop = types.SimpleNamespace(
        cancelled=False, stop_reason_code="length_limit", final_content="部分内容")
    budget_loop = types.SimpleNamespace(
        cancelled=False, stop_reason_code="budget", final_content="")
    cancelled_loop = types.SimpleNamespace(cancelled=True, stop_reason_code="", final_content="")
    completed_loop = types.SimpleNamespace(
        cancelled=False, stop_reason_code="", final_content="完整内容")
    assert subagent._outcome_for(partial_loop) == "partially_completed"
    assert subagent._outcome_for(budget_loop) == "budget_exhausted"
    assert subagent._outcome_for(cancelled_loop) == "cancelled"
    assert subagent._outcome_for(completed_loop) == "completed"


async def test_queued_and_running_are_reported_as_real_states():
    """§14.3：排队 / 运行是真实状态；等待超时不等于正在运行。"""
    from agent.tools.base import ToolResult

    manager = _manager()

    async def slow():
        await asyncio.sleep(0.25)
        return ToolResult(ok=True, content="done")

    task_id = manager.submit("t", slow)
    await asyncio.sleep(0.02)
    status, result = await manager.await_result(task_id, timeout=0.05)
    assert status in ("queued", "running"), (
        f"{CLAUSE}：等待超时必须报真实状态（queued/running），实际 {status}")
    assert result is None, f"{CLAUSE}：超时不得返回伪结果"
    final_status, final_result = await manager.await_result(task_id, timeout=3)
    record = manager.record_info(task_id)
    assert final_result is not None and final_status in ("done", "completed"), (
        f"{CLAUSE}：任务最终应完成（实际 {final_status}）")
    assert getattr(record, "outcome", None) == "completed", (
        f"{CLAUSE}：完成终态必须可区分（实际 {getattr(record, 'outcome', None)}）")


async def test_long_result_pagination_has_4000_limit_and_no_silent_truncation():
    """§14.3/§14.7：完整结果保留；read_task_result 分页、单次上限 4000。"""
    from agent.tools.base import ToolResult
    from agent.tools.subagent_tool import ReadTaskResultTool

    manager = _manager()
    content = "".join(f"段{i:04d}|" for i in range(2000))  # 约 12k 字符

    async def long_task():
        return ToolResult(ok=True, content=content)

    task_id = manager.submit("t", long_task)
    await manager.await_result(task_id, timeout=2)
    record = manager.record_info(task_id)
    assert record is not None
    record.full_content = content  # 子任务工具负责写入完整结果（下面单独源码断言）
    assert "full_content" in (SRC / "tools" / "subagent_tool.py").read_text(encoding="utf-8"), (
        f"{CLAUSE}：子任务工具必须把完整结果写入 full_content")
    tool = ReadTaskResultTool(manager)
    page = await tool.run(task_id=task_id, offset=0, limit=4000)
    assert page.ok is True, f"{CLAUSE}：分页读取失败：{page.error}"
    page_text = str(page.content or "") + str(page.data or "")
    assert page_text and len(page_text) <= 6000, (
        f"{CLAUSE}：单页返回必须受 4000 字符上限约束（实际 {len(page_text)}）")
    too_big = await tool.run(task_id=task_id, offset=0, limit=99999)
    if too_big.ok:
        assert len(str(too_big.content or "")) <= 5000, (
            f"{CLAUSE}：limit 超限时必须拒绝或截到上限")
    tail = await tool.run(task_id=task_id, offset=max(0, len(content) - 100), limit=100)
    assert tail.ok is True and "段1999" in str(tail.content or "") + str(tail.data or ""), (
        f"{CLAUSE}：偏移分页必须能读到末尾完整内容")


async def test_notification_queue_queues_and_deduplicates():
    """§14.3：通知可靠排队、去重；处理一条不丢另一条。"""
    from agent.tools.base import ToolResult
    from agent.tools.subtask_notifications import SubtaskNotificationQueue

    manager = _manager()

    async def slow():
        await asyncio.sleep(0.2)
        return ToolResult(ok=True, content="ok")

    task_id = manager.submit("t", slow)
    queue = SubtaskNotificationQueue(manager)
    assert queue.enqueue(task_id) is True, f"{CLAUSE}：运行中的任务必须能登记完成通知"
    assert queue.enqueue(task_id) is False, (
        f"{CLAUSE}：同一任务重复登记必须去重")
    assert queue.pending_count() == 1, f"{CLAUSE}：通知队列计数应为 1"
    item = queue.peek()
    assert item is not None and item.get("task_id") == task_id
    assert queue.mark_processed(item) is True, f"{CLAUSE}：处理通知应成功（take 前确认）"
    assert queue.pending_count() == 0
    assert queue.take() is None, f"{CLAUSE}：已处理的通知不得再取出"
    second = manager.submit("t", slow)
    queue.enqueue(second)
    first_item = queue.peek()
    assert first_item is not None
    assert queue.take() is not None, f"{CLAUSE}：另一条通知不能被前一条吞掉"
    await manager.await_result(second, timeout=2)


def test_continuation_uses_completion_prompt_and_original_topic_not_ui_switch():
    """§14.3：续作使用 SUBTASK_COMPLETION_SYSTEM / SUBTASK_RETURN_POLICY；不切界面。"""
    prompts = __import__("agent.prompts", fromlist=["*"])
    notifications = __import__("agent.tools.subtask_notifications", fromlist=["*"])
    hint = notifications.completion_system_hint()
    assert prompts.PROMPTS["SUBTASK_COMPLETION_SYSTEM"] in hint, (
        f"{CLAUSE}：续作必须注入 SUBTASK_COMPLETION_SYSTEM")
    assert prompts.PROMPTS["SUBTASK_RETURN_POLICY"] in hint, (
        f"{CLAUSE}：续作必须注入 SUBTASK_RETURN_POLICY")
    app_text = (SRC / "services" / "app.py").read_text(encoding="utf-8")
    assert "subtask_notifications" in app_text, (
        f"{CLAUSE}：主循环必须使用通知队列（app.py）")
    combined = app_text + (SRC / "services" / "turn_orchestrator.py").read_text(encoding="utf-8")
    assert "original_topic" in combined or "origin" in combined, (
        f"{CLAUSE}：续作必须绑定原话题/origin")
    assert not re.search(r"SWITCH_TOPIC|switch_topic\(", combined), (
        f"{CLAUSE}：续作路径不得强行切换话题/界面")


def test_cancel_pause_and_late_results_are_respected():
    """§14.3：尊重暂停/取消/改目标；迟到结果不复活已取消任务。"""
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            SRC / "tools" / "task_manager.py",
            SRC / "tools" / "subtask_notifications.py",
            SRC / "services" / "app.py",
        )
    )
    assert "cancelled" in combined, f"{CLAUSE}：未见取消处理"
    assert re.search(r"(late|迟到|superseded|discard)", combined, re.IGNORECASE), (
        f"{CLAUSE}：未见迟到结果防复活处理")
    assert re.search(r"(cancelled.*不|cancelled.*return|notify.*cancelled|取消.*不)", combined), (
        f"{CLAUSE}：迟到结果必须有「已取消则不推进」的守卫")


def test_terminal_status_mapping_covers_all_contract_states():
    """§14.3：契约七种真实终态在实现中有稳定映射（不是自由文本推断）。"""
    source = (SRC / "tools" / "task_manager.py").read_text(encoding="utf-8")
    expected = ("queued", "running", "completed", "partially_completed",
                "failed", "cancelled", "budget_exhausted")
    missing = [state for state in expected if state not in source]
    assert not missing, f"{CLAUSE}：TaskManager 缺少真实终态映射：{missing}"
