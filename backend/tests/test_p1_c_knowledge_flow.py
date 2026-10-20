"""Phase 1 C：主对话 propose_knowledge 审批 + 后台 derivation 证据校验/拒绝墓碑。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.server import create_app
from agent.config import Settings
from agent.graph.nodes import NodeService
from agent.knowledge.evidence import EvidenceCatalog, EvidenceEntry, build_background_catalog
from agent.knowledge.extraction import KnowledgeExtraction, validate_knowledge
from agent.knowledge.inject import InjectionSource
from agent.knowledge.lifecycle import KnowledgeService
from agent.knowledge.rejection import mark_ignored, mark_rejected
from agent.tools.knowledge_propose import ProposeKnowledgeTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _catalog() -> EvidenceCatalog:
    return EvidenceCatalog(
        [
            EvidenceEntry(id="msg_user", kind="message", label="用户消息", role="user"),
            EvidenceEntry(id="msg_ai", kind="message", label="助手消息", role="assistant"),
            EvidenceEntry(id="tr_1", kind="tool_result", label="fs_read"),
            EvidenceEntry(id="att_1", kind="attachment", label="鹅.txt"),
        ]
    )


def _tool(conn: sqlite3.Connection, *, turn_id: str | None = "turn_1") -> ProposeKnowledgeTool:
    catalog = _catalog()
    return ProposeKnowledgeTool(
        conn,
        evidence_provider=lambda: catalog,
        turn_id_provider=lambda: turn_id,
    )


def _args(**overrides):
    payload = {
        "content": "用户偏好清淡饮食",
        "category": "general_fact",
        "scope": {"type": "user", "node_ids": ["user:local"]},
        "epistemic_status": "user_explicit",
        "evidence_ids": ["msg_user"],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 主对话新增知识：一律审批，pending 不进注入面
# ---------------------------------------------------------------------------


def test_low_impact_proposal_still_waits_for_approval(db_conn: sqlite3.Connection):
    tool = _tool(db_conn)
    result = asyncio.run(tool.run(**_args()))
    assert result.ok, result.error
    assert result.status == "awaiting_approval"
    assert result.code == "awaiting_user_approval"
    assert result.protocol_version == 1
    knowledge_id = result.data["knowledge_id"]
    item = KnowledgeService(db_conn).get(knowledge_id)
    assert item is not None and item.state.value == "pending_review"
    assert item.provenance["source"] == "main_chat"
    assert item.provenance["epistemic_status"] == "user_explicit"
    assert item.provenance["evidence_ids"] == ["msg_user"]
    assert item.provenance["turn_id"] == "turn_1"
    # 待审批不作为有效知识：不得进入注入面。
    assert InjectionSource(db_conn).list_active() == []


def test_proposal_evidence_must_exist(db_conn: sqlite3.Connection):
    tool = _tool(db_conn)
    result = asyncio.run(tool.run(**_args(evidence_ids=["ghost"])))
    assert not result.ok
    assert result.code == "evidence_not_found"
    assert result.data["missing_evidence_ids"] == ["ghost"]
    assert db_conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"] == 0


def test_qio_observed_requires_real_observation(db_conn: sqlite3.Connection):
    tool = _tool(db_conn)
    rejected = asyncio.run(
        tool.run(**_args(epistemic_status="qio_observed", evidence_ids=["msg_user"]))
    )
    assert not rejected.ok and rejected.code == "evidence_unsupported"
    accepted = asyncio.run(
        tool.run(
            **_args(
                content="fs_read 实际读到了鹅.txt",
                epistemic_status="qio_observed",
                evidence_ids=["tr_1"],
            )
        )
    )
    assert accepted.ok and accepted.status == "awaiting_approval"


def test_user_explicit_requires_a_user_message(db_conn: sqlite3.Connection):
    tool = _tool(db_conn)
    rejected = asyncio.run(tool.run(**_args(evidence_ids=["msg_ai"])))
    assert not rejected.ok and rejected.code == "evidence_unsupported"
    accepted = asyncio.run(tool.run(**_args(content="用户明确说过不吃辣")))
    assert accepted.ok


def test_scope_is_resolved_to_real_nodes(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    topic = nodes.create_topic("测试话题")
    tool = _tool(db_conn)
    result = asyncio.run(
        tool.run(
            **_args(
                content="这个项目使用 SQLite",
                scope={"type": "topic", "node_ids": [topic.id]},
            )
        )
    )
    assert result.ok, result.error
    item = KnowledgeService(db_conn).get(result.data["knowledge_id"])
    assert item.node_ids == [topic.id]
    # 编造的 scope 节点 → 结构化失败，不落库。
    bad = asyncio.run(
        tool.run(**_args(content="编造节点", scope={"type": "topic", "node_ids": ["topic_ghost"]}))
    )
    assert not bad.ok and bad.code == "invalid_scope"


def test_user_scope_maps_legacy_global_id_to_real_user_node(db_conn: sqlite3.Connection):
    tool = _tool(db_conn)
    result = asyncio.run(tool.run(**_args()))
    item = KnowledgeService(db_conn).get(result.data["knowledge_id"])
    user_node = NodeService(db_conn).get_or_create_user_root()
    assert item.node_ids == [user_node.id]
    assert item.provenance.get("scope") == "global"


def test_same_turn_duplicate_returns_same_pending_item(db_conn: sqlite3.Connection):
    tool = _tool(db_conn)
    first = asyncio.run(tool.run(**_args(content="同一轮重复提交", evidence_ids=["msg_user"])))
    second = asyncio.run(tool.run(**_args(content="同一轮重复提交", evidence_ids=["msg_user"])))
    assert first.data["knowledge_id"] == second.data["knowledge_id"]
    assert second.code == "already_awaiting_approval"
    assert db_conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"] == 1


def test_active_duplicate_is_not_recreated(db_conn: sqlite3.Connection):
    ks = KnowledgeService(db_conn)
    item = ks.create(category="general_fact", content="已经是有效知识")
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    tool = _tool(db_conn)
    result = asyncio.run(tool.run(**_args(content="已经是有效知识")))
    assert result.ok and result.code == "already_active"
    assert result.data["knowledge_id"] == item.id
    assert db_conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"] == 1


def test_rejected_content_cannot_be_saved_again_by_propose(db_conn: sqlite3.Connection):
    ks = KnowledgeService(db_conn)
    item = ks.create(category="general_fact", content="用户拒绝过的内容")
    ks.submit(item.id)
    ks.reject(item.id)
    mark_rejected(db_conn, item.id)
    db_conn.commit()
    tool = _tool(db_conn)
    result = asyncio.run(tool.run(**_args(content="用户拒绝过的内容")))
    assert not result.ok
    assert result.status == "conflict" and result.code == "rejected_content"
    assert result.data["rejected"]["knowledge_id"] == item.id


# ---------------------------------------------------------------------------
# 后台 derivation：冻结接口 + 证据校验 + 拒绝墓碑
# ---------------------------------------------------------------------------


class _FakeAdapter:
    mode = "native"
    model = "fake-p1-c"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def complete(self, messages, tools, **kwargs) -> Completion:
        # 固定 system + 数据消息（json.dumps），不是把数据 format 进提示词。
        assert messages[0].role == "system"
        assert messages[-1].role == "user"
        json.loads(messages[-1].content)
        return Completion(
            message=ChatMessage(
                role="assistant", content=json.dumps(self.payload, ensure_ascii=False)
            )
        )


def _background_catalog() -> EvidenceCatalog:
    return build_background_catalog(
        fragment_id="frag_1",
        messages=[{"id": "msg_1", "role": "user", "content": "我喜欢清淡饮食"}],
    )


def test_derivation_high_impact_waits_and_low_impact_activates(db_conn: sqlite3.Connection):
    from agent.knowledge.derivation import commit_candidates, extract_candidates

    topic = NodeService(db_conn).create_topic("话题甲")
    payload = {
        "candidates": [
            {
                "content": "用户偏好清淡饮食",
                "category": "user_profile",
                "attach": "user",
                "epistemic_status": "user_explicit",
                "evidence_ids": ["msg_1"],
            },
            {
                "content": "SQLite 支持 WAL 模式",
                "category": "general_fact",
                "attach": "topic",
                "epistemic_status": "qio_inference",
                "evidence_ids": ["frag_1"],
            },
        ]
    }
    catalog = _background_catalog()
    extraction, error = asyncio.run(
        extract_candidates(
            _FakeAdapter(payload), summary={"title": "t", "summary": "s"}, evidence_catalog=catalog
        )
    )
    assert error is None and extraction is not None
    high_impact = commit_candidates(
        db_conn,
        extraction,
        topic_id=topic.id,
        entity_ids=[],
        fragment_id="frag_1",
        evidence_catalog=catalog,
    )
    assert len(high_impact) == 1
    assert high_impact[0]["category"] == "user_profile"
    states = {
        row["content"]: row["state"]
        for row in db_conn.execute("SELECT content, state FROM knowledge").fetchall()
    }
    assert states["用户偏好清淡饮食"] == "pending_review"
    assert states["SQLite 支持 WAL 模式"] == "active"
    stored = db_conn.execute(
        "SELECT provenance FROM knowledge WHERE content = ?", ("SQLite 支持 WAL 模式",)
    ).fetchone()
    provenance = json.loads(stored["provenance"])
    assert provenance["evidence_ids"] == ["frag_1"]
    assert provenance["epistemic_status"] == "qio_inference"
    assert provenance["fragment_id"] == "frag_1"


def test_derivation_skips_unknown_evidence_and_tombstoned_content(db_conn: sqlite3.Connection):
    from agent.knowledge.derivation import commit_candidates

    topic = NodeService(db_conn).create_topic("话题乙")
    rejected = KnowledgeService(db_conn).create(
        category="general_fact", content="用户不要的旧内容"
    )
    KnowledgeService(db_conn).submit(rejected.id)
    mark_ignored(db_conn, rejected.id)
    db_conn.commit()
    extraction = KnowledgeExtraction(
        candidates=[
            {
                "content": "证据不存在的候选",
                "category": "general_fact",
                "attach": "topic",
                "epistemic_status": "qio_inference",
                "evidence_ids": ["no_such_id"],
            },
            {
                "content": "用户不要的旧内容",
                "category": "general_fact",
                "attach": "topic",
                "epistemic_status": "qio_inference",
                "evidence_ids": ["frag_1"],
            },
            {
                "content": "正常可保存的内容",
                "category": "general_fact",
                "attach": "topic",
                "epistemic_status": "qio_inference",
                "evidence_ids": ["frag_1"],
            },
        ]
    )
    catalog = _background_catalog()
    commit_candidates(
        db_conn,
        extraction,
        topic_id=topic.id,
        entity_ids=[],
        fragment_id="frag_1",
        evidence_catalog=catalog,
    )
    contents = {row["content"] for row in db_conn.execute("SELECT content FROM knowledge")}
    assert "证据不存在的候选" not in contents
    assert "用户不要的旧内容" in contents  # 只有原有的墓碑行，没有第二条
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM knowledge WHERE content = ?", ("用户不要的旧内容",)
    ).fetchone()["c"] == 1
    assert "正常可保存的内容" in contents


def test_repair_layer_keeps_new_fields():
    text = json.dumps(
        {
            "candidates": [
                {
                    "content": "用户偏好清淡饮食",
                    "category": "general_fact",
                    "epistemic_status": "qio_observed",
                    "evidence_ids": ["tr_1", "tr_1", ""],
                }
            ]
        },
        ensure_ascii=False,
    )
    outcome = validate_knowledge(text)
    assert outcome.error is None, outcome.error
    candidate = outcome.value.candidates[0]
    assert candidate.epistemic_status == "qio_observed"
    assert candidate.evidence_ids == ["tr_1"]


# ---------------------------------------------------------------------------
# 审批入口 API：pending 可见、reject 写墓碑、payload 带确定程度与依据
# ---------------------------------------------------------------------------


def _client(conn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path), conn)
    return TestClient(app)


def test_pending_proposal_is_visible_and_reject_writes_tombstone(
    db_conn: sqlite3.Connection, tmp_path
):
    tool = _tool(db_conn)
    result = asyncio.run(tool.run(**_args(content="待审批的提议", evidence_ids=["msg_user"])))
    knowledge_id = result.data["knowledge_id"]
    with _client(db_conn, tmp_path) as client:
        rows = client.get("/api/knowledge").json()["knowledge"]
        row = next(r for r in rows if r["id"] == knowledge_id)
        assert row["state"] == "pending_review"
        assert row["epistemic_status"] == "user_explicit"
        assert row["evidence_ids"] == ["msg_user"]
        assert row["source"] == "主对话"
        rejected = client.post(f"/api/knowledge/{knowledge_id}/reject")
        assert rejected.status_code == 200
        assert rejected.json()["knowledge"]["state"] == "draft"
    provenance = json.loads(
        db_conn.execute(
            "SELECT provenance FROM knowledge WHERE id = ?", (knowledge_id,)
        ).fetchone()["provenance"]
    )
    assert provenance.get("rejected_at")


def test_app_registers_new_tools_and_evidence_provider(db_conn: sqlite3.Connection, tmp_path):
    app = create_app(Settings(data_dir=tmp_path), db_conn)
    ctx = app.state.ctx
    assert ctx.registry.get("propose_knowledge") is not None
    assert ctx.registry.get("create_entity") is not None
    # 没有 active turn 时目录为空（不伪造来源），且惰性构造可调用。
    assert ctx._turn_evidence_catalog().ids() == []
