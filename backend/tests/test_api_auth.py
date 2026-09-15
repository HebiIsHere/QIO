"""本机 API 身份边界：没有 QIO 会话令牌就调不动 QIO backend。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring

TOKEN = "test-session-token-0123456789abcdef"
BACKEND_URL = "http://127.0.0.1:8734"
TAURI_ORIGIN = "http://tauri.localhost"


def _app(db_conn: sqlite3.Connection, tmp_path, **kwargs):
    settings = Settings(data_dir=tmp_path, **kwargs)
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    return app


@pytest.fixture()
def secure_client(db_conn, tmp_path):
    """生产口径：配置了会话令牌，只信任 Tauri origin。"""
    with TestClient(
        _app(db_conn, tmp_path, session_token=TOKEN, dev_insecure=False),
        base_url=BACKEND_URL,
    ) as client:
        yield client


SENSITIVE_ROUTES = [
    ("GET", "/api/credentials"),
    ("POST", "/api/credentials"),
    ("GET", "/api/settings/memory"),
    ("PUT", "/api/settings/memory"),
    ("GET", "/api/settings/loop"),
    ("GET", "/api/settings/search"),
    ("GET", "/api/settings/computer"),
    ("GET", "/api/settings/trace"),
    ("GET", "/api/traces"),
    ("GET", "/api/graph/topics"),
    ("GET", "/api/graph/positions"),
    ("GET", "/api/planet/overview"),
    ("POST", "/api/planet/browse"),
    ("GET", "/api/fragments/frag_x/messages"),
    ("GET", "/api/knowledge"),
    ("GET", "/api/entities"),
    ("GET", "/api/session/context"),
    ("POST", "/api/turns"),
    ("POST", "/api/turns/cancel"),
    ("POST", "/api/approvals/appr_x/respond"),
    ("POST", "/api/events/ticket"),
    ("GET", "/api/events"),
    ("GET", "/api/instance"),
]


@pytest.mark.parametrize(("method", "path"), SENSITIVE_ROUTES)
def test_sensitive_routes_reject_missing_token(secure_client, method, path):
    resp = secure_client.request(method, path, json={})
    assert resp.status_code == 401, f"{method} {path} 竟然没有要求会话令牌"


def test_wrong_token_is_rejected(secure_client):
    resp = secure_client.get("/api/credentials", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_session_header_and_bearer_both_work(secure_client):
    bearer = secure_client.get(
        "/api/credentials", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    session = secure_client.get("/api/credentials", headers={"X-QIO-Session": TOKEN})
    assert bearer.status_code == 200
    assert session.status_code == 200


def test_health_is_public(secure_client):
    assert secure_client.get("/api/health").status_code == 200


def test_malicious_origin_is_rejected_even_with_token(secure_client):
    """别的网页拿不到令牌；即使拿到了，origin 也过不了这一关。"""
    resp = secure_client.post(
        "/api/turns",
        json={"message": "hi"},
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://attacker.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "origin_rejected"


def test_tauri_origin_is_allowed(secure_client):
    resp = secure_client.get(
        "/api/credentials",
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": TAURI_ORIGIN},
    )
    assert resp.status_code == 200


def test_non_loopback_host_is_rejected(db_conn, tmp_path):
    """DNS rebinding：Host 不是回环地址的请求一律拒绝。"""
    app = _app(db_conn, tmp_path, session_token=TOKEN, dev_insecure=False)
    with TestClient(app, base_url="http://evil.example") as client:
        resp = client.get("/api/credentials", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "host_not_loopback"


def test_cors_does_not_allow_wildcard(secure_client):
    resp = secure_client.get(
        "/api/credentials", headers={"Origin": "https://attacker.example"}
    )
    assert resp.headers.get("access-control-allow-origin") in (None, "")


def test_dev_origin_allowed_only_in_development(db_conn, tmp_path):
    dev_origin = "http://127.0.0.1:5199"
    prod = _app(db_conn, tmp_path, session_token=TOKEN, dev_insecure=False)
    with TestClient(prod, base_url=BACKEND_URL) as client:
        denied = client.get(
            "/api/credentials",
            headers={"Authorization": f"Bearer {TOKEN}", "Origin": dev_origin},
        )
    assert denied.status_code == 403

    dev = _app(db_conn, tmp_path, session_token=TOKEN, dev_insecure=True)
    with TestClient(dev, base_url=BACKEND_URL) as client:
        allowed = client.get(
            "/api/credentials",
            headers={"Authorization": f"Bearer {TOKEN}", "Origin": dev_origin},
        )
    assert allowed.status_code == 200


def test_test_event_route_only_exists_in_development(db_conn, tmp_path):
    prod = _app(db_conn, tmp_path, session_token=TOKEN, dev_insecure=False)
    with TestClient(prod, base_url=BACKEND_URL) as client:
        resp = client.post(
            "/api/events/test?event_type=WARNING",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"code": "x"},
        )
    assert resp.status_code == 404, "生产口径不得暴露测试事件注入口"

    dev = _app(db_conn, tmp_path, session_token=TOKEN, dev_insecure=True)
    with TestClient(dev, base_url=BACKEND_URL) as client:
        resp = client.post(
            "/api/events/test?event_type=WARNING",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"code": "x"},
        )
    assert resp.status_code == 200


def test_post_turn_returns_turn_id_immediately(secure_client):
    resp = secure_client.post(
        "/api/turns",
        json={"message": "你好"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["status"] == "accepted"
    assert body["turn_id"].startswith("turn_")


def test_post_turn_requires_message(secure_client):
    resp = secure_client.post(
        "/api/turns",
        json={"message": "   "},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 400


def test_events_ticket_is_single_use_and_events_only(secure_client):
    ticket_resp = secure_client.post(
        "/api/events/ticket", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert ticket_resp.status_code == 200
    ticket = ticket_resp.json()["ticket"]
    assert ticket

    # ticket 只对 SSE 入口有效，且一次性
    assert secure_client.get("/api/credentials", params={"ticket": ticket}).status_code == 401
    # SSE 本身是长连接（测试里不去读它的无限 body），直接验证认证判定：
    auth = secure_client.app.state.auth
    headers = {"host": "127.0.0.1:8734", "origin": TAURI_ORIGIN}
    assert auth.check_request(
        path="/api/events", method="GET", headers=headers, ticket=ticket
    ) == (True, "ticket")
    assert auth.check_request(
        path="/api/events", method="GET", headers=headers, ticket=ticket
    )[0] is False, "ticket 必须一次性"
    # 没有 ticket 的 SSE 请求同样被拦住
    assert secure_client.get("/api/events").status_code == 401


def test_instance_endpoint_confirms_identity(secure_client):
    resp = secure_client.get("/api/instance", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_required"] is True
    assert body["instance_id"].startswith("qio_")
