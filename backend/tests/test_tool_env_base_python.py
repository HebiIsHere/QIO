"""第三阶段：安装版（冻结后端）里「依赖环境用哪个解释器」的解析。

缺陷（安装产物实测）：冻结后 sys.executable 是 qio-backend.exe，而 ToolEnvManager 直接把它当
解释器用 —— `qio-backend.exe -m venv <dir>` 只认 --tool-worker，`-m venv` 被当成未知参数忽略，
于是**又启动一个后端**。真实安装目录里的原始输出：

    WARNING:agent.core.loop:tool dev_run_tests failed: 创建专用环境失败：Traceback (most recent call last):
      File "agent\\config.py", line 32, in default_data_dir
      File "pathlib.py", line 1385, in expanduser
    RuntimeError: Could not determine home directory.

结论：安装版里带第三方依赖的工具一步都走不了。这里钉住四件事：

1. 冻结态**绝不**把 sys.executable 当解释器；
2. QIO_PYTHON 一旦指定就以它为准（版本不匹配 → 明确失败，不静默换别的解释器）；
3. 找不到解释器时给**可行动**的说明（提到需要的 Python 版本与 QIO_PYTHON），不是 traceback，
   而且一个子进程都不许起；
4. 非冻结态行为不变（仍然用后端自己的解释器）。

注意：所有版本号都从 tool_envs._backend_major_minor() 推导，不写死 —— CI 的 windows 任务与
py3.12 任务跑的是 3.12，写死 3.11 会假红（第一版就是这么错的）。
"""

from __future__ import annotations

import sys

from agent.tools import tool_envs

# 当前后端的 major.minor（3.11 或 3.12 都可能）与一个**肯定不同**的版本。
EXPECTED = tool_envs._backend_major_minor()
OTHER = "3.13" if EXPECTED != "3.13" else "3.12"


def _freeze(monkeypatch, exe: str = r"C:\app\qio-backend.exe") -> str:
    """把当前进程伪装成 PyInstaller 冻结产物。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    return exe


def _fake_probe(monkeypatch, versions: dict[str, str]) -> None:
    """把「跑一下候选解释器问版本」换成查表（测试里不去真跑外部进程）。"""
    monkeypatch.setattr(tool_envs, "_probe_python_version", lambda path: versions.get(path))


def test_frozen_mode_never_uses_the_backend_executable(monkeypatch):
    exe = _freeze(monkeypatch)
    good = r"C:\python-match\python.exe"
    _fake_probe(monkeypatch, {good: EXPECTED})
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [good])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == good
    assert resolved != exe, "冻结态把 qio-backend.exe 当解释器会悄悄再起一个后端"
    assert exe not in note


def test_frozen_mode_without_a_matching_interpreter_is_actionable(monkeypatch):
    exe = _freeze(monkeypatch)
    bad = r"C:\python-other\python.exe"
    _fake_probe(monkeypatch, {bad: OTHER})
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [bad])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None
    assert EXPECTED in note and tool_envs.BASE_PYTHON_ENV in note, note
    assert OTHER in note, "要把找到的不匹配版本如实说出来"
    assert exe not in note


def test_the_explicit_interpreter_wins_and_a_mismatch_is_refused(monkeypatch):
    _freeze(monkeypatch)
    good = r"C:\python-match\python.exe"
    bad = r"C:\python-other\python.exe"
    _fake_probe(monkeypatch, {good: EXPECTED, bad: OTHER})
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [good])
    monkeypatch.setenv(tool_envs.BASE_PYTHON_ENV, bad)

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None, "显式指定不匹配时必须失败，不能悄悄换成另一个解释器"
    assert OTHER in note and EXPECTED in note, note


def test_the_explicit_interpreter_is_used_when_it_matches(monkeypatch):
    _freeze(monkeypatch)
    good = r"C:\python-match\python.exe"
    _fake_probe(monkeypatch, {good: EXPECTED})
    monkeypatch.setenv(tool_envs.BASE_PYTHON_ENV, good)

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == good
    assert EXPECTED in note


def test_an_unusable_explicit_interpreter_is_reported(monkeypatch):
    _freeze(monkeypatch)
    monkeypatch.setenv(tool_envs.BASE_PYTHON_ENV, r"C:\nope\python.exe")
    _fake_probe(monkeypatch, {})

    resolved, note = tool_envs._resolve_base_python()

    assert resolved is None
    assert "不能用" in note and r"C:\nope\python.exe" in note


def test_non_frozen_mode_still_uses_the_backend_interpreter(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(tool_envs.BASE_PYTHON_ENV, raising=False)
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [r"C:\python-match\python.exe"])

    resolved, note = tool_envs._resolve_base_python()

    assert resolved == sys.executable
    assert "非冻结态" in note


class _Approvals:
    """最小的审批通道替身：记下问了什么，并同意。"""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict):
        self.asked.append((kind, payload))
        return type("Decision", (), {"decision": "approved"})()


async def test_a_frozen_manager_without_an_interpreter_runs_nothing(monkeypatch, tmp_path):
    _freeze(monkeypatch)
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [])
    calls: list[list[str]] = []

    async def runner(argv, timeout, cwd=None):
        calls.append(list(argv))
        return True, ""

    manager = tool_envs.ToolEnvManager(tmp_path / "tool-envs", runner=runner)
    assert manager.base_python == ""
    approvals = _Approvals()

    # 用户可见的那条路径：先问「是否允许装依赖」，同意之后才走到建环境这一步。
    status = await manager.ensure(["six>=1.16"], approvals=approvals, tool_name="six_probe")

    assert [kind for kind, _ in approvals.asked] == ["dependency_install"]
    assert status.ok is False
    assert EXPECTED in status.reason and tool_envs.BASE_PYTHON_ENV in status.reason, status.reason
    assert calls == [], "没有解释器时一个子进程都不该起（更不能拿 qio-backend.exe 冒充）"


async def test_a_frozen_manager_prepares_with_the_resolved_interpreter(monkeypatch, tmp_path):
    exe = _freeze(monkeypatch)
    good = r"C:\python-match\python.exe"
    _fake_probe(monkeypatch, {good: EXPECTED})
    monkeypatch.setattr(tool_envs, "_candidate_pythons", lambda: [good])
    calls: list[list[str]] = []

    async def runner(argv, timeout, cwd=None):
        calls.append(list(argv))
        return False, "（测试：到这里就够了）"

    manager = tool_envs.ToolEnvManager(tmp_path / "tool-envs", runner=runner)
    assert manager.base_python == good

    status = await manager._prepare(["six>=1.16"], fingerprint="fp")

    assert status.ok is False
    assert calls and calls[0][:3] == [good, "-m", "venv"], calls
    assert all(call[0] != exe for call in calls)


def test_the_install_environment_keeps_home_but_not_business_variables(monkeypatch):
    """子进程不是安全边界：缺 home 只会让工具炸（实测 RuntimeError）；业务/凭据变量仍然不给。"""
    monkeypatch.setenv("USERPROFILE", r"C:\Users\tester")
    monkeypatch.setenv("HOMEDRIVE", "C:")
    monkeypatch.setenv("HOMEPATH", r"\Users\tester")
    monkeypatch.setenv("QIO_DATA_DIR", r"C:\secret-data")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")

    env = tool_envs._clean_env()

    assert env["USERPROFILE"] == r"C:\Users\tester"
    assert env["HOMEDRIVE"] == "C:" and env["HOMEPATH"] == r"\Users\tester"
    assert "QIO_DATA_DIR" not in env
    assert "OPENAI_API_KEY" not in env
    assert "sk-must-not-leak" not in " ".join(env.values())
