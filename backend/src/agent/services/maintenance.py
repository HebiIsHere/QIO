"""M12 offline maintenance: scheduler + contradiction scan + dreaming + tool candidates.

Runs in the background (periodic or manual). All failures are isolated:
a broken maintenance pass must never affect the main loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent.knowledge.lifecycle import HIGH_IMPACT_CATEGORIES, KnowledgeService
from agent.selector.tokenize import tokenize
from agent import prompts
from agent.prompts import TOOL_AUTOMATION_PROMPT
from agent.services.background import (
    BackgroundClosed,
    BackgroundHandle,
    BackgroundTasks,
)

logger = logging.getLogger(__name__)

# 维护「跑一轮」在注册表里的唯一名字：手动与周期共用它 ⇒ 已有一轮在跑时合并，
# 不会并发执行两份同样的工作（F06 的并发规则）。
RUN_NAME = "maintenance:run"

# 关闭报告里的阶段名（与 services/lifecycle.py 同名常量一致；定义在这里避免
# maintenance 反向依赖 lifecycle）。
PHASE_MAINTENANCE_STOPPED = "maintenance_stopped"
PHASE_MAINTENANCE_UNFINISHED = "maintenance_unfinished"

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
    """旧入口（保留）：有嵌入就用余弦，否则词元重合。"""
    embedding = ctx.embedding if (ctx.embedding is not None and ctx.embedding.available()) else None
    return _similarity_with(embedding, a, b)


def _similarity_with(embedding, a: str, b: str) -> float:
    """**纯计算**：两段文本的相似度（不碰数据库、不碰 ctx）。

    有嵌入时用它算余弦，否则退回词元重合 —— 与旧 `_similarity` 逐字一致，
    只是把「拿嵌入」这一步挪到输入读取侧，好让本函数能进工作线程。
    """
    if embedding is not None:
        vecs = embedding.embed_texts([a, b])
        if vecs is not None:
            import numpy as np

            va, vb = vecs[0], vecs[1]
            denom = float(np.linalg.norm(va)) * float(np.linalg.norm(vb)) or 1e-9
            return float(np.dot(va, vb) / denom)
    return _token_overlap(a, b)


# ---------------------------------------------------------------------------
# 拆分约定（契约 WS3 §3，task-12）
#
# 维护这一轮过去整段跑在事件循环上（实测 460ms~5.4s，全部花在「工具候选聚类」
# 里逐条消息的嵌入上）。现在每个重活都按同一套拆：
#
#   plan_*   输入读取：循环侧、**只读**数据库，产出纯数据
#   *_compute 纯计算：不碰 ctx / 数据库，可交给 `ctx.heavy` 的工作线程
#   commit_* 结果提交：回到循环侧写库 / 发审批；提交前校验「代次」
#
# 「代次」= 这一轮维护开始时 `ctx.turns.active` 的引用。计算期间用户开始了新一轮
# （引用变了）就丢弃这次结果：过时的维护结果不落地，也不再花一次模型调用。
# 模型调用（`await`）始终留在循环侧，**不进线程**。
# ---------------------------------------------------------------------------


def _maintenance_generation(ctx):
    """这一轮维护开始时的代次：当前主 turn 的引用（没有主 turn 就是 None）。"""
    return getattr(getattr(ctx, "turns", None), "active", None)


async def _run_heavy(ctx, fn, *args):
    """把纯计算交给 AppContext 的有上限执行器；没有执行器（替身 ctx）就同步跑。"""
    heavy = getattr(ctx, "heavy", None)
    if heavy is None:
        return fn(*args)
    return await heavy.run(fn, *args)


# ---------------------------------------------------------------------------
# 1) implicit feedback: contradiction scan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContradictionPlan:
    """矛盾扫描的「输入」：近期用户消息 + 活跃知识（纯数据）。"""

    messages: list[str]
    knowledge: list[dict]
    generation: object


def plan_contradiction_scan(ctx) -> ContradictionPlan:
    """输入读取（循环侧，只读）。"""
    rows = ctx.conn.execute(
        "SELECT content FROM messages WHERE role = 'user' AND content != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (SCAN_LIMIT,),
    ).fetchall()
    knowledge = ctx.conn.execute(
        "SELECT id, content, category, confidence FROM knowledge WHERE state = 'active'"
    ).fetchall()
    return ContradictionPlan(
        messages=[m["content"] or "" for m in rows],
        knowledge=[
            {"id": k["id"], "content": k["content"] or "", "category": k["category"]}
            for k in knowledge
        ],
        generation=_maintenance_generation(ctx),
    )


def find_contradiction_hits(messages: list[str], knowledge: list[dict]) -> dict[str, dict]:
    """**纯计算**：带否定词的近期消息命中哪些活跃知识（词元重合 ≥ 2）。

    判定口径与旧实现逐字一致；返回的键/值与旧实现相同，便于提交侧原样使用。
    """
    hits: dict[str, dict] = {}
    for text in messages:
        if not any(w in text for w in NEGATION_WORDS):
            continue
        msg_tokens = set(tokenize(text))
        for k in knowledge:
            ktokens = set(tokenize(k["content"] or ""))
            if len(msg_tokens & ktokens) >= 2:
                hits.setdefault(
                    k["id"],
                    {"id": k["id"], "content": k["content"], "category": k["category"]},
                )
    return hits


async def commit_contradiction_scan(ctx, plan: ContradictionPlan, hits: dict) -> int:
    """结果提交（循环侧）：下调置信度、必要时请求审批。"""
    if _maintenance_generation(ctx) is not plan.generation:
        logger.info("contradiction scan result discarded: a new turn started meanwhile")
        return 0
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
    return applied


async def scan_contradictions(ctx) -> dict:
    plan = plan_contradiction_scan(ctx)
    hits = await _run_heavy(ctx, find_contradiction_hits, plan.messages, plan.knowledge)
    return {"contradiction_hits": await commit_contradiction_scan(ctx, plan, hits)}


# ---------------------------------------------------------------------------
# 2) dreaming: subagent analysis + candidate landing
# ---------------------------------------------------------------------------

def _request_approval(ctx, kind: str, payload: dict) -> None:
    """发起一个审批等待（异步应用）。

    F06：过去这里是裸的 `asyncio.create_task(...)` —— 句柄没人持有，关闭时既等不到
    也取消不到。现在优先登记进维护调度器的审批注册表（与周期/手动维护同一生命周期）；
    没有调度器（替身 ctx / 离线调用）时退回 create_task，真正进入关闭流程时**拒绝**
    新建并如实留痕，绝不偷偷跑在数据库已经关掉之后。
    """

    async def _wait_and_apply():
        try:
            result = await ctx.approvals.request(kind, payload)
            if result.decision == "approved":
                await _apply_approval(ctx, kind, payload)
        except Exception:  # noqa: BLE001
            logger.warning("approval handling failed for %s", kind, exc_info=True)

    scheduler = getattr(ctx, "maintenance", None)
    if scheduler is not None and hasattr(scheduler, "register_approval_wait"):
        try:
            scheduler.register_approval_wait(kind, _wait_and_apply)
            return
        except BackgroundClosed:
            logger.warning("关闭中：不再新建维护审批等待（%s）", kind)
            return
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
    prompt = prompts.render_dreaming_prompt(
        knowledge="\n".join(f"- [{k['id']}] ({k['category']}) {k['content']}" for k in knowledge),
        summaries="\n".join(f"- {s['summary'][:200]}" for s in summaries) or "(无)",
    )
    try:
        adapter = await ctx.build_adapter()
        if adapter is None:
            return {"dreaming_candidates": 0, "error": "no adapter"}
        from agent.core.loop import AgentLoop
        from agent.credentials.usage import credential_usage_sink
        from agent.tools.registry import ToolRegistry

        loop = AgentLoop(
            adapter,
            ToolRegistry(),
            ctx.bus,
            max_iterations=1,
            token_budget=40_000,
            # 后台分析也是真金白银的调用，同样要记到用的那把钥匙上
            usage_sink=credential_usage_sink(ctx.credentials, adapter),
        )
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

@dataclass(frozen=True)
class ToolClusterPlan:
    """工具候选挖掘的「输入」：消息文本 + 这次用的相似度口径（纯数据）。

    `embedding` 只放**嵌入后端本身**（不是 ctx）：工作线程里只做推理与相似度比较，
    碰不到数据库、也碰不到 AppContext。
    """

    texts: list[str]
    embedding: object | None
    threshold: float
    generation: object


def plan_tool_candidates(ctx) -> ToolClusterPlan | None:
    """输入读取（循环侧，只读）。消息太少（< CLUSTER_MIN_SIZE）时返回 None。"""
    rows = ctx.conn.execute(
        "SELECT content FROM messages WHERE role = 'user' AND content != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (SCAN_LIMIT,),
    ).fetchall()
    texts = [r["content"] for r in rows]
    if len(texts) < CLUSTER_MIN_SIZE:
        return None
    embedding = (
        ctx.embedding if (ctx.embedding is not None and ctx.embedding.available()) else None
    )
    return ToolClusterPlan(
        texts=texts,
        embedding=embedding,
        # 口径与旧实现一致：能嵌入就用嵌入阈值，否则用词元重合阈值
        threshold=(
            EMBED_SIMILARITY_THRESHOLD if embedding is not None else TOKEN_OVERLAP_THRESHOLD
        ),
        generation=_maintenance_generation(ctx),
    )


def cluster_texts(texts: list[str], embedding, threshold: float) -> list[list[int]]:
    """**纯计算**（可交给工作线程）：按「与簇首条相似度 ≥ 阈值」顺序聚类。

    返回的是**下标**而不是文本，提交侧再取原文 —— 这样顺序与旧实现逐字一致，
    而且线程里不搬动大对象。逐条比较会调 `embed_texts`，过去这一步整段跑在
    事件循环上（240 条消息实测 5.4 s）。
    """
    clusters: list[list[int]] = []
    for index, text in enumerate(texts):
        placed = False
        for cluster in clusters:
            if _similarity_with(embedding, text, texts[cluster[0]]) >= threshold:
                cluster.append(index)
                placed = True
                break
        if not placed:
            clusters.append([index])
    return clusters


async def commit_tool_candidates(ctx, plan: ToolClusterPlan, clusters: list[list[int]]) -> int:
    """结果提交（循环侧）：够大的簇生成草案并请求审批。

    模型调用（`_generate_draft` 里的 `await`）留在循环上，不进线程。提交前校验
    代次：计算期间用户开始了新一轮就丢弃 —— 不落审批、也不再花一次模型调用。
    """
    if _maintenance_generation(ctx) is not plan.generation:
        logger.info("tool candidate mining result discarded: a new turn started meanwhile")
        return 0
    generated = 0
    for i, cluster in enumerate(clusters):
        if len(cluster) < CLUSTER_MIN_SIZE:
            continue
        texts = [plan.texts[index] for index in cluster]
        representative = max(texts, key=len)
        try:
            draft = await _generate_draft(ctx, texts)
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
    return generated


async def mine_tool_candidates(ctx) -> dict:
    plan = plan_tool_candidates(ctx)
    if plan is None:
        return {"tool_candidates": 0}
    clusters = await _run_heavy(ctx, cluster_texts, plan.texts, plan.embedding, plan.threshold)
    return {"tool_candidates": await commit_tool_candidates(ctx, plan, clusters)}


async def _generate_draft(ctx, cluster: list[str]) -> dict:
    adapter = await ctx.build_adapter()
    if adapter is None:
        raise RuntimeError("no adapter")
    from agent.adapters.base import ChatMessage
    from agent.core.loop import AgentLoop
    from agent.credentials.usage import credential_usage_sink
    from agent.tools.registry import ToolRegistry

    prompt = TOOL_AUTOMATION_PROMPT + "\n".join(f"- {c[:200]}" for c in cluster)
    loop = AgentLoop(
        adapter,
        ToolRegistry(),
        ctx.bus,
        max_iterations=1,
        token_budget=20_000,
        usage_sink=credential_usage_sink(ctx.credentials, adapter),
    )
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
    """维护的唯一生命周期管理（周期巡检 + 手动触发 + 关闭收尾）。

    F06 的根因是「手动维护没人管」：POST /api/maintenance/run 直接
    asyncio.create_task(run_once())，句柄随手丢掉。于是：

    * 关闭时 stop() 只取消周期循环，**手动那一轮照样在跑**；
    * 那一轮还在写库时，aclose() 已经把 adapter / 数据库按「干净关闭」处理了；
    * 关闭报告里当然也不会有它的名字 —— 「未结束」被吞成了「已完成」。

    修法：把「跑一轮维护」收进**一个入口 + 一份注册表**：

    * `_execute()` 是唯一的执行体，所有失败都在这里被如实记成返回值（不吞异常）；
    * 周期循环与 run_manual() 都经 `_start_run()` 登记，同一个名字（RUN_NAME）
      ⇒ 已有一轮在跑时**合并**到它，绝不并发跑第二份同样的工作；
    * `stop()` 先置「关闭中」（新维护一律拒绝），再取消周期循环、对剩下的执行单元
      有界等待 + 取消，最后用注册表的报告**如实**说「确认结束了没有」；
    * 所有等待都有硬上限（沿用 background.py 的三档预算），不无限等，
      也不靠「提前标 clean」掩盖。
    """

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._running = False
        self._task: asyncio.Task | None = None
        self._closing = False
        # 手动维护 + 周期维护共用的一份后台注册表（关闭时由它给出「确认结束没有」）。
        self._runs = BackgroundTasks(name="maintenance")
        # 维护发起的等待（审批等）：同一生命周期，不能是无人管理的 create_task。
        self._approvals = BackgroundTasks(name="maintenance-approval")
        self._run_handle: BackgroundHandle | None = None
        self._last_result: dict | None = None
        # 一旦某次 stop() 如实报告过「有执行单元未确认结束」，这件事就**不能**被
        # 后来的重复调用抹掉（否则调用方会把整个关闭说成干净）。
        self._reported_unfinished = False

    # -- 状态 -------------------------------------------------------------

    @property
    def closing(self) -> bool:
        """是否已进入关闭流程（此后不再接受新的维护请求）。"""
        return self._closing

    def is_running(self) -> bool:
        """周期调度循环是否在跑（关闭之后必须为 False）。"""
        return self._task is not None and not self._task.done()

    def _inactive_handles(self) -> list[BackgroundHandle]:
        """账上**还没结束**的维护执行单元（手动 + 周期，按对象身份去重）。"""
        seen: dict[int, BackgroundHandle] = {}
        for handle in list(self._runs._tracked()) + (
            [self._run_handle] if self._run_handle is not None else []
        ):
            if handle.done:
                continue
            seen.setdefault(id(handle), handle)
        return list(seen.values())

    def is_busy(self) -> bool:
        """是否有一轮维护正在执行（手动或周期，含「已取消但未结束」的）。"""
        return self._running or bool(self._inactive_handles())

    def last_result(self) -> dict | None:
        """最近一轮维护的结果（手动 / 周期共用；没跑过就是 None）。"""
        return self._last_result

    def register_approval_wait(self, kind: str, factory) -> BackgroundHandle:
        """登记一个「等审批 + 应用结果」的后台等待（同一生命周期）。

        关闭流程已经开始时抛 :class:`BackgroundClosed` —— 调用方（`_request_approval`）
        据此如实放弃这次审批，而不是在数据库/适配器即将释放时偷偷起一个协程。
        `replace=True`：同类审批等待只有最近一次需要被追踪（关闭时不会被漏掉，
        也不会让注册表随维护轮次无限增长）。
        """
        return self._approvals.register(f"{kind}", factory, replace=True)

    def start(self) -> None:
        """启动调度器。

        必须在有 running event loop 的地方调用（FastAPI 的 lifespan startup），
        而不是在 `create_app()` 这种同步构造阶段 —— 那里没有 loop，
        旧代码只能靠 `except RuntimeError: pass` 吞掉异常，
        结果是「后台维护从来没有真正启动」而且没人知道。
        """
        if self._closing:
            # 关闭之后不允许再启动：否则会留下一个「没人会再停它」的循环。
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def next_delay_seconds(self) -> float:
        """下一次巡检的间隔（测试会把它调小以驱动真实循环）。"""
        interval = self.ctx.settings_store.get_int("maintenance.interval_hours", 24)
        return float(max(1, interval) * 3600)

    # -- 发起一轮（唯一的登记入口）----------------------------------------

    def _start_run(
        self, factory: Callable[[], Awaitable[Any]]
    ) -> tuple[BackgroundHandle | None, bool]:
        """登记一轮维护。返回 (句柄, 是否由本次调用启动)。

        * 已进入关闭流程 → (None, False)（调用方如实拒绝，不偷偷同步执行）；
        * 已有一轮在跑 → (已有句柄, False)（**合并**，不并发执行第二份工作）；
        * 正常情况下 → (新句柄, True)。
        """
        if self._closing:
            return None, False
        handle = self._runs.register(RUN_NAME, factory)
        if handle is self._run_handle:
            return handle, False
        self._run_handle = handle
        return handle, True

    async def run_manual(self) -> dict:
        """手动维护入口（POST /api/maintenance/run）：**立即返回**、不阻塞 HTTP。

        返回契约（与 API 层冻结）：

        * 关闭中 ⇒ {ok: False, started: False, accepted: False, running: False,
          reason: "shutting_down"}（API 映射 409）；
        * 已有维护在跑 ⇒ {ok: True, started: False, accepted: True, running: True,
          reason: "already_running"}（合并到既有运行，不重复执行）；
        * 正常启动 ⇒ {ok: True, started: True, accepted: True, running: True,
          reason: None}。

        句柄由 BackgroundTasks 持有，所以关闭时能等到 / 取消到它，
        并**确认执行单元真的终止**之后才报告干净。
        """
        # 先记下「发起前已经在跑的维护执行单元」；`_start_run` 返回的句柄如果就在
        # 这份名单里，说明注册表把这次请求**合并**到了既有运行上（不是新起的）。
        before = {id(handle) for handle in self._inactive_handles()}
        handle, started = self._start_run(self._run_registered)
        if handle is not None and id(handle) in before:
            started = False
        if handle is None:
            return {
                "ok": False,
                "started": False,
                "accepted": False,
                "running": self.is_busy(),
                "reason": "shutting_down",
            }
        if not started:
            return {
                "ok": True,
                "started": False,
                "accepted": True,
                "running": True,
                "reason": "already_running",
            }
        return {
            "ok": True,
            "started": True,
            "accepted": True,
            "running": True,
            "reason": None,
        }

    async def _run_registered(self) -> dict:
        """登记后的执行体：走 `run_once()`，结果留在 last_result 里。

        刻意经 `self.run_once(...)`：这样既有调用点（测试 monkeypatch、既有内部调用）
        替换 `run_once` 时，周期巡检与手动触发两条路都跟着替换。
        """
        result = await self.run_once()
        self._last_result = result
        return result

    async def run_once(self) -> dict:
        """跑一轮维护（周期巡检与手动触发共用的**唯一执行体**）。

        每个重活都在自己的模块里拆成「输入读取 / 纯计算 / 结果提交」（见文件上方
        的拆分约定）：纯计算走 `ctx.heavy` 的工作线程，循环侧只剩只读读取、毫秒级
        DB 写与 `await` 模型调用。`prune_*` 本身就是毫秒级 DB 写（实测 0.1ms），
        留在循环侧当作提交动作。

        重入保护：已经在跑就返回 already_running —— 手动请求在 run_manual() 里
        就已经合并掉了，这道闸是给「直接 await run_once()」的调用点用的（既有测试
        与既有内部调用），保证同一时刻只有一份工作在跑。

        取消**不吞**：`asyncio.CancelledError` 原样抛出，由关闭路径判定
        「执行单元是否确认结束」（见 :meth:`stop`）。
        """
        if self._closing:
            return {"ok": False, "reason": "shutting_down"}
        if self._running:
            return {"ok": False, "reason": "already_running"}
        self._running = True
        try:
            results: dict[str, Any] = {"ok": True}
            # 顺带清理过期的工具输出（记录保留，只清正文）：维护是天然的执行时机
            results["tool_outputs_purged"] = self.ctx.prune_tool_outputs()
            # 整条记录的保留天数默认 0（永久保留）；用户设置过才在这里隐式清理
            results["tool_records_purged"] = self.ctx.prune_tool_records()
            results.update(await scan_contradictions(self.ctx))
            results.update(await run_dreaming(self.ctx))
            results.update(await mine_tool_candidates(self.ctx))
            return results
        except asyncio.CancelledError:
            # 取消**不是失败**：如实向上抛（调用方据此判定「未确认结束」），
            # 绝不在这里把取消吞成 {"ok": True}。
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("maintenance run failed: %s", exc)
            return {"ok": False, "reason": str(exc)[:200]}
        finally:
            self._running = False

    async def stop(self) -> dict:
        """停止维护并**如实报告**：关闭中 → 停周期循环 → 有界等待/取消 → 确认终止。

        返回契约（与 aclose() 冻结）：

        * clean：所有维护执行单元（周期循环、在跑的那一轮、维护发起的等待）是否
          都已**确认结束**；
        * unfinished：clean 的反面（显式布尔，便于合并进 CloseReport）；
        * pending：取消后仍未确认结束的名字；
        * reason：一句话原因（尽到告知义务，不吞问题）。

        等待沿用 services/background.py 的有界预算（默认 10 + 2 + 2 秒），
        所以关闭耗时始终有上限：不会因为一个卡住的任务无限等待。
        """
        self._closing = True
        # 1) 先停周期循环：它可能正好在 await 一轮维护，取消之后不会再发起新的。
        loop_task, self._task = self._task, None
        if loop_task is not None and not loop_task.done():
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 关闭阶段不能再抛
                logger.warning("maintenance loop ended with an error", exc_info=True)
        # 2) 在跑的那一轮 + 维护发起的等待：有界等待 → 取消 → 确认结束。
        run_report = await self._runs.shutdown()
        approval_report = await self._approvals.shutdown()
        pending = sorted(
            set(run_report.unfinished)
            | set(run_report.still_running)
            | set(approval_report.unfinished)
            | set(approval_report.still_running)
        )
        clean = not pending
        phases: list[str] = []
        reason: str | None = None
        if not clean:
            self._reported_unfinished = True
            phases.append(PHASE_MAINTENANCE_UNFINISHED)
            reason = "关闭时仍有维护执行单元未确认结束：" + "、".join(pending)
            logger.error("%s（不写干净退出，不释放 database / adapter）", reason)
        elif self._reported_unfinished:
            # 之前如实报过未结束：不把历史抹掉 —— 只报「这一次的等待结果」。
            phases.append(PHASE_MAINTENANCE_UNFINISHED)
            logger.info("维护本轮等待结束；但更早的一次关闭报告里仍有未确认结束的执行单元")
        else:
            phases.append(PHASE_MAINTENANCE_STOPPED)
            logger.info("维护已确认全部结束（周期循环 + 在跑的一轮 + 维护发起的等待）")
        return {
            "clean": clean,
            "unfinished": (not clean),
            "pending": pending,
            "reason": reason,
            "phases": phases,
        }

    async def _loop(self) -> None:
        """主循环：**整个循环体**都在异常隔离内。

        旧实现的 try 只包住 `run_once()`，而 `ctx._active_loop` 这种引用
        （这个属性早已不存在）在 try 之外抛 AttributeError，调度器会被一次
        无关的错误永久杀死 —— 之后再也不会执行任何维护。
        """
        while True:
            try:
                await asyncio.sleep(self.next_delay_seconds())
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 一次失败不能杀死调度器
                logger.warning("maintenance tick failed", exc_info=True)

    async def _tick(self) -> None:
        if self._closing:
            return
        if self.ctx.settings_store.get("maintenance.enabled", "true") == "false":
            return
        # 只在「完全没有主 Turn」时跑。
        #
        # 不能只看 active AgentLoop：Turn 可能正处在上下文准备、结果保存或收尾阶段，
        # 这时 loop 已经交出/还没建立，但主任务并没有结束 —— 用 active_loop 判断
        # 会让维护任务和主任务同时跑。
        if self.ctx.turns.active is not None:
            return
        # 周期巡检也走**同一个登记入口**：与手动维护共用一份注册表与去重名字，
        # 所以关闭时能等到它，手动与周期也不会并发跑两份同样的工作。
        before = {id(item) for item in self._inactive_handles()}
        handle, started = self._start_run(self._run_registered)
        if handle is not None and id(handle) in before:
            started = False
        if handle is None or not started:
            # 关闭中，或已有一轮（手动 / 周期）在跑：不重复执行、也不替别人等它。
            return
        try:
            # 等这一轮结束：循环的节奏就是「跑完再睡」。异常已在 run_once 里被记为
            # 返回值；取消（关闭）**必须原样抛出**，由 stop() 判定「确认结束没有」
            # —— 在这里吞掉取消会让循环永远停不下来。
            await handle.task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 一次失败不能杀死调度器
            logger.warning("maintenance tick failed", exc_info=True)


