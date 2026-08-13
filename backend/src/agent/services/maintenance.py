"""M12 offline maintenance: scheduler + contradiction scan + dreaming + tool candidates.

Runs in the background (periodic or manual). All failures are isolated:
a broken maintenance pass must never affect the main loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agent.knowledge.lifecycle import HIGH_IMPACT_CATEGORIES, KnowledgeService
from agent.selector.tokenize import tokenize
from agent.prompts import DREAMING_PROMPT, TOOL_AUTOMATION_PROMPT

logger = logging.getLogger(__name__)

NEGATION_WORDS = ["不是", "不对", "其实", "并没有", "从来", "根本", "反而", "错了"]
CONFIDENCE_STEP = 0.2
MAX_DREAMING_CANDIDATES = 10
CLUSTER_MIN_SIZE = 3
EMBED_SIMILARITY_THRESHOLD = 0.85
TOKEN_OVERLAP_THRESHOLD = 0.6
SCAN_LIMIT = 500


def _token_overlap(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _similarity(ctx, a: str, b: str) -> float:
    if ctx.embedding is not None and ctx.embedding.available():
        vecs = ctx.embedding.embed_texts([a, b])
        if vecs is not None:
            import numpy as np

            va, vb = vecs[0], vecs[1]
            denom = np.linalg.norm(va) * np.linalg.norm(vb) or 1e-9
            return float(np.dot(va, vb) / denom)
    return _token_overlap(a, b)


# ---------------------------------------------------------------------------
# 1) implicit feedback: contradiction scan
# ---------------------------------------------------------------------------

async def scan_contradictions(ctx) -> dict:
    rows = ctx.conn.execute(
        "SELECT content FROM messages WHERE role = 'user' AND content != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (SCAN_LIMIT,),
    ).fetchall()
    knowledge = ctx.conn.execute(
        "SELECT id, content, category, confidence FROM knowledge WHERE state = 'active'"
    ).fetchall()
    hits: dict[str, dict] = {}
    for msg in rows:
        text = msg["content"] or ""
        if not any(w in text for w in NEGATION_WORDS):
            continue
        msg_tokens = set(tokenize(text))
        for k in knowledge:
            ktokens = set(tokenize(k["content"] or ""))
            if len(msg_tokens & ktokens) >= 2:
                hits.setdefault(k["id"], {"id": k["id"], "content": k["content"], "category": k["category"]})
    applied = 0
    for hit in hits.values():
        try:
            ks = KnowledgeService(ctx.conn)
            item = ks.get(hit["id"])
            if item is None:
                continue
            new_conf = max(0.1, (item.confidence or 0.9) - CONFIDENCE_STEP)
            ctx.conn.execute(
                "UPDATE knowledge SET confidence = ? WHERE id = ?", (new_conf, hit["id"])
            )
            ctx.conn.commit()
            applied += 1
            if item.category in HIGH_IMPACT_CATEGORIES:
                _request_approval(
                    ctx,
                    "high_impact_knowledge",
                    {
                        "knowledge_id": hit["id"],
                        "content": hit["content"],
                        "action": "degrade",
                        "reason": "用户近期表达了反驳，置信度已下调，请确认",
                    },
                )
        except Exception:  # noqa: BLE001 - isolated
            logger.warning("contradiction scan item failed", exc_info=True)
    return {"contradiction_hits": applied}


# ---------------------------------------------------------------------------
# 2) dreaming: subagent analysis + candidate landing
# ---------------------------------------------------------------------------

def _request_approval(ctx, kind: str, payload: dict) -> None:
    """Fire-and-forget approval; applied asynchronously when approved."""

    async def _wait_and_apply():
        try:
            result = await ctx.approvals.request(kind, payload)
            if result.decision == "approved":
                await _apply_approval(ctx, kind, payload)
        except Exception:  # noqa: BLE001
            logger.warning("approval handling failed for %s", kind, exc_info=True)

    asyncio.create_task(_wait_and_apply())


async def _apply_approval(ctx, kind: str, payload: dict) -> None:
    if kind == "tool_candidate":
        request = payload.get("description") or payload.get("name") or "自动工具候选"
        task = ctx.dev_workspaces.create(request)
        logger.info("tool candidate approved -> dev workspace %s", task.id)
        return
    if kind != "high_impact_knowledge":
        return
    kid = payload.get("knowledge_id")
    action = payload.get("action")
    new_content = payload.get("new_content")
    ks = KnowledgeService(ctx.conn)
    item = ks.get(kid)
    if item is None:
        return
    if action == "correct" and new_content:
        new_item = ks.create(
            category=item.category, content=new_content,
            node_ids=list(item.node_ids), supersedes_id=item.id,
            provenance={"dream_correct": True},
        )
        ks.submit(new_item.id)
        ks.verify(new_item.id, verified_by="user")
        ks.activate(new_item.id)
    elif action in ("expire", "degrade", "merge"):
        ks.revoke(item.id)


def _parse_candidates(text: str) -> list[dict]:
    import re

    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = match.group(1) if match else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    return data.get("candidates", []) if isinstance(data, dict) else []


async def run_dreaming(ctx) -> dict:
    knowledge = ctx.conn.execute(
        "SELECT id, category, content FROM knowledge WHERE state = 'active' "
        "ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    if not knowledge:
        return {"dreaming_candidates": 0}
    summaries = ctx.conn.execute(
        "SELECT f.summary FROM memory_index mi JOIN fragments f ON f.id = mi.fragment_id "
        "WHERE f.summary IS NOT NULL AND f.summary != '' "
        "ORDER BY f.created_at DESC LIMIT 10"
    ).fetchall()
    prompt = DREAMING_PROMPT.format(
        knowledge="\n".join(f"- [{k['id']}] ({k['category']}) {k['content']}" for k in knowledge),
        summaries="\n".join(f"- {s['summary'][:200]}" for s in summaries) or "(无)",
    )
    try:
        adapter = await ctx.build_adapter()
        if adapter is None:
            return {"dreaming_candidates": 0, "error": "no adapter"}
        from agent.core.loop import AgentLoop
        from agent.tools.registry import ToolRegistry

        loop = AgentLoop(adapter, ToolRegistry(), ctx.bus, max_iterations=1, token_budget=40_000)
        result = await loop.run(prompt)
        candidates = _parse_candidates(result.final_content or "")[:MAX_DREAMING_CANDIDATES]
    except Exception as exc:  # noqa: BLE001 - isolated
        logger.warning("dreaming analysis failed: %s", exc)
        return {"dreaming_candidates": 0, "error": str(exc)[:200]}
    applied = 0
    ks = KnowledgeService(ctx.conn)
    for cand in candidates:
        kid = cand.get("knowledge_id")
        action = cand.get("action")
        item = ks.get(kid) if kid else None
        if item is None or action not in ("correct", "merge", "expire"):
            continue
        applied += 1
        if item.category in HIGH_IMPACT_CATEGORIES:
            _request_approval(
                ctx,
                "high_impact_knowledge",
                {
                    "knowledge_id": kid,
                    "content": item.content,
                    "action": "correct" if action in ("correct", "merge") else "expire",
                    "new_content": cand.get("new_content"),
                    "reason": cand.get("reason", "Dreaming 维护建议"),
                },
            )
            continue
        if action in ("correct", "merge") and cand.get("new_content"):
            new_item = ks.create(
                category=item.category, content=cand["new_content"],
                node_ids=list(item.node_ids), supersedes_id=item.id,
                provenance={"dream_correct": True},
            )
            ks.submit(new_item.id)
            ks.verify(new_item.id, verified_by="system")
            ks.activate(new_item.id)
        else:
            ks.revoke(item.id)
    return {"dreaming_candidates": applied}


# ---------------------------------------------------------------------------
# 3) trajectory -> tool candidates
# ---------------------------------------------------------------------------

async def mine_tool_candidates(ctx) -> dict:
    rows = ctx.conn.execute(
        "SELECT content FROM messages WHERE role = 'user' AND content != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (SCAN_LIMIT,),
    ).fetchall()
    texts = [r["content"] for r in rows]
    if len(texts) < CLUSTER_MIN_SIZE:
        return {"tool_candidates": 0}
    clusters: list[list[str]] = []
    for text in texts:
        placed = False
        for cluster in clusters:
            if _similarity(ctx, text, cluster[0]) >= (
                EMBED_SIMILARITY_THRESHOLD if ctx.embedding and ctx.embedding.available()
                else TOKEN_OVERLAP_THRESHOLD
            ):
                cluster.append(text)
                placed = True
                break
        if not placed:
            clusters.append([text])
    generated = 0
    for i, cluster in enumerate(clusters):
        if len(cluster) < CLUSTER_MIN_SIZE:
            continue
        representative = max(cluster, key=len)
        try:
            draft = await _generate_draft(ctx, cluster)
        except Exception:  # noqa: BLE001
            draft = {
                "name": f"auto_tool_{i}",
                "description": representative[:200],
                "parameters": {},
                "example": representative[:200],
            }
        _request_approval(
            ctx,
            "tool_candidate",
            {
                "name": draft.get("name", f"auto_tool_{i}"),
                "description": draft.get("description", "")[:300],
                "parameters": draft.get("parameters", {}),
                "example": representative[:300],
                "source_count": len(cluster),
            },
        )
        generated += 1
    return {"tool_candidates": generated}


async def _generate_draft(ctx, cluster: list[str]) -> dict:
    adapter = await ctx.build_adapter()
    if adapter is None:
        raise RuntimeError("no adapter")
    from agent.adapters.base import ChatMessage
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    prompt = TOOL_AUTOMATION_PROMPT + "\n".join(f"- {c[:200]}" for c in cluster)
    loop = AgentLoop(adapter, ToolRegistry(), ctx.bus, max_iterations=1, token_budget=20_000)
    result = await loop.run(prompt)
    import re

    match = re.search(r"\{.*\}", result.final_content or "", re.DOTALL)
    if not match:
        raise RuntimeError("no json")
    return json.loads(match.group(0))


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------

class MaintenanceScheduler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            interval = self.ctx.settings_store.get_int("maintenance.interval_hours", 24)
            await asyncio.sleep(max(1, interval) * 3600)
            enabled = self.ctx.settings_store.get("maintenance.enabled", "true") != "false"
            if not enabled:
                continue
            if self.ctx._active_loop is not None:
                continue  # idle only
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                logger.warning("maintenance pass failed", exc_info=True)

    async def run_once(self) -> dict:
        if self._running:
            return {"ok": False, "reason": "already_running"}
        self._running = True
        try:
            results: dict[str, Any] = {"ok": True}
            results.update(await scan_contradictions(self.ctx))
            results.update(await run_dreaming(self.ctx))
            results.update(await mine_tool_candidates(self.ctx))
            return results
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance run failed: %s", exc)
            return {"ok": False, "reason": str(exc)[:200]}
        finally:
            self._running = False
