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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from agent.security.operations import ExecResult, PendingOperation, operation_digest
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


class LegacyApprovalUseRespond(ApprovalError):
    """旧 request() 审批没有可恢复执行计划：只能用旧 /respond 处理。

    不能把它当成 v2 waiting 项让 /decide 处理 —— 那会把「没有执行计划」的
    历史记录伪装成一条可批准的授权（前端也会误判成生命周期审批）。
    """

    code = "legacy_approval_use_respond"
    status_code = 409


class LegacyApprovalNotExecutable(ApprovalError):
    """已恢复/已中断的旧审批：那次操作没有执行，也不会再执行。

    以前这里落到服务层的「没人等待 → False」再变成模糊 404，读起来像「找不到这条审批」；
    实际事实是明确的：进程重启时那次工具调用已经没了，授权不可能再生效。
    """

    code = "legacy_approval_not_executable"
    status_code = 409


class ApprovalsUnreadable(ApprovalError):
    """审批数据读取失败：**这不代表没有审批/历史**，可以重试。

    以前服务层把读取异常吞成空集合/部分集合、API 返回 200 —— 前端把失败的补读当成功，
    会清掉本地待办。现在读取失败必须显式失败（503），让界面保留已有数据并提示重试。
    """

    code = "approvals_unreadable"
    status_code = 503


