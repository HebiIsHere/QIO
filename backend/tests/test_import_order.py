"""导入顺序：先导入 agent.tools.* 不能炸（循环导入回归）。

回归的缺陷：`agent.tools` → lifecycle → registry → `agent.core.narrative` →
`agent.core`（包初始化）→ `agent.core.loop` → `agent.tools.registry`（此时还没初始化
完）围成一个圈。谁先被导入决定成败：先拉 `agent.core` 就没事，先碰 `agent.tools.*`
就以 ImportError 收场 —— 症状是「单独跑某一个测试文件」看到的是导入错误，而不是真正
的断言结果；只有整包一起跑（先被收集的文件恰好先拉了 agent.core）才正常。

这里必须在**新解释器**里 import：进程内的导入顺序在 pytest 里早被别的测试定下了，
同一个进程里再也测不出这个性质。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND_DIR / "src"),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_tools_can_be_imported_before_core():
    """先导入 agent.tools.* —— 单独跑某个测试文件时就是这种顺序。"""
    proc = _import_in_fresh_interpreter("agent.tools.sandbox")
    assert proc.returncode == 0, proc.stderr


def test_registry_can_be_imported_first():
    """最先被导入的就是循环里那个模块，最容易炸。"""
    proc = _import_in_fresh_interpreter("agent.tools.registry")
    assert proc.returncode == 0, proc.stderr


def test_core_can_still_be_imported_first():
    """反向顺序本来就没问题，剪圈时不能把它弄坏。"""
    proc = _import_in_fresh_interpreter("agent.core")
    assert proc.returncode == 0, proc.stderr
