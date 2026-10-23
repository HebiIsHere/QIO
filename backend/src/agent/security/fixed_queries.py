"""固定查询分类器（R9-G 契约 §4）——固定查询的**唯一**许可依据。

只按**真实程序身份 + 精确 argv 结构**判定；不搜索任何字符串，不按程序名整程序放行，
不因同名/通配/解释器参数/重定向/串联/嵌套获得豁免。解析不确定 → None（退出固定查询，
走普通流程：read_only 拒绝、restricted 由引擎/模型审查/审批处理）。

接口冻结（契约 §4）：
    FixedQueryFacts(kind, program_path, program_name, argv, cwd, shell)
    classify_fixed_query(facts) -> FixedQuery | None

`FixedQueryFacts` 必须由**启动方**从真实 LaunchSpec 生成（真实解析后的程序路径 +
实际 argv + 实际 cwd），不得由模型文本/工具自报。引擎会用 `effect.detail` 重新构造
facts 再调用本分类器（`fixed_query_facts_from_detail` 提供同一份构造逻辑），
因此工具侧的结论不是许可来源。

覆盖（仅限已验证无修改行为的具体用法）：
* 程序版本：python --version/-V、git --version、node/npm/pip/uv/cargo/rustc/go/java/
  dotnet/php/ruby/perl/deno/bun/gcc/clang/cmake/make/docker/podman 的版本查询形式；
* 当前目录：pwd（无参 / -P / -L）；
* 系统基础信息：whoami、uname 的只读标志、id 的只读标志、hostname、date（无参/POSIX 格式）；
* 指定目录文件列表：ls 的已验证参数（不整程序放行：参数结构不确定 → None）；
* 文件属性：stat / file 的已验证参数（目标路径作为 facts.paths 交给引擎读取规则校验）。

明确**不**属于固定查询：`shell=True` 的任何命令（无法证明无重定向/串联）、
解释器带脚本/内联代码（python -c、node -e 等）、未知参数、未知程序、同名替身
（程序位于 cwd/临时目录/名字与真实文件不一致）。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

WINDOWS = sys.platform == "win32"

#: argv 上限：固定查询都是小参数集合；超出即解析不确定（退出固定查询）。
MAX_ARGV = 64

#: Windows 可执行/包装后缀（只在名字归一化时剥离；路径本身不做扩展名猜测）。
#: .cmd/.bat 是 npm/pnpm/yarn 等在 Windows 上的官方入口；仍受「真实路径 + 名字 +
#: 不在 cwd/临时目录」的同名替身校验约束。
_WIN_EXE_SUFFIXES = (".exe", ".com", ".cmd", ".bat")


@dataclass(frozen=True)
class FixedQueryFacts:
    """由启动方从真实 LaunchSpec 生成的事实（非模型文本）。"""

    kind: str  # program | shell
    program_path: str | None
    program_name: str
    argv: tuple[str, ...]
    cwd: str | None
    shell: bool


@dataclass(frozen=True)
class FixedQuery:
    kind: str  # version | cwd | system_info | list_dir | stat
    facts: dict
    reason: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "facts": dict(self.facts), "reason": self.reason}


# --------------------------------------------------------------------------
# 程序身份归一化
# --------------------------------------------------------------------------

_PYTHON_RE = re.compile("^python(3(?:[.][0-9]+)*)?$")
_PYTHON3_RE = re.compile("^py$")

#: 程序版本查询：**精确 argv**（tuple 相等）才命中，不做前缀/包含匹配。
_VERSION_FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "python": (("--version",), ("-V",)),
    "git": (("--version",),),
    "node": (("--version",), ("-v",)),
    "npm": (("--version",), ("-v",)),
    "pnpm": (("--version",), ("-v",)),
    "yarn": (("--version",), ("-v",)),
    "pip": (("--version",), ("-V",)),
    "pip3": (("--version",), ("-V",)),
    "uv": (("--version",), ("-V",)),
    "cargo": (("--version",), ("-V",)),
    "rustc": (("--version",), ("-V",)),
    "go": (("version",),),
    "java": (("-version",), ("--version",)),
    "dotnet": (("--version",),),
    "php": (("--version",), ("-v",)),
    "ruby": (("--version",), ("-v",)),
    "perl": (("--version",), ("-v",)),
    "deno": (("--version",),),
    "bun": (("--version",), ("-v",)),
    "gcc": (("--version",),),
    "clang": (("--version",),),
    "cmake": (("--version",),),
    "make": (("--version",),),
    "docker": (("--version",),),
    "podman": (("--version",),),
}

_SYSTEM_INFO_FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "whoami": ((),),
    "uname": (
        (),
        ("-a",),
        ("-s",),
        ("-r",),
        ("-m",),
        ("-n",),
        ("-v",),
        ("-p",),
        ("-i",),
        ("-o",),
    ),
    "id": (
        (),
        ("-u",),
        ("-g",),
        ("-G",),
        ("-n",),
        ("-un",),
        ("-gn",),
        ("-Gn",),
        ("-rn",),
        ("-ru",),
        ("-rg",),
    ),
    "hostname": ((),),
    "date": ((),),
}

_CWD_FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "pwd": ((), ("-P",), ("-L",)),
}

_LIST_DIR_PROGRAMS = frozenset({"ls"})
_STAT_PROGRAMS = frozenset({"stat", "file"})
#: 真正的 shell：shell=True 或这些程序被 run_program 直接拉起时一律不做固定查询
#: （无法证明无重定向/串联/嵌套）。解释器（python/node/...）不在这里：它们只
#: 允许精确的版本参数形式，其它 argv 不会命中版本/查询形式而自然退出固定查询。
_SHELL_PROGRAMS = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "bash",
        "dash",
        "zsh",
        "fish",
        "wsl",
        "wsl.exe",
    }
)

#: ls 短选项（GNU/BSD 只读选项；不含任何写语义）。
_LS_SHORT_FLAGS = frozenset("aAbcCdDfFgGhHiklLmMnNopqQrRsStuUvwxX1")
#: ls 需要取值的短选项（值单独一个 token 或紧跟其后）。
_LS_VALUE_SHORT = frozenset("ITw")
#: ls 长选项（含只读语义；不认识的 → 解析不确定）。
_LS_LONG_FLAGS = frozenset(
    {
        "all",
        "almost-all",
        "author",
        "block-size",
        "classify",
        "color",
        "context",
        "directory",
        "dired",
        "escape",
        "file-type",
        "format",
        "full-time",
        "group-directories-first",
        "help",
        "hide",
        "human-readable",
        "ignore",
        "ignore-backups",
        "indicator-style",
        "inode",
        "literal",
        "numeric-uid-gid",
        "quote-name",
        "quoting-style",
        "recursive",
        "reverse",
        "show-control-chars",
        "si",
        "size",
        "sort",
        "tabsize",
        "time",
        "time-style",
        "version",
        "width",
    }
)
_LS_LONG_WITH_VALUE = frozenset(
    {
        "block-size",
        "color",
        "format",
        "hide",
        "ignore",
        "indicator-style",
        "quoting-style",
        "sort",
        "tabsize",
        "time",
        "time-style",
        "width",
    }
)

#: stat 短选项 / 取值短选项 / 长选项。
_STAT_SHORT_FLAGS = frozenset("cfLtT")
_STAT_VALUE_SHORT = frozenset({"c", "T"})
_STAT_LONG_FLAGS = frozenset(
    {
        "cached",
        "dereference",
        "file-system",
        "format",
        "help",
        "printf",
        "terse",
        "version",
    }
)
_STAT_LONG_WITH_VALUE = frozenset({"format", "printf"})

#: file 短选项 / 取值短选项 / 长选项（只读工具；仍按结构判定）。
_FILE_SHORT_FLAGS = frozenset("bChiIkLnNprsSvz")
_FILE_VALUE_SHORT = frozenset("efFmP")
_FILE_LONG_FLAGS = frozenset(
    {
        "brief",
        "dereference",
        "exclude",
        "exclude-quiet",
        "extension",
        "help",
        "keep-going",
        "list",
        "magic-file",
        "mime",
        "mime-encoding",
        "mime-type",
        "no-dereference",
        "print0",
        "separator",
        "uncompress",
        "version",
    }
)
_FILE_LONG_WITH_VALUE = frozenset(
    {"exclude", "magic-file", "separator"}
)


def _basename(path: str | None) -> str:
    text = str(path or "").strip().strip('"')
    if not text:
        return ""
    return text.replace(chr(92), "/").rsplit("/", 1)[-1]


def _normalize_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    base = _basename(text)
    if WINDOWS:
        base = base.lower()
        for suffix in _WIN_EXE_SUFFIXES:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
    return base


def _family(name: str) -> str | None:
    """真实程序名 → 已知程序族；未知 → None。"""
    if not name:
        return None
    if _PYTHON_RE.match(name) or _PYTHON3_RE.match(name):
        return "python"
    return name if name in _VERSION_FORMS or name in _SYSTEM_INFO_FORMS or name in _CWD_FORMS or name in _LIST_DIR_PROGRAMS or name in _STAT_PROGRAMS else None


def _temp_roots() -> list[str]:
    roots: list[str] = []
    for candidate in (
        tempfile.gettempdir(),
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        os.environ.get("TMPDIR"),
    ):
        if not candidate:
            continue
        try:
            resolved = str(Path(candidate).resolve())
        except (OSError, ValueError):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _real_path(path: str) -> Path | None:
    try:
        candidate = Path(str(path))
        if not candidate.is_absolute():
            return None
        return Path(os.path.realpath(str(candidate)))
    except (OSError, ValueError):
        return None


def _identity_problem(facts: FixedQueryFacts) -> str | None:
    """真实程序身份校验：不存在/不是文件/同名替身/位于 cwd 或临时目录 → 拒绝。"""
    raw_path = str(facts.program_path or "").strip()
    if not raw_path:
        return "缺少真实解析后的程序路径（无法核对程序身份）"
    real = _real_path(raw_path)
    if real is None:
        return "程序路径不是绝对路径（无法核对程序身份）"
    try:
        if not real.is_file():
            return "程序路径不是普通文件"
    except OSError:
        return "程序路径不可读"
    if not WINDOWS and not os.access(str(real), os.X_OK):
        return "程序文件没有可执行权限"
    display_name = _normalize_name(raw_path)
    if not display_name:
        return "程序文件名为空"
    if _basename(str(real)) and _normalize_name(str(real)) != display_name:
        # 符号链接允许（python3 -> python3.11），但必须属于同一已知程序族
        if _family(_normalize_name(str(real))) != _family(display_name):
            return "真实程序文件名与请求的程序身份不一致（可能同名替身）"
    requested = _normalize_name(facts.program_name)
    if requested and _family(requested) != _family(display_name):
        return "请求的程序与真实解析出的程序不是同一身份（可能同名替身）"
    cwd_text = str(facts.cwd or "").strip()
    if cwd_text:
        try:
            cwd_real = Path(os.path.realpath(cwd_text))
        except (OSError, ValueError):
            cwd_real = None
        if cwd_real is not None:
            try:
                if real == cwd_real or real.is_relative_to(cwd_real):
                    return "程序位于命令自己的工作目录之内（可能是同名替身）"
            except (OSError, ValueError):
                pass
    for root in _temp_roots():
        try:
            if real.is_relative_to(Path(root)):
                return "程序位于临时目录（可能是同名替身/一次性程序）"
        except (OSError, ValueError):
            continue
    return None


def _bad_token(token: str) -> bool:
    # argv 里出现 shell 控制字符/重定向/串联字符时，固定查询一律不成立
    # （程序本身可能不解释它们，但「查询 + 重定向/串联」必须退出固定查询）。
    if (not token) or (chr(0) in token) or (chr(10) in token) or (chr(13) in token):
        return True
    return any(char in token for char in ("|", "&", ";", "<", ">"))


def _split_long_option(token: str) -> tuple[str, str | None]:
    body = token[2:]
    if "=" in body:
        name, value = body.split("=", 1)
        return name, value
    return body, None


def _parse_structured(
    argv: tuple[str, ...],
    *,
    short_flags: frozenset[str],
    short_values: frozenset[str],
    long_flags: frozenset[str],
    long_values: frozenset[str],
) -> tuple[list[str], dict] | None:
    """解析「选项 + 操作数」结构；不确定 → None。返回 (paths, options facts)。"""
    if len(argv) > MAX_ARGV:
        return None
    paths: list[str] = []
    options: list[str] = []
    index = 0
    end_options = False
    while index < len(argv):
        token = str(argv[index])
        if _bad_token(token):
            return None
        if not end_options and token == "--":
            end_options = True
            index += 1
            continue
        if not end_options and token.startswith("-") and token != "-":
            if token.startswith("--"):
                name, value = _split_long_option(token)
                if name not in long_flags:
                    return None
                options.append("--" + name)
                if name in long_values and value is None:
                    if index + 1 >= len(argv) or _bad_token(str(argv[index + 1])):
                        return None
                    index += 2
                    continue
                index += 1
                continue
            # 短选项：一次可以合并多个，但取值短选项必须是最后一个字符（或自带值）
            body = token[1:]
            if not body:
                return None
            for position, char in enumerate(body):
                if char in short_values:
                    if position != len(body) - 1:
                        return None  # 组合在一起无法确定值归属
                    if index + 1 >= len(argv) or _bad_token(str(argv[index + 1])):
                        return None
                    index += 1
                    break
                if char not in short_flags:
                    return None
            else:
                pass
            options.append(token)
            index += 1
            continue
        paths.append(token)
        index += 1
    return paths, {"options": options, "paths": paths}


def _classify_version(family: str, argv: tuple[str, ...]) -> FixedQuery | None:
    forms = _VERSION_FORMS.get(family)
    if not forms:
        return None
    if argv not in forms:
        return None
    return FixedQuery(
        kind="version",
        facts={"argv": list(argv), "program_family": family},
        reason=f"程序版本查询：{family} 的精确版本参数（argv 完全匹配）",
    )


def _classify_simple_forms(
    family: str, argv: tuple[str, ...]
) -> FixedQuery | None:
    for kind, table in (
        ("system_info", _SYSTEM_INFO_FORMS),
        ("cwd", _CWD_FORMS),
    ):
        forms = table.get(family)
        if not forms:
            continue
        if kind == "system_info" and family == "date" and not argv:
            return FixedQuery(
                kind="system_info",
                facts={"argv": [], "program_family": family},
                reason="系统基础信息（date 无参查询）",
            )
        if kind == "system_info" and family == "date" and len(argv) == 1:
            token = str(argv[0])
            # POSIX 格式参数：以 + 开头的单个格式串（只影响输出格式，不修改系统）
            if token.startswith("+") and _bad_token(token) is False and len(token) <= 512:
                return FixedQuery(
                    kind="system_info",
                    facts={"argv": [token], "program_family": family},
                    reason="系统基础信息（date 只读格式参数）",
                )
            return None
        if argv in forms:
            return FixedQuery(
                kind=kind,
                facts={"argv": list(argv), "program_family": family},
                reason=(
                    "系统基础信息只读查询"
                    if kind == "system_info"
                    else "当前目录查询（pwd 只读参数）"
                ),
            )
    return None


def _classify_list_dir(argv: tuple[str, ...]) -> FixedQuery | None:
    parsed = _parse_structured(
        argv,
        short_flags=_LS_SHORT_FLAGS,
        short_values=_LS_VALUE_SHORT,
        long_flags=_LS_LONG_FLAGS,
        long_values=_LS_LONG_WITH_VALUE,
    )
    if parsed is None:
        return None
    paths, options = parsed
    if not paths:
        paths = ["."]  # ls 无操作数 = 当前目录（只读）
    return FixedQuery(
        kind="list_dir",
        facts={**options, "paths": list(paths), "options": list(options["options"])},
        reason="指定目录文件列表（ls 已验证只读参数；目标路径交由读取规则校验）",
    )


def _classify_stat(family: str, argv: tuple[str, ...]) -> FixedQuery | None:
    if family == "stat":
        parsed = _parse_structured(
            argv,
            short_flags=_STAT_SHORT_FLAGS,
            short_values=_STAT_VALUE_SHORT,
            long_flags=_STAT_LONG_FLAGS,
            long_values=_STAT_LONG_WITH_VALUE,
        )
    else:
        parsed = _parse_structured(
            argv,
            short_flags=_FILE_SHORT_FLAGS,
            short_values=_FILE_VALUE_SHORT,
            long_flags=_FILE_LONG_FLAGS,
            long_values=_FILE_LONG_WITH_VALUE,
        )
    if parsed is None:
        return None
    paths, options = parsed
    if not paths:
        return None  # stat/file 没有操作数没有查询目标：不属固定查询
    return FixedQuery(
        kind="stat",
        facts={**options, "paths": list(paths), "options": list(options["options"])},
        reason="文件大小/类型/修改时间属性查询（已验证只读参数；目标路径交由读取规则校验）",
    )


def classify_fixed_query(facts: FixedQueryFacts) -> FixedQuery | None:
    """固定查询判定（纯函数）。

    * 不搜索任何文字：只看真实程序身份 + argv 结构；
    * shell=True 一律 None（无法证明无重定向/串联/嵌套）；
    * 未知程序 / 未知参数 / 同名替身 / 解释器脚本 / 危险参数 → None。
    """
    try:
        if facts is None:
            return None
        if bool(facts.shell) or str(facts.kind or "").strip().lower() == "shell":
            return None
        if not str(facts.program_path or "").strip():
            return None
        argv = tuple(str(item) for item in (facts.argv or ()))
        if len(argv) > MAX_ARGV:
            return None
        if any(_bad_token(token) for token in argv):
            return None
        identity_problem = _identity_problem(facts)
        if identity_problem:
            return None
        real_name = _normalize_name(str(facts.program_path))
        family = _family(real_name)
        if family is None:
            # 解释器/未知程序不做固定查询豁免（即使 argv 看起来像版本参数）
            return None
        if _normalize_name(str(facts.program_path)) in _SHELL_PROGRAMS:
            return None
        version = _classify_version(family, argv)
        if version is not None:
            return version
        simple = _classify_simple_forms(family, argv)
        if simple is not None:
            return simple
        if family in _LIST_DIR_PROGRAMS:
            return _classify_list_dir(argv)
        if family in _STAT_PROGRAMS:
            return _classify_stat(family, argv)
        return None
    except Exception:  # noqa: BLE001 - 分类失败按「不是固定查询」处理（fail-closed）
        return None


# --------------------------------------------------------------------------
# 给 engine 的接线辅助（Lead 接线用；同一份构造逻辑，避免两套 facts）
# --------------------------------------------------------------------------


def fixed_query_facts_from_detail(detail: Mapping[str, Any] | None) -> FixedQueryFacts | None:
    """从 effect.detail 构造 FixedQueryFacts（引擎重算用）。

    新 effect 把启动方从真实 LaunchSpec 生成的事实放在 `detail['execution']`；
    旧字段（argv=[program,*args]、shell）只作为**无法提供真实路径**时的保守回退
    （此时 program_path 为空 → 分类器必然返回 None，不会误放行）。
    """
    data = dict(detail or {})
    execution = data.get("execution")
    if isinstance(execution, Mapping):
        return FixedQueryFacts(
            kind=str(execution.get("kind") or "program"),
            program_path=(
                str(execution["program_path"]) if execution.get("program_path") else None
            ),
            program_name=str(execution.get("program_name") or ""),
            argv=tuple(str(item) for item in (execution.get("argv") or ())),
            cwd=(str(execution["cwd"]) if execution.get("cwd") else None),
            shell=bool(execution.get("shell")),
        )
    legacy_argv = [str(item) for item in (data.get("argv") or ())]
    program = str(data.get("program") or "") or (legacy_argv[0] if legacy_argv else "")
    args = legacy_argv[1:] if legacy_argv and legacy_argv[0] == program else legacy_argv
    return FixedQueryFacts(
        kind="shell" if data.get("shell") else "program",
        program_path=None,
        program_name=program,
        argv=tuple(args),
        cwd=(str(data["cwd"]) if data.get("cwd") else None),
        shell=bool(data.get("shell")),
    )


__all__ = [
    "FixedQueryFacts",
    "FixedQuery",
    "classify_fixed_query",
    "fixed_query_facts_from_detail",
]
