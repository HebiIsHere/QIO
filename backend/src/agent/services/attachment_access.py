"""附件读取/写回/删除的授权接入（契约 §2/§3.1）。

W3 的附件服务只负责文件事实；**权限判定不在这里**。本模块是唯一适配层：

1. 把一次附件操作翻译成契约类型 Effect/Target（只读 READ / 写回 WRITE / 删除用户材料 DELETE）；
2. 调用 W1 的 AuthorizationGateway.authorize（延迟导入 agent.security，W1 合入后自动为真引擎）；
3. 把 allow / approve / deny 翻译成调用方可以直接使用的结论；
4. 网关不可用时：READ 明确标注 degraded（只读读取本身在只读模式也允许），
   WRITE / DELETE **一律拒绝**（绝不因为网关没接入就把写操作放出去）；
   测试用 Fake 网关走同一入口（本模块不复制任何判定逻辑）。

同步上下文（materials 等历史代码）无法 await 网关：由异步边界先把读取授权结果
放进 ContextVar（current_read_grants），同步函数只做"这条附件是否在已授权集合里"的
查表，不自己判定权限。生产路径（turn 编排、API、工具）都会先设置这个集合。
"""

from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 契约类型的延迟导入：W1 的 agent.security 合入后用权威定义；未合入用同形状占位
# （只是数据类型，不含任何判定逻辑）。
# ---------------------------------------------------------------------------

try:  # W1 合入后使用权威类型
    from agent.security.effects import Effect, EffectKind, Target, Verdict  # type: ignore
    from agent.security.operations import PendingOperation  # type: ignore

    _AUTHORITATIVE_TYPES = True
except Exception:  # noqa: BLE001 - W1 未合入：用同形状占位类型
    _AUTHORITATIVE_TYPES = False

    class EffectKind:  # type: ignore[no-redef]
        READ = "read"
        WRITE = "write"
        CREATE = "create"
        DELETE = "delete"
        EXECUTE = "execute"
        INSTALL_DEPS = "install_deps"
        SYSTEM_CHANGE = "system_change"
        NETWORK_EGRESS = "network_egress"
        INTERNAL_DATA = "internal_data"
        TOOL_REGISTER = "tool_register"
        SUBAGENT = "subagent"

    @dataclass(frozen=True)
    class Target:  # type: ignore[no-redef]
        kind: str
        value: str
        identity: str | None = None
        exists: bool | None = None

    @dataclass(frozen=True)
    class Effect:  # type: ignore[no-redef]
        kind: Any
        targets: tuple[Target, ...]
        purpose: str
        tool: str | None = None
        detail: dict = field(default_factory=dict)
        reversible: bool = False

    @dataclass
    class PendingOperation:  # type: ignore[no-redef]
        kind: str
        label: str
        payload: dict
        effect: dict
        fingerprint: dict
        instruction_id: str | None
        instruction_text: str | None
        purpose: str
        mode_at_request: str
        confirmations_required: int = 1

    class Verdict:  # type: ignore[no-redef]
        ALLOW = "allow"
        APPROVE = "approve"
        DENY = "deny"


def authoritative_types() -> bool:
    """是否已用 W1 的权威契约类型（报告/自测用）。"""
    return _AUTHORITATIVE_TYPES


# ---------------------------------------------------------------------------
# 网关定位
# ---------------------------------------------------------------------------


def gateway_of(owner: Any) -> Any | None:
    """从 AppContext / 工具 / app.state.ctx 上取授权网关（W1 接线的唯一事实源）。

    兼容：ctx.authorization / ctx.security / services 注册名 security。
    """
    if owner is None:
        return None
    for name in ("authorization", "security", "auth_gateway", "gateway"):
        found = getattr(owner, name, None)
        if found is not None and callable(getattr(found, "authorize", None)):
            return found
    return None


def gateway_from_request(request: Any) -> Any | None:
    try:
        return gateway_of(request.app.state.ctx)
    except Exception:  # noqa: BLE001 - 拿不到就 None（调用方按未接入处理）
        return None


# ---------------------------------------------------------------------------
# 同步上下文的读取授权集合（异步边界写入，这里只查表）
# ---------------------------------------------------------------------------

current_read_grants: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "qio_attachment_read_grants", default=None
)


def set_read_grants(attachment_ids: Iterable[str]) -> contextvars.Token:
    return current_read_grants.set(frozenset(str(item) for item in attachment_ids))


def reset_read_grants(token: contextvars.Token) -> None:
    try:
        current_read_grants.reset(token)
    except Exception:  # noqa: BLE001 - 上下文已结束时不需要恢复
        pass


