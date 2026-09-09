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
    assert env["QIO_KEY_MAIN_KEY"] == "sk-real"


def test_duplicate_create_rejected(store: CredentialStore):
    store.create("k1", "s1", tags=["main-loop"])
    with pytest.raises(ValueError):
        store.create("k1", "s2", tags=["main-loop"])
def test_create_rejects_empty_key_id(store: CredentialStore):
    with pytest.raises(ValueError):
        store.create("", "sk-secret", tags=["main-loop"])
    with pytest.raises(ValueError):
        store.create("   ", "sk-secret", tags=["main-loop"])


def test_update_metadata_keeps_secret_and_version(store: CredentialStore):
    store.create("k1", "sk-hush", tags=["main-loop"], note="old", budget=100)
    meta = store.update_metadata(
        "k1", tags=["main-loop", "vision"], note="new", budget=500
    )
    assert meta["tags"] == ["main-loop", "vision"]
    assert meta["note"] == "new"
    assert meta["budget"] == 500
    assert meta["version"] == 1  # metadata change must not bump the secret version
    assert store.get_secret("k1") == "sk-hush"
    log = store.audit_log("k1")
    assert [e["action"] for e in log] == ["create", "update"]


def test_update_metadata_resets_budget_used_when_budget_changes(store: CredentialStore):
    store.create("k1", "s", tags=["main-loop"], budget=100)
    store.record_usage("k1", tokens=60)
    assert store.budget_left("k1") == 40
    store.update_metadata("k1", budget=1000)
    assert store.budget_left("k1") == 1000


def test_disable_hides_from_policy_and_get_secret(store: CredentialStore, policy: CredentialPolicy):
    store.create("k1", "sec", tags=["main-loop"])
    store.set_enabled("k1", False)
    assert store.get_metadata("k1")["enabled"] is False
    assert store.get_secret("k1") is None
    assert policy.resolve("main-loop", ["main-loop"]) == []


def test_enable_restores_access(store: CredentialStore, policy: CredentialPolicy):
    store.create("k1", "sec", tags=["main-loop"])
    store.set_enabled("k1", False)
    store.set_enabled("k1", True)
    assert store.get_secret("k1") == "sec"
    assert [r.key_id for r in policy.resolve("main-loop", ["main-loop"])] == ["k1"]


def test_delete_removes_record_secret_and_audit(store: CredentialStore, policy: CredentialPolicy):
    store.create("k1", "sec", tags=["main-loop"], note="to remove")
    store.delete("k1")
    assert store.get_metadata("k1") is None
    assert store.get_secret("k1") is None
    assert store.audit_log("k1") == []
    assert policy.resolve("main-loop", ["main-loop"]) == []
    with pytest.raises(KeyError):
        store.delete("k1")


def test_resolve_prefers_purpose_key_then_main_loop_fallback(
    store: CredentialStore, policy: CredentialPolicy
):
    store.create("main-key", "sm", tags=["main-loop"], budget=1000)
    store.create("vision-key", "sv", tags=["vision"], budget=10)
    refs = policy.resolve("main-loop", ["main-loop", "vision"])
    # purpose key wins even with less budget; main-loop is the last resort
    assert [r.key_id for r in refs] == ["vision-key", "main-key"]


def test_resolve_uses_main_loop_when_no_purpose_key(store: CredentialStore, policy: CredentialPolicy):
    store.create("main-key", "sm", tags=["main-loop"], budget=100)
    refs = policy.resolve("main-loop", ["main-loop", "vision"])
    assert [r.key_id for r in refs] == ["main-key"]


def test_get_default_secret_returns_main_loop_credential(store: CredentialStore):
    store.create("main-key", "sm", tags=["main-loop"], budget=100)
    store.create("vision-key", "sv", tags=["vision"])
    assert store.get_default_secret() == "sm"
    store.set_enabled("main-key", False)
    assert store.get_default_secret() is None


def test_get_default_secret_ignores_exhausted_budget(store: CredentialStore):
    store.create("main-key", "sm", tags=["main-loop"], budget=100)
    store.record_usage("main-key", tokens=150)
    assert store.get_default_secret() is None


def test_list_tagged_filters_by_tag_and_budget(store: CredentialStore):
    store.create("sub-key", "ss", tags=["subagent"], budget=100)
    store.create("main-key", "sm", tags=["main-loop"], budget=50)
    store.record_usage("sub-key", tokens=150)
    assert [c["id"] for c in store.list_tagged("subagent")] == []
    store.create("sub-key2", "ss2", tags=["subagent"], budget=100)
    assert [c["id"] for c in store.list_tagged("subagent")] == ["sub-key2"]


def test_get_default_meta_returns_main_loop(store: CredentialStore):
    store.create("main-key", "sm", tags=["main-loop"], budget=100)
    store.create("sub-key", "ss", tags=["subagent"])
    meta = store.get_default_meta()
    assert meta is not None and meta["id"] == "main-key"
