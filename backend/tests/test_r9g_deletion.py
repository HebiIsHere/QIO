"""R9-G 契约 §5：永久删除结构化识别正反例。

原则：按实际程序身份 + 参数语法/已知语义识别；不全文搜索命令文字；
讨论/引用/输出这些词不触发；shell 含管道/重定向/串联/嵌套 → 不视为可解析的
永久删除（走普通流程）；无法解析不因「未命中词」而放行（verified=False 或 None）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from agent.security.deletion import DeleteObject, DeleteIntent, detect_delete_intent

PY = sys.executable
WINDOWS = sys.platform == "win32"


def _execution(
    program: str,
    argv=(),
    *,
    path: str | None = None,
    cwd: str | None = None,
    shell: bool = False,
    shell_cmd: str | None = None,
) -> dict:
    return {
        "execution": {
            "kind": "shell" if shell else "program",
            "program_path": path,
            "program_name": program,
            "argv": [str(item) for item in argv],
            "cwd": cwd,
            "shell": shell,
            "shell_cmd": shell_cmd,
        }
    }


def _intent(program: str, argv=(), **kwargs) -> DeleteIntent | None:
    return detect_delete_intent(effect_kind="execute", detail=_execution(program, argv, **kwargs))


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


# --------------------------------------------------------------------------
# 程序 + argv 结构：POSIX 删除工具
# --------------------------------------------------------------------------


def test_rm_flags_and_operands(workspace):
    victim = workspace / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    intent = _intent("rm", ["-rf", str(victim)])
    assert intent is not None and intent.permanent is True
    assert intent.method == "command.delete"
    assert intent.program == "rm"
    assert intent.verified is True
    assert len(intent.objects) == 1
    item = intent.objects[0]
    assert Path(item.path) == victim.resolve()
    assert item.is_directory is False
    assert item.identity, "存在对象的身份必须被解析出来"
    assert "永久删除" in intent.reason


def test_rm_glob_is_unverified_but_still_permanent(workspace):
    intent = _intent("rm", ["-f", "*.txt"], cwd=str(workspace))
    assert intent is not None and intent.permanent is True
    assert intent.verified is False
    assert intent.objects and intent.objects[0].path.endswith("*.txt")


def test_rm_without_operands_is_not_a_delete_intent():
    assert _intent("rm", ["-rf"]) is None
    assert _intent("rm", ["--version"]) is None


def test_rmdir_marks_directory(workspace):
    target = workspace / "old-dir"
    target.mkdir()
    intent = _intent("rmdir", [str(target)])
    assert intent is not None and intent.verified is True
    assert intent.objects[0].is_directory is True
    assert Path(intent.objects[0].path) == target.resolve()


def test_unlink_single_operand(workspace):
    victim = workspace / "one.txt"
    victim.write_text("x", encoding="utf-8")
    assert _intent("unlink", [str(victim)]) is not None
    assert _intent("unlink", ["a", "b"]) is not None
    assert _intent("unlink", [str(victim)]).verified is True
    two = _intent("unlink", ["a", "b"])
    assert two.verified is False


def test_shred_is_permanent(workspace):
    victim = workspace / "secret.txt"
    victim.write_text("secret", encoding="utf-8")
    intent = _intent("shred", ["-u", "-n", "3", str(victim)])
    assert intent is not None and intent.permanent is True
    assert intent.verified is True
    assert Path(intent.objects[0].path) == victim.resolve()


def test_find_delete_is_permanent_but_unverified(workspace):
    intent = _intent("find", [str(workspace), "-name", "*.log", "-delete"])
    assert intent is not None and intent.permanent is True
    assert intent.verified is False
    assert intent.objects == ()
    assert "find" in intent.reason


def test_git_rm_and_clean(workspace):
    victim = workspace / "tracked.txt"
    victim.write_text("x", encoding="utf-8")
    rm = _intent("git", ["rm", "--", str(victim)])
    assert rm is not None and rm.permanent is True
    assert rm.verified is True
    assert Path(rm.objects[0].path) == victim.resolve()
    clean = _intent("git", ["clean", "-fd"])
    assert clean is not None and clean.permanent is True
    assert clean.verified is False
    assert _intent("git", ["clean", "-n"]) is None
    assert _intent("git", ["status"]) is None


def test_discussion_and_grep_text_do_not_trigger():
    # 程序身份不是删除程序：argv 里出现删除命令文字不触发
    assert _intent("git", ["log", "--grep", "rm -rf /tmp/x"]) is None
    assert _intent("echo", ["rm", "-rf", "/tmp/x"]) is None
    assert _intent("python", ["-c", "print('rm -rf /tmp/x')"]) is None


# --------------------------------------------------------------------------
# shell 命令语法
# --------------------------------------------------------------------------


def test_shell_simple_delete_is_parsed(workspace):
    victim = workspace / "shell-victim.txt"
    victim.write_text("x", encoding="utf-8")
    intent = _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd=(
        "del /f /q " + str(victim) if WINDOWS else "rm -f " + str(victim)
    ))
    assert intent is not None and intent.permanent is True
    assert intent.method == "command.delete"
    assert intent.shell_cmd
    assert Path(intent.objects[0].path) == victim.resolve()


@pytest.mark.parametrize(
    "shell_cmd",
    [
        "cat x | rm -f y",
        "echo x > y",
        "rm -f a && echo done",
        "ls; rm -f a",
        "rm -f a" + chr(10) + "echo done",
    ],
)
def test_shell_control_chars_are_not_parseable(shell_cmd):
    assert _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd=shell_cmd) is None


def test_shell_discussion_does_not_trigger():
    assert _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd="echo rm -rf /tmp/x") is None
    assert _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd="echo del x.txt") is None


def test_shell_nested_or_privileged_programs_are_not_parsed():
    assert _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd="powershell -Command Remove-Item x.txt") is None
    assert _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd="sudo rm -f /tmp/x") is None
    assert _intent("cmd" if WINDOWS else "sh", [], shell=True, shell_cmd="python -c import os") is None


def test_shell_expansion_is_unverified(workspace):
    intent = _intent(
        "cmd" if WINDOWS else "sh",
        [],
        shell=True,
        shell_cmd=("del " + chr(37) + "TEMP" + chr(37) + "/x.txt" if WINDOWS else "rm -f $HOME/x"),
    )
    assert intent is not None and intent.permanent is True
    assert intent.verified is False


# --------------------------------------------------------------------------
# 文件工具：默认回收站，不产生 permanent intent
# --------------------------------------------------------------------------


def test_file_tool_default_delete_is_recycle_bin_not_permanent(workspace):
    victim = workspace / "one.txt"
    victim.write_text("x", encoding="utf-8")
    assert detect_delete_intent(effect_kind="delete", detail={"paths": [str(victim)]}) is None
    assert detect_delete_intent(effect_kind="delete", detail={"permanent": False, "path": str(victim)}) is None


def test_file_tool_explicit_permanent_delete_intent(workspace):
    victim = workspace / "one.txt"
    victim.write_text("x", encoding="utf-8")
    intent = detect_delete_intent(
        effect_kind="delete",
        detail={"permanent": True, "objects": [{"path": str(victim)}], "tool": "fs_delete_permanent"},
    )
    assert intent is not None and intent.permanent is True
    assert intent.method == "fs.permanent_delete"
    assert intent.verified is True
    assert Path(intent.objects[0].path) == victim.resolve()
    assert intent.objects[0].identity


def test_non_delete_effects_never_produce_intent():
    del_intent = detect_delete_intent(effect_kind="execute", detail={})
    assert del_intent is None
    assert detect_delete_intent(effect_kind="write", detail={"permanent": True}) is None
    assert detect_delete_intent(effect_kind="read", detail={}) is None


def test_program_identity_requires_actual_delete_program(workspace):
    # 同名替身（未知身份的程序）不由 argv 文字触发：看不懂就不宣称是永久删除
    assert _intent("some-tool", ["rm", "-rf", str(workspace)]) is None
    # Windows 风格 del 参数只有在程序身份是 del/erase/rd/rmdir 时才解析
    intent = _intent("del", ["/f", "/q", "x.txt"], cwd=str(workspace))
    assert intent is not None and intent.permanent is True
    assert intent.verified is True
