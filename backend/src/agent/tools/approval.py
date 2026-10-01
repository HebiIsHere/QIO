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
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agent.api.events import EventType, make_event

DEFAULT_TIMEOUT_SECONDS = 300.0
logger = logging.getLogger(__name__)


def refusal_reason(decision: str) -> str:
    """审批没通过的人话原因：超时 / 拒绝 / 取消必须分开说。

    真实事故：三种结局被写成同一句「未获批准，未执行」，读起来像是用户拒绝了，
    实际是 5 分钟自动过期（一轮里 6 次）。
    """
    if decision == "timeout":
        return "审批等待超时（等你确认超过 5 分钟，已自动取消）"
    if decision == "rejected":
        return "你点了拒绝"
    if decision == "cancelled":
        return "本轮已停止"
    return "未获批准"


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
    def __init__(
        self,
        bus,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        conn=None,
    ) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds
        self._waiters: dict[str, asyncio.Future[ApprovalResult]] = {}
        self._requests: dict[str, ApprovalRequest] = {}
        self._turn_id: str | None = None
        self._session_id: str | None = None
        # 可选的持久化连接（见迁移 23）：有它才记录「等待中的审批」，
        # 这样重启后能说清「那次操作没有执行」；没它就与旧行为完全一致。
        self.conn = conn
        self._mark_interrupted()

    # -- 跨重启的等待记录 --------------------------------------------------

    def _mark_interrupted(self) -> None:
        """启动时把上一个进程留下的 pending 一律标成 interrupted。

        判定放在构造时：新进程刚开始不可能有自己的等待项 —— 此刻还写着 pending
        的，全是上一个进程没来得及回答的（不恢复等待，只留一条明确记录）。
        """
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "UPDATE pending_approvals SET status = 'interrupted', resolved_at = ? "
                "WHERE status = 'pending'",
                (_now().isoformat(),),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 - 记录失败不能挡住启动
            logger.warning("failed to mark interrupted approvals", exc_info=True)

    def _remember(self, request: ApprovalRequest) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO pending_approvals "
                "(approval_id, kind, payload, turn_id, session_id, created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    request.approval_id,
                    request.kind,
                    json.dumps(request.payload or {}, ensure_ascii=False),
                    request.turn_id,
                    request.session_id,
                    request.created_at,
                    request.expires_at,
                ),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 - 落库失败不能挡住审批本身
            logger.warning("failed to persist pending approval", exc_info=True)

    def _settle(self, approval_id: str, status: str) -> None:
        """收口一条等待记录（单次使用：只有仍是 pending 的才会被改）。"""
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "UPDATE pending_approvals SET status = ?, resolved_at = ? "
                "WHERE approval_id = ? AND status = 'pending'",
                (status, _now().isoformat(), approval_id),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 - 同上
            logger.warning("failed to settle pending approval", exc_info=True)

    def interrupted(self, limit: int = 20) -> list[dict]:
        """上一次进程结束时仍没人回答的审批（给界面看的事实，不是待办）。

        它们**不会再恢复等待**：等待中的那次工具调用随进程一起没了。所以这里
        只报告「那一次操作没有执行」，让用户知道发生过什么。
        """
        if self.conn is None:
            return []
        try:
            rows = self.conn.execute(
                "SELECT approval_id, kind, payload, turn_id, created_at, expires_at "
                "FROM pending_approvals WHERE status = 'interrupted' "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        except Exception:  # noqa: BLE001 - 读不到就不报告，不猜
            logger.warning("failed to read interrupted approvals", exc_info=True)
            return []
        out: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append(
                {
                    "approval_id": row["approval_id"],
                    "kind": row["kind"],
                    "what": str(payload.get("description") or ""),
                    "turn_id": row["turn_id"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "outcome": "not_executed",
                }
            )
        return out

    # -- introspection ----------------------------------------------------

    def pending(self) -> list[dict]:
        """仍然有效、仍在等待用户决定的审批（重连 / RESYNC 时用它恢复界面）。

        只返回「有人真的在等」的请求：已应答、已超时的不算。
        过期的顺手清掉，避免界面恢复出一个已经不存在的授权。
        """
        now = _now().isoformat()
        out: list[dict] = []
        for approval_id, request in list(self._requests.items()):
            future = self._waiters.get(approval_id)
            if future is None or future.done():
                self._requests.pop(approval_id, None)
                continue
            if request.expires_at and request.expires_at < now:
                self._waiters.pop(approval_id, None)
                self._requests.pop(approval_id, None)
                continue
            out.append(request.as_payload())
        out.sort(key=lambda item: str(item.get("created_at") or ""))
        return out

    def set_context(self, *, turn_id: str | None = None, session_id: str | None = None) -> None:
        """当前上下文（本轮 turn / 本会话）：新审批自动绑定到它。

        由 turn 运行时在开始一轮时设置，这样审批不需要每个工具各自传参。
        以前只有 `turn_id` 被真正传进来，`session_id` 从头到尾是 NULL ——
        `respond()` 里那条会话比对因此一次也没有生效过。现在 AppContext 给出
        `session_id`（本进程一个），turn 运行时把它一起设进来。

        **这是绑定校验，不是访问控制。** 客户端把审批里带回来的 session_id 原样回传，
        不一致就拒答（防止答错到别的审批/别的会话）。真正的门在 HTTP 层：
        `api/server.py` 的 session_guard 用会话令牌认证每个请求（settings.session_token）。
        也不要把它当成「跨重启屏障」：落库的 interrupted 审批是**有意**可以在新会话里被
        处理的（客户端回传的仍是它原来那个 session_id），这是既有产品行为。
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
        payload = self._with_narrative_explanation(payload)
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
        self._remember(request)
        await self.bus.publish(
            make_event(EventType.APPROVAL_REQUIRED, {"approval": request.as_payload()})
        )
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            # 过期也是审批的**结局**：必须让客户端知道，
            # 否则界面会一直显示一个已经不可能被批准的 Allow / Reject。
            await self.bus.publish(
                make_event(
                    EventType.APPROVAL_RESULT,
                    {
                        "approval_id": approval_id,
                        "decision": "timeout",
                        "reason": "expired",
                        "turn_id": request.turn_id,
                    },
                )
            )
            self._settle(approval_id, "timeout")
            return ApprovalResult(approval_id, "timeout")
        except asyncio.CancelledError:
            # 等待审批的那一轮被取消（用户按了 Stop）：审批也随之结束。
            # 不通知的话，服务端已经不认它了，界面还留着可点的 Allow / Reject。
            try:
                await self.bus.publish(
                    make_event(
                        EventType.APPROVAL_RESULT,
                        {
                            "approval_id": approval_id,
                            "decision": "cancelled",
                            "reason": "turn_cancelled",
                            "turn_id": request.turn_id,
                        },
                    )
                )
            except Exception:  # noqa: BLE001 - 取消路径上的通知是尽力而为
                pass
            self._settle(approval_id, "cancelled")
            raise
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
        self._settle(approval_id, decision)
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

    # -- model-authored explanation ---------------------------------------

    @staticmethod
    def _with_narrative_explanation(payload: dict) -> dict:
        """把模型写的 explanation 合并进审批载荷。

        只补一个键，并且只在载荷自己没有 explanation 时补：

        * 系统生成的事实字段（description / access / capabilities / scope / detail /
          arguments …）一个都不动，模型也无法通过载荷提交它们；
        * 工具创建流程自带的说明（tool_create 的提案 explanation）优先，不被覆盖；
        * 拿不到模型说明时保持原样 —— 审批照常发起与应答。
        """
        out = dict(payload or {})
        if str(out.get("explanation") or "").strip():
            return out
        from agent.tools.registry import current_narrative

        narrative = current_narrative()
        if narrative is not None and narrative.explanation:
            out["explanation"] = narrative.explanation
        return out
