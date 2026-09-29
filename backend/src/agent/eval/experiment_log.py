"""逐条留痕：一次运行一个目录，记录每条用例的输入、标准答案、候选、预测、耗时与费用。

规矩只有两条：

1. `cases.jsonl` 是唯一事实源 —— 报告里的汇总数字必须由它重算（`aggregate`），
   不允许手写；
2. 运行元信息（代码版本、配置、机器、时间）落在 `meta.json`，让"这次数字是哪份代码跑的"可查。
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "evals" / "runs"


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    directory: Path
    cases_path: Path
    meta_path: Path


def code_version(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            suffix = "+dirty" if dirty.stdout.strip() else ""
            return out.stdout.strip() + suffix
    except Exception:
        pass
    return "unknown"


def start_run(
    root: Path | None = None,
    *,
    name: str,
    config: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> RunPaths:
    root = Path(root) if root else DEFAULT_ROOT
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    directory = root / name / run_id
    directory.mkdir(parents=True, exist_ok=True)
    paths = RunPaths(
        run_id=run_id,
        directory=directory,
        cases_path=directory / "cases.jsonl",
        meta_path=directory / "meta.json",
    )
    meta = {
        "run_id": run_id,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code_version(),
        "config": config or {},
        "machine": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_count": platform.os.cpu_count(),
        },
    }
    paths.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def log_case(
    run: RunPaths,
    *,
    job: str,
    case_id: str,
    query: str,
    gold: Sequence[str],
    ranked: Sequence[str],
    candidates: Sequence[str] | None = None,
    scores: Sequence[float] | None = None,
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    predicted: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """追加一条记录（只追加，不覆盖）。"""
    record = {
        "job": job,
        "case_id": case_id,
        "query": query,
        "gold": list(gold),
        "ranked": list(ranked),
        "candidates": list(candidates) if candidates is not None else list(ranked),
        "scores": [round(float(s), 6) for s in scores] if scores is not None else None,
        "predicted": predicted,
        "gold_rank": _gold_rank(ranked, gold),
        "latency_ms": round(latency_ms, 3) if latency_ms is not None else None,
        "cost_usd": cost_usd,
        "extra": extra or {},
    }
    with run.cases_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _gold_rank(ranked: Sequence[str], gold: Sequence[str]) -> int | None:
    wanted = set(gold)
    for index, doc_id in enumerate(ranked, start=1):
        if doc_id in wanted:
            return index
    return None


def load_cases(run: RunPaths | Path) -> list[dict[str, Any]]:
    path = run.cases_path if isinstance(run, RunPaths) else Path(run)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def aggregate(
    cases: Iterable[dict[str, Any]], *, job: str | None = None, k: int = 5
) -> dict[str, Any]:
    """从逐条记录重算指标（报告里只能用它算出来的数字）。"""
    rows = [c for c in cases if job is None or c.get("job") == job]
    n = len(rows)
    if not n:
        return {"n": 0}
    hit1 = hitk = 0
    rr = 0.0
    precise = 0
    latencies = [c["latency_ms"] for c in rows if c.get("latency_ms") is not None]
    costs = [c["cost_usd"] for c in rows if c.get("cost_usd") is not None]
    for row in rows:
        ranked = row.get("ranked") or []
        gold = set(row.get("gold") or [])
        top1 = set(ranked[:1])
        topk = set(ranked[:k])
        if top1 & gold:
            hit1 += 1
        if topk & gold:
            hitk += 1
        rank = row.get("gold_rank")
        if rank:
            rr += 1.0 / rank
        precise += len(topk & gold)

    def rate(num: float, den: int) -> float:
        return round(num / den, 4) if den else 0.0

    out: dict[str, Any] = {
        "n": n,
        "recall@1": rate(hit1, n),
        f"recall@{k}": rate(hitk, n),
        "mrr": round(rr / n, 4),
        f"precision@{k}": rate(precise, n * k),
        f"no_gold_in_top{k}_rate": rate(n - hitk, n),
    }
    if latencies:
        ordered = sorted(latencies)
        out["latency_ms"] = {
            "n": len(ordered),
            "mean_ms": round(sum(ordered) / len(ordered), 2),
            "p50_ms": round(ordered[len(ordered) // 2], 2),
            "p90_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 2),
        }
    if costs:
        out["cost_usd"] = round(sum(costs), 6)
    return out


def aggregate_by(rows: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """按某个字段分组后再各自汇总（例如按难度分层、按臂分）。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get(key) or row.get("extra", {}).get(key) or "unknown"), []).append(row)
    return {name: aggregate(items) for name, items in sorted(buckets.items())}
