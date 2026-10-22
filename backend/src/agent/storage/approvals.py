"""审批持久化 v2（契约 §5，迁移 53 已冻结列/表）。

权威状态机（state 列）：

    waiting -> approved -> executing -> completed | failed
    waiting -> rejected | cancelled | expired
    executing -> interrupted_unverified
    approved -> invalidated（批准后重核对失败）

旧 status 列保留兼容（pending/interrupted/timeout 等），读取旧行时 state 为 NULL
则按旧 status 映射；新写入只写 state。默认有效期 600 秒，从创建时算起，刷新、暂缓、
重启都不续期。

本模块只负责持久化与状态迁移（原子 CAS），不执行操作、不发事件 —— 那些在
tools/approval.py 与执行器注册表里完成。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent.security.operations import ExecResult, PendingOperation
from agent.storage.db import transaction

DEFAULT_TIMEOUT_SECONDS = 600.0
TIME_LIMIT_SECONDS = 600

#: 仍处于活动状态（会出现在待审批列表 / 恢复流程里）。
ACTIVE_STATES = frozenset({"waiting", "approved", "executing"})
TERMINAL_STATES = frozenset(
    {"completed", "failed", "rejected", "cancelled", "expired", "interrupted_unverified", "invalidated"}
)

#: 旧 status -> 新 state（读取旧行时映射）。
LEGACY_STATUS_TO_STATE = {
    "pending": "waiting",
    "interrupted": "interrupted_unverified",
    "timeout": "expired",
    "approved": "approved",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "completed": "completed",
    "failed": "failed",
}


class ApprovalError(Exception):
    """审批状态机错误基类（service/API 据此映射 HTTP 状态）。"""

    code = "approval_error"
    status_code = 400


class ApprovalNotFound(ApprovalError):
    code = "approval_not_found"
    status_code = 404


class ApprovalConflict(ApprovalError):
    code = "approval_conflict"
    status_code = 409


class ApprovalConfirmRequired(ApprovalError):
    code = "approval_confirm_required"
    status_code = 409


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 极端结构
        return "{}"


def _loads(text: Any, fallback: Any) -> Any:
    if text in (None, ""):
        return fallback
    try:
        value = json.loads(str(text))
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


class ApprovalStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        instance_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.instance_id = instance_id
        self._clock = clock or _now

    def now(self) -> datetime:
        return self._clock()

    # -- 行读取与形状 -------------------------------------------------------

    @staticmethod
    def state_of(row: sqlite3.Row | dict | None) -> str:
        if row is None:
            return ""
        data = dict(row)
        state = str(data.get("state") or "").strip()
        if state:
            return state
        return LEGACY_STATUS_TO_STATE.get(str(data.get("status") or ""), "waiting")

    @classmethod
    def is_v2(cls, row: sqlite3.Row | dict | None) -> bool:
        """v2 行：有权威 state，且有可恢复 operation（旧 request() 行两者都没有）。"""
        if row is None:
            return False
        data = dict(row)
        if not str(data.get("state") or "").strip():
            return False
        operation = _loads(data.get("operation"), {})
        return bool(isinstance(operation, dict) and operation.get("kind"))

    def row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        operation_raw = _loads(data.get("operation"), {})
        operation = PendingOperation.from_dict(operation_raw) if operation_raw else PendingOperation(
            kind=str(data.get("kind") or ""), label=""
        )
        payload = _loads(data.get("payload"), {})
        fingerprint = _loads(data.get("fingerprint"), {})
        result = _loads(data.get("result"), {})
        state = self.state_of(data)
        return {
            "approval_id": str(data.get("approval_id") or ""),
            "kind": str(data.get("kind") or operation.kind),
            "label": operation.label or str(payload.get("description") or ""),
            "purpose": str(data.get("purpose") or operation.purpose or ""),
            "instruction_id": data.get("instruction_id"),
            "instruction_text": data.get("instruction_text"),
            "state": state,
            "status": data.get("status"),
            "turn_id": data.get("turn_id"),
            "session_id": data.get("session_id"),
            "created_at": data.get("created_at"),
            "expires_at": data.get("expires_at"),
            "resolved_at": data.get("resolved_at"),
            "confirmed_stage": int(data.get("confirmed_stage") or 0),
            "requires_confirmations": int(
                operation.confirmations_required or 1
            ),
            "revisions": int(data.get("revisions") or 0),
            "attempt": int(data.get("attempt") or 0),
            "claimed_at": data.get("claimed_at"),
            "mode_at_request": data.get("mode_at_request") or operation.mode_at_request,
            "mode_at_approval": data.get("mode_at_approval"),
            "source": operation.source or str(payload.get("source") or "tool"),
            "owner_instance_id": data.get("owner_instance_id"),
            "resumed_at": data.get("resumed_at"),
            "result": result,
            "error": data.get("error"),
            "operation": operation.summary(),
            "operation_detail": operation.as_dict(),
            "payload": payload if isinstance(payload, dict) else {},
            "fingerprint": fingerprint,
            "state_machine": {
                "confirmed_stage": int(data.get("confirmed_stage") or 0),
                "requires_confirmations": int(operation.confirmations_required or 1),
            },
        }

    # -- 创建 ---------------------------------------------------------------

    def create(
        self,
        operation: PendingOperation,
        *,
        approval_id: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        turn_id: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        moment = self.now()
        created_at = _iso(moment)
        expires_at = _iso(moment + timedelta(seconds=float(timeout_seconds)))
        identifier = approval_id or ("appr_" + uuid.uuid4().hex[:12])
        payload = {
            "description": operation.label,
            "kind": operation.kind,
            "purpose": operation.purpose,
            "source": operation.source,
            "summary": operation.summary(),
        }
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO pending_approvals ("
                " approval_id, kind, payload, turn_id, session_id, created_at, expires_at,"
                " status, resolved_at, owner_instance_id, state, operation, instruction_id,"
                " instruction_text, purpose, fingerprint, mode_at_request, mode_at_approval,"
                " confirmed_stage, revisions, attempt, claimed_at, result, error, resumed_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    operation.kind,
                    _json(payload),
                    turn_id if turn_id is not None else operation.turn_id,
                    session_id if session_id is not None else operation.session_id,
                    created_at,
                    expires_at,
                    "pending",
                    None,
                    self.instance_id,
                    "waiting",
                    _json(operation.as_dict()),
                    operation.instruction_id,
                    operation.instruction_text,
                    operation.purpose,
                    _json(operation.fingerprint),
                    operation.mode_at_request,
                    None,
                    0,
                    0,
                    0,
                    None,
                    None,
                    None,
                    None,
                ),
            )
            self._audit(identifier, "created", {"kind": operation.kind, "label": operation.label})
        row = self.conn.execute(
            "SELECT * FROM pending_approvals WHERE approval_id = ?", (identifier,)
        ).fetchone()
        return self.row_to_dict(row) or {}

    # -- 读取 ---------------------------------------------------------------

    def get(self, approval_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending_approvals WHERE approval_id = ?", (str(approval_id),)
        ).fetchone()
        return self.row_to_dict(row)

    def get_raw(self, approval_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM pending_approvals WHERE approval_id = ?", (str(approval_id),)
        ).fetchone()

    def list_pending(self, *, limit: int = 100) -> list[dict]:
        """等待中/已批准待执行/执行中（界面恢复用；过期项由 expire_waiting 收口）。"""
        now = _iso(self.now())
        rows = self.conn.execute(
            "SELECT * FROM pending_approvals WHERE state IN ('waiting','approved','executing')"
            " AND (expires_at IS NULL OR expires_at >= ?)"
            " ORDER BY created_at ASC LIMIT ?",
            (now, max(1, int(limit))),
        ).fetchall()
        return [item for item in (self.row_to_dict(row) for row in rows) if item]

    def count_by_state(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS c FROM pending_approvals GROUP BY state"
        ).fetchall()
        return {str(row["state"] or ""): int(row["c"]) for row in rows}

    def history(self, *, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM approval_history ORDER BY at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            data = dict(row)
            out.append(
                {
                    "approval_id": data.get("approval_id"),
                    "kind": data.get("kind"),
                    "summary": data.get("summary"),
                    "outcome": data.get("outcome"),
                    "detail": _loads(data.get("detail"), {}),
                    "at": data.get("at"),
                }
            )
        return out

    def interrupted(self, *, limit: int = 20) -> list[dict]:
        """重启后仍未核对/未执行完的审批事实（不是待办）。"""
        rows = self.conn.execute(
            "SELECT * FROM pending_approvals"
            " WHERE status = 'interrupted' OR state = 'interrupted_unverified'"
            " ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            item = self.row_to_dict(row)
            if item is None:
                continue
            item["what"] = str(item.get("label") or item["payload"].get("description") or "")
            item["outcome"] = "not_executed"
            out.append(item)
        return out

    def audit_trail(self, approval_id: str, *, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM approval_audit WHERE approval_id = ? ORDER BY seq ASC LIMIT ?",
            (str(approval_id), max(1, int(limit))),
        ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "action": row["action"],
                "detail": _loads(row["detail"], {}),
                "at": row["at"],
            }
            for row in rows
        ]

    # -- 决定 / 确认（CAS） ------------------------------------------------

    def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        expected_revision: int | None = None,
        mode_at_approval: str | None = None,
        actor: str = "user",
    ) -> dict:
        row = self.get_raw(approval_id)
        if row is None:
            raise ApprovalNotFound("没有找到这条审批（可能已被处理或已过期）")
        state = self.state_of(row)
        normalized = str(decision or "").strip().lower()
        if normalized in ("approve", "approved"):
            normalized = "approved"
        elif normalized in ("reject", "rejected"):
            normalized = "rejected"
        elif normalized in ("cancel", "cancelled", "canceled"):
            normalized = "cancelled"
        if normalized not in ("approved", "rejected", "cancelled"):
            raise ApprovalError("无效的决定：" + str(decision))
        if expected_revision is not None and int(row["revisions"] or 0) != int(expected_revision):
            raise ApprovalConflict("这条审批已经被更新过（revision 不匹配），请刷新后再决定")
        if normalized in ("rejected", "cancelled"):
            if state not in ("waiting", "approved"):
                raise ApprovalConflict("这条审批当前状态是「" + state + "」，不能" + normalized)
            target = "rejected" if normalized == "rejected" else "cancelled"
            status = "rejected" if normalized == "rejected" else "cancelled"
            self._transition(
                approval_id,
                from_states=("waiting", "approved"),
                expected_revision=None,
                sets={
                    "state": target,
                    "status": status,
                    "resolved_at": _iso(self.now()),
                    "revisions": int(row["revisions"] or 0) + 1,
                },
                action=target,
                actor=actor,
            )
            return self.get(approval_id) or {}
        # approve
        if state != "waiting":
            raise ApprovalConflict("这条审批当前状态是「" + state + "」，不能重复批准")
        operation = _loads(row["operation"], {})
        required = int(operation.get("confirmations_required") or 1)
        confirmed = int(row["confirmed_stage"] or 0)
        if required > 1 and confirmed < required:
            raise ApprovalConfirmRequired(
                "这是内部运行数据：需要连续 " + str(required) + " 次确认（当前 " + str(confirmed) + "/" + str(required) + "）"
            )
        self._transition(
            approval_id,
            from_states=("waiting",),
            expected_revision=None,
            sets={
                "state": "approved",
                "status": "approved",
                "mode_at_approval": mode_at_approval,
                "revisions": int(row["revisions"] or 0) + 1,
            },
            action="approved",
            actor=actor,
        )
        return self.get(approval_id) or {}

    def confirm(
        self,
        approval_id: str,
        stage: int,
        *,
        expected_revision: int | None = None,
        mode_at_approval: str | None = None,
        actor: str = "user",
    ) -> dict:
        row = self.get_raw(approval_id)
        if row is None:
            raise ApprovalNotFound("没有找到这条审批（可能已被处理或已过期）")
        state = self.state_of(row)
        if state != "waiting":
            raise ApprovalConflict("这条审批当前状态是「" + state + "」，不能继续确认")
        operation = _loads(row["operation"], {})
        required = int(operation.get("confirmations_required") or 1)
        if required <= 1:
            raise ApprovalConflict("这条审批不需要分阶段确认，请使用「允许/拒绝」")
        if expected_revision is not None and int(row["revisions"] or 0) != int(expected_revision):
            raise ApprovalConflict("这条审批已经被更新过（revision 不匹配），请刷新后再确认")
        current = int(row["confirmed_stage"] or 0)
        expected_stage = current + 1
        if int(stage) != expected_stage:
            raise ApprovalConflict(
                "确认阶段不连续：应为第 " + str(expected_stage) + " 次确认（当前 " + str(current) + "/" + str(required) + "）"
            )
        if int(row["revisions"] or 0) != int(row["revisions"] or 0):  # pragma: no cover
            raise ApprovalConflict("并发冲突")
        sets: dict[str, Any] = {
            "confirmed_stage": int(stage),
            "revisions": int(row["revisions"] or 0) + 1,
        }
        if int(stage) >= required:
            sets["state"] = "approved"
            sets["status"] = "approved"
            sets["mode_at_approval"] = mode_at_approval
        self._transition(
            approval_id,
            from_states=("waiting",),
            expected_revision=None,
            sets=sets,
            action="confirmed" if int(stage) < required else "confirm_completed",
            actor=actor,
            detail={"stage": int(stage), "required": required},
        )
        return self.get(approval_id) or {}

    # -- 执行领取 / 收口 ----------------------------------------------------

    def claim(self, approval_id: str) -> dict | None:
        """原子领取：只有 state='approved' 的那一行会被改成 executing（幂等防重）。"""
        moment = _iso(self.now())
        cur = self.conn.execute(
            "UPDATE pending_approvals SET state = 'executing', status = 'approved',"
            " attempt = attempt + 1, claimed_at = ?, revisions = revisions + 1"
            " WHERE approval_id = ? AND state = 'approved'",
            (moment, str(approval_id)),
        )
        if int(cur.rowcount or 0) != 1:
            self._audit(approval_id, "claim_rejected", {"reason": "not_approved_or_taken"})
            return None
        self._audit(approval_id, "claimed", {})
        return self.get(approval_id)

    def finish(self, approval_id: str, result: ExecResult) -> dict:
        state = result.state if result.state in ("completed", "failed", "interrupted_unverified") else (
            "completed" if result.ok else "failed"
        )
        status = {
            "completed": "completed",
            "failed": "failed",
            "interrupted_unverified": "interrupted",
        }[state]
        outcome = {
            "completed": "executed",
            "failed": "failed",
            "interrupted_unverified": "interrupted_unverified",
        }[state]
        detail = dict(result.detail or {})
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE pending_approvals SET state = ?, status = ?, result = ?, error = ?,"
                " resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ?",
                (
                    state,
                    status,
                    _json(detail),
                    result.error,
                    _iso(self.now()),
                    str(approval_id),
                ),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO approval_history"
                " (approval_id, kind, summary, outcome, detail, at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    str(approval_id),
                    str(detail.get("kind") or ""),
                    str(result.summary or ""),
                    outcome,
                    _json(detail),
                    _iso(self.now()),
                ),
            )
            self._audit(approval_id, outcome, {"summary": result.summary, "ok": bool(result.ok)})
        return self.get(approval_id) or {}

    def mark_invalidated(self, approval_id: str, reason: str) -> dict:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE pending_approvals SET state = 'invalidated', status = 'invalidated',"
                " error = ?, resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND state IN ('approved','waiting')",
                (str(reason or ""), _iso(self.now()), str(approval_id)),
            )
            self._audit(approval_id, "invalidated", {"reason": str(reason or "")[:300]})
        return self.get(approval_id) or {}

    def mark_expired(self, approval_id: str) -> dict:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE pending_approvals SET state = 'expired', status = 'timeout',"
                " resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND state IN ('waiting','approved')",
                (_iso(self.now()), str(approval_id)),
            )
            self._audit(approval_id, "expired", {})
        return self.get(approval_id) or {}

    def mark_interrupted_unverified(self, approval_id: str, reason: str) -> dict:
        """执行中崩溃：不自动重放，界面给「待核对」入口。"""
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE pending_approvals SET state = 'interrupted_unverified',"
                " status = 'interrupted', error = ?, resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND state = 'executing'",
                (str(reason or ""), _iso(self.now()), str(approval_id)),
            )
            self._audit(approval_id, "interrupted_unverified", {"reason": str(reason or "")[:300]})
        return self.get(approval_id) or {}

    # -- 过期 / 启动恢复 ----------------------------------------------------

    def expire_waiting(self) -> list[dict]:
        now = _iso(self.now())
        rows = self.conn.execute(
            "SELECT approval_id FROM pending_approvals"
            " WHERE state = 'waiting' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ).fetchall()
        expired: list[dict] = []
        for row in rows:
            expired.append(self.mark_expired(str(row["approval_id"])))
        return expired

    def recover(self, *, owner_alive: Callable[[str | None], bool | None] | None = None) -> dict:
        """启动恢复（契约 §5.1）。

        * waiting 且未过期 -> 恢复待审批；owner 已死/无归属 -> 本实例接管；
        * waiting 已过期 -> expired；
        * approved（已决定未执行）-> 交给调用方重核对后执行；
        * executing（可能已部分执行）-> interrupted_unverified，不自动重放；
        * 旧 request() 行（没有 state/operation）-> 按旧行为标 interrupted（那次操作没有执行）。
        """
        report: dict[str, list[str]] = {
            "waiting": [],
            "taken_over": [],
            "expired": [],
            "approved": [],
            "interrupted": [],
            "legacy_interrupted": [],
            "left_alone": [],
        }
        rows = self.conn.execute(
            "SELECT * FROM pending_approvals WHERE"
            " status IN ('pending','interrupted') OR state IN ('waiting','approved','executing')"
        ).fetchall()
        now_dt = self.now()
        for row in rows:
            approval_id = str(row["approval_id"])
            if not self.is_v2(row):
                # 旧 request() 的内存等待随进程消失：如实记录「那次没有执行」。
                self.conn.execute(
                    "UPDATE pending_approvals SET status = 'interrupted', state = ?,"
                    " resolved_at = ? WHERE approval_id = ? AND status = 'pending'",
                    ("interrupted_unverified", _iso(now_dt), approval_id),
                )
                self._audit(approval_id, "legacy_interrupted", {"reason": "process_restarted"})
                report["legacy_interrupted"].append(approval_id)
                continue
            state = self.state_of(row)
            expires = _parse_iso(row["expires_at"])
            expired = expires is not None and expires < now_dt
            if state == "waiting":
                if expired:
                    self.mark_expired(approval_id)
                    report["expired"].append(approval_id)
                    continue
                owner = str(row["owner_instance_id"] or "") or None
                alive = owner_alive(owner) if owner_alive is not None else None
                if owner and owner != self.instance_id and alive is True:
                    report["left_alone"].append(approval_id)
                    continue
                self.conn.execute(
                    "UPDATE pending_approvals SET owner_instance_id = ?, resumed_at = ?"
                    " WHERE approval_id = ? AND state = 'waiting'",
                    (self.instance_id, _iso(now_dt), approval_id),
                )
                self._audit(approval_id, "resumed_waiting", {"owner": owner})
                if owner and owner != self.instance_id:
                    report["taken_over"].append(approval_id)
                else:
                    report["waiting"].append(approval_id)
            elif state == "approved":
                if expired:
                    self.mark_invalidated(approval_id, "已超过 600 秒有效期，这次批准不再执行")
                    report["expired"].append(approval_id)
                else:
                    report["approved"].append(approval_id)
            elif state == "executing":
                self.mark_interrupted_unverified(
                    approval_id, "上次进程在执行中退出，可能已部分执行；未自动重放，请先核对结果"
                )
                report["interrupted"].append(approval_id)
        return report

    # -- 内部：CAS 迁移 + 审计 ---------------------------------------------

    def _transition(
        self,
        approval_id: str,
        *,
        from_states: tuple[str, ...],
        expected_revision: int | None,
        sets: dict[str, Any],
        action: str,
        actor: str,
        detail: dict | None = None,
    ) -> None:
        allowed = set(from_states)
        if not set(from_states).issubset({"waiting", "approved", "executing"}):
            raise ValueError("invalid from_states")
        assigned = ", ".join(key + " = ?" for key in sets)
        params: list[Any] = list(sets.values())
        placeholders = ",".join("?" for _ in from_states)
        sql = (
            "UPDATE pending_approvals SET " + assigned + " WHERE approval_id = ?"
            " AND state IN (" + placeholders + ")"
        )
        params.append(str(approval_id))
        params.extend(from_states)
        current = self.get_raw(approval_id)
        if current is None:
            raise ApprovalNotFound("没有找到这条审批")
        if self.state_of(current) not in allowed:
            raise ApprovalConflict("这条审批已经被处理过")
        cur = self.conn.execute(sql, params)
        if int(cur.rowcount or 0) != 1:
            raise ApprovalConflict("并发冲突：这条审批已被其它请求处理，请刷新")
        payload = {"actor": actor}
        payload.update(detail or {})
        self._audit(approval_id, action, payload)

    def _audit(self, approval_id: str, action: str, detail: dict | None = None) -> None:
        try:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM approval_audit WHERE approval_id = ?",
                (str(approval_id),),
            ).fetchone()
            seq = int(row["m"] if row is not None else 0) + 1
            self.conn.execute(
                "INSERT INTO approval_audit (approval_id, seq, action, detail, at)"
                " VALUES (?,?,?,?,?)",
                (str(approval_id), seq, str(action), _json(detail or {}), _iso(self.now())),
            )
        except sqlite3.Error:  # pragma: no cover - 审计失败不能挡住状态迁移
            pass
