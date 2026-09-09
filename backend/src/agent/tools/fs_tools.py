"""Filesystem tools for computer control (patch-style edits, redline guard).

All permission decisions are delegated to the injected ComputerSandbox; the
tools only apply the verdict ('auto' runs directly, 'approve' asks the user
via ApprovalService, 'deny' returns a rejection).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.tools.base import Tool, ToolResult


class _FsTool(Tool):
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals

    def _approve(self, payload: dict) -> bool:
        """Whether the approval for a high-risk action is granted."""
        return True


class FsReadTool(_FsTool):
    name = "fs_read"
    description = "读取一个文件的内容。path 必填。根内自动放行；根外需审批。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        verdict = self.computer.read_verdict(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            r = await self.approvals.request("computer", {"action": "read", "path": path})
            if r.decision != "approved":
                return ToolResult(ok=False, error="读取未获批准，未读取")
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        return ToolResult(ok=True, content=text)


class FsWriteTool(_FsTool):
    name = "fs_write"
    description = "写入/覆盖一个文件。path、content 必填。需审批（accept-edits 模式根内自动）。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not path:
            return ToolResult(ok=False, error="path 必填")
        verdict = self.computer.write_verdict(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            r = await self.approvals.request("computer", {"action": "write", "path": path})
            if r.decision != "approved":
                return ToolResult(ok=False, error="写入未获批准，未写入")
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            return ToolResult(ok=False, error=f"写入失败：{exc}")
        return ToolResult(ok=True, content=f"已写入 {path}")


class FsPatchTool(_FsTool):
    name = "fs_patch"
    description = "对文件做结构化编辑：把 old 替换为 new。需审批。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        old = str(kwargs.get("old") or "")
        new = str(kwargs.get("new") or "")
        if not path or not old:
            return ToolResult(ok=False, error="path 与 old 必填")
        verdict = self.computer.write_verdict(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            r = await self.approvals.request(
                "computer", {"action": "patch", "path": path, "old": old}
            )
            if r.decision != "approved":
                return ToolResult(ok=False, error="编辑未获批准，未修改")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        if old not in text:
            return ToolResult(ok=False, error="未找到待替换的内容 old")
        text = text.replace(old, new, 1)
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"写入失败：{exc}")
        return ToolResult(ok=True, content=f"已编辑 {path}")


class FsListTool(_FsTool):
    name = "fs_list"
    description = "列出目录内容。path 必填。根内自动放行。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        verdict = self.computer.read_verdict(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            r = await self.approvals.request("computer", {"action": "list", "path": path})
            if r.decision != "approved":
                return ToolResult(ok=False, error="列目录未获批准")
        try:
            entries = sorted(p.name for p in Path(path).iterdir())
        except OSError as exc:
            return ToolResult(ok=False, error=f"列目录失败：{exc}")
        return ToolResult(ok=True, content="\n".join(entries) or "(空目录)")


class FsFindTool(_FsTool):
    name = "fs_find"
    description = "按名称查找文件/目录。query 必填，dir 可选（默认工作区根）。根内自动放行。"
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "dir": {"type": "string"}},
        "required": ["query"],
    }
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, error="query 必填")
        directory = str(kwargs.get("dir") or "").strip()
        verdict = self.computer.read_verdict(directory) if directory else "auto"
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            r = await self.approvals.request("computer", {"action": "find", "path": directory})
            if r.decision != "approved":
                return ToolResult(ok=False, error="查找未获批准")
        root = Path(directory or ".")
        try:
            matches = sorted(
                str(p)
                for p in root.rglob("*")
                if p.is_file() and query in p.name
            )
        except OSError as exc:
            return ToolResult(ok=False, error=f"查找失败：{exc}")
        return ToolResult(ok=True, content="\n".join(matches[:50]) or "(无匹配)")


class FsInfoTool(_FsTool):
    name = "fs_info"
    description = "返回文件大小/修改时间。path 必填。根内自动放行。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        verdict = self.computer.read_verdict(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            r = await self.approvals.request("computer", {"action": "info", "path": path})
            if r.decision != "approved":
                return ToolResult(ok=False, error="读取信息未获批准")
        try:
            p = Path(path)
            st = p.stat()
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取信息失败：{exc}")
        lines = [
            f"name: {p.name}",
            f"size: {st.st_size}",
            f"mtime: {int(st.st_mtime)}",
            f"path: {p}",
        ]
        return ToolResult(ok=True, content="\n".join(lines))
