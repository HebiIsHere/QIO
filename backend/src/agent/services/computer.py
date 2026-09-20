"""Shared computer-control safety layer.

Path normalization, redline interception, command risk classification and
permission-mode verdicts live here. Each computer-control tool routes its
permission checks through this; no tool implements its own security logic.

命令模型（2026-09 收紧）：

* `run_program` 走 `classify_argv(program, args)`：**只有** argv 级白名单里的
  只读程序 + 合法参数才算 LOW，可自动执行；并且执行时 `shell=False`。
* `run_shell` 走 `shell_verdict(cmd)`：自由 shell 永远需要高等级审批
  （`plan` 模式直接拒绝），因为 `|`、`&&`、`;`、`$()` 这类元字符会让
  「按字符串前缀判断安全」与「实际交给 shell 执行」彻底脱节
  （`ls && evil` 以前会被判低危并自动执行）。
* `classify_command` 保留给需要「按整条命令判断风险」的调用方，含元字符
  的命令至少是 DANGER。
"""

from __future__ import annotations

import shlex
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

# shell 元字符：出现即说明「安全判断」与「实际执行」不是同一个对象
SHELL_METACHARACTERS = ("|", ">", "<", "&", ";", "`", "$(", "${", "\n", "\r")

# 只读程序白名单（fail-closed：不在表里即 HIGH）
READONLY_PROGRAMS = {
    "pwd", "ls", "dir", "cat", "type", "head", "tail", "wc", "find", "grep",
    "rg", "which", "where", "whoami", "date", "df", "du", "tree", "stat", "file",
}
# git 只允许只读子命令（写操作 / 可执行逃逸子命令一律 HIGH）
GIT_READONLY_SUBCOMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "branch", "remote", "tag",
    "describe", "blame", "ls-files", "shortlog", "grep", "cat-file", "config-list",
}
# 任何程序都不接受的参数（会改变解析目标或写文件）
FORBIDDEN_ARG_TOKENS = {
    "-c", "--upload-pack", "--exec-path", "--git-dir", "--work-tree", "--exec",
    "-o", "--output", "--config", "--hook", "--receive-pack", "--no-verify",
    "-R", "--replace-all", "--edit", "--set", "--add", "--unset",
}

# 破坏性/提权程序（比 HIGH 更严重，但在授权流程里同样走审批）
DANGER_PROGRAMS = {
    "rm", "del", "rd", "rmdir", "sudo", "dd", "mkfs", "shutdown", "reboot",
    "halt", "poweroff", "format", "diskpart", "reg", "net", "sc", "takeown",
    "icacls", "attrib", "taskkill", "kill", "chmod", "chown", "mkfs.ext4",
}

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

    # -- root / containment ------------------------------------------------

    def root(self) -> Path:
        """声明的工作区根（文件工具的唯一默认基准，不用进程 cwd）。"""
        return self._root()

    def contains(self, path: str | Path) -> bool:
        """resolve 之后是否仍在工作区根内（symlink 也会被解析到真实目标）。"""
        try:
            candidate = path if isinstance(path, Path) else Path(path)
            return candidate.resolve().is_relative_to(self._root())
        except Exception:  # noqa: BLE001 - 无法判定的路径一律当作根外
            return False

    def resolve_in_root(self, path: str) -> Path:
        """相对路径以工作区根为基准解析（`fs_*` 工具统一入口）。"""
        candidate = Path(str(path or "").strip())
        if not candidate.is_absolute():
            candidate = self._root() / candidate
        return candidate.resolve()

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

    def has_shell_metacharacters(self, cmd: str) -> bool:
        return any(token in cmd for token in SHELL_METACHARACTERS)

    def classify_argv(self, program: str, args: list[str] | None = None) -> CommandRisk:
        """argv 级风险判定：只有只读程序 + 合法参数才是 LOW。"""
        argv = [str(a) for a in (args or [])]
        prog = Path(str(program or "")).name.lower()
        if prog.endswith(".exe"):
            prog = prog[:-4]
        if not prog:
            return CommandRisk.HIGH
        if prog in DANGER_PROGRAMS:
            return CommandRisk.DANGER
        if prog not in READONLY_PROGRAMS and prog != "git":
            return CommandRisk.HIGH
        for arg in argv:
            low = arg.lower()
            if low in FORBIDDEN_ARG_TOKENS:
                return CommandRisk.HIGH
            if any(tok in arg for tok in SHELL_METACHARACTERS):
                # 参数里带 shell 元字符：交给审批，不做自动放行
                return CommandRisk.HIGH
            if prog == "git" and low.startswith("--upload-pack"):
                return CommandRisk.HIGH
        if prog == "git":
            sub = next((a.lower() for a in argv if not a.startswith("-")), None)
            if sub is None:
                # 只允许 git --version 这类无子命令的只读用法
                return CommandRisk.LOW if argv and argv[0].lower() == "--version" else CommandRisk.HIGH
            if sub not in GIT_READONLY_SUBCOMMANDS:
                return CommandRisk.HIGH
        return CommandRisk.LOW

    def classify_command(self, cmd: str) -> CommandRisk:
        """整条命令的风险（含 shell 元字符 → 至少 DANGER）。"""
        text = str(cmd or "").strip()
        if not text:
            return CommandRisk.HIGH
        if self.has_shell_metacharacters(text):
            return CommandRisk.DANGER
        try:
            parts = shlex.split(text, posix=False)
        except ValueError:
            return CommandRisk.HIGH
        if not parts:
            return CommandRisk.HIGH
        program, args = parts[0].strip('"'), [p.strip('"') for p in parts[1:]]
        return self.classify_argv(program, args)

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

    def shell_verdict(self, cmd: str) -> tuple[str, CommandRisk]:
        """自由 shell（`run_shell`）的风险判定：永远需要高等级审批。

        即使命令看起来只是 `ls`，只要它被交给 shell，就可能通过元字符、
        环境变量替换、重定向等方式执行别的东西；这里不做「看起来安全就放行」。
        """
        risk = self.classify_command(cmd)
        if self.mode == "bypass":
            return "auto", risk
        if self.mode == "plan":
            return "deny", risk
        return "approve", risk

    def command_verdict_for_program(
        self, program: str, args: list[str] | None = None
    ) -> tuple[str, CommandRisk]:
        """`run_program`（argv + shell=False）的风险判定。"""
        risk = self.classify_argv(program, args)
        if self.mode == "bypass":
            return "auto", risk
        if self.mode == "plan":
            return ("auto" if risk == CommandRisk.LOW else "deny"), risk
        return ("auto" if risk == CommandRisk.LOW else "approve"), risk
