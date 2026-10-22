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
from typing import Iterable

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
    source: str  # user_explicit | user_approved
    created_at: str
    revoked: bool = False

    def allows(
        self,
        operation: str,
        *,
        path: object = None,
        program: object = None,
        url_host: object = None,
    ) -> bool:
        """这个 grant 是否覆盖一次具体操作（对象必须命中，操作必须列出）。"""
        if self.revoked or operation not in self.operations:
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
OPERATION_WRITE = "write"
OPERATION_CREATE = "create"
OPERATION_DELETE = "delete"
OPERATION_EXECUTE = "execute"
OPERATION_INSTALL_DEPS = "install_deps"
OPERATION_NETWORK_EGRESS = "network_egress"

_EFFECT_TO_OPERATIONS = {
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
