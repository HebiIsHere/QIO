"""Shared computer-control safety layer.

Path normalization, redline interception, command risk classification and
permission-mode verdicts live here. Each computer-control tool routes its
permission checks through this; no tool implements its own security logic.

命令模型（2026-09 收紧，2026-10 结构化重建）：

* run_program 走 command_verdict_for_program(program, args, cwd=...)：
  自动放行必须同时满足四个条件 ——
    1. 可信程序：真实解析出的可执行文件不在进程 cwd / 工作区根之下
       （否则视为可执行替身，一律 HIGH）；
    2. 显式只读语法：git 同一子命令按参数分成「查询形态 / 写形态」，
       写形态（tag 创建/删除、branch 新建、remote add 等）一律 HIGH；
    3. 实际只读访问：文件操作数与 git -C 的值 resolve 后，
       红线 → deny、根内 → auto、根外 → approve（与 fs_read 同一规则）；
    4. 可验证的可执行身份：resolve_program 给出真实可执行路径，
       显式路径解析失败 → HIGH（fail-closed）。
  执行时 shell=False。
* run_shell 走 shell_verdict(cmd)：自由 shell 永远需要高等级审批
  （plan 模式直接拒绝），因为管道、重定向、命令替换会让
  「按字符串前缀判断安全」与「实际交给 shell 执行」彻底脱节。
* classify_command 保留给需要「按整条命令判断风险」的调用方，含元字符
  的命令至少是 DANGER。
* resolve_program 公开：判定层用它验证「将要执行的到底是什么」。
"""

from __future__ import annotations

import logging
import shlex
import shutil
from enum import Enum
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


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
# 等号赋值形式（--x=y）同样写向/逃逸：以前只查完整 token，等号形式是漏洞
EQ_WRITE_PREFIXES = (
    "--git-dir=", "--work-tree=", "--upload-pack=", "--receive-pack=",
    "--exec-path=", "--config=", "--output=",
)

# 程序级写参数表：命中即 HIGH（find 的写语义 + 明显写向的未知参数）
FIND_WRITE_PREFIXES = ("-delete", "-exec", "-ok", "-fls", "-fprint")
GENERIC_WRITE_PREFIXES = ("--output",)

# 「以文件为操作数」的程序：文件操作数要与 fs_read 走同一套红线/根内/根外规则
FILE_OPERAND_PROGRAMS = {
    "cat", "type", "head", "tail", "wc", "stat", "file",
    "grep", "rg", "find", "tree", "du", "dir", "ls",
}
# grep/rg 的 pattern 只有像路径时才算操作数；纯单词 pattern 不算
GREP_PATTERN_OPTIONS = ("-e", "--regexp")
GREP_PATTERN_FILE_OPTIONS = ("-f", "--file")

# git branch 的只读选项（大小写敏感：-l 是列表，-L 不是）
BRANCH_READONLY_OPTIONS = {
    "-a", "-r", "-l", "--list", "--show-current", "-vv",
    "--contains", "--merged", "--no-merged",
}
BRANCH_VALUE_OPTIONS = ("--contains", "--merged", "--no-merged")
BRANCH_VALUE_EQ_PREFIXES = ("--contains=", "--merged=", "--no-merged=")
# git remote 的分级
REMOTE_FLAG_READONLY = {"-v", "--verbose"}
REMOTE_READONLY_SUBS = {"get-url", "show"}
REMOTE_WRITE_SUBS = {"add", "rename", "remove", "rm", "set-url", "prune", "update"}

# 破坏性/提权程序（比 HIGH 更严重，但在授权流程里同样走审批）
DANGER_PROGRAMS = {
    "rm", "del", "rd", "rmdir", "sudo", "dd", "mkfs", "shutdown", "reboot",
    "halt", "poweroff", "format", "diskpart", "reg", "net", "sc", "takeown",
    "icacls", "attrib", "taskkill", "kill", "chmod", "chown", "mkfs.ext4",
}

PERMISSION_MODES = ("default", "plan", "accept-edits", "bypass")
DEFAULT_MODE = "default"

_EXEC_SUFFIXES = ("", ".exe", ".bat", ".cmd")


def _normalize(path: str) -> Path:
    return Path(path).resolve()


def _looks_like_path(token: str) -> bool:
    """token 是否「像路径」：分隔符 / 盘符 / . / .. 前缀。"""
    return (
        any(ch in token for ch in ("/", "\\"))
        or (len(token) > 1 and token[1] == ":")
        or token in (".", "..")
        or token.startswith("./")
        or token.startswith("../")
    )


