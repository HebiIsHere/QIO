"""P4-B：安装包自带 Python 运行时的解析（这条优先级对冻结态与开发态是同一套）。

背景（计划 docs/p4-plan.md 第 2 节）：安装包把解释器随包带上（resources/python-runtime），
外壳启动时把目录通过 QIO_BUNDLED_PYTHON_DIR 交给后端。解析顺序：

    QIO_PYTHON（显式；不匹配 → 明确失败）
      → QIO_BUNDLED_PYTHON_DIR（自带运行时；目录 / 解释器 / 版本任一不对 → 明确失败，不换别的）
      → 冻结态：py -0p → PATH（机器上装的，版本必须匹配）
      → 非冻结态：后端自己的解释器

这里钉住四件事：

1. 显式指定最大：哪怕自带运行时可用，也以 QIO_PYTHON 为准；
2. 自带运行时命中时就用它（冻结态与非冻结态一致），说明文案要写清「安装包自带运行时」；
3. 自带运行时不匹配 / 缺解释器 / 跑不起来 → **明确失败**，不许退回机器上的 Python ——
   那会把「包装坏了」伪装成「这台机器恰好能跑」；同样地，自带不匹配时也不许拿
   机器上那个匹配的顶上；
4. 两个都没有时，仍然是原来那条**可行动**的失败文案（提到需要的版本、QIO_PYTHON，
   并说清外壳本该通过 QIO_BUNDLED_PYTHON_DIR 把自带运行时传进来）。

测试不依赖本机真的装了哪个 Python，也不联网：解释器目录用 tmp_path 造假，版本探测
（_probe_python_version）按既有口径换成本地表（与 test_tool_env_base_python.py 一致）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agent.tools import tool_envs

# 当前后端的 major.minor（本机 3.11，CI 的 py3.12 任务会是 3.12；不要写死）与一个肯定不同的版本。
EXPECTED = tool_envs._backend_major_minor()
OTHER = "3.13" if EXPECTED != "3.13" else "3.12"


def _freeze(monkeypatch, exe: str = r"C:\app\qio-backend.exe") -> str:
    """把当前进程伪装成 PyInstaller 冻结产物（sys.executable 是 qio-backend.exe）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    return exe


def _fake_probe(monkeypatch, versions: dict[str, str]) -> None:
    """把「跑一下候选解释器问版本」换成查表（测试里不去真跑外部进程）。"""
    monkeypatch.setattr(tool_envs, "_probe_python_version", lambda path: versions.get(path))


def _bundled_exe(directory: Path) -> str:
    """自带运行时目录里解释器的路径（与实现同一套命名）。"""
    name = "python.exe" if os.name == "nt" else "bin/python3"
    return str(directory / name)


def _bundled_tree(tmp_path: Path, *, name: str = "python-runtime") -> Path:
    """造一份假的自带运行时目录：只要解释器是个文件（真版本由探测表回答）。"""
    directory = tmp_path / name
    exe = directory / ("python.exe" if os.name == "nt" else "bin/python3")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n# 假解释器：版本由测试里的探测表回答\n", encoding="utf-8")
    return directory


def _use_bundled(monkeypatch, directory: Path) -> str:
    monkeypatch.setenv(tool_envs.BUNDLED_PYTHON_ENV, str(directory))
    return _bundled_exe(directory)


# -- 自带运行时被选中 -------------------------------------------------------


def test_frozen_mode_uses_the_bundled_runtime(monkeypatch, tmp_path):
    """冻结态：外壳把自带运行时交进来，就用它 —— 哪怕机器上也有一个匹配的。"""
    _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    _fake_probe(monkeypatch, {bundled: EXPECTED})
    machine = r"C:\python-match\python.exe"
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [machine])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == bundled
    assert "自带运行时" in note and EXPECTED in note, note
    assert machine not in note, "选了自带运行时就不该再去列机器上的候选"


