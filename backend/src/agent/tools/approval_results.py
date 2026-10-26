"""旧审批（legacy request()）执行结果闭环的唯一收尾入口。

为什么需要它：旧 /respond 路径只把用户的决定交给等待中的工具；工具随后真实执行，
但没有任何地方把「执行完了没有、成功还是失败」写回审批历史/事件 —— 界面上
「已批准」永远升不到「已执行成功 / 执行失败」。契约（COMMON §4.2）要求在**工具执行
边界**统一收尾，而不是在每个工具里复制粘贴：

1. ToolRegistry.execute 进入时 begin_collection（ContextVar 里放本次调用的旧审批 id
   列表；并行调用各自 task 隔离，嵌套调用各有自己的作用域）；
2. 旧 ApprovalService.request() 拿到 approved 结果后 register_approved；
3. 工具返回（成功 / 失败 / 取消）后 settle 统一写 approval_history 与
   APPROVAL_RESULT(phase=executed/failed)。只记录结果，**不重新执行**任何工具。

没有 collector 的调用方（单元测试、非工具上下文）保持原行为：登记是 no-op。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any

logger = logging.getLogger(__name__)

#: 这些工具状态表示「本次调用还没结束」：此时不能写 executed/failed，
#: 保留「已批准（等待执行）」的中间事实 —— 不能把等待说成成功。
_PENDING_STATUSES = frozenset({"awaiting_approval", "awaiting_input", "queued", "running"})

_collector: ContextVar[list[str] | None] = ContextVar(
    "qio_legacy_approval_results", default=None
)


def begin_collection() -> Token:
    """进入一次工具调用的收尾作用域（可嵌套：内层覆盖外层，退出时恢复）。"""
    return _collector.set([])


def reset_collection(token: Token) -> None:
    try:
        _collector.reset(token)
    except ValueError:  # pragma: no cover - 跨上下文重置（防御；不影响执行）
        pass


def register_approved(approval_id: str) -> None:
    """登记一条刚被批准、等待真实执行结果的旧审批（无 collector 时 no-op）。"""
    backlog = _collector.get()
    if backlog is None:
        return
    identifier = str(approval_id or "")
    if identifier and identifier not in backlog:
        backlog.append(identifier)


async def settle(approvals: Any, result: Any) -> list[dict]:
    """把本次工具调用里已批准的旧审批统一收口（成功 / 失败 / 取消）。"""
    backlog = _collector.get()
    if approvals is None or not backlog:
        return []
    ids = list(backlog)
    backlog.clear()
    status = str(getattr(result, "status", "") or "")
    if status in _PENDING_STATUSES:
        # 工具还在等别的东西（例如另一个 v2 审批）：执行没有结束，保持中间事实。
        return []
    ok = bool(getattr(result, "ok", False))
    error = getattr(result, "error", None)
    settled: list[dict] = []
    for approval_id in ids:
        try:
            event = await approvals.record_legacy_execution(
                approval_id, ok=ok, error=error
            )
        except Exception:  # noqa: BLE001 - 收尾失败不能改变工具结果
            logger.warning(
                "failed to settle legacy approval %s", approval_id, exc_info=True
            )
            continue
        if event:
            settled.append(event)
    return settled
