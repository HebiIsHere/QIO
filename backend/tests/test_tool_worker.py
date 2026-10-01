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


# ---------- 编码契约：通道编码由协议钉死，不跟随子进程 locale ----------

# GitHub 的 windows-latest、英文 Windows 用户机都是 cp1252：中文无法用它编码。
# 本机的开发机是中文 Windows（cp936），缺陷不会自然复现，所以显式构造 cp1252 子进程。
_NON_UTF8_ENV = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}


def _run_under(env_overrides: dict, payload: object, *, raw: bytes | None = None):
    """在一个显式指定编码的子进程里跑 worker（真子进程，不是 mock）。"""
    import os

    env = dict(os.environ)
    env.update(env_overrides)
    body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run(
        [sys.executable, str(WORKER)],
        input=body,
        capture_output=True,
        env=env,
        timeout=60.0,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_worker_result_channel_is_utf8_even_under_cp1252():
    """英文 Windows 上以前会在这里直接崩掉：结果里有中文就抛 UnicodeEncodeError，
    进程死在结果通道上，父进程只看到「标准输出为空」。"""
    code, out, err = _run_under(_NON_UTF8_ENV, {"code": "def run(**kwargs):\n    return 42\n", "arguments": {}})
    assert code == 0, f"worker 在 cp1252 下异常退出：{err!r}"
    result = _result(out.decode("utf-8"))  # 协议是 UTF-8 字节，与子进程 locale 无关
    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
    assert "对象" in result["error"]


def test_worker_missing_entry_error_survives_cp1252():
    code, out, _ = _run_under(_NON_UTF8_ENV, {"code": "x = 1\n", "arguments": {}})
    assert code == 0
    result = _result(out.decode("utf-8"))
    assert result["ok"] is False
    assert "run" in result["error"]


def test_worker_error_channel_is_utf8_not_backslash_escapes_under_cp1252():
    """stderr 以前被 backslashreplace 写成字面 \\u5de5\\u5177…，读的人看到的是转义。"""
    code, _out, err = _run_under(_NON_UTF8_ENV, {}, raw=b"{not json")
    assert code != 0
    text = err.decode("utf-8")
    assert "请求" in text
    assert "\\u5de5" not in text


def test_worker_round_trips_non_ascii_arguments():
    """请求体也是 UTF-8 字节：中文参数不能被按 locale 解释成乱码。"""
    code, out, _ = _run(
        {
            "code": "def run(**kwargs):\n    return {'echo': kwargs['text']}\n",
            "arguments": {"text": "中文参数 ✅"},
        }
    )
    assert code == 0
    result = _result(out)
    assert result["value"] == {"echo": "中文参数 ✅"}


def test_worker_round_trips_non_ascii_arguments_under_cp1252():
    code, out, _ = _run_under(
        _NON_UTF8_ENV,
        {
            "code": "def run(**kwargs):\n    return {'echo': kwargs['text']}\n",
            "arguments": {"text": "中文参数"},
        },
    )
    assert code == 0
    result = _result(out.decode("utf-8"))
    assert result["value"] == {"echo": "中文参数"}


def test_worker_result_line_stays_single_line_with_embedded_newlines():
    """工具输出里的换行必须进 JSON 转义，结果通道仍然只有一行。"""
    code, out, _ = _run(
        {
            "code": "def run(**kwargs):\n    print('a\\nb')\n    return {'ok': 1}\n",
            "arguments": {},
        }
    )
    assert code == 0
    result = _result(out)
    assert result["stdout"] == "a\nb\n"
