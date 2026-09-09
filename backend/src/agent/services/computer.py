"""Shared computer-control safety layer.

Path normalization, redline interception, command risk classification and
permission-mode verdicts live here. Each computer-control tool routes its
permission checks through this; no tool implements its own security logic.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Callable


class CommandRisk(str, Enum):
    LOW = "low"
    HIGH = "high"
    DANGER = "danger"


# 绝对红线：即使在工作区内也拒绝
REDLINE_NAMES = {".env", ".git", ".ssh", ".config", "credentials"}
REDLINE_SUFFIXES = ("id_rsa", "id_ed25519", ".pem", "cookie", "cookies.sqlite")

# 低危命令前缀白名单（fail-closed：未匹配即 HIGH/DANGER）
LOW_CMD_PREFIXES = (
    "git status", "git diff", "git log", "ls", "pwd", "which",
    "find", "grep", "cat", "head", "tail", "echo", "wc", "df", "du",
)

# 破坏性/提权命令前缀（比 HIGH 更严重，但在授权流程里同样走审批）
DANGER_CMD_PREFIXES = (
    "rm -rf", "rm ", "sudo", "dd ", "mkfs", "shutdown", "reboot", "halt",
    "poweroff", "format ", "del ", "rd ", "chmod 777", "chmod +x /", "> /dev/",
)

PERMISSION_MODES = ("default", "plan", "accept-edits", "bypass")
DEFAULT_MODE = "default"


def _normalize(path: str) -> Path:
    return Path(path).resolve()


class ComputerSandbox:
    """Resolve permission verdicts for computer-control actions."""

    def __init__(self, resolve_root: Callable[[], str], permission_mode: Callable[[], str]) -> None:
        self._resolve_root = resolve_root
        self._permission_mode = permission_mode

    @property
    def mode(self) -> str:
        value = self._permission_mode() if callable(self._permission_mode) else str(self._permission_mode)
        return value if value in PERMISSION_MODES else DEFAULT_MODE

    def _root(self) -> Path:
        raw = self._resolve_root() if callable(self._resolve_root) else str(self._resolve_root)
        return _normalize(raw or ".")

    def _is_redline(self, p: Path) -> bool:
        for part in p.parts:
            if part.lower() in REDLINE_NAMES:
                return True
        low = p.name.lower()
        if low in REDLINE_NAMES:
            return True
        return any(low.endswith(s) for s in REDLINE_SUFFIXES)

    def _inside_root(self, p: Path) -> bool:
        try:
            return p.is_relative_to(self._root())
        except Exception:
            return False

    def check_path(self, path: str) -> str:
        """Return 'auto' | 'approve' | 'deny' for a filesystem path."""
        try:
            p = _normalize(path)
        except Exception:
            return "deny"
        if self._is_redline(p):
            return "deny"
        return "auto" if self._inside_root(p) else "approve"

    def classify_command(self, cmd: str) -> CommandRisk:
        c = cmd.strip().lower()
        if any(c.startswith(x) for x in LOW_CMD_PREFIXES):
            return CommandRisk.LOW
        if any(c.startswith(x) for x in DANGER_CMD_PREFIXES):
            return CommandRisk.DANGER
        # fail-closed：未匹配 → HIGH（走审批，不放行）
        return CommandRisk.HIGH

    # -- mode-aware verdicts --------------------------------------------------

    def read_verdict(self, path: str) -> str:
        verdict = self.check_path(path)
        if verdict == "deny":
            return "deny"
        if verdict == "auto":
            return "auto"
        return "auto" if self.mode == "bypass" else "approve"

    def write_verdict(self, path: str) -> str:
        verdict = self.check_path(path)
        if verdict == "deny":
            return "deny"
        if self.mode == "bypass":
            return "auto"
        if self.mode == "plan":
            return "deny"
        if self.mode == "accept-edits":
            return "auto" if verdict == "auto" else "approve"
        return "approve"

    def command_verdict(self, cmd: str) -> tuple[str, CommandRisk]:
        risk = self.classify_command(cmd)
        if self.mode == "bypass":
            return "auto", risk
        if self.mode == "plan":
            return ("auto" if risk == CommandRisk.LOW else "deny"), risk
        return ("auto" if risk == CommandRisk.LOW else "approve"), risk
