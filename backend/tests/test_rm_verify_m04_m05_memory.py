# -*- coding: utf-8 -*-
"""F 组独立验证：M04（提炼不得覆盖人工修订 / 不得丢属性）、M05（派生任务重试边界）。

只依据可观察行为断言（SQLite 行、卡片属性、模型调用次数、派生任务状态），
模型一律是本地假 provider，不联网、不调用真实 Key。

* M04 —— 提炼候选只提一个属性时，另一个必须保留；人工纠正过的值在旧结果放行后
  仍然保留；用户删掉的属性不得被普通提炼反转。
* M05 —— 实体提炼首次因结构错误失败后必须留下**可重试的实体任务**；修好后重试
  只跑实体（摘要不得被重复调用）、卡片不重复写。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.entities.cards import (
    EntityAttribute,
    EntityCardCandidate,
    EntityCardService,
)
from agent.services import derived_tasks
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

# ---------------------------------------------------------------------------
# 假 provider：按提示词分派摘要 / 实体 / 知识（本地生成，不联网）
# ---------------------------------------------------------------------------


class _FakeAdapter:
    mode = "native"
    model = "fake-rmf-derivation"

    def __init__(self, *, summary=None, entities=None, knowledge=None) -> None:
        self.summary = (
            json.dumps(
                {"title": "标题", "summary": "这是一段摘要", "entities": [], "keywords": []},
                ensure_ascii=False,
            )
            if summary is None
            else summary
        )
        self.entities = {"entities": []} if entities is None else entities
        self.knowledge = (
            json.dumps({"candidates": []}, ensure_ascii=False)
            if knowledge is None
            else knowledge
        )
        self.calls = {"summary": 0, "entities": 0, "knowledge": 0}

    async def complete(self, messages, tools, **kwargs) -> Completion:
        first = messages[0]
        prompt = first["content"] if isinstance(first, dict) else first.content
        if "summarizing a closed conversation fragment" in prompt:
            key, payload = "summary", self.summary
        elif "extracting durable knowledge" in prompt:
            key, payload = "knowledge", self.knowledge
        elif "实体" in prompt and "提炼" in prompt:
            key, payload = "entities", self.entities
        else:
            raise AssertionError(f"未知提示词：{prompt[:40]!r}")
        self.calls[key] += 1
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return Completion(message=ChatMessage(role="assistant", content=content))


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "rmf.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("F组验证").id
    ctx.memory.append_message(topic_id=topic, role="user", content="我家的鹅最近有点生病")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了，我留意着")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


def _attrs(card) -> dict[str, str]:  # noqa: ANN001 - EntityCard
    return {str(a.get("key")): str(a.get("value")) for a in card.attributes}


def _candidate(**kwargs) -> EntityCardCandidate:
    return EntityCardCandidate(**kwargs)


def _attribute(key: str, value: str) -> EntityAttribute:
    return EntityAttribute(key=key, value=value)


@pytest.fixture()
def entity_client(db_conn, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# M04
# ---------------------------------------------------------------------------


def test_m04_extraction_keeps_attributes_the_candidate_did_not_mention(db_conn):
    svc = EntityCardService(db_conn)
    card = svc.upsert(
        _candidate(
            name="我家的鹅",
            attributes=[_attribute("状态", "生病"), _attribute("颜色", "白")],
        )
    )
    # 新候选只提一个属性（真实提炼里很常见：这一轮只说了状态）
    svc.upsert(_candidate(name="我家的鹅", attributes=[_attribute("状态", "康复")]))

    got = svc.get(card.id)
    assert got is not None
    attrs = _attrs(got)
    assert attrs.get("颜色") == "白", f"候选没提到的属性不得被删掉：{attrs}"
    assert attrs.get("状态") == "康复", f"候选提到的属性应当更新：{attrs}"


def test_m04_manual_edit_via_management_api_survives_released_extraction(
    entity_client, db_conn
):
    """暂停提炼期间人工纠正 → 放行旧结果（提炼候选）后人工值仍保留（管理 API 入口）。"""
    svc = EntityCardService(db_conn)
    card = svc.upsert(_candidate(name="我家的鹅", attributes=[_attribute("状态", "生病")]))

    resp = entity_client.post(
        f"/api/entities/{card.id}/revise",
        json={"attributes": [{"key": "状态", "value": "已康复"}]},
    )
    assert resp.status_code == 200, resp.text
    assert _attrs(svc.get(card.id)).get("状态") == "已康复"

    # 放行旧提炼结果：仍然带着纠正之前的旧值
    svc.upsert(_candidate(name="我家的鹅", attributes=[_attribute("状态", "生病")]))

    attrs = _attrs(svc.get(card.id))
    assert attrs.get("状态") == "已康复", (
        f"人工纠正过的值不得被放行的旧提炼结果覆盖回来：{attrs}"
    )


def test_m04_user_deleted_attribute_is_not_revived_by_extraction(entity_client, db_conn):
    """用户删掉属性之后，普通提炼重放同一条候选不得把它加回来（纠正工具入口）。"""
    svc = EntityCardService(db_conn)
    card = svc.upsert(
        _candidate(
            name="我家的鹅",
            attributes=[_attribute("状态", "生病"), _attribute("颜色", "白")],
        )
    )

    async def _delete() -> None:
        from agent.tools.entity_tools import CorrectEntityTool

        result = await CorrectEntityTool(db_conn).run(
            entity="我家的鹅", attribute_key="颜色", delete_attribute=True
        )
        assert result.ok, result

    asyncio.run(_delete())
    assert "颜色" not in _attrs(svc.get(card.id))

    # 普通提炼重放旧候选（同一轮的结果迟到）
    svc.upsert(
        _candidate(
            name="我家的鹅",
            attributes=[_attribute("状态", "生病"), _attribute("颜色", "白")],
        )
    )

    attrs = _attrs(svc.get(card.id))
    assert "颜色" not in attrs, f"用户删除的属性和提炼不得被反转回来：{attrs}"


# ---------------------------------------------------------------------------
# M05
# ---------------------------------------------------------------------------


def test_m05_entity_failure_leaves_a_retryable_task_and_only_it_retries(tmp_path: Path):
    ctx = _app(tmp_path)
    try:
        sealed = _seal(ctx)
        version = int(sealed.content_version or 0)

        # 第一次提炼：摘要与知识正常，实体卡输出结构错误（无法安全恢复）
        bad = _FakeAdapter(entities={"entities": [{"name": 123}]})
        asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(bad, limit=5))

        summary_row = ctx.conn.execute(
            "SELECT summary FROM fragments WHERE id = ?", (sealed.id,)
        ).fetchone()
        assert (summary_row["summary"] or "").strip(), "摘要这一轮应当已经成功"

        task = derived_tasks.task_for(
            ctx.conn, derived_tasks.KIND_ENTITIES, sealed.id, version
        )
        assert task is not None and task.state != derived_tasks.STATE_COMPLETED, (
            "实体提炼失败必须留下一条可重试的实体任务，否则永远不会再试；"
            f"task={task!r}"
        )

        # 故障解除（退避拨回过去）→ 只重试实体，摘要不得被重复调用
        ctx.conn.execute(
            "UPDATE derived_tasks SET run_after = NULL, state = ? WHERE kind = ?",
            (derived_tasks.STATE_PENDING, derived_tasks.KIND_ENTITIES),
        )
        good = _FakeAdapter(
            entities={
                "entities": [
                    {
                        "name": "我家的鹅",
                        "attributes": [{"key": "状态", "value": "生病"}],
                        "relations": [],
                    }
                ]
            }
        )
        asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(good, limit=5))

        assert good.calls["summary"] == 0, "摘要已经成功，重试实体时不得重复调用摘要"
        assert good.calls["entities"] == 1, f"实体应当重试并成功：{good.calls}"

        count = ctx.conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"]
        assert count == 1, f"实体卡应当被写一遍：{count}"

        # 重复 drain：不重复写卡片
        asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(good, limit=5))
        count_again = ctx.conn.execute("SELECT COUNT(*) c FROM entity_cards").fetchone()["c"]
        assert count_again == 1, f"重复 drain 不得重复写卡片：{count_again}"
    finally:
        ctx.conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
