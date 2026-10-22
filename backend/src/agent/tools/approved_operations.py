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
import inspect
import logging
from pathlib import Path
from typing import Any, Callable

from agent.security.operations import ExecutionContext, ExecResult, OperationRegistry
from agent.tools import security_gate
from agent.trace.redact import redact_text

logger = logging.getLogger(__name__)


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
    if not root:
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
            internal_data_root=str(root),
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


async def execute_fs_write(payload: dict, ctx: ExecutionContext) -> ExecResult:
    path = str(payload.get("path") or "").strip()
    if not path:
        return _failure("审批单缺少目标路径：未执行", "missing_path")
    if "content" not in payload or payload.get("content") is None:
        return _failure("审批单缺少待写入内容：未执行", "missing_content", path=path)
    content = str(payload.get("content"))
    fingerprint = _fingerprint(ctx)

    blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
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
    if base_digest:
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
    try:
        await asyncio.to_thread(security_gate.atomic_write_text, path, content)
    except OSError as exc:
        return _failure(
            f"写入失败（原文件保持原样）：{exc}",
            f"{type(exc).__name__}: {exc}",
            path=path,
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

    blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
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
    if base_digest:
        current_digest = await asyncio.to_thread(security_gate.content_sha256, path)
        if current_digest != base_digest:
            return _failure(
                "目标内容在审批期间发生了变化：本次未修改（需重新审批）",
                "invalidated: base_content_changed",
                invalidated=True,
                path=path,
            )
    try:
        current = await asyncio.to_thread(
            Path(path).read_text, encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        return _failure(
            f"读取目标失败，本次未修改：{exc}",
            f"{type(exc).__name__}: {exc}",
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
    try:
        await asyncio.to_thread(security_gate.atomic_write_text, path, updated)
    except OSError as exc:
        return _failure(
            f"写入失败（原文件保持原样）：{exc}",
            f"{type(exc).__name__}: {exc}",
            path=path,
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


async def execute_fs_delete(payload: dict, ctx: ExecutionContext) -> ExecResult:
    path = str(payload.get("path") or "").strip()
    if not path:
        return _failure("审批单缺少目标路径：未执行", "missing_path")
    fingerprint = _fingerprint(ctx)

    blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
    if blocked is not None:
        return blocked

    expected = _expected_identity(payload, fingerprint)
    same, reason = _check_identity(path, expected)
    if not same:
        return _failure(
            f"对象已变化，本次未删除：{reason}",
            f"invalidated: {reason}",
            invalidated=True,
            path=path,
        )
    ok, code, detail = await _move_to_recycle_bin(path)
    if not ok:
        unavailable = code == "recycle_bin_unavailable"
        return _failure(
            (
                "回收站路径暂不可用：文件已保留，未删除（不会直接 unlink）"
                if unavailable
                else "回收站操作失败：文件已保留，未删除（不会直接 unlink）"
            )
            + (f"；原因：{detail}" if detail else ""),
            f"{code}: {detail}" if detail else code,
            path=path,
            file_preserved=True,
        )
    return _success(f"已移入系统回收站：{path}", path=path, recycled=True)


# --------------------------------------------------------------------------
# 进程操作（复用 cmd_tools 的真实执行路径，不复制权限判定）
# --------------------------------------------------------------------------


class _ApprovedReplayGateway:
    """已批准操作的重放网关。

    审批执行器在领取**之前**已经做过模式快照与对象重核对；这里不再重复向用户
    申请审批（否则批准后又会弹一次）。但 `mode()` 仍返回当前真实模式，于是
    cmd_tools 的只读执行层兜底照常生效（不会因为「批准过」就绕过只读）。
    """

    def __init__(self, security: Any) -> None:
        self._security = security

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

    async def authorize(self, effect, *, ctx=None):
        from agent.security.effects import AuthDecision, Verdict

        return AuthDecision(
            verdict=Verdict.ALLOW,
            reason="用户已批准这次操作；审批执行器已完成模式与对象重核对",
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


async def execute_proc_run_program(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    program = str(payload.get("program") or "").strip()
    if not program:
        return _failure("审批单缺少 program：未执行", "missing_program")
    args = [str(item) for item in (payload.get("args") or [])]
    from agent.tools.cmd_tools import RunProgramTool

    tool = RunProgramTool(
        computer=computer,
        approvals=None,
        security=_ApprovedReplayGateway(security),
    )
    return await _run_approved_tool(
        tool,
        {
            "program": program,
            "args": args,
            "cwd": payload.get("cwd"),
            "timeout": payload.get("timeout") or 30,
        },
        label="proc.run_program",
    )


async def execute_proc_shell(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        return _failure("审批单缺少 cmd：未执行", "missing_cmd")
    from agent.tools.cmd_tools import RunCmdTool

    tool = RunCmdTool(
        computer=computer,
        approvals=None,
        security=_ApprovedReplayGateway(security),
    )
    return await _run_approved_tool(
        tool,
        {
            "cmd": cmd,
            "cwd": payload.get("cwd"),
            "timeout": payload.get("timeout") or 30,
        },
        label="proc.shell",
    )


async def execute_proc_kill(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    pid = str(payload.get("pid") or "").strip()
    if not pid:
        return _failure("审批单缺少 pid：未执行", "missing_pid")
    from agent.tools.cmd_tools import ProcKillTool

    tool = ProcKillTool(
        computer=computer,
        approvals=None,
        security=_ApprovedReplayGateway(security),
    )
    return await _run_approved_tool(tool, {"pid": pid}, label="proc.kill")


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
]
