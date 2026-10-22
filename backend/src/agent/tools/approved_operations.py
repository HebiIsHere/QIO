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
            if identity is None and index < len(fingerprint_rows):
                candidate = fingerprint_rows[index]
                if isinstance(candidate, dict):
                    identity = candidate.get("identity")
            if path:
                rows.append({"path": path, "identity": identity})
    elif str(payload.get("path") or "").strip():
        rows.append(
            {
                "path": str(payload.get("path")).strip(),
                "identity": fingerprint.get("identity"),
            }
        )
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


async def execute_fs_delete(payload: dict, ctx: ExecutionContext) -> ExecResult:
    fingerprint = _fingerprint(ctx)
    rows = _delete_rows(payload, fingerprint)
    if not rows:
        return _failure("审批单缺少目标路径：未执行", "missing_path")
    results: list[dict] = []
    failed: list[dict] = []
    for row in rows:
        path = row["path"]
        blocked = _require_internal_data_confirmations(ctx, fingerprint, path)
        if blocked is not None:
            item = {
                "path": path,
                "ok": False,
                "invalidated": True,
                "reason": blocked.summary,
            }
            results.append(item)
            failed.append(item)
            continue
        same, reason = _identity_matches(path, row.get("identity"))
        if not same:
            item = {
                "path": path,
                "ok": False,
                "invalidated": True,
                "reason": reason,
            }
            results.append(item)
            failed.append(item)
            continue
        ok, code, detail = await _move_to_recycle_bin(path)
        if ok:
            await asyncio.to_thread(_note_recycled_origin, path)
        item = {
            "path": path,
            "ok": bool(ok),
            "mechanism": code if ok else None,
            "reason": detail,
            "deleted_permanently": False,
        }
        results.append(item)
        if not ok:
            item["code"] = code
            failed.append(item)
    detail_data = {"results": results, "deleted_permanently": False}
    if failed:
        return _failure(
            "部分文件没有移入系统回收站：失败项已保留文件（未做永久删除）",
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


def _recheck_proc_launch(
    ctx: ExecutionContext,
    spec: Any,
    *,
    computer: Any,
) -> tuple[Any | None, ExecResult | None]:
    """构造并核对 permit：指纹/spec_digest/effect 快照不一致 → 不执行。"""
    from agent.security import launch as launch_boundary

    fingerprint = _fingerprint(ctx)
    effect = dict(getattr(ctx.operation, "effect", {}) or {})
    unchanged, reason, digest = launch_boundary.recheck_approved_spec(
        spec,
        fingerprint,
        computer=computer,
        effect=effect,
        payload=ctx.operation.payload,
    )
    if not unchanged:
        return None, _failure(
            "审批失效：" + reason + "（本次未执行）",
            "invalidated: " + (reason or "spec_changed"),
            invalidated=True,
            spec_digest=digest,
        )
    mode_now = _mode_text(ctx.current_mode())
    if mode_now == "read_only":
        return None, _failure(
            "当前已切换到只读模式：这次批准不再允许执行程序/命令"
            "（只读模式不能被审批绕过），本次未执行，需重新提出",
            "invalidated: mode_read_only",
            invalidated=True,
        )
    allow_roots = tuple(
        str(item) for item in (fingerprint.get("allowed_roots") or ()) if str(item)
    ) or launch_boundary.write_roots_for(computer, mode_now)
    confirmed = bool(fingerprint.get("internal_data_roots")) and (
        int(getattr(ctx.operation, "confirmations_required", 1) or 1) >= 3
    )
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
        reason="用户已批准这次操作；执行前已重核对 payload/程序身份/spec_digest/spec 一致",
    )
    return permit, None


async def execute_proc_run_program(
    payload: dict, ctx: ExecutionContext, *, computer: Any, security: Any = None
) -> ExecResult:
    from agent.security import launch as launch_boundary
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
    spec = launch_boundary.LaunchSpec(
        kind="program", program=program, argv=tuple(args), cwd=cwd, timeout=timeout
    )
    permit, rejected = _recheck_proc_launch(ctx, spec, computer=computer)
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
    from agent.security import launch as launch_boundary
    from agent.tools.cmd_tools import RunCmdTool

    changed = _reject_if_payload_changed(payload, ctx)
    if changed is not None:
        return changed
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        return _failure("审批单缺少 cmd：未执行", "missing_cmd")
    timeout = float(payload.get("timeout") or 30)
    cwd = str(payload.get("cwd") or "") or None
    spec = launch_boundary.LaunchSpec(
        kind="shell",
        program=None,
        argv=(),
        cwd=cwd,
        shell_cmd=cmd,
        timeout=timeout,
    )
    permit, rejected = _recheck_proc_launch(ctx, spec, computer=computer)
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
    results: list[dict] = []
    all_ok = True
    for item in targets:
        path = str(item.get("path") or "")
        if not path:
            results.append({"path": path, "ok": False, "reason": "缺少路径"})
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
                "path": path,
                "ok": ok,
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