class OverridesUnsupported(ApprovalError):
    """这条审批路径不支持 overrides（预算 / 授权时长等覆盖）。

    契约 §4.2：不得静默丢弃用户的修改 —— 明确拒绝，让界面显示真实原因，
    而不是表现成「已接受修改」。
    """

    code = "overrides_unsupported"
    status_code = 400


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


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """审批状态写入的事务边界（BEGIN IMMEDIATE：先拿写锁，再读-核对-写）。

    为什么不用 BEGIN 延迟事务：两个连接同时「读状态 → 判断 → 写」时，延迟事务会
    在升级写锁时才冲突（busy/或读到旧值后覆盖）。这里一开始就拿写锁，把
    「核对前一状态 + CAS 更新 + 审计」放在同一个串行区间里；真正的防丢更新仍靠
    每条 UPDATE 的 revision/confirmed_stage CAS 条件（旧请求不能覆盖新状态）。
    已在外层事务中时不嵌套 BEGIN（SQLite 不支持嵌套）。
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


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
        """权威状态：**只读 state 列**。

        旧 request() 行（state 为空、operation 为空）没有 v2 状态。以前这里用
        LEGACY_STATUS_TO_STATE 兜底成 "waiting"，于是恢复后的旧审批被读成一条
        可 /decide 的 waiting 项 —— 前端据此误调新接口。现在如实返回空串，
        旧行身份由 is_legacy / legacy 字段表达。
        """
        if row is None:
            return ""
        data = dict(row)
        return str(data.get("state") or "").strip()

    @staticmethod
    def _has_operation(row: sqlite3.Row | dict | None) -> bool:
        if row is None:
            return False
        data = dict(row)
        operation = _loads(data.get("operation"), {})
        return bool(isinstance(operation, dict) and operation.get("kind"))

    @classmethod
    def is_v2(cls, row: sqlite3.Row | dict | None) -> bool:
        """v2 行：有权威 state，且有可恢复 operation（旧 request() 行两者都没有）。"""
        if row is None:
            return False
        data = dict(row)
        if not str(data.get("state") or "").strip():
            return False
        return cls._has_operation(data)

    @classmethod
    def is_legacy(cls, row: sqlite3.Row | dict | None) -> bool:
        """旧 request() 行：没有可恢复的 operation（迁移 53 之前写下的行）。"""
        if row is None:
            return False
        return not cls._has_operation(row)

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
        legacy = not (isinstance(operation_raw, dict) and operation_raw.get("kind"))
        stored_state = self.state_of(data)
        return {
            "approval_id": str(data.get("approval_id") or ""),
            "kind": str(data.get("kind") or operation.kind),
            "label": operation.label or str(payload.get("description") or ""),
            "purpose": str(data.get("purpose") or operation.purpose or ""),
            "instruction_id": data.get("instruction_id"),
            "instruction_text": data.get("instruction_text"),
            # 旧 request() 行没有 v2 状态：state 明确为 null，绝不由旧 status 伪造 waiting。
            "state": None if legacy else stored_state,
            "legacy": legacy,
            # 历史/诊断事实（旧行可能被早期版本写过 v2 状态）：只读，不参与状态机。
            "stored_state": stored_state or None,
            # v2 生命周期接口不接受 overrides（只有部分旧路径端到端支持）。
            "overrides_supported": False,
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
            self._audit(
                identifier,
                "created",
                {"kind": operation.kind, "label": operation.label},
                strict=True,
            )
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
            " WHERE status IN ('interrupted','interrupted_legacy')"
            " OR state = 'interrupted_unverified'"
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

    def write_history(
        self,
        approval_id: str,
        *,
        summary: str,
        outcome: str,
        detail: dict | None = None,
    ) -> None:
        """公开的「只写可读历史」入口（旧审批结果闭环用；不触碰状态机）。

        旧 request() 路径没有 v2 state 可迁移，用户决定与真实执行结果是两条事实，
        都要能刷新后回看（approval_history PK approval_id，后写的真实终态覆盖中间态）。
        """
        self._write_history(
            approval_id, summary=summary, outcome=outcome, detail=detail
        )

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

    def _row_expired(self, row: Any) -> bool:
        expires = _parse_iso(row["expires_at"])
        return expires is not None and expires < self.now()

    def _operation_changed(self, row: Any) -> bool:
        """审批记录里的操作内容在提交后是否被改动（独立摘要比对）。"""
        raw = _loads(row["operation"], {})
        if not isinstance(raw, dict) or not raw:
            return False
        expected = str(
            (_loads(row["fingerprint"], {}) or {}).get("operation_digest") or ""
        )
        if not expected:
            return False
        try:
            actual = operation_digest(PendingOperation.from_dict(raw))
        except Exception:  # noqa: BLE001 - 解析不了就按"无法核对"处理
            return True
        return actual != expected

    def _ensure_waiting_binding(self, row: Any, approval_id: str) -> None:
        """等待中的审批：过期先标 expired；操作内容被改动则重置确认并拒绝。

        R9-H4 §10.3：重置确认必须按「读到的前一状态（revision/confirmed_stage）」
        做 CAS 并提交后才报错；并发中若别的请求已推进/撤销，本次不得把阶段回退，
        而是报冲突让调用方刷新。
        """
        if self.state_of(row) != "waiting":
            return
        if self._row_expired(row):
            self.mark_expired(approval_id)
            raise ApprovalConflict(
                "这条审批已超过原始有效期（不会重新获得 10 分钟），请重新提出"
            )
        if not self._operation_changed(row):
            return
        revision = int(row["revisions"] or 0)
        stage = int(row["confirmed_stage"] or 0)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.get_raw(approval_id)
            if current is None:
                self.conn.execute("ROLLBACK")
                raise ApprovalNotFound("没有找到这条审批（可能已被处理或已过期）")
            if (
                self.state_of(current) != "waiting"
                or int(current["revisions"] or 0) != revision
                or int(current["confirmed_stage"] or 0) != stage
            ):
                self.conn.execute("ROLLBACK")
                raise ApprovalConflict(
                    "这条审批已被其它请求更新（确认阶段或内容绑定已变化），请刷新后再确认"
                )
            cur = self.conn.execute(
                "UPDATE pending_approvals SET confirmed_stage = 0,"
                " revisions = revisions + 1"
                " WHERE approval_id = ? AND state = 'waiting' AND revisions = ?"
                " AND confirmed_stage = ?",
                (str(approval_id), revision, stage),
            )
            if int(cur.rowcount or 0) != 1:
                self.conn.execute("ROLLBACK")
                raise ApprovalConflict(
                    "并发冲突：确认重置未命中（这条审批已被其它请求更新），请刷新"
                )
            self._audit(
                approval_id,
                "binding_reset",
                {"reason": "operation_changed_after_submission"},
                strict=True,
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        raise ApprovalConflict(
            "审批单绑定的操作内容在提交后被改动；确认已重置，请重新确认"
        )

    @staticmethod
    def _stored_required_confirmations(row: Any) -> int:
        operation = _loads(row["operation"], {})
        try:
            return max(1, int(operation.get("confirmations_required") or 1))
        except (TypeError, ValueError):
            return 1

    def _correct_required_confirmations(
        self, approval_id: str, row: Any, derived: int | None
    ) -> int:
        """把审批单的确认次数纠正为**后端推导值**（只升不降），并重算操作摘要。

        历史/错误行可能把内部维护写成 1 次；这里不信任存储值，按真实影响对象推导
        出的最低次数校正，写入 audit（确认阶段进度保持不变，不能伪造已完成）。
        """
        stored = self._stored_required_confirmations(row)
        try:
            required = max(stored, int(derived or 1), 1)
        except (TypeError, ValueError):
            required = stored
        if required <= stored:
            return stored
        raw = _loads(row["operation"], {})
        if not isinstance(raw, dict) or not raw:
            return stored
        raw["confirmations_required"] = required
        try:
            candidate = PendingOperation.from_dict(raw)
            digest = operation_digest(candidate)
        except Exception:  # noqa: BLE001 - 无法重算摘要则不纠正（保守）
            return stored
        fingerprint = dict(_loads(row["fingerprint"], {}) or {})
        fingerprint["operation_digest"] = digest
        with _write_transaction(self.conn):
            current = self.get_raw(approval_id)
            if current is None:
                return stored
            cur = self.conn.execute(
                "UPDATE pending_approvals SET operation = ?, fingerprint = ?"
                " WHERE approval_id = ? AND state = 'waiting' AND revisions = ?"
                " AND confirmed_stage = ?",
                (
                    _json(raw),
                    _json(fingerprint),
                    str(approval_id),
                    int(row["revisions"] or 0),
                    int(row["confirmed_stage"] or 0),
                ),
            )
            if int(cur.rowcount or 0) != 1:
                raise ApprovalConflict(
                    "审批确认次数校正时状态已变化，请刷新后再操作"
                )
            self._audit(
                approval_id,
                "confirmations_corrected",
                {"from": stored, "to": required, "source": "backend_effect"},
                strict=True,
            )
        return required

    def _ensure_v2(self, row: Any, approval_id: str) -> None:
        """v2 生命周期接口（决定/确认）不接受旧 request() 行：明确指向 /respond。

        旧行没有可恢复的执行计划；把它当 waiting 处理只会得到一个 CAS 冲突，
        读起来像「并发/已处理」，而不是「这条属于旧路径，请用 respond」。
        """
        if not self.is_v2(row):
            raise LegacyApprovalUseRespond(
                "这条审批来自旧版路径（没有可恢复的执行计划），不能用新接口处理；"
                "请用 /api/approvals/" + str(approval_id) + "/respond 回答。"
            )

    def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        expected_revision: int | None = None,
        mode_at_approval: str | None = None,
        actor: str = "user",
        required_confirmations: int | None = None,
    ) -> dict:
        row = self.get_raw(approval_id)
        if row is None:
            raise ApprovalNotFound("没有找到这条审批（可能已被处理或已过期）")
        self._ensure_v2(row, approval_id)
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
                expected_revision=int(row["revisions"] or 0),
                sets={
                    "state": target,
                    "status": status,
                    "resolved_at": _iso(self.now()),
                    "revisions": int(row["revisions"] or 0) + 1,
                },
                action=target,
                actor=actor,
            )
            # F02：拒绝/取消是**终态**，用户刷新后仍要能回看（approval_history 每次审批
            # 一条最新结果，PK approval_id；不改变授权/执行语义，只补可读历史）。
            settled = self.row_to_dict(self.get_raw(approval_id)) or {}
            label = str(settled.get("label") or "")
            summary = (
                ("已拒绝：%s（不会执行）" if target == "rejected" else "已取消：%s（不会执行）")
                % (label or "这次操作")
            )
            self._write_history(
                approval_id,
                summary=summary,
                outcome=target,
                detail={"decision": target, "label": label},
            )
            return self.get(approval_id) or {}
        # approve
        if state != "waiting":
            raise ApprovalConflict("这条审批当前状态是「" + state + "」，不能重复批准")
        self._ensure_waiting_binding(row, approval_id)
        required = self._correct_required_confirmations(
            approval_id, row, required_confirmations
        )
        confirmed = int(row["confirmed_stage"] or 0)
        if required > 1 and confirmed < required:
            raise ApprovalConfirmRequired(
                "这是内部运行数据：需要连续 " + str(required) + " 次确认（当前 " + str(confirmed) + "/" + str(required) + "）"
            )
        self._transition(
            approval_id,
            from_states=("waiting",),
            expected_revision=int(row["revisions"] or 0),
            expected_confirmed_stage=confirmed,
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
        required_confirmations: int | None = None,
    ) -> dict:
        row = self.get_raw(approval_id)
        if row is None:
            raise ApprovalNotFound("没有找到这条审批（可能已被处理或已过期）")
        self._ensure_v2(row, approval_id)
        state = self.state_of(row)
        if state != "waiting":
            raise ApprovalConflict("这条审批当前状态是「" + state + "」，不能继续确认")
        self._ensure_waiting_binding(row, approval_id)
        row = self.get_raw(approval_id) or row
        # §8.4：确认次数由后端按真实影响对象校正（历史行可能只写了 1 次）。
        required = self._correct_required_confirmations(
            approval_id, row, required_confirmations
        )
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
            expected_revision=int(row["revisions"] or 0),
            expected_confirmed_stage=current,
            sets=sets,
            action="confirmed" if int(stage) < required else "confirm_completed",
            actor=actor,
            detail={"stage": int(stage), "required": required},
        )
        return self.get(approval_id) or {}

    # -- 执行领取 / 收口 ----------------------------------------------------

    def claim(self, approval_id: str) -> dict | None:
        """原子领取：只有 state='approved'、未过期、内容未变的那一行会被改成 executing。

        R9-H4 §10.3：读到的 revision/confirmed_stage 作为 CAS 条件；并发领取只有一个
        成功（另一个拿到 rowcount=0，不重复执行）。过期/内容变化在同一事务内先收口，
        不会「先标 expired 又被另一个连接领取」。
        """
        with _write_transaction(self.conn):
            row = self.get_raw(approval_id)
            if row is None:
                return None
            if self.state_of(row) != "approved":
                self._audit(
                    approval_id,
                    "claim_rejected",
                    {"reason": "not_approved_or_taken", "state": self.state_of(row)},
                )
                return None
            if self._row_expired(row):
                self.mark_expired(approval_id)
                return None
            if self._operation_changed(row):
                self.mark_invalidated(approval_id, "operation_changed_after_approval")
                return None
            revision = int(row["revisions"] or 0)
            stage = int(row["confirmed_stage"] or 0)
            moment = _iso(self.now())
            cur = self.conn.execute(
                "UPDATE pending_approvals SET state = 'executing', status = 'approved',"
                " attempt = attempt + 1, claimed_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND state = 'approved' AND revisions = ?"
                " AND confirmed_stage = ?"
                " AND (expires_at IS NULL OR expires_at >= ?)",
                (moment, str(approval_id), revision, stage, moment),
            )
            if int(cur.rowcount or 0) != 1:
                self._audit(
                    approval_id, "claim_rejected", {"reason": "not_approved_or_taken"}
                )
                return None
            self._audit(approval_id, "claimed", {"attempt": int(row["attempt"] or 0) + 1})
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
        with _write_transaction(self.conn):
            row = self.get_raw(approval_id)
            if row is None:
                return {}
            # F02/F03：历史 detail 带上 kind/label（前端「结果与历史」用来显示这是
            # 哪条审批、操作名是什么；只补缺失键，不改执行器给的逐项结果）。
            item = self.row_to_dict(row) or {}
            detail.setdefault("kind", str(item.get("kind") or ""))
            if item.get("label"):
                detail.setdefault("label", str(item.get("label") or ""))
            if self.state_of(row) != "executing":
                # 终态只能从 executing 收口一次；迟到的旧结果不得覆盖已完成/已作废的行。
                self._audit(
                    approval_id,
                    "finish_rejected",
                    {"reason": "not_executing", "state": self.state_of(row)},
                )
                return self.get(approval_id) or {}
            revision = int(row["revisions"] or 0)
            cur = self.conn.execute(
                "UPDATE pending_approvals SET state = ?, status = ?, result = ?, error = ?,"
                " resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND state = 'executing' AND revisions = ?",
                (
                    state,
                    status,
                    _json(detail),
                    result.error,
                    _iso(self.now()),
                    str(approval_id),
                    revision,
                ),
            )
            if int(cur.rowcount or 0) != 1:
                self._audit(
                    approval_id,
                    "finish_rejected",
                    {"reason": "revision_conflict", "expected_revision": revision},
                )
                return self.get(approval_id) or {}
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
            audit_detail: dict[str, Any] = {
                "summary": result.summary,
                "ok": bool(result.ok),
                "state": state,
            }
            basis = detail.get("authorization_basis")
            if basis:
                audit_detail["authorization_basis"] = basis
            self._audit(approval_id, outcome, audit_detail, strict=True)
        return self.get(approval_id) or {}

    def mark_invalidated(self, approval_id: str, reason: str) -> dict:
        with _write_transaction(self.conn):
            row = self.get_raw(approval_id)
            if row is None:
                return {}
            state = self.state_of(row)
            if state not in ("approved", "waiting", "executing"):
                # 终态/已作废：旧请求不得再覆盖（例如执行完成后迟到的 invalidated）。
                self._audit(
                    approval_id,
                    "invalidated_rejected",
                    {"reason": str(reason or "")[:300], "state": state},
                )
                return self.get(approval_id) or {}
            revision = int(row["revisions"] or 0)
            cur = self.conn.execute(
                "UPDATE pending_approvals SET state = 'invalidated', status = 'invalidated',"
                " error = ?, resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND revisions = ?"
                " AND state IN ('approved','waiting','executing')",
                (str(reason or ""), _iso(self.now()), str(approval_id), revision),
            )
            if int(cur.rowcount or 0) != 1:
                self._audit(
                    approval_id,
                    "invalidated_rejected",
                    {"reason": "revision_conflict", "expected_revision": revision},
                )
                return self.get(approval_id) or {}
            self._audit(
                approval_id,
                "invalidated",
                {"reason": str(reason or "")[:300]},
                strict=True,
            )
            self._write_history(
                approval_id,
                summary=str(reason or "批准后重核对失败，本次未执行"),
                outcome="invalidated",
                detail={"reason": str(reason or "")},
            )
        return self.get(approval_id) or {}

    def mark_expired(self, approval_id: str) -> dict:
        with _write_transaction(self.conn):
            row = self.get_raw(approval_id)
            if row is None:
                return {}
            state = self.state_of(row)
            if state not in ("waiting", "approved"):
                self._audit(
                    approval_id, "expired_rejected", {"state": state}
                )
                return self.get(approval_id) or {}
            revision = int(row["revisions"] or 0)
            cur = self.conn.execute(
                "UPDATE pending_approvals SET state = 'expired', status = 'timeout',"
                " resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND revisions = ? AND state IN ('waiting','approved')",
                (_iso(self.now()), str(approval_id), revision),
            )
            if int(cur.rowcount or 0) != 1:
                self._audit(
                    approval_id,
                    "expired_rejected",
                    {"reason": "revision_conflict", "expected_revision": revision},
                )
                return self.get(approval_id) or {}
            self._audit(approval_id, "expired", {}, strict=True)
            # F02：过期也是终态（刷新后仍可回看；不改期限/授权语义）
            item = self.row_to_dict(self.get_raw(approval_id)) or {}
            label = str(item.get("label") or "")
            self._write_history(
                approval_id,
                summary=("已过期：%s（本次没有执行）" % label)
                if label
                else "已过期，本次没有执行",
                outcome="expired",
                detail={"reason": "expired", "label": label},
            )
        return self.get(approval_id) or {}

    def mark_interrupted_unverified(self, approval_id: str, reason: str) -> dict:
        """执行中崩溃：不自动重放，界面给「待核对」入口。"""
        with _write_transaction(self.conn):
            row = self.get_raw(approval_id)
            if row is None:
                return {}
            if self.state_of(row) != "executing":
                self._audit(
                    approval_id,
                    "interrupted_rejected",
                    {"state": self.state_of(row)},
                )
                return self.get(approval_id) or {}
            revision = int(row["revisions"] or 0)
            cur = self.conn.execute(
                "UPDATE pending_approvals SET state = 'interrupted_unverified',"
                " status = 'interrupted', error = ?, resolved_at = ?, revisions = revisions + 1"
                " WHERE approval_id = ? AND state = 'executing' AND revisions = ?",
                (str(reason or ""), _iso(self.now()), str(approval_id), revision),
            )
            if int(cur.rowcount or 0) != 1:
                self._audit(
                    approval_id,
                    "interrupted_rejected",
                    {"reason": "revision_conflict", "expected_revision": revision},
                )
                return self.get(approval_id) or {}
            self._audit(
                approval_id,
                "interrupted_unverified",
                {"reason": str(reason or "")[:300]},
                strict=True,
            )
            self._write_history(
                approval_id,
                summary=str(reason or "上次进程在执行中退出，未自动重放"),
                outcome="interrupted_unverified",
                detail={"reason": str(reason or "")},
            )
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
            " status IN ('pending','interrupted','interrupted_legacy')"
            " OR state IN ('waiting','approved','executing')"
        ).fetchall()
        now_dt = self.now()
        for row in rows:
            approval_id = str(row["approval_id"])
            if not self.is_v2(row):
                # 旧 request() 的内存等待随进程消失：用**独立事实**记录「那次没有执行」。
                # 不写 state 列 —— 旧行没有 v2 状态，写成 interrupted_unverified 会把
                # 「旧路径」伪装成生命周期记录（前端因此误判）。
                if str(row["status"] or "") == "pending":
                    self.conn.execute(
                        "UPDATE pending_approvals SET status = 'interrupted_legacy',"
                        " state = NULL, resolved_at = ?"
                        " WHERE approval_id = ? AND status = 'pending'",
                        (_iso(now_dt), approval_id),
                    )
                    self._audit(
                        approval_id, "legacy_interrupted", {"reason": "process_restarted"}
                    )
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
        expected_confirmed_stage: int | None = None,
    ) -> None:
        """唯一的状态推进入口：BEGIN IMMEDIATE 内「读 → 核对前一状态 → CAS → 审计」。

        R9-H4 §10.3：expected_revision / expected_confirmed_stage 是**读到的旧状态**；
        UPDATE 把它们写进 WHERE。并发时只有一个请求命中（同一个确认阶段只能推进一次），
        旧请求不能覆盖新阶段、不能把次数改小、不能绕过撤回（revision 已被撤回动作递增）。
        """
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
        if expected_revision is not None:
            sql += " AND revisions = ?"
            params.append(int(expected_revision))
        if expected_confirmed_stage is not None:
            sql += " AND confirmed_stage = ?"
            params.append(int(expected_confirmed_stage))
        with _write_transaction(self.conn):
            current = self.get_raw(approval_id)
            if current is None:
                raise ApprovalNotFound("没有找到这条审批")
            if self.state_of(current) not in allowed:
                raise ApprovalConflict("这条审批已经被处理过")
            if expected_revision is not None and int(
                current["revisions"] or 0
            ) != int(expected_revision):
                raise ApprovalConflict(
                    "这条审批已经被更新过（revision 不匹配），请刷新后再操作"
                )
            if expected_confirmed_stage is not None and int(
                current["confirmed_stage"] or 0
            ) != int(expected_confirmed_stage):
                raise ApprovalConflict(
                    "确认阶段已变化（可能已被并发推進/重置），请刷新后再确认"
                )
            cur = self.conn.execute(sql, params)
            if int(cur.rowcount or 0) != 1:
                raise ApprovalConflict("并发冲突：这条审批已被其它请求处理，请刷新")
            payload = {"actor": actor}
            payload.update(detail or {})
            self._audit(approval_id, action, payload, strict=True)

    def _write_history(
        self,
        approval_id: str,
        *,
        summary: str,
        outcome: str,
        detail: dict | None = None,
    ) -> None:
        """写一条可读历史（approval_history，PK approval_id：每个审批一条最新结果）。

        detail 统一补上 kind / label（只补不覆盖调用方给的值）：前端「结果与历史」
        需要知道这是哪一条审批、当时的人类可读操作名。不改表结构。
        """
        try:
            row = self.get_raw(approval_id)
            kind = str(row["kind"] or "") if row is not None else ""
            label = ""
            if row is not None:
                item = self.row_to_dict(row) or {}
                kind = str(item.get("kind") or kind)
                label = str(item.get("label") or "")
            merged = dict(detail or {})
            merged.setdefault("kind", kind)
            if label:
                merged.setdefault("label", label)
            self.conn.execute(
                "INSERT OR REPLACE INTO approval_history"
                " (approval_id, kind, summary, outcome, detail, at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    str(approval_id),
                    kind,
                    str(summary or ""),
                    str(outcome),
                    _json(merged),
                    _iso(self.now()),
                ),
            )
        except sqlite3.Error:  # pragma: no cover - 历史失败不能挡住状态收口
            pass

    def _audit(
        self,
        approval_id: str,
        action: str,
        detail: dict | None = None,
        *,
        strict: bool = False,
    ) -> None:
        """写审计。

        strict=True 用于「状态迁移 + 审计必须一起成功」的调用点（在 _write_transaction
        内）：审计写不进去就抛错回滚，不允许状态变了而审计缺失。默认非 strict 只用于
        恢复/领取代拒绝这类不影响状态迁移的诊断事件。
        """
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
        except sqlite3.Error as exc:
            if strict:
                raise ApprovalError(
                    "审批审计写入失败，状态迁移已回滚（本次没有生效）"
                ) from exc
