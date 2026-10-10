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
from agent.storage.db import transaction
from agent.storage.instance_registry import RECORD_APPROVAL

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
    decision: str  # approved / rejected / timeout / cancelled
    scope: dict | None = None
    overrides: dict | None = None


class ApprovalService:
    def __init__(
        self,
        bus,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        conn=None,
        registry=None,
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
        # 可选的实例归属表（见 storage/instance_registry.py，契约 C1）：有它才按
        # 「归属者是否确认已退出」决定要不要把 pending 标成 interrupted。
        # 没有它时保持旧行为（库只有一个写入者的路径不变）。
        self.registry = registry
        self._instance_id = getattr(registry, "instance_id", None)
        self._mark_interrupted()
        # 由本方法排进事件循环的发布任务：持有引用，避免被 GC 提前回收。
        self._pending_publishes: set[asyncio.Task] = set()

    # -- 按任务作废（放弃开发）---------------------------------------------

    def invalidate_for_task(self, task_id: str, *, reason: str = "task_abandoned") -> int:
        """作废所有仍指向这个开发任务的未决审批，返回作废条数。

        为什么需要它：用户在等确认的时候点了「放弃开发」。如果只把任务标成放弃，
        那条审批还挂在界面上等人点「允许」；一旦被批准，迟到的执行就会拿到授权
        —— 任务已经放弃了，代码却还在跑。所以放弃路径必须先作废审批。

        每一条的结局是**明确的**（不是静默丢弃、也不抛异常给等待方）：

        * 等待方收到 `ApprovalResult(..., "cancelled")`，据此返回「这次没有执行」；
        * 从 `_waiters` / `_requests` 摘掉，`_settle` 落库为 cancelled；
        * 发一条 `APPROVAL_RESULT` 事件（载荷带 reason），界面上的确认卡自己消失。

        单次使用语义不变：作废过的审批再 `respond()` 必然返回 False。
        """
        victims = [
            approval_id
            for approval_id, request in list(self._requests.items())
            if self._refers_to(request.payload, task_id)
        ]
        invalidated = 0
        for approval_id in victims:
            future = self._waiters.get(approval_id)
            request = self._requests.get(approval_id)
            if request is None:
                continue
            if future is None or future.done():
                # 已经没人等了（超时/已应答/被取消）：不算作废，也不去动它。
                self._requests.pop(approval_id, None)
                continue
            future.set_result(ApprovalResult(approval_id, "cancelled"))
            self._waiters.pop(approval_id, None)
            self._requests.pop(approval_id, None)
            self._settle(approval_id, "cancelled")
            self._publish_result(
                {
                    "approval_id": approval_id,
                    "decision": "cancelled",
                    "reason": reason,
                    "turn_id": request.turn_id,
                }
            )
            invalidated += 1
        return invalidated

    @staticmethod
    def _refers_to(payload: object, task_id: str) -> bool:
        """这条审批的载荷是不是指向这个开发任务（三种既有写法都要认）。

        测试执行授权写 `workspace` + `code_boundary.task_id`；注册审批写
        `workspace`（见 tools/lifecycle.py）；别的调用方可能只写 `task_id`。
        """
        if not isinstance(payload, dict):
            return False
        if payload.get("workspace") == task_id:
            return True
        if payload.get("task_id") == task_id:
            return True
        boundary = payload.get("code_boundary")
        return isinstance(boundary, dict) and boundary.get("task_id") == task_id

    def _publish_result(self, data: dict) -> None:
        """发布一条 APPROVAL_RESULT。

        本方法由同步方法调用（契约签名是 `def invalidate_for_task`），所以在有
        事件循环时把它排进循环；没有循环（纯同步调用）就只做状态收口 —— 没有
        界面在等，事件无处可发，也不该因此让作废失败。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            task = loop.create_task(
                self.bus.publish(make_event(EventType.APPROVAL_RESULT, data))
            )
        except RuntimeError:
            # 事件循环正在关闭：状态已经收口，通知发不出去就算了，
            # 绝不能让「作废」因为通知失败而失败。
            return
        self._pending_publishes.add(task)
        task.add_done_callback(self._pending_publishes.discard)

    # -- 跨重启的等待记录 --------------------------------------------------

    def _mark_interrupted(self) -> None:
        """启动时处理上一个进程留下的 pending —— **按实例归属判定**（契约 C1）。

        没有归属表时的旧行为：库里还写着 pending 的，全是上一个进程没来得及
        回答的（新进程刚开始不可能有自己的等待项）→ 一律标 interrupted。

        有归属表后不能这么粗糙：第二个实例启动时，第一个实例可能**正在**
        等用户点「允许」。所以逐条按归属判定：

        * 归属者确认已退出 → 标 interrupted（理由仍是「那次操作没有执行」）；
        * 归属者还活着 → 一行都不动（那是别人正在等的审批）；
        * 归属判不出来（unknown，或旧记录没有归属）→ 保守保留 pending + 计数，
          等后续维护重判。绝不因为「判不出来」就把它标成中断。
        """
        if self.conn is None:
            return
        if self.registry is None:
            self._set_interrupted_unowned()
            return
        try:
            rows = self.conn.execute(
                "SELECT approval_id, owner_instance_id FROM pending_approvals "
                "WHERE status = 'pending'"
            ).fetchall()
        except Exception:  # noqa: BLE001 - 记录失败不能挡住启动
            logger.warning("failed to read pending approvals", exc_info=True)
            return
        victims: list[str] = []
        deferred = 0
        for row in rows:
            owner = self._owner_of(row)
            state = self.registry.owner_alive(owner) if owner else None
            if state is True:
                continue
            if state is None:
                # 归属未知（含旧记录）：保守保留原状态。
                deferred += 1
                continue
            victims.append(str(row["approval_id"]))
        try:
            with transaction(self.conn):
                for approval_id in victims:
                    self.conn.execute(
                        "UPDATE pending_approvals SET status = 'interrupted', resolved_at = ? "
                        "WHERE approval_id = ? AND status = 'pending'",
                        (_now().isoformat(), approval_id),
                    )
        except Exception:  # noqa: BLE001 - 记录失败不能挡住启动
            logger.warning("failed to mark interrupted approvals", exc_info=True)
            return
        if deferred:
            logger.info(
                "pending approvals: %s 条因归属未知而保留 pending（不改状态）", deferred
            )

    def _owner_of(self, row) -> str | None:
        """这条审批的归属者：优先归属表，退回行上的归属列（老行没有 → None）。"""
        owner = None
        try:
            owner = self.registry.owner_instance_id(RECORD_APPROVAL, str(row["approval_id"]))
        except Exception:  # noqa: BLE001 - 读不到归属表就退回列值
            owner = None
        if owner:
            return str(owner)
        try:
            column = row["owner_instance_id"]
        except (IndexError, KeyError):
            return None
        return str(column) if column else None

    def _set_interrupted_unowned(self) -> None:
        """旧路径（没有实例归属表）：上一个进程留下的 pending 一律标 interrupted。"""
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE pending_approvals SET status = 'interrupted', resolved_at = ? "
                    "WHERE status = 'pending'",
                    (_now().isoformat(),),
                )
        except Exception:  # noqa: BLE001 - 记录失败不能挡住启动
            logger.warning("failed to mark interrupted approvals", exc_info=True)

    def _remember(self, request: ApprovalRequest) -> None:
        if self.conn is None:
            return
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO pending_approvals "
                    "(approval_id, kind, payload, turn_id, session_id, created_at, expires_at, "
                    " status, owner_instance_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        request.approval_id,
                        request.kind,
                        json.dumps(request.payload or {}, ensure_ascii=False),
                        request.turn_id,
                        request.session_id,
                        request.created_at,
                        request.expires_at,
                        self._instance_id,
                    ),
                )
            if self.registry is not None and self._instance_id:
                self.registry.claim(RECORD_APPROVAL, request.approval_id, self._instance_id)
        except Exception:  # noqa: BLE001 - 落库失败不能挡住审批本身
            logger.warning("failed to persist pending approval", exc_info=True)

    def _settle(self, approval_id: str, status: str) -> None:
        """收口一条等待记录（单次使用：只有仍是 pending 的才会被改）。"""
        if self.conn is None:
            return
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE pending_approvals SET status = ?, resolved_at = ? "
                    "WHERE approval_id = ? AND status = 'pending'",
                    (status, _now().isoformat(), approval_id),
                )
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
