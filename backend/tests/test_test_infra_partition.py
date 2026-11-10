"""测试基础设施自测：serial / not-serial 分区与 xdist 收集完整性。

证明的是入口脚本与分区脚本的**机器可校验**不变量：
* scripts/test_partition_check.py 报并集 = 全量、交集 = 空（不是靠自觉）；
* conftest 里的串行清单每条都真的匹配到被收集的 serial 用例（没有静默失效）；
* 带 -n 的 xdist 收集与单进程收集得到完全相同的 nodeid 集合（没有静默排除测试）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
PARTITION_SCRIPT = REPO_ROOT / "scripts" / "test_partition_check.py"


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(name, None)
    return env


def _collect(marker: str | None, extra: list[str] | None = None) -> set[str]:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-o",
        "addopts=",
        "--no-header",
    ]
    if marker:
        cmd += ["-m", marker]
    cmd += extra or []
    completed = subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_clean_env(),
    )
    assert completed.returncode == 0, (
        f"collect failed: marker={marker!r} extra={extra!r}"
        + completed.stdout[-2000:]
        + completed.stderr[-2000:]
    )
    nodeids: set[str] = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line and "::" in line and not line.startswith(("=", "FAILED", "ERROR")):
            nodeids.add(line)
    return nodeids


def test_partition_script_reports_complete_partition():
    """并集 = 全量、交集 = 空；计数清单必须可解析且非空。"""
    completed = subprocess.run(
        [sys.executable, str(PARTITION_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_clean_env(),
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"分区校验失败：" + output[-3000:]
    assert "RESULT OK" in output
    counts = {
        name: int(value)
        for name, value in re.findall(
            r"(全量 selected|并行组|串行组|并集|交集)\s*=\s*(\d+)", output
        )
    }
    assert set(counts) == {"全量 selected", "并行组", "串行组", "并集", "交集"}, output
    assert counts["并行组"] > 0 and counts["串行组"] > 0
    assert counts["并集"] == counts["全量 selected"], output
    assert counts["交集"] == 0, output


def test_serial_registry_entries_are_effective():
    """conftest 的串行清单（文件级 / 用例级）每一条都真的匹配到 serial 收集结果。"""
    import conftest

    serial = _collect("serial")
    assert serial, "没有任何 serial 用例：串行清单没有生效"
    for module, reason in conftest.SERIAL_MODULES.items():
        assert reason.strip(), f"{module} 缺少串行原因"
        prefix = f"tests/{module}::"
        assert any(nodeid.startswith(prefix) for nodeid in serial), (
            f"串行清单里的 {module} 没有匹配到任何被收集的 serial 用例（清单失效）"
        )
    for node_prefix, reason in conftest.SERIAL_NODEIDS.items():
        assert reason.strip(), f"{node_prefix} 缺少串行原因"
        full_prefix = f"tests/{node_prefix}"
        assert any(
            nodeid == full_prefix or nodeid.startswith(full_prefix + "[")
            for nodeid in serial
        ), f"串行清单里的 {node_prefix} 没有匹配到 serial 用例（清单失效）"


def test_xdist_collection_does_not_drop_or_add_tests():
    """xdist 收集与单进程收集的 nodeid 集合必须完全一致（新并行配置不得静默排除测试）。"""
    single = _collect(None)
    parallel = _collect(None, ["-n", "2"])
    missing = single - parallel
    added = parallel - single
    assert not missing and not added, (
        f"xdist 收集不一致：missing={len(missing)} added={len(added)} "
        f"示例={sorted(missing or added)[:5]}"
    )
    assert len(parallel) > 0
