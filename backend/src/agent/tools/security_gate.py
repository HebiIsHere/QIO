"""统一授权网关的执行层适配（W2）。

契约 §2/§3 要求：所有真实操作入口（文件工具、命令、脚本、自建工具、子进程、
子智能体）共享同一个判定。工具层不自己判权限，只负责：

1. 把一次操作翻译成契约类型 Effect/Target（本模块 effect_* 构造函数）；
2. 调用 AuthorizationGateway.authorize（`authorize`）/ submit（`submit_operation`）；
3. 把 allow / approve / deny 翻译成工具结果（`denied_result` / `pending_result`）；
4. 提供执行层需要的辅助：文件身份重核对、原子写、内部数据路径识别、
   子智能体 AgentScope 构造与上下文传播。

**没有任何权限判定逻辑**：verdict 只来自 W1 的引擎。本模块在 gateway 为 None 时
返回 None，工具回退到旧的 ComputerSandbox 判定路径（只用于旧测试与尚未接线的
构造点；接入应用后 W1 的 app.py 会把 security 服务注入到所有工具）。

注入约定（需要 W1 在 services/app.py 接线）：
* `self.services.register("security", self.security)`（AuthorizationGateway 实例）；
* 工具声明 `inject = ["computer", "approvals", "security"]`，注册时自动 attach；
* 网关需要提供 `authorize(effect, *, ctx=None)`、`submit(operation, *, ctx=None)`、
  `mode()`、可选 `instruction()` / `internal_data_paths()` / `child_scope(purpose=...)`。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Sequence

from agent.tools.base import ToolResult
from agent.tools.security_types import (
    AgentScope,
    AuthDecision,
    Effect,
    EffectKind,
    ApprovalTicket,
    PendingOperation,
    Target,
    Verdict,
    normalize_mode,
)

# 新权限模式（契约 §1）。旧值 default/plan/accept-edits/bypass 一律经 normalize_mode 映射。
MODE_READ_ONLY = "read_only"
MODE_RESTRICTED = "restricted"
MODE_UNRESTRICTED = "unrestricted"

#: 内部运行数据需要连续三次确认（契约 §4）。
INTERNAL_DATA_CONFIRMATIONS = 3

#: 子智能体只允许一层（契约 §2.3/§2.4）：「超出父级范围由主智能体统一向用户申请」。
SUBAGENT_MAX_DEPTH = 2


def gateway_of(owner: Any) -> Any | None:
    """工具实例上注入的授权网关（服务名 security；没有就 None）。"""
    return getattr(owner, "security", None)


def mode_value(value: Any) -> str:
    """任意模式表示 → 新值字符串（旧值映射、未知值不静默提高）。"""
    if value is None:
        return MODE_RESTRICTED
    raw = getattr(value, "value", value)
    try:
        return normalize_mode(str(raw or "")).value
    except Exception:  # noqa: BLE001 - 模式读不到按受限（保守）
        return MODE_RESTRICTED


def mode_of(gateway: Any | None, computer: Any | None = None) -> str:
    """当前权限模式：优先引擎（唯一事实源），旧接线时退回 ComputerSandbox。"""
    if gateway is not None:
        try:
            getter = getattr(gateway, "mode", None)
            if callable(getter):
                return mode_value(getter())
            if getter is not None:
                return mode_value(getter)
        except Exception:  # noqa: BLE001 - 读模式失败按保守
            pass
    if computer is not None:
        return mode_value(getattr(computer, "mode", None))
    return MODE_RESTRICTED


def verdict_of(decision: Any) -> str:
    """AuthDecision → "allow" | "approve" | "deny"（也兼容 fake gateway 返回字符串）。"""
    if decision is None:
        return "deny"
    raw = getattr(decision, "verdict", decision)
    raw = getattr(raw, "value", raw)
    text = str(raw or "").strip().lower()
    if text in ("allow", "approve", "deny"):
        return text
    return "deny"


def reason_of(decision: Any, default: str = "") -> str:
    return str(getattr(decision, "reason", "") or default or "").strip()


def approval_id_of(decision: Any) -> str | None:
    value = getattr(decision, "approval_id", None)
    return str(value) if value else None


# --------------------------------------------------------------------------
# Effect / Target 构造
# --------------------------------------------------------------------------


def path_target(
    path: str | os.PathLike[str],
    *,
    identity: str | None = None,
    exists: bool | None = None,
) -> Target:
    return Target(kind="path", value=str(path), identity=identity, exists=exists)


def program_target(program: str) -> Target:
    return Target(kind="program", value=str(program))


def record_target(value: str) -> Target:
    return Target(kind="record", value=str(value))


def make_effect(
    kind: Any,
    targets: Sequence[Target] = (),
    *,
    purpose: str = "",
    tool: str | None = None,
    detail: dict | None = None,
    reversible: bool = False,
) -> Effect:
    return Effect(
        kind=kind if isinstance(kind, EffectKind) else EffectKind(kind),
        targets=tuple(targets),
        purpose=str(purpose or ""),
        tool=tool,
        detail=dict(detail or {}),
        reversible=bool(reversible),
    )


def read_effect(tool: str, path: Any, *, purpose: str = "读取文件", detail: dict | None = None) -> Effect:
    return make_effect(
        EffectKind.READ,
        [path_target(path, exists=os.path.exists(str(path)))],
        purpose=purpose,
        tool=tool,
        detail=detail or {},
        reversible=True,
    )


def write_effect(
    tool: str,
    path: Any,
    *,
    purpose: str,
    content_sha256: str | None = None,
    identity: str | None = None,
    exists: bool | None = None,
    detail: dict | None = None,
    internal: bool = False,
) -> Effect:
    data = dict(detail or {})
    if content_sha256:
        data["content_sha256"] = content_sha256
    kind = EffectKind.INTERNAL_DATA if internal else EffectKind.WRITE
    return make_effect(
        kind,
        [path_target(path, identity=identity, exists=exists)],
        purpose=purpose,
        tool=tool,
        detail=data,
    )


def execute_effect(
    tool: str,
    program: str,
    args: Sequence[str] = (),
    *,
    cwd: str | None = None,
    shell: bool = False,
    purpose: str = "执行程序",
    detail: dict | None = None,
) -> Effect:
    targets = [program_target(program)]
    if cwd:
        targets.append(path_target(cwd))
    data = {"argv": [str(program), *[str(a) for a in args]], "shell": bool(shell)}
    data.update(detail or {})
    return make_effect(
        EffectKind.EXECUTE, targets, purpose=purpose, tool=tool, detail=data
    )


def script_effect(
    tool: str,
    *,
    purpose: str,
    code_digest: str = "",
    detail: dict | None = None,
) -> Effect:
    """脚本/自建工具执行：没有可解析的 argv，用 record 目标 + 代码摘要。"""
    data = {"script": True, "code_sha256": str(code_digest or "")}
    data.update(detail or {})
    return make_effect(
        EffectKind.EXECUTE,
        [record_target(f"script:{tool}")],
        purpose=purpose,
        tool=tool,
        detail=data,
    )


def tool_register_effect(tool: str, name: str, *, detail: dict | None = None) -> Effect:
    return make_effect(
        EffectKind.TOOL_REGISTER,
        [record_target(f"tool:{name}")],
        purpose=f"注册自建工具 {name}",
        tool=tool,
        detail=detail or {},
    )


def install_deps_effect(tool: str, packages: Sequence[str], *, target: Any, detail: dict | None = None) -> Effect:
    return make_effect(
        EffectKind.INSTALL_DEPS,
        [path_target(target)],
        purpose="准备 QIO 专用依赖环境（只装声明的包）",
        tool=tool,
        detail={"packages": [str(p) for p in packages], **dict(detail or {})},
    )


def subagent_effect(tool: str, *, definition_name: str, purpose: str = "创建子智能体") -> Effect:
    return make_effect(
        EffectKind.SUBAGENT,
        [record_target(f"subagent:{definition_name}")],
        purpose=purpose,
        tool=tool,
        detail={"definition": definition_name},
    )


def effect_to_dict(effect: Any) -> dict:
    """审计/审批载荷用的可序列化 effect（不复制任何判定）。"""
    kind = getattr(effect, "kind", None)
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
        "kind": getattr(kind, "value", str(kind or "")),
        "targets": targets,
        "purpose": getattr(effect, "purpose", ""),
        "tool": getattr(effect, "tool", None),
        "detail": dict(getattr(effect, "detail", {}) or {}),
        "reversible": bool(getattr(effect, "reversible", False)),
    }


def effect_from_dict(data: dict | None) -> Effect | None:
    """把审计/审批载荷里的 effect 快照还原为 Effect（只用于执行前重核对）。

    还原失败返回 None：调用方必须按"无法核对"处理，不得默认允许。
    """
    if not isinstance(data, dict):
        return None
    kind_raw = str(data.get("kind") or "")
    for kind in EffectKind:
        if kind.value == kind_raw:
            break
    else:
        return None
    targets = []
    for item in data.get("targets") or ():
        if not isinstance(item, dict):
            continue
        targets.append(
            Target(
                kind=str(item.get("kind") or ""),
                value=str(item.get("value") or ""),
                identity=item.get("identity"),
                exists=item.get("exists"),
            )
        )
    return Effect(
        kind=kind,
        targets=tuple(targets),
        purpose=str(data.get("purpose") or ""),
        tool=data.get("tool"),
        detail=dict(data.get("detail") or {}),
        reversible=bool(data.get("reversible", False)),
    )


# --------------------------------------------------------------------------
# 网关调用
# --------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


async def authorize(gateway: Any | None, effect: Effect, *, ctx: Any = None) -> AuthDecision | None:
    """调用统一入口；gateway 为 None 时返回 None（旧路径回退由调用方决定）。"""
    if gateway is None:
        return None
    fn = getattr(gateway, "authorize", None)
    if not callable(fn):
        return None
    if ctx is None:
        try:
            result = fn(effect)
        except TypeError:
            # 个别实现把 ctx 设为必填关键字；契约里它是可选的，这里只做一次兼容重试。
            result = fn(effect, ctx=None)
    else:
        result = fn(effect, ctx=ctx)
    return await _maybe_await(result)


def submit_operation(gateway: Any | None, operation: PendingOperation, *, ctx: Any = None) -> Any | None:
    """提交待审批操作；网关不支持 submit 时返回 None（调用方不得自行阻塞等待）。"""
    if gateway is None:
        return None
    fn = getattr(gateway, "submit", None)
    if not callable(fn):
        return None
    return fn(operation, ctx=ctx) if ctx is not None else fn(operation)


async def submit_pending(
    gateway: Any,
    operation: PendingOperation,
    *,
    ctx: Any = None,
) -> ApprovalTicket | Any | None:
    result = submit_operation(gateway, operation, ctx=ctx)
    return await _maybe_await(result)


def instruction_snapshot(gateway: Any | None) -> dict:
    """当前用户指令（审批单要带 instruction id + 原话；拿不到就不编）。"""
    if gateway is None:
        return {}
    try:
        getter = getattr(gateway, "instruction", None)
        instruction = getter() if callable(getter) else getter
    except Exception:  # noqa: BLE001 - 指令读不到不阻断操作
        return {}
    if instruction is None:
        return {}
    return {
        "instruction_id": getattr(instruction, "instruction_id", None),
        "instruction_text": getattr(instruction, "text", None),
    }


def build_pending_operation(
    *,
    kind: str,
    label: str,
    payload: dict,
    effect: Effect,
    fingerprint: dict | None = None,
    purpose: str = "",
    mode_at_request: str = "",
    confirmations_required: int = 1,
    instruction: dict | None = None,
) -> PendingOperation:
    """按契约 §5.3 构造待审批操作（字段可序列化、可恢复、可重核对）。"""
    snapshot = dict(instruction or {})
    try:
        return PendingOperation(
            kind=str(kind),
            label=str(label),
            payload=dict(payload or {}),
            effect=effect_to_dict(effect),
            fingerprint=dict(fingerprint or {}),
            instruction_id=snapshot.get("instruction_id"),
            instruction_text=snapshot.get("instruction_text"),
            purpose=str(purpose or effect.purpose or label),
            mode_at_request=mode_value(mode_at_request),
            confirmations_required=int(confirmations_required),
        )
    except TypeError:
        # W1 的 dataclass 字段顺序/默认值若不同，按其字段名逐项构造。
        kwargs = {
            "kind": str(kind),
            "label": str(label),
            "payload": dict(payload or {}),
            "effect": effect_to_dict(effect),
            "fingerprint": dict(fingerprint or {}),
            "instruction_id": snapshot.get("instruction_id"),
            "instruction_text": snapshot.get("instruction_text"),
            "purpose": str(purpose or effect.purpose or label),
            "mode_at_request": mode_value(mode_at_request),
            "confirmations_required": int(confirmations_required),
        }
        fields = set(getattr(PendingOperation, "__dataclass_fields__", {}) or {})
        try:
            return PendingOperation(**{k: v for k, v in kwargs.items() if k in fields})
        except Exception:  # noqa: BLE001 - 最终兜底：至少把 kind/label 交出去
            return PendingOperation(kind=str(kind))  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# 判定结果 → 工具结果
# --------------------------------------------------------------------------


def denied_result(decision: Any, default_reason: str) -> ToolResult:
    reason = reason_of(decision, default_reason) or default_reason
    return ToolResult(
        ok=False,
        error=reason,
        category="permission",
        recoverable=False,
        code="permission_denied",
    )


def pending_result(
    ticket: Any,
    *,
    operation_label: str,
    confirmations_required: int = 1,
    extra: dict | None = None,
) -> ToolResult:
    """审批待执行：工具立即返回，批准后由审批执行器真正执行（契约 §2.1/§5.1）。"""
    approval_id = str(getattr(ticket, "approval_id", "") or "")
    state = str(getattr(ticket, "state", "waiting") or "waiting")
    expires_at = str(getattr(ticket, "expires_at", "") or "")
    confirmed = int(getattr(ticket, "confirmations_required", confirmations_required) or confirmations_required)
    if confirmed >= INTERNAL_DATA_CONFIRMATIONS:
        content = (
            f"已提交审批：{operation_label}。这是 QIO 内部运行数据，直接改写可能损坏应用，"
            f"需要你连续确认三次（当前 0/{confirmed}）；任一步取消都会停止，"
            "确认完成前不会执行。"
        )
    else:
        content = f"已提交审批：{operation_label}。批准后会自动执行；本次调用尚未修改任何内容。"
    data = {
        "approval_id": approval_id,
        "state": state,
        "expires_at": expires_at,
        "confirmations_required": confirmed,
        "pending_approval": True,
        "operation": operation_label,
    }
    data.update(extra or {})
    return ToolResult(
        ok=True,
        status="awaiting_approval",
        code="pending_approval",
        content=content,
        data=data,
        category=None,
        recoverable=False,
    )


# --------------------------------------------------------------------------
# 内部运行数据（契约 §4）
# --------------------------------------------------------------------------


def internal_data_roots(gateway: Any | None) -> list[Path]:
    """引擎提供的内部运行数据根（拿不到就空：绝不按常见路径瞎猜）。"""
    if gateway is None:
        return []
    raw = None
    for name in ("internal_data_paths", "_internal_data_paths", "internal_data_path_provider"):
        value = getattr(gateway, name, None)
        if value is not None:
            raw = value
            break
    try:
        if callable(raw):
            raw = raw()
    except Exception:  # noqa: BLE001 - 路径清单读不到时按空（不伪造保护范围）
        return []
    if raw is None:
        return []
    roots: list[Path] = []
    for item in raw if isinstance(raw, (list, tuple, set)) else [raw]:
        try:
            roots.append(Path(str(item)).resolve())
        except (OSError, ValueError):
            continue
    return roots


def match_internal_data(gateway: Any | None, path: Any) -> Path | None:
    """目标是否属于内部运行数据；返回命中的根/对象路径（供审批单写明范围）。

    R9-H4 §8：除真实注册路径外，还要按 identity+birth 生命周期识别硬链接/改名/
    移动后的同一对象；普通同名/同内容复制件不命中。识别失败时不得把已登记对象
    静默降级为普通文件。
    """
    try:
        candidate = Path(str(path)).resolve()
    except (OSError, ValueError):
        return None
    roots = internal_data_roots(gateway)
    for root in roots:
        try:
            if candidate == root or candidate.is_relative_to(root):
                return root
        except (OSError, ValueError):
            continue
    if gateway is None:
        return None
    tokens = ()
    known = ()
    lookup = None
    try:
        tokens_fn = getattr(gateway, "internal_data_identity_tokens", None)
        if callable(tokens_fn):
            tokens = tuple(tokens_fn() or ())
        known_fn = getattr(gateway, "internal_data_identity_known_paths", None)
        if callable(known_fn):
            known = tuple(known_fn() or ())
        lookup = getattr(gateway, "internal_data_identity_lookup", None)
        if not callable(lookup):
            lookup = None
    except Exception:  # noqa: BLE001 - 身份登记读失败不能伪造普通文件
        return None
    if not tokens and not known and lookup is None:
        return None
    try:
        from agent.security import internal_data as internal_data_module
        from agent.services.file_identity import identity_of

        # engine 公开方法是 (identity_key, birth_key=None)；internal_data 回调会传
        # FileIdentity。这里适配两者，避免把 repr 当身份键导致 lookup 永远 unknown。
        raw_lookup = lookup

        def _lookup_adapter(identity: object):
            if raw_lookup is None:
                return {"state": "unknown", "reason": "没有生命周期查询"}
            if hasattr(identity, "key"):
                identity_key = str(getattr(identity, "key") or "")
                birth_key = getattr(identity, "birth_key", None)
            elif isinstance(identity, dict):
                identity_key = str(
                    identity.get("key")
                    or identity.get("identity_key")
                    or ""
                )
                birth_key = identity.get("birth_key")
            else:
                identity_key = str(identity or "")
                birth_key = None
            try:
                return raw_lookup(identity_key, birth_key)
            except TypeError:
                return raw_lookup(identity_key)

        matched = internal_data_module.is_internal_data_object(
            candidate,
            roots,
            identity_keys=tokens,
            known_paths=known,
            identity_of=identity_of,
            identity_lookup=_lookup_adapter if raw_lookup is not None else None,
        )
    except Exception:  # noqa: BLE001 - 身份核对异常不能把可能受保护对象当普通文件
        return candidate if known else None
    if matched:
        # 对象路径作为“命中位置”返回（可能不在原注册根下，但仍属于同一内部对象）。
        return candidate
    return None


def internal_data_mentions(gateway: Any | None, text: str) -> list[str]:
    """脚本内容里是否**明确提到**内部数据路径（执行前能证明的只有这一种）。

    诚实边界：任意脚本可以不写路径、按运行时拼出来再写。那一步无法在执行前
    可靠识别 —— 由事后核验（execution_guard）如实上报，而不是假装拦住了。
    """
    body = str(text or "")
    if not body:
        return []
    hits: list[str] = []
    for root in internal_data_roots(gateway):
        raw = str(root)
        if raw and (raw in body or raw.replace("\\", "\\") in body):
            hits.append(raw)
    return hits


# --------------------------------------------------------------------------
# 文件身份 / 原子写（契约 §3.1/§7）
# --------------------------------------------------------------------------


def file_identity(path: Any) -> dict:
    """文件身份：优先 W3 的 FileIdentity（volume+file_id）；还没合入时退回 stat 指纹。

    回退指纹只用于「执行前对象没变」的核对，**不会**被用来声称「这是同一个文件」
    （契约 §7：相同内容/相同大小不当然是原文件）。
    """
    target = Path(str(path))
    try:
        from agent.services.file_identity import identity_of  # W3 合入后使用权威实现

        found = identity_of(target)
        if found is not None:
            return {
                "source": "file_identity",
                # 有权威身份的对象一定存在（W3 identity_of 对不存在的路径返回 None）。
                "exists": True,
                "volume": str(getattr(found, "volume", "")),
                "file_id": str(getattr(found, "file_id", "")),
                "path": str(getattr(found, "path", target)),
                "size": int(getattr(found, "size", 0)),
                "mtime_ns": int(getattr(found, "mtime_ns", 0)),
            }
    except Exception:  # noqa: BLE001 - 没有 W3 实现就走回退
        pass
    try:
        st = target.stat()
    except OSError:
        return {"source": "stat", "exists": False, "path": str(target)}
    return {
        "source": "stat",
        "exists": True,
        "path": str(target),
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
    }


def identity_key(identity: dict | None) -> str | None:
    """FileIdentity/dict → 契约 Target.identity 的文件身份键（拿不到就不编）。"""
    if not identity:
        return None
    if identity.get("source") == "file_identity":
        return f"{identity.get('volume')}:{identity.get('file_id')}"
    if identity.get("exists") is False:
        return None
    return (
        f"stat:{identity.get('path')}:{identity.get('size')}:{identity.get('mtime_ns')}"
    )


def path_identity_key(path: Any) -> str | None:
    """路径当前对象的统一身份键（文件工具/审批恢复共用的唯一 helper）。

    只读取真实文件身份（volume+file_id；实现缺失时沿用既有 stat 回退键），
    **不做任何权限判定**。文件工具构造 path target、审批恢复重建 effect 时用它保证
    target 带的是真实 identity，而不是路径字符串。
    """
    return identity_key(file_identity(path))


def with_target_identity(effect: Any) -> Any:
    """为 path target 补上真实 identity（已有值不覆盖）；不改变其它字段。

    审批恢复会从持久化 effect 摘要重建 Effect；目标身份若缺失，重新读取真实身份补齐，
    供内部数据对象身份核对/执行前重核对使用。取不到身份就保持缺席（按不可确认处理）。
    """
    import dataclasses

    targets = []
    changed = False
    for target in getattr(effect, "targets", ()) or ():
        if getattr(target, "kind", "") == "path" and not getattr(target, "identity", None):
            key = path_identity_key(getattr(target, "value", ""))
            if key:
                targets.append(
                    Target(
                        kind=target.kind,
                        value=target.value,
                        identity=key,
                        exists=getattr(target, "exists", None),
                    )
                )
                changed = True
                continue
        targets.append(target)
    if not changed:
        return effect
    try:
        return dataclasses.replace(effect, targets=tuple(targets))
    except Exception:  # noqa: BLE001 - 未知 Effect 形状：保持原对象
        return effect


def content_sha256(path: Any) -> str | None:
    target = Path(str(path))
    digest = hashlib.sha256()
    try:
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()


def recheck_identity(path: Any, expected: dict | None) -> tuple[bool, str]:
    """执行前重核对对象身份：变了就不覆盖（返回中文原因）。

    * 权威身份（volume+file_id）不同 → 不是同一个文件；
    * 只写（expected.exists=False）但现在已存在 → 目标被抢先创建；
    * 回退 stat 指纹变化 → 内容可能已被改动，同样不覆盖。
    """
    if not expected:
        return True, ""
    current = file_identity(path)
    if expected.get("exists") is False:
        if current.get("exists"):
            return False, "目标文件在执行前已存在（审批后被其它程序创建），为安全不覆盖"
        return True, ""
    if expected.get("source") == "file_identity" and current.get("source") == "file_identity":
        if expected.get("volume") != current.get("volume") or expected.get("file_id") != current.get("file_id"):
            return False, "目标文件已不是原来的那个文件（身份不一致），不覆盖"
        return True, ""
    expected_key = (expected.get("size"), expected.get("mtime_ns"))
    current_key = (current.get("size"), current.get("mtime_ns"))
    if expected_key != current_key:
        return False, "目标文件在审批/执行前发生了变化（大小或修改时间不一致），不覆盖"
    return True, ""


def atomic_write_text(path: Any, text: str) -> None:
    """同目录临时文件 + 原子替换；失败时原文件保持不变（契约 §3.1）。"""
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".qio-tmp-", suffix=target.suffix, dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


# --------------------------------------------------------------------------
# 子智能体 AgentScope（契约 §2.3/§2.4）
# --------------------------------------------------------------------------

try:  # W1 的上下文变量（合入后就是权威来源）
    from agent.security.context import agent_scope as _SECURITY_AGENT_SCOPE  # type: ignore
except Exception:  # noqa: BLE001
    _SECURITY_AGENT_SCOPE = None

_local_agent_scope: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "qio_w2_agent_scope", default=None
)


def current_agent_scope() -> Any | None:
    """当前子智能体能力子集（没有 = 主智能体，返回 None）。"""
    if _SECURITY_AGENT_SCOPE is not None:
        try:
            found = _SECURITY_AGENT_SCOPE.get()
            if found is not None:
                return found
        except Exception:  # noqa: BLE001
            pass
    return _local_agent_scope.get()


@contextlib.contextmanager
def agent_scope_context(scope: Any | None) -> AsyncIterator[None]:
    """在这个上下文里创建的所有 effect 都属于该 AgentScope。"""
    tokens = []
    if scope is not None and _SECURITY_AGENT_SCOPE is not None:
        try:
            tokens.append((_SECURITY_AGENT_SCOPE, _SECURITY_AGENT_SCOPE.set(scope)))
        except Exception:  # noqa: BLE001 - W1 变量不可写时只用本地
            pass
    tokens.append((_local_agent_scope, _local_agent_scope.set(scope)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            with contextlib.suppress(Exception):
                var.reset(token)


def _grant_ids(grants: Iterable[Any] = ()) -> tuple[str, ...]:
    return tuple(
        str(getattr(grant, "grant_id", "") or "")
        for grant in grants
        if str(getattr(grant, "grant_id", "") or "")
    )


def make_agent_scope(
    *,
    scope_id: str | None = None,
    grants: Iterable[Any] = (),
    parent_grant_ids: Iterable[str] = (),
    parent_instruction_id: str | None = None,
    depth: int = 1,
    max_depth: int = SUBAGENT_MAX_DEPTH,
    purpose: str = "",
    label: str = "",
) -> Any:
    """按 AgentScope 的真实字段形状构造（本函数是**唯一**适配点）。

    W1 权威定义（agent/security/context.AgentScope）字段：
    parent_instruction_id / grants / depth / purpose / label。
    grant 对象优先；只有 id 时（旧 fake / 契约最小形状）退回 *grant_ids 字段。
    """
    grant_objects = tuple(grants or ())
    ids = tuple(str(item) for item in parent_grant_ids if str(item))
    if grant_objects and not ids:
        ids = _grant_ids(grant_objects)
    values = {
        "scope_id": scope_id or f"scope_{uuid.uuid4().hex[:12]}",
        "parent_instruction_id": parent_instruction_id,
        "grants": grant_objects,
        "parent_grant_ids": ids,
        "grant_ids": ids,
        "parent_grants": grant_objects or ids,
        "depth": int(depth),
        "max_depth": int(max_depth),
        "purpose": str(purpose or ""),
        "label": str(label or ""),
        "created_by": "subagent",
    }
    fields = set(getattr(AgentScope, "__dataclass_fields__", {}) or {})
    kwargs = {key: value for key, value in values.items() if key in fields}
    try:
        return AgentScope(**kwargs)
    except Exception:  # noqa: BLE001 - 字段类型差异时退回最小构造
        try:
            return AgentScope()
        except Exception:  # noqa: BLE001
            from types import SimpleNamespace

            return SimpleNamespace(**kwargs)


async def create_child_scope(
    gateway: Any | None,
    *,
    purpose: str,
    parent_scope: Any | None = None,
) -> Any | None:
    """创建子智能体能力子集（父 grant 子集 + 深度 + 用途）——只能缩小，不能扩大。

    * 网关提供 child_scope/child_agent_scope 时以它为准（W1 的权威实现）；
    * 否则按当前 InstructionContext 的 active_grants 生成子集（父指令结束即失效）；
    * **单层**：父上下文已经处在子智能体里（depth >= 1）时返回 None —— 子智能体
      不能自己再建下一层，嵌套需求由主智能体统一申请；
    * 没有网关（旧测试/未接线）返回 None，不伪造授权。
    """
    if gateway is None:
        return None
    parent_scope = parent_scope if parent_scope is not None else current_agent_scope()
    try:
        parent_depth = int(getattr(parent_scope, "depth", 0) or 0)
    except (TypeError, ValueError):
        parent_depth = 0
    if parent_depth >= 1:
        return None
    for name in ("child_scope", "create_child_scope", "child_agent_scope"):
        fn = getattr(gateway, name, None)
        if callable(fn):
            try:
                try:
                    result = fn(purpose=purpose)
                except TypeError:
                    result = fn()
            except Exception:  # noqa: BLE001 - 回退到本地构造
                break
            else:
                return await _maybe_await(result)
    snapshot = instruction_snapshot(gateway)
    instruction = None
    try:
        getter = getattr(gateway, "instruction", None)
        instruction = getter() if callable(getter) else getter
    except Exception:  # noqa: BLE001
        instruction = None
    grants: list[Any] = []
    active = getattr(instruction, "active_grants", None)
    if callable(active):
        try:
            grants = list(active() or [])
        except Exception:  # noqa: BLE001 - 拿不到 active_grants 时退回 grants 字段
            grants = []
    if not grants:
        grants = [
            grant
            for grant in getattr(instruction, "grants", []) or []
            if not getattr(grant, "revoked", False)
        ]
    instruction_id = getattr(instruction, "instruction_id", None) or snapshot.get(
        "instruction_id"
    )
    return make_agent_scope(
        grants=grants,
        parent_instruction_id=instruction_id,
        depth=1,
        purpose=purpose or snapshot.get("instruction_text") or "",
    )


def scope_summary(scope: Any | None) -> dict:
    if scope is None:
        return {}
    grants = tuple(getattr(scope, "grants", ()) or ())
    ids = getattr(scope, "parent_grant_ids", None)
    if not ids and grants:
        ids = _grant_ids(grants)
    return {
        "scope_id": str(getattr(scope, "scope_id", "") or ""),
        "parent_instruction_id": getattr(scope, "parent_instruction_id", None),
        "parent_grant_ids": [str(x) for x in (ids or ())],
        "grant_count": len(grants) if grants else len(tuple(ids or ())),
        "depth": int(getattr(scope, "depth", 0) or 0),
        "purpose": str(getattr(scope, "purpose", "") or ""),
    }


# --------------------------------------------------------------------------
# 执行层兜底（契约 §2.3/§3.2）
# --------------------------------------------------------------------------

READ_ONLY_EXEC_DENIED = (
    "只读模式下 QIO 不能执行可能修改业务文件的脚本或程序："
    "这条路径无法证明只读，本次已拒绝（没有任何内容被执行）。"
)
SCRIPT_BOUNDARY_UNAVAILABLE = (
    "当前模式要求脚本执行处在低完整性边界内，但本机无法核实该边界已生效："
    "为不静默降级，这次没有执行脚本（执行边界暂不可用）。"
)
INTEGRITY_UNVERIFIED = "低完整性边界未能核实生效"


def is_read_only(mode: str) -> bool:
    return mode_value(mode) == MODE_READ_ONLY


def low_integrity_required(mode: str, *, authorized_write_scope: bool = False) -> bool:
    """read_only 强制 Low IL；restricted 对未获授权写范围的脚本同样强制。"""
    current = mode_value(mode)
    if current == MODE_READ_ONLY:
        return True
    if current == MODE_RESTRICTED and not authorized_write_scope:
        return True
    return False


def script_execution_denied(mode: str, *, reason: str = "") -> ToolResult | None:
    """任意脚本/自建工具在执行层的统一结论（无法可靠约束 → 明确拒绝）。"""
    if is_read_only(mode):
        return ToolResult(
            ok=False,
            error=reason or READ_ONLY_EXEC_DENIED,
            category="permission",
            recoverable=False,
            code="readonly_execution_denied",
        )
    return None


async def to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """把同步扫描（快照/哈希）移出事件循环。"""
    loop = asyncio.get_running_loop()
    if kwargs:
        import functools

        fn = functools.partial(fn, **kwargs)
    return await loop.run_in_executor(None, fn, *args)
