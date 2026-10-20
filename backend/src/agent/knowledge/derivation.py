"""知识派生（Phase 1 C 提供的冻结接口；B 的 MemoryLifecycle 直接委托）。

冻结签名（docs/qio-refactor-contract.md §6；_P1-BRIEF-b.md §5）：

    extract_candidates(adapter, *, summary, evidence_catalog, tracer=None)
        -> tuple[Any, str | None]
    commit_candidates(conn, extraction, *, topic_id, entity_ids, fragment_id,
                      evidence_catalog, tracer=None) -> list[dict]

边界：

* 模型调用（抽取）与领域写库（提交）分开：调用方负责事务边界与任务所有权
  （B 的 run_knowledge_task 在 derived_tasks.hold 内调用 commit_candidates）；
* 证据目录形状：list[dict]，条目 {"id": 真实 id, "kind": "message|fragment|
  summary|entity|knowledge|tool_result|attachment|material", "label": 简短说明}
  （也接受 EvidenceCatalog 实例，多一个可选 role 字段）；
* 高影响分类（user_profile / agent_self / goal）提交后留在 pending_review，
  由库房的待确认队列提示用户；低影响分类自动生效；
* 用户明确拒绝（ignored_at / rejected_at 墓碑）的同 category+content 不再保存，
  只留痕；
* 返回本轮新建的高影响候选列表（现有 _pending_candidates 语义）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from agent.adapters.base import BaseAdapter
from agent.knowledge.evidence import (
    DEFAULT_EPISTEMIC_STATUS,
    EvidenceCatalog,
    coerce_catalog,
    validate_epistemic_status,
)
from agent.knowledge.extraction import (
    KnowledgeExtraction,
    extract_candidates_outcome,
)
from agent.knowledge.lifecycle import (
    HIGH_IMPACT_CATEGORIES,
    KnowledgeService,
)
from agent.knowledge.rejection import rejection_record
from agent.knowledge.verify import VerificationService

logger = logging.getLogger(__name__)

#: 高影响候选「为什么值得你确认」的人话说明（与库房现有口径一致）。
HIGH_IMPACT_REASON = {
    "user_profile": "这会影响 QIO 对你的长期理解",
    "agent_self": "这会影响 QIO 对自己的认识",
    "goal": "这会影响 QIO 对你目标的判断",
}

DERIVATION_SOURCE = "background_extraction"


def _safe_exc(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _record_repairs(tracer, scope: str, notes) -> None:
    """本地修正过什么必须留痕（只记类型 + 计数，不落模型原文）。"""
    if tracer is None or not notes:
        return
    from agent.memory.model_output import render_notes

    detail = render_notes(notes)
    logger.info("%s派生修正：%s", scope, detail)
    tracer.write("derivation_repairs", f"{scope}:{detail}")


def _record_failure(tracer, code: str, message: str) -> None:
    from agent.trace.redact import redact_text

    safe = redact_text(message)
    logger.warning("%s: %s", code, safe)
    if tracer is not None:
        tracer.warning(code, safe)


async def extract_candidates(
    adapter: BaseAdapter,
    *,
    summary: Any,
    evidence_catalog: Any,
    tracer=None,
) -> tuple[KnowledgeExtraction | None, str | None]:
    """调用主模型抽取知识候选（不写库）。

    返回 (抽取结果 | None, 可读失败原因 | None)。修正留痕写进 tracer；
    领域写入由 :func:commit_candidates 在调用方指定的事务边界内完成。
    """
    catalog = coerce_catalog(evidence_catalog)
    outcome = await extract_candidates_outcome(
        adapter, summary=summary, evidence_catalog=catalog
    )
    _record_repairs(tracer, "知识抽取", outcome.notes)
    if outcome.value is None:
        reason = outcome.error or "知识抽取不可用"
        _record_failure(tracer, "knowledge_extraction_failed", reason)
        return None, reason
    return outcome.value, None


def _knowledge_contents_for_fragment(
    conn: sqlite3.Connection, fragment_id: str
) -> set[str]:
    """这个片段已经写过的知识内容（任务重试时幂等跳过）。"""
    contents: set[str] = set()
    if not fragment_id:
        return contents
    import json

    for row in conn.execute(
        "SELECT content, provenance FROM knowledge WHERE provenance LIKE ?",
        (f"%{fragment_id}%",),
    ).fetchall():
        try:
            provenance = json.loads(row["provenance"] or "{}")
        except (TypeError, ValueError):
            provenance = {}
        if provenance.get("fragment_id") == fragment_id:
            contents.add(str(row["content"]))
    return contents


def _scope_nodes(
    conn: sqlite3.Connection, cand: Any, topic_id: str, entity_ids: list[str]
) -> list[str]:
    """attach -> 真实节点归属（与旧提交规则一致；实体名无法确定时回落话题）。"""
    attach = str(getattr(cand, "attach", None) or "").strip()
    entity_name = str(getattr(cand, "entity", None) or "").strip()
    if attach == "user":
        from agent.knowledge.scope import user_global_node_id

        return [user_global_node_id(conn)]
    if attach == "entity" and entity_name:
        try:
            from agent.entities.cards import EntityCardService

            card = EntityCardService(conn).find_by_name(entity_name)
            if card is not None and card.node_id:
                return [card.node_id]
        except Exception:  # noqa: BLE001 - 归属降级不阻断提交
            logger.warning("entity scope lookup failed: %s", _safe_exc(Exception()))
        if entity_name in entity_ids:
            return [entity_name]
    return [topic_id]


def commit_candidates(
    conn: sqlite3.Connection,
    extraction: Any,
    *,
    topic_id: str,
    entity_ids: list[str],
    fragment_id: str,
    evidence_catalog: Any,
    tracer=None,
) -> list[dict]:
    """把抽取结果落库（调用方负责事务边界与所有权核验）。

    返回本轮新建的高影响候选（事务提交后由调用方登记到待确认队列）。
    """
    if extraction is None:
        return []
    catalog = coerce_catalog(evidence_catalog)
    candidates = list(getattr(extraction, "candidates", None) or [])
    if not candidates:
        return []
    ks = KnowledgeService(conn)
    vs = VerificationService(conn, ks)
    existing = _knowledge_contents_for_fragment(conn, fragment_id)
    high_impact: list[dict] = []
    for cand in candidates:
        content = str(getattr(cand, "content", "") or "").strip()
        category = str(getattr(cand, "category", "") or "general_fact").strip()
        if not content:
            continue
        if content in existing:
            continue
        tombstone = rejection_record(conn, category=category, content=content)
        if tombstone is not None:
            _record_failure(
                tracer,
                "knowledge_rejected_content",
                "跳过用户明确拒绝过的内容：" + content[:60],
            )
            continue
        evidence_ids = [
            str(item) for item in (getattr(cand, "evidence_ids", None) or [])
        ]
        clean_ids, missing = catalog.validate_evidence_ids(evidence_ids)
        if missing:
            _record_failure(
                tracer,
                "knowledge_evidence_missing",
                "跳过证据不在目录里的候选：" + ", ".join(missing[:3]),
            )
            continue
        epistemic_status = str(
            getattr(cand, "epistemic_status", None) or DEFAULT_EPISTEMIC_STATUS
        )
        epistemic_reason = validate_epistemic_status(
            catalog, epistemic_status, clean_ids
        )
        if epistemic_reason is not None:
            _record_failure(
                tracer,
                "knowledge_evidence_unsupported",
                "跳过证据不支持的候选：" + epistemic_reason,
            )
            continue
        node_ids = _scope_nodes(conn, cand, topic_id, entity_ids)
        try:
            item = ks.create(
                category=category,
                content=content,
                node_ids=node_ids,
                provenance={
                    "fragment_id": fragment_id,
                    "source": DERIVATION_SOURCE,
                    "epistemic_status": epistemic_status,
                    "evidence_ids": clean_ids,
                },
            )
            if tracer is not None:
                tracer.write("knowledge", item.id)
            ks.submit(item.id)
            if category in HIGH_IMPACT_CATEGORIES:
                high_impact.append(
                    {
                        "knowledge_id": item.id,
                        "category": category,
                        "content": content,
                        "impact": "high",
                        "reason": HIGH_IMPACT_REASON.get(
                            category, "这条知识会长期生效"
                        ),
                        "epistemic_status": epistemic_status,
                        "evidence_ids": clean_ids,
                    }
                )
                existing.add(content)
                continue
            result = vs.review(ks.get(item.id), verified_by="system")
            if result.accepted:
                ks.activate(item.id)
            existing.add(content)
        except Exception as exc:  # noqa: BLE001 - 单条候选失败不阻断整批
            logger.warning("knowledge candidate failed: %s", _safe_exc(exc))
    return high_impact
