"""Tool router: selective tool spec delivery per PLANNING step.

Design:
- always-on runtime core tools stay visible;
- conditionally-meaningful tools (task tools / web search) are exposed only
  when their condition holds (pending tasks; web intent or capability);
- the rest are ranked by similarity to the query, capped at top_n.

Embedding performance: the query is embedded ONCE per planning step, and tool
description embeddings are computed in ONE batch and cached (keyed by backend
identity + text), so a second route with the same tools re-embeds nothing.

契约 4（响应性）：路由的**结构判断**（core/条件工具、意图加权、截断）是纯内存
操作，留在事件循环上；embed_texts 批量与余弦等**纯 CPU 计算**经有界执行器
（agent/tools/blocking.py，max_workers=4）移出事件循环 —— 慢的路由不再把
健康检查 / 取消 / 发消息全部堵死。对应入口是 route_async；
同步 route() 行为保持不变（既有调用方与测试不受影响）。

并发与过时结果：嵌入线程只**计算**新向量，不写共享缓存 _tool_vecs；
缓存的写提交在事件循环侧完成，且提交前核对 backend 身份 —— 失效/换后端/
被取消之后晚到的旧结果一律丢弃，不覆盖缓存终态。数据库连接、审批调用、
事件提交等有状态操作不进线程（契约边界）。
"""

from __future__ import annotations

from typing import Any

from agent.adapters.base import ToolSpec
from agent.selector.tokenize import tokenize

# 真正 runtime core：长期存在
CORE_TOOLS = [
    "memory_search",
    "continue_from_fragment",
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

# 开发意图下稳定提前的工具链（与原内联元组等价）
DEV_TOOLCHAIN = (
    "create_tool",
    "dev_list_files",
    "dev_write_file",
    "dev_read_file",
    "dev_run_tests",
    "dev_submit_tool",
)

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

    # -- structural split（纯结构判断，事件循环上执行） --------------------

    def _split(
        self,
        query: str,
        specs: list[ToolSpec],
        *,
        pending_tasks: bool,
        web_allowed: bool,
    ) -> tuple[list[ToolSpec], list[ToolSpec], list[ToolSpec]]:
        """core / 条件工具 / 其余候选。纯结构判断，不做任何计算。"""
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
        return core, conditional, others

    def _empty_query_result(
        self, core: list[ToolSpec], conditional: list[ToolSpec], others: list[ToolSpec]
    ) -> list[ToolSpec]:
        remaining = self.top_n - len(core)
        return core + (conditional + others)[: max(0, remaining)]

    def _finish_ranking(
        self,
        query: str,
        scored: list[tuple[float, ToolSpec]],
        core: list[ToolSpec],
        conditional: list[ToolSpec],
    ) -> list[ToolSpec]:
        """意图加权 + 排序 + top_n 截断（结构判断，事件循环上执行）。"""
        wants = wants_web(query)
        boosted: list[tuple[float, ToolSpec]] = []
        for score, spec in scored:
            if wants and spec.name == "web_search":
                score += 0.6  # 用户明确要联网 → 稳定带出
            elif any(w in query for w in DEV_HINT_WORDS) and spec.name in DEV_TOOLCHAIN:
                score += 0.5
            boosted.append((score, spec))
        remaining = self.top_n - len(core) - len(conditional)
        ranked = [spec for _, spec in sorted(boosted, key=lambda p: (-p[0], p[1].name))]
        return core + conditional + ranked[: max(0, remaining)]

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

    # -- routing（同步入口，行为不变） -------------------------------------

    def route(
        self,
        query: str,
        specs: list[ToolSpec],
        *,
        pending_tasks: bool = False,
        web_allowed: bool = True,
    ) -> list[ToolSpec]:
        core, conditional, others = self._split(
            query, specs, pending_tasks=pending_tasks, web_allowed=web_allowed
        )
        if not query.strip():
            return self._empty_query_result(core, conditional, others)

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

        return self._finish_ranking(query, list(zip(scores, others)), core, conditional)

    # -- routing（异步入口：纯 CPU 计算经有界执行器） -----------------------

    async def route_async(
        self,
        query: str,
        specs: list[ToolSpec],
        *,
        pending_tasks: bool = False,
        web_allowed: bool = True,
    ) -> list[ToolSpec]:
        """与 route() 同语义；嵌入批量/余弦经有界执行器在非事件循环线程上算。

        - 结构判断（core/条件/加权/截断）留在事件循环：纯结构、纯内存；
        - embed_texts 批量 + query 向量 + 余弦打分合并为**一个**线程任务，
          线程内只算新向量、不读不写共享缓存（快照传入）；
        - 缓存写提交回到事件循环侧：提交前核对 backend 身份与 key，
          过时（invalidate/换后端）与被取消后晚到的旧结果一律丢弃；
        - 不把 db 连接、审批调用、事件提交搬进线程。
        """
        core, conditional, others = self._split(
            query, specs, pending_tasks=pending_tasks, web_allowed=web_allowed
        )
        if not query.strip():
            return self._empty_query_result(core, conditional, others)

        texts = [f"{s.name} {s.description}" for s in others]
        if not (self._embed_available() and texts):
            scored = [(self._token_similarity(query, t), s) for s, t in zip(others, texts)]
            return self._finish_ranking(query, scored, core, conditional)

        import asyncio as _asyncio

        from agent.tools.blocking import get_blocking_executor

        loop = _asyncio.get_running_loop()
        executor = get_blocking_executor()
        bkey = self._backend_key()
        emb = self.embedding  # 快照：线程期间 backend 可能被换掉
        missing_idx = [i for i, t in enumerate(texts) if (bkey, t) not in self._tool_vecs]
        cached_vecs = [self._tool_vecs.get((bkey, t)) for t in texts]

        def _embed_and_score() -> tuple[Any, list[float]]:
            """线程内只算：新向量 + query 向量 + 分数。不碰共享缓存。"""
            new_vecs = None
            if missing_idx:
                new_vecs = emb.embed_texts([texts[i] for i in missing_idx])
            qvecs = emb.embed_texts([query])
            qv = None if qvecs is None else qvecs[0]
            scores: list[float] = []
            pos_of = {idx: pos for pos, idx in enumerate(missing_idx)}
            for i, _text in enumerate(texts):
                tv = cached_vecs[i]
                if tv is None and new_vecs is not None and i in pos_of:
                    pos = pos_of[i]
                    if pos < len(new_vecs):
                        tv = new_vecs[pos]
                scores.append(
                    self._cosine(qv, tv) if qv is not None and tv is not None else 0.0
                )
            return new_vecs, scores

        new_vecs, scores = await loop.run_in_executor(executor, _embed_and_score)

        # ——以下都在事件循环上——
        # 缓存写提交只在事件循环侧；过时结果（backend 已换 / 缓存已失效）不写。
        if self.embedding is emb and self._backend_key() == bkey and new_vecs is not None:
            for i, v in zip(missing_idx, new_vecs):
                # setdefault：并发调用下同 key 晚到者不覆盖已提交的值
                self._tool_vecs.setdefault((bkey, texts[i]), v)

        return self._finish_ranking(query, list(zip(scores, others)), core, conditional)
