from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.storage.settings import SettingsStore


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _set_state(conn: sqlite3.Connection, *, welcome_version: str, seen: bool = True) -> None:
    store = SettingsStore(conn)
    store.set("onboarding.welcome_version", welcome_version)
    store.set("onboarding.wizard_seen", "1" if seen else "0")


def test_crossing_into_migration_version_shows_once(client, db_conn):
    client.app.version = "0.1.7"
    _set_state(db_conn, welcome_version="0.1.6")

    assert client.get("/api/onboarding/status").json()["show_wizard"] is True
    client.post("/api/onboarding/seen")
    assert client.get("/api/onboarding/status").json()["show_wizard"] is False


def test_later_versions_do_not_pop_again(client, db_conn):
    _set_state(db_conn, welcome_version="0.1.7")
    client.app.version = "0.1.8"
    assert client.get("/api/onboarding/status").json()["show_wizard"] is False

    client.app.version = "0.2.0"
    assert client.get("/api/onboarding/status").json()["show_wizard"] is False


def test_user_far_behind_still_gets_one_popup(client, db_conn):
    client.app.version = "0.1.9"
    _set_state(db_conn, welcome_version="0.1.3")
    assert client.get("/api/onboarding/status").json()["show_wizard"] is True


def test_fresh_install_still_shows(client):
    client.app.version = "0.1.9"
    assert client.get("/api/onboarding/status").json()["show_wizard"] is True
