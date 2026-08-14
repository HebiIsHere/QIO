"""知识管理 API：列表/手动新建/批准/打回。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def test_knowledge_manage_flow(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ks = KnowledgeService(conn)
    item = ks.create(category="general_fact", content="待审核条目")
    ks.submit(item.id)
    app = create_app(Settings(data_dir=tmp_path), conn)
    client = TestClient(app)

    # 列表：包含该条目且结构化
    r = client.get("/api/knowledge")
    assert r.status_code == 200
    rows = r.json()["knowledge"]
    assert any(x["id"] == item.id and x["state"] == "pending_review" for x in rows)
    assert "topic_name" in rows[0]

    # 批准 → active
    r = client.post(f"/api/knowledge/{item.id}/verify")
    assert r.status_code == 200
    assert r.json()["knowledge"]["state"] == "active"

    # 再造一条并打回 → draft
    item2 = ks.create(category="general_fact", content="将被打回")
    ks.submit(item2.id)
    r = client.post(f"/api/knowledge/{item2.id}/reject")
    assert r.status_code == 200
    assert r.json()["knowledge"]["state"] == "draft"

    # 手动新建 → 直接 active（用户权威，高影响分类也可）
    r = client.post(
        "/api/knowledge",
        json={"category": "user_profile", "content": "用户最爱的火锅店是五里关"},
    )
    assert r.status_code == 200
    assert r.json()["knowledge"]["state"] == "active"
    assert r.json()["knowledge"]["category"] == "user_profile"

    # 非法：未知分类 / 空内容 / 不存在
    assert client.post("/api/knowledge", json={"category": "nope", "content": "x"}).status_code == 400
    assert client.post("/api/knowledge", json={"category": "goal", "content": "  "}).status_code == 400
    assert client.post("/api/knowledge/ghost/verify").status_code == 404

def test_entity_manage_api(tmp_path):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.config import Settings
    from agent.entities.cards import EntityAttribute, EntityCardCandidate, EntityCardService
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    svc = EntityCardService(conn)
    card = svc.upsert(EntityCardCandidate(
        name="王翠华",
        attributes=[EntityAttribute(key="职业", value="退休教师")],
    ))
    app = create_app(Settings(data_dir=tmp_path), conn)
    client = TestClient(app)

    r = client.get("/api/entities")
    assert r.status_code == 200
    row = r.json()["entities"][0]
    assert row["id"] == card.id and row["name"] == "王翠华"
    assert isinstance(row["attributes"], list)

    r = client.post(f"/api/entities/{card.id}/relations", json={"type": "属于", "target": "王翠华的弟弟"})
    assert r.status_code == 200
    assert any(x["type"] == "属于" and x["target"] == "王翠华的弟弟" for x in r.json()["entity"]["relations"])

    r = client.request("DELETE", f"/api/entities/{card.id}/relations", json={"type": "属于", "target": "王翠华的弟弟"})
    assert r.status_code == 200
    assert all(x["type"] != "属于" for x in r.json()["entity"]["relations"])

    r = client.post(f"/api/entities/{card.id}/revise", json={"summary": "我妈妈，退休教师，住成都"})
    assert r.status_code == 200
    assert r.json()["entity"]["summary"] == "我妈妈，退休教师，住成都"
