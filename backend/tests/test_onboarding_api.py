from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_status_first_run_shows_wizard(client):
    body = client.get("/api/onboarding/status").json()
    assert body["done"] is False
    assert body["has_credential"] is False
    assert body["has_name"] is False
    assert body["wizard_seen"] is False
    assert body["welcome_version"] == ""
    assert body["show_wizard"] is True
    assert body["hint_dismissed"] is False


def test_seen_stamps_current_version_and_stops_forcing(client):
    version = client.get("/api/instance").json()["version"]
    seen = client.post("/api/onboarding/seen").json()
    assert seen["wizard_seen"] is True
    assert seen["welcome_version"] == version
    assert seen["show_wizard"] is False
    # 幂等：再打一次仍然稳定
    assert client.post("/api/onboarding/seen").json()["show_wizard"] is False


def test_new_version_forces_wizard_once_even_when_done(client, db_conn):
    client.post("/api/onboarding/seen")
    client.post("/api/onboarding/complete")
    assert client.get("/api/onboarding/status").json()["show_wizard"] is False

    # 模拟「刚更新到这版」：已展示版本落后于当前版本
    from agent.storage.settings import SettingsStore

    SettingsStore(db_conn).set("onboarding.welcome_version", "0.0.0")
    body = client.get("/api/onboarding/status").json()
    assert body["done"] is True
    assert body["show_wizard"] is True
    assert client.post("/api/onboarding/seen").json()["show_wizard"] is False


def test_profile_writes_knowledge_card_and_topics_idempotently(client, db_conn):
    payload = {
        "name": "小舟",
        "intro": "在做本地优先的个人助手",
        "tags": [{"key": "职业", "value": "独立开发者"}],
        "style": "简洁",
        "goals": ["学习", "写作"],
    }
    first = client.post("/api/onboarding/profile", json=payload)
    assert first.status_code == 200
    summary = first.json()
    assert summary["name"] == "小舟"
    assert sorted(summary["topics"]) == ["写作", "学习"]

    # 重复提交不产生重复知识 / 实体卡 / 话题
    client.post("/api/onboarding/profile", json=payload)
    from agent.entities.cards import EntityCardService
    from agent.graph.nodes import NodeService
    from agent.knowledge.lifecycle import KnowledgeService

    profiles = [
        item
        for item in KnowledgeService(db_conn).list_items(category="user_profile")
        if item.state.value == "active"
    ]
    assert len(profiles) == 1
    assert "小舟" in profiles[0].content
    assert len([c for c in EntityCardService(db_conn).list_active() if c.name == "小舟"]) == 1
    assert len([t for t in NodeService(db_conn).list_topics() if t.name == "学习"]) == 1

    status = client.get("/api/onboarding/status").json()
    assert status["has_name"] is True


def test_profile_requires_name(client):
    assert client.post("/api/onboarding/profile", json={"name": "  "}).status_code == 400


def test_complete_and_hint_roundtrip(client):
    assert client.post("/api/onboarding/complete").json()["done"] is True
    assert (
        client.post("/api/onboarding/hint", json={"dismissed": True}).json()["hint_dismissed"]
        is True
    )
    assert (
        client.post("/api/onboarding/hint", json={"dismissed": False}).json()["hint_dismissed"]
        is False
    )
