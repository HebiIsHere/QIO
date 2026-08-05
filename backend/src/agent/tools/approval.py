"""Approval service: emits APPROVAL_REQUIRED and waits for APPROVAL_RESULT.

The lifecycle uses two approval segments: tool creation, and credential
grant (only when the tool references a credential). Responses arrive via
the HTTP API (POST /api/approvals/{id}/respond).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.api.events import EventType, make_event

DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass
class ApprovalRequest:
    approval_id: str
    kind: str
    payload: dict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class ApprovalResult:
    approval_id: str
    decision: str  # approved / rejected / timeout
    scope: dict | None = None


class ApprovalService:
    def __init__(self, bus, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds
        self._waiters: dict[str, asyncio.Future[ApprovalResult]] = {}

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalResult] = loop.create_future()
        self._waiters[approval_id] = future
        request = ApprovalRequest(approval_id, kind, payload)
        await self.bus.publish(
            make_event(EventType.APPROVAL_REQUIRED, {"approval": request.__dict__})
        )
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return ApprovalResult(approval_id, "timeout")
        finally:
            self._waiters.pop(approval_id, None)

    async def respond(self, approval_id: str, decision: str, scope: dict | None = None) -> bool:
        """Called by the API layer when the frontend answers."""
        future = self._waiters.get(approval_id)
        if future is None or future.done():
            return False
        if decision not in ("approved", "rejected"):
            raise ValueError(f"invalid decision: {decision}")
        result = ApprovalResult(approval_id, decision, scope)
        future.set_result(result)
        await self.bus.publish(
            make_event(
                EventType.APPROVAL_RESULT,
                {"approval_id": approval_id, "decision": decision},
            )
        )
        return True