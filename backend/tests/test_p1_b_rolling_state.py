# -*- coding: utf-8 -*-
"""Phase 1 / B：滚动摘要覆盖位置与输入版本（迁移 44）持久化、幂等、重启恢复。

验收点：
* 只处理未覆盖的新消息；已覆盖原文不再进模型输入；
* 覆盖位置 / 输入版本持久化（迁移 44 列；迁移未落地时降级到 fragments.meta）；
* 同一输入重跑幂等（不再调用模型）；重启后可恢复；
* 崩溃 / 取消不推进游标；覆盖位置只前进；摘要写入与游标推进同一事务。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.rolling import (
    COVERED_COUNT,
    COVERED_MESSAGE_ID,
    INPUT_VERSION,
    UPDATED_AT,
    compute_input_version,
    covered_message_ids,
    load_state,
    rolling_view,
    save_state,
)
from agent.services.app import AppContext
from agent.storage import migration_p1_b
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _payload(summary: str) -> str:
    return json.dumps(
        {"title": "滚动", "summary": summary, "entities": [], "keywords": []},
        ensure_ascii=False,
    )


def _content(message) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(message.content or "")


class _RollingAdapter:
    """只服务滚动摘要；记录每次输入（previous_summary + new_messages）。"""

    mode = "native"
    model = "fake-p1b-rolling"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        texts = [_content(m) for m in messages]
        data = json.loads(texts[1]) if len(texts) > 1 else {}
        self.calls.append(data)
        if self.fail:
            raise RuntimeError("rolling 模型失败")
        ids = [str(m["message_id"]) for m in data.get("new_messages", [])]
        previous = data.get("previous_summary")
        summary = (f"{previous}|" if previous else "") + "滚动:" + ",".join(ids)
        return Completion(message=ChatMessage(role="assistant", content=_payload(summary)))


def _app(tmp_path: Path, *, apply_p1: bool = True) -> AppContext:
    conn = connect(tmp_path / f"rolling_{int(apply_p1)}.db")
    apply_migrations(conn)
    if apply_p1:
        migration_p1_b.apply(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _append(ctx: AppContext, topic: str, user_text: str, assistant_text: str) -> None:
    ctx.memory.append_message(topic_id=topic, role="user", content=user_text)
    ctx.memory.append_message(topic_id=topic, role="assistant", content=assistant_text)


def _expire_cooldown(ctx: AppContext, fragment_id: str) -> None:
    row = ctx.conn.execute(
        "SELECT meta FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    meta = json.loads(row["meta"] or "{}")
    meta["consolidated_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    ctx.conn.execute(
        "UPDATE fragments SET meta = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), fragment_id),
    )


def _rolling_row(ctx: AppContext, fragment_id: str) -> dict:
    return dict(
        ctx.conn.execute(
            "SELECT summary, rolling_covered_message_id, rolling_covered_count, "
            "rolling_input_version, rolling_updated_at FROM fragments WHERE id = ?",
            (fragment_id,),
        ).fetchone()
    )


def _message_ids(ctx: AppContext, fragment_id: str) -> list[str]:
    return [
        str(row["id"])
        for row in ctx.conn.execute(
            "SELECT id FROM messages WHERE fragment_id = ? ORDER BY rowid",
            (fragment_id,),
        ).fetchall()
    ]


def test_consolidate_persists_cursor_and_only_new_messages(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = ctx.topics.nodes.create_topic("滚动").id
    _append(ctx, topic, "第一条：我养了一只鹅", "记下了")
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    adapter = _RollingAdapter()

    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is True
    ids = _message_ids(ctx, fragment.id)
    row = _rolling_row(ctx, fragment.id)
    assert row["rolling_covered_message_id"] == ids[-1]
    assert int(row["rolling_covered_count"]) == len(ids)
    assert row["rolling_input_version"] == compute_input_version(
        fragment.id, covered_message_ids(ctx.conn, fragment.id, ids[-1])
    )
    assert row["rolling_updated_at"]
    first_input = adapter.calls[0]
    assert [str(m["message_id"]) for m in first_input["new_messages"]] == ids
    assert first_input["previous_summary"] is None

    # 同一输入重跑：冷却窗口内不调用模型；冷却过后也没有新消息 → 幂等返回。
    calls_before = len(adapter.calls)
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is False
    _expire_cooldown(ctx, fragment.id)
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is False
    assert len(adapter.calls) == calls_before, "没有新消息时不得重复调用模型"
    assert int(_rolling_row(ctx, fragment.id)["rolling_covered_count"]) == len(ids)

    # 新增两条消息：只把这 2 条喂给模型，输入里带已有摘要（不是重复喂已覆盖原文）。
    _append(ctx, topic, "第二条：它今天有点生病", "我留意着")
    _expire_cooldown(ctx, fragment.id)
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is True
    new_ids = _message_ids(ctx, fragment.id)[len(ids):]
    assert len(new_ids) == 2
    second_input = adapter.calls[-1]
    assert [str(m["message_id"]) for m in second_input["new_messages"]] == new_ids
    old_text = "第一条：我养了一只鹅"
    assert old_text not in json.dumps(second_input["new_messages"], ensure_ascii=False)
    assert second_input["previous_summary"], "更新摘要必须基于已有滚动摘要"
    advanced = _rolling_row(ctx, fragment.id)
    assert advanced["rolling_covered_message_id"] == new_ids[-1]
    assert int(advanced["rolling_covered_count"]) == len(ids) + len(new_ids)


def test_cursor_survives_restart(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = ctx.topics.nodes.create_topic("重启").id
    _append(ctx, topic, "重启前消息", "收到")
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    adapter = _RollingAdapter()
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is True

    restarted = AppContext(Settings(data_dir=tmp_path), ctx.conn, EventBus())
    restarted.credentials._kr = MemoryKeyring()
    state = load_state(restarted.conn, fragment.id)
    assert state.covered_message_id == _message_ids(ctx, fragment.id)[-1]
    assert state.covered_count == 2
    assert state.input_version
    _expire_cooldown(restarted, fragment.id)
    calls_before = len(adapter.calls)
    assert asyncio.run(restarted.memory_lifecycle.consolidate(topic, adapter)) is False
    assert len(adapter.calls) == calls_before, "重启后不得重跑已覆盖输入"
    view = rolling_view(restarted.conn, fragment.id)
    assert view.summary and view.uncovered == ()


def test_failed_consolidation_does_not_advance_cursor(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = ctx.topics.nodes.create_topic("失败").id
    _append(ctx, topic, "旧消息", "收到")
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    good = _RollingAdapter()
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, good)) is True
    before = _rolling_row(ctx, fragment.id)

    _append(ctx, topic, "新消息一", "收到")
    _append(ctx, topic, "新消息二", "收到")
    _expire_cooldown(ctx, fragment.id)
    bad = _RollingAdapter(fail=True)
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, bad)) is False
    after = _rolling_row(ctx, fragment.id)
    assert after["rolling_covered_message_id"] == before["rolling_covered_message_id"]
    assert after["rolling_covered_count"] == before["rolling_covered_count"]
    assert after["summary"] == before["summary"], "失败不得覆盖已有滚动摘要"
    # 原文保留，任务可重试
    assert ctx.memory_lifecycle.fragments.message_count(fragment.id) == 6
    _expire_cooldown(ctx, fragment.id)
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, good)) is True
    retried = good.calls[-1]
    retried_ids = [str(m["message_id"]) for m in retried["new_messages"]]
    assert len(retried_ids) == 4, "失败那次读到的 4 条未覆盖消息全部补上，不跳过"
    assert int(_rolling_row(ctx, fragment.id)["rolling_covered_count"]) == 6


def test_cursor_only_moves_forward(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = ctx.topics.nodes.create_topic("只前进").id
    _append(ctx, topic, "消息一", "收到")
    _append(ctx, topic, "消息二", "收到")
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    adapter = _RollingAdapter()
    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is True
    ids = _message_ids(ctx, fragment.id)
    before = _rolling_row(ctx, fragment.id)

    # 试图用更旧的游标写回：不得前进到旧位置、不得覆盖摘要。
    moved = save_state(
        ctx.conn,
        fragment.id,
        summary="回退摘要",
        summary_model="fake",
        meta={},
        covered_message_id=ids[0],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    assert moved is False
    after = _rolling_row(ctx, fragment.id)
    assert after["rolling_covered_message_id"] == before["rolling_covered_message_id"]
    assert after["summary"] == before["summary"]


def test_meta_fallback_when_migration_not_applied(tmp_path: Path):
    ctx = _app(tmp_path, apply_p1=False)
    topic = ctx.topics.nodes.create_topic("降级").id
    _append(ctx, topic, "降级消息", "收到")
    fragment = ctx.memory_lifecycle.fragments.open_fragment(topic)
    adapter = _RollingAdapter()

    assert asyncio.run(ctx.memory_lifecycle.consolidate(topic, adapter)) is True
    row = ctx.conn.execute(
        "SELECT meta FROM fragments WHERE id = ?", (fragment.id,)
    ).fetchone()
    meta = json.loads(row["meta"] or "{}")
    assert meta[COVERED_MESSAGE_ID] == _message_ids(ctx, fragment.id)[-1]
    assert int(meta[COVERED_COUNT]) == 2
    assert meta[INPUT_VERSION]
    assert meta[UPDATED_AT]
    # 列不存在时读路径同样能恢复
    state = load_state(ctx.conn, fragment.id)
    assert state.covered_message_id == meta[COVERED_MESSAGE_ID]
    assert state.covered_count == int(meta[COVERED_COUNT])