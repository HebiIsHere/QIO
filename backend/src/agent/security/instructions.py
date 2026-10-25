"""指令授权（契约 §2.2）。

授权语义 = 哪条用户指令 + 哪些对象 + 什么操作 + 什么目的 + 什么边界/例外。
内部 id 只用于追踪校验；turn / topic / session / 开发任务都不是授权有效期。

两条铁律：

1. 每条用户指令建立独立的 InstructionContext；指令结束（turn 结束）即关闭其
   全部 grant，绝不因为同话题/同目录/历史许可而延续到后续指令。
2. 只有两种 grant 来源：user_explicit（从用户原话确定性提取，见 intent.py）
   与 user_approved（用户点「允许」产生的有界授权，对象/操作/目的都来自审批单）。
   模型不能自行声明 grant —— 本模块没有任何面向模型/工具的添加入口，唯一的
   add_grant 由审批服务在用户批准后调用。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from agent.security.context import current_instruction
from agent.security.effects import Effect


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_path(value: object) -> str:
    """路径比较键：normpath + normcase（Windows 大小写不敏感）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(os.path.normpath(text))
    except (OSError, ValueError):  # pragma: no cover - 极端输入
        return text.lower()


@dataclass
class Scope:
    """授权对象范围。kind: path | path_prefix | url_host | program。"""

    kind: str
    value: str

    def matches_path(self, path: object) -> bool:
        candidate = _norm_path(path)
        if not candidate:
            return False
        if self.kind == "path":
            return candidate == _norm_path(self.value)
        if self.kind == "path_prefix":
            prefix = _norm_path(self.value).rstrip("\\/")
            if not prefix:
                return False
            return (
                candidate == prefix
                or candidate.startswith(prefix + os.sep)
                or candidate.startswith(prefix + "/")
            )
        return False

    def matches_program(self, program: object) -> bool:
        if self.kind != "program":
            return False
        left = str(program or "").strip()
        right = str(self.value or "").strip()
        return _norm_path(left) == _norm_path(right) or left.lower() == right.lower()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value}


@dataclass
class Grant:
    grant_id: str
    instruction_id: str
    operations: frozenset[str]
    objects: list[Scope]
    purpose: str
    source: str  # user_explicit | user_approved | verified_authorization | verified_read
    created_at: str
    revoked: bool = False
    #: R9-H13 §3.H（F6）：来自已核实授权记录（模型审查 ALLOW）的边界依据。
    #: 只是事实，不生成额外权限；`allows` 每次都会实时核对记录/要求仍然有效。
    requirement_id: str | None = None
    requirement_version: int | None = None
    source_record_id: str | None = None
    fingerprint: str | None = None
    limits: tuple[str, ...] = ()
    #: 记录撤销/要求版本变化时必须立即失效；核对异常按失效（失败关闭）。
    active_check: Callable[[], bool] | None = field(
        default=None, repr=False, compare=False
    )

    def is_active(self) -> bool:
        """授权是否仍有效：显式撤销 + 记录/要求现状（撤销后读取也必须停止）。"""
        if self.revoked:
            return False
        checker = self.active_check
        if checker is None:
            return True
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - 核对失败按失效（失败关闭）
            return False

    def allows(
        self,
        operation: str,
        *,
        path: object = None,
        program: object = None,
        url_host: object = None,
    ) -> bool:
        """这个 grant 是否覆盖一次具体操作（对象必须命中，操作必须列出）。"""
        if not self.is_active() or operation not in self.operations:
            return False
        for scope in self.objects:
            if path is not None and scope.matches_path(path):
                return True
            if program is not None and scope.matches_program(program):
                return True
            if url_host is not None and scope.kind == "url_host":
                return str(url_host).strip().lower() == str(scope.value).strip().lower()
        return False

    def as_dict(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "instruction_id": self.instruction_id,
            "operations": sorted(self.operations),
            "objects": [scope.as_dict() for scope in self.objects],
            "purpose": self.purpose,
            "source": self.source,
            "created_at": self.created_at,
            "revoked": self.revoked,
            "requirement_id": self.requirement_id,
            "requirement_version": self.requirement_version,
            "source_record_id": self.source_record_id,
            "fingerprint": self.fingerprint,
            "limits": list(self.limits),
        }


