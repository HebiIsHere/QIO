"""主 Agent Loop 的凭据选择：main-loop 优先，专项凭据不得被静默顶上去。"""

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
    )
    store.create(
        key_id="main-key",
        secret="sk-main",
        tags=["main-loop"],
        endpoint="https://api.openai.com/v1",
        budget=1000,
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
    )
    policy = CredentialPolicy(store)
    refs = policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)
    assert [r.key_id for r in refs] == ["vision-key"]


def test_purpose_only_tags_are_not_picked_for_unrelated_usage(policy):
    refs = policy.resolve("embedding", ["embedding"])
    assert refs == [], "没有 embedding 用途的凭据时不能拿 vision/main-loop 顶上"
