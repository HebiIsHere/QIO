# -*- coding: utf-8 -*-
"""实体提炼派生任务：输入证据目录必须与输入对齐（回归）。

真实缺陷（v0.3.0 发布提交与当前分支同形）
----------------------------------------
`MemoryLifecycle.run_entities_task` 调实体抽取时没有传 `evidence_catalog`；
抽取侧把「没传」当成**空目录** —— 于是模型即使引用片段里真实存在的消息 id，
也会被判「evidence_ids 不在输入证据目录里」。列表非空但一条都没保住，
可修正层按「无法安全恢复」整批拒绝，派生任务必然失败。

本用例用临时库 + 假适配器：假模型引用片段内真实消息 id，要求：
1. 任务成功（state=completed），实体卡真的写入；
2. 模型收到的输入证据目录里包含这些真实消息 id（不是空目录）。

修前预期：任务返回 False、任务行 state=failed、无实体卡（RED）。
"""

from __future__ import annotations

import asyncio
import json

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks as dt
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _ScriptedAdapter:
    """只回一份固定实体 JSON 的假 provider；同时记录最后一次输入。"""

    mode = "native"
    model = "fake-entity-evidence"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.inputs: list[str] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        system = messages[0]["content"] if isinstance(messages[0], dict) else messages[0].content
        assert '"entities"' in system, "实体提炼必须收到实体 schema"
        last = messages[-1]
        raw = last["content"] if isinstance(last, dict) else last.content
        self.inputs.append(raw)
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=json.dumps(self.payload, ensure_ascii=False),
            )
        )


def _app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "entities_evidence.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("实体证据回归").id
    ctx.memory.append_message(topic_id=topic, role="user", content="我家的鹅最近嘴巴红肿")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了，我留意着")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


def test_entity_task_accepts_real_message_ids_as_evidence(tmp_path):
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    message_ids = [
        str(row["id"])
        for row in ctx.conn.execute(
            "SELECT id FROM messages WHERE fragment_id = ? ORDER BY created_at",
            (sealed.id,),
        ).fetchall()
    ]
    assert len(message_ids) == 2

    version = int(sealed.content_version)
    dt.enqueue(ctx.conn, dt.KIND_ENTITIES, sealed.id, version)
    task = dt.claim_due(ctx.conn, kinds=(dt.KIND_ENTITIES,))[0]

    adapter = _ScriptedAdapter(
        {
            "entities": [
                {
                    "name": "我家的鹅",
                    "identity": "用户家里那只白鹅",
                    "summary": "最近嘴巴红肿",
                    "decision": "create",
                    "reason": "持续健康记录需要",
                    "evidence_ids": [message_ids[0]],
                    "aliases": [],
                    "attributes": [],
                    "relations": [],
                }
            ]
        }
    )
    ok = asyncio.run(ctx.memory_lifecycle.run_entities_task(task, adapter))
    assert ok is True, "模型引用片段内真实消息 id 时，实体提炼任务必须成功"

    # 模型输入的证据目录必须包含片段里的真实消息 id（而不是空目录）。
    prompt = json.loads(adapter.inputs[-1])
    catalog_ids = {str(entry["id"]) for entry in prompt["evidence_catalog"]}
    assert message_ids[0] in catalog_ids, "输入证据目录必须包含片段消息 id"

    row = dt.task_for(ctx.conn, dt.KIND_ENTITIES, sealed.id, version)
    assert row.state == dt.STATE_COMPLETED
    cards = [
        dict(r)
        for r in ctx.conn.execute("SELECT name, state FROM entity_cards").fetchall()
    ]
    assert [card["name"] for card in cards] == ["我家的鹅"]
    ctx.conn.close()
