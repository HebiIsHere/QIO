"""Filesystem tools for computer control (patch-style edits, redline guard).

统一路径收口：`resolve → normalize → containment → 权限判定 → 执行`。

* 相对路径（含省略 `dir`）一律以 `ComputerSandbox.root()` 为基准，
  **不用** `process.cwd()`；
* containment 用 resolve 之后的真实路径判断，所以 `..`、绝对路径、
  指向根外的 symlink（含嵌套 symlink）都逃不出去；
* 所有权限判定都委派给注入的 ComputerSandbox；工具只执行判定结果
  （'auto' 直接执行、'approve' 走 ApprovalService、'deny' 直接拒绝）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent.tools.base import Tool, ToolResult

FIND_LIMIT = 50


class _FsTool(Tool):
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals

    def _resolve(self, raw: str) -> Path:
        """相对路径以工作区根为基准解析，返回规范化后的绝对路径。"""
        return self.computer.resolve_in_root(raw)

    async def _permitted(self, verdict: str, payload: dict, denied: str) -> ToolResult | None:
        """把 sandbox 判定翻译成工具行为；返回非 None 表示应当直接返回。"""
        if verdict == "deny":
            return ToolResult(ok=False, error=denied)
        if verdict == "approve":
            # 审批内容必须让普通用户看懂（spec 第 66~70 条）：把这次具体操作
            # 翻译成「想做什么 / 会访问什么 / 一次性还是长期」，原始 action
            # 仍然保留（高级详情用）。
            from agent.tools.approval_present import describe_computer_action

            described = dict(payload)
            described.update(describe_computer_action(payload))
            r = await self.approvals.request("computer", described)
            if r.decision != "approved":
                from agent.tools.approval import refusal_reason

                return ToolResult(
                    ok=False,
                    error=f"{payload.get('action')} 未获批准：{refusal_reason(r.decision)}",
                )
        return None


class FsReadTool(_FsTool):
    name = "fs_read"
    description = "读取一个文件的内容。path 必填。根内自动放行；根外需审批。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        blocked = await self._permitted(
            self.computer.read_verdict(str(target)),
            {"action": "read", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
        )
        if blocked is not None:
            return blocked
        try:
            with open(target, "r", encoding="utf-8") as f:
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
        target = self._resolve(path)
        blocked = await self._permitted(
            self.computer.write_verdict(str(target)),
            {"action": "write", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
        )
        if blocked is not None:
            return blocked
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            return ToolResult(ok=False, error=f"写入失败：{exc}")
        return ToolResult(ok=True, content=f"已写入 {target}")


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
        target = self._resolve(path)
        blocked = await self._permitted(
            self.computer.write_verdict(str(target)),
            {"action": "patch", "path": str(target), "old": old},
            "拒绝访问该路径（敏感路径）",
        )
        if blocked is not None:
            return blocked
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        if old not in text:
            return ToolResult(ok=False, error="未找到待替换的内容 old")
        text = text.replace(old, new, 1)
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"写入失败：{exc}")
        return ToolResult(ok=True, content=f"已编辑 {target}")


class FsListTool(_FsTool):
    name = "fs_list"
    description = "列出目录内容。path 必填。根内自动放行。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        blocked = await self._permitted(
            self.computer.read_verdict(str(target)),
            {"action": "list", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
        )
        if blocked is not None:
            return blocked
        try:
            entries = sorted(p.name for p in target.iterdir())
        except OSError as exc:
            return ToolResult(ok=False, error=f"列目录失败：{exc}")
        return ToolResult(ok=True, content="\n".join(entries) or "(空目录)")


class FsFindTool(_FsTool):
    name = "fs_find"
    description = "按名称查找文件。query 必填，dir 可选（默认工作区根，不跟随符号链接）。根内自动放行。"
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
        # 默认（以及相对路径）都以工作区根为基准，而不是后端进程的 cwd
        root = self._resolve(directory) if directory else self.computer.root()
        blocked = await self._permitted(
            self.computer.read_verdict(str(root)),
            {"action": "find", "path": str(root)},
            "拒绝访问该路径（敏感路径）",
        )
        if blocked is not None:
            return blocked
        if not root.exists():
            return ToolResult(ok=False, error=f"目录不存在：{root}")
        try:
            matches = _find_files(root, query, limit=FIND_LIMIT)
        except OSError as exc:
            return ToolResult(ok=False, error=f"查找失败：{exc}")
        return ToolResult(ok=True, content="\n".join(matches) or "(无匹配)")


class FsInfoTool(_FsTool):
    name = "fs_info"
    description = "返回文件大小/修改时间。path 必填。根内自动放行。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        target = self._resolve(path)
        blocked = await self._permitted(
            self.computer.read_verdict(str(target)),
            {"action": "info", "path": str(target)},
            "拒绝访问该路径（敏感路径）",
        )
        if blocked is not None:
            return blocked
        try:
            p = target
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


def _find_files(root: Path, query: str, *, limit: int) -> list[str]:
    """不跟随符号链接的广度优先查找，且只返回仍在 root 内的真实文件。

    `Path.rglob` 在 Windows 上会沿着 symlink 目录走到根外（实测），所以这里
    用 `os.scandir` + `follow_symlinks=False` 自己走，并逐个结果做 containment。
    """
    base = Path(root).resolve()
    found: list[str] = []
    queue: list[Path] = [base]
    while queue and len(found) < limit:
        current = queue.pop(0)
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    queue.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue  # symlink 文件也不返回：读它可能落在根外
            except OSError:
                continue
            if query in entry.name:
                resolved = Path(entry.path).resolve()
                if resolved.is_relative_to(base):
                    found.append(str(resolved))
                    if len(found) >= limit:
                        break
    return sorted(found)
