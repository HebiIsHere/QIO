"""分段边界的离线评测（阶段 4.4）。

规则调试样本（tune）与留出样本（holdout）**分开报告**：tune 上的数字只说明
规则写没写对，holdout 才是「换一种说话方式还行不行」的证据。

这个评测只跑**确定性策略**（memory/boundary.py）：不调用模型、不联网，
所以「调用成本」是 0；Embedding 一类的语义信号首版没有启用，
「降级表现」记 N/A 而不是编一个数字。

用法（backend 目录下）：
    uv run --frozen python -m agent.eval.boundary_eval
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from agent.memory.boundary import ACTION_SPLIT, POLICY_VERSION, FragmentBoundaryPolicy

CASES_PATH = Path(__file__).with_name("boundary_cases.jsonl")


@dataclass
class CaseResult:
    case_id: str
    split: str
    expected: str
    actual: str
    reason: str
    ok: bool


def load_cases(path: Path = CASES_PATH) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def evaluate(cases: list[dict], *, max_turns: int = 10, max_tokens: int = 8000) -> list[CaseResult]:
    policy = FragmentBoundaryPolicy(max_turns=max_turns, max_tokens=max_tokens)
    results: list[CaseResult] = []
    for case in cases:
        decision = policy.decide(
            user_input=case["text"],
            fragment_turns=int(case.get("turns", 0)),
            fragment_tokens=int(case.get("tokens", 0)),
        )
        expected = case["expect"]
        # 「不切」的两种答案（continue / uncertain）都算对得上「不该切」：
        # 策略的价值在「该切的时候切、不该切的时候别切」，不是猜中某个词。
        if expected == "split":
            ok = decision.action == ACTION_SPLIT
        else:
            ok = decision.action != ACTION_SPLIT
        results.append(
            CaseResult(
                case_id=case["id"],
                split=case.get("split", "tune"),
                expected=expected,
                actual=decision.action,
                reason=decision.reason,
                ok=ok,
            )
        )
    return results


def summarize(results: list[CaseResult]) -> dict:
    def bucket(name: str) -> list[CaseResult]:
        return [r for r in results if r.split == name]

    def stats(rows: list[CaseResult]) -> dict:
        total = len(rows)
        wrong_split = [r for r in rows if r.expected != "split" and r.actual == ACTION_SPLIT]
        missed_split = [r for r in rows if r.expected == "split" and r.actual != ACTION_SPLIT]
        return {
            "count": total,
            "cases_ok": sum(1 for r in rows if r.ok),
            "誤切": len(wrong_split),
            "漏切": len(missed_split),
            "wrong_split_ids": [r.case_id for r in wrong_split],
            "missed_split_ids": [r.case_id for r in missed_split],
        }

    return {
        "policy_version": POLICY_VERSION,
        "tune": stats(bucket("tune")),
        "holdout": stats(bucket("holdout")),
        "model_calls": 0,
        "note": (
            "确定性策略：0 次模型调用；「边界延迟」在本版无观测手段（N/A）；"
            "Embedding 语义信号未启用，降级表现 N/A"
        ),
    }


def main() -> int:
    cases = load_cases()
    results = evaluate(cases)
    report = summarize(results)
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        print(f"[{mark}] {r.case_id}({r.split}) 期望={r.expected} 实际={r.actual}:{r.reason}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    tune = report["tune"]
    holdout = report["holdout"]
    print(
        f"\n规则样本 {tune['cases_ok']}/{tune['count']}；留出样本 {holdout['cases_ok']}/{holdout['count']}"
        f"；误切 {tune['誤切'] + holdout['誤切']}；漏切 {tune['漏切'] + holdout['漏切']}"
    )
    # 留出样本的失败要能被 CI 看见，但规则样本的失败同样不该被忽略
    return 0 if (tune["cases_ok"] == tune["count"] and holdout["cases_ok"] == holdout["count"]) else 1


if __name__ == "__main__":
    sys.exit(main())
