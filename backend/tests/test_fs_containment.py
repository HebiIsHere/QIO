"""文件权限范围：默认根是工作区，`..` / 绝对路径 / symlink 都逃不出去。"""

from __future__ import annotations

import os
import subprocess

import pytest

from agent.services.computer import ComputerSandbox
from agent.tools.fs_tools import FsFindTool, FsReadTool, FsWriteTool


def _make_dir_link(link, target) -> bool:
    """尽力造一个指向外部的目录链接（Windows 无权限时退回 junction）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        # junction 不需要 SeCreateSymbolicLinkPrivilege，普通用户也能建
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and link.exists()
    return False


def _sandbox(root, mode="default") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


class _Approval:
    def __init__(self, decision="rejected") -> None:
        self.decision = decision
        self.calls: list[dict] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append(payload)
        return type("R", (), {"decision": self.decision})()


def test_relative_paths_resolve_against_sandbox_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    sandbox = _sandbox(root)
    assert sandbox.root() == root.resolve()
    assert sandbox.resolve_in_root("a.txt") == (root / "a.txt").resolve()
    assert sandbox.contains(sandbox.resolve_in_root("a.txt"))


def test_containment_rejects_escapes(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    sandbox = _sandbox(root)

    assert not sandbox.contains(outside)
    assert not sandbox.contains(root / ".." / "outside.txt")
    # 判定为「需审批」而不是「根内自动」
    assert sandbox.check_path(str(root / ".." / "outside.txt")) == "approve"
    assert sandbox.check_path(str(outside)) == "approve"


def test_symlink_escape_is_not_auto(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "key.txt").write_text("top-secret", encoding="utf-8")
    link = root / "link"
    if not _make_dir_link(link, secret_dir):
        pytest.skip("本机不允许创建目录符号链接/junction")

    sandbox = _sandbox(root)
    escaped = link / "key.txt"
    assert not sandbox.contains(escaped)
    assert sandbox.read_verdict(str(escaped)) == "approve"
    assert sandbox.write_verdict(str(escaped)) != "auto"


@pytest.mark.asyncio
async def test_fs_read_refuses_escape_without_approval(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # 进程 cwd 在根外：不能成为默认基准

    tool = FsReadTool()
    tool.computer = _sandbox(root)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(path="../outside.txt")
    assert res.ok is False
    assert len(approval.calls) == 1, "根外读取必须走审批"
    assert str(tmp_path / "outside.txt") in approval.calls[0]["path"]


@pytest.mark.asyncio
async def test_fs_read_relative_inside_root_is_auto(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    tool = FsReadTool()
    tool.computer = _sandbox(root)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(path="note.txt")
    assert res.ok is True
    assert res.content == "hello"
    assert not approval.calls


@pytest.mark.asyncio
async def test_fs_find_defaults_to_sandbox_root_not_cwd(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "needle.txt").write_text("x", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "needle.txt").write_text("should not be found", encoding="utf-8")
    monkeypatch.chdir(elsewhere)

    tool = FsFindTool()
    tool.computer = _sandbox(root)
    tool.approvals = _Approval()

    res = await tool.run(query="needle")
    assert res.ok is True
    assert "nested" in res.content
    assert "elsewhere" not in res.content


@pytest.mark.asyncio
async def test_fs_find_does_not_walk_through_symlinked_directory(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "needle.txt").write_text("outside", encoding="utf-8")
    if not _make_dir_link(root / "link", outside):
        pytest.skip("本机不允许创建目录符号链接/junction")

    tool = FsFindTool()
    tool.computer = _sandbox(root)
    tool.approvals = _Approval()

    res = await tool.run(query="needle")
    assert "outside" not in (res.content or ""), "不得沿 symlink 走到根外"


@pytest.mark.asyncio
async def test_fs_write_outside_root_needs_approval(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(root)

    tool = FsWriteTool()
    tool.computer = _sandbox(root, mode="accept-edits")
    approval = _Approval(decision="approved")
    tool.approvals = approval

    res = await tool.run(path="../escape.txt", content="x")
    assert res.ok is True
    assert len(approval.calls) == 1, "accept-edits 只对根内的写自动放行"
    assert (tmp_path / "escape.txt").exists()
