"""多文件项目的执行：入口能 import 项目里的兄弟模块，越界路径一律拒绝。

回归的缺口：沙箱只接收一段代码字符串，工具自己写的 `pkg/util.py` 在运行时
根本不存在 —— 稍微拆分的项目一跑就是 ModuleNotFoundError。
"""

from __future__ import annotations

import asyncio

from agent.tools.sandbox import SandboxExecutor

ENTRY = (
    "from pkg.util import double\n"
    "\n"
    "def run(**kwargs):\n"
    "    return {'value': double(kwargs['x'])}\n"
)

PKG_FILES = {"pkg/__init__.py": "", "pkg/util.py": "def double(x):\n    return x * 2\n"}


def _run(coro):
    return asyncio.run(coro)


def _executor() -> SandboxExecutor:
    # 显式选受限子进程：本机是否装了 docker 不该影响这些用例的结论。
    return SandboxExecutor("subprocess")


def test_entry_can_import_a_sibling_module():
    result = _run(_executor().execute(ENTRY, {"x": 21}, files=PKG_FILES))

    assert result.ok, result.diagnostic()
    assert result.value == {"value": 42}


def test_entry_module_supports_relative_imports():
    """真正的包结构（`from .util import ...`）也要能跑 —— 这是多文件项目的常态。"""
    files = dict(PKG_FILES)
    files["pkg/main.py"] = (
        "from .util import double\n"
        "\n"
        "def run(**kwargs):\n"
        "    return {'value': double(kwargs['x'])}\n"
    )

    result = _run(
        _executor().execute("", {"x": 4}, files=files, entry="pkg.main:run")
    )

    assert result.ok, result.diagnostic()
    assert result.value == {"value": 8}


def test_without_files_the_sibling_module_is_not_importable():
    """反证：能 import 是因为项目文件被带进去了，不是环境里恰好有同名模块。"""
    result = _run(_executor().execute(ENTRY, {"x": 21}))

    assert not result.ok
    assert result.category == "missing_dependency"


def test_project_files_may_not_escape_the_project():
    result = _run(
        _executor().execute(
            "def run(**kwargs):\n    return {}\n",
            {},
            files={"../evil.py": "print('nope')"},
        )
    )

    assert not result.ok
    assert "路径" in (result.error or "")


def test_oversized_project_is_rejected_before_running():
    files = {f"mod_{i}.py": "x" * 20_000 for i in range(40)}  # 远超过上限

    result = _run(
        _executor().execute("def run(**kwargs):\n    return {}\n", {}, files=files)
    )

    assert not result.ok
    assert "上限" in (result.error or "")


def test_broken_entry_is_reported_as_a_code_problem():
    files = {"pkg/__init__.py": "", "pkg/main.py": "def other():\n    return 1\n"}

    result = _run(
        _executor().execute("", {}, files=files, entry="pkg.main:run")
    )

    assert not result.ok
    assert result.category == "code_error"
    assert "run" in (result.error or "")
