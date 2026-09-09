"""Command and process tools for computer control (risk-gated)."""

from __future__ import annotations

import asyncio
import platform
import sys
from typing import Any

from agent.tools.base import Tool, ToolResult


class _CmdTool(Tool):
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals


class RunCmdTool(_CmdTool):
    name = "run_cmd"
    description = "执行一条 shell 命令，捕获 stdout/stderr。低危命令自动放行；高危命令需审批。"
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
        verdict, risk = self.computer.command_verdict(cmd)
        if verdict == "deny":
            return ToolResult(ok=False, error="命令在当前模式下被拒绝（只读/无权限）")
        if verdict == "approve":
            r = await self.approvals.request(
                "computer", {"action": "run_cmd", "cmd": cmd, "risk": risk.value}
            )
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
        return ToolResult(ok=True, content=content or "(无输出)")


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
            proc = await asyncio.create_subprocess_shell(
                "tasklist /fo table /nh",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
            return ToolResult(ok=True, content=out.decode(errors="replace").strip())
        proc = await asyncio.create_subprocess_shell(
            "ps -eo pid,comm",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        return ToolResult(ok=True, content=out.decode(errors="replace").strip())


class ProcKillTool(_CmdTool):
    name = "proc_kill"
    description = "结束指定 pid 的进程。pid 必填。需审批。"
    parameters = {"type": "object", "properties": {"pid": {"type": "string"}}, "required": ["pid"]}

    async def run(self, **kwargs: Any) -> ToolResult:
        pid = str(kwargs.get("pid") or "").strip()
        if not pid:
            return ToolResult(ok=False, error="pid 必填")
        verdict = self.computer.command_verdict(f"kill {pid}")[0]
        if verdict == "deny":
            return ToolResult(ok=False, error="结束进程在当前模式下被拒绝")
        if verdict == "approve":
            r = await self.approvals.request("computer", {"action": "proc_kill", "pid": pid})
            if r.decision != "approved":
                return ToolResult(ok=False, error="结束进程未获批准")
        if platform.system().lower() == "windows":
            command = f"taskkill /f /pid {pid}"
        else:
            command = f"kill -9 {pid}"
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode != 0:
            return ToolResult(ok=False, error=f"结束进程失败：{err.decode(errors='replace').strip()}")
        return ToolResult(ok=True, content=f"已结束进程 {pid}")
