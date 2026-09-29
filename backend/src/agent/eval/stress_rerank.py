"""候选重排的三种做法，用于测量「重排到底能带来多少收益」。

- `IdentityRerank`：原序（对照组，验证实现没出错）；
- `LocalVectorRerank`：本地双编码器重排 —— 与召回同一套算法，预期收益接近零；
- `JevRerank`：一次请求对每条候选问一个"是否回答了这个问题"，按概率重排。

重排只能改顺序、不能改候选集，所以**候选集合本身**在三种做法下必须完全相同。
（注意：recall@5 不是不变量 —— 它看的是前 5 名，重排会改变谁落进前 5；
真正的不变量是"返回的 id 多重集与输入一致"。第一次实现时我把它当成不变量，
结果它反而帮我发现了"问题没有指向具体候选"这个 bug。）
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class Candidate:
    doc_id: str
    score: float
    text: str = ""


class IdentityRerank:
    name = "identity"

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        return [c.doc_id for c in candidates]


class LocalVectorRerank:
    """用查询向量与候选文本的余弦重新排序（与召回同算法，作为对照）。"""

    name = "local_vector"

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.calls = 0

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        import numpy as np

        if not candidates:
            return []
        vectors = self.backend.embed_texts([query] + [c.text for c in candidates])
        self.calls += 1
        if vectors is None:
            return [c.doc_id for c in candidates]
        query_vec = vectors[0]
        matrix = vectors[1:]
        scores = matrix @ query_vec
        order = np.argsort(-scores)
        return [candidates[i].doc_id for i in order]


class JevRerank:
    """一次请求带 N 个 noul 问题；按"是否回答了这个问题"的概率重排。"""

    name = "jev"
    # 每个问题必须**显式引用**对应候选（TypeSafe 用反引号指向 state 字段），
    # 否则 N 个问题问的是同一句话，模型给出同一批概率，排序退化成按 id 排 ——
    # 这个 bug 是靠"recall@5 必须不变"这条哨兵指标抓出来的。
    PROMPT = "`candidates.c{i}` 这条候选内容是否真的回答了 `question`？只按语义相关性判断，不要因为用词相近就判是。"

    def __init__(self, client: Any, *, max_candidates: int = 12) -> None:
        self.client = client
        self.max_candidates = max_candidates
        self.calls = 0
        self.failures = 0
        self.latencies_ms: list[float] = []

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        chosen = list(candidates)[: self.max_candidates]
        if not chosen:
            return []
        questions = {
            f"c{i}": {
                "type": "noul",
                "instructions": self.PROMPT.replace("{i}", str(i)),
            }
            for i in range(len(chosen))
        }
        state = {
            "question": query,
            "candidates": {f"c{i}": c.text[:600] for i, c in enumerate(chosen)},
        }
        try:
            answer = self.client.ask(state, questions)
        except Exception:
            self.failures += 1
            # 回退必须保留**全部**原始候选，不能只还前 max_candidates 条 ——
            # 否则云端一超时，尾部候选就凭空消失了。
            return [c.doc_id for c in candidates]
        self.calls += 1
        self.latencies_ms.append(answer.latency_ms)
        scored = [
            (answer.noul(f"c{i}", default=0.0), c.doc_id) for i, c in enumerate(chosen)
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        head = [doc_id for _, doc_id in scored]
        tail = [c.doc_id for c in candidates[len(chosen):]]
        return head + tail


class ConditionalRerank:
    """只在"排序不可信"时才调重排：顶部两条的相对分差小于阈值就触发。

    判据只用召回阶段已有的分数，不产生额外调用 —— 这是它能把成本压下来的原因。
    """

    name = "conditional"

    def __init__(self, reranker: Any, *, relative_gap: float = 0.15) -> None:
        self.reranker = reranker
        self.relative_gap = relative_gap
        self.triggered = 0
        self.skipped = 0

    def should_rerank(self, candidates: Sequence[Candidate]) -> bool:
        if len(candidates) < 2:
            return False
        first, second = candidates[0].score, candidates[1].score
        denom = abs(first) if abs(first) > 1e-9 else 1.0
        return (first - second) / denom < self.relative_gap

    def rerank(self, query: str, candidates: Sequence[Candidate]) -> list[str]:
        if not self.should_rerank(candidates):
            self.skipped += 1
            return [c.doc_id for c in candidates]
        self.triggered += 1
        return self.reranker.rerank(query, candidates)


def run_rerank(
    reranker: Any,
    cases: Sequence[Any],
    candidate_lists: Sequence[Sequence[Candidate]],
    *,
    k: int = 5,
) -> tuple[list[list[str]], list[float]]:
    """对每条用例应用一次重排，返回排名列表与耗时。"""
    ranked: list[list[str]] = []
    latencies: list[float] = []
    for case, candidates in zip(cases, candidate_lists):
        started = time.perf_counter()
        ranked.append(reranker.rerank(case.query, candidates))
        latencies.append((time.perf_counter() - started) * 1000)
    return ranked, latencies
