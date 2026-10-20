"""P2-B 定向测试共用桩（不是用例文件；文件名按 test_ 前缀仅为同目录可导入）。"""

from __future__ import annotations

import json
import sys
import types
from typing import Any, Iterable

from agent.adapters.base import ChatMessage, Completion
from agent.maintenance import models

#: A 的迁移 46 里变化日志表（B 只读；本分支没有 A 的迁移，测试按契约建同形表）。
CHANGE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_change_log (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id       TEXT NOT NULL,
    chain_id           TEXT NOT NULL,
    version            INTEGER NOT NULL,
    source             TEXT NOT NULL DEFAULT '',
    occurred_at        TEXT NOT NULL,
    created_turn_seq   INTEGER NOT NULL DEFAULT 0,
    consumed_batch_seq INTEGER,
    UNIQUE (chain_id, version)
)
"""


def prepare_db(conn) -> None:
    """应用迁移 47 + 按契约建 A 的变化日志表（只读用）。"""
    from agent.storage.migration_p2_b import apply

    apply(conn)
    conn.execute(CHANGE_LOG_DDL)


def make_knowledge(
    conn,
    content: str,
    *,
    category: str = "general_fact",
    nodes: Iterable[str] | None = None,
    epistemic_status: str = "user_explicit",
    source: str = "user_approved",
    evidence_ids: Iterable[str] | None = None,
):
    from agent.knowledge.lifecycle import KnowledgeService
    from agent.knowledge.scope import user_global_node_id

    ks = KnowledgeService(conn)
    node_ids = list(nodes) if nodes is not None else [user_global_node_id(conn)]
    provenance = {
        "source": source,
        "epistemic_status": epistemic_status,
        "evidence_ids": list(evidence_ids or []),
    }
    item = ks.create(
        category=category, content=content, node_ids=node_ids, provenance=provenance
    )
    item = ks.submit(item.id)
    item = ks.verify(item.id, verified_by="user")
    return ks.activate(item.id)


def add_change(conn, item, *, source: str = "user_approved", turn_seq: int = 1) -> int:
    cursor = conn.execute(
        "INSERT INTO knowledge_change_log"
        " (knowledge_id, chain_id, version, source, occurred_at, created_turn_seq)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            item.id,
            item.chain_id,
            int(item.version),
            source,
            "2026-10-21T00:00:00+00:00",
            int(turn_seq),
        ),
    )
    return int(cursor.lastrowid)


def make_snapshot(
    *,
    changes: Iterable[dict[str, Any]] = (),
    knowledge_ids: Iterable[str] = (),
    trigger: models.MaintenanceTrigger = models.MaintenanceTrigger.CHANGE_COUNT,
    limits: dict[str, int] | None = None,
    batch_seq: int = 1,
):
    items = list(changes)
    seqs = tuple(int(change["seq"]) for change in items)
    return models.MaintenanceSnapshot(
        batch_seq=batch_seq,
        trigger=trigger,
        change_seq_max=max(seqs) if seqs else 0,
        pending_change_count=len(seqs),
        waiting_turns=0,
        change_ids=seqs,
        knowledge_ids=tuple(str(value) for value in knowledge_ids),
        limits=dict(limits or models.MAINTENANCE_LIMITS),
    )


def maintenance_output(candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"candidates": list(candidates)}


def merge_candidate(
    knowledge_ids: Iterable[str],
    new_content: str,
    *,
    reason: str = "内容重复",
    evidence_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "action": "merge",
        "knowledge_ids": list(knowledge_ids),
        "new_content": new_content,
        "reason": reason,
        "evidence_ids": list(evidence_ids),
    }


def normalize_candidate(
    knowledge_id: str,
    new_content: str,
    *,
    reason: str = "格式整理",
    evidence_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "action": "normalize",
        "knowledge_ids": [knowledge_id],
        "new_content": new_content,
        "reason": reason,
        "evidence_ids": list(evidence_ids),
    }


def ask_candidate(
    question: str,
    knowledge_ids: Iterable[str] = (),
    *,
    evidence_ids: Iterable[str] = (),
    reason: str = "需要用户确认",
) -> dict[str, Any]:
    return {
        "action": "ask",
        "knowledge_ids": list(knowledge_ids),
        "question": question,
        "reason": reason,
        "evidence_ids": list(evidence_ids),
    }


def resolution_output(
    resolution: str,
    *,
    changes: Iterable[dict[str, Any]] = (),
    question: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "resolution": resolution,
        "changes": list(changes),
    }
    if question is not None:
        payload["question"] = question
    return payload


def revise_change(knowledge_id: str, new_content: str) -> dict[str, Any]:
    return {
        "action": "revise",
        "knowledge_id": knowledge_id,
        "new_content": new_content,
    }


def revoke_change(knowledge_id: str) -> dict[str, Any]:
    return {"action": "revoke", "knowledge_id": knowledge_id, "new_content": None}


class ScriptedAdapter:
    """按提示词分派的假 provider（维护 / 独立回答各一份脚本队列）。"""

    mode = "native"
    model = "fake-p2-b"

    def __init__(
        self,
        *,
        maintenance: Iterable[Any] = (),
        resolution: Iterable[Any] = (),
    ) -> None:
        self.maintenance = list(maintenance)
        self.resolution = list(resolution)
        self.calls: list[list[str]] = []

    async def complete(self, messages, tools, **kwargs):
        from agent.prompts import PROMPTS

        system = messages[0].content if hasattr(messages[0], "content") else messages[0]["content"]
        self.calls.append(
            [
                message.content if hasattr(message, "content") else message.get("content")
                for message in messages
            ]
        )
        if PROMPTS["MAINTENANCE_ANSWER_SYSTEM"] in system:
            queue = self.resolution
        elif PROMPTS["MAINTENANCE_SYSTEM"] in system:
            queue = self.maintenance
        else:  # pragma: no cover - 提示词来源必须唯一
            raise AssertionError("未知提示词：" + str(system)[:60])
        if not queue:
            raise AssertionError("脚本队列已用尽")
        payload = queue.pop(0)
        if callable(payload):
            payload = payload(len(self.calls))
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return Completion(message=ChatMessage(role="assistant", content=content))


class FakeCtx:
    """维护执行器只需要 conn 与 build_adapter；单测用最小替身。"""

    def __init__(self, conn, adapter=None) -> None:
        self.conn = conn
        self.adapter = adapter
        self.builds = 0

    async def build_adapter(self):
        self.builds += 1
        return self.adapter


def install_changes_stub(monkeypatch) -> list[dict[str, Any]]:
    """装一个假的 agent.maintenance.changes（A 的模块不在 B 分支时验证钩子）。"""
    calls: list[dict[str, Any]] = []

    module = types.ModuleType("agent.maintenance.changes")

    def record_effective_change(
        conn,
        *,
        knowledge_id,
        chain_id,
        version,
        source,
        occurred_at=None,
    ) -> bool:
        calls.append(
            {
                "knowledge_id": knowledge_id,
                "chain_id": chain_id,
                "version": int(version),
                "source": source,
            }
        )
        return True

    module.record_effective_change = record_effective_change
    monkeypatch.setitem(sys.modules, "agent.maintenance.changes", module)
    return calls


def install_broken_changes_stub(monkeypatch) -> None:
    module = types.ModuleType("agent.maintenance.changes")

    def record_effective_change(*args, **kwargs) -> bool:
        raise RuntimeError("变化记录基础设施故障")

    module.record_effective_change = record_effective_change
    monkeypatch.setitem(sys.modules, "agent.maintenance.changes", module)


def count_rows(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"])


def knowledge_state(conn, knowledge_id: str) -> str:
    row = conn.execute(
        "SELECT state FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    return "" if row is None else str(row["state"])


def active_contents(conn) -> list[str]:
    return [
        str(row["content"])
        for row in conn.execute(
            "SELECT content FROM knowledge WHERE state = 'active' ORDER BY content"
        ).fetchall()
    ]
