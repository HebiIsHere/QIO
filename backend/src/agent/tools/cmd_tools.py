"""Command and process tools for computer control (risk-gated).

两个命令工具，语义完全不同：

* `run_program`：`program + args` → `asyncio.create_subprocess_exec(..., shell=False)`，
  只读程序白名单 + 参数校验，可以按策略自动执行；
* `run_shell`：交给系统 shell 的自由命令，**永远**需要高等级审批
  （`plan` 模式直接拒绝）。以前只有一个 `run_cmd`：用字符串前缀判断它「安全」，
  却把原始字符串交给 shell 执行 —— `ls && evil` 会被判低危并自动跑掉。
"""

from __future__ import annotations

import asyncio
import platform
import sys
from typing import Any

from agent.tools.base import Tool, ToolResult

MAX_OUTPUT_CHARS = 20_000


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n…（输出已截断，共 {len(text)} 字符）"


class _CmdTool(Tool):
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals


class RunProgramTool(_CmdTool):
    """安全路径：argv 白名单，`shell=False`。"""

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
    timeout_ms = 45_000

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
                return ToolResult(ok=False, error="程序执行未获批准，未执行")
        return await self._exec(program, args, kwargs.get("cwd"), kwargs.get("timeout", 30))

    async def _exec(self, program: str, args: list[str], cwd: Any, timeout: Any) -> ToolResult:
        cwd_str = str(cwd or "") or None
        try:
            proc = await asyncio.create_subprocess_exec(
                program,
                *args,
                cwd=cwd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=False,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(timeout or 30))
        except asyncio.TimeoutError:
            return ToolResult(ok=False, error="程序执行超时")
        except FileNotFoundError:
            return ToolResult(ok=False, error=f"找不到程序：{program}")
        except Exception as exc:  # noqa: BLE001 - boundary
            return ToolResult(ok=False, error=f"程序执行失败：{exc}")
        content = out.decode(errors="replace").strip()
        if err:
            content += "\n[stderr]\n" + err.decode(errors="replace").strip()
        return ToolResult(ok=True, content=_clip(content) or "(无输出)")


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
    timeout_ms = 45_000

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
                return ToolResult(ok=False, error="命令执行未获批准，未执行")
        timeout = float(kwargs.get("timeout", 30))
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResult(ok=False, error="命令执行超时")
        except Exception as exc:  # noqa: BLE001 - boundary
            return ToolResult(ok=False, error=f"命令执行失败：{exc}")
        content = out.decode(errors="replace").strip()
        if err:
            content += ("\n[stderr]\n" + err.decode(errors="replace").strip())
        return ToolResult(ok=True, content=_clip(content) or "(无输出)")


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
        return ToolResult(ok=True, content="\n".join(f"{k}: {v}" for k, v in info.items()))


class ProcListTool(_CmdTool):
    name = "proc_list"
    description = "列出当前进程列表。自动放行。"
    parameters = {"type": "object", "properties": {}}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        if platform.system().lower() == "windows":
            proc = await asyncio.create_subprocess_exec(
                "tasklist",
                "/fo",
                "table",
                "/nh",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            return ToolResult(ok=True, content=_clip(out.decode(errors="replace").strip()))
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-eo",
            "pid,comm",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        return ToolResult(ok=True, content=_clip(out.decode(errors="replace").strip()))


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
