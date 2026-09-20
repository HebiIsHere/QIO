"""高影响知识候选：回答完成后在对话里自然确认（第三阶段 spec 第 14~19 条）。

* 只有高影响候选（用户画像 / Agent 自我 / 目标）才出现在对话里；
* 低影响候选照旧自动生效，不打扰用户；
* 用户忽略过的同一条内容不再重复提示；
* 「忽略」与「打回草稿」不是同一件事。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.memory.summary import FragmentSummary
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

CANDIDATES = {
    "candidates": [
        {
            "content": "用户偏好用中文沟通",
            "category": "user_profile",
            "attach": "user",
            "entity": None,
        },
        {
            "content": "QIO 后端默认跑在 8734 端口",
            "category": "general_fact",
            "attach": "topic",
            "entity": None,
        },
    ]
}

LOW_IMPACT_ONLY = {
    "candidates": [
        {
            "content": "QIO 后端默认跑在 8734 端口",
            "category": "general_fact",
            "attach": "topic",
            "entity": None,
        }
    ]
}


class ExtractionAdapter:
    """只回答知识抽取那一次调用。"""

    mode = "native"
    model = "fake-model"

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or CANDIDATES
        self.calls = 0

    async def complete(self, messages, tools, **kwargs) -> Completion:
        self.calls += 1
        return Completion(
            message=ChatMessage(
                role="assistant", content=json.dumps(self.payload, ensure_ascii=False)
            )
        )


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


def _summary() -> FragmentSummary:
    return FragmentSummary(title="偏好与端口", summary="用户说明了自己的沟通偏好。")


async def _seed_candidates(ctx: AppContext, payload: dict | None = None) -> str:
    topic = ctx.topics.nodes.create_topic("话题甲")
    await ctx.memory_lifecycle.extract_knowledge(
        ExtractionAdapter(payload),
        _summary(),
        topic.id,
        [],
        f"frag_{topic.id[-6:]}",
    )
    return topic.id


async def _collect(ctx: AppContext, names: set[str]):
    collected: list[dict] = []

    async def consumer() -> None:
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] in names:
                        collected.append(evt)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish() -> None:
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return collected, finish


async def test_extraction_queues_only_high_impact_candidates(ctx: AppContext):
    await _seed_candidates(ctx)
    queued = ctx.memory_lifecycle.take_knowledge_candidates()

    assert [c["category"] for c in queued] == ["user_profile"]
    assert queued[0]["content"] == "用户偏好用中文沟通"
    # 低影响候选照旧自动生效，不进候选队列
    states = {
        row["content"]: row["state"]
        for row in ctx.conn.execute("SELECT content, state FROM knowledge").fetchall()
    }
    assert states["QIO 后端默认跑在 8734 端口"] == "active"
    assert states["用户偏好用中文沟通"] == "pending_review"


async def test_high_impact_candidate_is_announced_once_after_answer(ctx: AppContext):
    await _seed_candidates(ctx)
    collected, finish = await _collect(ctx, {"KNOWLEDGE_CANDIDATE"})
    await ctx.emit_knowledge_candidates("turn-1")
    # 队列在发出后即清空：同一批候选不会因为下一轮又提示一遍
    await ctx.emit_knowledge_candidates("turn-2")
    await finish()

    assert len(collected) == 1
    data = collected[0]["data"]
    assert data["impact"] == "high"
    assert data["category"] == "user_profile"
    assert data["content"] == "用户偏好用中文沟通"
    assert data["turn_id"] == "turn-1"
    assert data["reason"]
    row = ctx.conn.execute(
        "SELECT id FROM knowledge WHERE content = ?", (data["content"],)
    ).fetchone()
    assert data["knowledge_id"] == row["id"]


async def test_low_impact_candidates_are_never_announced(ctx: AppContext):
    await _seed_candidates(ctx, LOW_IMPACT_ONLY)
    collected, finish = await _collect(ctx, {"KNOWLEDGE_CANDIDATE"})
    await ctx.emit_knowledge_candidates("turn-1")
    await finish()
    assert collected == []


async def test_ignored_candidate_is_not_announced_again(ctx: AppContext):
    """用户说过不要的知识，不能换个轮次再问一遍。"""
    await _seed_candidates(ctx)
    candidate = ctx.memory_lifecycle.take_knowledge_candidates()[0]

    async with _client(ctx) as client:
        resp = await client.post(f"/api/knowledge/{candidate['knowledge_id']}/ignore")
        assert resp.status_code == 200

    # 下一轮模型又抽到同一条内容
    await _seed_candidates(ctx)
    collected, finish = await _collect(ctx, {"KNOWLEDGE_CANDIDATE"})
    await ctx.emit_knowledge_candidates("turn-2")
    await finish()
    assert collected == []


def _client(ctx: AppContext):
    from agent.api.server import create_app

    app = create_app(ctx.settings, ctx.conn)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_ignore_endpoint_revokes_and_is_idempotent(ctx: AppContext):
    await _seed_candidates(ctx)
    candidate = ctx.memory_lifecycle.take_knowledge_candidates()[0]
    knowledge_id = candidate["knowledge_id"]

    async with _client(ctx) as client:
        missing = await client.post("/api/knowledge/kn_missing/ignore")
        assert missing.status_code == 404

        first = await client.post(f"/api/knowledge/{knowledge_id}/ignore")
        assert first.status_code == 200
        assert first.json() == {"ok": True, "knowledge_id": knowledge_id}

        second = await client.post(f"/api/knowledge/{knowledge_id}/ignore")
        assert second.status_code == 200
        assert second.json()["ok"] is True

    row = ctx.conn.execute(
        "SELECT state, provenance FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    assert row["state"] == "revoked"
    provenance = json.loads(row["provenance"])
    assert provenance["reason"] == "user_ignored"
    assert provenance["ignored_at"]


async def test_same_category_is_not_asked_again_after_ignore(ctx: AppContext):
    """忽略之后，同类的**换句话**候选也不能马上再问一遍。

    真实运行观察到的问题：用户忽略「用户偏好默认用中文回复…」后，下一轮模型
    换了个说法（「用户更喜欢简洁、不啰嗦的解释风格…」）又变成候选弹出来。
    只按字符串去重挡不住这件事，所以按类别做冷却。
    """
    rephrased = {
        "candidates": [
            {
                "content": "用户更喜欢简洁、不啰嗦的解释风格",
                "category": "user_profile",
                "attach": "user",
                "entity": None,
            }
        ]
    }
    await _seed_candidates(ctx)
    candidate = ctx.memory_lifecycle.take_knowledge_candidates()[0]
    async with _client(ctx) as client:
        assert (await client.post(f"/api/knowledge/{candidate['knowledge_id']}/ignore")).status_code == 200

    # 换一句话、同一类别
    await _seed_candidates(ctx, rephrased)
    collected, finish = await _collect(ctx, {"KNOWLEDGE_CANDIDATE"})
    await ctx.emit_knowledge_candidates("turn-3")
    await finish()
    assert collected == [], "同类候选在冷却期内不应再弹到对话里"

    # 冷却期过后可以再问（时间到了就不该一直沉默）
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ctx.settings_store.set("knowledge.ignored_at.user_profile", old)
    await _seed_candidates(ctx, rephrased)
    collected2, finish2 = await _collect(ctx, {"KNOWLEDGE_CANDIDATE"})
    await ctx.emit_knowledge_candidates("turn-4")
    await finish2()
    assert len(collected2) == 1
