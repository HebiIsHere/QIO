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


def test_frozen_mode_never_uses_the_exe_as_a_python_interpreter(tmp_path, monkeypatch):
    """冻结后的 backend exe **不是** python 解释器：只能走 --tool-worker 分流。

    `qio-backend.exe -c "…"` 会去启动后端服务（main.py 的分流只认 --tool-worker），
    所以解析结果里绝不能出现 -c / -m 这类解释器参数。
    """
    exe = tmp_path / "qio-backend.exe"
    exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(executor_env.sys, "executable", str(exe))
    monkeypatch.setattr(executor_env.sys, "frozen", True, raising=False)
    spec = executor_env.resolve_tool_executor()
    assert spec.kind == "worker-exe"
    assert spec.argv == [str(exe), "--tool-worker"]
    assert "-c" not in spec.argv and "-m" not in spec.argv


def test_frozen_mode_refuses_the_dev_only_escape_hatches(tmp_path, monkeypatch):
    """QIO_TOOL_PYTHON 是开发态逃生口：冻结环境里没有 worker 源码，必须如实拒绝。"""
    exe = tmp_path / "qio-backend.exe"
    exe.write_text("x", encoding="utf-8")
    interpreter = tmp_path / "python.exe"
    interpreter.write_text("x", encoding="utf-8")
    monkeypatch.setattr(executor_env.sys, "executable", str(exe))
    monkeypatch.setattr(executor_env.sys, "frozen", True, raising=False)
    monkeypatch.setattr(executor_env, "_worker_script_path", lambda: tmp_path / "nope.py")
    monkeypatch.setenv("QIO_TOOL_PYTHON", str(interpreter))
    with pytest.raises(executor_env.ToolRuntimeUnavailable) as exc:
        executor_env.resolve_tool_executor()
    assert "冻结" in str(exc.value)


def test_worker_source_is_the_current_source_in_dev_mode(monkeypatch):
    monkeypatch.setattr(executor_env.sys, "frozen", False, raising=False)
    monkeypatch.delattr(executor_env.sys, "_MEIPASS", raising=False)
    source = executor_env.worker_source()
    assert "PROTOCOL_ENCODING" in source
    assert "def main(" in source


def test_worker_source_prefers_the_frozen_bundle_resource(tmp_path, monkeypatch):
    """冻结产物把 worker 源码放进包内资源：容器执行时读的是那一份。"""
    bundle = tmp_path / "bundle"
    resource = bundle / "agent" / "tool_worker.py"
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("# frozen copy\n", encoding="utf-8")
    monkeypatch.setattr(executor_env.sys, "_MEIPASS", str(bundle), raising=False)
    assert executor_env.worker_source() == "# frozen copy\n"


def test_worker_source_reports_clearly_when_it_is_missing(tmp_path, monkeypatch):
    monkeypatch.delattr(executor_env.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(executor_env, "_worker_script_path", lambda: tmp_path / "nope.py")
    with pytest.raises(executor_env.ToolRuntimeUnavailable) as exc:
        executor_env.worker_source()
    assert "worker 源码" in str(exc.value)


def test_missing_worker_script_reports_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(executor_env.sys, "frozen", False, raising=False)
    monkeypatch.setattr(executor_env, "_worker_script_path", lambda: tmp_path / "nope.py")
    with pytest.raises(executor_env.ToolRuntimeUnavailable) as exc:
        executor_env.resolve_tool_executor()
    assert "worker" in str(exc.value)
