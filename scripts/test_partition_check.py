"""机器校验测试分区：「-m not serial」与「-m serial」的并集 = 全量、交集 = 空。

用法（repo 根或任意目录均可；脚本自己切到 backend 跑 pytest）：

    python scripts/test_partition_check.py [--marker "not requires_docker"]

做法：用 pytest --collect-only 分别取全量 / 并行组 / 串行组的 nodeid 集合，
断言并集 = 全量、交集 = 空，并打印计数清单。不把「标记成 serial」当独占机器：
它只表示该用例不能与并行组同时跑；串行阶段仍与其它串行用例共用一台机器。

条件 skip 说明：--collect-only 只收集、不执行，skipif / importorskip 在三次收集中
走同一代码路径，因此不影响并集/交集判定；运行时 skip 的计数请看入口脚本的 JUnit 汇总。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"


def marker_and(base: str, marker: str) -> str:
    marker = marker.strip()
    if not marker:
        return base
    return f"{base} and ({marker})"


def collect(expression: str | None, extra_args: list[str]) -> set[str]:
    """跑一次 pytest --collect-only，返回 nodeid 集合。"""
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
    if expression:
        cmd += ["-m", expression]
    cmd += extra_args
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)  # 不让外部 addopts 重复注入 --junitxml 等参数
    completed = subprocess.run(
        cmd,
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        tail = (completed.stdout or "")[-2000:] + " | " + (completed.stderr or "")[-2000:]
        raise SystemExit(f"collect failed: {expression!r} rc={completed.returncode} {tail}")
    nodeids: set[str] = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or "::" not in line:
            continue
        if line.startswith("=") or line.startswith("FAILED") or line.startswith("ERROR"):
            continue
        nodeids.add(line)
    return nodeids


def _print_sample(label: str, nodeids: set[str], limit: int = 20) -> None:
    if not nodeids:
        return
    print(f"{label}（前 {min(limit, len(nodeids))} 条）：")
    for nodeid in sorted(nodeids)[:limit]:
        print(f"  - {nodeid}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 serial / not-serial 两组测试分区：并集=全量、交集=空。",
    )
    parser.add_argument("--marker", default="", help="额外 marker 表达式（与两组 AND 叠加）")
    parser.add_argument(
        "--extra-pytest-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="额外 pytest 参数（可重复；一般不用）",
    )
    args = parser.parse_args(argv)

    base_expression = args.marker.strip() or None
    parallel_expression = marker_and("not serial", args.marker)
    serial_expression = marker_and("serial", args.marker)
    print(f"[partition] cwd={BACKEND_DIR}")
    print(f"[partition] 全量 marker: {base_expression if base_expression else '(无)'}")
    print(f"[partition] 并行组 marker: {parallel_expression}")
    print(f"[partition] 串行组 marker: {serial_expression}")

    full = collect(base_expression, args.extra_pytest_arg)
    parallel = collect(parallel_expression, args.extra_pytest_arg)
    serial = collect(serial_expression, args.extra_pytest_arg)

    union = parallel | serial
    intersection = parallel & serial
    missing = full - union
    extra = union - full
    print("[partition] 计数：")
    print(f"  全量 selected = {len(full)}")
    print(f"  并行组       = {len(parallel)}")
    print(f"  串行组       = {len(serial)}")
    print(f"  并集         = {len(union)}")
    print(f"  交集         = {len(intersection)}")
    print(f"  漏收（全量 - 并集） = {len(missing)}")
    print(f"  多收（并集 - 全量） = {len(extra)}")
    _print_sample("漏收用例", missing)
    _print_sample("交集用例", intersection)
    _print_sample("多收用例", extra)
    print("[partition] 条件 skip：--collect-only 不执行用例，skipif / importorskip 在三次收集中")
    print("            走同一代码路径，因此不影响并集/交集；运行时 skip 计数见 JUnit 汇总。")
    ok = not missing and not extra and not intersection
    print("[partition] RESULT " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
