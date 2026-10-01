"""项目级依赖环境：按需准备专用 Python，缺依赖不再只是「报错了事」。

回归的缺口：声明了第三方依赖也没有任何地方能把它装上 —— 工具永远跑不起来，
用户还得自己去猜要装什么、装到哪。这里给每个「依赖集合」准备一个 QIO 管理的
专用环境，并且**不静默回落到随包解释器**（那等于假装依赖装上了）。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from agent.tools.tool_envs import ToolEnvManager


class _FakeRunner:
    """记录命令行、按脚本返回结果的安装器替身（测试里不真的建 venv / 联网）。"""

    def __init__(self, results: list[tuple[bool, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results or [])

    async def __call__(self, argv: list[str], timeout: float, cwd=None):
        self.calls.append(list(argv))
        # 假装真的建好了环境：把解释器文件写出来，否则「就绪」检查不该通过。
        if len(argv) >= 4 and argv[1:3] == ["-m", "venv"]:
            target = Path(argv[3])
            name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_text("", encoding="utf-8")
        if self._results:
            return self._results.pop(0)
        return True, ""


class _Approvals:
    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision)


def _manager(tmp_path, runner) -> ToolEnvManager:
    return ToolEnvManager(tmp_path / "envs", base_python="C:/python.exe", runner=runner)


def test_no_requirements_needs_no_environment(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure([]))

    assert status.ok
    assert status.interpreter is None
    assert runner.calls == []


def test_declared_dependencies_are_not_ready_before_preparing(tmp_path):
    manager = _manager(tmp_path, _FakeRunner())

    assert manager.is_ready(["requests"]) is False
    assert manager.status_for(["requests"]).ok is False


def test_prepare_asks_the_user_and_installs_only_declared_packages(tmp_path):
    runner = _FakeRunner()
    approvals = _Approvals()
    manager = _manager(tmp_path, runner)

    status = asyncio.run(
        manager.ensure(["requests>=2.31"], approvals=approvals, tool_name="weather")
    )

    assert status.ok, status.reason
    assert status.interpreter
    assert status.interpreter.replace("\\", "/").endswith(manager.interpreter_name)
    # 问过用户，而且把要装的东西写清了
    assert approvals.requests
    assert approvals.requests[0][0] == "dependency_install"
    assert "requests>=2.31" in approvals.requests[0][1]["packages"]
    # 创建环境 + 安装依赖，两步都只针对声明的包
    assert runner.calls[0][:3] == ["C:/python.exe", "-m", "venv"]
    install = runner.calls[1]
    assert install[-1] == "requests>=2.31"
    assert "-m" in install and "pip" in install


def test_a_refusal_creates_nothing(tmp_path):
    runner = _FakeRunner()
    approvals = _Approvals(decision="rejected")
    manager = _manager(tmp_path, runner)

    status = asyncio.run(
        manager.ensure(["requests"], approvals=approvals, tool_name="weather")
    )

    assert status.ok is False
    assert "没有同意" in (status.reason or "")
    assert runner.calls == []
    assert not (tmp_path / "envs").exists() or not manager.is_ready(["requests"])


def test_a_ready_environment_is_reused_without_installing_again(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    asyncio.run(manager.ensure(["requests"], approvals=_Approvals()))
    calls_after_first = len(runner.calls)

    again = asyncio.run(manager.ensure(["requests"], approvals=_Approvals()))

    assert again.ok and again.reused is True
    assert len(runner.calls) == calls_after_first


def test_the_same_dependency_set_maps_to_one_environment(tmp_path):
    manager = _manager(tmp_path, _FakeRunner())

    assert manager.key_for(["b", "a"]) == manager.key_for(["a", "b"])
    assert manager.key_for(["a"]) != manager.key_for(["a", "b"])


def test_a_failed_install_is_reported_and_not_remembered_as_ready(tmp_path):
    runner = _FakeRunner(results=[(True, ""), (False, "ERROR: 网络不通")])
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure(["requests"], approvals=_Approvals()))

    assert status.ok is False
    assert "网络不通" in (status.reason or "")
    # 失败不写记录：下次还会重新尝试（而不是永远卡在「已就绪」的假象里）
    assert manager.is_ready(["requests"]) is False
