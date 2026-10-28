"""`api/search_routes.py`：通道状态 + 真实连接检查的 HTTP 形状。

路由工厂已就绪（`register_search_routes(app, ctx)`），但 `api/server.py` 由其他分支
拥有、本分支不改它：测试里**自己挂一次**同一段代码，正是要交给 lead 插入的那一行。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.search_routes import register_search_routes
from agent.api.server import create_app
from agent.config import Settings
from agent.services import search as search_module
from agent.services.search import REASON_OK, ChannelProbe


@pytest.fixture
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    # ← lead 需要插进 api/server.py 的就是这一行（见报告）
    register_search_routes(app, app.state.ctx)
    with TestClient(app) as c:
        yield c


def test_status_shape_and_no_secret(client):
    r = client.get("/api/settings/search/status")
    assert r.status_code == 200
    body = r.json()

    ids = [row["id"] for row in body["channels"]]
    assert ids == ["bocha", "searxng", "exa", "parallel", "duckduckgo", "bing", "baidu"]
    row = body["channels"][0]
    for key in (
        "id",
        "label",
        "configured",
        "usable",
        "checked_at",
        "checking",
        "reason_code",
        "message",
    ):
        assert key in row, f"通道状态缺少字段 {key}"
    assert row["usable"] is None and row["check_state"] == "unchecked"
    # 回退顺序与参数范围都给出来（界面只展示，不自己算）
    assert body["chain"] == ["exa", "parallel", "duckduckgo", "bing", "baidu"]
    assert body["params"]["top_k_min"] == 1 and body["params"]["top_k_max"] == 20
    assert body["params"]["max_fetch_chars_min"] == 1000
    assert body["params"]["max_fetch_chars_max"] == 40000


def test_status_reflects_configured_without_claiming_verified(client):
    client.put(
        "/api/settings/search",
        json={"bocha_api_key": "bk-secret-value", "searxng_url": "http://127.0.0.1:8080"},
    )
    body = client.get("/api/settings/search/status").json()
    bocha = next(row for row in body["channels"] if row["id"] == "bocha")
    assert bocha["configured"] is True
    # 关键：只填了 Key ≠ 已验证可用
    assert bocha["usable"] is None
    assert bocha["checked_at"] is None
    assert "bk-secret-value" not in str(body)


def test_check_requires_known_channel_ids(client):
    r = client.post("/api/settings/search/check", json={"channels": ["nope"]})
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


def test_check_rejects_wrong_shape(client):
    r = client.post("/api/settings/search/check", json={"channels": "bocha"})
    assert r.status_code == 400


def test_check_reports_real_result_per_channel(client, monkeypatch):
    client.put(
        "/api/settings/search",
        json={
            "bocha_api_key": "bk-secret-value",
            "searxng_url": "http://127.0.0.1:8080",
        },
    )

    async def ok_bocha(api_key, *, timeout=None):  # noqa: ANN001
        return ChannelProbe(True, REASON_OK, "博查可用（检查请求返回正常）")

    async def broken_searxng(instance_url, *, timeout=None):  # noqa: ANN001
        return ChannelProbe(False, "unreachable", "连不上 SearXNG：请检查网络、代理或地址")

    async def ok_provider(provider, label):  # noqa: ANN001
        return ChannelProbe(True, REASON_OK, f"{label}可用（检查请求有返回）")

    monkeypatch.setattr(search_module, "probe_bocha", ok_bocha)
    monkeypatch.setattr(search_module, "probe_searxng", broken_searxng)
    monkeypatch.setattr(search_module, "probe_provider", ok_provider)

    r = client.post(
        "/api/settings/search/check", json={"channels": ["bocha", "searxng", "bing"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["checked_channels"] == ["bocha", "searxng", "bing"]

    rows = {row["id"]: row for row in body["channels"]}
    assert rows["bocha"]["usable"] is True
    assert rows["bocha"]["checked_at"]
    assert rows["searxng"]["usable"] is False
    assert rows["searxng"]["reason_code"] == "unreachable"
    assert rows["bing"]["usable"] is True
    # 没被检查的通道保持「未检查」（不能连带显示成已验证）
    assert rows["baidu"]["usable"] is None
    assert "bk-secret-value" not in str(body)


def test_check_without_channels_checks_all(client, monkeypatch):
    async def ok_provider(provider, label):  # noqa: ANN001
        return ChannelProbe(True, REASON_OK, "ok")

    async def ok_bocha(api_key, *, timeout=None):  # noqa: ANN001
        return ChannelProbe(True, REASON_OK, "ok")

    async def ok_searxng(instance_url, *, timeout=None):  # noqa: ANN001
        return ChannelProbe(True, REASON_OK, "ok")

    monkeypatch.setattr(search_module, "probe_bocha", ok_bocha)
    monkeypatch.setattr(search_module, "probe_searxng", ok_searxng)
    monkeypatch.setattr(search_module, "probe_provider", ok_provider)

    body = client.post("/api/settings/search/check", json={}).json()
    assert len(body["checked_channels"]) == 7
    # 未配置的通道如实报「未配置」，不会因为「检查过了」就变成可用
    bocha = next(row for row in body["channels"] if row["id"] == "bocha")
    assert bocha["configured"] is False
    assert bocha["usable"] is False
    assert bocha["reason_code"] == "not_configured"


def test_check_is_not_a_scheduled_success(client, monkeypatch):
    """检查失败就是失败：没有「定时任务替它成功」这种事。"""
    client.put("/api/settings/search", json={"bocha_api_key": "bk-secret-value"})

    async def failing(api_key, *, timeout=None):  # noqa: ANN001
        return ChannelProbe(False, "invalid_key", "博查凭据无效或没有权限（HTTP 401）")

    monkeypatch.setattr(search_module, "probe_bocha", failing)
    body = client.post(
        "/api/settings/search/check", json={"channels": ["bocha"]}
    ).json()
    bocha = next(row for row in body["channels"] if row["id"] == "bocha")
    assert bocha["usable"] is False
    assert bocha["check_state"] == "failed"
    assert bocha["reason_code"] == "invalid_key"

    # 再查一次状态：仍是失败结论（不会自己翻成成功）
    again = client.get("/api/settings/search/status").json()
    bocha = next(row for row in again["channels"] if row["id"] == "bocha")
    assert bocha["usable"] is False
