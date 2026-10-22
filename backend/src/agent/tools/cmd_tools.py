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
import platform
import sys
from typing import Any, Awaitable, Callable

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

    def _readonly_program_proof(self, program: str, args: list[str]) -> str | None:
        """只读模式下「能可靠证明只读」的 run_program：白名单 + shell=False + 无元字符。

        返回 None = 可以执行；返回中文原因 = 执行层拒绝（不是引擎判定，而是
        执行层无法证明只读时的兜底 —— 契约 §2.3 read_only）。
        """
        if self.computer is None:
            return "当前环境没有可用的程序风险分类器，无法证明这个程序只读"
        try:
            argv_risk = self.computer.classify_argv(program, args)
        except Exception:  # noqa: BLE001 - 分类失败即无法证明
            return "无法证明这个程序只读（风险分类失败）"
        risk_value = getattr(argv_risk, "value", str(argv_risk))
        if risk_value != "low":
            return "这个程序的参数不是可证明的只读形态（只读模式只允许白名单程序的只读用法）"
        if any(meta in str(program) for meta in ("|", ">", "<", "&", ";", "`", "$(")):
            return "程序名里含有 shell 元字符"
        return None

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
            return ToolResult(ok=False, error=f"{refusal_reason(r.decision)}")
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
    ) -> ToolResult | None:
        """统一执行入口：read_only 执行层兜底 + 网关判定 + 待审批提交。

        返回 None 表示可以执行；返回 ToolResult 表示应当直接返回（拒绝/待审批）。
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
                return await self._legacy_approve(legacy_payload, denied)
            return None
        decision = await security_gate.authorize(self.security, effect)
        verdict = security_gate.verdict_of(decision)
        if verdict == "allow":
            return None
        if verdict == "deny":
            return security_gate.denied_result(decision, denied)
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
        verdict, risk = self.computer.command_verdict_for_program(
            program, args, cwd=cwd_arg
        )
        # 执行层兜底（契约 §2.3 read_only）：只有白名单 + shell=False 的只读
        # argv 才允许在只读模式下执行；其余明确拒绝，不静默降级。
        proof = self._readonly_program_proof(program, args) if self._mode() == "read_only" else None
        effect = security_gate.execute_effect(
            "run_program",
            program,
            args,
            cwd=cwd_arg,
            shell=False,
            purpose="运行程序（argv 白名单、shell=False）",
            detail={"risk": risk.value},
        )
        pending = (
            "proc.run_program",
            f"运行程序 {program}",
            {"program": program, "args": args, "cwd": cwd_arg, "risk": risk.value},
            {
                "kind": "proc.run_program",
                "argv": [program, *args],
                "cwd": cwd_arg,
                "risk": risk.value,
            },
        )
        blocked = await self._authorize_execution(
            effect,
            legacy_verdict=verdict,
            legacy_payload={"action": "run_program", "program": program, "args": args, "risk": risk.value},
            denied=f"程序在当前模式下被拒绝（{risk.value}）",
            pending=pending,
            readonly_allowed=proof is None,
            readonly_reason=(
                "只读模式下只能执行可证明只读的程序（白名单 + shell=False）："
                f"{proof}。本次已拒绝。"
            ),
        )
        if blocked is not None:
            return blocked
        return await self._exec(program, args, kwargs.get("cwd"), kwargs.get("timeout", 30))

    async def _exec(self, program: str, args: list[str], cwd: Any, timeout: Any) -> ToolResult:
        cwd_str = str(cwd or "") or None

        def _create():
            return asyncio.create_subprocess_exec(
                program,
                *args,
                cwd=cwd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=False,
                **proc_cleanup.new_session_kwargs(),
            )

        return await _run_child(
            _create,
            timeout=float(timeout or 30),
            label="程序",
            missing_error=f"找不到程序：{program}",
        )


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
        verdict, risk = self.computer.shell_verdict(cmd)
        effect = security_gate.execute_effect(
            "run_shell",
            "shell",
            [cmd],
            cwd=cwd,
            shell=True,
            purpose="运行自由 shell 命令",
            detail={"risk": risk.value, "command": cmd},
        )
        pending = (
            "proc.shell",
            "运行一条 shell 命令",
            {"cmd": cmd, "cwd": cwd, "risk": risk.value},
            {"kind": "proc.shell", "cmd": cmd, "cwd": cwd, "risk": risk.value},
        )
        blocked = await self._authorize_execution(
            effect,
            legacy_verdict=verdict,
            legacy_payload={"action": "run_shell", "cmd": cmd, "risk": risk.value},
            denied=f"命令在当前模式下被拒绝（{risk.value}）：shell 命令需要用户批准",
            pending=pending,
            readonly_allowed=False,
            readonly_reason=(
                "只读模式下 QIO 不能运行 shell 命令：管道/重定向/命令替换无法证明只读，"
                "本次已拒绝（没有任何命令被执行）。"
            ),
        )
        if blocked is not None:
            return blocked
        timeout = float(kwargs.get("timeout", 30))

        def _create():
            return asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **proc_cleanup.new_session_kwargs(),
            )

        return await _run_child(_create, timeout=timeout, label="命令")


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
