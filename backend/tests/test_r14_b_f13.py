"""R14-B / F13：原片段封存后恢复答案仍可保存并出现在原轮次/检索/导出。

旧实现：retry 把回答写入话题**当前开放片段**（MemoryWriter 按 topic 找 open fragment），
封存后的原片段被绕过或直接失败 —— 迟到答案要么丢失、要么出现在错误的新片段里。

本文件验证（模型 fake；真实临时 SQLite）：
* 恢复前封存 / 生成中封存 → 答案写进原片段（closed_at 不变、不重开、不写当前新片段）；
* 连续容量分段：答案归属原片段、只在原问题之后出现一次；
* 与 F03 共用 answer_key 与事务收口：同一逻辑答案只有一份正文、记录同事务 answered；
* 历史加载 / 检索 / 冷归档导出都包含它；
* 恢复不推进导航版本、不覆盖用户后来的导航（recovery_nav_version 复核保留）。
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.navigation_version import advance as advance_nav_version
from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import TopicStages
from agent.services.topic_recovery import record_topic_failure, retry_topic_answer
from agent.storage.archive import archive_messages, restore_message_content
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_MARKER = "[[QIO:ANSWER]]"
ANSWER_TEXT = "封存后的迟到答案正文"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "r14_b_f13.db"


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

    def __init__(self, *, text: str = ANSWER_MARKER + "\n" + ANSWER_TEXT) -> None:
        self.text = text
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
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


def _wire(app: AppContext, adapter: FakeAdapter | None = None) -> FakeAdapter:
    adapter = adapter or FakeAdapter()
    app.turn_orchestrator = FakeOrchestrator()  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]
    return adapter


def _prepare(app: AppContext, *, turn_id: str = "turn_r"):
    """一个话题 + 原片段 + 原用户消息（问题已落库，回答尚未生成）。"""
    topic_id = app.topics.nodes.create_topic("目标话题").id
    fragment = app.fragments.get_or_create_open(topic_id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="封存前的问题",
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _make_record(app: AppContext, topic_id: str, fragment_id: str, message_id: str, **overrides):
    now_version = current_nav_version(app.conn)
    payload = {
        "turn_id": "turn_r",
        "topic_id": topic_id,
        "from_topic_id": topic_id,
        "fragment_id": fragment_id,
        "user_message_id": message_id,
        "operation_id": "op-1",
        "param_version": 1,
        "base_nav_version": now_version,
        "recovery_nav_version": now_version,
        "failed_stage": "answer",
        "message": "话题已切换，回答生成失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    return record_topic_failure(app, **payload)


def _fragment_row(app: AppContext, fragment_id: str):
    return app.conn.execute(
        "SELECT * FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()


def _assistant_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT * FROM messages WHERE role = 'assistant'"
    ).fetchall()


# ---------------------------------------------------------------------------
# 1) 恢复前封存：答案写进原片段
# ---------------------------------------------------------------------------


async def test_late_answer_saved_into_sealed_fragment(app, db_path, tmp_path):
    topic_id, fragment_id, message_id = _prepare(app)
    record = _make_record(app, topic_id, fragment_id, message_id)

    # 恢复前封存（容量封存，摘要保留旧版本）
    app.fragments.close(fragment_id, "封存时的摘要", summary_version=1)
    sealed = dict(_fragment_row(app, fragment_id))
    assert sealed["closed_at"] is not None
    closed_at_before = sealed["closed_at"]

    # 用户在同一话题继续（新的开放片段 + 新轮次消息）
    new_fragment = app.fragments.get_or_create_open(topic_id)
    assert new_fragment.id != fragment_id
    later_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="后来的新问题",
        content_type="text",
        turn_id="turn_r2",
    )

    # 「进程重启」后重试：持久记录 + 封存片段
    restarted = _new_app(db_path, tmp_path)
    adapter = _wire(restarted)
    version_before = current_nav_version(restarted.conn)

    result = await retry_topic_answer(restarted, record["record_id"])

    assert result["ok"] is True and result["answered"] is True
    assert adapter.calls == 1
    rows = _assistant_rows(restarted)
    assert len(rows) == 1, "迟到答案只能有一份"
    answer_row = dict(rows[0])
    assert answer_row["fragment_id"] == fragment_id, "必须写回原片段（不是当前新片段）"
    assert answer_row["turn_id"] == "turn_r"
    assert answer_row["late_answer_of_message_id"] == message_id
    assert answer_row["content"] == ANSWER_TEXT
    assert answer_row["answer_key"] == hashlib.sha256(
        f"turn_r:{message_id}".encode()
    ).hexdigest()
    assert answer_row["id"] == result["answer_id"]

    # 不重开/不改写已封存片段
    sealed_after = dict(_fragment_row(restarted, fragment_id))
    assert sealed_after["closed_at"] == closed_at_before
    assert sealed_after["summary"] == "封存时的摘要"
    # 不写当前新片段；不新建重复轮次/片段
    assert restarted.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE fragment_id = ? AND role = 'assistant'",
        (new_fragment.id,),
    ).fetchone()["c"] == 0
    assert restarted.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 2
    assert restarted.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE role = 'user'"
    ).fetchone()["c"] == 2, "不搬动/不复制原问题"
    # 不推进导航版本（容量封存不是用户导航，恢复也不是）
    assert current_nav_version(restarted.conn) == version_before

    # 记录同事务收口 answered（F03 共用事务/身份）
    stored = topic_recovery.get_record(restarted, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == result["answer_id"]

    # 历史加载：出现在原问题之后、同片段；当前对话页可见一次
    history = restarted.session_messages(topic_id)
    ids = [item["id"] for item in history]
    assert ids.count(answer_row["id"]) == 1
    assert ids.index(answer_row["id"]) > ids.index(message_id)
    page = restarted.session_messages_page(topic_id)
    assert answer_row["id"] in [item["id"] for item in page["messages"]]
    fragment_messages = restarted.fragments.messages(fragment_id)
    assert [item["id"] for item in fragment_messages].count(answer_row["id"]) == 1

    # 检索（原文检索）不能漏
    hits = restarted.retriever.search_messages("迟到答案", topic_id=topic_id, top_k=5)
    assert any(hit.message_id == answer_row["id"] for hit in hits), hits

    # 冷归档「导出」路径包含它，且可按真实身份还原
    archive_dir = tmp_path / "archive"
    archived = archive_messages(restarted.conn, archive_dir, older_than_days=0)
    assert archived >= 1
    assert restore_message_content(restarted.conn, answer_row["id"]) == ANSWER_TEXT
    files = list(archive_dir.glob("*.jsonl.gz"))
    assert files, "归档文件应包含迟到答案"
    import gzip, json

    exported_ids = set()
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                exported_ids.add(json.loads(line).get("_archived_message_id"))
    assert answer_row["id"] in exported_ids


# ---------------------------------------------------------------------------
# 2) 生成中封存：模型调用期间片段被容量封存，答案仍然归原片段
# ---------------------------------------------------------------------------


async def test_answer_generated_while_fragment_is_sealed_mid_flight(app):
    topic_id, fragment_id, message_id = _prepare(app)
    adapter = _wire(app)
    adapter.release.clear()
    record = _make_record(app, topic_id, fragment_id, message_id)
    assert topic_recovery.get_record(app, record["record_id"])["answer_text"] in (None, "")

    task = asyncio.create_task(retry_topic_answer(app, record["record_id"]))
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    # 生成中：片段被容量封存
    app.fragments.close(fragment_id, "生成中封存的摘要")
    sealed_at = dict(_fragment_row(app, fragment_id))["closed_at"]
    assert sealed_at is not None
    adapter.release.set()
    result = await asyncio.wait_for(task, timeout=5)

    assert result["ok"] is True and result["answered"] is True
    row = dict(_assistant_rows(app)[0])
    assert row["fragment_id"] == fragment_id
    assert dict(_fragment_row(app, fragment_id))["closed_at"] == sealed_at, (
        "封存后不得被恢复重开或改写封存时间"
    )
    assert app.fragments.open_fragment(topic_id) is None, "不得为了写答案新建/占用当前开放片段"


# ---------------------------------------------------------------------------
# 3) 连续容量分段：只写原片段，不写后续片段
# ---------------------------------------------------------------------------


async def test_consecutive_capacity_segments_keep_answer_in_original_segment(app):
    topic_id, fragment_id, message_id = _prepare(app)
    record = _make_record(app, topic_id, fragment_id, message_id)

    # 容量分段：封存原片段 → 新片段接着（来源/同阶段）
    app.fragments.close(fragment_id, "第一段摘要")
    app.fragments.mark_continuation(topic_id, fragment_id, same_stage=True, reason="capacity")
    second = app.fragments.get_or_create_open(topic_id)
    assert second.source_fragment_id == fragment_id
    second_user, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="第二段的问题",
        content_type="text",
        turn_id="turn_r2",
    )

    adapter = _wire(app)
    result = await retry_topic_answer(app, record["record_id"])
    assert result["ok"] is True and result["answered"] is True

    rows = _assistant_rows(app)
    assert len(rows) == 1
    assert dict(rows[0])["fragment_id"] == fragment_id
    assert app.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE fragment_id = ? AND role = 'assistant'",
        (second.id,),
    ).fetchone()["c"] == 0

    # 历史顺序：原问题 → 迟到答案；第二段消息仍在（不搬、不复制）
    ids = [item["id"] for item in app.session_messages(topic_id)]
    assert ids.index(result["answer_id"]) > ids.index(message_id)
    assert second_user in ids
    # 第二段摘要保留旧版本（不伪装已包含迟到答案）
    assert dict(_fragment_row(app, fragment_id))["summary"] == "第一段摘要"


# ---------------------------------------------------------------------------
# 4) 恢复不覆盖用户后来的导航（保留 recovery_nav_version 复核）
# ---------------------------------------------------------------------------


async def test_late_answer_retry_respects_later_user_navigation(app):
    topic_id, fragment_id, message_id = _prepare(app)
    record = _make_record(app, topic_id, fragment_id, message_id)
    app.fragments.close(fragment_id, "封存摘要")
    closed_before = dict(_fragment_row(app, fragment_id))["closed_at"]

    # 用户后来真的导航到另一个话题（推进导航版本）
    other = app.topics.nodes.create_topic("用户后来打开的话题").id
    app.navigation.enter_topic(other)
    adapter = _wire(app)

    result = await retry_topic_answer(app, record["record_id"])
    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["code"] == "topic_application_stale"
    assert result["expected_nav_version"] == record["recovery_nav_version"]
    assert result["actual_nav_version"] == current_nav_version(app.conn)
    assert adapter.calls == 0, "stale 时不得重新生成回答"
    assert _assistant_rows(app) == []
    assert dict(_fragment_row(app, fragment_id))["closed_at"] == closed_before
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "pending", "stale ≠ 拒绝：记录保留可恢复"


# ---------------------------------------------------------------------------
# 5) 与 F03 共用唯一答案身份：两个恢复记录 → 只有一份正文
# ---------------------------------------------------------------------------


async def test_late_answer_shares_answer_key_across_records(app):
    topic_id, fragment_id, message_id = _prepare(app)
    app.fragments.close(fragment_id, "封存摘要")
    first = _make_record(app, topic_id, fragment_id, message_id, record_id="trec_late_first")
    second = _make_record(app, topic_id, fragment_id, message_id, record_id="trec_late_second")
    assert first["answer_key"] == second["answer_key"]
    adapter = _wire(app)

    done = await retry_topic_answer(app, first["record_id"])
    again = await retry_topic_answer(app, second["record_id"])
    assert done["answered"] is True and again["answered"] is True
    assert again["answer_id"] == done["answer_id"]
    assert adapter.calls == 1, "同一逻辑答案已存在时不得重复生成"
    assert len(_assistant_rows(app)) == 1
    assert topic_recovery.get_record(app, second["record_id"])["status"] == "answered"
