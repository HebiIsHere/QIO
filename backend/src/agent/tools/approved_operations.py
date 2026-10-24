"""审批通过后的真实执行器注册（契约 §5.3 / §3.1）。

审批执行器（tools/approval.py::_execute_approved）负责：状态机、领取前重核对、
发布结果。**真正的动作**在这里按 operation.kind 注册：

* `fs.write` / `fs.patch`：执行前重核对对象身份与内容摘要，变化 → 不执行并报告
  invalidated 事实；原子写（同目录临时文件 + os.replace），失败保原件；
* `fs.delete`：必须进系统回收站。回收站模块（W3 `agent/services/recycle_bin.py`）
  还没合入时**如实返回「暂不可用」并保留文件**，绝不自己 unlink；
* `proc.run_program` / `proc.shell` / `proc.kill`：复用 cmd_tools 的真实执行路径
  （超时、输出截断、进程树收尾都不另写一套）；重放网关只放行「已批准」的这次执行，
  mode() 仍返回当前真实模式，执行层的只读兜底照常生效；
* `subagent.create`：按审批单里快照的 AgentScope 子集重建子智能体（不扩大范围）；
* `dep.install` / `net.egress` / `internal.data_write`：当前没有独立执行器，
  显式注册「不可用」处理器并说明原因 —— 不允许静默丢单。

**这里不复制任何权限判定**：审批已由用户完成；执行入口前的模式/对象重核对由
审批执行器（ExecutionContext.authorize_started）负责。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from pathlib import Path
from typing import Any, Callable

from agent.security.operations import ExecutionContext, ExecResult, OperationRegistry
from agent.tools import security_gate
from agent.trace.redact import redact_text

logger = logging.getLogger(__name__)


def _file_origins():
    """R9-F：后端文件来源 store（W3）；模块缺失时返回 None（不阻断执行）。"""
    try:
        from agent.services import file_origins as module

        return module
    except Exception:  # noqa: BLE001
        return None


def _snapshot_origin(path: str):
    module = _file_origins()
    if module is None:
        return None
    try:
        return module.snapshot_before_write(path)
    except Exception:  # noqa: BLE001 - 记账失败不改变执行结果
        return None


def _note_write_origin(path: str, *, existed_before: bool, pre_record, source: str) -> None:
    module = _file_origins()
    if module is None:
        return
    try:
        module.note_authorized_write_path(
            path,
            existed_before=existed_before,
            pre_record=pre_record,
            source=source,
        )
    except Exception:  # noqa: BLE001 - 记账失败不改变写入结果
        pass


def _note_recycled_origin(path: str) -> None:
    module = _file_origins()
    if module is None:
        return
    try:
        module.note_recycled_path(path)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 结果构造
# --------------------------------------------------------------------------


def _success(summary: str, *, detail: dict | None = None, **facts: Any) -> ExecResult:
    data = dict(detail or {})
    data.update(facts)
    return ExecResult(ok=True, state="completed", summary=summary, detail=data)


def _failure(
    summary: str,
    error: str | None = None,
    *,
    invalidated: bool = False,
    detail: dict | None = None,
    **facts: Any,
) -> ExecResult:
    data = dict(detail or {})
    data.update(facts)
    if invalidated:
        data["invalidated"] = True
    return ExecResult(
        ok=False,
        state="failed",
        summary=summary,
        error=error or summary,
        detail=data,
    )


def _fingerprint(ctx: ExecutionContext) -> dict:
    try:
        return dict(getattr(ctx.operation, "fingerprint", {}) or {})
    except Exception:  # noqa: BLE001
        return {}


def _expected_identity(payload: dict, fingerprint: dict) -> dict:
    return dict(
        fingerprint.get("identity")
        or payload.get("expected_identity")
        or {}
    )


REQUIREMENT_BINDING_KEY = "requirement_binding"


def _normalize_binding(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        requirement_id = value.get("requirement_id") or value.get("id")
        version = value.get("version", 1)
    else:
        requirement_id = getattr(value, "requirement_id", None)
        version = getattr(value, "version", 1)
    if not requirement_id:
        return None
    try:
        version = int(version or 1)
    except (TypeError, ValueError):
        version = 1
    return {"requirement_id": str(requirement_id), "version": version}


def _binding_from_operation(payload: dict, fingerprint: dict) -> dict | None:
    return _normalize_binding(
        payload.get(REQUIREMENT_BINDING_KEY) or fingerprint.get(REQUIREMENT_BINDING_KEY)
    )


def _engine_of(ctx: ExecutionContext) -> Any:
    return getattr(ctx, "engine", None)


def _requirements_of(ctx: ExecutionContext) -> Any:
    engine = _engine_of(ctx)
    return getattr(engine, "requirements", None) if engine is not None else None


def _effect_from_operation(ctx: ExecutionContext) -> Any:
    """审批单里持久化的 effect 摘要 → 契约 Effect（用于执行前重新走引擎判定）。"""
    raw = dict(getattr(getattr(ctx, "operation", None), "effect", None) or {})
    if not raw:
        return None
    try:
        from agent.security.effects import Effect, EffectKind, Target
    except Exception:  # noqa: BLE001 - 核心类型不可用则不重放判定（其余核对仍做）
        return None
    kind_raw = str(raw.get("kind") or "")
    try:
        kind = EffectKind(kind_raw)
    except Exception:  # noqa: BLE001
        return None
    targets = []
    for item in raw.get("targets") or []:
        if not isinstance(item, dict):
            continue
        targets.append(
            Target(
                kind=str(item.get("kind") or "path"),
                value=str(item.get("value") or ""),
                identity=item.get("identity"),
                exists=item.get("exists"),
            )
        )
    return Effect(
        kind=kind,
        targets=tuple(targets),
        purpose=str(raw.get("purpose") or ""),
        tool=raw.get("tool"),
        detail=dict(raw.get("detail") or {}),
        reversible=bool(raw.get("reversible", False)),
    )


def _stored_operation_binding(ctx: ExecutionContext) -> dict:
    return dict(_fingerprint(ctx).get("operation_binding") or {})


def _review_binding_mismatch(ctx: ExecutionContext) -> str | None:
    """审批单里的操作绑定指纹必须与 effect 快照重算结果一致（R9-H §4）。

    内容/前置状态/程序/参数/cwd/脚本摘要任一变化 → 审查指纹不同 → 旧审批不能
    继续执行。历史审批没有该字段时返回 None，由既有的要求/对象重核对兜底。
    """
    stored = _stored_operation_binding(ctx)
    expected = str(stored.get("fingerprint") or "")
    if not expected:
        return None
    engine = _engine_of(ctx)
    effect = _effect_from_operation(ctx)
    if engine is None or effect is None:
        return "审批单绑定了操作事实指纹，但当前无法重算（审批环境不完整）：本次未执行"
    try:
        from agent.security.review import operation_fingerprint

        operation = engine._review_operation(effect)
        mode = str(
            stored.get("mode")
            or getattr(getattr(ctx, "operation", None), "mode_at_request", "")
            or "restricted"
        )
        actual = str(operation_fingerprint(operation, mode=mode))
    except Exception:  # noqa: BLE001 - 重算失败不能当作一致
        return "审批单绑定的操作事实无法重算：本次未执行（需重新审批）"
    if not actual or actual != expected:
        return (
            "审批单记录的操作事实（内容/前置状态/程序/参数/cwd/脚本摘要）与本次执行不一致："
            "本次未执行（需重新审批）"
        )
    return None


def _payload_content_mismatch(ctx: ExecutionContext, payload: dict) -> str | None:
    """审批指纹记录的**拟写入内容**摘要必须与本次真正执行的 payload 内容一致。

    契约字段语义（docs/r9h-contracts.md §4）：
    * content_digest = 拟写入内容/补丁/附件写回内容摘要（新内容）；
    * precondition_digest（旧字段 base_content_sha256/content_sha256 同义）= 覆盖前状态。
    附件写回的旧审批形状用 content_sha256 表示**覆盖前**内容、new_sha256 表示新内容，
    因此按操作类型选择"新内容"来源，不能把旧内容摘要当新内容比较。
    """
    fingerprint = _fingerprint(ctx)
    stored = _stored_operation_binding(ctx)
    operation = getattr(ctx, "operation", None)
    kind = str(getattr(operation, "kind", "") or "")
    if kind.startswith("attachment.write_back") or kind.startswith(
        "attachment.batch_write_back"
    ):
        expected = str(
            stored.get("content_digest")
            or fingerprint.get("content_digest")
            or fingerprint.get("new_sha256")
            or ""
        )
    else:
        expected = str(
            stored.get("content_digest")
            or fingerprint.get("content_digest")
            or fingerprint.get("content_sha256")
            or fingerprint.get("new_sha256")
            or ""
        )
    if not expected:
        return None
    actual = str(payload.get("content_sha256") or payload.get("sha256") or "")
    if not actual:
        return None
    if actual != expected:
        return "待执行内容与审批时绑定的内容摘要不一致：本次未执行（需重新审批）"
    return None


async def _recheck_requirement_and_mode(
    ctx: ExecutionContext,
    *,
    payload: dict,
    fingerprint: dict,
    path: str,
    item_effect: Any = None,
) -> ExecResult | None:
    """批准后执行前重核对（契约 §7）：模式快照/要求版本/授权/审批有效期/对象。

    返回 None 表示仍覆盖；返回 ExecResult 表示不得执行（保留文件并如实报状态）。
    """
    authorize_started = getattr(ctx, "authorize_started", None)
    if callable(authorize_started):
        try:
            # R9-H5 §4：批量逐项核对时传子项 effect，让单项限制只影响该项；
            # 整批共同条件已在 _execute_approved 的 common_only 检查中完成。
            if item_effect is not None:
                try:
                    allowed, reason = await authorize_started(item_effect)
                except TypeError:  # 兼容旧签名
                    allowed, reason = await authorize_started()
            else:
                allowed, reason = await authorize_started()
        except Exception:  # noqa: BLE001 - 重核对异常按未通过（失败关闭）
            return _failure(
                "执行前重核对失败：本次未执行",
                "invalidated: recheck_error",
                invalidated=True,
                path=path,
            )
        if not allowed:
            return _failure(
                reason or "当前模式/审批状态已变化，本次未执行",
                "invalidated: mode_changed",
                invalidated=True,
                path=path,
            )
    # 要求绑定：仍 active 且版本一致（补充/修正/撤回后不得沿用旧允许）。
    binding = _binding_from_operation(payload, fingerprint)
    if binding is None:
        # 审批执行器会把提交时的要求绑定写在 PendingOperation 上；payload/fingerprint
        # 缺失（旧记录/兼容形状）时以审批单自身绑定的要求为唯一来源，不回退"最近一条"。
        operation = getattr(ctx, "operation", None)
        requirement_id = str(getattr(operation, "requirement_id", "") or "")
        if requirement_id:
            version = getattr(operation, "requirement_version", None)
            binding = {"requirement_id": requirement_id, "version": int(version or 1)}
    ledger = _requirements_of(ctx)
    validate = getattr(ledger, "validate_binding", None)
    if binding and callable(validate):
        try:
            try:
                from agent.security.requirements import RequirementBinding  # type: ignore

                target = RequirementBinding(
                    requirement_id=str(binding["requirement_id"]),
                    version=int(binding["version"]),
                )
            except Exception:  # noqa: BLE001
                target = binding
            valid = bool(validate(target))
        except TypeError:
            try:
                valid = bool(
                    validate(
                        requirement_id=binding["requirement_id"],
                        version=binding["version"],
                    )
                )
            except Exception:  # noqa: BLE001
                valid = False
        except Exception:  # noqa: BLE001
            valid = False
        if not valid:
            return _failure(
                "对应的用户要求已被修正/撤回或版本不符：本次未执行（需重新提出）",
                "invalidated: requirement_invalid",
                invalidated=True,
                path=path,
            )
    # 用当前模式重新走一次引擎判定：明确拒绝 → 不执行；需要重新确认 → 不自动执行。
    engine = _engine_of(ctx)
    effect = item_effect if item_effect is not None else _effect_from_operation(ctx)
    if engine is not None and effect is not None and callable(getattr(engine, "authorize", None)):
        try:
            decision = await engine.authorize(effect)
        except Exception:  # noqa: BLE001 - 引擎异常按未通过（失败关闭）
            return _failure(
                "执行前重新判定失败：本次未执行",
                "invalidated: recheck_error",
                invalidated=True,
                path=path,
            )
        verdict = security_gate.verdict_of(decision)
        if verdict == "deny":
            return _failure(
                f"当前授权已不允许这次操作：{security_gate.reason_of(decision, '拒绝')}（本次未执行）",
                "invalidated: authorization_denied",
                invalidated=True,
                path=path,
            )
        # approve 不自动放行：审批单本身是用户的明确决定，但若当前需要新的确认，
        # 交由上层（Lead 的 RecheckService / 审批恢复流程）按最新状态处理；这里
        # 至少保证不执行「当前明确拒绝」的操作。
    return None


def _redacted(text: Any, limit: int = 2000) -> str:
    return redact_text(str(text or "")[:limit])


def _tool_result_to_exec(result: Any, *, label: str) -> ExecResult:
    """ToolResult → ExecResult（审批结果落库要的事实，不是中文猜语义）。"""
    ok = bool(getattr(result, "ok", False))
    content = _redacted(getattr(result, "content", "") or "")
    error_raw = getattr(result, "error", None)
    error = _redacted(error_raw) if error_raw else None
    summary = (content or error or "")[:300]
    return ExecResult(
        ok=ok,
        state="completed" if ok else "failed",
        summary=summary or ("已执行" if ok else "执行失败"),
        error=None if ok else (error or "execution_failed"),
        detail={
            "tool": label,
            "ok": ok,
            "content": content,
            "error": error or "",
            "category": getattr(result, "category", None),
            "code": getattr(result, "code", None),
        },
    )


def _require_internal_data_confirmations(
    ctx: ExecutionContext, fingerprint: dict, path: str
) -> ExecResult | None:
    """内部运行数据必须走完三次确认才执行（契约 §4；确认状态由后端校验）。"""
    root = fingerprint.get("internal_data_root")
    entries = fingerprint.get("internal_data_entries") or ()
    # R9-H4 §8：执行开始前按**当前**对象识别复核最低确认次数，不能只信审批单里
    # 写入的一次旧判断。目录内枚举到受保护子对象也属于内部数据维护。
    current_internal = bool(root) or bool(entries)
    if not current_internal:
        engine = getattr(ctx, "engine", None)
        try:
            current_internal = (
                security_gate.match_internal_data(engine, path) is not None
                if engine is not None
                else False
            )
        except Exception:  # noqa: BLE001 - 识别失败不能静默降级
            current_internal = True
    if not current_internal:
        return None
    try:
        required = int(getattr(ctx.operation, "confirmations_required", 1) or 1)
    except (TypeError, ValueError):
        required = 1
    if required < 3:
        return _failure(
            "目标是 QIO 内部运行数据，但没有完成三次确认：本次未执行",
            "internal_confirmations_missing",
            invalidated=True,
            path=path,
            internal_data_root=str(root or path),
        )
    return None


def _check_identity(path: str, expected: dict) -> tuple[bool, str]:
    """执行前对象重核对：有指纹按指纹；没有指纹时对已存在的目标拒绝覆盖。"""
    if expected:
        return security_gate.recheck_identity(path, expected)
    try:
        exists = Path(path).exists()
    except OSError:
        exists = True
    if exists:
        return (
            False,
            "审批单没有对象身份指纹，无法在执行前核对目标是否被改动，为安全不覆盖",
        )
    return True, ""


# --------------------------------------------------------------------------
# 文件操作
# --------------------------------------------------------------------------


async def _authorize_content_read(
    ctx: ExecutionContext, path: str, *, purpose: str
) -> ExecResult | None:
    """R9-H6 §3/§5：审批恢复中任何必要正文读取前，先核对最新读取限制。

    复用执行前重核对链路（要求 active/版本、模式、最新已知限制），但不生成
    第二次读取审批；只核对允许/拒绝。读取被拒时调用方不得读取正文。
    """
    from agent.security.effects import Effect, EffectKind, Target

    read_effect = Effect(
        kind=EffectKind.READ,
        targets=(Target("path", path),),
        purpose=purpose,
        tool="fs.read",
        detail={
            "action": "read",
            "limit_impacts": [
                {"operation": "read", "objects": [{"kind": "path", "value": path}]}
            ],
            "limit_impacts_complete": True,
        },
    )
    prior_basis = dict(getattr(ctx, "authorization_basis", {}) or {})
    starter = getattr(ctx, "authorize_started", None)
    if not callable(starter):
        return _failure("无法核对最新读取限制：本次未读取、未修改", "read_check_unavailable")
    try:
        try:
            allowed, reason = await starter(read_effect)
        except TypeError:  # 旧签名不能按读取 effect 核对：失败关闭
            return _failure(
                "执行器无法按最新读取限制核对读取：本次未读取、未修改",
                "read_check_unavailable",
            )
    except Exception as exc:  # noqa: BLE001 - 读取核对异常按失败关闭
        return _failure(
            "读取限制核对失败（" + type(exc).__name__ + "）：本次未读取、未修改",
            "read_check_failed",
        )
    finally:
        if prior_basis:
            ctx.authorization_basis = prior_basis
    if not allowed:
        return _failure(
            "读取限制未通过：" + str(reason or "最新限制禁止读取") + "；本次未读取、未修改",
            "read_denied",
            invalidated=True,
            path=path,
        )
    return None


async def _authorize_content_reads(
    ctx: ExecutionContext, paths: list[str], *, purpose: str
) -> ExecResult | None:
    """R9-H7 §3：审批恢复删除/目录快照读取前核对最新读取限制。"""
    if not paths:
        return None
    from agent.security.effects import Effect, EffectKind, Target

    read_effect = Effect(
        kind=EffectKind.READ,
        targets=tuple(Target("path", path) for path in paths),
        purpose=purpose,
        tool="fs.read",
        detail={
            "action": "read",
            "limit_impacts": [
                {
                    "operation": "read",
                    "objects": [{"kind": "path", "value": path} for path in paths],
                }
            ],
            "limit_impacts_complete": True,
        },
    )
    prior_basis = dict(getattr(ctx, "authorization_basis", {}) or {})
    starter = getattr(ctx, "authorize_started", None)
    if not callable(starter):
        return _failure("无法核对最新读取限制：本次未读取、未执行", "read_check_unavailable")
    try:
        try:
            allowed, reason = await starter(read_effect)
        except TypeError:
            return _failure(
                "执行器无法按最新读取限制核对读取：本次未读取、未执行",
                "read_check_unavailable",
            )
    except Exception as exc:  # noqa: BLE001
        return _failure(
            "读取限制核对失败（" + type(exc).__name__ + "）：本次未读取、未执行",
            "read_check_failed",
        )
    finally:
        ctx.authorization_basis = prior_basis
    if not allowed:
        return _failure(
            "读取限制未通过：" + str(reason or "最新限制禁止读取") + "；本次未读取、未执行",
            "read_denied",
            invalidated=True,
        )
    return None


async def execute_fs_write(payload: dict, ctx: ExecutionContext) -> ExecResult:
    path = str(payload.get("path") or "").strip()
    if not path:
        return _failure("审批单缺少目标路径：未执行", "missing_path")
    if "content" not in payload or payload.get("content") is None:
        return _failure("审批单缺少待写入内容：未执行", "missing_content", path=path)
    content = str(payload.get("content"))
    fingerprint = _fingerprint(ctx)

    binding_error = _review_binding_mismatch(ctx) or _payload_content_mismatch(ctx, payload)
    if binding_error:
        return _failure(binding_error, "invalidated: operation_binding", invalidated=True, path=path)
    blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
    if blocked is not None:
        return blocked
    blocked = await _recheck_requirement_and_mode(
        ctx, payload=payload, fingerprint=fingerprint, path=path
    )
    if blocked is not None:
        return blocked

    expected = _expected_identity(payload, fingerprint)
    same, reason = _check_identity(path, expected)
    if not same:
        return _failure(
            f"对象已变化，本次未执行：{reason}",
            f"invalidated: {reason}",
            invalidated=True,
            path=path,
        )
    base_kind_write = str(fingerprint.get("base_content_digest_kind") or "")
    if base_kind_write == "internal_metadata":
        # R9-H8 §3：确认前不读正文，但必须核对确认前的 size/mtime 元数据；
        # 身份不变不代表内容不变。等待期间原地编辑会在这里失效。
        current_meta = await asyncio.to_thread(security_gate.file_identity, path)
        if not current_meta.get("exists"):
            return _failure(
                "内部维护目标在确认期间被删除：本次未写入",
                "invalidated: internal_target_missing",
                invalidated=True,
                path=path,
            )
        try:
            same_meta = (
                int(current_meta.get("size") or 0) == int(fingerprint.get("base_size") or 0)
                and int(current_meta.get("mtime_ns") or 0)
                == int(fingerprint.get("base_mtime_ns") or 0)
            )
        except (TypeError, ValueError):
            same_meta = False
        if not same_meta:
            return _failure(
                "内部维护目标在确认期间发生了变化（大小/修改时间不一致）："
                "本次未写入，保留最新内容；需重新提出维护",
                "invalidated: internal_metadata_changed",
                invalidated=True,
                path=path,
            )
    base_digest = fingerprint.get("base_content_sha256")
    if base_digest:
        read_blocked = await _authorize_content_read(
            ctx, path, purpose="读取原内容以核对覆盖前摘要"
        )
        if read_blocked is not None:
            return read_blocked
        current_digest = await asyncio.to_thread(security_gate.content_sha256, path)
        if current_digest != base_digest:
            return _failure(
                "目标内容在审批期间发生了变化：本次未执行（需重新审批）",
                "invalidated: base_content_changed",
                invalidated=True,
                path=path,
            )
    expected_digest = str(
        fingerprint.get("content_sha256") or payload.get("content_sha256") or ""
    )
    digest = security_gate.text_sha256(content)
    if expected_digest and digest != expected_digest:
        return _failure(
            "待写入内容与提交审批时不一致：本次未执行（需重新审批）",
            "invalidated: content_sha256_mismatch",
            invalidated=True,
            path=path,
        )
    # R9-F：写前抓来源/存在性，写成功后一次记账（不得把已存在用户文件冒充生成物）。
    pre_record = await asyncio.to_thread(_snapshot_origin, path)
    existed_before = await asyncio.to_thread(lambda: Path(path).exists())
    try:
        await asyncio.to_thread(security_gate.atomic_write_text, path, content)
    except OSError as exc:
        return _failure(
            f"写入失败（原文件保持原样）：{exc}",
            f"{type(exc).__name__}: {exc}",
            path=path,
        )
    await asyncio.to_thread(
        _note_write_origin,
        path,
        existed_before=existed_before,
        pre_record=pre_record,
        source="approved.fs_write",
    )
    return _success(
        f"已写入 {path}",
        path=path,
        bytes=len(content.encode("utf-8")),
        content_sha256=digest,
    )


async def execute_fs_patch(payload: dict, ctx: ExecutionContext) -> ExecResult:
    path = str(payload.get("path") or "").strip()
    old = payload.get("old")
    new = payload.get("new")
    if not path:
        return _failure("审批单缺少目标路径：未执行", "missing_path")
    if old is None or new is None:
        return _failure("审批单缺少 old/new 内容：未执行", "missing_patch_content", path=path)
    old_text = str(old)
    new_text = str(new)
    fingerprint = _fingerprint(ctx)

    binding_error = _review_binding_mismatch(ctx) or _payload_content_mismatch(ctx, payload)
    if binding_error:
        return _failure(binding_error, "invalidated: operation_binding", invalidated=True, path=path)
    blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
    if blocked is not None:
        return blocked
    blocked = await _recheck_requirement_and_mode(
        ctx, payload=payload, fingerprint=fingerprint, path=path
    )
    if blocked is not None:
        return blocked

    expected = _expected_identity(payload, fingerprint)
    same, reason = _check_identity(path, expected)
    if not same:
        return _failure(
            f"对象已变化，本次未执行：{reason}",
            f"invalidated: {reason}",
            invalidated=True,
            path=path,
        )
    base_digest = fingerprint.get("base_content_sha256")
    base_kind = str(fingerprint.get("base_content_digest_kind") or "")
    # H6 §3：恢复执行中的正文读取也要先核对最新读取限制；读取被拒时在
    # read_bytes 之前返回，不计算补丁、不修改。
    read_blocked = await _authorize_content_read(
        ctx, path, purpose="补丁审批恢复读取正文"
    )
    if read_blocked is not None:
        return read_blocked
    try:
        raw = await asyncio.to_thread(Path(path).read_bytes)
        current = raw.decode("utf-8")
        current = current.replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        return _failure(
            f"读取目标失败，本次未修改：{exc}",
            f"{type(exc).__name__}: {exc}",
            path=path,
        )
    if base_digest:
        current_digest = (
            security_gate.text_sha256(current)
            if base_kind == "text"
            else hashlib.sha256(raw).hexdigest()
        )
        if current_digest != base_digest:
            return _failure(
                "目标内容在审批期间发生了变化：本次未修改（需重新审批）",
                "invalidated: base_content_changed",
                invalidated=True,
                path=path,
            )
    if old_text not in current:
        return _failure(
            "目标内容已变化（找不到待替换的 old）：本次未修改，需重新审批",
            "invalidated: old_not_found",
            invalidated=True,
            path=path,
        )
    updated = current.replace(old_text, new_text, 1)
    digest = security_gate.text_sha256(updated)
    expected_digest = str(fingerprint.get("content_sha256") or "")
    if expected_digest and digest != expected_digest:
        return _failure(
            "替换后的内容与提交审批时不一致：本次未修改（需重新审批）",
            "invalidated: content_sha256_mismatch",
            invalidated=True,
            path=path,
        )
    pre_record = await asyncio.to_thread(_snapshot_origin, path)
    existed_before = await asyncio.to_thread(lambda: Path(path).exists())
    try:
        await asyncio.to_thread(security_gate.atomic_write_text, path, updated)
    except OSError as exc:
        return _failure(
            f"写入失败（原文件保持原样）：{exc}",
            f"{type(exc).__name__}: {exc}",
            path=path,
        )
    await asyncio.to_thread(
        _note_write_origin,
        path,
        existed_before=existed_before,
        pre_record=pre_record,
        source="approved.fs_patch",
    )
    return _success(
        f"已修改 {path}",
        path=path,
        bytes=len(updated.encode("utf-8")),
        content_sha256=digest,
    )


def _recycle_bin_mover() -> Callable[..., Any] | None:
    """回收站入口（W3 模块，延迟导入；不存在时绝不自己删文件）。"""
    try:
        from agent.services import recycle_bin  # type: ignore
    except Exception:  # noqa: BLE001 - W3 模块尚未合入
        return None
    for name in (
        "move_to_recycle_bin",
        "send_to_recycle_bin",
        "recycle",
        "move_to_bin",
    ):
        fn = getattr(recycle_bin, name, None)
        if callable(fn):
            return fn
    return None


async def _move_to_recycle_bin(path: str) -> tuple[bool, str, str]:
    """返回 (是否成功, 机器代码, 中文原因)；模块不可用与真实失败分开报告。"""
    mover = _recycle_bin_mover()
    if mover is None:
        return (
            False,
            "recycle_bin_unavailable",
            "回收站模块暂不可用（W3 agent/services/recycle_bin.py 尚未提供可用入口）",
        )
    try:
        result = mover(str(path))
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 - 回收站失败必须保留文件
        return False, "recycle_bin_failed", f"{type(exc).__name__}: {exc}"
    if isinstance(result, dict):
        ok = bool(result.get("ok", result.get("success", False)))
        detail = str(result.get("error") or result.get("reason") or "")
        return ok, ("recycled" if ok else "recycle_bin_failed"), detail
    ok = bool(result)
    return ok, ("recycled" if ok else "recycle_bin_failed"), ""


def _delete_rows(payload: dict, fingerprint: dict) -> list[dict]:
    """审批单 → 待删除清单（兼容新 paths 列表与旧的单 path 形状）。"""
    rows: list[dict] = []
    fingerprint_rows = (
        fingerprint.get("paths") if isinstance(fingerprint.get("paths"), list) else []
    )
    raw_paths = payload.get("paths")
    if isinstance(raw_paths, list):
        for index, item in enumerate(raw_paths):
            if isinstance(item, dict):
                path = str(item.get("path") or "").strip()
                identity = item.get("identity")
            else:
                path = str(item or "").strip()
                identity = None
            content_sha256 = None
            is_directory = False
            directory_impact_data = None
            content_deferred = False
            base_size = None
            base_mtime_ns = None
            if index < len(fingerprint_rows):
                candidate = fingerprint_rows[index]
                if isinstance(candidate, dict):
                    if identity is None:
                        identity = candidate.get("identity")
                    content_sha256 = candidate.get("content_sha256")
                    is_directory = bool(candidate.get("is_directory"))
                    directory_impact_data = candidate.get("directory_impact")
                    content_deferred = bool(candidate.get("content_deferred"))
                    base_size = candidate.get("base_size")
                    base_mtime_ns = candidate.get("base_mtime_ns")
            if isinstance(item, dict):
                # R9-H4：payload 里逐项携带的真实目录影响（审批快照）优先。
                directory_impact_data = item.get("directory_impact") or directory_impact_data
                if item.get("is_directory") is not None:
                    is_directory = bool(item.get("is_directory"))
            if path:
                rows.append(
                    {
                        "path": path,
                        "identity": identity,
                        "content_sha256": content_sha256,
                        "content_deferred": content_deferred,
                        "base_size": base_size,
                        "base_mtime_ns": base_mtime_ns,
                        "is_directory": is_directory,
                        "directory_impact": directory_impact_data,
                    }
                )
    elif str(payload.get("path") or "").strip():
        rows.append(
            {
                "path": str(payload.get("path")).strip(),
                "identity": fingerprint.get("identity"),
            }
        )
    # 批量审批单的 items 列表携带逐项 item_id（批次状态/恢复只继续未执行项）。
    item_ids: dict[str, str] = {}
    for entry in payload.get("items") or []:
        if isinstance(entry, dict) and entry.get("path"):
            item_ids[str(entry["path"])] = str(entry.get("item_id") or "")
    if item_ids:
        for row in rows:
            row["item_id"] = item_ids.get(str(row.get("path")), "")
    return rows


def _identity_matches(path: str, expected: Any) -> tuple[bool, str]:
    """执行前身份核对：dict 指纹走 recheck_identity；身份键字符串直接对比。"""
    if isinstance(expected, dict) and expected:
        return security_gate.recheck_identity(path, expected)
    if isinstance(expected, str) and expected:
        current = security_gate.file_identity(path)
        current_key = security_gate.identity_key(current)
        if current_key is None or current_key != expected:
            return False, "目标已不是审批时的那一个文件（身份不一致），不删除"
        return True, ""
    return _check_identity(path, {})


def _check_directory_impact(expected: Any, path: str, ctx: ExecutionContext) -> str | None:
    """R9-H4 §7：目录删除执行前重算影响；新增/移除/替换或不完整 → 该项失效。"""
    from agent.security import internal_data
    from agent.tools import fs_tools

    if not isinstance(expected, dict) or not expected:
        return "审批记录缺少目录影响依据：无法可靠比较，需重新确认"
    # R9-H9 §1：完整性是独立执行前提，不能由“两次摘要相同”或三次确认豁免。
    if expected.get("complete") is not True:
        return "目录影响不完整（达到枚举上限或枚举失败）：本次未删除，需重新确认"
    protected = []
    engine = getattr(ctx, "engine", None)
    paths_fn = getattr(engine, "internal_data_paths", None)
    if callable(paths_fn):
        try:
            protected = list(paths_fn() or ())
        except Exception:  # noqa: BLE001 - 保护范围读不到仍按无保护比较，但不放宽
            protected = []
    # 保护属性必须与审批时使用同一识别口径（路径 + identity/birth），否则目录里
    # 的硬链接/移动内部对象会被误判为“替换”，导致合法删除永远失效。
    if expected.get("content_deferred"):
        # R9-H8 §5：内部维护确认前只保存元数据快照；执行时只比较元数据影响，
        # 不读取正文/摘要。身份/大小/mtime/子项集合变化才失效。
        current = fs_tools.directory_impact(
            path,
            operation="delete",
            include_content=False,
            is_protected=lambda candidate: security_gate.match_internal_data(
                engine, candidate
            )
            is not None,
        )
        if (
            str(current.get("digest") or "") != str(expected.get("digest") or "")
            or bool(current.get("complete", True))
            != bool(expected.get("complete", True))
        ):
            return "目录元数据在确认期间发生变化：本次未删除（需重新确认）"
        return None
    result = fs_tools.verify_directory_impact(
        expected,
        path,
        is_protected=lambda candidate: security_gate.match_internal_data(
            engine, candidate
        )
        is not None,
    )
    if bool(result.get("ok")):
        return None
    return str(
        result.get("reason")
        or "目录内容在审批期间发生变化：本次未执行（需重新确认）"
    )


async def execute_fs_delete(payload: dict, ctx: ExecutionContext) -> ExecResult:
    fingerprint = _fingerprint(ctx)
    binding_error = _review_binding_mismatch(ctx)
    if binding_error:
        return _failure(binding_error, "invalidated: operation_binding", invalidated=True)
    rows = _delete_rows(payload, fingerprint)
    if not rows:
        return _failure("审批单缺少目标路径：未执行", "missing_path")
    results: list[dict] = []
    failed: list[dict] = []
    for row in rows:
        path = row["path"]
        item_id = str(row.get("item_id") or "")
        blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
        if blocked is not None:
            item = {
                "item_id": item_id,
                "path": path,
                "ok": False,
                "status": "invalidated",
                "invalidated": True,
                "reason": blocked.summary,
            }
            results.append(item)
            failed.append(item)
            continue
        # R9-G §7：批准后重放前逐项重核对（模式/要求版本/授权/审批/对象）。
        # H5 §4：使用该子项 effect 核对，B 的单项限制不能使 A 整批失效。
        from agent.security.effects import Effect, EffectKind, Target

        # R9-H6 §2：目录不是“只有目录路径”的一个对象。逐项重核对必须携带
        # 审批快照里的完整子对象影响，否则“禁止删除 B 内某个文件”无法命中。
        directory_impact_data = row.get("directory_impact")
        item_objects: list[dict] = [{"kind": "path", "value": path}]
        item_complete = True
        if row.get("is_directory"):
            if not isinstance(directory_impact_data, dict) or not directory_impact_data:
                item_complete = False
            else:
                entries = directory_impact_data.get("entries") or []
                if not entries:
                    item_complete = False
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    child = str(entry.get("path") or "").strip()
                    if child and child != path:
                        item_objects.append({"kind": "path", "value": child})
                if directory_impact_data.get("complete") is not True:
                    item_complete = False
        item_effect = Effect(
            kind=EffectKind.DELETE,
            targets=(Target("path", path, identity=row.get("identity")),),
            purpose=f"删除{'目录' if row.get('is_directory') else '文件'}：{path}",
            tool="fs.delete",
            detail={
                "action": "delete",
                "is_directory": bool(row.get("is_directory")),
                "directory_impact": directory_impact_data,
                "directory_impact_complete": item_complete,
                "limit_impacts": [
                    {
                        "operation": "delete",
                        "objects": item_objects,
                    }
                ],
                "limit_impacts_complete": item_complete,
            },
        )
        blocked = await _recheck_requirement_and_mode(
            ctx,
            payload=payload,
            fingerprint=fingerprint,
            path=path,
            item_effect=item_effect,
        )
        if blocked is not None:
            item = {
                "item_id": item_id,
                "path": path,
                "ok": False,
                "status": "invalidated",
                "invalidated": True,
                "reason": blocked.summary,
            }
            results.append(item)
            failed.append(item)
            continue
        same, reason = _identity_matches(path, row.get("identity"))
        if not same:
            item = {
                "item_id": item_id,
                "path": path,
                "ok": False,
                "status": "invalidated",
                "invalidated": True,
                "reason": reason,
            }
            results.append(item)
            failed.append(item)
            continue
        if row.get("content_deferred") and not row.get("is_directory"):
            current_meta = await asyncio.to_thread(security_gate.file_identity, path)
            try:
                same_meta = (
                    bool(current_meta.get("exists"))
                    and int(current_meta.get("size") or 0) == int(row.get("base_size") or 0)
                    and int(current_meta.get("mtime_ns") or 0)
                    == int(row.get("base_mtime_ns") or 0)
                )
            except (TypeError, ValueError):
                same_meta = False
            if not same_meta:
                item = {
                    "item_id": item_id,
                    "path": path,
                    "ok": False,
                    "status": "invalidated",
                    "invalidated": True,
                    "reason": "内部维护目标在确认期间发生变化：本次未删除（保留最新内容）",
                    "code": "invalidated: internal_metadata_changed",
                }
                results.append(item)
                failed.append(item)
                continue
        # 旧形状/手写单 path 的目录删除没有目录影响依据：必须 fail-closed 重新确认，
        # 不能因为缺少 is_directory 字段就跳过新增/移除/替换子对象核对。
        try:
            path_is_dir = await asyncio.to_thread(lambda: Path(path).is_dir())
        except OSError:
            path_is_dir = False
        if path_is_dir and not row.get("is_directory"):
            item = {
                "item_id": item_id,
                "path": path,
                "ok": False,
                "status": "invalidated",
                "invalidated": True,
                "reason": "审批记录没有目录影响依据（旧审批/手写 path）：无法可靠比较子对象变化，需重新确认",
                "code": "invalidated: directory_impact_missing",
            }
            results.append(item)
            failed.append(item)
            continue
        if row.get("is_directory"):
            snapshot = row.get("directory_impact") or {}
            snapshot_paths = [
                str(entry.get("path"))
                for entry in (snapshot.get("entries") or ())
                if isinstance(entry, dict) and entry.get("path")
            ]
            read_blocked = None
            if not snapshot.get("content_deferred"):
                read_blocked = await _authorize_content_reads(
                    ctx, snapshot_paths or [path], purpose="删除目录前核对子文件内容快照"
                )
            if read_blocked is not None:
                item = {
                    "item_id": item_id,
                    "path": path,
                    "ok": False,
                    "status": "invalidated",
                    "invalidated": True,
                    "reason": read_blocked.summary,
                    "code": "invalidated: read_denied",
                }
                results.append(item)
                failed.append(item)
                continue
            directory_error = await asyncio.to_thread(
                _check_directory_impact,
                row.get("directory_impact"),
                path,
                ctx,
            )
            if directory_error:
                item = {
                    "item_id": item_id,
                    "path": path,
                    "ok": False,
                    "status": "invalidated",
                    "invalidated": True,
                    "reason": directory_error,
                    "code": "invalidated: directory_changed",
                }
                results.append(item)
                failed.append(item)
                continue
        expected_content = row.get("content_sha256")
        if expected_content and not row.get("is_directory"):
            read_blocked = await _authorize_content_reads(
                ctx, [path], purpose="删除文件前核对内容快照"
            )
            if read_blocked is not None:
                item = {
                    "item_id": item_id,
                    "path": path,
                    "ok": False,
                    "status": "invalidated",
                    "invalidated": True,
                    "reason": read_blocked.summary,
                    "code": "invalidated: read_denied",
                }
                results.append(item)
                failed.append(item)
                continue
            current_content = await asyncio.to_thread(_w3_content_sha, path)
            if current_content is None or current_content != expected_content:
                item = {
                    "item_id": item_id,
                    "path": path,
                    "ok": False,
                    "status": "invalidated",
                    "invalidated": True,
                    "reason": "目标文件内容在审批期间发生了变化：未删除（保留原文件，需重新确认）",
                    "code": "invalidated: content_changed",
                }
                results.append(item)
                failed.append(item)
                continue
        ok, code, detail = await _move_to_recycle_bin(path)
        if ok:
            await asyncio.to_thread(_note_recycled_origin, path)
        item = {
            "item_id": item_id,
            "path": path,
            "ok": bool(ok),
            "status": "completed" if ok else "failed",
            "mechanism": code if ok else None,
            "reason": detail,
            "deleted_permanently": False,
        }
        results.append(item)
        if not ok:
            item["code"] = code
            failed.append(item)
    completed_count = sum(1 for item in results if item.get("ok"))
    failed_count = len(failed)
    detail_data = {
        "results": results,
        "partial": bool(completed_count and failed_count),
        "completed_count": completed_count,
        "failed_count": failed_count,
        "items": [
            {
                "item_id": item.get("item_id"),
                "path": item.get("path"),
                "status": item.get("status"),
                "ok": item.get("ok"),
                "reason": item.get("reason"),
            }
            for item in results
        ],
        "deleted_permanently": False,
    }
    if failed:
        failure_line = "部分文件没有移入系统回收站：失败项已保留文件（未做永久删除）"
        first_reason = next(
            (str(item.get("reason") or "") for item in failed if item.get("reason")),
            "",
        )
        if first_reason:
            failure_line += "；" + first_reason
        return _failure(
            failure_line,
            "recycle_partial_failure",
            detail=detail_data,
        )
    return _success(
        f"已把 {len(results)} 个文件移入系统回收站",
        detail=detail_data,
        recycled=True,
    )


# --------------------------------------------------------------------------
# 进程操作（复用 cmd_tools 的真实执行路径，不复制权限判定）
# --------------------------------------------------------------------------


def _mode_text(value: Any) -> str:
    from agent.security.launch import normalize_mode_value

    return normalize_mode_value(value)


class _ApprovedReplayGateway:
    """已批准操作的重放网关（契约 §3.3 / 问题 A）。

    批准只表示「不重复询问」：**不是**无条件 ALLOW。authorize() 必须先核对
    「本次要执行的效果」与审批单记录（payload / effect 快照 / 指纹）一致，才返回
    有界 ALLOW；以下情况一律 DENY：当前已切到只读模式、操作类型/argv/命令/cwd
    与审批记录不一致、没有审批记录快照（无法核对）。mode() 仍返回当前真实模式，
    cmd_tools 的只读执行层兜底照常生效。
    """

    def __init__(
        self,
        security: Any,
        *,
        operation: Any = None,
        approval_id: str | None = None,
    ) -> None:
        self._security = security
        self._operation = operation
        self._approval_id = approval_id

    def mode(self):
        if self._security is not None:
            try:
                return self._security.mode()
            except Exception:  # noqa: BLE001 - 读不到模式按受限（保守）
                pass
        from agent.security.modes import PermissionMode

        return PermissionMode.RESTRICTED

    def instruction(self):
        if self._security is None:
            return None
        try:
            return self._security.instruction()
        except Exception:  # noqa: BLE001
            return None

    def _expected_shape(self) -> dict:
        """审批单记录里的「应被执行的操作形状」（没有 effect 快照时从 payload 推导）。"""
        operation = self._operation
        if operation is None:
            return {}
        effect = dict(getattr(operation, "effect", {}) or {})
        detail = dict(effect.get("detail") or {})
        payload = dict(getattr(operation, "payload", {}) or {})
        shape: dict[str, Any] = {
            "kind": str(effect.get("kind") or ""),
            "argv": detail.get("argv"),
            "command": detail.get("command"),
            "cwd": detail.get("cwd"),
            "targets": [
                (str(item.get("kind") or ""), str(item.get("value") or ""))
                for item in (effect.get("targets") or [])
                if isinstance(item, dict)
            ],
            # R9-G：执行事实与永久删除绑定做逐项核对（旧审批单没有这些字段则跳过）
            "execution": dict(detail.get("execution") or {}),
            "delete_intent": dict(detail.get("delete_intent") or {}),
        }
        # 旧审批单可能没有 effect 快照：仍然按 payload 绑定（不是无条件放行）。
        if shape["argv"] is None and str(getattr(operation, "kind", "")) == "proc.run_program":
            program = str(payload.get("program") or "")
            if program:
                shape["argv"] = [program, *[str(a) for a in (payload.get("args") or [])]]
        if shape["command"] is None and str(getattr(operation, "kind", "")) == "proc.shell":
            command = str(payload.get("cmd") or "")
            if command:
                shape["command"] = command
        if shape["cwd"] is None and payload.get("cwd"):
            shape["cwd"] = str(payload.get("cwd"))
        return shape

    def _binding_error(self, effect) -> str | None:
        """返回不一致原因；None = 本次执行就是被批准的同一个操作。"""
        if self._operation is None:
            return "没有审批记录快照，无法核对这次执行是否就是被批准的同一个操作"
        expected = self._expected_shape()
        actual = security_gate.effect_to_dict(effect)
        actual_detail = dict(actual.get("detail") or {})
        if expected.get("kind") and str(actual.get("kind") or "") != str(expected["kind"]):
            return (
                f"审批记录的操作类型（{expected['kind']}）与本次执行"
                f"（{actual.get('kind')}）不一致"
            )
        expected_argv = expected.get("argv")
        if expected_argv:
            actual_argv = [str(item) for item in (actual_detail.get("argv") or [])]
            if [str(item) for item in expected_argv] != actual_argv:
                return "审批记录的程序/参数（argv）与本次执行不一致"
        expected_command = expected.get("command")
        if expected_command is not None and str(actual_detail.get("command") or "") != str(
            expected_command
        ):
            return "审批记录的 shell 命令与本次执行不一致"
        expected_paths = [
            value for kind, value in (expected.get("targets") or []) if kind == "path"
        ]
        actual_paths = [
            str(item.get("value") or "")
            for item in (actual.get("targets") or [])
            if isinstance(item, dict) and item.get("kind") == "path"
        ]
        if expected_paths and expected_paths != actual_paths:
            return "审批记录的对象/工作目录与本次执行不一致"
        # R9-G：执行事实（真实程序身份/argv/cwd/规格摘要）与永久删除绑定必须一致
        expected_execution = dict(expected.get("execution") or {})
        actual_execution = dict(actual_detail.get("execution") or {})
        if expected_execution:
            for key in ("kind", "program_path", "program_name", "shell", "spec_digest"):
                if key in expected_execution and expected_execution.get(key) != actual_execution.get(key):
                    return (
                        "审批记录的执行事实（" + key + "）与本次执行不一致："
                        "程序/参数/工作目录/脚本内容必须与批准时完全相同"
                    )
        expected_delete = dict(expected.get("delete_intent") or {})
        actual_delete = dict(actual_detail.get("delete_intent") or {})
        if expected_delete.get("permanent") != actual_delete.get("permanent"):
            return "审批记录的永久删除意图与本次执行不一致"
        if expected_delete.get("permanent"):
            expected_set = _delete_identity_set(expected_delete)
            actual_set = _delete_identity_set(actual_delete)
            if expected_set != actual_set:
                return "审批记录的永久删除对象集合/身份与本次执行不一致"
            if not bool(expected_delete.get("verified")) and bool(actual_delete.get("verified")):
                return "审批记录与本次执行的永久删除可解析性不一致"
            operation_payload = dict(getattr(self._operation, "payload", {}) or {})
            if not bool(operation_payload.get("explicit_confirmation_required")):
                return "审批单没有明确人工确认标记，不能按永久删除执行"
        return None

    async def authorize(self, effect, *, ctx=None):
        from agent.security.effects import AuthDecision, Verdict

        if _mode_text(self.mode()) == "read_only":
            return AuthDecision(
                verdict=Verdict.DENY,
                reason=(
                    "当前是只读模式：批准过的程序执行也不能绕过只读"
                    "（本次未执行，需切回受限/无限制模式后重新提出）"
                ),
                effects=(effect,),
            )
        problem = self._binding_error(effect)
        if problem:
            return AuthDecision(
                verdict=Verdict.DENY,
                reason="审批失效：" + problem + "；本次未执行（需要重新提出审批）",
                approval_id=self._approval_id,
                effects=(effect,),
            )
        return AuthDecision(
            verdict=Verdict.ALLOW,
            reason=(
                "用户已批准这次操作，且本次执行与审批记录一致"
                "（只免去重复询问；执行边界仍生效）"
            ),
            approval_id=self._approval_id,
            effects=(effect,),
        )


async def _run_approved_tool(tool, arguments: dict, *, label: str) -> ExecResult:
    try:
        result = await tool.run(**arguments)
    except Exception as exc:  # noqa: BLE001 - 执行失败也是审批结果
        return _failure(
            f"执行时出错，本次没有完成：{exc}",
            f"{type(exc).__name__}: {exc}",
            tool=label,
        )
    return _tool_result_to_exec(result, label=label)


def _reject_if_payload_changed(payload: dict, ctx: ExecutionContext) -> ExecResult | None:
    """重放前核对：本次 payload 必须就是审批单里记录的那一份（不执行任何东西）。"""
    approved = dict(getattr(ctx.operation, "payload", {}) or {})
    current = dict(payload or {})
    if current != approved:
        return _failure(
            "审批失效：本次执行的参数与审批单记录不一致（payload 被改动）："
            "程序/参数/工作目录/脚本内容必须与批准时完全相同，本次未执行，请重新提出",
            "invalidated: payload_changed",
            invalidated=True,
        )
    return None


def _delete_identity_set(delete_data: dict) -> list[list]:
    objects = []
    for item in (dict(delete_data or {}).get("objects") or ()):
        if isinstance(item, dict):
            objects.append(
                [
                    str(item.get("path") or ""),
                    str(item.get("identity") or ""),
                    bool(item.get("is_directory")),
                ]
            )
    return objects


async def _maybe_service_recheck(
    ctx: ExecutionContext, *, binding: dict, effect: dict
) -> tuple[bool, str, str] | None:
    """契约 §7 接线点：审批执行器/AppContext 注入的 RecheckService。

    约定（Lead 接线）：`ExecutionContext.recheck(binding, *, effect)` 或
    `ExecutionContext.recheck_service.recheck(binding, *, effect)` 返回
    `RecheckResult(ok, code, reason, refreshed)`。本模块不自行构造
    RecheckService（不猜构造签名）；未注入时返回 None，由本地重核对兜底并在结果
    detail 上如实标注 `recheck_mode=local_fallback`。
    """
    import inspect

    candidate = getattr(ctx, "recheck", None) or getattr(ctx, "recheck_service", None)
    if candidate is None:
        return None
    callback = getattr(candidate, "recheck", candidate)
    try:
        outcome = callback(binding, effect=effect)
    except TypeError:
        try:
            outcome = callback(binding)
        except Exception as exc:  # noqa: BLE001 - 重核对失败不能放行
            return False, "recheck_error", f"重核对服务调用失败：{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, "recheck_error", f"重核对服务调用失败：{type(exc).__name__}: {exc}"
    if inspect.isawaitable(outcome):
        try:
            outcome = await outcome
        except Exception as exc:  # noqa: BLE001
            return False, "recheck_error", f"重核对服务执行失败：{type(exc).__name__}: {exc}"
    if isinstance(outcome, tuple) and len(outcome) >= 2:
        return bool(outcome[0]), str(outcome[1] if len(outcome) == 2 else outcome[1]), ""
    return (
        bool(getattr(outcome, "ok", False)),
        str(getattr(outcome, "code", "") or ""),
        str(getattr(outcome, "reason", "") or ""),
    )


def _requirement_recheck_error(
    ctx: ExecutionContext, fingerprint: dict, payload: dict
) -> str | None:
    """执行前核对要求绑定：与 payload 一致、要求仍 active/版本一致。"""
    from agent.tools.cmd_tools import verify_requirement_binding_record

    from agent.tools.cmd_tools import _requirement_record_from_value

    binding = dict(fingerprint.get("operation_binding") or {})
    requirement = dict(binding.get("requirement") or fingerprint.get("requirement") or {})
    requirement_record = _requirement_record_from_value(requirement)
    payload_requirement = _requirement_record_from_value(payload.get("requirement_binding"))
    if payload_requirement.get("requirement_id") and requirement_record.get("requirement_id"):
        if (
            str(payload_requirement.get("requirement_id"))
            != str(requirement_record.get("requirement_id"))
            or payload_requirement.get("requirement_version")
            != requirement_record.get("requirement_version")
        ):
            return (
                "审批单 payload 与指纹中的用户要求绑定不一致（可能被改动）："
                "本次未执行，需要重新提出"
            )
    engine = getattr(ctx, "engine", None)
    ok, reason = verify_requirement_binding_record(
        requirement_record, security=engine, engine=engine
    )
    if ok:
        return None
    return "审批失效：" + reason + "（本次未执行）"


def _delete_recheck_error(
    fingerprint: dict, payload: dict, intent: Any
) -> str | None:
    """永久删除的确认绑定与对象身份重核对（契约 §5/§7）。"""
    fp_delete = dict(fingerprint.get("delete_intent") or {})
    fp_explicit = bool(fingerprint.get("explicit_confirmation_required"))
    payload_explicit = bool(payload.get("explicit_confirmation_required"))
    fp_permanent = bool(fp_delete.get("permanent"))
    current = None if intent is None else intent.as_dict()
    current_permanent = bool(current and current.get("permanent"))
    if not (fp_explicit or fp_permanent):
        if current_permanent:
            return (
                "审批单没有绑定永久删除（当前执行结构会永久删除对象）："
                "本次未执行，需要重新提出明确人工确认"
            )
        if payload_explicit:
            return "审批单 payload 声称永久删除但审批指纹没有绑定：本次未执行，需要重新提出"
        return None
    if not payload_explicit:
        return "审批单缺少明确人工确认标记（explicit_confirmation_required）：本次未执行"
    if not current_permanent:
        return (
            "审批时是永久删除，但当前执行结构不再是永久删除："
            "结构与审批记录不一致，本次未执行"
        )
    expected_set = _delete_identity_set(fp_delete)
    actual_set = [
        [str(path), str(identity or ""), bool(is_directory)]
        for path, identity, is_directory in intent.identity_set()
    ]
    if expected_set != actual_set:
        return (
            "永久删除的对象集合/身份与审批时不一致（对象被替换或命令被改动）："
            "本次未执行，需要重新确认"
        )
    if bool(fp_delete.get("verified")) != bool(intent.verified):
        return "永久删除的可解析性在审批后发生变化：本次未执行，需要重新确认"
    for item in intent.objects:
        if not item.identity and Path(item.path).exists():
            return (
                "永久删除对象在审批时没有可靠身份，现在却已存在："
                "无法核对是否为同一对象，本次未执行"
            )
    return None


def _execution_and_intent(payload: dict, computer: Any, *, shell: bool) -> tuple[Any, dict, Any]:
    """用 cmd_tools 同一套事实生成逻辑重建 spec/execution/delete intent（单一来源）。"""
    from agent.security import launch as launch_boundary
    from agent.tools.cmd_tools import RunCmdTool, RunProgramTool

    tool = (RunCmdTool if shell else RunProgramTool)(
        computer=computer, approvals=None, security=None
    )
    timeout = float(payload.get("timeout") or 30)
    cwd = str(payload.get("cwd") or "") or None
    if shell:
        spec = launch_boundary.LaunchSpec(
            kind="shell",
            program=None,
            argv=(),
            cwd=cwd,
            shell_cmd=str(payload.get("cmd") or ""),
            timeout=timeout,
        )
    else:
        spec = launch_boundary.LaunchSpec(
            kind="program",
            program=str(payload.get("program") or ""),
            argv=tuple(str(item) for item in (payload.get("args") or ())),
            cwd=cwd,
            timeout=timeout,
        )
    execution = tool._execution_facts(spec, shell=shell)
    execution["internal_data_roots"] = list(payload.get("internal_data_roots") or ())
    intent = tool._delete_intent(execution)
    return spec, execution, intent


async def _recheck_proc_launch(
    ctx: ExecutionContext,
    *,
    payload: dict,
    computer: Any,
    shell: bool,
) -> tuple[Any | None, ExecResult | None, dict]:
    """执行前重核对（契约 §7）：不通过 → 返回 invalidated ExecResult、不执行。

    本地兜底核对：payload/effect 快照/spec_digest/程序身份/脚本内容摘要/要求绑定/
    模式（无限制→受限）/内部数据三次确认/永久删除确认绑定与对象身份。
    若审批执行器/AppContext 注入了 RecheckService（ctx.recheck 或
    ctx.recheck_service），再调用它重新走引擎判定（契约 §7 接线点）。
    """
    from agent.security import launch as launch_boundary

    spec, execution, intent = _execution_and_intent(payload, computer, shell=shell)
    fingerprint = _fingerprint(ctx)
    effect = dict(getattr(ctx.operation, "effect", {}) or {})
    recheck_detail = {
        "mode": "local_fallback",
        "checks": [
            "payload",
            "effect_snapshot",
            "spec_digest",
            "program_identity",
            "content_digests",
            "requirement_binding",
            "mode",
            "internal_data_confirmations",
            "delete_confirmation_binding",
            "delete_object_identity",
        ],
        "approval_expiry": "checked_by_approval_executor",
    }
    unchanged, reason, digest = launch_boundary.recheck_approved_spec(
        spec,
        fingerprint,
        computer=computer,
        effect=effect,
        payload=ctx.operation.payload,
    )
    if not unchanged:
        return (
            None,
            _failure(
                "审批失效：" + reason + "（本次未执行）",
                "invalidated: " + (reason or "spec_changed"),
                invalidated=True,
                spec_digest=digest,
            ),
            recheck_detail,
        )
    recheck_detail["spec_digest"] = digest

    requirement_error = _requirement_recheck_error(ctx, fingerprint, payload)
    if requirement_error:
        return (
            None,
            _failure(
                requirement_error,
                "invalidated: requirement_binding",
                invalidated=True,
            ),
            recheck_detail,
        )

    mode_now = _mode_text(ctx.current_mode())
    binding = dict(fingerprint.get("operation_binding") or {})
    binding_mode = str(binding.get("mode") or "")
    recheck_detail["mode_now"] = mode_now
    recheck_detail["binding_mode"] = binding_mode or None
    if mode_now == "read_only":
        return (
            None,
            _failure(
                "当前已切换到只读模式：这次批准不再允许执行程序/命令"
                "（只读模式不能被审批绕过），本次未执行，需重新提出",
                "invalidated: mode_read_only",
                invalidated=True,
            ),
            recheck_detail,
        )
    if binding_mode and binding_mode != mode_now and binding_mode == "unrestricted":
        return (
            None,
            _failure(
                "审批时是完全无限制模式，现在已切回受限模式："
                "需要按当前模式重新判定（固定规则/授权/审批），本次未执行，需重新提出",
                "invalidated: mode_binding_changed",
                invalidated=True,
            ),
            recheck_detail,
        )

    internal_roots = [str(item) for item in (fingerprint.get("internal_data_roots") or ())]
    try:
        confirmations = int(getattr(ctx.operation, "confirmations_required", 1) or 1)
    except (TypeError, ValueError):
        confirmations = 1
    recheck_detail["confirmations_required"] = confirmations
    if internal_roots and confirmations < 3:
        return (
            None,
            _failure(
                "目标是 QIO 内部运行数据，但没有完成连续三次确认："
                "普通一次确认不能替代三次，本次未执行",
                "internal_confirmations_missing",
                invalidated=True,
            ),
            recheck_detail,
        )

    delete_error = _delete_recheck_error(fingerprint, payload, intent)
    if delete_error:
        return (
            None,
            _failure(
                delete_error,
                "invalidated: delete_binding",
                invalidated=True,
            ),
            recheck_detail,
        )
    if intent is not None and intent.permanent:
        recheck_detail["delete_binding"] = "verified"

    service_outcome = await _maybe_service_recheck(ctx, binding=binding, effect=effect)
    if service_outcome is not None:
        ok, code, service_reason = service_outcome
        recheck_detail.update(
            {
                "mode": "service",
                "service_code": str(code or "")[:120],
                "service_reason": str(service_reason or "")[:300],
            }
        )
        if not ok:
            return (
                None,
                _failure(
                    "执行前重核对失败："
                    + (service_reason or code or "recheck_failed")
                    + "（本次未执行）",
                    "invalidated: recheck_service",
                    invalidated=True,
                ),
                recheck_detail,
            )

    allow_roots = tuple(
        str(item) for item in (fingerprint.get("allowed_roots") or ()) if str(item)
    ) or launch_boundary.write_roots_for(computer, mode_now)
    confirmed = bool(internal_roots) and confirmations >= 3
    permit = launch_boundary.build_permit(
        spec,
        computer=computer,
        mode=mode_now,
        allowed_roots=allow_roots,
        allowed_operations=("execute",),
        network_hosts=None,
        internal_data_confirmed=confirmed,
        approval_id=ctx.approval_id,
        source="approval",
        reason=(
            "用户已批准这次操作；执行前已重核对 payload/要求绑定/模式/程序身份/"
            "内容摘要/永久删除确认绑定"
        ),
    )
    return permit, None, recheck_detail


async def execute_proc_run_program(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    from agent.tools.cmd_tools import RunProgramTool

    changed = _reject_if_payload_changed(payload, ctx)
    if changed is not None:
        return changed
    program = str(payload.get("program") or "").strip()
    if not program:
        return _failure("审批单缺少 program：未执行", "missing_program")
    args = [str(item) for item in (payload.get("args") or [])]
    timeout = float(payload.get("timeout") or 30)
    cwd = str(payload.get("cwd") or "") or None
    permit, rejected, recheck_detail = await _recheck_proc_launch(
        ctx, payload=payload, computer=computer, shell=False
    )
    if rejected is not None:
        return rejected
    tool = RunProgramTool(
        computer=computer,
        approvals=None,
        security=_ApprovedReplayGateway(
            security, operation=ctx.operation, approval_id=ctx.approval_id
        ),
    )
    result = await _run_approved_tool(
        tool,
        {
            "program": program,
            "args": args,
            "cwd": cwd,
            "timeout": timeout,
            "_launch_permit": permit,
        },
        label="proc.run_program",
    )
    detail = dict(result.detail or {})
    detail["permit"] = permit.as_dict()
    detail["approval_id"] = ctx.approval_id
    detail["recheck"] = recheck_detail
    return ExecResult(
        ok=result.ok,
        state=result.state,
        summary=result.summary,
        error=result.error,
        detail=detail,
    )


async def execute_proc_shell(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    from agent.tools.cmd_tools import RunCmdTool

    changed = _reject_if_payload_changed(payload, ctx)
    if changed is not None:
        return changed
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        return _failure("审批单缺少 cmd：未执行", "missing_cmd")
    timeout = float(payload.get("timeout") or 30)
    cwd = str(payload.get("cwd") or "") or None
    permit, rejected, recheck_detail = await _recheck_proc_launch(
        ctx, payload=payload, computer=computer, shell=True
    )
    if rejected is not None:
        return rejected
    tool = RunCmdTool(
        computer=computer,
        approvals=None,
        security=_ApprovedReplayGateway(
            security, operation=ctx.operation, approval_id=ctx.approval_id
        ),
    )
    result = await _run_approved_tool(
        tool,
        {
            "cmd": cmd,
            "cwd": cwd,
            "timeout": timeout,
            "_launch_permit": permit,
        },
        label="proc.shell",
    )
    detail = dict(result.detail or {})
    detail["permit"] = permit.as_dict()
    detail["approval_id"] = ctx.approval_id
    detail["recheck"] = recheck_detail
    return ExecResult(
        ok=result.ok,
        state=result.state,
        summary=result.summary,
        error=result.error,
        detail=detail,
    )


async def execute_proc_kill(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    changed = _reject_if_payload_changed(payload, ctx)
    if changed is not None:
        return changed
    pid = str(payload.get("pid") or "").strip()
    if not pid:
        return _failure("审批单缺少 pid：未执行", "missing_pid")
    expected_created = str(payload.get("process_created_at") or "").strip()
    if expected_created:
        from agent.tools.cmd_tools import process_creation_token

        current_created = process_creation_token(pid)
        if not current_created or current_created != expected_created:
            return _failure(
                "审批失效：pid 已指向另一个进程（进程生命周期不一致），本次未结束任何进程",
                "invalidated: process_identity_changed",
                invalidated=True,
            )
    from agent.security.operations import replay_gateway_kwargs
    from agent.tools.cmd_tools import ProcKillTool

    # R9-H4 §10.2：结束进程重放必须携带原审批快照/操作依据，否则永远报
    # “没有审批记录”。这里与 run_program/shell 使用同一快照接口。
    tool = ProcKillTool(
        computer=computer,
        approvals=None,
        security=_ApprovedReplayGateway(security, **replay_gateway_kwargs(ctx)),
    )
    return await _run_approved_tool(tool, {"pid": pid}, label="proc.kill")


# --------------------------------------------------------------------------
# 附件（W3：services/attachments.py + services/attachment_access.py）
# --------------------------------------------------------------------------


def _w3_services():
    """W3 的文件身份 / 回收站实现（延迟导入：模块还没合入时如实不可用）。"""
    from agent.services import file_identity as w3_identity  # type: ignore

    return w3_identity


def _w3_identity_key(path: str) -> str | None:
    try:
        w3 = _w3_services()
        found = w3.identity_of(Path(path))
    except Exception:  # noqa: BLE001 - 身份读不到按不可核对处理
        return None
    return str(getattr(found, "key", "") or "") or None


def _w3_content_sha(path: str) -> str | None:
    try:
        w3 = _w3_services()
        return w3.content_sha256(Path(path))
    except Exception:  # noqa: BLE001
        return None


async def execute_attachment_write_back(
    payload: dict,
    ctx: ExecutionContext,
    *,
    attachments_getter: Callable[[], Any] | None,
) -> ExecResult:
    """attachment.write_back：批准后真正写回源文件（W3 的已授权实现）。"""
    service = attachments_getter() if callable(attachments_getter) else None
    if service is None:
        return _failure(
            "附件服务暂不可用：没有写回任何文件（需要应用完整装配）",
            "attachment_service_unavailable",
        )
    attachment_id = str(payload.get("attachment_id") or "")
    path = str(payload.get("path") or "")
    encoded = payload.get("content_b64")
    if not attachment_id or not path or encoded is None:
        return _failure(
            "审批单缺少 attachment_id/path/content_b64：未写回",
            "missing_attachment_payload",
        )
    try:
        import base64

        data = base64.b64decode(str(encoded), validate=True)
    except Exception as exc:  # noqa: BLE001 - 审批单内容不是合法 base64：不写
        return _failure(
            f"审批单里的写回内容不是合法 base64：未写回（{type(exc).__name__}）",
            "invalid_content_encoding",
            path=path,
            attachment_id=attachment_id,
        )
    fingerprint = _fingerprint(ctx)
    binding_error = _review_binding_mismatch(ctx) or _payload_content_mismatch(ctx, payload)
    if binding_error:
        return _failure(
            binding_error,
            "invalidated: operation_binding",
            invalidated=True,
            path=path,
            attachment_id=attachment_id,
        )
    expected_new_sha = str(payload.get("sha256") or fingerprint.get("new_sha256") or "")
    if expected_new_sha:
        import hashlib as _hashlib

        actual = _hashlib.sha256(data).hexdigest()
        if actual != expected_new_sha:
            return _failure(
                "待写回内容与提交审批时不一致：本次未写回（需重新审批）",
                "invalidated: new_content_sha256_mismatch",
                invalidated=True,
                path=path,
                attachment_id=attachment_id,
            )
    expected_identity = str(fingerprint.get("identity") or payload.get("identity") or "")
    expected_old_sha = str(fingerprint.get("content_sha256") or "")
    if expected_identity:
        current_key = _w3_identity_key(path)
        if current_key is None or current_key != expected_identity:
            return _failure(
                "源文件已不是审批时的那一份（身份变化）：本次未写回（请重新读取后再确认）",
                "invalidated: source_identity_changed",
                invalidated=True,
                path=path,
                attachment_id=attachment_id,
            )
    if expected_old_sha:
        current_sha = _w3_content_sha(path)
        if current_sha is None or current_sha != expected_old_sha:
            return _failure(
                "源文件内容在审批期间发生了变化：本次未写回（请重新读取后再确认）",
                "invalidated: source_content_changed",
                invalidated=True,
                path=path,
                attachment_id=attachment_id,
            )
    blocked = await _recheck_requirement_and_mode(
        ctx, payload=payload, fingerprint=fingerprint, path=path
    )
    if blocked is not None:
        return blocked
    try:
        outcome = await asyncio.to_thread(
            service._write_back_source_now,
            attachment_id,
            data,
            expected_identity=expected_identity or None,
            expected_sha256=expected_old_sha or None,
        )
    except Exception as exc:  # noqa: BLE001 - 写回失败/重核对失败都如实落库
        return _failure(
            f"写回源文件失败（原文件保留）：{_redacted(exc)}",
            f"{type(exc).__name__}: {_redacted(exc)}",
            path=path,
            attachment_id=attachment_id,
        )
    outcome = dict(outcome or {})
    # R9-F：写回/原子替换成功后更新来源与文件身份（W3），失败不改变写回结果。
    note = getattr(service, "note_origin_after_write_back", None)
    if callable(note):
        try:
            note(attachment_id, outcome)
        except Exception:  # noqa: BLE001 - 来源记账失败不改变写回事实
            pass
    return _success(
        f"已写回源文件：{path}",
        path=str(outcome.get("path") or path),
        attachment_id=attachment_id,
        bytes=int(outcome.get("bytes") or len(data)),
        sha256=outcome.get("sha256"),
        previous_sha256=outcome.get("previous_sha256"),
        identity=outcome.get("identity"),
        identity_changed=bool(outcome.get("identity_changed")),
    )


async def execute_attachment_batch_write_back(
    payload: dict,
    ctx: ExecutionContext,
    *,
    attachments_getter: Callable[[], Any] | None,
) -> ExecResult:
    """attachment.batch_write_back：批准后批量写回，逐项重核对 + 逐项状态（契约 §6）。"""
    service = attachments_getter() if callable(attachments_getter) else None
    if service is None:
        return _failure(
            "附件服务暂不可用：没有写回任何文件（需要应用完整装配）",
            "attachment_service_unavailable",
        )
    items = [dict(item) for item in (payload.get("items") or []) if isinstance(item, dict)]
    if not items:
        return _failure("审批单缺少批量写回清单：未写回", "missing_batch_items")
    fingerprint = _fingerprint(ctx)
    fingerprint_items = {
        str(entry.get("attachment_id") or ""): entry
        for entry in (fingerprint.get("items") or [])
        if isinstance(entry, dict)
    }
    binding_error = _review_binding_mismatch(ctx)
    if binding_error:
        return _failure(binding_error, "invalidated: operation_binding", invalidated=True)
    import base64 as _base64
    import hashlib as _hashlib

    results: list[dict] = []
    all_ok = True
    for item in items:
        item_id = str(item.get("item_id") or "")
        attachment_id = str(item.get("attachment_id") or "")
        path = str(item.get("path") or "")
        reason = ""
        status = "failed"
        outcome: dict = {}
        blocked = None
        try:
            data = _base64.b64decode(str(item.get("content_b64") or ""), validate=True)
        except Exception as exc:  # noqa: BLE001 - 非法内容不写
            data = None
            reason = f"写回内容不是合法 base64：{type(exc).__name__}"
        if data is not None:
            expected_new_sha = str(item.get("sha256") or "")
            if expected_new_sha and _hashlib.sha256(data).hexdigest() != expected_new_sha:
                reason = "待写回内容与提交审批时不一致（需重新审批）"
                blocked = True
        if data is not None and blocked is None:
            blocked_result = await _recheck_requirement_and_mode(
                ctx, payload=payload, fingerprint=fingerprint, path=path
            )
            if blocked_result is not None:
                reason = blocked_result.summary
                blocked = True
        if data is not None and blocked is None:
            expected_identity = str(item.get("identity") or "")
            expected_old_sha = str(
                (fingerprint_items.get(attachment_id) or {}).get("content_sha256") or ""
            )
            if expected_identity:
                current_key = _w3_identity_key(path)
                if current_key is None or current_key != expected_identity:
                    reason = "源文件已不是审批时的那一份（身份变化）"
                    blocked = True
            if blocked is None and expected_old_sha:
                current_sha = _w3_content_sha(path)
                if current_sha is None or current_sha != expected_old_sha:
                    reason = "源文件内容在审批期间发生了变化"
                    blocked = True
        if data is not None and blocked is None:
            try:
                outcome = await asyncio.to_thread(
                    service._write_back_source_now,
                    attachment_id,
                    data,
                    expected_identity=str(item.get("identity") or "") or None,
                    expected_sha256=str(
                        (fingerprint_items.get(attachment_id) or {}).get("content_sha256")
                        or ""
                    )
                    or None,
                )
                status = "completed"
            except Exception as exc:  # noqa: BLE001 - 失败保留文件
                reason = f"{type(exc).__name__}: {_redacted(exc)}"
        ok = status == "completed"
        all_ok = all_ok and ok
        if ok:
            note = getattr(service, "note_origin_after_write_back", None)
            if callable(note):
                try:
                    note(attachment_id, outcome)
                except Exception:  # noqa: BLE001
                    pass
        results.append(
            {
                "item_id": item_id,
                "attachment_id": attachment_id,
                "path": path,
                "ok": ok,
                "status": status if not blocked else "invalidated",
                "reason": reason,
                "sha256": outcome.get("sha256"),
            }
        )
    detail = {
        "batch": True,
        "items": results,
        "results": results,
        "deleted_permanently": False,
    }
    if not all_ok:
        return _failure(
            "批量写回未全部完成：失效/失败的项已保留文件",
            "batch_partial_failure",
            detail=detail,
        )
    return _success("已批量写回 %d 个附件源文件" % len(results), detail=detail)


async def execute_attachment_recycle(
    payload: dict,
    ctx: ExecutionContext,
    *,
    recycle_mover: Callable[..., Any] | None = None,
) -> ExecResult:
    """attachment.recycle：批准后把用户材料逐个移入系统回收站（失败保留文件）。"""
    targets = [dict(item) for item in (payload.get("targets") or []) if isinstance(item, dict)]
    if not targets:
        return _failure("审批单缺少待删除的用户材料清单：未删除", "missing_targets")
    binding_error = _review_binding_mismatch(ctx)
    if binding_error:
        return _failure(binding_error, "invalidated: operation_binding", invalidated=True)
    if recycle_mover is None:
        try:
            from agent.services import recycle_bin as recycle_module  # type: ignore

            recycle_mover = recycle_module.send_to_recycle_bin
        except Exception:  # noqa: BLE001 - W3 模块不可用：保留文件
            return _failure(
                "回收站路径暂不可用：文件全部保留，未删除（不会直接 unlink）",
                "recycle_bin_unavailable",
                file_preserved=True,
            )
    fingerprint = _fingerprint(ctx)
    fingerprint_items: dict[str, dict] = {}
    for entry in fingerprint.get("items") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("attachment_id"):
            fingerprint_items[str(entry.get("attachment_id"))] = entry
        if entry.get("path"):
            fingerprint_items[str(entry.get("path"))] = entry
    results: list[dict] = []
    all_ok = True
    for item in targets:
        path = str(item.get("path") or "")
        if not path:
            results.append({"path": path, "ok": False, "reason": "缺少路径"})
            all_ok = False
            continue
        blocked = await _recheck_requirement_and_mode(
            ctx, payload=payload, fingerprint=fingerprint, path=path
        )
        if blocked is not None:
            results.append(
                {
                    "attachment_id": item.get("attachment_id"),
                    "item_id": item.get("item_id"),
                    "path": path,
                    "ok": False,
                    "status": "invalidated",
                    "invalidated": True,
                    "reason": blocked.summary,
                }
            )
            all_ok = False
            continue
        expected_identity = str(item.get("identity") or "")
        if expected_identity:
            current_key = _w3_identity_key(path)
            if current_key is None or current_key != expected_identity:
                results.append(
                    {
                        "path": path,
                        "ok": False,
                        "identity_changed": True,
                        "reason": "对象身份已变化，未删除（保留文件）",
                    }
                )
                all_ok = False
                continue
        meta = fingerprint_items.get(str(item.get("attachment_id") or "")) or fingerprint_items.get(path) or {}
        expected_content = str(meta.get("content_sha256") or "")
        if expected_content and not bool(meta.get("is_directory")):
            current_content = await asyncio.to_thread(_w3_content_sha, path)
            if current_content is None or current_content != expected_content:
                results.append(
                    {
                        "attachment_id": item.get("attachment_id"),
                        "item_id": item.get("item_id"),
                        "path": path,
                        "ok": False,
                        "status": "invalidated",
                        "invalidated": True,
                        "reason": "文件内容在审批期间发生了变化：未删除（保留文件，需重新确认）",
                    }
                )
                all_ok = False
                continue
        try:
            outcome = recycle_mover(path)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:  # noqa: BLE001 - 失败保留文件
            outcome = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
        outcome = dict(outcome or {})
        ok = bool(outcome.get("ok"))
        all_ok = all_ok and ok
        if ok:
            await asyncio.to_thread(_note_recycled_origin, path)
        results.append(
            {
                "attachment_id": item.get("attachment_id"),
                "item_id": item.get("item_id"),
                "path": path,
                "ok": ok,
                "status": "completed" if ok else "failed",
                "mechanism": outcome.get("mechanism"),
                "reason": outcome.get("reason") or item.get("reason") or "",
                "deleted_permanently": bool(outcome.get("deleted_permanently", False)),
                "trashed_path": outcome.get("trashed_path"),
            }
        )
    detail = {"results": results, "deleted_permanently": False}
    if not all_ok:
        return _failure(
            "部分用户材料没有移入回收站：失败项已保留文件（未做永久删除）",
            "recycle_partial_failure",
            detail=detail,
        )
    return _success(
        f"已把 {len(results)} 个用户材料移入系统回收站",
        detail=detail,
    )


async def execute_attachment_read(
    payload: dict,
    ctx: ExecutionContext,
    *,
    attachments_getter: Callable[[], Any] | None,
) -> ExecResult:
    """attachment.read：批准后真正读取附件源文件（只读）。"""
    service = attachments_getter() if callable(attachments_getter) else None
    if service is None:
        return _failure(
            "附件服务暂不可用：本次没有读取任何内容",
            "attachment_service_unavailable",
        )
    attachment_id = str(payload.get("attachment_id") or "")
    if not attachment_id:
        return _failure("审批单缺少 attachment_id：本次没有读取", "missing_attachment_id")
    try:
        offset = max(0, int(payload.get("offset") or 0))
        limit = max(1, min(int(payload.get("limit") or 4000), 20000))
    except (TypeError, ValueError):
        offset, limit = 0, 4000
    try:
        resolved = await asyncio.to_thread(service.resolve_source, attachment_id)
    except Exception as exc:  # noqa: BLE001
        return _failure(
            f"解析附件源文件失败：{_redacted(exc)}",
            f"{type(exc).__name__}: {_redacted(exc)}",
            attachment_id=attachment_id,
        )
    path = getattr(resolved, "path", None)
    if path is None:
        return _failure(
            "附件源文件现在不可用：" + str(getattr(resolved, "reason", "") or "没有可读内容"),
            "attachment_source_unavailable",
            attachment_id=attachment_id,
            state=str(getattr(resolved, "state", "") or ""),
        )
    try:
        raw = await asyncio.to_thread(Path(str(path)).read_bytes)
    except OSError as exc:
        return _failure(
            f"读取附件源文件失败：{_redacted(exc)}",
            f"{type(exc).__name__}: {_redacted(exc)}",
            attachment_id=attachment_id,
            path=str(path),
        )
    text = raw.decode("utf-8", errors="replace")
    preview = text[offset : offset + limit]
    return _success(
        f"已读取附件源文件：{Path(str(path)).name}（{len(raw)} 字节，本次返回 {len(preview)} 字符）",
        attachment_id=attachment_id,
        path=str(path),
        size=len(raw),
        offset=offset,
        limit=limit,
        truncated=offset + len(preview) < len(text),
        preview=_redacted(preview),
    )


# --------------------------------------------------------------------------
# 子智能体 / 未实现执行器
# --------------------------------------------------------------------------


async def execute_subagent_create(
    payload: dict, ctx: ExecutionContext, *, tool_lookup: Callable[[str], Any] | None
) -> ExecResult:
    name = str(payload.get("tool") or "").strip()
    if not name:
        return _failure("审批单缺少 tool 名称：未执行", "missing_tool")
    tool = tool_lookup(name) if callable(tool_lookup) else None
    if tool is None:
        return _failure(
            "创建子智能体所需的工具已不存在，本次未执行",
            "tool_not_found: " + name,
            tool=name,
        )
    runner = getattr(tool, "execute_after_approval", None)
    if not callable(runner):
        return _failure(
            "这个子智能体工具没有提供审批执行入口，本次未执行（需重新提出）",
            "executor_unavailable: subagent.create",
            tool=name,
        )
    try:
        result = await runner(dict(payload))
    except Exception as exc:  # noqa: BLE001 - 执行失败也是审批结果
        return _failure(
            f"重建子智能体失败，本次未完成：{exc}",
            f"{type(exc).__name__}: {exc}",
            tool=name,
        )
    return _tool_result_to_exec(result, label="subagent.create")


def _unavailable(reason: str) -> Callable[[dict, ExecutionContext], Any]:
    async def handler(payload: dict, ctx: ExecutionContext) -> ExecResult:
        return _failure(
            reason,
            "executor_unavailable: " + str(getattr(ctx.operation, "kind", "")),
            executor_unavailable=True,
            kind=str(getattr(ctx.operation, "kind", "")),
        )

    return handler


# --------------------------------------------------------------------------
# 注册入口
# --------------------------------------------------------------------------


def register_approved_operations(
    registry: OperationRegistry,
    *,
    computer: Any = None,
    security: Any = None,
    tool_lookup: Callable[[str], Any] | None = None,
    attachments_getter: Callable[[], Any] | None = None,
) -> list[str]:
    """把审批后执行器注册到 OperationRegistry；重复接线时幂等替换。

    返回实际注册的 kind 列表（诊断/测试用）。
    """
    registered: list[str] = []

    def add(kind: str, handler: Callable[[dict, ExecutionContext], Any]) -> None:
        registry.register(kind, handler, replace=True)
        registered.append(kind)

    # 文件操作：不依赖 AppContext 的其它服务，永远可用。
    add("fs.write", execute_fs_write)
    add("fs.patch", execute_fs_patch)
    add("fs.delete", execute_fs_delete)

    # 进程操作：需要真实的 ComputerSandbox（与工具同一份风险分类器）。
    if computer is not None:
        add(
            "proc.run_program",
            lambda payload, ctx: execute_proc_run_program(
                payload, ctx, computer=computer, security=security
            ),
        )
        add(
            "proc.shell",
            lambda payload, ctx: execute_proc_shell(
                payload, ctx, computer=computer, security=security
            ),
        )
        add(
            "proc.kill",
            lambda payload, ctx: execute_proc_kill(
                payload, ctx, computer=computer, security=security
            ),
        )
    else:
        reason = "没有可用的 ComputerSandbox：进程操作执行器暂不可用，本次未执行"
        add("proc.run_program", _unavailable(reason))
        add("proc.shell", _unavailable(reason))
        add("proc.kill", _unavailable(reason))

    # 子智能体：按审批单里快照的 AgentScope 子集重建。
    add(
        "subagent.create",
        lambda payload, ctx: execute_subagent_create(
            payload, ctx, tool_lookup=tool_lookup
        ),
    )

    # 附件（W3）：写回源文件 / 读取 / 删除进系统回收站（均用 W3 已授权实现）。
    if callable(attachments_getter):
        add(
            "attachment.write_back",
            lambda payload, ctx: execute_attachment_write_back(
                payload, ctx, attachments_getter=attachments_getter
            ),
        )
        add(
            "attachment.read",
            lambda payload, ctx: execute_attachment_read(
                payload, ctx, attachments_getter=attachments_getter
            ),
        )
    else:
        reason = "附件服务没有接线：附件写回/读取执行器暂不可用，本次未执行"
        add("attachment.write_back", _unavailable(reason))
        add("attachment.read", _unavailable(reason))
    add("attachment.recycle", execute_attachment_recycle)
    add(
        "attachment.batch_write_back",
        lambda payload, ctx: execute_attachment_batch_write_back(
            payload, ctx, attachments_getter=attachments_getter
        ),
    )

    # 当前还没有独立执行器的类型：显式注册「不可用」，不允许静默丢单。
    add(
        "dep.install",
        _unavailable(
            "依赖安装没有独立的审批执行器：安装由工具环境准备路径直接执行；本次未执行"
        ),
    )
    add(
        "net.egress",
        _unavailable(
            "网络出口没有独立的审批执行器：当前没有可重放的网络发送动作；本次未执行"
        ),
    )
    add(
        "internal.data_write",
        _unavailable(
            "内部运行数据直接维护没有独立的审批执行器；AI 文件工具的内部数据写入"
            "以 fs.write（已完成三次确认）执行。本次未执行"
        ),
    )
    logger.info("approved operations registered: %s", ", ".join(registered))
    return registered


__all__ = [
    "register_approved_operations",
    "execute_fs_write",
    "execute_fs_patch",
    "execute_fs_delete",
    "execute_proc_run_program",
    "execute_proc_shell",
    "execute_proc_kill",
    "execute_subagent_create",
    "execute_attachment_write_back",
    "execute_attachment_recycle",
    "execute_attachment_read",
]
