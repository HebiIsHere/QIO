"""R9-G 批量操作逐项展开与状态（契约 §6）。

批量入口把一批操作展开成独立的 `BatchItem`（每项一个 effect + 自己的对象/内容
指纹），再逐项交给统一引擎判定；本模块**不聚合**判定结论，也不把一项的允许
当成整批的允许。需要用户确认的项由调用方合并成**一个**审批操作（payload.items
列明逐项对象/动作/内容），已允许项直接执行，被拒/失效项保留并如实报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

#: 逐项状态：判定/执行生命周期（界面与恢复流程按它展示）。
STATUS_ALLOWED = "allowed"
STATUS_DENIED = "denied"
STATUS_WAITING = "waiting"
STATUS_INVALIDATED = "invalidated"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

TERMINAL_SUCCESS = frozenset({STATUS_COMPLETED})
TERMINAL_FAILURE = frozenset({STATUS_DENIED, STATUS_INVALIDATED, STATUS_FAILED})


@dataclass(frozen=True)
class BatchItem:
    item_id: str
    effect: Any
    label: str = ""
    payload: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BatchItemDecision:
    item: BatchItem
    status: str
    decision: Any = None
    reason: str = ""


@dataclass(frozen=True)
class BatchAuthorization:
    items: tuple[BatchItemDecision, ...] = ()

    def by_status(self, status: str) -> tuple[BatchItemDecision, ...]:
        return tuple(item for item in self.items if item.status == status)

    @property
    def allowed(self) -> tuple[BatchItemDecision, ...]:
        return self.by_status(STATUS_ALLOWED)

    @property
    def waiting(self) -> tuple[BatchItemDecision, ...]:
        return self.by_status(STATUS_WAITING)

    @property
    def denied(self) -> tuple[BatchItemDecision, ...]:
        return self.by_status(STATUS_DENIED)


async def authorize_batch_items(
    engine: Any, items: Sequence[BatchItem], *, ctx: Any = None
) -> BatchAuthorization:
    """逐项调用统一引擎；每项独立结论，不把一个对象的允许覆盖整批。"""
    decisions: list[BatchItemDecision] = []
    for item in items:
        decision = await engine.authorize(item.effect, ctx=ctx)
        verdict = str(getattr(getattr(decision, "verdict", None), "value", "") or "")
        if verdict == "allow":
            status = STATUS_ALLOWED
        elif verdict == "deny":
            status = STATUS_DENIED
        else:
            status = STATUS_WAITING
        decisions.append(
            BatchItemDecision(
                item=item,
                status=status,
                decision=decision,
                reason=str(getattr(decision, "reason", "") or ""),
            )
        )
    return BatchAuthorization(items=tuple(decisions))
