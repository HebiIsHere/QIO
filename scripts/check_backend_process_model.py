"""把 verify_backend_process_model.py 的 JSON 报告变成 CI 门槛。

为什么需要它：`scripts/verify_backend_process_model.py`（Agent A）只**采集数据** —— 跑完就
`return 0`，所以它自己在 CI 里当不了闸门：进程模型退化了它照样绿。这个脚本读它的 JSON，把
两条已经实测过的形状钉成断言，形状一变就让 CI 红（提醒有人重新验证，而不是悄悄放过）：

* `--case 2`：onefile = launcher + child（同一个 exe 路径的两个进程）；**只杀 launcher 会留下
  child、端口仍然开着** —— 这正是壳必须用 Job Object 的原因；
* `--case job`：`assign` 紧跟 `spawn()` 时 child 在 job 里、关掉 job 句柄后**不留孤儿**、端口释放；
  把 assign 挪到 child 出现之后，child 立刻逃逸（反证：时序敏感）。

用法（Windows）：
    python scripts/verify_backend_process_model.py --exe <exe> --case 2 --json case2.json
    python scripts/check_backend_process_model.py --report case2.json --case 2
    python scripts/check_backend_process_model.py --selftest      # 离线自检：坏报告必须变红
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _fail(message: str) -> None:
    print(f"  [FAIL] {message}")
    # CI 上同时发一条注解（注解公开可读，不用登录下载日志就能定位）。
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error title=进程模型门槛::{message}")


def _ok(message: str) -> None:
    print(f"  [ok]   {message}")


def _require(condition: bool, message: str) -> bool:
    if condition:
        _ok(message)
        return True
    _fail(message)
    return False


def check_case2(report: dict) -> bool:
    """只杀 launcher：child 必须活着并继续占着端口（壳必须靠 Job Object 兜住）。"""
    case = report.get("case2")
    if not isinstance(case, dict):
        _fail("报告里没有 case2 —— 验证脚本没跑成功")
        return False
    passed = True
    ready = case.get("ready") or {}
    passed &= _require(bool(ready.get("ready")), f"后端真的起来并响应了（ready in {ready.get('seconds')}s）")
    before = case.get("before") or {}
    processes = before.get("processes") or []
    passed &= _require(
        int(before.get("count") or 0) >= 2,
        f"启动后是同 exe 的两个进程（launcher + child），实际 {before.get('count')} 个",
    )
    same_exe = {str(item.get("exe") or "").lower() for item in processes}
    passed &= _require(
        len(same_exe) == 1 and "" not in same_exe,
        f"两个进程的可执行文件路径相同（onefile 的 launcher/child 分不出父子），实际 {sorted(same_exe)}",
    )
    child_alive = case.get("child_alive") or []
    passed &= _require(
        bool(child_alive),
        f"只杀 launcher 之后 child 仍然活着（孤儿）—— 这是危险形状本身，实际 child_alive={child_alive}",
    )
    passed &= _require(
        case.get("port_open_after") is True,
        "孤儿 child 仍然占着端口（所以壳必须用 Job Object 收整个树）",
    )
    return passed


def check_job(report: dict) -> bool:
    """Job Object：立即 assign 必须收住 child；延迟 assign 必须逃逸（时序反证）。"""
    passed = True
    immediate = report.get("job_immediate")
    late = report.get("job_late")
    if not isinstance(immediate, dict) or not isinstance(late, dict):
        _fail("报告里缺 job_immediate / job_late —— 验证脚本没跑 case job")
        return False

    ready = immediate.get("ready") or {}
    passed &= _require(bool(ready.get("ready")), "立即 assign 时后端真的起来并响应了")
    passed &= _require(bool(immediate.get("assign_ok")), "立即 assign 成功")
    passed &= _require(
        immediate.get("child_in_job") is True,
        "立即 assign：**真正服务的 child** 在 job 里（这是壳不退化的关键）",
    )
    passed &= _require(
        immediate.get("launcher_alive") is False,
        "关闭 job 句柄后连 launcher 也不在了",
    )
    passed &= _require(
        immediate.get("port_open_after") is False,
        "关闭 job 句柄后端口已释放（不留孤儿、不挡安装器写 exe）",
    )
    after = immediate.get("after_close") or {}
    passed &= _require(
        int(after.get("count") or 0) == 0,
        f"关闭 job 句柄后本次实验相关的进程为 0，实际 {after.get('count')}",
    )

    # 反证：assign 挪到 child 之后 → child 逃逸。这一条如果不再成立，说明时序敏感的说法变了，
    # 需要有人重新验证（不是「产品坏了」，而是「结论过期了」）。
    passed &= _require(bool(late.get("assign_ok")), "延迟 assign 本身也是成功的调用")
    passed &= _require(
        late.get("child_in_job") is False,
        "反证仍然成立：延迟 assign 时 child 逃逸（不在 job 里）",
    )
    passed &= _require(
        late.get("port_open_after") is True,
        "反证仍然成立：逃逸的 child 仍占着端口",
    )
    return passed


CHECKS = {"2": check_case2, "job": check_job}


def _good_case2() -> dict:
    return {
        "case2": {
            "ready": {"ready": True, "seconds": 3.5},
            "before": {
                "count": 2,
                "processes": [
                    {"pid": 100, "ppid": 1, "exe": "C:/x/qio-backend.exe"},
                    {"pid": 200, "ppid": 100, "exe": "C:/x/qio-backend.exe"},
                ],
            },
            "killed_pid": 100,
            "child_alive": [200],
            "port_open_after": True,
        }
    }


def _good_job() -> dict:
    return {
        "job_immediate": {
            "assign_ok": True,
            "ready": {"ready": True, "seconds": 3.0},
            "child_in_job": True,
            "launcher_alive": False,
            "port_open_after": False,
            "after_close": {"count": 0, "processes": []},
        },
        "job_late": {
            "assign_ok": True,
            "child_in_job": False,
            "launcher_alive": False,
            "port_open_after": True,
            "after_close": {"count": 1, "processes": [{"pid": 300}]},
        },
    }


def selftest() -> int:
    """离线自检：好报告必须过，每种退化都必须红。"""
    cases: list[tuple[str, str, dict, bool]] = []
    cases.append(("case2 好报告", "2", _good_case2(), True))
    cases.append(("job 好报告", "job", _good_job(), True))

    broken = _good_case2()
    broken["case2"]["child_alive"] = []
    cases.append(("launcher 死后 child 也死（模型变了）", "2", broken, False))

    single = _good_case2()
    single["case2"]["before"] = {"count": 1, "processes": [{"pid": 100, "exe": "C:/x/qio-backend.exe"}]}
    cases.append(("不是两个进程（onefile 模型变了）", "2", single, False))

    not_ready = _good_case2()
    not_ready["case2"]["ready"] = {"ready": False, "seconds": 90}
    cases.append(("后端根本没起来", "2", not_ready, False))

    escaped = _good_job()
    escaped["job_immediate"]["child_in_job"] = False
    cases.append(("child 逃出 job", "job", escaped, False))

    orphan = _good_job()
    orphan["job_immediate"]["port_open_after"] = True
    orphan["job_immediate"]["after_close"] = {"count": 1, "processes": [{"pid": 200}]}
    cases.append(("关掉 job 后还留孤儿占端口", "job", orphan, False))

    no_late_proof = _good_job()
    no_late_proof["job_late"]["child_in_job"] = True
    cases.append(("反证不再成立（时序不敏感了）", "job", no_late_proof, False))

    no_case = {"job_immediate": _good_job()["job_immediate"]}
    cases.append(("报告里缺 job_late", "job", no_case, False))

    failures = 0
    for label, kind, report, expected in cases:
        print(f"== selftest: {label}（期望 {'过' if expected else '红'}）==")
        got = CHECKS[kind](report)
        if got is not expected:
            failures += 1
            print(f"   -> selftest 失败：期望 {expected}，实际 {got}")
    print()
    print(f"selftest：{len(cases)} 个场景，" + ("全部符合预期" if not failures else f"{failures} 个不符合预期"))
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="把进程模型实测报告变成 CI 门槛")
    parser.add_argument("--report", help="verify_backend_process_model.py --json 的输出")
    parser.add_argument("--case", choices=sorted(CHECKS), help="2 / job")
    parser.add_argument("--selftest", action="store_true", help="离线自检（好报告过、退化报告红）")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.report or not args.case:
        parser.error("要么 --selftest，要么同时给 --report 与 --case")
    path = Path(args.report)
    if not path.is_file():
        print(f"找不到报告：{path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"报告不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    print(f"== 进程模型门槛：case {args.case}（{path}）==")
    passed = CHECKS[args.case](report)
    print()
    print("门槛：" + ("通过" if passed else "**未通过**（进程模型或时序已经变了，需要人工重新验证）"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())