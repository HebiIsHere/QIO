"""Tool router: selective tool spec delivery per PLANNING step.

As the tool count grows, delivering every spec inflates the context and
confuses model selection. The router keeps core tools always visible and
ranks the rest by similarity to the current query (embedding when
available, token overlap otherwise), capped at top_n.
"""

from __future__ import annotations

from agent.adapters.base import ToolSpec
from agent.selector.tokenize import tokenize

CORE_TOOLS = [
    "memory_search",
    "switch_topic",
    "create_topic",
    "await_task",
    "read_task_result",
]

DEFAULT_TOP_N = 20
DEV_HINT_WORDS = {"工具", "开发", "创建工具", "写一个", "做一个工具", "自动化"}


class ToolRouter:
    def __init__(self, top_n: int = DEFAULT_TOP_N, embedding=None) -> None:
        self.top_n = max(5, top_n)
        self.embedding = embedding

    # -- similarity -------------------------------------------------------

    def _similarity(self, a: str, b: str) -> float:
        if self.embedding is not None and self.embedding.available():
            vecs = self.embedding.embed_texts([a, b])
            if vecs is not None:
                import numpy as np

                va, vb = vecs[0], vecs[1]
                denom = float(np.linalg.norm(va)) * float(np.linalg.norm(vb)) or 1e-9
                return float(np.dot(va, vb) / denom)
        ta, tb = set(tokenize(a)), set(tokenize(b))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta)

    # -- routing ----------------------------------------------------------

    def route(self, query: str, specs: list[ToolSpec]) -> list[ToolSpec]:
        by_name = {s.name: s for s in specs}
        core = [by_name[c] for c in CORE_TOOLS if c in by_name]
        others = [s for s in specs if s.name not in CORE_TOOLS]
        if not query.strip():
            remaining = self.top_n - len(core)
            return core + others[: max(0, remaining)]
        scored = []
        for spec in others:
            text = f"{spec.name} {spec.description}"
            score = self._similarity(query, text)
            # 开发意图提示词：确保开发工具链可见
            if any(w in query for w in DEV_HINT_WORDS) and spec.name in (
                "create_tool",
                "dev_write_file",
                "dev_read_file",
                "dev_run_tests",
                "dev_submit_tool",
            ):
                score += 0.5
            scored.append((score, spec))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        remaining = self.top_n - len(core)
        return core + [spec for _, spec in scored[: max(0, remaining)]]
