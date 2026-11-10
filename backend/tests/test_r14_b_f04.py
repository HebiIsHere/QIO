"""R14-B / F04-backend：恢复事项独立于活动轮次（持久可见 / 启动只恢复可见性 / 幂等收口）。

旧实现：恢复记录只在 app 内存 dict —— TURN_END、刷新、断连、进程重启后记录消失，
恢复清单里也完全看不到这类事项。

本文件验证（模型 fake，SQLite 真实临时库）：
* 持久记录进入既有 GET /api/recovery/records 查询（kind/actions/字段形状）；
* 记录与活动轮次（turn_journal）分开表达，互不覆盖；
* 断连/重启后仍可见；启动只恢复可见性（不自动执行/批准/续期）；
* 租约状态表达（处理中不可重试 / 失效可重试）；
* 已完成不被迟到失败覆盖（按身份/版本合并）；
* retry-answer HTTP 契约：200 / 202 / 404 / 409 / 503。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.recovery_routes import build_router
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import TopicStages
from agent.services.topic_recovery import claim_record, record_topic_failure, retry_topic_answer
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_MARKER = "[[QIO:ANSWER]]"
TABLE = "topic_recovery_records"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "r14_b_f04.db"


def _new_app(db_path, tmp_path) -> AppContext:
    conn = connect(db_path)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


@pytest.fixture()
def app(db_path, tmp_path) -> AppContext:
    return _new_app(db_path, tmp_path)


class FakeAdapter:
    mode = "native"
    model = "fake-recovery"
    protocol_in_prompt = False

    def __init__(self, *, text: str = ANSWER_MARKER + "\n恢复后的回答") -> None:
        self.text = text
        self.calls = 0
        self.requests: list = []

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        self.requests.append(list(messages))
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append((turn_id, topic_id, user_message_id))
        return [
            ChatMessage(role="system", content="SYS"),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire(app: AppContext, adapter=None) -> tuple:
    orchestrator = FakeOrchestrator()
    adapter = adapter or FakeAdapter()
    app.turn_orchestrator = orchestrator  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]
    return orchestrator, adapter


def _prepare(app: AppContext, *, turn_id: str = "turn_r"):
    topic_id = app.topics.nodes.create_topic("目标话题").id
    fragment = app.fragments.get_or_create_open(topic_id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="原问题",
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _make_record(app: AppContext, topic_id: str, message_id: str, **overrides):
    payload = {
        "turn_id": "turn_r",
        "topic_id": topic_id,
        "user_message_id": message_id,
        "operation_id": "op-1",
        "param_version": 1,
        "base_nav_version": current_nav_version(app.conn),
        "recovery_nav_version": current_nav_version(app.conn),
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    return record_topic_failure(app, **payload)


def _client(ctx: AppContext) -> TestClient:
    api = FastAPI()
    api.include_router(build_router(ctx))
    return TestClient(api)


def _get_listing(client: TestClient, **params):
    response = client.get("/api/recovery/records", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _topic_records(listing: dict) -> list[dict]:
    return [item for item in listing["records"] if item["kind"] == "topic_answer_recovery"]


def _row(app: AppContext, record_id: str):
    return app.conn.execute(
        f"SELECT * FROM {TABLE} WHERE record_id = ?", (record_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# 1) 进现有恢复清单（形状 / 动作 / 字段）
# ---------------------------------------------------------------------------


def test_get_records_includes_persistent_topic_recovery_record(app):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)

    with _client(app) as client:
        listing = _get_listing(client)

    items = _topic_records(listing)
    assert len(items) == 1
    item = items[0]
    assert item["record_id"] == record["record_id"]
    assert item["kind"] == "topic_answer_recovery"
    assert item["state_class"] == "answer_pending"
    assert item["status"] == "pending"
    assert item["topic_id"] == topic_id
    assert item["turn_id"] == "turn_r"
    assert item["failed_stage"] == "context_prepare"
    assert item["attempts"] == 0
    assert item["answer_id"] is None
    assert item["created_at"] and item["updated_at"]
    assert item["actions"] == [
        {"id": "retry_answer", "label": "重试回答", "enabled": True, "reason": ""}
    ]
    assert listing["shown"] >= 1 and listing["total"] >= 1


def test_listing_separates_turn_records_from_topic_recovery(app):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)
    # 既有来源：一条没有归属的旧 turn 记录（与恢复事项种类不同、互不覆盖）
    app.conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, "
        "created_at, updated_at) VALUES ('turn_legacy', '旧消息', ?, 0, 'interrupted', ?, ?)",
        (topic_id, "2026-11-09T0:00:00+00:00", "2026-11-09T0:00:00+00:00"),
    )

    with _client(app) as client:
        listing = _get_listing(client)
        all_kinds = {item["kind"] for item in listing["records"]}
        assert "user_turn" in all_kinds and "topic_answer_recovery" in all_kinds
        assert listing["total"] == len(listing["records"]) == 2

        filtered = _get_listing(client, kinds="topic_answer_recovery")
        kinds = {item["kind"] for item in filtered["records"]}
        assert kinds == {"topic_answer_recovery"}
        assert filtered["records"][0]["record_id"] == record["record_id"]

        only_turns = _get_listing(client, kinds="user_turn")
        assert {item["kind"] for item in only_turns["records"]} == {"user_turn"}


# ---------------------------------------------------------------------------
# 2) 重启可见 / 启动不自动执行、不续期
# ---------------------------------------------------------------------------


def test_record_stays_visible_after_restart_without_auto_execute(app, db_path, tmp_path):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)
    before = dict(_row(app, record["record_id"]))

    # 「进程重启」：同一数据库新建 AppContext；不装配任何模型/编排器通道
    restarted = _new_app(db_path, tmp_path)
    assert getattr(restarted, topic_recovery.TOPIC_RECOVERY_ATTR, None) is None or not getattr(
        restarted, topic_recovery.TOPIC_RECOVERY_ATTR
    )
    with _client(restarted) as client:
        listing = _get_listing(client)
    items = _topic_records(listing)
    assert [item["record_id"] for item in items] == [record["record_id"]]
    # 启动只恢复可见性：不自动执行、不批准、不续期
    after = dict(_row(app, record["record_id"]))
    assert after["status"] == "pending"
    assert after["attempt_count"] == before["attempt_count"] == 0
    assert after["claim_owner"] is None and after["claim_until"] is None
    assert after["answer_id"] is None


# ---------------------------------------------------------------------------
# 3) 租约状态与动作
# ---------------------------------------------------------------------------


def test_lease_state_is_reported_and_blocks_double_execution(app):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)

    # 有效租约 = 处理中：动作禁用、给出可读原因
    app.conn.execute(
        f"UPDATE {TABLE} SET claim_owner = 'qio_other|a1', claim_until = "
        "(SELECT datetime('now', '+1 hour')) WHERE record_id = ?",
        (record["record_id"],),
    )
    with _client(app) as client:
        item = _topic_records(_get_listing(client))[0]
    assert item["state_class"] == "answer_in_progress"
    assert item["actions"][0]["id"] == "retry_answer"
    assert item["actions"][0]["enabled"] is False
    assert item["actions"][0]["reason"]

    # 租约失效（例如执行者崩溃）→ 重新可重试
    app.conn.execute(
        f"UPDATE {TABLE} SET claim_until = '2000-01-01T00:00:00+00:00' WHERE record_id = ?",
        (record["record_id"],),
    )
    with _client(app) as client:
        item = _topic_records(_get_listing(client))[0]
    assert item["state_class"] == "answer_pending"
    assert item["actions"][0]["enabled"] is True

    # 直接修复数据让租约彻底失效，再走真实重试（lease 过期可被重新领取）
    app.conn.execute(
        f"UPDATE {TABLE} SET claim_owner = NULL WHERE record_id = ?",
        (record["record_id"],),
    )
    _wire(app)
    result = asyncio.run(retry_topic_answer(app, record["record_id"]))
    assert result["ok"] is True and result["answered"] is True


# ---------------------------------------------------------------------------
# 4) 已完成不被迟到失败覆盖（身份/版本合并）
# ---------------------------------------------------------------------------


async def test_completed_record_is_not_overwritten_by_late_failure_or_stale_listing(app):
    topic_id, _fragment, message_id = _prepare(app)
    _wire(app)
    record = _make_record(app, topic_id, message_id)
    done = await retry_topic_answer(app, record["record_id"])
    assert done["answered"] is True

    # 迟到的失败重放（同身份重放一条失败）：不得打回 pending
    record_topic_failure(
        app,
        record_id=record["record_id"],
        turn_id="turn_r",
        topic_id=topic_id,
        user_message_id=message_id,
        operation_id="op-1",
        failed_stage="answer",
        message="迟到失败",
        stages=TopicStages(context_ready=True),
    )
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == done["answer_id"]

    with _client(app) as client:
        listing = _get_listing(client)
    assert _topic_records(listing) == [], "已完成事项不得再出现在未完成清单里"

    # 已完成后再重试：返回同一 answer_id（不重复生成）
    again = await retry_topic_answer(app, record["record_id"])
    assert again["answered"] is True
    assert again["answer_id"] == done["answer_id"]


def test_classes_filter_applies_to_all_kinds(app):
    """classes 过滤对 topic 恢复事项与既有来源都生效（未知 class 不得退化成全量）。"""
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)
    app.conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, "
        "created_at, updated_at) VALUES ('turn_legacy', '旧消息', ?, 0, 'interrupted', ?, ?)",
        (topic_id, "2026-11-09T0:00:00+00:00", "2026-11-09T0:00:00+00:00"),
    )
    with _client(app) as client:
        only_topic = _get_listing(client, classes="answer_pending")
        assert [item["record_id"] for item in only_topic["records"]] == [
            record["record_id"]
        ], "classes 过滤不得把 user_turn 一起带出来"
        unknown = _get_listing(client, classes="not_a_real_class")
        assert unknown["records"] == [], "未知 class 不能被当成「不过滤」"


# ---------------------------------------------------------------------------
# 4.1) 归属失败恢复（F06 接缝）：先修归属、再装配上下文、再回答
# ---------------------------------------------------------------------------


async def test_retry_ownership_failure_repairs_ownership_then_answers(app):
    old_topic = app.topics.nodes.create_topic("旧话题").id
    old_fragment = app.fragments.get_or_create_open(old_topic)
    message_id, _ = app.memory.append_message(
        topic_id=old_topic,
        role="user",
        content="换到目标话题再回答",
        content_type="text",
        turn_id="turn_own",
    )
    app.bindings.record_binding(
        "turn_own", old_topic, fragment_id=old_fragment.id
    )
    target_topic = app.topics.nodes.create_topic("目标话题").id
    version_before = current_nav_version(app.conn)
    record = record_topic_failure(
        app,
        turn_id="turn_own",
        topic_id=target_topic,
        user_message_id=message_id,
        failed_stage="ownership",
        message="话题已切换，但归属搬迁失败",
        base_nav_version=version_before,
        recovery_nav_version=version_before,
        stages=TopicStages(
            tool_success=True, navigation_applied=True, turn_ownership=False
        ),
    )

    adapter = FakeAdapter()
    real_repair = app.turn_orchestrator.repair_topic_ownership

    class _Orchestrator:
        repair_topic_ownership = real_repair

        async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
            return [
                ChatMessage(role="system", content="SYS"),
                ChatMessage(role="user", content="换到目标话题再回答"),
            ]

    app.turn_orchestrator = _Orchestrator()  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]

    result = await retry_topic_answer(app, record["record_id"])

    assert result["ok"] is True and result["answered"] is True
    binding = app.bindings.binding_for("turn_own")
    assert binding is not None and binding.topic_id == target_topic, "恢复必须先修归属"
    message_row = app.conn.execute(
        "SELECT fragment_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    target_fragment = app.fragments.get_or_create_open(target_topic)
    assert message_row["fragment_id"] == target_fragment.id, (
        "只搬本轮这一条原消息到目标话题片段（不复制）"
    )
    answer_row = app.conn.execute(
        "SELECT fragment_id, turn_id FROM messages WHERE role = 'assistant'"
    ).fetchone()
    assert answer_row is not None
    assert answer_row["fragment_id"] == target_fragment.id
    assert answer_row["turn_id"] == "turn_own"
    assert current_nav_version(app.conn) == version_before, "恢复不推进导航版本"
    assert app.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE role = 'user' AND turn_id = 'turn_own'"
    ).fetchone()["c"] == 1, "不复制用户消息"
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["fragment_id"] == target_fragment.id, (
        "固定片段随真实归属更新（之后重试仍写同一片段）"
    )


async def test_retry_ownership_failure_reports_repair_failure_as_recoverable(app):
    old_topic = app.topics.nodes.create_topic("旧话题").id
    old_fragment = app.fragments.get_or_create_open(old_topic)
    message_id, _ = app.memory.append_message(
        topic_id=old_topic,
        role="user",
        content="换到目标话题",
        content_type="text",
        turn_id="turn_own2",
    )
    app.bindings.record_binding("turn_own2", old_topic, fragment_id=old_fragment.id)
    target_topic = app.topics.nodes.create_topic("目标话题").id
    record = record_topic_failure(
        app,
        turn_id="turn_own2",
        topic_id=target_topic,
        user_message_id=message_id,
        failed_stage="ownership",
        message="归属搬迁失败",
        recovery_nav_version=current_nav_version(app.conn),
        stages=TopicStages(tool_success=True, navigation_applied=True),
    )

    class _FailingOrchestrator:
        async def repair_topic_ownership(self, **kwargs):
            raise RuntimeError("rebind channel down")

        async def prepare_topic_answer_messages(self, **kwargs):  # pragma: no cover
            raise AssertionError("归属没修好之前不得装配上下文")

    app.turn_orchestrator = _FailingOrchestrator()  # type: ignore[assignment]
    result = await retry_topic_answer(app, record["record_id"])

    assert result["ok"] is False
    assert result["code"] == "topic_ownership_failed"
    assert result["failed_stage"] == "ownership"
    assert result["recoverable"] is True
    assert result["status_code"] == 503
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "pending"
    assert stored["failed_stage"] == "ownership"
    assert stored["claim_owner"] is None and stored["claim_until"] is None


# ---------------------------------------------------------------------------
# 4.2) 详情入口：按身份刷新一条恢复事项（pending / answered / 未知）
# ---------------------------------------------------------------------------


def test_record_detail_endpoint_reports_identity_and_status(app):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)
    with _client(app) as client:
        response = client.get("/api/recovery/records/" + record["record_id"])
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["ok"] is True
        assert detail["record_id"] == record["record_id"]
        assert detail["kind"] == "topic_answer_recovery"
        assert detail["state_class"] == "answer_pending"
        assert detail["answered"] is False
        assert detail["turn_id"] == "turn_r"
        assert detail["answer_id"] is None

        missing = client.get("/api/recovery/records/trec_missing_detail")
        assert missing.status_code == 404
        assert missing.json()["code"] == "recovery_not_found"


async def test_record_detail_reports_answered_record(app):
    topic_id, _fragment, message_id = _prepare(app)
    _wire(app)
    record = _make_record(app, topic_id, message_id)
    done = await retry_topic_answer(app, record["record_id"])
    assert done["answered"] is True
    with _client(app) as client:
        response = client.get("/api/recovery/records/" + record["record_id"])
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail["status"] == "answered"
        assert detail["answered"] is True
        assert detail["state_class"] == "answered"
        assert detail["answer_id"] == done["answer_id"]
        assert detail["actions"] == [], "已完成事项不再提供重试动作"


# ---------------------------------------------------------------------------
# 5) retry-answer HTTP 契约
# ---------------------------------------------------------------------------


async def test_retry_answer_http_contract(app):
    topic_id, _fragment, message_id = _prepare(app)
    _wire(app)
    record = _make_record(app, topic_id, message_id)

    with _client(app) as client:
        # 未知记录
        missing = client.post(
            "/api/recovery/records/trec_missing/retry-answer",
            json={"expected_class": "topic_answer_recovery"},
        )
        assert missing.status_code == 404
        assert missing.json()["code"] == "recovery_not_found"

        # 成功 200：带 answer_id / answered
        ok = client.post(
            "/api/recovery/records/{}/retry-answer".format(record["record_id"]),
            json={"expected_class": "topic_answer_recovery"},
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["ok"] is True and body["answered"] is True
        assert body["answer_id"]
        assert body["record_id"] == record["record_id"]

        # 幂等重复：200 + 同一 answer_id
        again = client.post(
            "/api/recovery/records/{}/retry-answer".format(record["record_id"]), json={}
        )
        assert again.status_code == 200
        assert again.json()["answer_id"] == body["answer_id"]

        # class 不符：409
        conflict = client.post(
            "/api/recovery/records/{}/retry-answer".format(record["record_id"]),
            json={"expected_class": "user_turn"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "recovery_class_conflict"


async def test_retry_answer_http_503_when_context_prepare_unavailable(app):
    topic_id, _fragment, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id)
    # 没有可用装配通道（进程刚启动、没有模型凭据）：明确 503 可恢复失败，
    # 不伪造成功；记录保留可重试、租约已释放。
    with _client(app) as client:
        response = client.post(
            "/api/recovery/records/{}/retry-answer".format(record["record_id"]), json={}
        )
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["recoverable"] is True
    assert body["failed_stage"] == "context_prepare"
    assert body["code"] in {
        "recovery_unavailable",
        "topic_context_prepare_failed",
    }
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "pending"
    assert stored["claim_owner"] is None and stored["claim_until"] is None
    assert stored["attempt_count"] == 1