@dataclass
class InstructionContext:
    instruction_id: str
    text: str
    turn_id: str
    topic_id: str | None
    created_at: str
    grants: list[Grant] = field(default_factory=list)

    def active_grants(self) -> list[Grant]:
        return [grant for grant in self.grants if not grant.revoked]

    def grants_for(
        self,
        operation: str,
        *,
        path: object = None,
        program: object = None,
        url_host: object = None,
    ) -> list[Grant]:
        return [
            grant
            for grant in self.active_grants()
            if grant.allows(operation, path=path, program=program, url_host=url_host)
        ]

    def as_dict(self) -> dict:
        return {
            "instruction_id": self.instruction_id,
            "text": self.text,
            "turn_id": self.turn_id,
            "topic_id": self.topic_id,
            "created_at": self.created_at,
            "grants": [grant.as_dict() for grant in self.grants],
        }


#: 操作名（grant.operations 用这些稳定字符串）。
OPERATION_READ = "read"
OPERATION_WRITE = "write"
OPERATION_CREATE = "create"
OPERATION_DELETE = "delete"
OPERATION_EXECUTE = "execute"
OPERATION_INSTALL_DEPS = "install_deps"
OPERATION_NETWORK_EGRESS = "network_egress"

_EFFECT_TO_OPERATIONS = {
    "read": (OPERATION_READ,),
    "write": (OPERATION_WRITE, OPERATION_CREATE),
    "create": (OPERATION_CREATE, OPERATION_WRITE),
    "delete": (OPERATION_DELETE,),
    "execute": (OPERATION_EXECUTE,),
    "install_deps": (OPERATION_INSTALL_DEPS,),
    "network_egress": (OPERATION_NETWORK_EGRESS,),
}


def operations_for(effect: Effect) -> tuple[str, ...]:
    """effect 对应的 grant 操作名集合。"""
    return _EFFECT_TO_OPERATIONS.get(effect.kind.value, ())


def instruction_allows_path(effect: Effect, path: object, grant: Grant) -> bool:
    """grant 是否覆盖 effect 对 path 的操作（供引擎/工具复用）。"""
    for operation in operations_for(effect):
        if grant.allows(operation, path=path):
            return True
    return False


class InstructionRegistry:
    """本轮指令的创建/关闭入口（每个进程一个）。

    begin() 只在轮次开始时被 turn 运行时调用一次；end() 会关闭全部 grant，
    释放后的指令 id 不再被任何判定接受。
    """

    def __init__(self) -> None:
        self._active: InstructionContext | None = None
        self._closed: set[str] = set()

    def begin(
        self,
        *,
        turn_id: str,
        text: str,
        topic_id: str | None = None,
        extract: bool = True,
    ) -> InstructionContext:
        instruction_id = (
            f"instr_{turn_id}" if turn_id else f"instr_{uuid.uuid4().hex[:12]}"
        )
        created_at = _now_iso()
        instruction = InstructionContext(
            instruction_id=instruction_id,
            text=str(text or ""),
            turn_id=str(turn_id or ""),
            topic_id=topic_id,
            created_at=created_at,
        )
        # R9-F（契约 §2、§4.7）：自然语言许可**不再**由动作词/路径关键词产生。
        # 受限模式下由模型授权审查理解具体用户要求，再经后端验证形成有界授权记录
        # （见 security/review.py 与 AuthorizationLedger）；这里只保留用户原话与
        # 生命周期，不再把"句子里出现修改/删除"当成 grant。
        # extract 参数保留仅为兼容旧调用点，语义固定为"不产生 grant"。
        _ = extract
        self._active = instruction
        return instruction

    def end(self, instruction: InstructionContext | None = None) -> None:
        """关闭指令及其全部 grant（幂等）。"""
        target = instruction if instruction is not None else self._active
        if target is None:
            return
        for grant in target.grants:
            grant.revoked = True
        self._closed.add(target.instruction_id)
        if self._active is target:
            self._active = None

    def current(self) -> InstructionContext | None:
        return self._active

    def is_closed(self, instruction_id: str | None) -> bool:
        return bool(instruction_id) and instruction_id in self._closed

    def add_grant(
        self,
        instruction: InstructionContext | None,
        *,
        operations: Iterable[str],
        objects: list[Scope],
        purpose: str,
        source: str = "user_approved",
    ) -> Grant | None:
        """给当前指令追加有界授权（只允许审批服务在用户批准后调用）。

        指令已经结束 / 传入的不是当前指令时拒绝：批准不得写到已经关闭的指令上，
        更不能变成长期权限。
        """
        if instruction is None or instruction is not self._active:
            return None
        if instruction.instruction_id in self._closed:
            return None
        grant = Grant(
            grant_id=f"grant_{uuid.uuid4().hex[:12]}",
            instruction_id=instruction.instruction_id,
            operations=frozenset(str(op) for op in operations),
            objects=list(objects),
            purpose=str(purpose or ""),
            source=source,
            created_at=_now_iso(),
        )
        instruction.grants.append(grant)
        return grant


