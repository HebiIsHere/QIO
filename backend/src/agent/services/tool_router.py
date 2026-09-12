"""Tool router: selective tool spec delivery per PLANNING step.

Design:
- always-on runtime core tools stay visible;
- conditionally-meaningful tools (task tools / web search) are exposed only
  when their condition holds (pending tasks; web intent or capability);
- the rest are ranked by similarity to the query, capped at top_n.

Embedding performance: the query is embedded ONCE per planning step, and tool
description embeddings are computed in ONE batch and cached (keyed by backend
identity + text), so a second route with the same tools re-embeds nothing.
"""

from __future__ import annotations

from typing import Any

from agent.adapters.base import ToolSpec
from agent.selector.tokenize import tokenize

# 真正 runtime core：长期存在
CORE_TOOLS = [
    "memory_search",
    "switch_topic",
    "create_topic",
]

# 条件暴露：仅在对应条件成立时可见
CONDITIONAL_TOOLS = {
    "await_task": "pending_tasks",
    "read_task_result": "pending_tasks",
}

DEFAULT_TOP_N = 20
DEV_HINT_WORDS = {"工具", "开发", "创建工具", "写一个", "做一个工具", "自动化"}

# 明确的联网意图（刻意保持精简；不作为唯一机制——router 排序同样能带出 web_search）
WEB_INTENT_WORDS = {"联网", "搜索", "搜一下", "查一下", "网上", "最新", "实时", "web", "search"}


def wants_web(query: str) -> bool:
    q = (query or "").lower()
    return any(w in q for w in WEB_INTENT_WORDS)


class ToolRouter:
    def __init__(self, top_n: int = DEFAULT_TOP_N, embedding=None) -> None:
        self.top_n = max(5, top_n)
        self.embedding = embedding
        self._tool_vecs: dict[tuple[str, str], Any] = {}

    # -- embedding cache --------------------------------------------------

    def _backend_key(self) -> str:
        e = self.embedding
        if e is None:
            return "none"
        return "|".join(
            str(getattr(e, attr, "")) for attr in ("name", "model_name", "dims")
        )

    def invalidate(self) -> None:
        """Tool definitions or backend changed → drop cached vectors."""
        self._tool_vecs.clear()

    def _embed_available(self) -> bool:
        return self.embedding is not None and self.embedding.available()

    # -- similarity -------------------------------------------------------

    @staticmethod
    def _cosine(va: Any, vb: Any) -> float:
        import numpy as np

        denom = float(np.linalg.norm(va)) * float(np.linalg.norm(vb)) or 1e-9
        return float(np.dot(va, vb) / denom)

    def _token_similarity(self, a: str, b: str) -> float:
        ta, tb = set(tokenize(a)), set(tokenize(b))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta)

    # -- routing ----------------------------------------------------------

    def route(
        self,
        query: str,
        specs: list[ToolSpec],
        *,
        pending_tasks: bool = False,
        web_allowed: bool = True,
    ) -> list[ToolSpec]:
        conditions = {"pending_tasks": pending_tasks}
        by_name = {s.name: s for s in specs}
        core = [by_name[c] for c in CORE_TOOLS if c in by_name]
        # 条件工具：条件成立才进入候选；不成立则完全不出现（不进 others）
        conditional_names = set(CONDITIONAL_TOOLS)
        conditional = [
            s
            for name, cond in CONDITIONAL_TOOLS.items()
            if (s := by_name.get(name)) is not None and conditions.get(cond, False)
        ]
        core_names = {s.name for s in core}
        others = [
            s for s in specs if s.name not in core_names and s.name not in conditional_names
        ]
        if not web_allowed:
            others = [s for s in others if s.name != "web_search"]

        if not query.strip():
            remaining = self.top_n - len(core)
            return core + (conditional + others)[: max(0, remaining)]

        texts = [f"{s.name} {s.description}" for s in others]
        scores: list[float] = []
        if self._embed_available() and texts:
            # 工具描述：批量、缓存（未缓存部分一次算完）
            bkey = self._backend_key()
            missing = [
                (i, t) for i, t in enumerate(texts) if (bkey, t) not in self._tool_vecs
            ]
            if missing:
                vecs = self.embedding.embed_texts([t for _, t in missing])
                if vecs is not None:
                    for (i, t), v in zip(missing, vecs):
                        self._tool_vecs[(bkey, t)] = v
            # query：每次 planning 只算一次
            qvecs = self.embedding.embed_texts([query])
            qv = None if qvecs is None else qvecs[0]
            for text in texts:
                tv = self._tool_vecs.get((bkey, text))
                scores.append(self._cosine(qv, tv) if qv is not None and tv is not None else 0.0)
        else:
            scores = [self._token_similarity(query, t) for t in texts]

        scored = list(zip(scores, others))
        wants = wants_web(query)
        boosted: list[tuple[float, ToolSpec]] = []
        for score, spec in scored:
            if wants and spec.name == "web_search":
                score += 0.6  # 用户明确要联网 → 稳定带出
            elif any(w in query for w in DEV_HINT_WORDS) and spec.name in (
                "create_tool",
                "dev_list_files",
                "dev_write_file",
                "dev_read_file",
                "dev_run_tests",
                "dev_submit_tool",
            ):
                score += 0.5
            boosted.append((score, spec))

        remaining = self.top_n - len(core) - len(conditional)
        ranked = [spec for _, spec in sorted(boosted, key=lambda p: (-p[0], p[1].name))]
        return core + conditional + ranked[: max(0, remaining)]