def _windows_switch(token: str) -> bool:
    """Windows 风格选项开关（dir /b、find /c）：不是路径操作数。"""
    return len(token) > 1 and len(token) <= 3 and token.startswith("/") and token[1:].isalpha()


def _has_path_form(text: str) -> bool:
    return any(ch in text for ch in ("/", "\\")) or (len(text) > 1 and text[1] == ":")


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

    def ensure_root(self) -> Path:
        """建工作区根目录（幂等，只建这一层）。

        根目录不存在时，相对路径的 fs_* 会一律报「系统找不到指定的路径」，
        调用方分不清是路径写错了还是环境没准备好（真实事故：默认根
        %APPDATA%\\qio\\workspace 从来没被创建过，一轮里失败 11 次）。
        建不出来不在这里伪造成功：只记日志，工具仍然会如实报错。
        """
        root = self._root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("computer workspace root unavailable: %s (%s)", root, exc)
        return root

    def contains(self, path: str | Path) -> bool:
        """resolve 之后是否仍在工作区根内（symlink 也会被解析到真实目标）。"""
        try:
            candidate = path if isinstance(path, Path) else Path(path)
            return candidate.resolve().is_relative_to(self._root())
        except Exception:  # noqa: BLE001 - 无法判定的路径一律当作根外
            return False

    def resolve_in_root(self, path: str) -> Path:
        """相对路径以工作区根为基准解析（fs_* 工具统一入口）。"""
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

    # -- program identity ----------------------------------------------------

    def resolve_program(self, program: str, cwd: str | None = None) -> str | None:
        """解析出将要真实执行的可执行文件路径；找不到 → None。

        * 含路径分隔符或盘符 → 直接按该路径找（缺 .exe/.bat/.cmd 时补全）；
        * 裸程序名 → 先看 subprocess cwd 里有没有同名可执行文件（替身候选，
          防御 CreateProcess / PATH 语义漂移），再用 shutil.which 在 PATH 上找
          （含补 .exe/.bat/.cmd）。
        """
        text = str(program or "").strip()
        if not text:
            return None
        if _has_path_form(text):
            base = Path(text)
            if not base.is_absolute() and not (len(text) > 1 and text[1] == ":"):
                base = (Path(str(cwd)).resolve() if cwd else self._root()) / base
            for suffix in _EXEC_SUFFIXES:
                candidate = base if suffix == "" else Path(str(base) + suffix)
                try:
                    if candidate.is_file():
                        return str(candidate.resolve())
                except OSError:
                    continue
            return None
        if cwd:
            cwd_dir = Path(str(cwd))
            for suffix in _EXEC_SUFFIXES:
                candidate = cwd_dir / (text + suffix)
                if candidate.is_file():
                    return str(candidate.resolve())
        found = shutil.which(text)
        if not found:
            for suffix in _EXEC_SUFFIXES[1:]:
                found = shutil.which(text + suffix)
                if found:
                    break
        return str(Path(found).resolve()) if found else None

    def _resolved_untrusted(self, resolved: Path, cwd: str | None) -> bool:
        """解析结果落在进程 cwd 或工作区根之下 → 视为不可信替身。"""
        if self.contains(resolved):
            return True
        if cwd:
            try:
                return resolved.is_relative_to(Path(str(cwd)).resolve())
            except Exception:  # noqa: BLE001
                return False
        return False

    # -- argv classification ---------------------------------------------------

    def classify_argv(self, program: str, args: list[str] | None = None) -> CommandRisk:
        """argv 级风险判定：只有只读程序 + 合法参数才是 LOW。

        历史入口（不做可执行身份解析）；路径操作数的一致性结果会升级风险。
        """
        risk, path_state = self._analyze_argv(program, args, check_resolve=False)
        if risk == CommandRisk.LOW and path_state in ("approve", "deny"):
            return CommandRisk.HIGH
        return risk

    def _analyze_argv(
        self, program: str, args: list[str] | None, *, cwd: str | None = None, check_resolve: bool = False
    ) -> tuple[CommandRisk, str | None]:
        """结构化判定。返回 (risk, path_state)，path_state ∈ None/auto/approve/deny。"""
        argv = [str(a) for a in (args or [])]
        text = str(program or "").strip()
        prog = Path(text).name.lower() if text else ""
        if prog.endswith(".exe"):
            prog = prog[:-4]
        if not prog:
            return CommandRisk.HIGH, None
        if prog in DANGER_PROGRAMS:
            return CommandRisk.DANGER, None
        if prog not in READONLY_PROGRAMS and prog != "git":
            return CommandRisk.HIGH, None
        path_state: str | None = None

        # 可信程序：解析结果不能落在进程 cwd / 工作区根之下
        if check_resolve:
            resolved = self.resolve_program(text, cwd=cwd)
            if resolved is None:
                if _has_path_form(text):
                    # 显式给出的路径解析失败 → fail-closed
                    return CommandRisk.HIGH, None
            elif self._resolved_untrusted(Path(resolved), cwd):
                return CommandRisk.HIGH, None

        # 全局 token 扫描（所有程序）：完整 token / 等号形式 / shell 元字符
        for tok in argv:
            if prog == "git" and tok == "-C":
                continue  # -C 值单独按路径核验，不按 FORBIDDEN 拦截
            low = tok.lower()
            if low in FORBIDDEN_ARG_TOKENS:
                return CommandRisk.HIGH, path_state
            if any(low.startswith(pref) for pref in EQ_WRITE_PREFIXES):
                return CommandRisk.HIGH, path_state
            if any(m in tok for m in SHELL_METACHARACTERS):
                # 参数里带 shell 元字符：交给审批，不做自动放行
                return CommandRisk.HIGH, path_state

        if prog == "git":
            return self._analyze_git(argv, cwd, path_state)
        return self._analyze_generic(prog, argv, path_state)

    def _analyze_git(
        self, argv: list[str], cwd: str | None, path_state: str | None
    ) -> tuple[CommandRisk, str | None]:
        # git -C <path>：值 resolve 后必须在工作区根内且非红线
        i = 0
        while i < len(argv):
            if argv[i] == "-C":
                if i + 1 >= len(argv):
                    return CommandRisk.HIGH, path_state  # -C 缺值：fail-closed
                target = self._resolve_operand(argv[i + 1], cwd)
                if self._is_redline(target):
                    path_state = "deny"
                elif not self.contains(target):
                    return CommandRisk.HIGH, path_state
                i += 2
                continue
            i += 1
        # 子命令识别：跳过 -C 及其值、跳过其它选项
        sub: str | None = None
        rest: list[str] = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "-C":
                i += 2
                continue
            if tok.startswith("-") or _windows_switch(tok):
                i += 1
                continue
            sub = tok.lower()
            rest = argv[i + 1:]
            break
        if sub is None:
            # 只允许 git --version 这类无子命令的只读用法
            if argv and argv[0].lower() == "--version":
                return CommandRisk.LOW, path_state
            return CommandRisk.HIGH, path_state
        if sub not in GIT_READONLY_SUBCOMMANDS:
            return CommandRisk.HIGH, path_state
        if sub == "tag":
            return self._git_tag_risk(rest), path_state
        if sub == "branch":
            return self._git_branch_risk(rest), path_state
        if sub == "remote":
            return self._git_remote_risk(rest), path_state
        return CommandRisk.LOW, path_state

    def _git_tag_risk(self, rest: list[str]) -> CommandRisk:
        """tag：无参或仅 -l/--list 与其 pattern → LOW；创建/删除/强改 → HIGH。"""
        seen_list = False
        for tok in rest:
            if not tok.startswith("-"):
                if not seen_list:
                    return CommandRisk.HIGH  # 位置参数 tag 名 = 创建
                continue  # -l/--list 的匹配 pattern
            if tok.lower() in ("-l", "--list"):
                seen_list = True
                continue
            return CommandRisk.HIGH  # -d/--delete/-f/--force 及未知选项：fail-closed
        return CommandRisk.LOW

    def _git_branch_risk(self, rest: list[str]) -> CommandRisk:
        """branch：查询形态 → LOW；位置参数名/删除/改名等 → HIGH。"""
        seen_list = False
        expect_value = False
        for tok in rest:
            if not tok.startswith("-"):
                if expect_value:
                    expect_value = False  # --contains/--merged/--no-merged 的值
                    continue
                if seen_list:
                    continue  # --list 的匹配 pattern
                return CommandRisk.HIGH  # 位置参数分支名 = 创建/重命名
            # branch 选项大小写敏感（-l 是列表，-L 不是）：按原文匹配，不做归一
            expect_value = False
            if tok in BRANCH_READONLY_OPTIONS or any(tok.startswith(p) for p in BRANCH_VALUE_EQ_PREFIXES):
                if tok in BRANCH_VALUE_OPTIONS:
                    expect_value = True
                if tok in ("-l", "--list"):
                    seen_list = True
                continue
            return CommandRisk.HIGH  # -d/-D/--copy/--move/-m/-f/-L 等：fail-closed
        return CommandRisk.LOW

    def _git_remote_risk(self, rest: list[str]) -> CommandRisk:
        """remote：无参/-v/get-url/show → LOW；add/rename/remove/... → HIGH。"""
        names = [t.lower() for t in rest if not t.startswith("-")]
        flags = [t.lower() for t in rest if t.startswith("-")]
        if any(f not in REMOTE_FLAG_READONLY for f in flags):
            return CommandRisk.HIGH
        if not names:
            return CommandRisk.LOW
        if names[0] in REMOTE_WRITE_SUBS:
            return CommandRisk.HIGH
        if names[0] in REMOTE_READONLY_SUBS:
            return CommandRisk.LOW
        return CommandRisk.HIGH  # 未知子命令：fail-closed

    def _analyze_generic(
        self, prog: str, argv: list[str], path_state: str | None
    ) -> tuple[CommandRisk, str | None]:
        # 程序级写参数（find 的写语义 + 明显写向的未知参数）
        for tok in argv:
            low = tok.lower()
            if any(low.startswith(p) for p in GENERIC_WRITE_PREFIXES):
                return CommandRisk.HIGH, path_state
            if prog == "find" and any(low.startswith(p) for p in FIND_WRITE_PREFIXES):
                return CommandRisk.HIGH, path_state
        # 文件路径操作数一致性：与 fs_read 同一红线/根内/根外规则
        for raw in self._path_operands(prog, argv):
            target = self._resolve_operand(raw)
            if self._is_redline(target):
                path_state = "deny"
            elif not self.contains(target):
                if path_state != "deny":
                    path_state = "approve"
        return CommandRisk.LOW, path_state

    def _path_operands(self, prog: str, argv: list[str]) -> list[str]:
        """提取疑似路径操作数（非 - 开头 token；等号形式值也算）。"""
        if prog not in FILE_OPERAND_PROGRAMS:
            return []
        pattern_first = prog in ("grep", "rg")
        operands: list[str] = []
        pattern_gate = False   # 下一 token 是 pattern（像路径才算操作数）
        file_operand = False   # 下一 token 是真实文件（-f/--file 的值）
        seen_pattern = False
        for tok in argv:
            if tok.startswith("-") or _windows_switch(tok):
                if "=" in tok:
                    value = tok.split("=", 1)[1]
                    if value:
                        operands.append(value)
                if pattern_first:
                    if tok in GREP_PATTERN_OPTIONS:
                        pattern_gate = True
                    elif tok in GREP_PATTERN_FILE_OPTIONS:
                        file_operand = True
                continue
            if pattern_first:
                if pattern_gate:
                    pattern_gate = False
                    if _looks_like_path(tok):
                        operands.append(tok)
                    continue
                if file_operand:
                    file_operand = False
                    operands.append(tok)
                    continue
                if not seen_pattern:
                    seen_pattern = True
                    if _looks_like_path(tok):
                        operands.append(tok)
                    continue
            operands.append(tok)
        return operands

    def _resolve_operand(self, raw: str, cwd: str | None = None) -> Path:
        """路径操作数解析：相对值以 subprocess cwd（缺省时工作区根）为基准。"""
        text = str(raw or "").strip()
        if not text:
            return self._root()
        p = Path(text)
        if not p.is_absolute() and not (len(text) > 1 and text[1] == ":"):
            base = Path(str(cwd)).resolve() if cwd else self._root()
            p = base / p
        try:
            return p.resolve()
        except Exception:  # noqa: BLE001
            return p

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
        """自由 shell（run_shell）的风险判定：永远需要高等级审批。

        即使命令看起来只是 ls，只要它被交给 shell，就可能通过元字符、
        环境变量替换、重定向等方式执行别的东西；这里不做「看起来安全就放行」。
        """
        risk = self.classify_command(cmd)
        if self.mode == "bypass":
            return "auto", risk
        if self.mode == "plan":
            return "deny", risk
        return "approve", risk

    def command_verdict_for_program(
        self, program: str, args: list[str] | None = None, *, cwd: str | None = None
    ) -> tuple[str, CommandRisk]:
        """run_program（argv + shell=False）的风险判定。

        cwd：run_program 将使用的子进程工作目录（可选；缺省按工作区根处理）。
        文件操作数一致性：红线 → deny（优先级最高）；根内 → auto；
        根外 → approve（plan 模式读根外也是 approve，与 fs_read 一致）。
        """
        risk, path_state = self._analyze_argv(program, args, cwd=cwd, check_resolve=True)
        if path_state == "deny":
            return "deny", (CommandRisk.HIGH if risk == CommandRisk.LOW else risk)
        if self.mode == "bypass":
            return "auto", risk
        if self.mode == "plan":
            if risk != CommandRisk.LOW:
                return "deny", risk
            return ("approve" if path_state == "approve" else "auto"), risk
        if risk != CommandRisk.LOW:
            return "approve", risk
        return ("approve" if path_state == "approve" else "auto"), risk
