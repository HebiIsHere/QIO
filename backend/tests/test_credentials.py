from __future__ import annotations

import sqlite3

import pytest

from agent.credentials.policy import CredentialPolicy
from agent.credentials.store import CredentialStore, MemoryKeyring


@pytest.fixture()
def store(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


@pytest.fixture()
def policy(store: CredentialStore) -> CredentialPolicy:
    return CredentialPolicy(store)


def test_create_stores_secret_and_metadata(store: CredentialStore):
    store.create(
        "main-key",
        "sk-secret-1",
        tags=["main-loop"],
        endpoint="https://api.example.com/v1",
        default_model="gpt-x",
        budget=1000,
    )
    assert store.get_secret("main-key") == "sk-secret-1"
    meta = store.get_metadata("main-key")
    assert meta["tags"] == ["main-loop"]
    assert meta["status"] == "active"
    assert meta["version"] == 1
    assert meta["endpoint"] == "https://api.example.com/v1"
    log = store.audit_log("main-key")
    assert len(log) == 1 and log[0]["action"] == "create"


def test_secret_never_serialized(store: CredentialStore):
    store.create("k1", "sk-hush", tags=["main-loop"])
    for meta in store.list_credentials():
        assert "secret" not in meta
        assert "sk-hush" not in str(meta)


def test_update_bumps_version(store: CredentialStore):
    store.create("k1", "v1-secret", tags=["main-loop"])
    assert store.update_secret("k1", "v2-secret") == 2
    assert store.get_secret("k1") == "v2-secret"
    log = store.audit_log("k1")
    assert [e["action"] for e in log] == ["create", "update"]
    assert log[-1]["from_version"] == 1 and log[-1]["to_version"] == 2


def test_revoke_removes_secret_and_hides_from_resolution(store: CredentialStore, policy: CredentialPolicy):
    store.create("k1", "sec", tags=["main-loop"])
    store.revoke("k1")
    assert store.get_secret("k1") is None
    refs = policy.resolve("main-loop", ["main-loop"])
    assert refs == []


def test_scope_restricts_requestors(store: CredentialStore, policy: CredentialPolicy):
    store.create("main-key", "s1", tags=["main-loop"])
    store.create(
        "subagent-key",
        "s2",
        tags=["subagent"],
        scope=["subagent-alpha"],
    )
    assert [r.key_id for r in policy.resolve("subagent-alpha", ["subagent"])] == ["subagent-key"]
    assert policy.resolve("main-loop", ["subagent"]) == []


def test_category_default_when_scope_null(store: CredentialStore, policy: CredentialPolicy):
    store.create("vision-key", "s1", tags=["vision"])
    assert [r.key_id for r in policy.resolve("main-loop", ["vision"])] == ["vision-key"]
    assert [r.key_id for r in policy.resolve("subagent-beta", ["vision"])] == ["vision-key"]


def test_budget_ordering_and_exhaustion(store: CredentialStore, policy: CredentialPolicy):
    store.create("key-a", "sa", tags=["vision"], budget=100)
    store.create("key-b", "sb", tags=["vision"], budget=1000)
    refs = policy.resolve("main-loop", ["vision"])
    assert [r.key_id for r in refs] == ["key-b", "key-a"]
    store.record_usage("key-b", tokens=1500)  # exceed 1000-token budget
    refs = policy.resolve("main-loop", ["vision"])
    assert [r.key_id for r in refs] == ["key-a"]


def test_snapshot_freezes_refs(store: CredentialStore, policy: CredentialPolicy):
    store.create("main-key", "s1", tags=["main-loop"], budget=10)
    snap = policy.snapshot("main-loop", ["main-loop"])
    assert snap.requestor == "main-loop"
    assert [r.key_id for r in snap.refs] == ["main-key"]
    store.revoke("main-key")
    # snapshot unchanged after revoke
    assert [r.key_id for r in snap.refs] == ["main-key"]


def test_snapshot_env_injection(store: CredentialStore, policy: CredentialPolicy):
    store.create("main-key", "sk-real", tags=["main-loop"])
    snap = policy.snapshot("main-loop", ["main-loop"])
    env = snap.env({"main-key": store.get_secret("main-key") or ""})
    assert env["SMART_AGENT_KEY_MAIN_KEY"] == "sk-real"


def test_duplicate_create_rejected(store: CredentialStore):
    store.create("k1", "s1", tags=["main-loop"])
    with pytest.raises(ValueError):
        store.create("k1", "s2", tags=["main-loop"])