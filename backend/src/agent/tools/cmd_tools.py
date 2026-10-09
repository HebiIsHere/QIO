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

from agent.tools import proc_cleanup
from agent.tools.approval import DEFAULT_TIMEOUT_SECONDS, refusal_reason
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
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals


class RunProgramTool(_CmdTool):
    """安全路径：argv 白名单，不经 shell。"""

    name = "run_program"
    description = (
        "直接运行一个程序（program + args，不经过 shell）。只读程序白名单内自动放行，"
        "其它程序需审批。需要管道/重定向/变量替换时改用 run_shell（必然要审批）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "program": {"type": "string", "description": "程序名，如 git / ls / pwd"},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参数列表，按顺序传给程序，不做 shell 解释",
            },
            "cwd": {"type": "string"},
            "timeout": {"type": "integer"},
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
        verdict, risk = self.computer.command_verdict_for_program(program, args)
        if verdict == "deny":
            return ToolResult(ok=False, error=f"程序在当前模式下被拒绝（{risk.value}）")
        if verdict == "approve":
            from agent.tools.approval_present import describe_computer_action

            payload = {
                "action": "run_program",
                "program": program,
                "args": args,
                "risk": risk.value,
            }
            payload.update(describe_computer_action(payload))
            r = await self.approvals.request("computer", payload)
            if r.decision != "approved":
                return ToolResult(ok=False, error=f"程序执行未获批准：{refusal_reason(r.decision)}")
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
    description = (
        "把一条命令交给系统 shell 执行（支持管道/重定向/变量替换）。属于高风险能力，"
        "每次都需要用户批准；只要期望的是只读查看，优先用 run_program。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "integer"},
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
        if verdict == "deny":
            return ToolResult(
                ok=False, error=f"命令在当前模式下被拒绝（{risk.value}）：shell 命令需要用户批准"
            )
        if verdict == "approve":
            from agent.tools.approval_present import describe_computer_action

            # 自由 shell 是最需要说清楚的一类审批：用户要看懂「想运行一条命令」
            # 以及实际命令（放在详情里），而不是只看到 run_shell 这个内部动作名。
            payload = {"action": "run_shell", "cmd": cmd, "risk": risk.value}
            payload.update(describe_computer_action(payload))
            r = await self.approvals.request("computer", payload)
            if r.decision != "approved":
                return ToolResult(ok=False, error=f"命令执行未获批准：{refusal_reason(r.decision)}")
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
    description = "返回系统平台/CPU/内存/磁盘信息。自动放行。"
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
    description = "列出当前进程列表。自动放行。"
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
    description = "结束指定 pid 的进程。pid 必填。需审批。"
    parameters = {"type": "object", "properties": {"pid": {"type": "string"}}, "required": ["pid"]}

    async def run(self, **kwargs: Any) -> ToolResult:
        pid = str(kwargs.get("pid") or "").strip()
        if not pid:
            return ToolResult(ok=False, error="pid 必填")
        if not pid.isdigit():
            return ToolResult(ok=False, error="pid 必须是数字")
        verdict = self.computer.command_verdict(f"kill {pid}")[0]
        if verdict == "deny":
            return ToolResult(ok=False, error="结束进程在当前模式下被拒绝")
        if verdict == "approve":
            from agent.tools.approval_present import describe_computer_action

            payload = {"action": "proc_kill", "pid": pid}
            payload.update(describe_computer_action(payload))
            r = await self.approvals.request("computer", payload)
            if r.decision != "approved":
                return ToolResult(ok=False, error="结束进程未获批准")
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
