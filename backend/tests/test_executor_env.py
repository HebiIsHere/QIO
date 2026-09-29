"""工具执行命令的解析。

冻结态用后端 exe 的 `--tool-worker` 模式，开发态用同一个 worker 脚本；两者的协议
一致（stdin 结构化请求 / stdout 单行结果），所以解析结果只有「用哪个程序 + 前置
参数」的差别。
"""

from __future__ import annotations

import sys

import pytest

from agent.tools import executor_env


def test_dev_mode_uses_current_interpreter_and_worker_script(monkeypatch):
    monkeypatch.setattr(executor_env.sys, "frozen", False, raising=False)
    spec = executor_env.resolve_tool_executor()
    assert spec.kind == "worker-script"
    assert spec.argv[0] == sys.executable
    assert spec.argv[1].endswith("tool_worker.py")


def test_frozen_mode_uses_backend_exe_worker_flag(tmp_path, monkeypatch):
    exe = tmp_path / "qio-backend.exe"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(executor_env.sys, "executable", str(exe))
    monkeypatch.setattr(executor_env.sys, "frozen", True, raising=False)
    spec = executor_env.resolve_tool_executor()
    assert spec.kind == "worker-exe"
    assert spec.argv == [str(exe), "--tool-worker"]


def test_missing_worker_script_reports_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_env.sys, "frozen", False, raising=False)
    monkeypatch.setattr(executor_env, "_worker_script_path", lambda: tmp_path / "nope.py")
    with pytest.raises(executor_env.ToolRuntimeUnavailable) as exc:
        executor_env.resolve_tool_executor()
    assert "worker" in str(exc.value)