def read_allowed(attachment_id: str) -> bool:
    """同步上下文查表：是否已由异步边界授权读取。

    None（尚未预授权，例如测试直调/旧路径）→ True：读取本身不改变任何事实，
    只读模式也允许；写/删不走这个入口。
    """
    grants = current_read_grants.get()
    if grants is None:
        return True
    return str(attachment_id) in grants


# ---------------------------------------------------------------------------
# Effect 构造
# ---------------------------------------------------------------------------


def _path_target(path: str | os.PathLike[str], *, identity: str | None, exists: bool | None) -> Target:
    return Target(kind="path", value=str(path), identity=identity, exists=exists)


def _effect(kind: Any, targets: Sequence[Target], *, purpose: str, tool: str | None, detail: dict | None = None, reversible: bool = False) -> Effect:
    return Effect(
        kind=kind,
        targets=tuple(targets),
        purpose=str(purpose or ""),
        tool=tool,
        detail=dict(detail or {}),
        reversible=bool(reversible),
    )


def read_effect(tool: str, *, path: str | os.PathLike[str], identity: str | None = None, purpose: str = "读取附件内容", detail: dict | None = None) -> Effect:
    return _effect(
        EffectKind.READ,
        [_path_target(path, identity=identity, exists=True)],
        purpose=purpose,
        tool=tool,
        detail=detail,
        reversible=True,
    )


def write_effect(tool: str, *, path: str | os.PathLike[str], identity: str | None, purpose: str, content_sha256: str | None = None, exists: bool | None = True, detail: dict | None = None) -> Effect:
    data = dict(detail or {})
    if content_sha256:
        data["content_sha256"] = str(content_sha256)
    return _effect(
        EffectKind.WRITE,
        [_path_target(path, identity=identity, exists=exists)],
        purpose=purpose,
        tool=tool,
        detail=data,
    )


def delete_effect(tool: str, *, targets: Sequence[tuple[str, str | None]], purpose: str, detail: dict | None = None) -> Effect:
    """targets = [(path, identity)]。删除用户材料必须走这个 effect。"""
    return _effect(
        EffectKind.DELETE,
        [
            _path_target(path, identity=identity, exists=True)
            for path, identity in targets
        ],
        purpose=purpose,
        tool=tool,
        detail=detail,
    )


def effect_to_dict(effect: Any) -> dict:
    kind = getattr(effect, "kind", None)
    raw = getattr(kind, "value", kind)
    targets = []
    for item in getattr(effect, "targets", ()) or ():
        targets.append(
            {
                "kind": getattr(item, "kind", ""),
                "value": getattr(item, "value", ""),
                "identity": getattr(item, "identity", None),
                "exists": getattr(item, "exists", None),
            }
        )
    return {
        "kind": str(raw or ""),
        "targets": targets,
        "purpose": getattr(effect, "purpose", ""),
        "tool": getattr(effect, "tool", None),
        "detail": dict(getattr(effect, "detail", {}) or {}),
        "reversible": bool(getattr(effect, "reversible", False)),
    }


# ---------------------------------------------------------------------------
# 网关调用与结论翻译
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def verdict_of(decision: Any) -> str:
    if decision is None:
        return "deny"
    if isinstance(decision, dict):
        raw = decision.get("verdict")
    else:
        raw = getattr(decision, "verdict", decision)
    raw = getattr(raw, "value", raw)
    text = str(raw or "").strip().lower()
    if text in ("allow", "approve", "deny"):
        return text
    return "deny"


def reason_of(decision: Any, default: str = "") -> str:
    if isinstance(decision, dict):
        return str(decision.get("reason") or default or "").strip()
    return str(getattr(decision, "reason", "") or default or "").strip()


def approval_id_of(decision: Any) -> str | None:
    value = decision.get("approval_id") if isinstance(decision, dict) else getattr(decision, "approval_id", None)
    return str(value) if value else None


async def authorize(gateway: Any, effect: Effect, *, ctx: Any = None) -> dict:
    """调用统一网关；返回可直接使用的结论（不是判定逻辑）。"""
    if gateway is None or not callable(getattr(gateway, "authorize", None)):
        return {
            "verdict": "deny" if _effect_kind(effect) != "read" else "allow",
            "allowed": _effect_kind(effect) == "read",
            "degraded": True,
            "reason": (
                "统一授权网关未接入（W1 未合入）：只读读取放行，写/删操作一律拒绝"
                if _effect_kind(effect) == "read"
                else "统一授权网关未接入（W1 未合入），不能执行写/删操作"
            ),
            "approval_id": None,
            "decision": None,
        }
    try:
        result = gateway.authorize(effect) if ctx is None else gateway.authorize(effect, ctx=ctx)
        decision = await _maybe_await(result)
    except Exception as exc:  # noqa: BLE001 - 网关故障：按拒绝处理（失败关闭）
        return {
            "verdict": "deny",
            "allowed": False,
            "degraded": False,
            "reason": f"授权网关调用失败：{type(exc).__name__}",
            "approval_id": None,
            "decision": None,
        }
    verdict = verdict_of(decision)
    return {
        "verdict": verdict,
        "allowed": verdict == "allow",
        "degraded": False,
        "reason": reason_of(decision, "统一授权网关没有给出原因"),
        "approval_id": approval_id_of(decision),
        "decision": decision,
    }


