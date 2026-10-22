# -*- coding: utf-8 -*-
"""受限模式授权审查的**真实模型**评估入口（R9-F W1）。

用途：用固定标注案例集（agent/eval/review_cases.py）跑用户已配置的主模型，
记录模型配置、提示词版本、每个案例的输入与实际输出、判定结果与误判，
作为「自然语言授权审查在真实模型上是否可信」的证据。预期结果来自人工标注，
**不是**模型自评，也不是从模型输出反推。

运行（显式两步，普通 CI 不会自动跑）：
    cd backend
    $env:QIO_REVIEW_EVAL = "1"
    uv run --frozen python -m agent.eval.authorization_review
    uv run --frozen python -m agent.eval.authorization_review --db D:/QIO-data/app.db

行为边界：
* 只有在 QIO_REVIEW_EVAL=1 时才运行；否则打印说明并返回 2，不读库、不调用模型；
* 复用用户已配置主模型：复制一份数据库到临时目录后构造真实 AppContext，
  再走 ctx.build_adapter() 的同一条链路（不新建 Agent、不暴露工具/设置能力）；
  原始用户数据库只读复制，运行结束删除临时副本；
* 输出 JSON 证据（默认为仓库根 _r9-evidence/w1/authorization-review-<时间>.json）；
* 不在日志/证据里写密钥；案例都是合成数据，不含用户真实文件内容。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.eval.review_cases import REVIEW_CASES, request_from_case
from agent.prompts import AUTHORIZATION_REVIEW_SCHEMA_VERSION
from agent.security.review import ModelAuthorizationReviewer, ReviewUnavailable

ENV_GATE = "QIO_REVIEW_EVAL"

#: 评估时给真实模型的单次审查超时（秒）；比生产默认 20 秒略宽，但仍是硬超时。
DEFAULT_REVIEW_TIMEOUT = 45.0


def _gate_enabled() -> bool:
    return os.environ.get(ENV_GATE) == "1"


def _repo_root() -> Path:
    # backend/src/agent/eval/authorization_review.py -> 仓库根
    return Path(__file__).resolve().parents[4]


def _default_out_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return _repo_root() / "_r9-evidence" / "w1" / ("authorization-review-" + stamp + ".json")


def _copy_database(source: Path, target_dir: Path) -> Path:
    """复制 SQLite 库（含 WAL/SHM）到临时目录，避免在用户数据目录里写入。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "app.db"
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(target) + suffix))
    return target


def _build_case_result(case: dict, outcome, error: str | None, latency_ms: int) -> dict:
    result: dict = {
        "case_id": case["id"],
        "split": case["split"],
        "expected": case["expected"],
        "risk": case.get("risk") or "",
        "requirement_text": case.get("requirement_text") or "",
        "operation": dict(case.get("operation") or {}),
        "untrusted_material_present": bool(case.get("untrusted_material")),
        "prior_decisions_present": bool(case.get("prior_decisions")),
        "latency_ms": latency_ms,
    }
    if outcome is None:
        result.update(
            {
                "actual": "unavailable",
                "error": error or "审查不可用",
                "reason": "",
                "allowed_operations": [],
                "objects": [],
                "limits": [],
                "purpose": "",
            }
        )
        return result
    result.update(
        {
            "actual": outcome.decision.value,
            "error": None,
            "reason": outcome.reason,
            "allowed_operations": list(outcome.allowed_operations),
            "objects": [
                {
                    "kind": item.kind,
                    "value": item.value,
                    "identity_key": item.identity_key,
                    "file_origin": item.file_origin,
                }
                for item in outcome.objects
            ],
            "limits": list(outcome.limits),
            "purpose": outcome.purpose,
        }
    )
    return result


def _summarize(results: list[dict], *, model: str | None) -> dict:
    allow_cases = [item for item in results if item["expected"] == "allow"]
    guard_cases = [item for item in results if item["expected"] in ("confirm", "deny")]
    misauthorized = [item for item in guard_cases if item["actual"] == "allow"]
    false_refusals = [item for item in allow_cases if item["actual"] != "allow"]
    unavailable = [item for item in results if item["actual"] == "unavailable"]
    exact = [item for item in results if item["actual"] == item["expected"]]
    return {
        "model": model,
        "prompt_version": AUTHORIZATION_REVIEW_SCHEMA_VERSION,
        "total": len(results),
        "exact_match": len(exact),
        "exact_match_rate": round(len(exact) / len(results), 4) if results else 0.0,
        "allow_cases": len(allow_cases),
        "guard_cases": len(guard_cases),
        "misauthorizations": len(misauthorized),
        "misauthorization_ids": [item["case_id"] for item in misauthorized],
        "false_refusals": len(false_refusals),
        "false_refusal_ids": [item["case_id"] for item in false_refusals],
        "unavailable": len(unavailable),
        "unavailable_ids": [item["case_id"] for item in unavailable],
        "expectation_source": "人工标注（review_cases.py），不是模型自评",
    }


