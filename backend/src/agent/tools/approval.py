"""Approval service: emits APPROVAL_REQUIRED and waits for APPROVAL_RESULT.

审批是一个**有身份、有寿命、只能消费一次**的授权对象：

* 绑定 `approval_id` + `turn_id` + `session_id`；
* 带 `created_at` / `expires_at`（过期即失效）；
* 带 `request_digest`（对 kind + payload 的摘要）：提交结果时可以比对，
  防止「批准 A 却被拿去执行 B」；
* 单次使用：一旦应答（或超时）立刻从等待表里摘掉，重放一律失败。

Responses arrive via the HTTP API (POST /api/approvals/{id}/respond).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agent.api.events import EventType, make_event

DEFAULT_TIMEOUT_SECONDS = 300.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def request_digest(kind: str, payload: dict) -> str:
    """审批请求的稳定摘要（不含密钥明文，只有哈希）。"""
    try:
        blob = json.dumps(
            {"kind": kind, "payload": payload}, ensure_ascii=False, sort_keys=True, default=str
        )
    except Exception:  # noqa: BLE001 - 任何结构都要能算出摘要
        blob = f"{kind}:{payload!r}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


@dataclass
class ApprovalRequest:
    approval_id: str
    kind: str
    payload: dict
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    turn_id: str | None = None
    session_id: str | None = None
    expires_at: str | None = None
    digest: str = ""

    def as_payload(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "expires_at": self.expires_at,
            "request_digest": self.digest,
        }


@dataclass(frozen=True)
class ApprovalResult:
    approval_id: str
    decision: str  # approved / rejected / timeout
    scope: dict | None = None
    overrides: dict | None = None


class ApprovalService:
    def __init__(self, bus, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds
        self._waiters: dict[str, asyncio.Future[ApprovalResult]] = {}
        self._requests: dict[str, ApprovalRequest] = {}
        self._turn_id: str | None = None
        self._session_id: str | None = None

    def set_context(self, *, turn_id: str | None = None, session_id: str | None = None) -> None:
        """当前上下文（本轮 turn / 本会话）：新审批自动绑定到它。

        由 turn 运行时在开始一轮时设置，这样审批不需要每个工具各自传参。
        """
        self._turn_id = turn_id
        self._session_id = session_id

    async def request(
        self,
        kind: str,
        payload: dict,
        *,
        turn_id: str | None = None,
        session_id: str | None = None,
    ) -> ApprovalResult:
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalResult] = loop.create_future()
        self._waiters[approval_id] = future
        created = _now()
        expires = created + timedelta(seconds=self.timeout_seconds)
        request = ApprovalRequest(
            approval_id=approval_id,
            kind=kind,
            payload=payload,
            created_at=created.isoformat(),
            turn_id=turn_id if turn_id is not None else self._turn_id,
            session_id=session_id if session_id is not None else self._session_id,
            expires_at=expires.isoformat(),
            digest=request_digest(kind, payload),
        )
        self._requests[approval_id] = request
        await self.bus.publish(
            make_event(EventType.APPROVAL_REQUIRED, {"approval": request.as_payload()})
        )
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            return ApprovalResult(approval_id, "timeout")
        finally:
            self._waiters.pop(approval_id, None)
            self._requests.pop(approval_id, None)

    async def respond(
        self,
        approval_id: str,
        decision: str,
        scope: dict | None = None,
        overrides: dict | None = None,
        *,
        turn_id: str | None = None,
        session_id: str | None = None,
        digest: str | None = None,
    ) -> bool:
        """Called by the API layer when the frontend answers.

        单次使用：等待表/请求表里已经不存在（应答过、超时过、id 是伪造的）→ False。
        """
        future = self._waiters.get(approval_id)
        if future is None or future.done():
            return False
        request = self._requests.get(approval_id)
        if request is None:
            return False
        if request.expires_at and request.expires_at < _now().isoformat():
            self._waiters.pop(approval_id, None)
            self._requests.pop(approval_id, None)
            return False
        if turn_id is not None and request.turn_id and turn_id != request.turn_id:
            raise ValueError("approval does not belong to this turn")
        if session_id is not None and request.session_id and session_id != request.session_id:
            raise ValueError("approval does not belong to this session")
        if digest is not None and request.digest and digest != request.digest:
            raise ValueError("approval digest mismatch")
        if decision not in ("approved", "rejected"):
            raise ValueError(f"invalid decision: {decision}")
        result = ApprovalResult(approval_id, decision, scope, overrides)
        future.set_result(result)
        # 单次使用：立刻失效，重放必然失败
        self._waiters.pop(approval_id, None)
        self._requests.pop(approval_id, None)
        await self.bus.publish(
            make_event(
                EventType.APPROVAL_RESULT,
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "turn_id": request.turn_id,
                },
            )
        )
        return True