def _effect_kind(effect: Any) -> str:
    kind = getattr(effect, "kind", "")
    return str(getattr(kind, "value", kind) or "")


async def authorize_read(gateway: Any, *, path: Any, identity: str | None = None, purpose: str = "读取附件内容", tool: str = "attachment.read", detail: dict | None = None) -> dict:
    return await authorize(gateway, read_effect(tool, path=path, identity=identity, purpose=purpose, detail=detail))


async def authorize_write(gateway: Any, *, path: Any, identity: str | None, purpose: str, content_sha256: str | None = None, tool: str = "attachment.write_back", detail: dict | None = None) -> dict:
    return await authorize(
        gateway,
        write_effect(tool, path=path, identity=identity, purpose=purpose, content_sha256=content_sha256, detail=detail),
    )


async def authorize_delete(gateway: Any, *, targets: Sequence[tuple[str, str | None]], purpose: str, tool: str = "attachment.delete_material", detail: dict | None = None) -> dict:
    return await authorize(gateway, delete_effect(tool, targets=targets, purpose=purpose, detail=detail))


def instruction_snapshot(gateway: Any) -> dict:
    if gateway is None:
        return {}
    try:
        getter = getattr(gateway, "instruction", None)
        instruction = getter() if callable(getter) else getter
    except Exception:  # noqa: BLE001 - 指令读不到不阻断审批单构造
        return {}
    if instruction is None:
        return {}
    return {
        "instruction_id": getattr(instruction, "instruction_id", None),
        "instruction_text": getattr(instruction, "text", None),
    }


def mode_value(gateway: Any) -> str:
    if gateway is None:
        return "restricted"
    try:
        getter = getattr(gateway, "mode", None)
        raw = getter() if callable(getter) else getter
        raw = getattr(raw, "value", raw)
        return str(raw or "restricted")
    except Exception:  # noqa: BLE001 - 读不到按受限（保守）
        return "restricted"


def build_pending_operation(*, kind: str, label: str, effect: Effect, payload: dict, fingerprint: dict | None = None, purpose: str = "", instruction: dict | None = None, mode_at_request: str = "restricted", confirmations_required: int = 1) -> Any:
    """按契约 §5.3 构造可恢复的待审批操作（字段可序列化；不含判定）。"""
    snapshot = dict(instruction or {})
    values = {
        "kind": str(kind),
        "label": str(label),
        "payload": dict(payload or {}),
        "effect": effect_to_dict(effect),
        "fingerprint": dict(fingerprint or {}),
        "instruction_id": snapshot.get("instruction_id"),
        "instruction_text": snapshot.get("instruction_text"),
        "purpose": str(purpose or getattr(effect, "purpose", "") or label),
        "mode_at_request": str(mode_at_request or "restricted"),
        "confirmations_required": int(confirmations_required),
    }
    try:
        fields = set(getattr(PendingOperation, "__dataclass_fields__", {}) or {})
        kwargs = {key: value for key, value in values.items() if key in fields}
        return PendingOperation(**kwargs)
    except Exception:  # noqa: BLE001 - 类型差异最终退回最小构造
        return PendingOperation(kind=str(kind), label=str(label)) if not _AUTHORITATIVE_TYPES else {"kind": str(kind), "label": str(label)}


async def submit(gateway: Any, operation: Any, *, ctx: Any = None) -> Any | None:
    if gateway is None:
        return None
    fn = getattr(gateway, "submit", None)
    if not callable(fn):
        return None
    try:
        result = fn(operation) if ctx is None else fn(operation, ctx=ctx)
        return await _maybe_await(result)
    except Exception:  # noqa: BLE001 - 提交失败由调用方如实报告
        return None


def ticket_summary(ticket: Any) -> dict:
    return {
        "approval_id": str(getattr(ticket, "approval_id", "") or ""),
        "state": str(getattr(ticket, "state", "waiting") or "waiting"),
        "expires_at": str(getattr(ticket, "expires_at", "") or ""),
        "confirmations_required": int(getattr(ticket, "confirmations_required", 1) or 1),
    }
