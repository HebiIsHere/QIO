"""默认工作区根目录必须存在。

真实事故：`%APPDATA%\\qio\\workspace` 从来没被创建过，于是所有相对路径的
`fs_*` 调用一律报「系统找不到指定的路径」（一轮里 11 次），模型分不清是自己
写错了路径还是环境没准备好，转而用绝对路径撞审批。
"""

from __future__ import annotations

from pathlib import Path

from agent.config import Settings
from agent.services.computer import ComputerSandbox


def test_ensure_dirs_creates_default_workspace_root(tmp_path: Path):
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    assert (tmp_path / "workspace").is_dir()


def test_computer_ensure_root_creates_configured_root(tmp_path: Path):
    root = tmp_path / "elsewhere"
    sb = ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: "default")
    assert sb.ensure_root() == root.resolve()
    assert root.is_dir()


def test_computer_ensure_root_does_not_raise_on_broken_path(tmp_path: Path):
    """路径上有文件挡着时只记日志，不抛：由工具如实报路径错误，不伪造成功。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    sb = ComputerSandbox(
        resolve_root=lambda: str(blocker / "workspace"), permission_mode=lambda: "default"
    )
    sb.ensure_root()  # 不抛异常
