"""完整位置状态的唯一权威与统一变更入口（R16 契约 §3/§4/§5/§14）。

核心事实（不新建第二份权威）
----------------------------
position_state 单行表只存**版本与因果信息**：position_version（完整位置状态版本）、
db_identity、last_source/last_turn_id/last_operation_id/last_nav_version/updated_at。
完整位置状态本身仍然读既有权威存储：

* Anchor：cursor 表 active 行（AnchorService；唯一写者仍是本模块的 apply()）；
* historic：AnchorService.is_historic_position 语义（focus 片段非开放）；
* pending：continuation_intents 的唯一 registered 选择（沿用既有存储，不复制）；
* nav_version：navigation_version 单行表（导航**决定**版本）；
* open_fragment_id：当前话题的开放片段（§14.5 内部事实，参与真实变化判定）；
* position_identity：settings['db.instance_id']（DB 身份，启动期 ensure_identity 对齐）。

两类版本职责严格分开（§3.2）
----------------------------
* nav_version 只表示「用户/工具的真实导航决定」：重放、渲染、草稿、弹窗收起、自动分段、
  内部片段细化都不推进；用于旧申请/旧命令失效（CAS）。
* position_version 用于排序完整位置状态：任何真实变化推进一次；无变化/幂等重放不推进。
* 进程内 seq 只作同实例乱序兜底，不在这里承担跨重启排序。

统一变更入口（§4）
------------------
所有生产位置写入必须且只能经 PositionStateService.apply()。它在**同一个 SQLite 事务**内完成：
校验命令身份/参数摘要 → 校验 CAS/前提 → 写 Anchor → 写/清 pending intent →
推进 nav_version（如适用）→ 推进 position_version（如适用）→ 写命令结果行。
事务提交后才由调用方发布事件（用 CommandResult.position 直接发布，不再二次读取）。
拒绝类命令零业务副作用；首次被拒的身份允许写一行 rejected/superseded 命令结果供重放。

身份与幂等（§5/§14）
--------------------
* command_id == identity_key（确定性、跨重启可复现）：
  request:<request_id> / operation:<operation_id>:<param_version> / internal:<kind>:<scope>；
* 同 identity_key + 同 param_digest：返回**存储时**的 result + **当前**只读 position，
  replay=True/changed=False，零写入/零版本推进/零广播；
* 同 identity_key + 不同 param_digest：status=conflict，零写入、不新增行，返回既有
  command_id 与当前 position（I05）；
* 命中 rejected/superseded 命令行：replay 既有拒绝事实（inactive=True），零写入。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent.graph.anchors import AnchorService
from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager
from agent.services.binding import (
    INTENT_CANCELLED,
    INTENT_CONSUMED,
    INTENT_REGISTERED,
    INTENT_REPLACED,
    INTENT_RESOLVED,
    IntentNotFound,
    TurnBindingService,
)
from agent.services.navigation_version import NavigationVersionConflict
from agent.services.navigation_version import cas_or_advance
from agent.services.navigation_version import current as current_nav_version
from agent.storage.db import transaction
from agent.storage.db_identity import DB_INSTANCE_KEY, ensure_instance_id

logger = logging.getLogger(__name__)

POSITION_STATE_TABLE = "position_state"
NAVIGATION_COMMANDS_TABLE = "navigation_commands"

#: §3.3 冻结转换表的全部 kind。
KIND_ENTER_TOPIC = "enter_topic"
KIND_SELECT_HISTORY = "select_history"
KIND_OPEN_FRAGMENT = "open_fragment"
KIND_CREATE_TOPIC = "create_topic"
KIND_APPLY_CONTINUATION = "apply_continuation"
KIND_INTERNAL_REFINE = "internal_refine"
KIND_AUTO_SEGMENT = "auto_segment"
KIND_CANCEL_CONTINUATION = "cancel_continuation"

ALL_KINDS: tuple[str, ...] = (
    KIND_ENTER_TOPIC,
    KIND_SELECT_HISTORY,
    KIND_OPEN_FRAGMENT,
    KIND_CREATE_TOPIC,
    KIND_APPLY_CONTINUATION,
    KIND_INTERNAL_REFINE,
    KIND_AUTO_SEGMENT,
    KIND_CANCEL_CONTINUATION,
)

#: 会推进 nav_version 的「真实导航决定」kind（cancel_continuation 是决定但 v2 §14.9
#: 冻结为不推进：取消不是新的导航选择，不使既有申请失效）。
NAV_DECISION_KINDS = frozenset(
    {KIND_ENTER_TOPIC, KIND_SELECT_HISTORY, KIND_OPEN_FRAGMENT, KIND_CREATE_TOPIC}
)

#: 真实用户/工具导航决定会清除全局唯一 registered 意图（§14.5）；select_history 用替换，
#: internal_refine/apply_continuation/auto_segment 不改 pending。
CLEARING_KINDS = frozenset({KIND_ENTER_TOPIC, KIND_OPEN_FRAGMENT, KIND_CREATE_TOPIC})

#: internal 转换的身份必须携带 expected_position_version（§14.1/§14.5）。
INTERNAL_KINDS = frozenset(
    {KIND_APPLY_CONTINUATION, KIND_INTERNAL_REFINE, KIND_AUTO_SEGMENT}
)

PENDING_ACTIONS: tuple[str, ...] = ("keep", "cancel", "register", "resolve", "consume")

STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"
STATUS_CONFLICT = "conflict"

REASON_NAV_STALE = "navigation_command_stale"
REASON_POSITION_STALE = "position_state_stale"
REASON_INTENT_INACTIVE = "continuation_request_inactive"
REASON_CONFLICT = "navigation_command_conflict"

_INITIATORS = ("user", "tool", "internal", "recovery")
_IDENTITY_KINDS = ("request", "operation", "internal")

_COMMAND_COLUMNS = (
    "command_id",
    "identity_key",
    "identity_kind",
    "command_kind",
    "param_digest",
    "request_id",
    "operation_id",
    "param_version",
    "status",
    "reason",
    "result_json",
    "position_json",
    "nav_version",
    "position_version",
    "initiator",
    "turn_id",
    "approval_id",
    "created_at",
    "updated_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def param_digest(transition: "PositionTransition") -> str:
    """执行参数的精确规范摘要（纯函数；§14.1 冻结接口）。

    只包含真正影响执行结果的字段（topic/fragment/name/pending 动作等）；CAS 前提
    （base_nav_version / expected_position_version）与展示字段不参与摘要。参数实质变化
    → 摘要变化 → 同身份再执行即 I05 冲突（零副作用）。
    """
    payload = {
        "kind": str(transition.kind),
        "target_topic_id": transition.target_topic_id,
        "target_fragment_id": transition.target_fragment_id,
        "move_anchor": bool(
            transition.move_anchor
            if transition.move_anchor is not None
            else transition.target_topic_id is not None
        ),
        "pending": transition.pending,
        "pending_topic_id": transition.pending_topic_id,
        "pending_fragment_id": transition.pending_fragment_id,
        "pending_request_id": transition.pending_request_id,
        "pending_intent_id": transition.pending_intent_id,
        "new_topic_name": transition.new_topic_name,
        "causal": dict(transition.causal or {}),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


@dataclass(frozen=True)
class NavCommandIdentity:
    """导航命令身份（§14.8 冻结字段）。

    * kind：request | operation | internal；
    * identity_key：request:<id> / operation:<opID>:<pv> / internal:<command_kind>:<scope>；
    * internal_scope：internal 身份的确定性 scope（如 "<turn_id>:<fragment_id>"）。
    """

    kind: str = "internal"
    identity_key: str = ""
    request_id: str | None = None
    operation_id: str | None = None
    param_version: int | None = None
    internal_scope: str | None = None

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        if kind not in _IDENTITY_KINDS:
            raise ValueError(f"非法导航命令身份 kind：{self.kind!r}")
        object.__setattr__(self, "kind", kind)
        key = str(self.identity_key or "").strip()
        if not key:
            if kind == "request":
                if not self.request_id:
                    raise ValueError("request 身份缺少 request_id")
                key = f"request:{self.request_id}"
            elif kind == "operation":
                if not self.operation_id or self.param_version is None:
                    raise ValueError("operation 身份缺少 operation_id / param_version")
                key = f"operation:{self.operation_id}:{int(self.param_version)}"
            else:
                raise ValueError("internal 身份必须显式提供 identity_key（含确定性 scope）")
            object.__setattr__(self, "identity_key", key)
        expected_prefix = {"request": "request:", "operation": "operation:", "internal": "internal:"}[kind]
        if not key.startswith(expected_prefix):
            raise ValueError(
                f"identity_key 前缀与 kind 不一致：kind={kind!r} key={key!r}"
            )
        if kind == "request" and not self.request_id:
            object.__setattr__(self, "request_id", key.split(":", 1)[1] or None)
        if kind == "operation" and (not self.operation_id or self.param_version is None):
            parts = key.split(":")
            if len(parts) >= 3 and not self.operation_id:
                object.__setattr__(self, "operation_id", parts[1])
            if len(parts) >= 3 and self.param_version is None:
                try:
                    object.__setattr__(self, "param_version", int(parts[2]))
                except ValueError:
                    pass
        if kind == "internal" and self.internal_scope is None:
            parts = key.split(":", 2)
            if len(parts) == 3:
                object.__setattr__(self, "internal_scope", parts[2])

    @property
    def command_kind(self) -> str | None:
        """internal 身份里编码的 command_kind（request/operation 没有该字段）。"""
        if self.kind != "internal":
            return None
        parts = str(self.identity_key).split(":", 2)
        return parts[1] if len(parts) >= 2 else None

    # -- 便利构造 ---------------------------------------------------------
    @classmethod
    def request(cls, request_id: str) -> "NavCommandIdentity":
        return cls(kind="request", request_id=str(request_id))

    @classmethod
    def operation(
        cls, operation_id: str, param_version: int
    ) -> "NavCommandIdentity":
        return cls(
            kind="operation",
            operation_id=str(operation_id),
            param_version=int(param_version),
        )

    @classmethod
    def internal(cls, command_kind: str, scope: str) -> "NavCommandIdentity":
        return cls(
            kind="internal",
            identity_key=f"internal:{command_kind}:{scope}",
            internal_scope=str(scope),
        )

    @classmethod
    def anonymous(cls, command_kind: str) -> "NavCommandIdentity":
        """旧调用方无命令身份时的兼容身份（UUID scope；§14.7 允许，生产新路径必须传真实身份）。"""
        return cls.internal(command_kind, f"anonymous:{uuid.uuid4().hex[:16]}")


@dataclass
class PositionTransition:
    """一次位置转换（§4 字段 + §14 澄清；全部 keyword 构造）。

    Anchor 规则：target_topic_id 为 None ⇒ Anchor 保持不变（工具 continue 登记态、
    cancel_continuation、auto_segment、不带目标的内部细化）；非 None ⇒
    Anchor=(target_topic_id, target_fragment_id)（fragment None = 该话题最新位置，存储 NULL）。
    create_topic 且 target_topic_id 为 None 时，apply 在同一事务内按 new_topic_name 建话题。
    """

    kind: str
    source: str
    identity: NavCommandIdentity
    initiator: str = "internal"
    target_topic_id: str | None = None
    target_fragment_id: str | None = None
    base_nav_version: int | None = None
    expected_position_version: int | None = None
    pending: str = "keep"
    pending_topic_id: str | None = None
    pending_fragment_id: str | None = None
    pending_request_id: str | None = None
    pending_intent_id: str | None = None
    nav_decision: bool | None = None
    move_anchor: bool | None = None
    turn_id: str | None = None
    approval_id: str | None = None
    operation_id: str | None = None
    param_version: int | None = None
    causal: dict[str, Any] = field(default_factory=dict)
    result_extra: dict[str, Any] = field(default_factory=dict)
    new_topic_name: str | None = None

    def effective_move_anchor(self) -> bool:
        if self.move_anchor is not None:
            return bool(self.move_anchor)
        return self.target_topic_id is not None


@dataclass(frozen=True)
class CommandResult:
    """命令结果（§14.1 冻结字段）。command_id == identity_key。"""

    command_id: str
    identity_key: str
    status: str
    changed: bool
    replay: bool
    inactive: bool
    conflict: bool
    code: str | None
    reason: str
    nav_version: int
    position_version: int
    position: dict[str, Any]
    result: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "identity_key": self.identity_key,
            "status": self.status,
            "changed": self.changed,
            "replay": self.replay,
            "inactive": self.inactive,
            "conflict": self.conflict,
            "code": self.code,
            "reason": self.reason,
            "nav_version": self.nav_version,
            "position_version": self.position_version,
            "position": self.position,
            "result": self.result,
        }


class PositionCommandConflict(RuntimeError):
    """同身份不同参数（I05）：零业务副作用。"""

    code = REASON_CONFLICT
    status_code = 409

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(
            f"导航命令身份 {result.command_id} 已用于不同参数，本次没有执行"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status_code": self.status_code,
            "command_id": self.result.command_id,
            "position": self.result.position,
            "nav_version": self.result.nav_version,
            "position_version": self.result.position_version,
        }


class PositionCommandRejected(RuntimeError):
    """CAS/前提不满足（零业务副作用）；result 是拒绝事实，调用方负责映射。"""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(f"{result.code or 'rejected'}：{result.reason}")

    @property
    def code(self) -> str | None:
        return self.result.code


class PositionStateUnavailable(RuntimeError):
    """position_state / navigation_commands 表缺失（未迁移的库不能假装有权威状态）。"""


@dataclass(frozen=True)
class _Facts:
    topic_id: str | None
    fragment_id: str | None
    historic: bool
    pending_intent_id: str | None
    pending_intent_version: int | None
    pending_state: str | None
    pending_source_fragment_id: str | None
    open_fragment_id: str | None

    def signature(self) -> tuple:
        return (
            self.topic_id,
            self.fragment_id,
            bool(self.historic),
            self.pending_intent_id,
            self.pending_intent_version,
            self.pending_state,
            self.pending_source_fragment_id,
            self.open_fragment_id,
        )


class PositionStateService:
    """完整位置状态的唯一写者 + 命令身份/幂等/冲突入口（§4/§5）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 只读 -------------------------------------------------------------
    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    def _read_state_row(self) -> sqlite3.Row | None:
        if not self._table_exists(POSITION_STATE_TABLE):
            return None
        return self.conn.execute(
            f"SELECT * FROM {POSITION_STATE_TABLE} WHERE id = 1"
        ).fetchone()

    def _ensure_row(self) -> None:
        self.conn.execute(
            f"INSERT OR IGNORE INTO {POSITION_STATE_TABLE} "
            "(id, position_version, db_identity, last_source, last_turn_id, "
            " last_operation_id, last_nav_version, updated_at) "
            "VALUES (1, 0, '', '', NULL, NULL, NULL, '')"
        )

    def current_position_version(self) -> int:
        row = self._read_state_row()
        if row is None:
            return 0
        try:
            return int(row["position_version"] or 0)
        except (TypeError, ValueError):  # pragma: no cover - 损坏行按 0（不造假版本）
            return 0

    def _read_identity(self) -> str:
        try:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (DB_INSTANCE_KEY,)
            ).fetchone()
        except sqlite3.Error:  # pragma: no cover - 极旧库无 settings
            return ""
        return str(row["value"] or "").strip() if row is not None else ""

    def _position_state_row(self) -> dict[str, Any] | None:
        row = self._read_state_row()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _fragment_title(self, fragment_id: str | None) -> str | None:
        if not fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            return str(row["title"])
        fragment = FragmentManager(self.conn).get(fragment_id)
        if fragment is not None and fragment.summary:
            return fragment.summary.strip().splitlines()[0][:40]
        return None

    def _facts(self) -> _Facts:
        anchors = AnchorService(self.conn)
        anchor = anchors.get_active()
        topic_id = anchor.topic_id if anchor is not None else None
        fragment_id = anchor.fragment_id if anchor is not None else None
        historic = (
            anchors.is_historic_position(topic_id) if topic_id else False
        )
        pending = TurnBindingService(self.conn).peek_intent()
        open_fragment_id = anchors.open_fragment_id(topic_id) if topic_id else None
        return _Facts(
            topic_id=topic_id,
            fragment_id=fragment_id,
            historic=bool(historic),
            pending_intent_id=pending.intent_id if pending is not None else None,
            pending_intent_version=pending.version if pending is not None else None,
            pending_state=pending.state if pending is not None else None,
            pending_source_fragment_id=(
                pending.source_fragment_id if pending is not None else None
            ),
            open_fragment_id=open_fragment_id,
        )

    def _pending_dict(self) -> dict[str, Any] | None:
        pending = TurnBindingService(self.conn).peek_intent()
        if pending is None:
            return None
        return {
            "intent_id": pending.intent_id,
            "topic_id": pending.topic_id,
            "version": pending.version,
            "state": pending.state,
            "source_fragment_id": pending.source_fragment_id,
            "resolved_fragment_id": pending.resolved_fragment_id,
            "request_id": pending.request_id,
        }

    def read(self) -> dict[str, Any]:
        """完整位置状态 wire 对象（§3.1 冻结形状）；单一读事务内一次读完，不做二次读取。"""
        with transaction(self.conn):
            anchors = AnchorService(self.conn)
            anchor = anchors.get_active()
            topic_id = anchor.topic_id if anchor is not None else None
            fragment_id = anchor.fragment_id if anchor is not None else None
            pending = self._pending_dict()
            historic = bool(anchors.is_historic_position(topic_id)) if topic_id else False
            topic_name = topic_id
            if topic_id:
                node = NodeService(self.conn).get_topic(topic_id)
                if node is not None:
                    topic_name = node.name
            state_row = self._position_state_row() or {}
            return {
                "topic_id": topic_id,
                "topic_name": topic_name,
                "fragment_id": fragment_id,
                "fragment_title": self._fragment_title(fragment_id),
                "historic": historic,
                "pending_intent_id": pending["intent_id"] if pending else None,
                "pending_intent_version": pending["version"] if pending else None,
                "pending_source_fragment_id": (
                    pending["source_fragment_id"] if pending else None
                ),
                "pending_source_title": (
                    self._fragment_title(pending["source_fragment_id"])
                    if pending and pending.get("source_fragment_id")
                    else None
                ),
                "nav_version": int(current_nav_version(self.conn)),
                "position_version": int(state_row.get("position_version") or 0),
                "position_identity": self._read_identity(),
            }

    # -- 启动期权威重建 ---------------------------------------------------
    def ensure_identity(self) -> bool:
        """启动期权威重建：对齐/重建 db_identity；换库时 position_version 归零。

        返回是否发生了重建。只有本方法（启动期）可以切换身份；实时事件/命令不得自行切换。
        命令历史保留（replay 时返回既有 result + 当前 position/新 identity，不标 superseded）。
        """
        if not self._table_exists(POSITION_STATE_TABLE):
            return False
        with transaction(self.conn):
            self._ensure_row()
            instance_id = ensure_instance_id(self.conn)
            row = self._read_state_row()
            current_identity = str(row["db_identity"] or "").strip() if row is not None else ""
            if current_identity == instance_id:
                return False
            self.conn.execute(
                f"UPDATE {POSITION_STATE_TABLE} SET position_version = 0, db_identity = ?, "
                "last_source = 'identity_rebuild', last_turn_id = NULL, last_operation_id = NULL, "
                "last_nav_version = ?, updated_at = ? WHERE id = 1",
                (instance_id, int(current_nav_version(self.conn)), _now()),
            )
            logger.info(
                "position_state 身份重建：position_version 归零并采用新 db_identity（%s）",
                instance_id[:8],
            )
            return True

    # -- 命令结果行（只读）------------------------------------------------
    def _lookup_row(self, identity_key: str) -> dict[str, Any] | None:
        if not self._table_exists(NAVIGATION_COMMANDS_TABLE):
            return None
        row = self.conn.execute(
            f"SELECT {', '.join(_COMMAND_COLUMNS)} FROM {NAVIGATION_COMMANDS_TABLE} "
            "WHERE identity_key = ?",
            (str(identity_key),),
        ).fetchone()
        if row is None:
            return None
        data = {key: row[key] for key in row.keys()}
        for field_name in ("result_json", "position_json"):
            try:
                data[field_name] = json.loads(data.get(field_name) or "{}")
            except (TypeError, ValueError):
                data[field_name] = {}
        return data

    def lookup(self, identity_key: str) -> dict[str, Any] | None:
        """只读命令行事实（诊断/测试；§14.1）。"""
        row = self._lookup_row(identity_key)
        if row is None:
            return None
        data = dict(row)
        data["result"] = data.pop("result_json", {})
        data["position"] = data.pop("position_json", {})
        return data

    def _current_versions(self, position: dict[str, Any]) -> tuple[int, int]:
        return int(current_nav_version(self.conn)), int(
            position.get("position_version") or 0
        )

    def _replay_result(self, row: dict[str, Any]) -> CommandResult:
        position = self.read()
        nav_version, position_version = self._current_versions(position)
        status = str(row.get("status") or STATUS_APPLIED)
        result = dict(row.get("result_json") or {})
        code = result.get("code")
        if status == STATUS_APPLIED:
            code = None
        return CommandResult(
            command_id=str(row.get("command_id") or row.get("identity_key") or ""),
            identity_key=str(row.get("identity_key") or ""),
            status=status,
            changed=False,
            replay=True,
            inactive=status in (STATUS_REJECTED, STATUS_SUPERSEDED),
            conflict=False,
            code=code,
            reason=str(row.get("reason") or ""),
            nav_version=nav_version,
            position_version=position_version,
            position=position,
            result=result,
        )

    def _conflict_result(self, row: dict[str, Any]) -> CommandResult:
        position = self.read()
        nav_version, position_version = self._current_versions(position)
        return CommandResult(
            command_id=str(row.get("command_id") or ""),
            identity_key=str(row.get("identity_key") or ""),
            status=STATUS_CONFLICT,
            changed=False,
            replay=False,
            inactive=False,
            conflict=True,
            code=REASON_CONFLICT,
            reason="同一导航命令身份被用于不同参数：本次没有执行",
            nav_version=nav_version,
            position_version=position_version,
            position=position,
            result={
                "code": REASON_CONFLICT,
                "existing_command_id": str(row.get("command_id") or ""),
                "existing_command_kind": str(row.get("command_kind") or ""),
            },
        )

    def replay(self, identity_key: str, param_digest: str) -> CommandResult | None:
        """只读重放查询（§14.1）：不存在返回 None；不写任何行。"""
        row = self._lookup_row(identity_key)
        if row is None:
            return None
        if str(row.get("param_digest") or "") != str(param_digest):
            return self._conflict_result(row)
        return self._replay_result(row)

    # -- 统一变更入口 -----------------------------------------------------
    def _validate(self, transition: PositionTransition) -> None:
        kind = str(transition.kind or "")
        if kind not in ALL_KINDS:
            raise ValueError(f"未知位置转换 kind：{transition.kind!r}")
        expected_decision = kind in NAV_DECISION_KINDS
        if transition.nav_decision is not None and bool(transition.nav_decision) != expected_decision:
            raise ValueError(
                f"nav_decision 与 kind 冻结值不一致：kind={kind!r} "
                f"expects {expected_decision!r}"
            )
        if str(transition.initiator) not in _INITIATORS:
            raise ValueError(f"非法 initiator：{transition.initiator!r}")
        identity = transition.identity
        if identity is None:
            raise ValueError("位置转换必须携带命令身份")
        # §14.8：apply 自己复查 kind 与 identity_key 前缀一致（不依赖构造函数，
        # object.__new__ / 反序列化绕过 __post_init__ 时也必须在这里拒绝）。
        if identity.kind not in _IDENTITY_KINDS:
            raise ValueError(f"非法导航命令身份 kind：{identity.kind!r}")
        prefix = {
            "request": "request:",
            "operation": "operation:",
            "internal": "internal:",
        }[identity.kind]
        if not str(identity.identity_key or "").startswith(prefix):
            raise ValueError(
                f"identity_key 前缀与 kind 不一致：kind={identity.kind!r} "
                f"key={identity.identity_key!r}"
            )
        if identity.kind == "internal":
            command_kind = identity.command_kind
            if command_kind is not None and command_kind != kind:
                raise ValueError(
                    f"internal 身份 command_kind {command_kind!r} 与转换 kind {kind!r} 不一致"
                )
            deterministic = ":anonymous:" not in identity.identity_key
            if deterministic and kind in INTERNAL_KINDS and transition.expected_position_version is None:
                raise ValueError(
                    f"internal 转换 {kind!r} 必须携带 expected_position_version（§14.1）"
                )
        if str(transition.pending) not in PENDING_ACTIONS:
            raise ValueError(f"非法 pending 动作：{transition.pending!r}")
        if transition.pending == "register":
            if not (transition.pending_topic_id or transition.target_topic_id):
                raise ValueError("pending=register 缺少 pending_topic_id/target_topic_id")
            if not (transition.pending_fragment_id or transition.target_fragment_id):
                raise ValueError("pending=register 缺少 pending_fragment_id/target_fragment_id")
        if transition.pending in ("resolve", "consume") and not transition.pending_intent_id:
            raise ValueError(f"pending={transition.pending} 缺少 pending_intent_id")
        if kind == KIND_CREATE_TOPIC and transition.target_topic_id is None:
            if not str(transition.new_topic_name or "").strip():
                raise ValueError("create_topic 需要 new_topic_name 或已建好的 target_topic_id")

    def _reject(
        self,
        transition: PositionTransition,
        *,
        status: str,
        code: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> CommandResult:
        position = self.read()
        nav_version, position_version = self._current_versions(position)
        result = {
            "code": code,
            "target_topic_id": transition.target_topic_id,
            "target_fragment_id": transition.target_fragment_id,
        }
        if extra:
            result.update(extra)
        return CommandResult(
            command_id=transition.identity.identity_key,
            identity_key=transition.identity.identity_key,
            status=status,
            changed=False,
            replay=False,
            inactive=status in (STATUS_REJECTED, STATUS_SUPERSEDED),
            conflict=False,
            code=code,
            reason=reason,
            nav_version=nav_version,
            position_version=position_version,
            position=position,
            result=result,
        )

    def _insert_command(
        self,
        transition: PositionTransition,
        digest: str,
        *,
        status: str,
        reason: str,
        result: CommandResult,
    ) -> None:
        identity = transition.identity
        now = _now()
        try:
            self.conn.execute(
                f"INSERT INTO {NAVIGATION_COMMANDS_TABLE} "
                "(" + ", ".join(_COMMAND_COLUMNS) + ") "
                "VALUES (" + ", ".join("?" for _ in _COMMAND_COLUMNS) + ")",
                (
                    result.command_id,
                    identity.identity_key,
                    identity.kind,
                    transition.kind,
                    digest,
                    identity.request_id,
                    identity.operation_id,
                    identity.param_version,
                    status,
                    reason,
                    json.dumps(result.result, ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(result.position, ensure_ascii=False, sort_keys=True, default=str),
                    result.nav_version,
                    result.position_version,
                    str(transition.initiator),
                    transition.turn_id,
                    transition.approval_id,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:  # pragma: no cover - 极端并发：同身份已落行
            logger.info("navigation_commands 同身份已存在，保留既有结果行：%s", identity.identity_key)

    def _nav_should_advance(
        self,
        transition: PositionTransition,
        *,
        anchor_changed: bool,
        pending_cleared: bool,
        registration_changed: bool,
    ) -> bool:
        kind = transition.kind
        if kind == KIND_ENTER_TOPIC:
            return bool(anchor_changed or pending_cleared)
        if kind == KIND_OPEN_FRAGMENT:
            return bool(anchor_changed or pending_cleared)
        if kind == KIND_CREATE_TOPIC:
            return True
        if kind == KIND_SELECT_HISTORY:
            # §14.5：工具登记路径（move_anchor=False）只按登记是否真实改变推进；
            # 点击路径（move_anchor=True）Anchor 真实移动到历史片段也是导航决定。
            return bool(
                registration_changed
                or (transition.effective_move_anchor() and anchor_changed)
            )
        # cancel_continuation / apply_continuation / internal_refine / auto_segment:
        # 不推进导航决定版本（§3.3 + §14.9）。
        return False

    def apply(self, transition: PositionTransition) -> CommandResult:
        """统一变更入口：同一事务内完成身份校验/CAS/Anchor/pending/两类版本/命令结果行。"""
        self._validate(transition)
        digest = param_digest(transition)
        if not (
            self._table_exists(POSITION_STATE_TABLE)
            and self._table_exists(NAVIGATION_COMMANDS_TABLE)
        ):
            raise PositionStateUnavailable(
                "position_state / navigation_commands 表缺失：请先执行迁移 83/84，"
                "不能绕过统一入口写位置"
            )
        with transaction(self.conn):
            self._ensure_row()
            existing = self._lookup_row(transition.identity.identity_key)
            if existing is not None:
                if str(existing.get("param_digest") or "") == digest:
                    return self._replay_result(existing)
                return self._conflict_result(existing)
            result = self._apply_new(transition, digest)
            if result.status in (STATUS_REJECTED, STATUS_SUPERSEDED):
                # 首次被拒：零业务副作用，允许在事务内只写一行拒绝事实供重放。
                self._insert_command(
                    transition,
                    digest,
                    status=result.status,
                    reason=result.reason,
                    result=result,
                )
            return result

    def record_rejection(
        self, transition: PositionTransition, result: CommandResult
    ) -> None:
        """业务回滚后补落拒绝行（调用方在外层事务因 rejected 回滚时使用；只写一次）。"""
        if not self._table_exists(NAVIGATION_COMMANDS_TABLE):
            return
        with transaction(self.conn):
            if self._lookup_row(transition.identity.identity_key) is not None:
                return
            self._insert_command(
                transition,
                param_digest(transition),
                status=result.status,
                reason=result.reason,
                result=result,
            )

    def _apply_new(self, transition: PositionTransition, digest: str) -> CommandResult:
        """在调用方事务内执行一次真实转换（无既有命令身份）。"""
        kind = transition.kind
        now_nav = int(current_nav_version(self.conn))
        if transition.base_nav_version is not None and int(transition.base_nav_version) != now_nav:
            return self._reject(
                transition,
                status=STATUS_SUPERSEDED,
                code=REASON_NAV_STALE,
                reason=(
                    f"导航位置已改变（依据版本 {int(transition.base_nav_version)}，"
                    f"当前版本 {now_nav}），这次导航没有执行"
                ),
                extra={
                    "expected_nav_version": int(transition.base_nav_version),
                    "actual_nav_version": now_nav,
                },
            )
        pos_before = int(self.current_position_version())
        if (
            transition.expected_position_version is not None
            and int(transition.expected_position_version) != pos_before
        ):
            return self._reject(
                transition,
                status=STATUS_SUPERSEDED,
                code=REASON_POSITION_STALE,
                reason=(
                    f"完整位置状态已改变（预期版本 "
                    f"{int(transition.expected_position_version)}，当前 {pos_before}），"
                    "这次内部推进没有执行"
                ),
                extra={
                    "expected_position_version": int(transition.expected_position_version),
                    "actual_position_version": pos_before,
                },
            )

        facts_before = self._facts()
        bindings = TurnBindingService(self.conn)
        anchors = AnchorService(self.conn)

        # 1) create_topic：同一事务内建话题（失败整体回滚）。
        target_topic = transition.target_topic_id
        target_fragment = transition.target_fragment_id
        created_topic_name: str | None = None
        if kind == KIND_CREATE_TOPIC and target_topic is None:
            created_topic_name = str(transition.new_topic_name or "").strip()
            node = NodeService(self.conn).create_topic(created_topic_name)
            target_topic = node.id
            target_fragment = None

        # 2) pending 意图：先按 §14.5 处理真实导航决定的清除，再执行本次登记/落实。
        pending_cleared_intent_id: str | None = None
        pending_result: dict[str, Any] = {}
        registration_changed = False
        # §14.5：只有**真实用户/工具导航决定**才清除全局唯一 registered 意图。
        # 旧调用方合成的 anonymous internal 身份（fixture / 兼容路径）不是导航决定，
        # 不得因为「例行补锚点」之类的位置写入误清用户等待中的接续申请。
        real_navigation_decision = (
            kind in CLEARING_KINDS
            and str(transition.initiator) in ("user", "tool")
            and ":anonymous:" not in transition.identity.identity_key
        )
        if real_navigation_decision:
            current_intent = bindings.peek_intent()
            if current_intent is not None:
                bindings.cancel_intent(current_intent.intent_id)
                pending_cleared_intent_id = current_intent.intent_id
        if transition.pending == "cancel":
            intent = (
                bindings.intent_by_id(transition.pending_intent_id)
                if transition.pending_intent_id
                else bindings.peek_intent()
            )
            if intent is not None and intent.state == INTENT_REGISTERED:
                cancelled = bindings.cancel_intent(intent.intent_id)
                pending_cleared_intent_id = cancelled.intent_id
                pending_result = {
                    "cancelled": True,
                    "cancelled_intent_id": cancelled.intent_id,
                }
            else:
                pending_result = {"cancelled": False, "cancelled_intent_id": None}
        elif transition.pending == "register":
            registration = bindings.register_intent_ex(
                transition.pending_topic_id or target_topic,
                transition.pending_fragment_id or target_fragment,
                request_id=transition.pending_request_id,
            )
            intent = registration.intent
            registration_changed = bool(registration.changed)
            inactive = intent.state in (INTENT_REPLACED, INTENT_CANCELLED)
            pending_result = {
                "intent_id": None if inactive else intent.intent_id,
                "intent_version": None if inactive else intent.version,
                "state": intent.state,
                "source_fragment_id": intent.source_fragment_id,
                "opens_current": False,
                "inactive": inactive,
                "pending": intent.state == INTENT_REGISTERED,
            }
        elif transition.pending == "resolve":
            intent = bindings.intent_by_id(transition.pending_intent_id or "")
            if intent is None:
                raise IntentNotFound(f"接续意图不存在：{transition.pending_intent_id}")
            if intent.state in (INTENT_REPLACED, INTENT_CANCELLED):
                return self._reject(
                    transition,
                    status=STATUS_REJECTED,
                    code=REASON_INTENT_INACTIVE,
                    reason=f"接续意图已失效（{intent.state}），不能重新落实",
                    extra={"state": intent.state, "intent_id": intent.intent_id},
                )
            resolved = bindings.resolve_intent(intent.intent_id, target_fragment or "")
            pending_result = {
                "intent_id": resolved.intent_id,
                "intent_version": resolved.version,
                "state": resolved.state,
                "source_fragment_id": resolved.source_fragment_id,
                "resolved_fragment_id": resolved.resolved_fragment_id,
                "pending": False,
            }
        elif transition.pending == "consume":
            intent = bindings.intent_by_id(transition.pending_intent_id or "")
            if intent is None:
                raise IntentNotFound(f"接续意图不存在：{transition.pending_intent_id}")
            consumed = bindings.consume_intent(intent.intent_id)
            pending_result = {
                "intent_id": consumed.intent_id,
                "intent_version": consumed.version,
                "state": consumed.state,
                "pending": False,
            }

        # 3) Anchor：只有 target_topic_id 非 None 且允许移动时才写。
        anchor_changed = False
        # create_topic 时 target_topic 在事务内才产生：默认语义是「写入新话题」；
        # move_anchor=False 只用于显式声明「不搬 Anchor」的调用方。
        should_move_anchor = transition.move_anchor is not False
        if target_topic is not None and should_move_anchor:
            active = anchors.get_active()
            if (
                active is None
                or active.topic_id != target_topic
                or active.fragment_id != target_fragment
            ):
                anchors.set_active(target_topic, target_fragment)
                anchor_changed = True

        # 4) 真实变化：完整位置状态内部事实（含 open_fragment_id）。
        facts_after = self._facts()
        changed = facts_before.signature() != facts_after.signature()
        if kind == KIND_AUTO_SEGMENT:
            # §14.5：封存开放片段一律是真实位置状态变化（causal 记 sealed_fragment_id）；
            # 调用方只在真的封存后调用，这里如实推进一次。
            changed = True
        if kind == KIND_SELECT_HISTORY:
            # 工具登记路径不改 Anchor：pending 登记/替换本身就是真实变化。
            changed = bool(changed or registration_changed)

        # 5) nav_version：只由真实导航决定推进（§3.3/§14.9）。
        nav_advance = self._nav_should_advance(
            transition,
            anchor_changed=anchor_changed,
            pending_cleared=pending_cleared_intent_id is not None,
            registration_changed=registration_changed,
        )
        new_nav = now_nav
        if nav_advance:
            try:
                new_nav = int(
                    cas_or_advance(
                        self.conn,
                        now_nav,
                        source=str(transition.source),
                        topic_id=facts_after.topic_id,
                        fragment_id=facts_after.fragment_id,
                        turn_id=transition.turn_id,
                    )
                )
            except NavigationVersionConflict as exc:
                return self._reject(
                    transition,
                    status=STATUS_SUPERSEDED,
                    code=REASON_NAV_STALE,
                    reason=str(exc),
                    extra={
                        "expected_nav_version": int(exc.expected),
                        "actual_nav_version": int(exc.actual),
                    },
                )

        # 6) position_version：任何真实变化推进一次；无变化/重放不推进。
        new_position = pos_before
        if changed:
            new_position = pos_before + 1
            self.conn.execute(
                f"UPDATE {POSITION_STATE_TABLE} SET position_version = ?, last_source = ?, "
                "last_turn_id = ?, last_operation_id = ?, last_nav_version = ?, updated_at = ? "
                "WHERE id = 1",
                (
                    new_position,
                    str(transition.source),
                    transition.turn_id,
                    transition.identity.operation_id,
                    new_nav,
                    _now(),
                ),
            )

        # 7) 事务内构造同源 position + result（提交后直接返回，不再二次读取）。
        position = self.read()
        result_payload: dict[str, Any] = dict(transition.result_extra or {})
        result_payload.update(pending_result)
        result_payload.setdefault("target_topic_id", target_topic)
        result_payload.setdefault("target_fragment_id", target_fragment)
        result_payload.setdefault("created_fragment_id", None)
        if pending_cleared_intent_id is not None:
            result_payload.setdefault("cleared_intent_id", pending_cleared_intent_id)
        if kind == KIND_CREATE_TOPIC and target_topic is not None:
            result_payload.setdefault("new_topic_id", target_topic)
            result_payload.setdefault("topic_id", target_topic)
            result_payload.setdefault("new_name", created_topic_name)
        if transition.causal:
            for key, value in transition.causal.items():
                result_payload.setdefault(str(key), value)
        result = CommandResult(
            command_id=transition.identity.identity_key,
            identity_key=transition.identity.identity_key,
            status=STATUS_APPLIED,
            changed=bool(changed),
            replay=False,
            inactive=False,
            conflict=False,
            code=None,
            reason="",
            nav_version=int(new_nav),
            position_version=int(new_position),
            position=position,
            result=result_payload,
        )
        self._insert_command(
            transition,
            digest,
            status=STATUS_APPLIED,
            reason="",
            result=result,
        )
        return result


__all__ = [
    "POSITION_STATE_TABLE",
    "NAVIGATION_COMMANDS_TABLE",
    "ALL_KINDS",
    "NAV_DECISION_KINDS",
    "KIND_ENTER_TOPIC",
    "KIND_SELECT_HISTORY",
    "KIND_OPEN_FRAGMENT",
    "KIND_CREATE_TOPIC",
    "KIND_APPLY_CONTINUATION",
    "KIND_INTERNAL_REFINE",
    "KIND_AUTO_SEGMENT",
    "KIND_CANCEL_CONTINUATION",
    "NavCommandIdentity",
    "PositionTransition",
    "CommandResult",
    "PositionStateService",
    "PositionCommandConflict",
    "PositionCommandRejected",
    "PositionStateUnavailable",
    "param_digest",
]