def current_grants() -> list[Grant]:
    instruction = current_instruction.get()
    return instruction.active_grants() if instruction is not None else []


def effect_allowed_by_current_instruction(effect: Effect, path: object) -> Grant | None:
    """当前指令里是否有 grant 覆盖 effect 对 path 的操作；返回命中的 grant。"""
    instruction = current_instruction.get()
    if instruction is None:
        return None
    for grant in instruction.active_grants():
        if instruction_allows_path(effect, path, grant):
            return grant
    return None


# ---------------------------------------------------------------------------
# R9-H13 §3.H（F6）：可委派范围的事实描述、逐操作逐对象覆盖核对与结构化申请
# ---------------------------------------------------------------------------
#
# 子智能体范围是父级「当前有效要求 + 当前模式 + 已核实授权」的明确子集：
# * 形成：父级 child_scope 只追加当前要求版本下仍有效的核实授权（引擎侧）；
# * 边界：对象/操作/要求版本/用途/内容指纹随 grant 保留，撤销后立即失效；
# * 不足：子智能体先向主智能体提交结构化申请（不弹用户窗），主智能体也没有
#   权限时才由主智能体请求用户；状态机 requested → granted / revoked / superseded。


def normalize_grant_operation(value: object) -> str | None:
    key = str(value or "").strip().lower()
    if not key:
        return None
    return {
        "read": OPERATION_READ,
        "write": OPERATION_WRITE,
        "create": OPERATION_CREATE,
        "delete": OPERATION_DELETE,
        "execute": OPERATION_EXECUTE,
        "shell": OPERATION_EXECUTE,
        "install": OPERATION_INSTALL_DEPS,
        "install_deps": OPERATION_INSTALL_DEPS,
        "egress": OPERATION_NETWORK_EGRESS,
        "network_egress": OPERATION_NETWORK_EGRESS,
        "system_change": "system_change",
    }.get(key)


def grant_is_active(grant: object) -> bool:
    checker = getattr(grant, "is_active", None)
    if callable(checker):
        return bool(checker())
    return not bool(getattr(grant, "revoked", False))


def active_grant_objects(grants: Iterable[object] = ()) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    objects: list[dict[str, str]] = []
    for grant in grants or ():
        if not grant_is_active(grant):
            continue
        for item in getattr(grant, "objects", ()) or ():
            pair = (str(getattr(item, "kind", "")), str(getattr(item, "value", "")))
            if pair[1] and pair not in seen:
                seen.add(pair)
                objects.append({"kind": pair[0], "value": pair[1]})
    return objects


def active_grant_operations(grants: Iterable[object] = ()) -> list[str]:
    operations: set[str] = set()
    for grant in grants or ():
        if not grant_is_active(grant):
            continue
        operations.update(str(op) for op in (getattr(grant, "operations", ()) or ()))
    return sorted(operations)


