"""Command and process tools for computer control (risk-gated).

两个命令工具，语义完全不同：

* `run_program`：`program + args` → `asyncio.create_subprocess_exec(..., shell=False)`，
  只读程序白名单 + 参数校验，可以按策略自动执行；
* `run_shell`：交给系统 shell 的自由命令，**永远**需要高等级审批
  （`plan` 模式直接拒绝）。以前只有一个 `run_cmd`：用字符串前缀判断它「安全」，
  却把原始字符串交给 shell 执行 —— `ls && evil` 会被判低危并自动跑掉。

结果与进程生命周期（契约 2 的三条铁律）：

* **退出码是事实**：0 → ok=True（stderr 非空只是输出，不是失败）；
  非 0 → ok=False +「退出码 N」，content 保留合并输出，模型要能看到原因。
  旧反例：git show 不存在的引用报 ok=True —— 已消除。
* **超时/取消收尾自己启动的进程树**：Windows 用 taskkill /T /F /PID
  （只按本次启动拿到的 pid，绝不按程序名），POSIX 用 start_new_session +
  killpg；终止后 await 真正退出；确认不了就如实说「清理未确认」。
* **取消不吞异常**：执行协程被 cancel 时先收尾进程树，再把原始
  CancelledError 重新抛出（loop 靠它判定取消终态）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.security import deletion, fixed_queries
from agent.security import launch as launch_boundary
from agent.tools import isolation, proc_cleanup, security_gate
from agent.tools.approval import DEFAULT_TIMEOUT_SECONDS, refusal_reason
from agent import prompts
from agent.tools.base import Tool, ToolResult

MAX_OUTPUT_CHARS = 20_000
# 审批等待发生在工具自己的 `run()` 里，所以工具级超时必须把「用户思考的时间」
# 也算进去。真实事故：run_shell 的工具超时是 45 秒，而审批给用户 5 分钟 ——
# 用户还没点确认，工具就已经报「超时（45000 毫秒）」结束了。
APPROVAL_SLACK_MS = int(DEFAULT_TIMEOUT_SECONDS * 1000)

# 终止动作之后等待子进程真正退出的宽限；taskkill /F 下通常 <1s。
_TERM_VERIFY_SECONDS = 10.0

# 合并输出里 stderr 的分隔标记（不写反斜杠转义，保持与旧格式一致）。
_STDERR_DELIM = chr(10) + "[stderr]" + chr(10)

#: 永久删除（不进回收站、不可恢复）的审批警告文案（审批单与结果都要出现）。
_PERMANENT_DELETE_WARNING = (
    "永久删除：不会进入系统回收站、不可恢复。只在你明确确认这组具体对象后才执行。"
)

_REQUIREMENT_BINDING_ATTRS = (
    "current_requirement_binding",
    "requirement_binding",
    "current_requirement",
)


def _requirement_record_from_value(value: Any) -> dict:
    """RequirementBinding/UserRequirement/dict → {requirement_id, requirement_version}。"""
    if value is None:
        return {"requirement_id": None, "requirement_version": None, "source": "unavailable"}
    if isinstance(value, dict):
        requirement_id = value.get("requirement_id") or value.get("id")
        version = value.get("requirement_version", value.get("version"))
        source = str(value.get("source") or "binding")
    else:
        requirement_id = getattr(value, "requirement_id", None) or getattr(value, "id", None)
        version = getattr(value, "requirement_version", getattr(value, "version", None))
        source = "binding"
    try:
        version_int = int(version) if version is not None else None
    except (TypeError, ValueError):
        version_int = None
    return {
        "requirement_id": str(requirement_id) if requirement_id else None,
        "requirement_version": version_int,
        "source": source,
    }


def current_requirement_binding_record(security: Any = None) -> dict:
    """当前操作绑定的用户要求（契约 §3 上下文绑定；Lead 接线点）。

    读取顺序：security.context 的 RequirementBinding contextvar（契约接口）→ 显式注入
    到 security 引擎/网关上的绑定（AppContext、审批执行器可注入）→ 无法获得时
    requirement_id=None（**不**回退“数据库最近一条 active 要求”）。
    """
    try:
        from agent.security import context as security_context
    except Exception:  # noqa: BLE001 - 上下文模块不可用按未绑定处理
        security_context = None
    if security_context is not None:
        for attr in _REQUIREMENT_BINDING_ATTRS:
            holder = getattr(security_context, attr, None)
            if holder is None:
                continue
            try:
                value = holder.get() if hasattr(holder, "get") else holder
            except Exception:  # noqa: BLE001
                value = None
            record = _requirement_record_from_value(value)
            if record.get("requirement_id"):
                return record
    if security is not None:
        for attr in _REQUIREMENT_BINDING_ATTRS:
            holder = getattr(security, attr, None)
            if holder is None:
                continue
            try:
                value = holder() if callable(holder) else holder
            except Exception:  # noqa: BLE001
                value = None
            record = _requirement_record_from_value(value)
            if record.get("requirement_id"):
                return record
    return {"requirement_id": None, "requirement_version": None, "source": "unavailable"}


def verify_requirement_binding_record(
    record: Any, *, security: Any = None, engine: Any = None
) -> tuple[bool, str]:
    """执行前核对要求绑定是否仍有效（修正/撤回/版本变化/不存在 → 失效）。

    优先用 W1 的 RequirementLedger.validate_binding（契约 §3）；未合入时退回
    by_id + status/version 的只读核对；没有任何台账/上下文证据时**不默认放行**。
    """
    data = _requirement_record_from_value(record)
    requirement_id = data.get("requirement_id")
    version = data.get("requirement_version")
    if not requirement_id:
        return (
            False,
            "审批单没有绑定用户要求（缺少 requirement_id），无法可靠恢复：需要重新判断（新审批）",
        )
    ledger = None
    for owner in (engine, security):
        if owner is None:
            continue
        ledger = getattr(owner, "requirements", None)
        if ledger is not None:
            break
    if ledger is not None:
        validator = getattr(ledger, "validate_binding", None)
        if callable(validator):
            try:
                from agent.security.requirements import RequirementBinding

                binding = RequirementBinding(
                    requirement_id=str(requirement_id),
                    version=int(version or 0),
                )
            except Exception:  # noqa: BLE001 - W1 版本接口尚未合入时退回只读检查
                binding = None
            if binding is not None:
                try:
                    if validator(binding):
                        return True, ""
                    return False, "用户要求已被修正/撤回或版本已变化（按最新状态处理）"
                except Exception as exc:  # noqa: BLE001 - 核对失败按不能确认处理
                    return (
                        False,
                        "要求版本核对失败（不能确认是否仍有效）：" + type(exc).__name__,
                    )
        by_id = getattr(ledger, "by_id", None)
        if callable(by_id):
            try:
                requirement = by_id(str(requirement_id))
            except Exception:  # noqa: BLE001
                requirement = None
            if requirement is None:
                return False, "绑定的用户要求不存在（可能已被替换/撤回）：需要重新判断"
            status = str(getattr(requirement, "status", "") or "")
            if status != "active":
                return False, "绑定的用户要求已不是 active 状态（被修正/撤回/替换）：需要重新判断"
            current_version = getattr(requirement, "version", None)
            if current_version is not None and version is not None:
                try:
                    if int(current_version) != int(version):
                        return False, "用户要求版本已变化：需要按最新要求重新判断"
                except (TypeError, ValueError):
                    return False, "用户要求版本无法核对"
            return True, ""
    current = current_requirement_binding_record(security=security)
    current_id = current.get("requirement_id")
    if current_id:
        same_id = current_id == requirement_id
        same_version = (
            current.get("requirement_version") == version
            or current.get("requirement_version") is None
            or version is None
        )
        if same_id and same_version:
            return True, ""
        return False, "当前用户要求与审批单绑定的要求不一致：需要按最新要求重新判断"
    return False, "无法核对用户要求版本（要求台账/上下文未接线）：需要重新判断"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _operation_fingerprint_value(
    *, mode: str, operation_kind: str, execution: dict, delete_intent: Any
) -> str:
    """操作指纹：优先 review.operation_fingerprint（契约 §7 单一实现），否则等价自算。"""
    try:
        from agent.security.review import ReviewObject, ReviewOperation, operation_fingerprint

        objects = []
        for item in (delete_intent.objects if delete_intent is not None else ()):
            objects.append(ReviewObject(kind="path", value=item.path, identity_key=item.identity))
        cwd = execution.get("cwd")
        if cwd:
            objects.append(ReviewObject(kind="path", value=str(cwd)))
        raw_argv = execution.get("argv")
        if not raw_argv and execution.get("shell_cmd"):
            raw_argv = [str(execution.get("shell_cmd"))]
        operation = ReviewOperation(
            kind="shell" if execution.get("shell") else "execute",
            objects=tuple(objects),
            program=(
                str(execution.get("program_path"))
                if execution.get("program_path")
                else (str(execution.get("program_name") or "") or None)
            ),
            argv=tuple(str(item) for item in (raw_argv or ())),
            cwd=(str(cwd) if cwd else None),
            script_sha256=(
                str(execution["script_sha256"]) if execution.get("script_sha256") else None
            ),
            network_hosts=(),
            summary=str(operation_kind),
        )
        return operation_fingerprint(operation, mode=mode)
    except Exception:  # noqa: BLE001 - 审查模块不可用时用等价摘要（含全部绑定维度）
        payload = {
            "mode": mode,
            "operation_kind": operation_kind,
            "execution": _jsonable(execution),
            "delete_identity_set": (
                [list(item) for item in delete_intent.identity_set()]
                if delete_intent is not None
                else []
            ),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_operation_binding_record(
    *,
    requirement: dict,
    mode: str,
    operation_kind: str,
    execution: dict,
    delete_intent: Any = None,
    authorization_id: str | None = None,
) -> dict:
    """契约 §7 OperationBinding 的 dict 形式（审批单/授权记录/队列项共用同一份）。"""
    content_digests = [list(item) for item in (execution.get("content_digests") or ())]
    if delete_intent is not None:
        for item in delete_intent.objects:
            content_digests.append([item.path, str(item.identity or "")])
    return {
        "requirement": {
            "requirement_id": requirement.get("requirement_id"),
            "version": requirement.get("requirement_version"),
        },
        "mode": str(mode),
        "operation_kind": str(operation_kind),
        "fingerprint": _operation_fingerprint_value(
            mode=mode,
            operation_kind=operation_kind,
            execution=execution,
            delete_intent=delete_intent,
        ),
        "spec_digest": execution.get("spec_digest"),
        "content_digests": content_digests,
        "conditions": [],
        "authorization_id": authorization_id,
        "delegation": None,
        "permanent_delete": (
            None
            if delete_intent is None
            else {
                "permanent": True,
                "verified": bool(delete_intent.verified),
                "method": delete_intent.method,
                "identity_set": [list(item) for item in delete_intent.identity_set()],
            }
        ),
    }


def _merged_text(stdout: str, stderr: str) -> str:
    """launcher 的 stdout/stderr → 与既有语义一致的合并输出（stderr 用 [stderr] 段）。"""
    content = str(stdout or "").strip()
    err_text = str(stderr or "").strip()
    if err_text:
        content += _STDERR_DELIM + err_text
    return content


def _payload_digest(payload: dict) -> str:
    """审批单 payload 的整体摘要：重放前核对「参数有没有被改」。"""
    import hashlib
    import json

    raw = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"{chr(10)}…（输出已截断，共 {len(text)} 字符）"


def _merged_content(out: bytes | None, err: bytes | None) -> str:
    """stdout 为主体，stderr 以 [stderr] 段落合并保留（模型需要看到原因）。"""
    content = (out or b"").decode(errors="replace").strip()
    if err:
        err_text = err.decode(errors="replace").strip()
        if err_text:
            content += _STDERR_DELIM + err_text
    return content


async def _run_child(
    create_subprocess: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
    label: str,
    missing_error: str | None = None,
) -> ToolResult:
    """启动 → 等待 → 按真实退出码裁决；超时/取消都要收尾自己启动的进程树。

    * 退出码 0 → ok=True（stderr 非空不算失败）；
    * 退出码非 0 → ok=False +「退出码 N」，content 保留合并输出；
    * 超时：终止本次启动的进程树并 await 退出验证，确认不了就如实说
      「清理未确认」，不得伪装已停止；
    * 取消：收尾进程树后把原始 CancelledError 重新抛出，不吞异常；
    * 启动失败：如实报错；proc 没建立就绝不进入清理流程。
    """
    proc = None
    try:
        proc = await create_subprocess()
    except asyncio.CancelledError:
        raise
    except FileNotFoundError:
        return ToolResult(
            ok=False, error=missing_error or f"{label}启动失败：未找到可执行文件"
        )
    except Exception as exc:  # noqa: BLE001 - 系统边界
        return ToolResult(ok=False, error=f"{label}启动失败：{exc}")
    if proc is None:
        return ToolResult(ok=False, error=f"{label}启动失败：未取回子进程对象")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        confirmed = await proc_cleanup.terminate_process_tree(
            proc, wait_exit_seconds=_TERM_VERIFY_SECONDS
        )
        if confirmed:
            return ToolResult(ok=False, error=f"{label}执行超时（进程已终止）")
        return ToolResult(
            ok=False, error=f"{label}执行超时（清理未确认，进程可能仍在运行）"
        )
    except asyncio.CancelledError:
        # 用户取消 / 外层工具超时 / 任务取消：先收尾自己启动的进程树并 await
        # 退出验证，再把取消原样抛回 —— 不吞异常（loop 靠它判定取消终态）。
        cleanup = asyncio.ensure_future(
            proc_cleanup.terminate_process_tree(
                proc, wait_exit_seconds=_TERM_VERIFY_SECONDS
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # 二次取消也不能跳过收尾：等清理真正完成再 propagate。
            await cleanup
            raise
        raise
    content = _merged_content(out, err)
    returncode = proc.returncode
    if returncode != 0:
        return ToolResult(
            ok=False, error=f"{label}退出码 {returncode}", content=_clip(content)
        )
    return ToolResult(ok=True, content=_clip(content) or "(无输出)")


class _CmdTool(Tool):
    # security = 统一授权网关（契约 §2）；None = 旧接线/单测回退路径。
    inject = ["computer", "approvals", "security"]

    def __init__(self, computer=None, approvals=None, security=None) -> None:
        self.computer = computer
        self.approvals = approvals
        self.security = security

    def _mode(self) -> str:
        return security_gate.mode_of(self.security, self.computer)

    # -- 受控启动（契约 §3.3）：真实执行统一走 ControlledLauncher -----------

    def _new_launcher(self):
        """受控启动器：消费后端许可（模式/写范围/内部数据），不复制权限判定。"""
        return launch_boundary.ControlledLauncher(
            computer=self.computer,
            security=self.security,
            mode_provider=self._mode,
        )

    def _write_roots(self) -> tuple[str, ...]:
        """普通命令的写范围事实（工作区根；拿不到就空，绝不用 cwd 冒充）。"""
        return launch_boundary.write_roots_for(self.computer, self._mode())

    def _internal_mentions(self, text: str) -> list[str]:
        """命令/参数里明确提到的内部运行数据路径（对象解析，不是权限判定）。"""
        if self.security is None:
            return []
        try:
            return list(security_gate.internal_data_mentions(self.security, text))
        except Exception:  # noqa: BLE001 - 识别失败不阻断：launcher 会做同一次识别
            return []

    # -- R9-G：结构化执行事实 / 固定查询 / 永久删除事实 ---------------------

    def _execution_facts(self, spec, *, shell: bool) -> dict:
        """从真实 LaunchSpec 生成执行事实（固定查询/永久删除/绑定都基于它）。"""
        facts = launch_boundary.build_spec_facts(spec, computer=self.computer)
        program_facts = dict(facts.get("program_facts") or {})
        program_path = str(program_facts.get("resolved") or "") or None
        program_name = ""
        if program_path:
            program_name = program_path.replace(chr(92), "/").rsplit("/", 1)[-1]
        if not program_name and not shell:
            requested = str(getattr(spec, "program", "") or "").strip().strip(chr(34))
            if requested:
                program_name = requested.replace(chr(92), "/").rsplit("/", 1)[-1]
        if shell:
            program_name = program_name or "shell"
        argv = [str(item) for item in tuple(getattr(spec, "argv", ()) or ())]
        argument_files = list(facts.get("argument_files") or ())
        content_digests: list[list[str]] = []
        if program_facts.get("sha256"):
            content_digests.append(
                ["program:" + str(program_path), str(program_facts["sha256"])]
            )
        for item in argument_files:
            if isinstance(item, dict) and item.get("sha256"):
                content_digests.append([str(item.get("path")), str(item.get("sha256"))])
        script_sha = ""
        if argument_files:
            script_sha = hashlib.sha256(
                json.dumps(argument_files, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        cwd_value = getattr(spec, "cwd", None)
        cwd_resolved = None
        if cwd_value:
            try:
                cwd_resolved = str(Path(str(cwd_value)).resolve())
            except (OSError, ValueError):
                cwd_resolved = str(cwd_value)
        return {
            "kind": "shell" if shell else "program",
            "program_path": program_path,
            "program_name": program_name,
            "argv": [] if shell else argv,
            "cwd": cwd_resolved,
            "shell": bool(shell),
            "shell_cmd": getattr(spec, "shell_cmd", None) if shell else None,
            "spec_digest": launch_boundary.compute_spec_digest(spec, computer=self.computer),
            "program_sha256": program_facts.get("sha256"),
            "argument_files": argument_files,
            "script_sha256": script_sha or None,
            "content_digests": content_digests,
        }

    def _fixed_query(self, execution: dict):
        """固定查询分类器（契约 §4 单一来源；不搜索文字、只看真实身份+argv 结构）。"""
        facts = fixed_queries.FixedQueryFacts(
            kind=str(execution.get("kind") or "program"),
            program_path=execution.get("program_path"),
            program_name=str(execution.get("program_name") or ""),
            argv=tuple(str(item) for item in (execution.get("argv") or ())),
            cwd=execution.get("cwd"),
            shell=bool(execution.get("shell")),
        )
        return fixed_queries.classify_fixed_query(facts)

    def _delete_intent(self, execution: dict):
        """永久删除结构化识别（契约 §5；不全文搜索命令文字）。"""
        return deletion.detect_delete_intent(
            effect_kind="execute",
            detail={"execution": execution},
        )

    def _requirement_binding(self) -> dict:
        return current_requirement_binding_record(self.security)

    def _delete_binding(self, intent) -> dict | None:
        if intent is None or not bool(getattr(intent, "permanent", False)):
            return None
        objects = [item.as_dict() for item in intent.objects]
        shown = "、".join(item.path for item in intent.objects[:5])
        if not shown:
            shown = "（命令含展开/引用/嵌套，无法可靠列出对象集合）"
        return {
            "intent": intent.as_dict(),
            "identity_set": [list(item) for item in intent.identity_set()],
            "objects": objects,
            "verified": bool(intent.verified),
            "warning": _PERMANENT_DELETE_WARNING,
            "recycle_bin": False,
            "irreversible": True,
            "label": "永久删除（不可恢复，不进回收站）：" + shown,
            "purpose": "用户明确确认的永久删除操作（对象身份与动作摘要进入审批指纹）",
        }

    def _execute_effect(
        self,
        *,
        tool: str,
        program: str,
        args: list[str],
        cwd: str | None,
        shell: bool,
        execution: dict,
        delete_intent,
        purpose: str,
    ):
        """EXECUTE effect：detail 携带启动方从真实 LaunchSpec 生成的结构化事实。"""
        targets = []
        if shell:
            targets.append(security_gate.program_target("shell"))
        else:
            targets.append(
                security_gate.program_target(str(execution.get("program_path") or program))
            )
        if cwd:
            targets.append(security_gate.path_target(str(cwd)))
        if delete_intent is not None:
            for item in delete_intent.objects:
                targets.append(
                    security_gate.path_target(
                        item.path, identity=item.identity, exists=True
                    )
                )
        detail = {
            "program": execution.get("program_path") or program,
            "argv": (
                [str(program), *[str(item) for item in args]]
                if not shell
                else ["shell", *[str(item) for item in args]]
            ),
            "cwd": cwd,
            "shell": bool(shell),
            "command": execution.get("shell_cmd") if shell else None,
            "script_sha256": execution.get("script_sha256"),
            "execution": execution,
            "delete_intent": None if delete_intent is None else delete_intent.as_dict(),
        }
        return security_gate.make_effect(
            security_gate.EffectKind.EXECUTE,
            targets,
            purpose=purpose,
            tool=tool,
            detail=detail,
        )

    def _pending_facts(
        self,
        kind: str,
        spec,
        payload: dict,
        *,
        risk: str | None = None,
        internal_roots=(),
        execution: dict | None = None,
        delete_intent: Any = None,
        mode: str | None = None,
    ) -> dict:
        """审批单指纹：操作绑定 + 要求绑定 + spec/内容摘要（执行前重核对消费）。"""
        facts = launch_boundary.build_spec_facts(spec, computer=self.computer)
        program_facts = facts.get("program_facts") or {}
        execution = dict(execution or {})
        requirement = self._requirement_binding()
        binding_mode = str(mode or self._mode())
        data = {
            "kind": kind,
            "argv": (
                [str(spec.program or ""), *[str(item) for item in spec.argv]]
                if str(spec.kind) == "program"
                else None
            ),
            "cwd": spec.cwd,
            "risk": risk,
            "spec_digest": launch_boundary.compute_spec_digest(spec, computer=self.computer),
            "program_sha256": program_facts.get("sha256"),
            "allowed_roots": list(self._write_roots()),
            "payload_digest": _payload_digest(payload),
            "internal_data_roots": list(internal_roots),
            "requirement": requirement,
            "operation_binding": build_operation_binding_record(
                requirement=requirement,
                mode=binding_mode,
                operation_kind=kind,
                execution=execution,
                delete_intent=delete_intent,
            ),
        }
        if delete_intent is not None:
            data["delete_intent"] = delete_intent.as_dict()
        return data

    def _permit_for(
        self,
        spec,
        *,
        mode: str,
        readonly_proof: bool = False,
        approval_id: str | None = None,
        source: str = "instruction",
    ):
        """授权/审批通过后构造 LaunchPermit（launcher 消费它，不重新判权限）。"""
        operations = ("execute_readonly",) if readonly_proof else ("execute",)
        roots = () if readonly_proof else self._write_roots()
        return launch_boundary.build_permit(
            spec,
            computer=self.computer,
            mode=mode,
            allowed_roots=roots,
            allowed_operations=operations,
            network_hosts=None,
            internal_data_confirmed=False,
            approval_id=approval_id,
            source=source,
            reason=(
                "执行已由统一授权网关判定/用户审批通过；受控启动仍执行同一执行边界"
                "（程序/参数/脚本内容执行前后不得更换）"
            ),
        )

    def _launch_permit_for_call(self, kwargs: dict, spec, *, readonly_proof: bool):
        supplied = kwargs.get("_launch_permit")
        if isinstance(supplied, launch_boundary.LaunchPermit):
            return supplied
        return self._permit_for(
            spec, mode=self._mode(), readonly_proof=readonly_proof
        )

    async def _submit_internal_data_operation(
        self,
        *,
        kind: str,
        label: str,
        spec,
        payload: dict,
        fingerprint: dict,
        effect,
        mode: str,
    ) -> ToolResult | None:
        """内部运行数据（契约 §4）：所有模式都要连续三次确认，先提交待审批、不执行。"""
        mentions = list(fingerprint.get("internal_data_roots") or [])
        if not mentions or self.security is None:
            return None
        operation = security_gate.build_pending_operation(
            kind=kind,
            label=label,
            payload=dict(payload),
            effect=effect,
            fingerprint=fingerprint,
            purpose=effect.purpose,
            mode_at_request=mode,
            confirmations_required=security_gate.INTERNAL_DATA_CONFIRMATIONS,
            instruction=security_gate.instruction_snapshot(self.security),
        )
        try:
            ticket = await security_gate.submit_pending(self.security, operation)
        except Exception as exc:  # noqa: BLE001 - 提交失败不能假装已提交
            return ToolResult(
                ok=False,
                error=f"提交审批失败：{type(exc).__name__}: {exc}（本次没有执行）",
                category="permission",
                recoverable=False,
            )
        if ticket is not None:
            extra = {
                "internal_data_roots": mentions,
                "confirmations_required": security_gate.INTERNAL_DATA_CONFIRMATIONS,
            }
            delete_data = fingerprint.get("delete_intent") or {}
            if delete_data.get("permanent"):
                extra["permanent_delete"] = delete_data
                extra["warning"] = _PERMANENT_DELETE_WARNING
            return security_gate.pending_result(
                ticket,
                operation_label=label,
                confirmations_required=security_gate.INTERNAL_DATA_CONFIRMATIONS,
                extra=extra,
            )
        return ToolResult(
            ok=False,
            error=(
                "这次操作涉及 QIO 内部运行数据，需要连续三次确认；"
                "但当前没有可用的审批执行器：本次没有执行。"
            ),
            category="permission",
            recoverable=False,
        )

    def _launch_result_to_tool(
        self, result, *, label: str, program: str | None = None
    ) -> ToolResult:
        """LaunchResult → ToolResult：保持既有退出码/超时/启动失败语义。"""
        merged = _clip(_merged_text(result.stdout, result.stderr))
        isolation_info = result.isolation or {}
        guard_info = result.guard or {}
        facts = {
            "launch_boundary": {
                "spec_digest": result.spec_digest,
                "category": result.category,
                "isolation_mechanisms": isolation_info.get("mechanisms"),
                "write_scope_pre_enforced": (isolation_info.get("write_scope") or {}).get(
                    "pre_enforced"
                ),
                "guard_boundary": guard_info.get("boundary"),
                "capability_boundaries": isolation_info.get("capability_boundaries"),
            }
        }
        category = result.category
        if result.ok and category is None:
            return ToolResult(ok=True, content=merged or "(无输出)", facts=facts)
        if category == "exit_code":
            return ToolResult(
                ok=False,
                error=f"{label}退出码 {result.returncode}",
                content=merged,
                facts=facts,
            )
        if category == "timeout":
            return ToolResult(
                ok=False,
                error=f"{label}{result.error or '执行超时'}",
                category="timeout",
                facts=facts,
            )
        if category == "startup":
            text = result.error or f"{label}启动失败"
            if label == "程序" and program and "找不到程序" in text:
                text = f"找不到程序：{program}"
            return ToolResult(ok=False, error=text, category="startup", facts=facts)
        if category == "output_limit":
            return ToolResult(
                ok=False,
                error=result.error or "输出超过上限",
                content=merged,
                category="output_limit",
                facts=facts,
            )
        if category == "permit_mismatch":
            return ToolResult(
                ok=False,
                error=result.error,
                content=merged,
                category="permission",
                recoverable=False,
                code="permit_invalidated",
                facts=facts,
            )
        if category in ("boundary_violation", "boundary_unverified"):
            return ToolResult(
                ok=False,
                error=result.error,
                content=merged,
                category=category,
                recoverable=False,
                code=category,
                facts=facts,
            )
        if category in ("permission", "boundary_unavailable", "unsupported"):
            return ToolResult(
                ok=False,
                error=result.error,
                content=merged,
                category="permission" if category == "permission" else category,
                recoverable=False,
                code=(
                    "permission_denied"
                    if category == "permission"
                    else "execution_boundary_unavailable"
                ),
                facts=facts,
            )
        return ToolResult(
            ok=False,
            error=result.error or f"{label}执行失败",
            content=merged,
            category=category,
            facts=facts,
        )

    async def _exec(self, spec, permit, *, label: str, program: str | None = None) -> ToolResult:
        """真实启动（契约 §3.3）：受控入口消费 permit；不另写一套启动/收尾。"""
        result = await self._new_launcher().launch(spec, permit, enforce_write_scope=False)
        return self._launch_result_to_tool(result, label=label, program=program)

    async def _submit_permanent_delete_operation(
        self, *, pending, effect, mode: str, delete_binding: dict
    ) -> ToolResult | None:
        """restricted 模式的永久删除：必须由用户对**这组具体对象**明确确认。"""
        if pending is None:
            return ToolResult(
                ok=False,
                error=(
                    "永久删除需要明确的人工确认，但当前没有可提交的审批操作："
                    "本次没有执行（没有任何文件被删除）。"
                ),
                category="permission",
                recoverable=False,
                code="permanent_delete_confirmation_unavailable",
            )
        kind, _label, payload, fingerprint = pending
        if self.security is None:
            legacy_payload = dict(payload)
            legacy_payload.update(
                {
                    "action": legacy_payload.get("action") or kind,
                    "permanent_delete": delete_binding["intent"],
                    "explicit_confirmation_required": True,
                    "warning": _PERMANENT_DELETE_WARNING,
                }
            )
            return await self._legacy_approve(legacy_payload, "永久删除被拒绝")
        explicit_payload = dict(payload)
        explicit_payload.update(
            {
                "permanent_delete": delete_binding["intent"],
                "explicit_confirmation_required": True,
                "recycle_bin": False,
                "irreversible": True,
                "warning": _PERMANENT_DELETE_WARNING,
            }
        )
        explicit_fingerprint = dict(fingerprint)
        explicit_fingerprint.update(
            {
                "explicit_confirmation_required": True,
                "permanent_delete": delete_binding,
                # 明确确认改变了 payload（增加了确认标记/警告），摘要必须同步重算，
                # 否则执行前 payload 重核对会把合法审批误判为被篡改。
                "payload_digest": _payload_digest(explicit_payload),
            }
        )
        operation = security_gate.build_pending_operation(
            kind=kind,
            label=str(delete_binding["label"]),
            payload=explicit_payload,
            effect=effect,
            fingerprint=explicit_fingerprint,
            purpose=str(delete_binding["purpose"]),
            mode_at_request=mode,
            confirmations_required=1,
            instruction=security_gate.instruction_snapshot(self.security),
        )
        try:
            ticket = await security_gate.submit_pending(self.security, operation)
        except Exception as exc:  # noqa: BLE001 - 提交失败不能假装已提交
            return ToolResult(
                ok=False,
                error=f"提交永久删除审批失败：{type(exc).__name__}: {exc}（本次没有执行）",
                category="permission",
                recoverable=False,
            )
        if ticket is not None:
            return security_gate.pending_result(
                ticket,
                operation_label=str(delete_binding["label"]),
                extra={
                    "permanent_delete": delete_binding["intent"],
                    "warning": _PERMANENT_DELETE_WARNING,
                    "explicit_confirmation_required": True,
                    "confirmations_required": 1,
                },
            )
        return ToolResult(
            ok=False,
            error=(
                "永久删除需要明确的人工确认，但当前没有可用的审批执行器："
                "本次没有执行（没有任何文件被删除）。"
            ),
            category="permission",
            recoverable=False,
            code="approval_unavailable",
        )

    async def _legacy_approve(self, payload: dict, denied: str) -> ToolResult | None:
        if self.approvals is None:
            return ToolResult(
                ok=False,
                error="这次操作需要你的批准，但当前没有可用的审批服务：本次没有执行。",
                category="permission",
                recoverable=False,
            )
        from agent.tools.approval_present import describe_computer_action

        described = dict(payload)
        described.update(describe_computer_action(payload))
        r = await self.approvals.request("computer", described)
        if r.decision != "approved":
            labels = {
                "run_program": "程序执行",
                "run_shell": "命令执行",
                "proc_kill": "结束进程",
            }
            label = labels.get(str(payload.get("action") or ""), "操作")
            return ToolResult(
                ok=False,
                error=f"{label}未获批准：{refusal_reason(r.decision)}",
            )
        return None

    async def _authorize_execution(
        self,
        effect,
        *,
        legacy_verdict: str,
        legacy_payload: dict,
        denied: str,
        pending: tuple | None = None,
        readonly_allowed: bool = False,
        readonly_reason: str = "",
        permanent_delete: dict | None = None,
    ) -> ToolResult | None:
        """统一执行入口：只读层兜底 + 网关判定 + 待审批提交 + 永久删除确认。

        永久删除政策（契约 §5）：read_only 由只读兜底拒绝；restricted 必须由用户对
        同一组具体对象明确确认 —— 模型 ALLOW/一般授权记录不能替代（只有 decision 带
        authorization_record/approval_id 的明确人工确认可直接复用）；unrestricted 放行。
        """
        mode = self._mode()
        if mode == "read_only" and not readonly_allowed:
            return ToolResult(
                ok=False,
                error=(
                    readonly_reason
                    or "只读模式下 QIO 不能执行可能修改业务文件的命令/脚本："
                    "这条路径无法证明只读，本次已拒绝（没有任何程序被执行）。"
                ),
                category="permission",
                recoverable=False,
                code="readonly_execution_denied",
            )
        if self.security is None:
            if legacy_verdict == "deny":
                return ToolResult(ok=False, error=denied)
            if legacy_verdict == "approve":
                merged = dict(legacy_payload)
                if permanent_delete is not None:
                    merged["permanent_delete"] = permanent_delete["intent"]
                    merged["explicit_confirmation_required"] = True
                    merged["warning"] = _PERMANENT_DELETE_WARNING
                return await self._legacy_approve(merged, denied)
            return None
        decision = await security_gate.authorize(self.security, effect)
        verdict = security_gate.verdict_of(decision)
        if verdict == "deny":
            return security_gate.denied_result(decision, denied)
        if permanent_delete is not None and mode == "restricted":
            if verdict == "allow":
                explicit_reference = security_gate.approval_id_of(decision)
                if explicit_reference:
                    # 来自明确人工确认记录的复用（契约 §5 复用条款）
                    return None
            return await self._submit_permanent_delete_operation(
                pending=pending,
                effect=effect,
                mode=mode,
                delete_binding=permanent_delete,
            )
        if verdict == "allow":
            return None
        if pending is not None:
            kind, label, pending_payload, fingerprint = pending
            operation = security_gate.build_pending_operation(
                kind=kind,
                label=label,
                payload=pending_payload,
                effect=effect,
                fingerprint=fingerprint,
                purpose=effect.purpose,
                mode_at_request=mode,
                instruction=security_gate.instruction_snapshot(self.security),
            )
            try:
                ticket = await security_gate.submit_pending(self.security, operation)
            except Exception as exc:  # noqa: BLE001 - 提交失败不能假装已提交
                return ToolResult(
                    ok=False,
                    error=f"提交审批失败：{type(exc).__name__}: {exc}（本次没有执行）",
                    category="permission",
                    recoverable=False,
                )
            if ticket is not None:
                return security_gate.pending_result(ticket, operation_label=label)
        if self.approvals is not None:
            return await self._legacy_approve(legacy_payload, denied)
        return ToolResult(
            ok=False,
            error="这次操作需要你的批准，但当前没有可用的审批执行器：本次没有执行（执行边界暂不可用）。",
            category="permission",
            recoverable=False,
            code="approval_unavailable",
        )

class RunProgramTool(_CmdTool):
    """安全路径：argv 白名单，不经 shell。"""

    name = "run_program"
    description = prompts.TOOL_DESCRIPTIONS["run_program"]
    parameters = {
        "type": "object",
        "properties": {
            "program": {
                "type": "string",
                "description": prompts.argument_description("run_program", "program"),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": prompts.argument_description("run_program", "args"),
            },
            "cwd": {
                "type": "string",
                "description": prompts.argument_description("run_program", "cwd"),
            },
            "timeout": {
                "type": "integer",
                "description": prompts.argument_description("run_program", "timeout"),
            },
        },
        "required": ["program"],
    }
    timeout_ms = 45_000 + APPROVAL_SLACK_MS

    async def run(self, **kwargs: Any) -> ToolResult:
        program = str(kwargs.get("program") or "").strip()
        if not program:
            return ToolResult(ok=False, error="program 必填")
        raw_args = kwargs.get("args") or []
        if not isinstance(raw_args, (list, tuple)):
            return ToolResult(ok=False, error="args 必须是字符串数组")
        args = [str(a) for a in raw_args]
        # Lead 接线（契约 1）：把请求里的 cwd 交给裁决 —— cwd 内同名替身
        # 不能仅凭程序名得到自动放行（resolve_program 会因此升级 HIGH）。
        cwd_arg = str(kwargs.get("cwd") or "") or None
        timeout = float(kwargs.get("timeout") or 30)
        mode = self._mode()
        verdict, risk = self.computer.command_verdict_for_program(
            program, args, cwd=cwd_arg
        )
        # 契约 §3.3：真实启动由 LaunchSpec 描述；§4 固定查询 / §5 永久删除 / 操作绑定
        # 全部只从这里的真实事实生成（不采信模型文本、不搜索命令文字）。
        spec = launch_boundary.LaunchSpec(
            kind="program",
            program=program,
            argv=tuple(args),
            cwd=cwd_arg,
            timeout=timeout,
        )
        execution = self._execution_facts(spec, shell=False)
        fixed_query = self._fixed_query(execution)
        delete_intent = self._delete_intent(execution)
        delete_binding = self._delete_binding(delete_intent)
        internal_roots = self._internal_mentions(" ".join([program, *args]))
        execution["internal_data_roots"] = internal_roots
        requirement = self._requirement_binding()
        label = (
            str(delete_binding["label"])
            if delete_binding is not None
            else f"运行程序 {program}"
        )
        effect = self._execute_effect(
            tool="run_program",
            program=program,
            args=args,
            cwd=cwd_arg,
            shell=False,
            execution=execution,
            delete_intent=delete_intent,
            purpose="运行程序（程序身份/argv 结构化判定；shell=False）",
        )
        payload: dict[str, Any] = {
            "program": program,
            "args": args,
            "cwd": cwd_arg,
            "risk": risk.value,
            "operation_kind": "proc.run_program",
            "operation_summary": label,
            "requirement_binding": requirement,
            "execution_facts": {
                "kind": execution.get("kind"),
                "program_path": execution.get("program_path"),
                "program_name": execution.get("program_name"),
                "cwd": execution.get("cwd"),
                "shell": False,
                "spec_digest": execution.get("spec_digest"),
                "script_sha256": execution.get("script_sha256"),
            },
        }
        if fixed_query is not None:
            # 仅供审计展示：引擎会自己用 effect.detail 重新分类，不采信本字段。
            payload["fixed_query_kind"] = fixed_query.kind
        if delete_binding is not None:
            payload.update(
                {
                    "permanent_delete": delete_binding["intent"],
                    "explicit_confirmation_required": True,
                    "warning": _PERMANENT_DELETE_WARNING,
                }
            )
        fingerprint = self._pending_facts(
            "proc.run_program",
            spec,
            payload,
            risk=risk.value,
            internal_roots=internal_roots,
            execution=execution,
            delete_intent=delete_intent,
            mode=mode,
        )
        # 内部运行数据三次确认优先（不叠加第四次）；确认前不执行。
        internal_pending = await self._submit_internal_data_operation(
            kind="proc.run_program",
            label=label,
            spec=spec,
            payload=payload,
            fingerprint=fingerprint,
            effect=effect,
            mode=mode,
        )
        if internal_pending is not None:
            return internal_pending
        fixed_ok = fixed_query is not None and delete_binding is None
        readonly_allowed = (mode != "read_only") or fixed_ok
        readonly_reason = (
            "只读模式只允许固定查询（程序版本/当前目录/系统基础信息/目录列表/文件属性）："
            "这条命令的程序身份/argv 结构不属固定查询"
            + ("；永久删除在只读模式一律拒绝" if delete_binding is not None else "")
            + "。本次已拒绝（没有任何程序被执行）。"
        )
        pending = ("proc.run_program", label, payload, fingerprint)
        blocked = await self._authorize_execution(
            effect,
            legacy_verdict=verdict,
            legacy_payload={
                "action": "run_program",
                "program": program,
                "args": args,
                "risk": risk.value,
                "permanent_delete": (
                    None if delete_binding is None else delete_binding["intent"]
                ),
            },
            denied=f"程序在当前模式下被拒绝（{risk.value}）",
            pending=pending,
            readonly_allowed=readonly_allowed,
            readonly_reason=readonly_reason,
            permanent_delete=delete_binding,
        )
        if blocked is not None:
            return blocked
        permit = self._launch_permit_for_call(
            kwargs, spec, readonly_proof=bool(mode == "read_only" and fixed_ok)
        )
        return await self._exec(spec, permit, label="程序", program=program)


class RunCmdTool(_CmdTool):
    """高风险路径：自由 shell 命令。永远需要审批（plan 模式拒绝）。"""

    name = "run_shell"
    description = prompts.TOOL_DESCRIPTIONS["run_shell"]
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {
                "type": "string",
                "description": prompts.argument_description("run_shell", "cmd"),
            },
            "cwd": {
                "type": "string",
                "description": prompts.argument_description("run_shell", "cwd"),
            },
            "timeout": {
                "type": "integer",
                "description": prompts.argument_description("run_shell", "timeout"),
            },
        },
        "required": ["cmd"],
    }
    timeout_ms = 45_000 + APPROVAL_SLACK_MS

    async def run(self, **kwargs: Any) -> ToolResult:
        cmd = str(kwargs.get("cmd") or "").strip()
        if not cmd:
            return ToolResult(ok=False, error="cmd 必填")
        cwd = str(kwargs.get("cwd") or "") or None
        timeout = float(kwargs.get("timeout") or 30)
        mode = self._mode()
        verdict, risk = self.computer.shell_verdict(cmd)
        # 契约 §3.3：shell 的真实身份 = 解析后的 shell 可执行文件 + 命令文本。
        spec = launch_boundary.LaunchSpec(
            kind="shell",
            program=None,
            argv=(),
            cwd=cwd,
            shell_cmd=cmd,
            timeout=timeout,
        )
        execution = self._execution_facts(spec, shell=True)
        # shell=True 永远不是固定查询（无法证明无重定向/串联/嵌套）。
        delete_intent = self._delete_intent(execution)
        delete_binding = self._delete_binding(delete_intent)
        internal_roots = self._internal_mentions(cmd)
        execution["internal_data_roots"] = internal_roots
        requirement = self._requirement_binding()
        label = (
            str(delete_binding["label"])
            if delete_binding is not None
            else "运行一条 shell 命令"
        )
        effect = self._execute_effect(
            tool="run_shell",
            program="shell",
            args=[cmd],
            cwd=cwd,
            shell=True,
            execution=execution,
            delete_intent=delete_intent,
            purpose="运行自由 shell 命令",
        )
        payload: dict[str, Any] = {
            "cmd": cmd,
            "cwd": cwd,
            "risk": risk.value,
            "shell": True,
            "operation_kind": "proc.shell",
            "operation_summary": label,
            "requirement_binding": requirement,
            "execution_facts": {
                "kind": "shell",
                "program_path": execution.get("program_path"),
                "program_name": execution.get("program_name"),
                "cwd": execution.get("cwd"),
                "shell": True,
                "spec_digest": execution.get("spec_digest"),
            },
        }
        if delete_binding is not None:
            payload.update(
                {
                    "permanent_delete": delete_binding["intent"],
                    "explicit_confirmation_required": True,
                    "warning": _PERMANENT_DELETE_WARNING,
                }
            )
        fingerprint = self._pending_facts(
            "proc.shell",
            spec,
            payload,
            risk=risk.value,
            internal_roots=internal_roots,
            execution=execution,
            delete_intent=delete_intent,
            mode=mode,
        )
        internal_pending = await self._submit_internal_data_operation(
            kind="proc.shell",
            label=label,
            spec=spec,
            payload=payload,
            fingerprint=fingerprint,
            effect=effect,
            mode=mode,
        )
        if internal_pending is not None:
            return internal_pending
        pending = ("proc.shell", label, payload, fingerprint)
        blocked = await self._authorize_execution(
            effect,
            legacy_verdict=verdict,
            legacy_payload={
                "action": "run_shell",
                "cmd": cmd,
                "risk": risk.value,
                "permanent_delete": (
                    None if delete_binding is None else delete_binding["intent"]
                ),
            },
            denied=f"命令在当前模式下被拒绝（{risk.value}）：shell 命令需要用户批准",
            pending=pending,
            readonly_allowed=False,
            readonly_reason=(
                "只读模式下 QIO 不能运行 shell 命令：管道/重定向/命令替换无法证明只读，"
                "本次已拒绝（没有任何命令被执行）。"
            ),
            permanent_delete=delete_binding,
        )
        if blocked is not None:
            return blocked
        permit = self._launch_permit_for_call(kwargs, spec, readonly_proof=False)
        return await self._exec(spec, permit, label="命令")


class SysInfoTool(_CmdTool):
    name = "sys_info"
    description = prompts.TOOL_DESCRIPTIONS["sys_info"]
    parameters = {"type": "object", "properties": {}}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        info = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "python_executable": sys.executable,
        }
        return ToolResult(ok=True, content=f"{chr(10)}".join(f"{k}: {v}" for k, v in info.items()))


class ProcListTool(_CmdTool):
    name = "proc_list"
    description = prompts.TOOL_DESCRIPTIONS["proc_list"]
    parameters = {"type": "object", "properties": {}}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        if platform.system().lower() == "windows":

            def _create():
                return asyncio.create_subprocess_exec(
                    "tasklist",
                    "/fo",
                    "table",
                    "/nh",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

        else:

            def _create():
                return asyncio.create_subprocess_exec(
                    "ps",
                    "-eo",
                    "pid,comm",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

        return await _run_child(_create, timeout=20, label="进程列表")


class ProcKillTool(_CmdTool):
    name = "proc_kill"
    description = prompts.TOOL_DESCRIPTIONS["proc_kill"]
    parameters = {
        "type": "object",
        "properties": {
            "pid": {
                "type": "string",
                "description": prompts.argument_description("proc_kill", "pid"),
            }
        },
        "required": ["pid"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        pid = str(kwargs.get("pid") or "").strip()
        if not pid:
            return ToolResult(ok=False, error="pid 必填")
        if not pid.isdigit():
            return ToolResult(ok=False, error="pid 必须是数字")
        # 契约 §3.1 proc_kill：只允许结束本实例管理的 pid（Job 归属）；其它 pid
        # 必须经过统一授权（默认需要用户批准）。
        managed = isolation.is_managed_pid(int(pid))
        verdict = self.computer.command_verdict(f"kill {pid}")[0]
        effect = security_gate.execute_effect(
            "proc_kill",
            "taskkill" if platform.system().lower() == "windows" else "kill",
            [pid],
            purpose="结束本实例管理的进程" if managed else "结束进程",
            detail={"pid": pid, "managed_by_this_instance": managed},
        )
        pending = (
            "proc.kill",
            f"结束进程 {pid}",
            {"pid": pid, "managed_by_this_instance": managed},
            {"kind": "proc.kill", "pid": pid, "managed": managed},
        )
        blocked = await self._authorize_execution(
            effect,
            legacy_verdict="auto" if managed else verdict,
            legacy_payload={"action": "proc_kill", "pid": pid},
            denied="结束进程在当前模式下被拒绝",
            pending=pending,
            readonly_allowed=False,
            readonly_reason="只读模式下 QIO 不能结束进程（系统级变更），本次已拒绝。",
        )
        if blocked is not None:
            return blocked
        if platform.system().lower() == "windows":
            argv = ["taskkill", "/f", "/pid", pid]
        else:
            argv = ["kill", "-9", pid]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode != 0:
            return ToolResult(ok=False, error=f"结束进程失败：{err.decode(errors='replace').strip()}")
        return ToolResult(ok=True, content=f"已结束进程 {pid}")
