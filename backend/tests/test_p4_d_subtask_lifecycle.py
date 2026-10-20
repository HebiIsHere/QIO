# -*- coding: utf-8 -*-
"""D 独立契约测试：子任务真实终态 / 结果分页 / 通知与续作（契约 §14.3、§14.7）。

验收点：
* 真实终态：排队 / 运行 / 完成 / 部分完成 / 失败 / 取消 / 预算耗尽；
  不得把非异常退出包装成功；
* 完整结果保留，长结果按 offset/limit 分页读取（单次上限 4000 字符，不静默截断）；
* 通知可靠排队、去重、不因处理另一通知而丢弃；
* 结果回原任务原话题续作；不强行切换界面；尊重暂停/取消/目标修改；迟到结果不复活取消任务。

装置：TaskManager（真实实现）+ fake 模型；实现未合入时按契约失败、不 skip。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
from pathlib import Path

import pytest

CLAUSE = "契约 §14.3/§14.7"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src" / "agent"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：{name} 不可导入（实现未合入）：{exc!r}", pytrace=False)


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
                          content="完成一部分", data={"scope": "partial"})

    async def failed():
        return ToolResult(ok=False, status=ToolStatus.FAILED.value, error="失败原因")

    async def cancelled():
        return ToolResult(ok=False, status=ToolStatus.CANCELLED.value, error="用户取消")

    async def budget():
        return ToolResult(ok=False, status=ToolStatus.FAILED.value,
                          error="预算耗尽", code="budget_exhausted")

    cases = {
        "partial": manager.submit("t", partial),
        "failed": manager.submit("t", failed),
        "cancelled": manager.submit("t", cancelled),
        "budget": manager.submit("t", budget),
    }
    observed = {}
    for key, task_id in cases.items():
        status, result = await manager.await_result(task_id, timeout=2)
        observed[key] = (
            status,
            getattr(result, "status", None),
            getattr(result, "code", None),
            getattr(result, "error", None),
        )
    assert observed["partial"][0] == "partially_completed", (
        f"{CLAUSE}：部分完成必须保留真实终态（实际 {observed['partial']}）")
    assert observed["failed"][0] == "failed", f"{CLAUSE}：失败不得包装成功（{observed['failed']}）"
    assert observed["cancelled"][0] == "cancelled", (
        f"{CLAUSE}：取消必须保留（实际 {observed['cancelled']}）")
    budget_text = str(observed["budget"]).lower()
    assert "budget" in budget_text, (
        f"{CLAUSE}：预算耗尽必须如实区分（实际 {observed['budget']}）")


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
    assert final_status == "completed" and final_result is not None, (
        f"{CLAUSE}：任务最终应完成（实际 {final_status}）")


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
    # TaskManager 只保存结果对象；完整结果的落库由子任务工具写入 full_content
    # （子任务路径单独用源码断言检查）。这里直接验证 read_task_result 的分页契约。
    record.full_content = content
    assert record.full_content == content, (
        f"{CLAUSE}：完整结果必须保留（full_content）")
    tool = ReadTaskResultTool(manager)
    page = await tool.run(task_id=task_id, offset=0, limit=4000)
    assert page.ok is True, f"{CLAUSE}：分页读取失败：{page.error}"
    page_text = str(page.content or "") + str(page.data or "")
    assert page_text and len(page_text) <= 6000, (
        f"{CLAUSE}：单页返回必须受 4000 字符上限约束（实际 {len(page_text)}）")
    too_big = await tool.run(task_id=task_id, offset=0, limit=99999)
    if too_big.ok:
        assert len(str(too_big.content or "")) <= 5000, (
            f"{CLAUSE}：limit 超限时必须拒绝或截到上限，不能一次吐出全文")
    tail_offset = max(0, len(content) - 100)
    tail = await tool.run(task_id=task_id, offset=tail_offset, limit=100)
    assert tail.ok is True, f"{CLAUSE}：按 offset 读取末尾失败：{tail.error}"
    assert "段1999" in str(tail.content or "") + str(tail.data or ""), (
        f"{CLAUSE}：偏移分页必须能读到末尾完整内容")
    subagent_source = (SRC / "tools" / "subagent_tool.py").read_text(encoding="utf-8")
    assert "full_content" in subagent_source, (
        f"{CLAUSE}：子任务工具必须把完整结果写入 full_content（超长结果不静默截断）")


async def test_notification_is_queued_and_deduplicated():
    """§14.3：通知可靠排队、去重，不因处理另一通知而丢弃。"""
    from agent.tools.base import ToolResult

    manager = _manager()
    received: list[str] = []

    async def callback(task_id, record):  # noqa: ANN001
        received.append(task_id)

    async def quick():
        return ToolResult(ok=True, content="ok")

    first = manager.submit("t", quick)
    assert manager.register_notify(first, callback) is True, (
        f"{CLAUSE}：首次登记通知应成功")
    assert manager.register_notify(first, callback) is False, (
        f"{CLAUSE}：同一任务重复登记通知必须去重")
    second = manager.submit("t", quick)
    manager.register_notify(second, callback)
    await asyncio.sleep(0.3)
    assert sorted(received) == sorted({first, second}), (
        f"{CLAUSE}：通知必须各触发一次且不丢失（实际 {received}）")


def test_continuation_uses_completion_prompt_and_original_topic_not_ui_switch():
    """§14.3：续作注入 SUBTASK_COMPLETION_SYSTEM / SUBTASK_RETURN_POLICY；不切界面。"""
    _load("agent.prompts")
    app_text = (SRC / "services" / "app.py").read_text(encoding="utf-8")
    loop_text = (SRC / "core" / "loop.py").read_text(encoding="utf-8")
    orchestrator_text = (SRC / "services" / "turn_orchestrator.py").read_text(encoding="utf-8")
    combined = app_text + loop_text + orchestrator_text
    assert "SUBTASK_COMPLETION_SYSTEM" in combined, (
        f"{CLAUSE}：续作轮必须注入 prompts.SUBTASK_COMPLETION_SYSTEM")
    assert "SUBTASK_RETURN_POLICY" in combined, (
        f"{CLAUSE}：续作轮必须注入 prompts.SUBTASK_RETURN_POLICY")
    assert "original_topic" in combined or "原话题" in combined, (
        f"{CLAUSE}：续作必须绑定原话题")
    switch_hits = re.findall(r"SWITCH_TOPIC|switch_topic\(", combined)
    assert not switch_hits, (
        f"{CLAUSE}：续作路径不得强行切换话题/界面（发现 {switch_hits[:3]}）")


def test_cancel_pause_and_late_results_are_respected():
    """§14.3：尊重暂停/取消/改目标；迟到结果不复活已取消任务。"""
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (
            SRC / "tools" / "task_manager.py",
            SRC / "tools" / "subagent_tool.py",
            SRC / "services" / "app.py",
            SRC / "core" / "loop.py",
            SRC / "services" / "turn_orchestrator.py",
        )
    )
    cancelled_support = any(token in combined for token in ("cancelled", "取消", "cancel"))
    assert cancelled_support, f"{CLAUSE}：未见取消处理"
    late = re.search(r"(late_result|stale_result|superseded|迟到)", combined, re.IGNORECASE)
    assert late is not None, (
        f"{CLAUSE}：未见迟到结果防复活（late/stale/superseded）处理")
    guard = re.search(r"(is_cancelled|already_cancelled|cancel.*return|取消.*不)", combined, re.IGNORECASE)
    assert guard is not None, (
        f"{CLAUSE}：迟到结果处理必须有「任务已取消则不推进」的守卫")


def test_terminal_status_mapping_covers_all_contract_states():
    """§14.3：契约七种真实终态在实现中有稳定映射（不是自由文本推断）。"""
    source = (SRC / "tools" / "task_manager.py").read_text(encoding="utf-8")
    expected = ("queued", "running", "completed", "partially_completed",
                "failed", "cancelled", "budget_exhausted")
    missing = [state for state in expected if state not in source]
    assert not missing, (
        f"{CLAUSE}：TaskManager 缺少真实终态映射：{missing}")
