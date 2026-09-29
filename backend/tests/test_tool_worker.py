"""工具 worker：结构化协议（stdin 请求 / stdout 单行结果）。

这些用例直接以真子进程运行 worker，不用 mock —— 协议是给进程间用的，
只有真的跨进程跑一次才能证明它成立。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1] / "src" / "agent" / "tool_worker.py"


def _run(payload: dict, *, extra_args: list[str] | None = None, timeout: float = 60.0):
    """跑一次 worker，返回 (returncode, stdout_text, stderr_text)。"""
    proc = subprocess.run(
        [sys.executable, str(WORKER), *(extra_args or [])],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _result(stdout: str) -> dict:
    """结果通道只允许一行 JSON。"""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout 不是单行 JSON：{stdout!r}"
    return json.loads(lines[0])


def test_worker_returns_value():
    code, out, _ = _run({
        "code": "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}\n",
        "arguments": {"a": 2, "b": 3},
    })
    assert code == 0
    result = _result(out)
    assert result["ok"] is True
    assert result["value"] == {"sum": 5}


def test_worker_reports_error_type_and_keeps_result_channel_clean():
    code, out, _ = _run({
        "code": "def run(**kwargs):\n    raise ValueError('kaboom')\n",
        "arguments": {},
    })
    result = _result(out)
    assert result["ok"] is False
    assert result["error_type"] == "ValueError"
    assert "kaboom" in result["error"]
    assert "ValueError" in result["stderr"]


def test_worker_captures_tool_output_instead_of_leaking_it():
    code, out, _ = _run({
        "code": (
            "import sys\n"
            "def run(**kwargs):\n"
            "    print('hello from tool')\n"
            "    print('to stderr', file=sys.stderr)\n"
            "    return {'ok': True}\n"
        ),
        "arguments": {},
    })
    result = _result(out)  # 工具的输出不能污染结果通道
    assert result["ok"] is True
    assert "hello from tool" in result["stdout"]
    assert "to stderr" in result["stderr"]


def test_worker_rejects_non_dict_result():
    code, out, _ = _run({
        "code": "def run(**kwargs):\n    return 42\n",
        "arguments": {},
    })
    result = _result(out)
    assert result["ok"] is False
    assert "对象" in result["error"]


def test_worker_rejects_unserializable_result():
    code, out, _ = _run({
        "code": "def run(**kwargs):\n    return {'x': object()}\n",
        "arguments": {},
    })
    result = _result(out)
    assert result["ok"] is False


def test_worker_reports_missing_run_function():
    code, out, _ = _run({"code": "x = 1\n", "arguments": {}})
    result = _result(out)
    assert result["ok"] is False
    assert "run" in result["error"]


def test_worker_ignores_unknown_leading_flags():
    """冻结入口会把 `--tool-worker` 一起传进来：worker 必须能接受它。"""
    code, out, _ = _run(
        {"code": "def run(**kwargs):\n    return {'ok': 1}\n", "arguments": {}},
        extra_args=["--tool-worker"],
    )
    assert code == 0
    assert _result(out)["ok"] is True


def test_worker_rejects_malformed_request():
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input="{not json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60.0,
    )
    assert proc.returncode != 0
    assert "请求" in proc.stderr
