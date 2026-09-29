"""入口分流：worker 模式必须在加载后端服务 / 数据库 / 凭据之前就返回。

用真子进程验证，因为「有没有 import uvicorn」「有没有建数据库」是进程级事实。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src"
MAIN = SRC / "agent" / "main.py"


def _env(**extra: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def test_importing_entrypoint_does_not_load_service_stack():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, agent.main; "
            "print('uvicorn' in sys.modules, 'fastapi' in sys.modules, "
            "'agent.api.server' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(),
        timeout=120.0,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False False False"


def test_tool_worker_mode_runs_without_starting_backend(tmp_path):
    data_dir = tmp_path / "qio-data"
    payload = json.dumps({
        "code": "def run(**kwargs):\n    return {'echo': kwargs['x']}\n",
        "arguments": {"x": 7},
    })
    proc = subprocess.run(
        [sys.executable, str(MAIN), "--tool-worker"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_env(QIO_DATA_DIR=str(data_dir)),
        timeout=120.0,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"] is True
    assert result["value"] == {"echo": 7}
    # worker 模式不得建数据库（那是服务启动才会做的事）
    assert not (data_dir / "app.db").exists()