def test_non_frozen_mode_also_prefers_the_bundled_runtime(monkeypatch, tmp_path):
    """开发态同样以自带运行时为准（外壳只要把目录交进来）：两种形态一条链。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(tool_envs.BASE_PYTHON_ENV, raising=False)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    _fake_probe(monkeypatch, {bundled: EXPECTED})

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == bundled
    assert resolved != sys.executable, "交进来的自带运行时优先于后端自己的解释器"
    assert "自带运行时" in note, note


def test_without_the_env_var_nothing_changes_in_non_frozen_mode(monkeypatch):
    """外壳没传（普通开发/测试）：还是后端自己的解释器 —— 行为一行不变。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(tool_envs.BASE_PYTHON_ENV, raising=False)
    monkeypatch.delenv(tool_envs.BUNDLED_PYTHON_ENV, raising=False)

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == sys.executable
    assert "非冻结态" in note, note


# -- 显式指定仍然最大 -------------------------------------------------------


def test_the_explicit_interpreter_wins_over_the_bundled_runtime(monkeypatch, tmp_path):
    _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    explicit = r"C:\python-explicit\python.exe"
    _fake_probe(monkeypatch, {bundled: EXPECTED, explicit: EXPECTED})
    monkeypatch.setenv(tool_envs.BASE_PYTHON_ENV, explicit)

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == explicit
    assert "显式指定" in note, note
    assert bundled not in note


def test_a_mismatched_explicit_interpreter_is_refused_even_with_a_bundled_runtime(monkeypatch, tmp_path):
    _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    explicit = r"C:\python-explicit\python.exe"
    _fake_probe(monkeypatch, {bundled: EXPECTED, explicit: OTHER})
    monkeypatch.setenv(tool_envs.BASE_PYTHON_ENV, explicit)

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None, "显式指定不匹配时必须失败，不能悄悄换成自带运行时"
    assert OTHER in note and EXPECTED in note, note
    assert bundled not in note


# -- 自带运行时的坏法都要明确失败（不静默换别的） ---------------------------


def test_a_mismatched_bundled_runtime_fails_instead_of_falling_back(monkeypatch, tmp_path):
    """自带运行时版本不对 = 安装包构建缺陷：明确失败，机器上那个匹配的也不许顶上。"""
    _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    _fake_probe(monkeypatch, {bundled: OTHER})
    machine = r"C:\python-match\python.exe"
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [machine])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None
    assert OTHER in note and EXPECTED in note, note
    assert tool_envs.BASE_PYTHON_ENV in note, "要说清退路是显式指定，而不是自动换一个"
    assert machine not in note


def test_a_bundled_directory_without_an_interpreter_is_reported(monkeypatch, tmp_path):
    _freeze(monkeypatch)
    empty = tmp_path / "python-runtime"
    empty.mkdir()
    monkeypatch.setenv(tool_envs.BUNDLED_PYTHON_ENV, str(empty))
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [r"C:\python-match\python.exe"])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None
    assert str(empty) in note, note
    assert tool_envs.BASE_PYTHON_ENV in note, note


def test_a_broken_bundled_interpreter_is_reported(monkeypatch, tmp_path):
    _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    _fake_probe(monkeypatch, {})  # 探测不到版本 = 跑不起来

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None
    assert bundled in note and "跑不起来" in note, note
    assert tool_envs.BASE_PYTHON_ENV in note, note


def test_without_any_runtime_the_message_stays_actionable(monkeypatch):
    """安装包里连自带运行时都没有（旧包 / 构建漏了）：原来那条可行动的失败一字不少。"""
    exe = _freeze(monkeypatch)
    monkeypatch.delenv(tool_envs.BASE_PYTHON_ENV, raising=False)
    monkeypatch.delenv(tool_envs.BUNDLED_PYTHON_ENV, raising=False)
    bad = r"C:\python-other\python.exe"
    _fake_probe(monkeypatch, {bad: OTHER})
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [bad])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None
    assert EXPECTED in note and tool_envs.BASE_PYTHON_ENV in note, note
    assert OTHER in note, "要把找到的不匹配版本如实说出来"
    assert tool_envs.BUNDLED_PYTHON_ENV in note, "也要说清自带运行时本该由外壳传进来"
    assert exe not in note


