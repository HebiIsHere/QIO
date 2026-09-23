from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.services.followups import suggest_follow_ups, validate_questions


class _StubAdapter:
    """只回一段固定 JSON 的假模型（用于验证解析与端点接通，不联网）。"""

    model = "stub-model"

    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def complete(self, messages, tools=None, temperature=0.0):
        return SimpleNamespace(message=SimpleNamespace(content=self.payload))


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_validate_questions_keeps_at_most_two():
    questions, error = validate_questions(
        '```json\n{"questions": ["一?", "二?", "三?"]}\n```'
    )
    assert error is None
    assert questions == ["一?", "二?"]


def test_validate_questions_rejects_broken_output():
    questions, error = validate_questions("我觉得可以问三个问题")
    assert questions == []
    assert error


@pytest.mark.asyncio
async def test_suggest_follow_ups_reads_the_description():
    adapter = _StubAdapter('{"questions": ["你说的记忆问题具体指哪一类？"]}')
    questions, error = await suggest_follow_ups(adapter, "最近在开发 QIO")
    assert error is None
    assert questions == ["你说的记忆问题具体指哪一类？"]


def test_endpoint_returns_nothing_without_a_model(client, db_conn):
    """没有可用模型时追问直接不出现（用户可见的行为）。"""
    client.app.state.ctx.build_adapter = _no_adapter
    body = client.post("/api/onboarding/followups", json={"description": "最近在开发 QIO"}).json()
    assert body["questions"] == []


def test_endpoint_returns_nothing_without_description(client):
    body = client.post("/api/onboarding/followups", json={"description": "   "}).json()
    assert body["questions"] == []


def test_endpoint_uses_the_model_when_available(client):
    async def _adapter():
        return _StubAdapter('{"questions": ["你希望我先讲逻辑还是先给代码？"]}')

    client.app.state.ctx.build_adapter = _adapter
    body = client.post(
        "/api/onboarding/followups", json={"description": "讨论方案时希望先解释思路"}
    ).json()
    assert body["questions"] == ["你希望我先讲逻辑还是先给代码？"]


async def _no_adapter():
    return None
