"""导入冒烟：**每一个**模块都必须能作为第一个 import 独立成立。

为什么不能只测三个入口（见 test_import_order.py）：循环导入是「谁先被导入决定
成败」的隐藏缺陷。仓库里已知的历史环是
`agent.tools.registry → agent.core.loop → agent.tools.registry`：先拉 `agent.core`
就没事，先碰 `agent.tools.*` 就以 ImportError 收场。整套测试一起跑时顺序恰好成立，
单独跑某个测试文件才会炸 —— 这种缺陷必须由「每个模块单独起进程 import」来抓。

这里对 `src/agent` 下每一个模块各起一个**新解释器**，只 import 它自己：
* 新解释器是必须的：同一个 pytest 进程里的导入顺序早被别的测试定下了；
* 覆盖全部模块是必须的：只测 3 个入口，别处新长出来的环照样漏。

注：只断言「import 成功」这一条真实契约，不做 allowlist —— 一旦有模块需要
「先导入别的包」才能成立，这个测试就必须失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
SRC = BACKEND_DIR / "src"
IMPORT_TIMEOUT_SECONDS = 300
MAX_WORKERS = 8


def _module_names() -> list[str]:
    names: list[str] = []
    for path in sorted((SRC / "agent").rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    return names


MODULES = _module_names()


def _import_first(module: str) -> tuple[str, subprocess.CompletedProcess[str]]:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=IMPORT_TIMEOUT_SECONDS,
    )
    return module, proc


def test_the_module_list_actually_covers_the_tree():
    """清单本身不能缩水：目录里有文件，就必须有同名条目。"""
    assert len(MODULES) >= 100, len(MODULES)
    for required in (
        "agent.core",
        "agent.core.loop",
        "agent.tools.registry",
        "agent.tools.sandbox",
        "agent.services.retrieval",
        "agent.services.app",
        "agent.trace.store",
        "agent.memory.fragment",
    ):
        assert required in MODULES, required


def test_every_module_can_be_the_first_import():
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_import_first, MODULES))

    failures = []
    for module, proc in results:
        if proc.returncode == 0:
            continue
        tail = [
            line
            for line in (proc.stderr or "").strip().splitlines()
            if line.strip() and not line.startswith(" ")
        ]
        failures.append({"module": module, "error": " | ".join(tail[-4:])})
    assert not failures, failures


def test_core_is_not_required_before_tools():
    """反向顺序（先 core）一直是好的，剪环时不能把它弄坏。"""
    proc = subprocess.run(
        [sys.executable, "-c", "import agent.core; import agent.tools.registry"],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=IMPORT_TIMEOUT_SECONDS,
    )
    assert proc.returncode == 0, proc.stderr
