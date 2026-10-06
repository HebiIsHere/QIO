"""主 Agent Loop 的凭据选择：main-loop 优先，显式默认项优先，专项凭据不得被静默顶上去。"""

from __future__ import annotations

import pytest

from agent.credentials.policy import CredentialPolicy
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.services.app import MAIN_LOOP_USAGE_TAGS


@pytest.fixture()
def policy(db_conn) -> CredentialPolicy:
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    # 专项凭据预算更大（旧实现按预算排序会把它排到 main-loop 前面）
    store.create(
        key_id="vision-key",
        secret="sk-vision",
        tags=["vision"],
        endpoint="https://api.openai.com/v1",
        budget=1_000_000,
        verify_state="verified",
    )
    store.create(
        key_id="main-key",
        secret="sk-main",
        tags=["main-loop"],
        endpoint="https://api.openai.com/v1",
        budget=1000,
        verify_state="verified",
    )
    return CredentialPolicy(store)


def test_main_loop_credential_wins_over_purpose_credential(policy):
    refs = policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)
    assert refs, "至少要解析出可用凭据"
    assert refs[0].key_id == "main-key", "主循环必须先选 main-loop 标签的凭据"


def test_purpose_credential_is_still_available_as_fallback(db_conn):
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id="vision-key",
        secret="sk-vision",
        tags=["vision"],
        endpoint="https://api.openai.com/v1",
        verify_state="verified",
    )
    policy = CredentialPolicy(store)
    refs = policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)
    assert [r.key_id for r in refs] == ["vision-key"]


def test_purpose_only_tags_are_not_picked_for_unrelated_usage(policy):
    refs = policy.resolve("embedding", ["embedding"])
    assert refs == [], "没有 embedding 用途的凭据时不能拿 vision/main-loop 顶上"


def test_unverified_credential_is_not_picked(db_conn):
    """新建但没通过验证的凭据不进自动选择：它还在，只是还不能被拿去用。"""
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id="fresh-key",
        secret="sk-fresh",
        tags=["main-loop"],
        endpoint="https://api.openai.com/v1",
        default_model="gpt-x",
    )
    policy = CredentialPolicy(store)
    assert policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS) == []
    assert store.get_default_meta() is None

    store.set_verified("fresh-key", True)
    assert [r.key_id for r in policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)] == ["fresh-key"]


def test_explicit_default_wins_over_budget_order(db_conn):
    """显式默认项优先于「预算余额降序」这种普通候选排序。"""
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id="small", secret="s1", tags=["main-loop"],
        endpoint="https://api.openai.com/v1", budget=100, verify_state="verified",
    )
    store.create(
        key_id="big", secret="s2", tags=["main-loop"],
        endpoint="https://api.openai.com/v1", budget=100_000, verify_state="verified",
    )
    policy = CredentialPolicy(store)
    assert policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)[0].key_id == "big"
    store.set_default("small")
    refs = policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)
    assert refs[0].key_id == "small"
    assert refs[0].is_default is True


def test_default_does_not_bypass_limits(db_conn):
    """默认标记只是排序，不是授权：停用 / 撤销 / 预算用尽 / 用途不符都不放行。"""
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id="star", secret="s1", tags=["main-loop"],
        endpoint="https://api.openai.com/v1", budget=100, verify_state="verified",
    )
    store.set_default("star")
    policy = CredentialPolicy(store)

    store.set_enabled("star", False)
    assert policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS) == []
    store.set_enabled("star", True)

    store.record_usage("star", input_tokens=400, output_tokens=100)
    assert policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS) == []
    store.update_metadata("star", budget=10_000)

    store.revoke("star")
    assert policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS) == []


def test_auto_default_only_for_the_first_usable_main_loop_credential(db_conn):
    """第一条验证可用的主对话凭据成为默认；之后新增的不替换它。"""
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id="first", secret="s1", tags=["main-loop"],
        endpoint="https://api.openai.com/v1", verify_state="verified",
    )
    assert store.current_default() is None
    assert store.promote_default() == "first"
    assert store.current_default()["id"] == "first"

    store.create(
        key_id="second", secret="s2", tags=["main-loop"],
        endpoint="https://api.openai.com/v1", verify_state="verified",
    )
    assert store.promote_default() == "first", "已有可用默认项时不得悄悄替换"
    assert store.current_default()["id"] == "first"
    # 用户显式改选才算数
    store.set_default("second")
    assert store.current_default()["id"] == "second"