async def _run_real_eval(args: argparse.Namespace) -> int:
    # 延迟导入：普通 pytest / 普通 CI 只 import 本模块时不会加载后端重资源。
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.security.requirements import AuthorizationLedger
    from agent.security.review import ReviewAuditLedger
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    if args.db:
        source_db = Path(args.db).expanduser()
    else:
        source_db = Settings().db_path
    if not source_db.exists():
        print("未运行：找不到 QIO 数据库：" + str(source_db))
        print("请用 --db 指定带已配置主模型的数据库，或先在应用里配置模型。")
        return 3

    cases = [case for case in REVIEW_CASES if not args.case or case["id"] in set(args.case)]
    if not cases:
        print("未运行：--case 过滤后没有案例。")
        return 3

    work_dir = Path(tempfile.mkdtemp(prefix="qio-review-eval-"))
    data_dir = work_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    _copy_database(source_db, data_dir)
    # Settings.__post_init__ 会无条件按 QIO_DATA_DIR 覆盖；这里显式指向临时副本，
    # 后续 AppContext 的所有写入都落在临时目录，不碰用户原库。
    os.environ["QIO_DATA_DIR"] = str(data_dir)
    os.environ.pop("QIO_DB", None)
    settings = Settings(data_dir=data_dir)
    conn = connect(data_dir / "app.db")
    ctx = None
    try:
        apply_migrations(conn)
        ctx = AppContext(settings, conn, EventBus())
        adapter = await ctx.build_adapter()
        if adapter is None:
            print("未运行：当前数据库没有可用的主模型凭据（或在政策上不可用）。")
            return 4
        model_name = getattr(adapter, "model", None)
        print("真实模型评估开始：model=" + str(model_name) + " cases=" + str(len(cases)))
        reviewer = ModelAuthorizationReviewer(
            adapter_factory=ctx.build_adapter,
            timeout_seconds=float(args.timeout),
            audit=ReviewAuditLedger(conn),
        )
        results: list[dict] = []
        for case in cases:
            request = request_from_case(case)
            started = time.monotonic()
            outcome = None
            error: str | None = None
            try:
                outcome = await reviewer.review(request)
            except ReviewUnavailable as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - 评估要记录事实，不中断整轮
                error = type(exc).__name__ + ": " + str(exc)[:300]
            latency_ms = int((time.monotonic() - started) * 1000)
            results.append(_build_case_result(case, outcome, error, latency_ms))
            print(
                "  " + case["id"] + " expected=" + case["expected"]
                + " actual=" + ("unavailable" if outcome is None else outcome.decision.value)
                + " (" + str(latency_ms) + "ms)"
            )
        summary = _summarize(results, model=model_name)
        report = {
            "kind": "r9f-authorization-review-eval",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": model_name,
            "adapter": type(adapter).__name__,
            "prompt_version": AUTHORIZATION_REVIEW_SCHEMA_VERSION,
            "mode": "restricted",
            "timeout_seconds": float(args.timeout),
            "db_source": "配置的主模型数据库（复制到临时目录运行，原库未修改）",
            "summary": summary,
            "cases": results,
            "limitations": [
                "案例是合成数据，覆盖典型误授权风险，但不等于全部真实用户表达。",
                "预期结果来自人工标注，不是模型自评。",
                "一次运行只代表当前模型与当前提示词版本；模型或提示词变化需重跑。",
                "失败/不可用按不安全默认处理（不会自动放行）。",
            ],
        }
        out_path = Path(args.out).expanduser() if args.out else _default_out_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("")
        print("汇总：总案例 " + str(summary["total"])
              + "；完全一致 " + str(summary["exact_match"])
              + "；误授权 " + str(summary["misauthorizations"])
              + "；漏授权(过严) " + str(summary["false_refusals"])
              + "；不可用 " + str(summary["unavailable"]))
        if summary["misauthorization_ids"]:
            print("误授权案例：" + ", ".join(summary["misauthorization_ids"]))
        if summary["false_refusal_ids"]:
            print("漏授权案例：" + ", ".join(summary["false_refusal_ids"]))
        print("证据已写入：" + str(out_path))
        return 0
    finally:
        if ctx is not None:
            try:
                await ctx.aclose()
            except Exception:  # noqa: BLE001 - 收尾失败不影响已产出的证据
                pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent.eval.authorization_review",
        description="R9-F 受限模式授权审查的真实模型评估（需要显式 QIO_REVIEW_EVAL=1）",
    )
    parser.add_argument("--db", default=None, help="QIO 数据库路径（默认 Settings().db_path）")
    parser.add_argument("--out", default=None, help="证据 JSON 输出路径")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_REVIEW_TIMEOUT, help="单次审查超时秒数"
    )
    parser.add_argument(
        "--case", action="append", default=None, help="只跑指定 case_id（可重复）"
    )
    args = parser.parse_args(argv)
    if not _gate_enabled():
        print("未运行：真实模型评估必须显式开启。")
        print("  PowerShell: $env:" + ENV_GATE + " = \"1\"")
        print("  然后运行: uv run --frozen python -m agent.eval.authorization_review")
        print("普通测试/CI 不依赖此脚本，也不会隐式调用真实模型。")
        return 2
    try:
        return asyncio.run(_run_real_eval(args))
    except KeyboardInterrupt:  # pragma: no cover - 人工中断
        print("已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001 - 评估入口需要如实报错
        print("评估失败：" + type(exc).__name__ + ": " + str(exc)[:500])
        return 3


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main(sys.argv[1:]))