# -- 目录形状（Windows / POSIX） -------------------------------------------


def test_the_names_follow_the_platform():
    """目录里去找哪个名字由平台决定：Windows 是 python.exe，POSIX 是 bin/python3。"""
    names = tool_envs._bundled_interpreter_names()

    if os.name == "nt":
        assert names == ("python.exe",)
    else:
        assert names[0] == "bin/python3"


def test_the_interpreter_is_looked_up_in_the_posix_layout(monkeypatch, tmp_path):
    """POSIX 侧是 bin/python3（构建脚本在 POSIX 上铺出来的形状）。

    这里只把「找哪几个相对路径」换掉，不去动全局 os.name（改 os.name 会让 pathlib 在
    测试结束时的清理里炸成 PosixPath —— 实测踩到过）。
    """
    directory = tmp_path / "python-runtime"
    (directory / "bin").mkdir(parents=True)
    (directory / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(tool_envs, "_bundled_interpreter_names", lambda: ("bin/python3", "bin/python"))

    assert tool_envs._bundled_interpreter(str(directory)) == str(directory / "bin" / "python3")


def test_the_windows_layout_is_looked_up_too(monkeypatch, tmp_path):
    """Windows 布局单独造（别用 _bundled_tree：它在 POSIX 上铺的是 bin/python3，CI 会假红）。"""
    directory = tmp_path / "python-runtime"
    directory.mkdir()
    (directory / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(tool_envs, "_bundled_interpreter_names", lambda: ("python.exe",))

    assert tool_envs._bundled_interpreter(str(directory)) == str(directory / "python.exe")


def test_an_empty_env_var_means_no_bundled_runtime(monkeypatch):
    monkeypatch.setenv(tool_envs.BUNDLED_PYTHON_ENV, "   ")

    assert tool_envs._bundled_python_dir() is None


# -- 管理器：真的会用自带运行时去建环境 -------------------------------------


async def test_a_frozen_manager_prepares_with_the_bundled_runtime(monkeypatch, tmp_path):
    exe = _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    _fake_probe(monkeypatch, {bundled: EXPECTED})
    calls: list[list[str]] = []

    async def runner(argv, timeout, cwd=None):
        calls.append(list(argv))
        return False, "（测试：到这里就够了）"

    manager = tool_envs.ToolEnvManager(tmp_path / "tool-envs", runner=runner)

    assert manager.base_python == bundled
    assert manager.base_python_problem is None
    assert "自带运行时" in manager.base_python_note

    status = await manager._prepare(["six>=1.16"], fingerprint="fp")

    assert status.ok is False
    assert calls and calls[0][:3] == [bundled, "-m", "venv"], calls
    assert all(call[0] != exe for call in calls), "冻结后端自己绝不能当解释器"


async def test_a_broken_bundled_runtime_runs_nothing(monkeypatch, tmp_path):
    """自带运行时坏了：在「是否允许装依赖」之后立刻明确失败，一个子进程都不许起。"""
    _freeze(monkeypatch)
    directory = _bundled_tree(tmp_path)
    bundled = _use_bundled(monkeypatch, directory)
    _fake_probe(monkeypatch, {bundled: OTHER})
    calls: list[list[str]] = []

    async def runner(argv, timeout, cwd=None):
        calls.append(list(argv))
        return True, ""

    manager = tool_envs.ToolEnvManager(tmp_path / "tool-envs", runner=runner)
    approvals = _Approvals()

    status = await manager.ensure(["six>=1.16"], approvals=approvals, tool_name="six_probe")

    assert [kind for kind, _ in approvals.asked] == ["dependency_install"]
    assert status.ok is False
    assert EXPECTED in (status.reason or "") and OTHER in (status.reason or ""), status.reason
    assert calls == [], "自带运行时不匹配时不该起任何子进程"


class _Approvals:
    """最小的审批通道替身：记下问了什么，并同意。"""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict):
        self.asked.append((kind, payload))
        return type("Decision", (), {"decision": "approved"})()