def describe_scope(scope: object | None) -> dict:
    """结构化描述子范围（审批载荷/子任务结果/审计共用同一事实）。"""
    if scope is None:
        return {}
    grants: list[dict] = []
    for grant in getattr(scope, "grants", ()) or ():
        objects = [
            {"kind": str(getattr(item, "kind", "")), "value": str(getattr(item, "value", ""))}
            for item in (getattr(grant, "objects", ()) or ())
        ]
        grants.append(
            {
                "grant_id": str(getattr(grant, "grant_id", "") or ""),
                "operations": sorted(str(op) for op in (getattr(grant, "operations", ()) or ())),
                "objects": objects,
                "source": str(getattr(grant, "source", "") or ""),
                "purpose": str(getattr(grant, "purpose", "") or ""),
                "requirement_id": getattr(grant, "requirement_id", None),
                "requirement_version": getattr(grant, "requirement_version", None),
                "source_record_id": getattr(grant, "source_record_id", None),
                "fingerprint": getattr(grant, "fingerprint", None),
                "limits": list(getattr(grant, "limits", ()) or ()),
                "revoked": not grant_is_active(grant),
            }
        )
    active = [grant for grant in (getattr(scope, "grants", ()) or ()) if grant_is_active(grant)]
    return {
        "scope_id": str(getattr(scope, "scope_id", "") or ""),
        "parent_instruction_id": getattr(scope, "parent_instruction_id", None),
        "depth": int(getattr(scope, "depth", 0) or 0),
        "purpose": str(getattr(scope, "purpose", "") or ""),
        "grant_count": len(grants),
        "grants": grants,
        "operations": active_grant_operations(active),
        "objects": active_grant_objects(active),
        "revocable": True,
    }


def scope_covers(grants: Iterable[object], operations: Iterable[object], objects: Iterable[object]) -> bool:
    """申请/重试事实是否已被当前子范围逐操作逐对象覆盖（不按文字猜测）。"""
    normalized_operations = [
        op for op in (normalize_grant_operation(item) for item in operations or ()) if op
    ]
    if not normalized_operations:
        return False
    active = [grant for grant in (grants or ()) if grant_is_active(grant)]
    if not active:
        return False
    for raw in objects or ():
        if isinstance(raw, dict):
            kind = str(raw.get("kind") or "path").strip().lower()
            value = str(raw.get("value") or "").strip()
        else:
            kind = str(getattr(raw, "kind", "path") or "path").strip().lower()
            value = str(getattr(raw, "value", "") or "").strip()
        if not value:
            return False
        for operation in normalized_operations:
            for grant in active:
                allowed = False
                if kind in {"path", "path_prefix", "directory"}:
                    # directory 请求按精确 path 语义与 grant 对象比较（不擅自放大成前缀）。
                    allowed = grant.allows(operation, path=value) or (
                        kind == "path_prefix"
                        and grant.allows(operation, path=value)
                    )
                elif kind in {"program", "program_name"}:
                    allowed = operation == OPERATION_EXECUTE and grant.allows(
                        OPERATION_EXECUTE, program=value
                    )
                elif kind in {"service", "url_host"}:
                    allowed = operation == OPERATION_NETWORK_EGRESS and grant.allows(
                        OPERATION_NETWORK_EGRESS, url_host=value
                    )
                if allowed:
                    break
            if not allowed:
                return False
    return True


