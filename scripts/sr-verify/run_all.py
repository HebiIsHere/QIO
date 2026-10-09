"""独立受控反例验收运行器（契约 6，Agent F）。

用法（在 backend 目录内）：

    uv run --frozen --extra dev python ../scripts/sr-verify/run_all.py

逐组执行五组反例（权限 / 进程 / 打码 / 状态 / 响应性），每组给出中文
PASS / FAIL / SKIP 结论：

* PASS  当前行为已满足**修复后**的契约期望；
* FAIL  反例成立（未修复基线上的预期结果——这正是需要修复的证据）；
* SKIP  环境缺件（如本机没有 git），不计入失败。

证据（布尔/线程号/时间戳，不含任何密钥原文）写入 scripts/sr-verify/output/。
退出码：全部 PASS → 0；存在 FAIL → 1（前后对照的机器可读信号）。
状态组（契约 5 前端时序）是 vitest 规格，见 README.md 的 npm 命令。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probes  # noqa: E402

GROUP_TITLES = {
    "permission_git_tag_default": "权限组 1a：git tag 创建（default 模式）",
    "permission_git_tag_plan": "权限组 1c：git tag 创建（plan 模式）",
    "permission_env_bypass": "权限组 1b：工作区外 .env 的 run_program 旁路",
    "permission_plan_shell_write": "权限组 1c 护栏：plan 模式自由 shell 写命令",
    "process_git_show_missing_ref": "进程组 2a：git show 不存在的引用的真实退出码",
    "process_timeout_tree": "进程组 2b：超时返回后进程树存活反例（含诱饵验收）",
    "process_cancel_tree": "进程组 2b 取消：取消后进程树退出 + CancelledError 传播",
    "redaction_bus_sse": "打码组：注册密钥 → SSE 字节 / 重放 / api_key / 日志四路径",
    "responsiveness_event_loop_gate": "响应性组：embed/文件扫描闸门推迟事件循环工作",
}

STATE_GROUP_NOTE = (
    "状态组（契约 5 前端两个时序反例）由 vitest 运行："
    "cd frontend && npm test -- --run src/__tests__/sr-f-turn-lifecycle.spec.ts"
    "（详见本目录 README.md）"
)


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    results: list[tuple[str, str, str]] = []
    fail = skip = passed = 0
    print("=" * 78)
    print("QIO 契约 6 独立受控反例验收（Agent F，2026-10-09 冻结契约）")
    print("PASS = 当前行为满足修复后期望；FAIL = 反例成立（未修复基线上为预期）")
    print("=" * 78)
    for label, title in GROUP_TITLES.items():
        runner = probes.ALL_PROBES.get(label)
        t0 = time.perf_counter()
        try:
            outcome = runner()
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Future):
                evidence = await asyncio.wait_for(outcome, timeout=120)
            else:
                evidence = outcome
        except Exception as exc:  # noqa: BLE001 - 运行器本身崩溃要如实报告
            verdict, reason = "FAIL", (f"探针异常：{type(exc).__name__}: {str(exc)[:120]}")
            evidence = {}
        else:
            verdict, reason = probes.judge(label, evidence)
        if verdict == "FAIL":
            fail += 1
        elif verdict == "SKIP":
            skip += 1
        else:
            passed += 1
        elapsed = round(time.perf_counter() - t0, 2)
        icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[verdict]
        print(f"{icon} {title}")
        print(f"      结论：{reason}（耗时 {elapsed}s，证据: scripts/sr-verify/output/{label}.json）")
    print("-" * 78)
    print(f"汇总：PASS={passed} FAIL={fail} SKIP={skip}")
    print(STATE_GROUP_NOTE)
    print("=" * 78)
    return 1 if fail else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except BrokenPipeError:
        sys.exit(1)
