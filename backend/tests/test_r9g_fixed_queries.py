"""R9-G 契约 §4：固定查询分类器正反例。

原则：只按**真实程序身份 + 精确 argv 结构**判定；不搜索文字；不整程序放行；
同名替身（cwd/临时目录/名字不符）、危险参数、解释器脚本、重定向/串联/嵌套、
未知程序/未知参数 → 一律 None（退出固定查询，走普通流程）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from agent.security.fixed_queries import (
    FixedQueryFacts,
    classify_fixed_query,
    fixed_query_facts_from_detail,
)

WINDOWS = sys.platform == "win32"
PY = sys.executable


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if WINDOWS:
        for base in (
            Path("C:/Program Files/Git/usr/bin"),
            Path("C:/Program Files/Git/bin"),
            Path("C:/Windows/System32"),
        ):
            candidate = base / (name + ".exe")
            if candidate.is_file():
                return str(candidate)
    return None


def _facts(
    program: str,
    args=(),
    *,
    path: str | None = None,
    cwd: str | None = None,
    shell: bool = False,
    name: str | None = None,
) -> FixedQueryFacts:
    return FixedQueryFacts(
        kind="shell" if shell else "program",
        program_path=path if path is not None else _which(program),
        program_name=name or program,
        argv=tuple(args),
        cwd=cwd,
        shell=shell,
    )


def _kind(facts: FixedQueryFacts) -> str | None:
    result = classify_fixed_query(facts)
    return None if result is None else result.kind


# --------------------------------------------------------------------------
# 程序版本
# --------------------------------------------------------------------------


def test_python_version_forms_are_fixed_queries():
    facts = _facts("python", ["--version"], path=PY, name="python")
    assert _kind(facts) == "version"
    assert _kind(_facts("python", ["-V"], path=PY, name="python")) == "version"


@pytest.mark.parametrize(
    "args",
    [
        ["-v"],  # python -v 是 verbose（不是版本查询）
        ["-c", "print(1)"],
        ["-m", "pip", "--version"],
        ["script.py"],
        ["--version", "extra"],
        ["--version", ";", "echo", "x"],
        ["--version", ">", "out.txt"],
    ],
)
def test_python_non_version_forms_are_not_fixed_queries(args):
    assert _kind(_facts("python", args, path=PY, name="python")) is None


def test_git_version_exact_forms():
    git = _which("git")
    if not git:
        pytest.skip("本机没有 git")
    assert _kind(_facts("git", ["--version"], path=git)) == "version"
    for args in (["status"], ["--version", "x"], ["-C", ".", "--version"]):
        assert _kind(_facts("git", args, path=git)) is None


def test_node_npm_version_forms():
    for name, forms in (("node", (["--version"], ["-v"])), ("npm", (["--version"], ["-v"]))):
        path = _which(name)
        if not path:
            continue
        for form in forms:
            assert _kind(_facts(name, form, path=path)) == "version"
        assert _kind(_facts(name, ["install", "x"], path=path)) is None


# --------------------------------------------------------------------------
# 当前目录 / 系统基础信息
# --------------------------------------------------------------------------


def test_pwd_only_readonly_forms():
    pwd = _which("pwd")
    if not pwd:
        pytest.skip("本机没有 pwd")
    assert _kind(_facts("pwd", [], path=pwd)) == "cwd"
    assert _kind(_facts("pwd", ["-P"], path=pwd)) == "cwd"
    assert _kind(_facts("pwd", ["--help"], path=pwd)) is None
    assert _kind(_facts("pwd", ["extra"], path=pwd)) is None


def test_system_info_whitelist_not_whole_program():
    whoami = _which("whoami")
    if whoami:
        assert _kind(_facts("whoami", [], path=whoami)) == "system_info"
        assert _kind(_facts("whoami", ["/all"], path=whoami)) is None
    uname = _which("uname")
    if uname:
        assert _kind(_facts("uname", [], path=uname)) == "system_info"
        assert _kind(_facts("uname", ["-a"], path=uname)) == "system_info"
        assert _kind(_facts("uname", ["-a", "-s"], path=uname)) is None
        assert _kind(_facts("uname", ["--kernel-name"], path=uname)) is None


# --------------------------------------------------------------------------
# 目录列表 / 文件属性（结构判定，目标路径交给读取规则）
# --------------------------------------------------------------------------


def test_list_dir_structural_forms():
    ls = _which("ls")
    if not ls:
        pytest.skip("本机没有 ls")
    assert _kind(_facts("ls", [], path=ls)) == "list_dir"
    assert _kind(_facts("ls", ["-la"], path=ls)) == "list_dir"
    assert _kind(_facts("ls", ["-I", "*.tmp", "."], path=ls)) == "list_dir"
    assert _kind(_facts("ls", ["--definitely-not-an-option"], path=ls)) is None
    assert _kind(_facts("ls", ["x", "&&", "rm", "y"], path=ls)) is None
    assert _kind(_facts("ls", ["x", ">", "y"], path=ls)) is None


def test_stat_and_file_structural_forms():
    stat = _which("stat")
    if stat:
        result = classify_fixed_query(_facts("stat", [PY], path=stat))
        assert result is not None and result.kind == "stat"
        assert Path(PY).resolve().exists()
        assert (result.facts.get("paths") or []) == [PY]
        formatted = classify_fixed_query(_facts("stat", ["-c", "%s", PY], path=stat))
        assert formatted is not None and formatted.kind == "stat"
        assert _kind(_facts("stat", ["--unknown-flag"], path=stat)) is None
        assert _kind(_facts("stat", [], path=stat)) is None
    file_tool = _which("file")
    if file_tool:
        result = classify_fixed_query(_facts("file", [PY], path=file_tool))
        assert result is not None and result.kind == "stat"


# --------------------------------------------------------------------------
# 同名替身 / 解释器 / shell / 未知
# --------------------------------------------------------------------------


def test_same_name_program_in_temp_or_cwd_is_rejected(tmp_path):
    fake = tmp_path / ("python.exe" if WINDOWS else "python")
    fake.write_bytes(b"not a real program")
    assert _kind(_facts("python", ["--version"], path=str(fake))) is None

    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    cwd_fake = cwd_dir / ("python.exe" if WINDOWS else "python")
    cwd_fake.write_bytes(b"fake")
    assert (
        _kind(
            _facts(
                "python", ["--version"], path=str(cwd_fake), cwd=str(cwd_dir)
            )
        )
        is None
    )


def test_name_identity_mismatch_is_rejected():
    git = _which("git")
    if not git:
        pytest.skip("本机没有 git")
    # 真实路径是 git，但 facts 声称是 python → 身份不一致
    assert _kind(_facts("git", ["--version"], path=git, name="python")) is None


def test_shell_is_never_a_fixed_query():
    pwd = _which("pwd")
    if pwd:
        assert _kind(_facts("pwd", [], path=pwd, shell=True)) is None
    assert (
        _kind(_facts("cmd", ["/c", "whoami"], shell=True)) is None if WINDOWS else True
    )


def test_unknown_program_or_missing_path_is_not_fixed_query():
    assert _kind(_facts("some-random-tool", ["--version"], path=PY, name="some-random-tool")) is None
    assert _kind(_facts("python", ["--version"], path="", name="python")) is None


def test_control_flow_tokens_break_fixed_query():
    ls = _which("ls")
    if not ls:
        pytest.skip("本机没有 ls")
    for args in (["."], ["-la", "."]):
        assert _kind(_facts("ls", args, path=ls)) == "list_dir"
    for token in ("|", "&", ";", "<", ">"):
        assert _kind(_facts("ls", [".", token, "x"], path=ls)) is None


# --------------------------------------------------------------------------
# 给 engine 的 facts 构造（同一份逻辑）
# --------------------------------------------------------------------------


def test_facts_from_effect_detail_roundtrip():
    git = _which("git")
    if not git:
        pytest.skip("本机没有 git")
    facts = fixed_query_facts_from_detail(
        {
            "execution": {
                "kind": "program",
                "program_path": git,
                "program_name": "git",
                "argv": ["--version"],
                "cwd": None,
                "shell": False,
            }
        }
    )
    assert facts is not None
    assert classify_fixed_query(facts) is not None
    # 旧 effect 没有真实程序路径 → 不因 argv 看起来像版本参数就放行
    legacy = fixed_query_facts_from_detail({"argv": ["git", "--version"], "shell": False})
    assert legacy is not None
    assert classify_fixed_query(legacy) is None


def test_classifier_is_mode_independent_pure_function():
    facts = _facts("python", ["--version"], path=PY, name="python")
    first = classify_fixed_query(facts)
    second = classify_fixed_query(facts)
    assert first is not None and second is not None
    assert first.kind == second.kind == "version"