def bind_verified_grants(
    grants: Iterable[object],
    records: Iterable[object],
    *,
    requirement_id: str,
    requirement_version: int | None,
    active_check_factory: Callable[[str], Callable[[], bool]] | None = None,
) -> tuple[Any, ...]:
    """把核实授权记录的对象/版本/内容/条件边界绑定到子 scope 的 grant 上。

    引擎形成的 verified grant 只有 grant_id（含记录 id 前缀）；这里补上
    requirement_id/version、记录 id、内容指纹、限制与实时撤销核对，撤销/改版后
    读取也必须立即停止（F2）。记录不存在时保持原 grant（不伪造事实）。
    """
    by_prefix: dict[str, object] = {}
    for record in records or ():
        record_id = str(getattr(record, "record_id", "") or "")
        if record_id:
            by_prefix[record_id[:24]] = record
    bound: list[Any] = []
    for grant in grants or ():
        grant_id = str(getattr(grant, "grant_id", "") or "")
        record = None
        for prefix in ("grant_verified_read_", "grant_verified_"):
            if grant_id.startswith(prefix):
                record = by_prefix.get(grant_id[len(prefix):])
                break
        if record is not None:
            record_id = str(getattr(record, "record_id", "") or "")
            try:
                grant.requirement_id = str(requirement_id)
                grant.requirement_version = (
                    int(requirement_version) if requirement_version is not None else None
                )
                grant.source_record_id = record_id
                grant.fingerprint = str(getattr(record, "fingerprint", "") or "")
                grant.limits = tuple(str(item) for item in (getattr(record, "limits", ()) or ()))
                if active_check_factory is not None and record_id:
                    grant.active_check = active_check_factory(record_id)
            except (AttributeError, TypeError, ValueError):
                # 非本模块 Grant 形状（旧 fake）：保持原样，不伪造边界事实。
                pass
        bound.append(grant)
    return tuple(bound)


SCOPE_REQUEST_REQUESTED = "requested"
SCOPE_REQUEST_GRANTED = "granted"
SCOPE_REQUEST_REVOKED = "revoked"
SCOPE_REQUEST_SUPERSEDED = "superseded"
SCOPE_REQUEST_DENIED = "denied"
SCOPE_REQUEST_STATES = frozenset(
    {
        SCOPE_REQUEST_REQUESTED,
        SCOPE_REQUEST_GRANTED,
        SCOPE_REQUEST_REVOKED,
        SCOPE_REQUEST_SUPERSEDED,
        SCOPE_REQUEST_DENIED,
    }
)


@dataclass
class ScopeRequest:
    """子智能体→主智能体的结构化范围申请（不是一句错误文字）。"""

    request_id: str
    requirement_id: str
    requirement_version: int | None
    mode: str
    purpose: str
    operations: tuple[str, ...] = ()
    objects: tuple[Scope, ...] = ()
    reason: str = ""
    state: str = SCOPE_REQUEST_REQUESTED
    escalation: str = "main_agent"  # main_agent | user
    attempts: int = 1
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    grants: tuple[dict, ...] = ()
    resolution: str = ""

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "state": self.state,
            "requirement_id": self.requirement_id,
            "requirement_version": self.requirement_version,
            "mode": self.mode,
            "purpose": self.purpose,
            "operations": list(self.operations),
            "objects": [item.as_dict() for item in self.objects],
            "reason": self.reason,
            "escalation": self.escalation,
            "attempts": int(self.attempts),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "grants": [dict(item) for item in self.grants],
            "resolution": self.resolution,
        }


