"""endpoint 属于凭据的安全身份：变化必须重新配置，不得静默复用旧 Key。"""

from __future__ import annotations

import pytest

from agent.credentials.store import CredentialStore, MemoryKeyring, validate_endpoint


@pytest.fixture()
def store(db_conn) -> CredentialStore:
    s = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    s.create(
        key_id="k1",
        secret="sk-original",
        tags=["main-loop"],
        endpoint="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
    )
    return s


def test_endpoint_change_without_secret_is_refused(store):
    with pytest.raises(ValueError):
        store.update_metadata("k1", endpoint="https://attacker.example/v1")
    assert store.get_metadata("k1")["endpoint"] == "https://api.openai.com/v1"
    assert store.get_secret("k1") == "sk-original"


def test_endpoint_change_with_secret_but_without_confirmation_is_refused(store):
    with pytest.raises(ValueError):
        store.update_metadata("k1", endpoint="https://attacker.example/v1", secret="sk-new")


def test_endpoint_change_with_secret_and_confirmation_succeeds(store):
    meta = store.update_metadata(
        "k1",
        endpoint="https://gateway.example/v1",
        secret="sk-new",
        confirm_reconfigure=True,
    )
    assert meta["endpoint"] == "https://gateway.example/v1"


def test_same_endpoint_edit_needs_no_reconfiguration(store):
    meta = store.update_metadata("k1", endpoint="https://api.openai.com/v1", note="hi")
    assert meta["endpoint"] == "https://api.openai.com/v1"
    assert meta["note"] == "hi"


def test_legacy_row_without_protocol_is_not_a_protocol_change(db_conn):
    """老数据没有 kind：把同样的协议写回去不算「改了发送目标」。

    运行时是按地址判断协议的（`providers.uses_anthropic`），所以 OpenAI 兼容端点的
    老凭据写回 "openai" 只是把隐含值显式化，不该要求用户重新输入 Key + 确认。
    """
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id="legacy-openai",
        secret="sk-old",
        tags=["main-loop"],
        endpoint="https://api.openai.com/v1",
        verify_state="legacy",
    )
    assert store.get_metadata("legacy-openai")["kind"] is None
    meta = store.update_metadata("legacy-openai", kind="openai", note="显式化")
    assert meta["kind"] == "openai"
    assert meta["note"] == "显式化"

    # 但真的改成 Anthropic 协议仍然必须重填 Key 并显式确认
    with pytest.raises(ValueError):
        store.update_metadata("legacy-openai", kind="anthropic")


def test_plain_http_only_for_localhost():
    validate_endpoint("https://api.example.com/v1")
    validate_endpoint("http://127.0.0.1:11434/v1")
    validate_endpoint("http://localhost:8000/v1")
    with pytest.raises(ValueError):
        validate_endpoint("http://api.example.com/v1")
    with pytest.raises(ValueError):
        validate_endpoint("ftp://api.example.com/v1")
