"""后端全量测试统一入口（本地与 CI 共用）。

契约 §7（docs/topic-application-contract-20261110.md）：

    python scripts/run_backend_suite.py [--workers N] [--junit-dir DIR] [--marker "not requires_docker"]

两个阶段（都按 marker 分组，不按名字/路径排除任何测试）：

1. 并行组：pytest -m "not serial [and (marker)]" -n N
   JUnit: <junit-dir>/pytest-parallel.xml
2. 串行组：pytest -m "serial [and (marker)]" -p no:xdist
   JUnit: <junit-dir>/pytest-serial.xml

分组不变量：两组并集 = 全量收集、交集 = 空（由 scripts/test_partition_check.py 机器校验）。
两阶段都会执行完（第一阶段失败不跳过第二阶段），任一阶段 exit code 非 0 → 脚本非 0 退出。

退出码：0 = 两阶段都通过；1 = 至少一个阶段失败；130 = 被中断。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
STAGE_JUNIT: dict[str, str] = {
    "parallel": "pytest-parallel.xml",
    "serial": "pytest-serial.xml",
}


def marker_and(base: str, marker: str) -> str:
    """把阶段基础表达式与用户 marker 表达式用 AND 组合（括号包裹用户表达式）。"""
    marker = marker.strip()
    if not marker:
        return base
    return f"{base} and ({marker})"


def _junit_counts(path: Path) -> dict[str, int]:
    """从 JUnit XML 汇总 tests/failures/errors/skipped（缺失/损坏时返回空）。"""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in root.iter("testsuite"):
        for key in counts:
            counts[key] += int(suite.get(key) or 0)
    return counts


def _failed_cases(path: Path, limit: int = 10) -> list[str]:
    """从 JUnit XML 取失败/错误用例名，供失败摘要使用。"""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    failed: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(f"{case.get('classname')}::{case.get('name')}")
    return failed[:limit]


def _pytest_env() -> dict[str, str]:
    """子进程 pytest 的环境：去掉会重复注入参数/报告的 PYTEST_ADDOPTS。"""
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _run_stage(
    name: str,
    expression: str,
    *extra_pytest_args: str,
    junit_path: Path | None,
    workers: int,
    use_xdist: bool,
) -> tuple[int, float, dict[str, int], list[str]]:
    """跑一个阶段；返回 (exit_code, 用时秒, JUnit 计数, 失败用例名)。"""
    cmd = [sys.executable, "-m", "pytest", "-m", expression]
    if use_xdist:
        cmd += ["-n", str(workers)]
    else:
        cmd += ["-p", "no:xdist"]
    if junit_path is not None:
        junit_path.parent.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--junitxml={junit_path}")
    cmd += list(extra_pytest_args)
    print(f"[stage {name}] cwd={BACKEND_DIR}")
    print(f"[stage {name}] cmd={' '.join(cmd)}", flush=True)
    started = time.perf_counter()
    try:
        completed = subprocess.run(cmd, cwd=str(BACKEND_DIR), env=_pytest_env())
        code = int(completed.returncode)
    except KeyboardInterrupt:
        print(f"[stage {name}] interrupted", flush=True)
        raise
    elapsed = time.perf_counter() - started
    counts = _junit_counts(junit_path) if junit_path is not None else {}
    failures = _failed_cases(junit_path) if (junit_path is not None and code != 0) else []
    detail = " ".join(f"{key}={value}" for key, value in counts.items()) if counts else "no junit report"
    print(
        f"[stage {name}] rc={code} elapsed={elapsed:.1f}s {detail} "
        f"junit={junit_path if junit_path is not None else '(none)'}",
        flush=True,
    )
    for case in failures:
        print(f"[stage {name}] FAILED {case}", flush=True)
    return code, elapsed, counts, failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="后端全量测试统一入口：并行组（xdist）+ 串行组（serial）。",
    )
    parser.add_argument("--workers", type=int, default=4, help="并行组 xdist worker 数（默认 4；禁止 auto）")
    parser.add_argument(
        "--junit-dir",
        default=None,
        help="JUnit 输出目录（相对调用者当前目录解析）；两个阶段分别写 pytest-parallel.xml / pytest-serial.xml",
    )
    parser.add_argument("--marker", default="", help='附加 marker 表达式（两组都用 AND 叠加），如 "not requires_docker"')
    parser.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="额外 pytest 参数（可重复；本地小样本调试用，CI 不用）",
    )
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers 必须 >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    junit_dir: Path | None = None
    if args.junit_dir:
        junit_dir = Path(args.junit_dir)
        if not junit_dir.is_absolute():
            junit_dir = (Path.cwd() / junit_dir).resolve()
    extra = list(args.pytest_arg)
    print(
        f"[suite] backend={BACKEND_DIR} workers={args.workers} "
        f"marker={args.marker!r} junit_dir={junit_dir}",
        flush=True,
    )
    stages = [
        ("parallel", marker_and("not serial", args.marker), True),
        ("serial", marker_and("serial", args.marker), False),
    ]
    results: list[tuple[str, int, float, dict[str, int], list[str]]] = []
    for name, expression, use_xdist in stages:
        junit_path = (junit_dir / STAGE_JUNIT[name]) if junit_dir is not None else None
        code, elapsed, counts, failures = _run_stage(
            name,
            expression,
            *extra,
            junit_path=junit_path,
            workers=args.workers,
            use_xdist=use_xdist,
        )
        results.append((name, code, elapsed, counts, failures))

    total_tests = sum(item[3].get("tests", 0) for item in results)
    total_failed = sum(item[3].get("failures", 0) + item[3].get("errors", 0) for item in results)
    total_elapsed = sum(item[2] for item in results)
    failed_stages = [item[0] for item in results if item[1] != 0]
    print(
        f"[suite] stages={len(results)} tests={total_tests} failed={total_failed} "
        f"elapsed={total_elapsed:.1f}s failed_stages={failed_stages or 'none'}",
        flush=True,
    )
    if failed_stages:
        print(f"[suite] RESULT FAILED ({', '.join(failed_stages)})", flush=True)
        return 1
    print("[suite] RESULT OK", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
