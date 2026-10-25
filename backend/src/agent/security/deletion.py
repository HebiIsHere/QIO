"""永久删除结构化识别（R9-G 契约 §5）。

只按**实际程序身份 + 参数语法/已知语义**识别永久删除；不全文搜索命令文字，不因
讨论/引用/输出里出现 rm/del/Remove-Item 等词而触发。

接口冻结（契约 §5）：
    DeleteObject(path, identity, is_directory)
    DeleteIntent(permanent, objects, method, program, argv, shell_cmd, verified, reason)
    detect_delete_intent(*, effect_kind, detail, facts) -> DeleteIntent | None

要点：
* 命令按解析出的程序身份与 argv 语法判定：rm / rmdir / unlink / shred / find -delete /
  git rm / git clean / Windows 的 del / erase / rd（经 shell 命令语法或同名可执行文件）；
* shell 含管道/重定向/串联/换行 → 不属于可解析的永久删除（返回 None，走普通流程）；
* 其它无法解析的形态（解释器内联代码、sudo、xargs、嵌套 shell、shell 变量/通配符展开）
  一律不走「可确认的永久删除」捷径：返回 None（普通流程）或 verified=False 的意图；
  **绝不因为「没命中某个词」而放行**；
* 文件工具：只有明确声明永久删除的合法工具/参数才产生 permanent intent；默认删除
  仍走系统回收站（effect_kind=delete 且 detail.permanent 不是 True → None）。

`facts` 可由 fixed_queries.FixedQueryFacts / dict（program_path/program_name/argv/
cwd/shell/shell_cmd）提供；新 effect 的启动方把真实事实放在 detail['execution']。
本模块不判权限、不做模式判断（政策在 engine，执行入口只按层做兜底与审批绑定）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

WINDOWS = sys.platform == "win32"

_SHELL_CONTROL_CHARS = ("|", "&", ";", "<", ">", chr(10), chr(13))
def _shell_ambiguous_chars() -> tuple[str, ...]:
    # POSIX shell 里反斜杠是转义符；Windows cmd 里反斜杠是路径分隔符，不属歧义字符。
    chars = (
        '"',
        "'",
        chr(96),
        "$",
        "(",
        ")",
        "{",
        "}",
        "%",
        "^",
        "[",
        "]",
        "*",
        "?",
    )
    if not WINDOWS:
        chars = chars + (chr(92),)
    return chars
_GLOB_CHARS = ("*", "?", "[", "]", "$", "%", "~", chr(96), '"', "'")


@dataclass(frozen=True)
class DeleteObject:
    path: str
    identity: str | None
    is_directory: bool

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "identity": self.identity,
            "is_directory": bool(self.is_directory),
        }


@dataclass(frozen=True)
class DeleteIntent:
    permanent: bool
    objects: tuple[DeleteObject, ...]
    method: str  # fs.permanent_delete | command.delete
    program: str | None
    argv: tuple[str, ...]
    shell_cmd: str | None
    verified: bool
    reason: str
    #: 本次删除是**递归**删除（-r/-R/--recursive、Windows /s）：目录操作数的
    #: 真实影响包含其全部子对象，调用方必须展开而不是只核对目录本身（契约 §3.D3）。
    recursive: bool = False

    def as_dict(self) -> dict:
        return {
            "permanent": bool(self.permanent),
            "objects": [item.as_dict() for item in self.objects],
            "method": self.method,
            "program": self.program,
            "argv": list(self.argv),
            "shell_cmd": self.shell_cmd,
            "verified": bool(self.verified),
            "reason": self.reason,
            "recursive": bool(self.recursive),
        }

    def identity_set(self) -> tuple[tuple[str, str, bool], ...]:
        """对象集合摘要（审批绑定/重核对用；path+identity+目录标记）。"""
        return tuple(
            (item.path, str(item.identity or ""), bool(item.is_directory))
            for item in self.objects
        )


# --------------------------------------------------------------------------
# 程序身份 / 参数解析
# --------------------------------------------------------------------------

_POSIX_DELETE_PROGRAMS = frozenset({"rm", "rmdir", "unlink", "shred"})
_WINDOWS_DELETE_PROGRAMS = frozenset({"del", "erase", "rd", "rmdir"})
_ALL_DELETE_PROGRAMS = _POSIX_DELETE_PROGRAMS | _WINDOWS_DELETE_PROGRAMS

_RM_FLAGS = frozenset(
    {
        "-f",
        "--force",
        "-i",
        "--interactive",
        "-r",
        "-R",
        "--recursive",
        "-d",
        "--dir",
        "-v",
        "--verbose",
        "--one-file-system",
        "--no-preserve-root",
        "--preserve-root",
        "-I",
        "--interactive=always",
        "--interactive=once",
        "--interactive=never",
    }
)
_RMDIR_FLAGS = frozenset(
    {
        "-p",
        "--parents",
        "-v",
        "--verbose",
        "--ignore-fail-on-non-empty",
        "--",
    }
)
_UNLINK_FLAGS = frozenset({"--help", "--version"})
_SHRED_FLAGS = frozenset(
    {
        "-f",
        "--force",
        "-n",
        "--iterations",
        "-s",
        "--size",
        "-u",
        "--remove",
        "-v",
        "--verbose",
        "-x",
        "--exact",
        "-z",
        "--zero",
    }
)
_SHRED_VALUE_FLAGS = frozenset({"-n", "--iterations", "-s", "--size"})

_WIN_DEL_FLAG_PREFIXES = ("/f", "/q", "/s", "/p", "/a")
_WIN_RD_FLAG_PREFIXES = ("/s", "/q")


def _basename(value: Any) -> str:
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    return text.replace(chr(92), "/").rsplit("/", 1)[-1]


def _normalize_program(name: Any) -> str:
    base = _basename(name).lower()
    for suffix in (".exe", ".com", ".cmd", ".bat"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _identity_of(path: str) -> str | None:
    try:
        from agent.services import file_identity

        found = file_identity.identity_of(Path(path))
    except Exception:  # noqa: BLE001 - 身份读不到按 None（不编造）
        return None
    if found is None:
        return None
    volume = str(getattr(found, "volume", "") or "")
    file_id = str(getattr(found, "file_id", "") or "")
    if volume and file_id:
        return volume + ":" + file_id
    return str(getattr(found, "key", "") or "") or None


def _resolve_object(raw: str, *, cwd: str | None) -> tuple[str, bool, bool]:
    """操作数 → (路径字符串, 是否能可靠解析, 是否目录)。"""
    text = str(raw or "").strip()
    if not text:
        return "", False, False
    try:
        candidate = Path(text)
    except (OSError, ValueError):
        return text, False, False
    if not candidate.is_absolute():
        if not cwd:
            return text, False, False
        try:
            candidate = Path(cwd) / candidate
        except (OSError, ValueError):
            return text, False, False
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return str(candidate), False, False
    try:
        is_directory = resolved.is_dir()
    except OSError:
        is_directory = False
    return str(resolved), True, is_directory


def _object(raw: str, *, cwd: str | None) -> tuple[DeleteObject, bool]:
    path, resolved_ok, is_directory = _resolve_object(raw, cwd=cwd)
    identity = _identity_of(path) if resolved_ok else None
    return DeleteObject(path=path, identity=identity, is_directory=is_directory), resolved_ok


def _literal_operand_problem(token: str) -> str | None:
    text = str(token or "")
    if not text:
        return "空操作数"
    if any(char in text for char in _GLOB_CHARS):
        return "操作数包含通配/展开字符，无法确定具体对象"
    if text in ("-", "--"):
        return "操作数不是具体路径"
    return None


def _parse_posix_args(
    argv: tuple[str, ...],
    *,
    flags: frozenset[str],
    value_flags: frozenset[str] = frozenset(),
) -> tuple[list[str], bool, str]:
    """解析 POSIX 风格参数。返回 (操作数, 是否完整解析, 说明)。"""
    operands: list[str] = []
    verified = True
    notes: list[str] = []
    index = 0
    end_options = False
    while index < len(argv):
        token = str(argv[index])
        if not end_options and token == "--":
            end_options = True
            index += 1
            continue
        if not end_options and token.startswith("-") and token != "-":
            if token in value_flags:
                index += 2
                continue
            if token in flags:
                index += 1
                continue
            if "=" in token and token.split("=", 1)[0] in flags:
                index += 1
                continue
            # 组合短选项（-rf / -fr / -Rf...）：每一个字符都必须是已知的短标志
            short_letters = {
                flag[1]
                for flag in flags
                if len(flag) == 2 and flag.startswith("-") and not flag.startswith("--")
            }
            if token.startswith("-") and not token.startswith("--"):
                body = token[1:]
                value_letters = {
                    flag[1]
                    for flag in value_flags
                    if len(flag) == 2 and flag.startswith("-")
                }
                if body and all(char in short_letters for char in body):
                    index += 1
                    continue
                if body and body[-1] in value_letters and all(
                    char in short_letters or char in value_letters for char in body[:-1]
                ):
                    index += 2
                    continue
            verified = False
            notes.append("未知参数 " + token)
            index += 1
            continue
        problem = _literal_operand_problem(token)
        if problem:
            verified = False
            notes.append(problem)
        operands.append(token)
        index += 1
    return operands, verified, "；".join(notes)


def _parse_windows_args(
    argv: tuple[str, ...], *, flag_prefixes: tuple[str, ...]
) -> tuple[list[str], bool, str]:
    operands: list[str] = []
    verified = True
    notes: list[str] = []
    for token in argv:
        text = str(token)
        if text.startswith("/") and text != "/":
            lowered = text.lower()
            if any(lowered == prefix or lowered.startswith(prefix + ":") for prefix in flag_prefixes):
                continue
            verified = False
            notes.append("未知参数 " + text)
            continue
        problem = _literal_operand_problem(text)
        if problem:
            verified = False
            notes.append(problem)
        operands.append(text)
    return operands, verified, "；".join(notes)


def _short_option_chars(argv: tuple[str, ...]) -> set[str]:
    """收集短选项字符（-rf → {r,f}）；``--`` 之后不再解析。"""
    chars: set[str] = set()
    end_of_options = False
    for token in argv:
        text = str(token)
        if end_of_options:
            continue
        if text == "--":
            end_of_options = True
            continue
        if not text.startswith("-") or text.startswith("--") or len(text) < 2:
            continue
        chars.update(char.lower() for char in text[1:])
    return chars


def _has_any_flag(argv: tuple[str, ...], names: tuple[str, ...]) -> bool:
    wanted = {str(name).lower() for name in names}
    return any(str(token).lower() in wanted for token in argv)


def _recursive_delete(name: str, argv: tuple[str, ...]) -> bool:
    """按程序身份 + 参数语义判断本次是否递归删除（会波及其子对象）。

    只依据真实启动语义（程序名 + 选项），不搜索命令文字；无法确定按 False 返回，
    但目录操作数仍由调用方按目录完整性核对（不因为这里 False 就当完整）。
    """
    args = tuple(str(item) for item in argv)
    if WINDOWS:
        if name in ("rd", "rmdir"):
            return _has_any_flag(args, ("/s",)) or any(
                str(token).lower().startswith("/s:") for token in args
            )
        if name in ("del", "erase"):
            return _has_any_flag(args, ("/s",)) or any(
                str(token).lower().startswith("/s:") for token in args
            )
        return False
    if name == "rm":
        return "r" in _short_option_chars(args) or _has_any_flag(
            args, ("--recursive",)
        )
    if name == "git":
        # git rm 的递归语义由 -r/--recursive 决定（仅磁盘删除非 --cached 时）。
        return "r" in _short_option_chars(args[1:]) or _has_any_flag(
            args[1:], ("--recursive",)
        )
    return False


def _intent_from_program(
    *,
    program_name: str,
    program_path: str | None,
    argv: tuple[str, ...],
    cwd: str | None,
    shell_cmd: str | None = None,
    extra_verified: bool = True,
    extra_note: str = "",
) -> DeleteIntent | None:
    name = _normalize_program(program_name or program_path)
    method = "command.delete"
    operands: list[str] = []
    verified = bool(extra_verified)
    notes: list[str] = [extra_note] if extra_note else []
    display_program = program_name or program_path
    recursive = _recursive_delete(name, argv)
    if name == "find":
        if "-delete" not in [str(item) for item in argv]:
            return None
        return DeleteIntent(
            permanent=True,
            objects=(),
            method=method,
            program=display_program,
            argv=tuple(str(item) for item in argv),
            shell_cmd=shell_cmd,
            verified=False,
            reason="find 的 -delete 动作会永久删除匹配到的文件；表达式/匹配集合无法可靠还原，"
            "本次不列为可确认的对象集合",
        )
    if name == "git":
        args = [str(item) for item in argv]
        if args and args[0] == "rm":
            operands, parsed, note = _parse_posix_args(tuple(args[1:]), flags=frozenset({"-f", "--force", "-r", "--cached", "-q", "--quiet", "--ignore-unmatch"}))
            verified = verified and parsed
            if note:
                notes.append(note)
        elif args and args[0] == "clean":
            force = any(str(item).startswith("-f") or str(item) == "--force" for item in args[1:])
            if not force:
                return None
            return DeleteIntent(
                permanent=True,
                objects=(),
                method=method,
                program=display_program,
                argv=tuple(args),
                shell_cmd=shell_cmd,
                verified=False,
                reason="git clean -f 会永久删除未跟踪文件；具体对象集合由 git 内部展开，无法可靠还原",
            )
        else:
            return None
    elif name == "rm":
        operands, parsed, note = _parse_posix_args(argv, flags=_RM_FLAGS)
        verified = verified and parsed
        if note:
            notes.append(note)
    elif name == "rmdir":
        operands, parsed, note = _parse_posix_args(argv, flags=_RMDIR_FLAGS)
        verified = verified and parsed
        if note:
            notes.append(note)
    elif name == "unlink":
        operands, parsed, note = _parse_posix_args(argv, flags=_UNLINK_FLAGS)
        if len(operands) != 1:
            verified = False
            notes.append("unlink 只接受一个操作数")
        verified = verified and parsed
        if note:
            notes.append(note)
    elif name == "shred":
        operands, parsed, note = _parse_posix_args(
            argv, flags=_SHRED_FLAGS, value_flags=_SHRED_VALUE_FLAGS
        )
        verified = verified and parsed
        if note:
            notes.append(note)
    elif name in _WINDOWS_DELETE_PROGRAMS:
        prefixes = _WIN_RD_FLAG_PREFIXES if name in ("rd", "rmdir") else _WIN_DEL_FLAG_PREFIXES
        operands, parsed, note = _parse_windows_args(argv, flag_prefixes=prefixes)
        verified = verified and parsed
        if note:
            notes.append(note)
    else:
        return None
    if not operands:
        # 没有任何具体对象：不构成可确认的永久删除（rm 无参会报错，不会删东西）
        return None
    objects: list[DeleteObject] = []
    for operand in operands:
        item, resolved = _object(operand, cwd=cwd)
        if not resolved:
            verified = False
            notes.append("对象路径无法在审批前可靠解析：" + operand)
        objects.append(item)
    reason = "程序身份 " + str(display_program) + " 的参数语法结构表示永久删除（不进系统回收站）"
    if recursive:
        reason += "；这是递归删除：目录操作数的子对象同样属于真实影响"
    if notes:
        reason += "；" + "；".join(notes[:4])
    return DeleteIntent(
        permanent=True,
        objects=tuple(objects),
        method=method,
        program=display_program,
        argv=tuple(str(item) for item in argv),
        shell_cmd=shell_cmd,
        verified=bool(verified),
        reason=reason,
        recursive=bool(recursive),
    )


def _split_shell_tokens(command: str) -> tuple[list[str] | None, bool]:
    """极简 shell 命令 token 化：只用于识别「命令位置上的删除程序」。

    返回 (tokens, ambiguous)。含管道/重定向/串联/换行 → tokens=None（不属于可解析的
    永久删除，走普通流程）；含引号/展开/转义 → ambiguous=True（对象集合不可靠）。
    """
    text = str(command or "")
    if not text.strip():
        return None, False
    if any(char in text for char in _SHELL_CONTROL_CHARS):
        return None, False
    ambiguous = any(char in text for char in _shell_ambiguous_chars())
    tokens = [token for token in text.split() if token]
    # 跳过前置环境变量赋值（VAR=value），它们不改变「实际执行哪个程序」
    while tokens:
        head = tokens[0]
        if "=" in head:
            name = head.split("=", 1)[0]
            if name and all(char.isalnum() or char == "_" for char in name):
                tokens = tokens[1:]
                continue
        break
    return tokens, ambiguous


def _detect_shell_delete(shell_cmd: str | None, *, cwd: str | None) -> DeleteIntent | None:
    tokens, ambiguous = _split_shell_tokens(shell_cmd or "")
    if not tokens:
        return None
    program = tokens[0]
    name = _normalize_program(program)
    if name not in _ALL_DELETE_PROGRAMS and name not in ("find", "git"):
        # 解释器/其它程序（python -c、sudo、xargs、嵌套 shell…）：不解析，走普通流程
        return None
    intent = _intent_from_program(
        program_name=program,
        program_path=None,
        argv=tuple(tokens[1:]),
        cwd=cwd,
        shell_cmd=str(shell_cmd or ""),
        extra_verified=not ambiguous,
        extra_note=(
            "shell 命令含引号/展开/转义字符，对象集合可能不完整"
            if ambiguous
            else ""
        ),
    )
    return intent


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def _view(facts: Any) -> dict:
    if facts is None:
        return {}
    if isinstance(facts, Mapping):
        return dict(facts)
    return {
        name: getattr(facts, name, None)
        for name in ("kind", "program_path", "program_name", "argv", "cwd", "shell", "shell_cmd")
    }


def _file_tool_intent(detail: Mapping[str, Any]) -> DeleteIntent | None:
    data = dict(detail or {})
    if data.get("permanent") is not True:
        return None
    raw_objects = data.get("objects")
    if raw_objects is None:
        raw_objects = data.get("paths")
    if raw_objects is None and data.get("path"):
        raw_objects = [data.get("path")]
    if not isinstance(raw_objects, (list, tuple)):
        raw_objects = []
    objects: list[DeleteObject] = []
    verified = bool(raw_objects)
    for raw in raw_objects:
        if isinstance(raw, Mapping):
            path = str(raw.get("path") or "")
            identity = raw.get("identity") or None
            is_directory = bool(raw.get("is_directory"))
            if not path:
                verified = False
                continue
            if not identity:
                identity = _identity_of(path)
            if not is_directory:
                try:
                    is_directory = Path(path).is_dir()
                except OSError:
                    is_directory = False
            objects.append(
                DeleteObject(
                    path=path,
                    identity=str(identity) if identity else None,
                    is_directory=is_directory,
                )
            )
        else:
            item, resolved = _object(str(raw), cwd=str(data.get("cwd") or "") or None)
            if not resolved:
                verified = False
            objects.append(item)
    if not objects:
        return None
    return DeleteIntent(
        permanent=True,
        objects=tuple(objects),
        method="fs.permanent_delete",
        program=str(data.get("tool") or "") or None,
        argv=(),
        shell_cmd=None,
        verified=verified,
        reason="文件工具明确声明永久删除且列出具体对象（默认删除语义必须走系统回收站）",
    )


def detect_delete_intent(*, effect_kind: Any, detail: Any, facts: Any = None) -> DeleteIntent | None:
    """结构化永久删除识别（纯函数）。返回 None = 不是可识别的永久删除。

    * effect_kind 为 EXECUTE：从实际程序身份 + argv 结构 / shell 命令语法识别；
    * effect_kind 为 DELETE：只有明确声明 permanent=True 才产生意图（默认回收站）；
    * 不搜索命令文字/用户文字；不确定 → None 或 verified=False（由政策决定确认方式）。
    """
    try:
        raw_kind = getattr(effect_kind, "value", effect_kind)
        kind = str(raw_kind or "").strip().lower()
        data = dict(detail or {}) if isinstance(detail, Mapping) else {}
        if kind == "delete":
            return _file_tool_intent(data)
        if kind != "execute":
            return None
        execution = data.get("execution")
        execution = dict(execution) if isinstance(execution, Mapping) else {}
        view = _view(facts)
        program_path = execution.get("program_path") or view.get("program_path")
        program_name = execution.get("program_name") or view.get("program_name")
        cwd_value = execution.get("cwd") or view.get("cwd")
        cwd = str(cwd_value) if cwd_value else None
        shell = bool(execution.get("shell") if "shell" in execution else view.get("shell"))
        shell_cmd = (
            execution.get("shell_cmd")
            or view.get("shell_cmd")
            or data.get("command")
            or data.get("cmd")
        )
        if shell or _normalize_program(program_name or program_path) in (
            "cmd",
            "powershell",
            "pwsh",
            "sh",
            "bash",
            "dash",
            "zsh",
            "fish",
            "wsl",
        ):
            return _detect_shell_delete(str(shell_cmd or ""), cwd=cwd) if shell_cmd else None
        raw_argv = execution.get("argv")
        if raw_argv is None:
            raw_argv = view.get("argv") or ()
        argv = tuple(str(item) for item in raw_argv)
        name = _normalize_program(program_name or program_path)
        if name not in _ALL_DELETE_PROGRAMS and name not in ("find", "git"):
            return None
        return _intent_from_program(
            program_name=str(program_name or program_path or ""),
            program_path=str(program_path) if program_path else None,
            argv=argv,
            cwd=cwd,
        )
    except Exception:  # noqa: BLE001 - 识别失败按「不是可识别永久删除」（普通流程）
        return None


def detect_delete_intent_for_effect(effect: Any) -> DeleteIntent | None:
    """effect → intent 的便捷入口（engine 接线用；不判权限、不读模式）。"""
    detail = dict(getattr(effect, "detail", {}) or {})
    kind = getattr(effect, "kind", None)
    facts = None
    try:
        from agent.security.fixed_queries import fixed_query_facts_from_detail

        facts = fixed_query_facts_from_detail(detail)
    except Exception:  # noqa: BLE001 - facts 只用于补充，缺失时按 detail 解析
        facts = None
    return detect_delete_intent(effect_kind=kind, detail=detail, facts=facts)


__all__ = [
    "DeleteObject",
    "DeleteIntent",
    "detect_delete_intent",
    "detect_delete_intent_for_effect",
]
