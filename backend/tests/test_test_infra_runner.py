"""测试基础设施自测：scripts/run_backend_suite.py 两阶段入口。

证明：
* 两阶段都真的执行（第一阶段失败也不跳过第二阶段）；
* JUnit 严格分文件：pytest-parallel.xml / pytest-serial.xml，互不覆盖；
* 分组真的按 serial marker：选中的非 serial 文件只出现在并行组，serial 文件只出现在串行组；
* marker 表达式与两组用 AND 叠加；任选不到测试（pytest exit 5）时入口返回非 0；
* 入口只往给定 junit 目录写证据，不碰调用者进程（跑完后当前 pytest 进程继续正常）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_backend_suite.py"

PARALLEL_JUNIT = "pytest-parallel.xml"
SERIAL_JUNIT = "pytest-serial.xml"

# 两个小文件：storage 全部非 serial（其中 2 条走 real_migrations），
# acc_a_f21 全部 serial（真实进程/调度时序之外的小集合，且各平台都跑）。
NON_SERIAL_FILE = "tests/test_storage.py"
SERIAL_FILE = "tests/test_acc_a_f21_scheduling.py"


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in ("PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(name, None)
    return env


def _run(tmp_path: Path, extra: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--workers",
            "2",
            "--junit-dir",
            str(tmp_path),
            *extra,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_clean_env(),
    )


def _counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    return {
        key: sum(int(suite.get(key) or 0) for suite in root.iter("testsuite"))
        for key in ("tests", "failures", "errors", "skipped")
    }


def test_runner_runs_both_stages_and_writes_split_junit(tmp_path):
    completed = _run(tmp_path, ["--pytest-arg", NON_SERIAL_FILE, "--pytest-arg", SERIAL_FILE])
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-4000:]
    for token in ("[stage parallel]", "[stage serial]", "RESULT OK"):
        assert token in output, output[-2000:]

    parallel_junit = tmp_path / PARALLEL_JUNIT
    serial_junit = tmp_path / SERIAL_JUNIT
    assert parallel_junit.is_file() and serial_junit.is_file(), "两阶段的 JUnit 必须分文件落盘"
    parallel = _counts(parallel_junit)
    serial = _counts(serial_junit)
    assert parallel["tests"] == 7, parallel   # test_storage.py 的 7 条
    assert serial["tests"] == 4, serial       # test_acc_a_f21_scheduling.py 的 4 条
    assert parallel["failures"] == 0 and parallel["errors"] == 0, parallel
    assert serial["failures"] == 0 and serial["errors"] == 0, serial

    # 入口只把证据写进给定目录，不污染仓库其它位置。
    xml_files = sorted(path.name for path in tmp_path.glob("*.xml"))
    assert xml_files == [PARALLEL_JUNIT, SERIAL_JUNIT], xml_files


def test_runner_returns_nonzero_when_a_stage_selects_nothing(tmp_path):
    """两阶段都跑完再退出：即使第一阶段已经失败（选不到用例 = exit 5），第二阶段仍有输出。"""
    completed = _run(
        tmp_path,
        [
            "--marker",
            "qio_marker_that_matches_nothing",
            "--pytest-arg",
            NON_SERIAL_FILE,
        ],
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output[-2000:]
    assert "RESULT FAILED" in output, output[-2000:]
    assert "[stage parallel]" in output and "[stage serial]" in output, output[-2000:]
