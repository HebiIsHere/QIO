"""旧双层排序 vs 新纯相关性排序：最小行为对照（离线、确定性）。

两个方案共用**同一后端、同一索引文本、同一查询上下文、同一数据快照、同一评测时刻**
（`FIXED_NOW`，避免时间推进改变排序），只差排序算法本身：

legacy（评测参考；生产默认已不是它，见 docs/status.md）
    候选阶段：底层召回 36 条 → 每条 `score += anchor .5 / entity .3 / keyword .2 /
              recency ≤.4` → 按这个被奖励过的分数截 12 条；
    排序阶段：`0.4 × 相关度归一 + 0.25 × 时效衰减 + 0.35 × 话题亲和` → 截 6 条。
relevance（新生产默认 = `services/params.py:RANKING`）
    候选阶段：底层召回 12 条（候选池），分数即原始相关度；
    排序阶段：唯一入口 `services/ranking.py`，默认不加任何奖励 → 截 6 条。

两种对照口径（用户要求分开做）：

    A. 固定同一批候选成员：把 legacy 的候选池成员原样喂给新排序，只比较最终顺序，
       隔离出「排序因素」的影响。
    B. 完整流程：同样的底层召回请求上限（36）与同样的最终预算（6），比较
       legacy 12→6 与新 12→6；**单独统计旧中间筛选**（先按奖励截 12）造成的
       正确答案丢失。

指标口径：`recall@k` 是「命中任一合法答案」（多答案用例沿用 case 里明确的
`expected` 集合）；`pool_hit` 是「正确答案有没有进候选池」。所有汇总数字都由逐条记录
重算（`experiment_log.aggregate`），不允许手写。

用法：

    python -m agent.eval.ranking_eval                  # 场景语料（时间 / 话题关系齐全）
    python -m agent.eval.ranking_eval --scenarios out_of_topic
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agent.eval import experiment_log
from agent.eval.scenario_corpus import build_scenarios
from agent.eval.stress_corpus import Memory, RecallCase, StressCorpus
from agent.selector.base import IndexedDoc, MemoryCandidate
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.selector.tokenize import tokenize
from agent.services import params
from agent.services.decay import EPHEMERAL, DecayPolicy
from agent.services.ranking import RankingContext, rank

#: 固定评测时刻：时间推进不得改变对照结果。
FIXED_NOW = datetime(2026, 9, 29, 0, 0, 0, tzinfo=timezone.utc)

EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"

# 旧生产权重（本次已从生产代码移除；只在这里作为对照参考保留）
LEGACY_RECALL_TOP = 36
LEGACY_CANDIDATE_POOL = 12
LEGACY_RETURN_LIMIT = 6
LEGACY_ANCHOR_WEIGHT = 0.5
LEGACY_ENTITY_WEIGHT = 0.3
LEGACY_KEYWORD_WEIGHT = 0.2
LEGACY_RECENCY_AMPLITUDE = 0.4
LEGACY_RECENCY_HALF_LIFE_DAYS = 30.0
LEGACY_RELEVANCE_WEIGHT = 0.4
LEGACY_DECAY_WEIGHT = 0.25
LEGACY_AFFINITY_WEIGHT = 0.35


@dataclass(frozen=True)
class ArmResult:
    """一条查询在一个方案下的中间量与最终顺序。"""

    arm: str
    ranked: list[str]
    candidate_pool: list[str]
    raw_recall: list[str]
    relevance: dict[str, float]
    factors: dict[str, dict[str, float]]
    scores: dict[str, float]
    cut_by_intermediate_filter: list[str]


def _iso_str(age_days: float) -> str:
    from datetime import timedelta

    return (FIXED_NOW - timedelta(days=age_days)).isoformat()


def build_docs(memories: Sequence[Memory]) -> list[IndexedDoc]:
    return [
        IndexedDoc(
            doc_id=m.id,
            text=m.text,
            topic_id=m.topic_id,
            entity_ids=list(m.entity_ids),
            keywords=[],
            created_at=_iso_str(m.age_days),
        )
        for m in memories
    ]


def _rule_boost(doc: IndexedDoc, *, anchor_topic_id: str | None, now: datetime) -> float:
    """旧候选阶段的奖励（评测参考实现，与旧 `selector/rules.py` 同口径）。"""
    boost = 0.0
    if anchor_topic_id and doc.topic_id == anchor_topic_id:
        boost += LEGACY_ANCHOR_WEIGHT
    if doc.created_at:
        created = datetime.fromisoformat(doc.created_at)
        age = max(0.0, (now - created).total_seconds() / 86400.0)
        boost += LEGACY_RECENCY_AMPLITUDE * pow(0.5, age / LEGACY_RECENCY_HALF_LIFE_DAYS)
    # keyword 奖励：离线场景语料没有 keywords 元数据 → 恒为 0（如实说明，不假装测过）
    # entity 奖励：场景查询没有实体标注 → 恒为 0
    return boost


def fingerprint_scores(topics, query: str) -> dict[str, float]:
    """话题指纹重合度（第一跳）：与生产 `Retriever._fingerprint_scores` 同口径。

    旧链路把它当作话题亲和奖励（权重 0.35）；新默认权重为 0。
    评测需要它才能忠实复现旧行为。
    """
    query_tokens = set(tokenize(query))
    scores: dict[str, float] = {}
    if not query_tokens:
        return scores
    for topic in topics:
        overlap = query_tokens & set(topic.keywords)
        if overlap:
            scores[topic.id] = len(overlap) / len(query_tokens)
    return scores


def legacy_arm(
    docs: Sequence[IndexedDoc],
    case: RecallCase,
    *,
    backend: BM25Backend,
    anchor_topic_id: str | None,
    fingerprints: dict[str, float] | None = None,
    now: datetime = FIXED_NOW,
    recall_top: int = LEGACY_RECALL_TOP,
    pool_size: int = LEGACY_CANDIDATE_POOL,
    limit: int = LEGACY_RETURN_LIMIT,
) -> ArmResult:
    """旧双层排序：召回 → 奖励 → 截池 → 再加权 → 截断。"""
    by_id = {d.doc_id: d for d in docs}
    raw = backend.search(case.query, top_k=recall_top)
    raw_ids = [h.doc_id for h in raw]

    boosted: list[tuple[float, str, float]] = []
    for hit in raw:
        doc = by_id[hit.doc_id]
        boosted.append((hit.score + _rule_boost(doc, anchor_topic_id=anchor_topic_id, now=now), doc.doc_id, hit.score))
    boosted.sort(key=lambda t: (-t[0], t[1]))
    pool = boosted[:pool_size]
    pool_ids = [doc_id for _, doc_id, _ in pool]

    max_relevance = max((score for _, _, score in pool), default=1.0) or 1.0
    decay = DecayPolicy({EPHEMERAL: LEGACY_RECENCY_HALF_LIFE_DAYS})
    scored: list[tuple[float, str]] = []
    factors: dict[str, dict[str, float]] = {}
    relevance: dict[str, float] = {}
    for _, doc_id, score in pool:
        doc = by_id[doc_id]
        relevance_norm = score / max_relevance
        recency = 0.0
        if doc.created_at:
            created = datetime.fromisoformat(doc.created_at)
            age = max(0.0, (now - created).total_seconds() / 86400.0)
            recency = decay.weight(age, EPHEMERAL)
        affinity = 0.0
        if anchor_topic_id and doc.topic_id == anchor_topic_id:
            affinity = 1.0
        elif doc.topic_id is not None:
            affinity = float((fingerprints or {}).get(doc.topic_id, 0.0))
        final = (
            LEGACY_RELEVANCE_WEIGHT * relevance_norm
            + LEGACY_DECAY_WEIGHT * recency
            + LEGACY_AFFINITY_WEIGHT * affinity
        )
        scored.append((final, doc_id))
        relevance[doc_id] = score
        factors[doc_id] = {
            "relevance_norm": round(LEGACY_RELEVANCE_WEIGHT * relevance_norm, 6),
            "recency": round(LEGACY_DECAY_WEIGHT * recency, 6),
            "topic_affinity": round(LEGACY_AFFINITY_WEIGHT * affinity, 6),
        }
    scored.sort(key=lambda t: (-t[0], t[1]))
    ranked = [doc_id for _, doc_id in scored[:limit]]
    # 旧中间筛选丢弃的候选里，有哪些原本在底层召回上限之内（正确答案是否被它切掉）
    cut = [doc_id for doc_id in raw_ids if doc_id not in set(pool_ids)]
    return ArmResult(
        arm="legacy",
        ranked=ranked,
        candidate_pool=pool_ids,
        raw_recall=raw_ids,
        relevance=relevance,
        factors=factors,
        scores={doc_id: final for final, doc_id in scored},
        cut_by_intermediate_filter=cut,
    )


def relevance_arm(
    docs: Sequence[IndexedDoc],
    case: RecallCase,
    *,
    selector: Selector,
    anchor_topic_id: str | None,
    fingerprints: dict[str, float] | None = None,
    now: datetime = FIXED_NOW,
    pool_size: int = params.LIMITS.candidate_pool,
    limit: int = LEGACY_RETURN_LIMIT,
    policy: params.RankingPolicy = params.RANKING,
) -> ArmResult:
    """新默认：候选只按原始相关度，排序入口只算一次（默认不加奖励）。"""
    pool_candidates = selector.select(case.query, candidate_pool=pool_size)
    ranked = rank(
        pool_candidates,
        ctx=RankingContext(
            query=case.query,
            anchor_topic_id=anchor_topic_id,
            fingerprint_scores=fingerprints or {},
            now=now,
        ),
        policy=policy,
        decay=DecayPolicy({EPHEMERAL: policy.recency_half_life_days}),
        kind_of=lambda _doc_id: EPHEMERAL,
    )
    return ArmResult(
        arm="relevance",
        ranked=[item.doc_id for item in ranked[:limit]],
        candidate_pool=[item.doc_id for item in pool_candidates],
        raw_recall=[item.doc_id for item in pool_candidates],
        relevance={item.doc_id: item.relevance for item in ranked},
        factors={item.doc_id: dict(item.factors) for item in ranked},
        scores={item.doc_id: item.score for item in ranked},
        cut_by_intermediate_filter=[],
    )


def relevance_arm_on_fixed_pool(
    docs: Sequence[IndexedDoc],
    case: RecallCase,
    *,
    pool_ids: Sequence[str],
    relevance: dict[str, float],
    anchor_topic_id: str | None,
    now: datetime = FIXED_NOW,
    policy: params.RankingPolicy = params.RANKING,
    limit: int = LEGACY_RETURN_LIMIT,
) -> ArmResult:
    """对照口径 A：候选成员固定为 legacy 的候选池，只让新排序决定顺序。"""
    by_id = {d.doc_id: d for d in docs}
    candidates = [
        MemoryCandidate(
            doc_id=doc_id,
            relevance=relevance.get(doc_id, 0.0),
            sources=("bm25",),
            topic_id=by_id[doc_id].topic_id,
            created_at=by_id[doc_id].created_at,
            keywords=tuple(by_id[doc_id].keywords),
            entity_ids=tuple(by_id[doc_id].entity_ids),
        )
        for doc_id in pool_ids
    ]
    ranked = rank(
        candidates,
        ctx=RankingContext(query=case.query, anchor_topic_id=anchor_topic_id, now=now),
        policy=policy,
        decay=DecayPolicy({EPHEMERAL: policy.recency_half_life_days}),
        kind_of=lambda _doc_id: EPHEMERAL,
    )
    return ArmResult(
        arm="relevance_fixed_pool",
        ranked=[item.doc_id for item in ranked[:limit]],
        candidate_pool=list(pool_ids),
        raw_recall=list(pool_ids),
        relevance={item.doc_id: item.relevance for item in ranked},
        factors={item.doc_id: dict(item.factors) for item in ranked},
        scores={item.doc_id: item.score for item in ranked},
        cut_by_intermediate_filter=[],
    )


def _hits(ranked: Sequence[str], expected: Sequence[str], k: int) -> bool:
    return bool(set(ranked[:k]) & set(expected))


def literal_answerable_hits(
    corpus: StressCorpus,
    *,
    k: int = 5,
    now: datetime = FIXED_NOW,
) -> dict[str, Any]:
    """字面可答用例（fact_update / same_topic）上，两臂各命中多少条。

    走的是同一份语料、同一后端、同一时刻；结果由逐条重算，测试只断言这里返回的数字。
    """
    docs = build_docs(corpus.memories)
    backend = BM25Backend()
    backend.index(docs)
    selector = Selector(recall=backend)
    selector.load(docs)
    legacy_hits = new_hits = total = 0
    changed: list[dict[str, Any]] = []
    for case in corpus.recall_cases:
        if not (case.keyword_answerable and case.category in ("fact_update", "same_topic")):
            continue
        total += 1
        fp = fingerprint_scores(corpus.topics, case.query)
        legacy = legacy_arm(
            docs, case, backend=backend, anchor_topic_id=None, fingerprints=fp, now=now, limit=k
        )
        new = relevance_arm(
            docs, case, selector=selector, anchor_topic_id=None, fingerprints=fp, now=now, limit=k
        )
        legacy_ok = _hits(legacy.ranked, case.expected, k)
        new_ok = _hits(new.ranked, case.expected, k)
        legacy_hits += int(legacy_ok)
        new_hits += int(new_ok)
        if legacy_ok != new_ok:
            changed.append(
                {
                    "case_id": case.id,
                    "kind": case.category,
                    "query": case.query,
                    "expected": list(case.expected),
                    "legacy_topk": legacy.ranked,
                    "new_topk": new.ranked,
                    "expected_in_new_pool": bool(
                        set(new.candidate_pool) & set(case.expected)
                    ),
                }
            )
    return {
        "k": k,
        "total": total,
        "legacy_hits": legacy_hits,
        "relevance_hits": new_hits,
        "changed": changed,
    }


def compare(
    corpus: StressCorpus,
    *,
    now: datetime = FIXED_NOW,
    limit: int = LEGACY_RETURN_LIMIT,
    recall_top: int = LEGACY_RECALL_TOP,
    pool_size: int = LEGACY_CANDIDATE_POOL,
) -> dict[str, Any]:
    """跑两种口径的对照，返回逐条记录 + 由记录重算的汇总。"""
    docs = build_docs(corpus.memories)
    backend = BM25Backend()
    backend.index(docs)
    selector = Selector(recall=backend)
    selector.load(docs)

    records: list[dict[str, Any]] = []
    for case in corpus.recall_cases:
        # 离线场景语料没有可靠的「当前话题」重建（跨话题场景里两组记忆落在同一个
        # topic 下），因此对照里关闭话题亲和 —— 这对两臂一视同仁，也让结论更保守：
        # 话题奖励的实际收益/风险不在本对照的覆盖范围内（见 docs/status.md）。
        anchor = None
        legacy = legacy_arm(
            docs,
            case,
            backend=backend,
            anchor_topic_id=anchor,
            now=now,
            recall_top=recall_top,
            pool_size=pool_size,
            limit=limit,
        )
        fresh = relevance_arm(
            docs,
            case,
            selector=selector,
            anchor_topic_id=anchor,
            now=now,
            pool_size=params.LIMITS.candidate_pool,
            limit=limit,
        )
        fixed = relevance_arm_on_fixed_pool(
            docs,
            case,
            pool_ids=legacy.candidate_pool,
            relevance=legacy.relevance,
            anchor_topic_id=anchor,
            now=now,
            limit=limit,
        )
        expected = list(case.expected)
        records.append(
            {
                "job": "ranking",
                "case_id": case.id,
                "query": case.query,
                "gold": expected,
                "kind": case.category,
                "anchor_topic_id": anchor,
                "legacy": _arms_record(legacy, expected, limit),
                "relevance": _arms_record(fresh, expected, limit),
                "relevance_fixed_pool": _arms_record(fixed, expected, limit),
                "stale": list(case.stale),
                "config": {
                    "now": now.isoformat(),
                    "recall_top": recall_top,
                    "legacy_pool": pool_size,
                    "relevance_pool": params.LIMITS.candidate_pool,
                    "limit": limit,
                    "ranking": params.RANKING.as_dict(),
                },
            }
        )
    return {"n": len(records), "records": records, "summary": _summarize(records, limit)}


def _arms_record(arm: ArmResult, expected: Sequence[str], limit: int) -> dict[str, Any]:
    return {
        "ranked": arm.ranked,
        "candidate_pool": arm.candidate_pool,
        "raw_recall": arm.raw_recall,
        "hit@1": _hits(arm.ranked, expected, 1),
        f"hit@{limit}": _hits(arm.ranked, expected, limit),
        "pool_hit": bool(set(arm.candidate_pool) & set(expected)),
        "relevance": {k: round(v, 6) for k, v in arm.relevance.items()},
        "factors": arm.factors,
        "scores": {k: round(v, 6) for k, v in arm.scores.items()},
        "cut_by_intermediate_filter": arm.cut_by_intermediate_filter,
    }


def _summarize(records: Sequence[dict[str, Any]], limit: int) -> dict[str, Any]:
    """从逐条记录重算汇总（不手写数字）。"""
    out: dict[str, Any] = {"n": len(records), "by_arm": {}, "by_kind": {}, "flips": {}}
    for arm in ("legacy", "relevance", "relevance_fixed_pool"):
        hit1 = sum(1 for r in records if r[arm]["hit@1"])
        hitk = sum(1 for r in records if r[arm][f"hit@{limit}"])
        pool = sum(1 for r in records if r[arm]["pool_hit"])
        n = len(records) or 1
        out["by_arm"][arm] = {
            "recall@1": round(hit1 / n, 4),
            f"recall@{limit}": round(hitk / n, 4),
            "pool_hit_rate": round(pool / n, 4),
        }
    # 完整流程对照：谁对谁错（按「命中任一答案」口径，同一条查询的两臂都算同一 k）
    worse = better = same = 0
    for r in records:
        legacy_ok = r["legacy"][f"hit@{limit}"]
        new_ok = r["relevance"][f"hit@{limit}"]
        if legacy_ok and not new_ok:
            worse += 1
        elif new_ok and not legacy_ok:
            better += 1
        else:
            same += 1
    out["flips"] = {"old_ok_new_miss": worse, "old_miss_new_ok": better, "same": same}
    # 固定候选成员（口径 A）：只比较顺序，成员相同 → hit@k 的差异纯粹来自排序
    order_changed = sum(
        1
        for r in records
        if r["legacy"]["ranked"] != r["relevance_fixed_pool"]["ranked"]
    )
    out["flips"]["order_changed_on_fixed_pool"] = order_changed
    # 旧中间筛选造成的正确答案丢失：答案在 36 条底层召回里，却被奖励截池切掉
    lost = [
        {
            "case_id": r["case_id"],
            "kind": r["kind"],
            "expected": r["gold"],
            "cut": [
                doc_id
                for doc_id in r["legacy"]["cut_by_intermediate_filter"]
                if doc_id in set(r["gold"])
            ],
        }
        for r in records
        if set(r["legacy"]["cut_by_intermediate_filter"]) & set(r["gold"])
    ]
    out["lost_by_intermediate_filter"] = {"count": len(lost), "cases": lost}
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_kind.setdefault(str(r["kind"]), []).append(r)
    for kind, rows in sorted(by_kind.items()):
        n = len(rows) or 1
        by_kind[kind] = {
            "n": len(rows),
            "legacy_recall@1": round(sum(1 for r in rows if r["legacy"]["hit@1"]) / n, 4),
            "legacy_recall@k": round(
                sum(1 for r in rows if r["legacy"][f"hit@{limit}"]) / n, 4
            ),
            "relevance_recall@1": round(sum(1 for r in rows if r["relevance"]["hit@1"]) / n, 4),
            "relevance_recall@k": round(
                sum(1 for r in rows if r["relevance"][f"hit@{limit}"]) / n, 4
            ),
        }
    out["by_kind"] = by_kind
    return out


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="旧双层 vs 新纯相关性排序对照")
    parser.add_argument(
        "--scenarios",
        default="handmade",
        help="evals/scenarios 下的语料名（handmade / holdout / out_of_topic）",
    )
    parser.add_argument("--k", type=int, default=LEGACY_RETURN_LIMIT)
    parser.add_argument("--out", default="", help="把逐条记录与汇总写到文件")
    parser.add_argument(
        "--log-run",
        action="store_true",
        help="同时写进 evals/runs/ranking-compare/<run_id>/（cases.jsonl 为唯一事实源）",
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    path = EVALS_DIR / "scenarios" / f"{args.scenarios}.jsonl"
    loaded = build_scenarios(path=path)
    result = compare(loaded.corpus, limit=args.k)
    payload = {"scenarios": args.scenarios, **result}
    if args.log_run:
        run = experiment_log.start_run(
            name="ranking-compare",
            config={
                "scenarios": args.scenarios,
                "now": FIXED_NOW.isoformat(),
                "ranking": params.RANKING.as_dict(),
                "limits": params.LIMITS.as_dict(),
            },
        )
        for row in result["records"]:
            experiment_log.log_case(
                run,
                job="ranking",
                case_id=row["case_id"],
                query=row["query"],
                gold=row["gold"],
                ranked=row["relevance"]["ranked"],
                candidates=row["legacy"]["candidate_pool"],
                scores=[row["relevance"]["scores"].get(d, 0.0) for d in row["relevance"]["ranked"]],
                extra={
                    "kind": row["kind"],
                    "legacy": row["legacy"],
                    "relevance_fixed_pool": row["relevance_fixed_pool"],
                    "config": row["config"],
                },
            )
        payload["run_id"] = run.run_id
        payload["run_dir"] = str(run.directory)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.out:
        out = Path(args.out)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