class ScopeRequestLedger:
    """范围申请状态机（requested → granted / revoked / superseded / denied）。

    只按 (requirement_id, purpose) 归并同一次子任务目标的申请；跨要求/跨目标不合并。
    状态变化只由真实事实触发：子范围覆盖申请内容 = granted；要求改版/失效 =
    superseded；已授予但当前范围不再覆盖（授权记录被撤销等）= revoked。
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ScopeRequest] = {}

    @staticmethod
    def _key(requirement_id: object, purpose: object) -> tuple[str, str]:
        return (str(requirement_id or ""), str(purpose or ""))

    def request(
        self,
        *,
        requirement_id: str,
        requirement_version: int | None,
        mode: str,
        purpose: str,
        operations: Iterable[object] = (),
        objects: Iterable[object] = (),
        reason: str = "",
        escalation: str = "main_agent",
    ) -> ScopeRequest:
        key = self._key(requirement_id, purpose)
        existing = self._items.get(key)
        normalized_objects: list[Scope] = []
        for raw in objects or ():
            if isinstance(raw, dict):
                kind = str(raw.get("kind") or "path").strip().lower()
                value = str(raw.get("value") or "").strip()
            else:
                kind = str(getattr(raw, "kind", "path") or "path").strip().lower()
                value = str(getattr(raw, "value", "") or "").strip()
            if value:
                normalized_objects.append(Scope(kind, value))
        normalized_operations = tuple(
            dict.fromkeys(
                op
                for op in (normalize_grant_operation(item) or str(item or "").strip() for item in operations or ())
                if op
            )
        )
        if existing is not None and existing.state == SCOPE_REQUEST_REQUESTED:
            existing.operations = tuple(
                dict.fromkeys((*existing.operations, *normalized_operations))
            )
            merged = {(item.kind, item.value): item for item in existing.objects}
            for item in normalized_objects:
                merged.setdefault((item.kind, item.value), item)
            existing.objects = tuple(merged.values())
            existing.requirement_version = requirement_version
            existing.mode = str(mode or "")
            existing.escalation = str(escalation or existing.escalation)
            existing.attempts += 1
            existing.updated_at = _now_iso()
            if reason:
                existing.reason = str(reason)[:500]
            return existing
        request = ScopeRequest(
            request_id="scope_req_" + uuid.uuid4().hex[:12],
            requirement_id=str(requirement_id or ""),
            requirement_version=requirement_version,
            mode=str(mode or ""),
            purpose=str(purpose or ""),
            operations=normalized_operations,
            objects=tuple(normalized_objects),
            reason=str(reason or "")[:500],
            escalation=str(escalation or "main_agent"),
        )
        self._items[key] = request
        return request

    def sync(
        self,
        *,
        requirement_id: str,
        requirement_version: int | None,
        purpose: str,
        grants: Iterable[object],
        requirement_active: bool,
        requirement_current_version: int | None = None,
        grants_detail: Iterable[dict] = (),
    ) -> list[ScopeRequest]:
        """用当前真实范围更新申请状态；返回该目标下的全部申请（供主智能体消费）。"""
        key = self._key(requirement_id, purpose)
        request = self._items.get(key)
        if request is None:
            return []
        current_version = (
            requirement_current_version
            if requirement_current_version is not None
            else requirement_version
        )
        version_changed = (
            request.requirement_version is not None
            and current_version is not None
            and int(request.requirement_version) != int(current_version)
        )
        if not requirement_active:
            request.state = SCOPE_REQUEST_SUPERSEDED
            request.resolution = "用户要求已撤回/失效，不能再用旧授权继续子任务"
        elif version_changed:
            request.state = SCOPE_REQUEST_SUPERSEDED
            request.resolution = "用户要求版本已变化，旧授权不能延续到新版本"
        elif request.operations and request.objects:
            if scope_covers(grants, request.operations, request.objects):
                request.state = SCOPE_REQUEST_GRANTED
                request.grants = tuple(dict(item) for item in (grants_detail or ()))
                request.resolution = "主智能体当前可委派范围已覆盖申请"
            elif request.state == SCOPE_REQUEST_GRANTED:
                request.state = SCOPE_REQUEST_REVOKED
                request.grants = ()
                request.resolution = "授权已被撤销或不再覆盖申请内容"
        request.updated_at = _now_iso()
        return [request]

    def for_requirement(self, requirement_id: str, purpose: str | None = None) -> list[ScopeRequest]:
        rid = str(requirement_id or "")
        return [
            request
            for (req_id, want), request in self._items.items()
            if req_id == rid and (purpose is None or want == str(purpose or ""))
        ]

    def mark(self, request_id: str, state: str, *, resolution: str = "") -> ScopeRequest | None:
        if state not in SCOPE_REQUEST_STATES:
            raise ValueError("非法范围申请状态：" + str(state))
        for request in self._items.values():
            if request.request_id == str(request_id):
                request.state = state
                request.resolution = str(resolution or request.resolution)
                request.updated_at = _now_iso()
                return request
        return None

    def reset(self) -> None:
        self._items.clear()


_SCOPE_REQUESTS = ScopeRequestLedger()


def scope_request_ledger() -> ScopeRequestLedger:
    return _SCOPE_REQUESTS


def reset_scope_request_ledger() -> None:
    _SCOPE_REQUESTS.reset()

