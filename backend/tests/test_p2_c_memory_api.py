# -*- coding: utf-8 -*-
"""阶段二 C 定向测试：记忆待确认入口 / 维护记录 / 安全恢复 API（契约 §12.5）。

覆盖：

* GET /api/memory/pending 两类事项按 kind / handler 区分，输出字段严格等于
  maintenance.models.PendingItemView（字段名冻结，不另造状态字符串）；
* 知识待审批来源标签统一为「后台整理」/「主对话」，且 /api/knowledge 同时返回
  provenance_source 原始键；
* 回答 / 稍后处理走维护执行器（本分支 B 的 runner 不存在 → 注入 fake runner），
  不写主对话 messages；runner 不可用时 503 且明确「未执行」；
* 维护批次列表 / 详情 / 恢复预览 / 恢复（fake runner 记录调用参数）；
* B 的迁移 47 未应用时读取如实 available=false、写动作 503，不伪造数据。

本分支没有 B 的迁移 47 / runner：测试自己按 B 简报建同样的表，并注入 fake runner。
模型调用一律 fake，不联网、不使用真实 Key。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService
from agent.maintenance.models import MaintenanceAnswerOutcome, PendingItemView
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


# ---------------------------------------------------------------------------
# 装置：数据库表 / 假 runner / app
# ---------------------------------------------------------------------------

_MAINTENANCE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS maintenance_batches (
        id             TEXT PRIMARY KEY,
        trigger_reason TEXT NOT NULL DEFAULT '',
        snapshot       TEXT NOT NULL DEFAULT '{}',
        status         TEXT NOT NULL DEFAULT 'completed',
        model          TEXT,
        created_at     TEXT NOT NULL DEFAULT '',
        finished_at    TEXT,
        stats          TEXT NOT NULL DEFAULT '{}',
        error          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS maintenance_items (
        id            TEXT PRIMARY KEY,
        batch_id      TEXT,
        kind          TEXT NOT NULL DEFAULT 'ask',
        status        TEXT NOT NULL DEFAULT 'awaiting_answer',
        knowledge_ids TEXT NOT NULL DEFAULT '[]',
        before        TEXT NOT NULL DEFAULT '[]',
        after         TEXT NOT NULL DEFAULT '[]',
        question      TEXT,
        answer        TEXT,
        evidence_ids  TEXT NOT NULL DEFAULT '[]',
        reason        TEXT,
        error         TEXT,
        created_at    TEXT NOT NULL DEFAULT '',
        updated_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS maintenance_operations (
        id         TEXT PRIMARY KEY,
        batch_id   TEXT,
        item_id    TEXT,
        action     TEXT NOT NULL DEFAULT '',
        target_ids TEXT NOT NULL DEFAULT '[]',
        result_id  TEXT,
        before     TEXT NOT NULL DEFAULT '[]',
        after      TEXT NOT NULL DEFAULT '[]',
        status     TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
)


def _create_maintenance_tables(conn) -> None:
    """按 B 简报（迁移 47 的字段）建表：只服务本分支定向测试。"""
    for statement in _MAINTENANCE_DDL:
        conn.execute(statement)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_app(tmp_path, *, maintenance_tables: bool = True):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    if maintenance_tables:
        _create_maintenance_tables(conn)
    else:
        # 整合前提变化：迁移 47 已登记进 schema.py（生产路径），所以这里显式移除
        # 维护表来模拟「维护记录尚未建立」的降级库，验证读取 / 写动作的诚实降级。
        for table in (
            "maintenance_operations",
            "maintenance_items",
            "maintenance_batches",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    app = create_app(Settings(data_dir=tmp_path), conn)
    client = TestClient(app)
    return conn, app, client


def _pending_knowledge(
    conn,
    *,
    content: str = "用户偏好五里关火锅",
    source: str = "main_chat",
    evidence_ids: list[str] | None = None,
) -> object:
    ks = KnowledgeService(conn)
    item = ks.create(
        category="general_fact",
        content=content,
        provenance={
            "source": source,
            "epistemic_status": "user_explicit",
            "evidence_ids": list(evidence_ids or []),
        },
    )
    ks.submit(item.id)
    conn.commit()
    return ks.get(item.id)


def _active_knowledge(conn, *, content: str = "用户住在成都") -> object:
    ks = KnowledgeService(conn)
    item = ks.create(category="general_fact", content=content, provenance={"source": "main_chat"})
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    item = ks.activate(item.id)
    conn.commit()
    return item


def _insert_question(
    conn,
    *,
    item_id: str = "mi_q1",
    status: str = "awaiting_answer",
    question: str = "用户提到的常去的火锅店是哪一家？",
    knowledge_ids: list[dict] | None = None,
    evidence_ids: list[str] | None = None,
    batch_id: str | None = None,
    created_at: str | None = None,
) -> str:
    now = created_at or _now()
    conn.execute(
        "INSERT INTO maintenance_items"
        " (id, batch_id, kind, status, knowledge_ids, before, after, question, answer,"
        "  evidence_ids, reason, error, created_at, updated_at)"
        " VALUES (?, ?, 'ask', ?, ?, '[]', '[]', ?, NULL, ?, NULL, NULL, ?, ?)",
        (
            item_id,
            batch_id,
            status,
            json.dumps(knowledge_ids or [], ensure_ascii=False),
            question,
            json.dumps(evidence_ids or [], ensure_ascii=False),
            now,
            now,
        ),
    )
    conn.commit()
    return item_id


def _insert_batch(
    conn,
    *,
    batch_id: str = "mb_1",
    trigger_reason: str = "change_count",
    status: str = "completed",
    stats: dict | None = None,
    created_at: str | None = None,
    error: str | None = None,
) -> str:
    now = created_at or _now()
    conn.execute(
        "INSERT INTO maintenance_batches"
        " (id, trigger_reason, snapshot, status, model, created_at, finished_at, stats, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            batch_id,
            trigger_reason,
            "{}",
            status,
            "fake-model",
            now,
            now,
            json.dumps(stats or {"applied": 1, "asked": 1, "rejected": 0}, ensure_ascii=False),
            error,
        ),
    )
    conn.commit()
    return batch_id


def _insert_operation(conn, *, batch_id: str = "mb_1", op_id: str = "op_1") -> str:
    now = _now()
    conn.execute(
        # item_id 置 NULL：整合后迁移 47 对 item_id 有外键（本用例只验证批次详情展示，
        # 不伪造一条不存在的事项）。
        "INSERT INTO maintenance_operations"
        " (id, batch_id, item_id, action, target_ids, result_id, before, after, status, created_at)"
        " VALUES (?, ?, NULL, 'normalize', ?, 'kn_new', ?, ?, 'applied', ?)",
        (
            op_id,
            batch_id,
            json.dumps(["kn_old"], ensure_ascii=False),
            json.dumps([{"id": "kn_old", "version": 1, "content": "旧内容"}], ensure_ascii=False),
            json.dumps([{"id": "kn_new", "version": 2, "content": "新内容"}], ensure_ascii=False),
            now,
        ),
    )
    conn.commit()
    return op_id


class FakeRunner:
    """B 的 MaintenanceRunner 的假实现：记录调用、按约定返回，不调用任何模型。"""

    def __init__(self, conn):
        self.conn = conn
        self.answer_calls: list[tuple[str, str]] = []
        self.preview_calls: list[dict] = []
        self.restore_calls: list[dict] = []
        self.answer_error: Exception | None = None
        self.preview_result: dict = {"items": []}
        self.restore_result: dict = {
            "ok": True,
            "status": "rolled_back",
            "restored": [{"knowledge_id": "kn_new", "result_id": "kn_restored"}],
            "blocked": [{"knowledge_id": "kn_other", "reason": "后来被用户修改"}],
        }
        self.mark_processed = True

    async def answer_question(self, item_id: str, answer: str) -> MaintenanceAnswerOutcome:
        self.answer_calls.append((item_id, answer))
        if self.answer_error is not None:
            raise self.answer_error
        if self.mark_processed:
            self.conn.execute(
                "UPDATE maintenance_items SET status = 'processed', answer = ?, updated_at = ?"
                " WHERE id = ?",
                (answer, _now(), item_id),
            )
            self.conn.commit()
        return MaintenanceAnswerOutcome(
            ok=True,
            status="processed",
            item_id=item_id,
            resolution="resolved",
            applied_changes=1,
            detail={"fake": True},
        )

    async def preview_restore(self, *, batch_id=None, item_id=None) -> dict:
        self.preview_calls.append({"batch_id": batch_id, "item_id": item_id})
        return dict(self.preview_result)

    async def restore(self, *, batch_id=None, item_id=None) -> dict:
        self.restore_calls.append({"batch_id": batch_id, "item_id": item_id})
        return dict(self.restore_result)


class FakeScheduler:
    def __init__(self, state: dict | None = None):
        self.state = state or {
            "turn_seq": 7,
            "last_processed_seq": 3,
            "batch_seq": 1,
            "pending_change_count": 4,
            "waiting_turns": 2,
        }
        self.raise_error: Exception | None = None

    def pending_state(self) -> dict:
        if self.raise_error is not None:
            raise self.raise_error
        return dict(self.state)


# ---------------------------------------------------------------------------
# GET /api/memory/pending：两类事项、冻结字段、筛选
# ---------------------------------------------------------------------------


def test_pending_list_separates_handlers_and_uses_frozen_fields(tmp_path):
    conn, _app, client = _make_app(tmp_path)
    proposal = _pending_knowledge(
        conn, content="用户偏好五里关火锅", source="main_chat", evidence_ids=["frag_1", "msg_2"]
    )
    involved = _active_knowledge(conn, content="用户住在成都")
    _insert_question(
        conn,
        item_id="mi_q1",
        question="用户提到的常去的火锅店是哪一家？",
        knowledge_ids=[{"id": involved.id, "version": involved.version, "chain_id": involved.chain_id}],
        evidence_ids=["frag_9"],
        created_at="2026-10-21T00:00:00+00:00",
    )

    r = client.get("/api/memory/pending")
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is False
    assert body["maintenance_available"] is True
    assert body["total"] == 2
    by_kind = {item["kind"]: item for item in body["items"]}
    assert set(by_kind) == {"knowledge_proposal", "maintenance_question"}

    proposal_view = by_kind["knowledge_proposal"]
    question_view = by_kind["maintenance_question"]
    # 字段严格等于冻结读取模型（不多造字段，也不漏字段）
    assert set(proposal_view) == set(PendingItemView.model_fields)
    assert set(question_view) == set(PendingItemView.model_fields)

    assert proposal_view["handler"] == "knowledge_proposal"
    assert proposal_view["status"] == "awaiting_approval"
    assert proposal_view["actions"] == ["approve", "approve_modified", "reject"]
    assert proposal_view["question"] is None
    ref = proposal_view["knowledge"][0]
    assert ref["id"] == proposal.id
    assert ref["version"] == proposal.version
    assert ref["content"] == "用户偏好五里关火锅"
    assert ref["category"] == "general_fact"
    assert ref["state"] == "pending_review"
    assert ref["scope"] == ["全局（你）"]
    assert ref["epistemic_status"] == "user_explicit"
    assert ref["source"] == "主对话"
    assert [e["id"] for e in proposal_view["evidence"]] == ["frag_1", "msg_2"]
    assert proposal_view["after"][0]["id"] == proposal.id
    assert proposal_view["before"] == []

    assert question_view["handler"] == "maintenance"
    assert question_view["status"] == "awaiting_answer"
    assert question_view["actions"] == ["answer", "defer"]
    assert question_view["question"] == "用户提到的常去的火锅店是哪一家？"
    assert question_view["knowledge"][0]["id"] == involved.id
    assert question_view["knowledge"][0]["content"] == "用户住在成都"
    assert question_view["knowledge"][0]["state"] == "active"
    assert [e["id"] for e in question_view["evidence"]] == ["frag_9"]
    assert question_view["batch_id"] is None


def test_pending_list_filters_by_kind_and_status(tmp_path):
    conn, _app, client = _make_app(tmp_path)
    _pending_knowledge(conn)
    _insert_question(conn, item_id="mi_a", status="awaiting_answer")
    _insert_question(conn, item_id="mi_b", status="deferred")

    only_questions = client.get(
        "/api/memory/pending", params={"kind": "maintenance_question"}
    ).json()
    assert {item["kind"] for item in only_questions["items"]} == {"maintenance_question"}
    assert only_questions["total"] == 2

    only_proposals = client.get(
        "/api/memory/pending", params={"kind": "knowledge_proposal"}
    ).json()
    assert [item["kind"] for item in only_proposals["items"]] == ["knowledge_proposal"]

    deferred = client.get("/api/memory/pending", params={"status": "deferred"}).json()
    assert [item["id"] for item in deferred["items"]] == ["mi_b"]
    assert deferred["items"][0]["status"] == "deferred"

    awaiting = client.get(
        "/api/memory/pending", params={"status": "awaiting_answer"}
    ).json()
    assert [item["id"] for item in awaiting["items"]] == ["mi_a"]

    assert client.get("/api/memory/pending", params={"kind": "bogus"}).status_code == 400
    assert client.get("/api/memory/pending", params={"status": "bogus"}).status_code == 400
    assert client.get("/api/memory/pending", params={"limit": 0}).status_code == 400


def test_pending_list_reports_truncation(tmp_path):
    conn, _app, client = _make_app(tmp_path)
    for index in range(3):
        _insert_question(
            conn,
            item_id=f"mi_{index}",
            created_at=f"2026-10-21T00:00:0{index}+00:00",
        )
    body = client.get(
        "/api/memory/pending", params={"kind": "maintenance_question", "limit": 2}
    ).json()
    assert body["total"] == 3
    assert body["truncated"] is True
    assert len(body["items"]) == 2
    assert [item["id"] for item in body["items"]] == ["mi_0", "mi_1"]


def test_pending_empty_state(tmp_path):
    _conn, _app, client = _make_app(tmp_path)
    body = client.get("/api/memory/pending").json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["truncated"] is False
    assert body["warnings"] == []


def test_pending_without_migration_47_is_honest(tmp_path):
    """本分支没有 B 的迁移 47：读取如实 available=false，写动作 503，不伪造数据。"""
    conn, _app, client = _make_app(tmp_path, maintenance_tables=False)
    _pending_knowledge(conn)

    pending = client.get("/api/memory/pending").json()
    assert [item["kind"] for item in pending["items"]] == ["knowledge_proposal"]
    assert pending["maintenance_available"] is False
    assert pending["warnings"] and "迁移 47" in pending["warnings"][0]

    batches = client.get("/api/memory/maintenance/batches").json()
    assert batches["available"] is False
    assert batches["batches"] == []

    assert client.get("/api/memory/maintenance/batches/mb_1").status_code == 503
    assert client.post("/api/memory/pending/mi_q1/defer").status_code == 503
    assert (
        client.post(
            "/api/memory/pending/mi_q1/answer", json={"answer": "五里关"}
        ).status_code
        == 503
    )


# ---------------------------------------------------------------------------
# POST answer / defer：走维护执行器、不写主对话
# ---------------------------------------------------------------------------


def test_answer_uses_maintenance_runner_and_does_not_touch_main_chat(tmp_path):
    conn, app, client = _make_app(tmp_path)
    _insert_question(conn, item_id="mi_q1")
    runner = FakeRunner(conn)
    app.state.ctx.maintenance_runner = runner
    messages_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    r = client.post(
        "/api/memory/pending/mi_q1/answer", json={"answer": "她最爱五里关火锅"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "processed"
    assert body["item_id"] == "mi_q1"
    assert body["resolution"] == "resolved"
    assert body["applied_changes"] == 1
    assert body["detail"] == {"fake": True}
    assert body["item"]["id"] == "mi_q1"
    assert body["item"]["status"] == "processed"
    assert runner.answer_calls == [("mi_q1", "她最爱五里关火锅")]
    # 回答进的是维护执行器，不是主对话 messages
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == messages_before
    # 已处理事项不再出现在待确认列表里
    assert all(
        item["id"] != "mi_q1" for item in client.get("/api/memory/pending").json()["items"]
    )


def test_answer_requires_runner_and_does_not_change_data(tmp_path):
    conn, _app, client = _make_app(tmp_path)
    _insert_question(conn, item_id="mi_q1")
    # 整合后 AppContext 会真正初始化维护执行器；这里显式模拟「执行器不可用」。
    _app.state.ctx.maintenance_runner = None
    r = client.post("/api/memory/pending/mi_q1/answer", json={"answer": "五里关"})
    assert r.status_code == 503
    assert "未执行" in r.json()["detail"]
    row = conn.execute(
        "SELECT status FROM maintenance_items WHERE id = 'mi_q1'"
    ).fetchone()
    assert row["status"] == "awaiting_answer"


def test_answer_validation_and_handler_separation(tmp_path):
    conn, app, client = _make_app(tmp_path)
    _insert_question(conn, item_id="mi_q1")
    proposal = _pending_knowledge(conn)
    runner = FakeRunner(conn)
    app.state.ctx.maintenance_runner = runner

    assert (
        client.post("/api/memory/pending/mi_q1/answer", json={"answer": "  "}).status_code
        == 400
    )
    # 知识待审批事项不能按维护问题回答（处理程序区分）
    conflict = client.post(
        f"/api/memory/pending/{proposal.id}/answer", json={"answer": "同意"}
    )
    assert conflict.status_code == 400
    assert "knowledge_proposal" in conflict.json()["detail"]
    assert (
        client.post("/api/memory/pending/nope/answer", json={"answer": "x"}).status_code
        == 404
    )
    assert runner.answer_calls == []

    # 已处理事项不能再次回答：明确 409，且不调用 runner
    conn.execute(
        "UPDATE maintenance_items SET status = 'processed' WHERE id = 'mi_q1'"
    )
    conn.commit()
    too_late = client.post(
        "/api/memory/pending/mi_q1/answer", json={"answer": "再答一次"}
    )
    assert too_late.status_code == 409
    assert runner.answer_calls == []


def test_answer_runner_failure_is_reported(tmp_path):
    conn, app, client = _make_app(tmp_path)
    _insert_question(conn, item_id="mi_q1")
    runner = FakeRunner(conn)
    runner.answer_error = RuntimeError("模型输出不可解析")
    app.state.ctx.maintenance_runner = runner

    r = client.post("/api/memory/pending/mi_q1/answer", json={"answer": "五里关"})
    assert r.status_code == 500
    assert "未确认" in r.json()["detail"]
    row = conn.execute(
        "SELECT status FROM maintenance_items WHERE id = 'mi_q1'"
    ).fetchone()
    assert row["status"] == "awaiting_answer"


def test_defer_persists_and_keeps_item_visible(tmp_path):
    conn, app, client = _make_app(tmp_path)
    _insert_question(conn, item_id="mi_q1")
    runner = FakeRunner(conn)
    app.state.ctx.maintenance_runner = runner

    r = client.post("/api/memory/pending/mi_q1/defer")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["item"]["status"] == "deferred"
    assert (
        conn.execute("SELECT status FROM maintenance_items WHERE id = 'mi_q1'").fetchone()[
            "status"
        ]
        == "deferred"
    )
    pending = client.get("/api/memory/pending").json()
    assert [item["id"] for item in pending["items"]] == ["mi_q1"]
    assert pending["items"][0]["status"] == "deferred"
    # 稍后处理不是回答：没有调用维护执行器
    assert runner.answer_calls == []


# ---------------------------------------------------------------------------
# 维护记录：批次列表 / 详情
# ---------------------------------------------------------------------------


def test_maintenance_batches_list_and_detail(tmp_path):
    conn, _app, client = _make_app(tmp_path)
    _insert_batch(
        conn,
        batch_id="mb_1",
        trigger_reason="change_count",
        status="completed",
        stats={"applied": 2, "asked": 1, "rejected": 0},
        created_at="2026-10-21T01:00:00+00:00",
    )
    _insert_batch(
        conn,
        batch_id="mb_0",
        trigger_reason="waiting_turns",
        status="failed",
        stats={"applied": 0, "asked": 0, "rejected": 0},
        created_at="2026-10-21T00:00:00+00:00",
        error="模型输出不可解析",
    )
    _insert_question(
        conn,
        item_id="mi_batch",
        question="合并后的问题",
        knowledge_ids=[{"id": "kn_old", "version": 2, "chain_id": "kn_chain"}],
        batch_id="mb_1",
    )
    _insert_operation(conn, batch_id="mb_1")

    listing = client.get("/api/memory/maintenance/batches").json()
    assert listing["available"] is True
    assert listing["total"] == 2
    assert listing["truncated"] is False
    assert [batch["id"] for batch in listing["batches"]] == ["mb_1", "mb_0"]
    first = listing["batches"][0]
    assert first["trigger"] == "change_count"
    assert first["status"] == "completed"
    assert first["stats"] == {"applied": 2, "asked": 1, "rejected": 0}
    assert listing["batches"][1]["error"] == "模型输出不可解析"

    detail = client.get("/api/memory/maintenance/batches/mb_1").json()
    assert detail["available"] is True
    assert detail["batch"]["id"] == "mb_1"
    assert len(detail["items"]) == 1
    item = detail["items"][0]
    assert item["kind"] == "ask"
    assert item["question"] == "合并后的问题"
    assert item["knowledge"] == [{"id": "kn_old", "version": 2, "chain_id": "kn_chain"}]
    assert len(detail["operations"]) == 1
    op = detail["operations"][0]
    assert op["action"] == "normalize"
    assert op["target_ids"] == ["kn_old"]
    assert op["before"][0]["content"] == "旧内容"
    assert op["after"][0]["content"] == "新内容"

    assert client.get("/api/memory/maintenance/batches/missing").status_code == 404
    assert (
        client.get("/api/memory/maintenance/batches", params={"limit": 0}).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# 恢复：预览与执行（fake runner 记录参数）
# ---------------------------------------------------------------------------


def test_restore_preview_uses_runner(tmp_path):
    _conn, app, client = _make_app(tmp_path)
    runner = FakeRunner(app.state.ctx.conn)
    runner.preview_result = {
        "batch_id": "mb_1",
        "items": [
            {"knowledge_id": "kn_new", "restorable": True, "reason": None},
            {"knowledge_id": "kn_other", "restorable": False, "reason": "后来被用户修改"},
        ],
    }
    app.state.ctx.maintenance_runner = runner

    r = client.get(
        "/api/memory/maintenance/restore/preview", params={"batch_id": "mb_1"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["preview"] == runner.preview_result
    assert runner.preview_calls == [{"batch_id": "mb_1", "item_id": None}]

    r = client.get(
        "/api/memory/maintenance/restore/preview", params={"item_id": "mi_1"}
    )
    assert r.status_code == 200
    assert runner.preview_calls[-1] == {"batch_id": None, "item_id": "mi_1"}

    # 必须且只能给一个目标；runner 不可用时 503
    assert client.get("/api/memory/maintenance/restore/preview").status_code == 400
    assert (
        client.get(
            "/api/memory/maintenance/restore/preview",
            params={"batch_id": "mb_1", "item_id": "mi_1"},
        ).status_code
        == 400
    )
    app.state.ctx.maintenance_runner = None
    assert (
        client.get(
            "/api/memory/maintenance/restore/preview", params={"batch_id": "mb_1"}
        ).status_code
        == 503
    )


def test_restore_requires_confirm_and_returns_blocked_parts(tmp_path):
    _conn, app, client = _make_app(tmp_path)
    runner = FakeRunner(app.state.ctx.conn)
    app.state.ctx.maintenance_runner = runner

    missing_confirm = client.post(
        "/api/memory/maintenance/restore", json={"batch_id": "mb_1"}
    )
    assert missing_confirm.status_code == 400
    assert "未执行" in missing_confirm.json()["detail"]
    assert runner.restore_calls == []

    false_confirm = client.post(
        "/api/memory/maintenance/restore",
        json={"batch_id": "mb_1", "confirm": False},
    )
    assert false_confirm.status_code == 400
    assert runner.restore_calls == []

    r = client.post(
        "/api/memory/maintenance/restore",
        json={"batch_id": "mb_1", "confirm": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "rolled_back"
    assert body["restored"] == [{"knowledge_id": "kn_new", "result_id": "kn_restored"}]
    assert body["blocked"] == [{"knowledge_id": "kn_other", "reason": "后来被用户修改"}]
    assert body["result"]["status"] == "rolled_back"
    assert runner.restore_calls == [{"batch_id": "mb_1", "item_id": None}]

    # 单条恢复 + 两个目标都不允许
    single = client.post(
        "/api/memory/maintenance/restore",
        json={"item_id": "mi_1", "confirm": True},
    )
    assert single.status_code == 200
    assert runner.restore_calls[-1] == {"batch_id": None, "item_id": "mi_1"}
    assert (
        client.post(
            "/api/memory/maintenance/restore",
            json={"batch_id": "mb_1", "item_id": "mi_1", "confirm": True},
        ).status_code
        == 400
    )


# ---------------------------------------------------------------------------
# 维护状态 / 来源标签
# ---------------------------------------------------------------------------


def test_maintenance_status_reports_unavailable_or_real_state(tmp_path):
    _conn, app, client = _make_app(tmp_path)
    # 整合后 AppContext 会真正初始化调度器：先验证真实状态可用。
    real = client.get("/api/memory/maintenance/status").json()
    assert real["available"] is True
    assert "pending_change_count" in real and "turn_seq" in real

    # 显式模拟「调度器未接入」的降级路径。
    app.state.ctx.knowledge_maintenance = None
    missing = client.get("/api/memory/maintenance/status").json()
    assert missing["available"] is False
    assert "未接入" in missing["reason"]

    scheduler = FakeScheduler()
    app.state.ctx.knowledge_maintenance = scheduler
    body = client.get("/api/memory/maintenance/status").json()
    assert body["available"] is True
    assert body["pending_change_count"] == 4
    assert body["turn_seq"] == 7

    scheduler.raise_error = RuntimeError("状态读取失败")
    assert client.get("/api/memory/maintenance/status").status_code == 500


@pytest.mark.parametrize(
    "source",
    [
        "background_extraction",
        "maintenance_merge",
        "maintenance_normalize",
        "maintenance_restore",
    ],
)
def test_knowledge_source_label_maps_maintenance_keys(tmp_path, source):
    conn, _app, client = _make_app(tmp_path)
    ks = KnowledgeService(conn)
    item = ks.create(
        category="general_fact",
        content=f"后台整理写入（{source}）",
        provenance={"source": source},
    )
    conn.commit()

    rows = client.get("/api/knowledge").json()["knowledge"]
    row = next(entry for entry in rows if entry["id"] == item.id)
    assert row["source"] == "后台整理"
    assert row["provenance_source"] == source
