"""凭据写入必须原子：元数据与密钥要么都在，要么都不在。

真实缺陷（两处）：

* `create` 先 INSERT 元数据、再写 keyring —— keyring 失败就留下
  「active 元数据但没有密钥」的半成品，用户看到的是一条永远跑不通的凭据；
* `update_secret` 先把 version +1 再写 keyring —— 写失败就是
  「版本号已经是新的，密钥还是旧的」，版本链与真实密钥对不上。

另外 `create` 过去不校验 endpoint，于是 `http://remote-host` 这种
明文远端端点可以绕过「与更新同一套」的安全规则被写进库里。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.credentials.store import CredentialStore, MemoryKeyring


class _FailingKeyring:
    """能读不能写：模拟 keyring 不可用（例如没有可用的 OS 凭据后端）。"""

    def __init__(self, *, readable: dict[tuple[str, str], str] | None = None) -> None:
        self._data = dict(readable or {})
        self.write_attempts = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.write_attempts += 1
        raise RuntimeError("keyring unavailable")

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("keyring unavailable")


@pytest.fixture()
def store(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


def test_create_failure_leaves_no_metadata_behind(db_conn, store):
    store._kr = _FailingKeyring()

    with pytest.raises(RuntimeError, match="keyring unavailable"):
        store.create("k1", "sk-secret", ["main-loop"], endpoint="https://api.example.com/v1")

    assert store._row("k1") is None, "密钥写失败时不能留下 active 元数据"
    assert store.list_credentials() == []


def test_create_can_be_retried_after_the_keyring_recovers(store):
    """第一次写失败之后，用户修好环境再试一次必须能成功（不留残留）。"""
    store._kr = _FailingKeyring()
    with pytest.raises(RuntimeError):
        store.create("k1", "sk-secret", ["main-loop"], endpoint="https://api.example.com/v1")

    store._kr = MemoryKeyring()
    assert store.create("k1", "sk-secret", ["main-loop"], endpoint="https://api.example.com/v1") == 1
    assert store.get_secret("k1") == "sk-secret"
    assert store.get_metadata("k1")["status"] == "active"


def test_create_rejects_remote_http_endpoint(store):
    """create 必须和更新 endpoint 用同一套规则：远端只能 HTTPS。"""
    with pytest.raises(ValueError):
        store.create("k1", "sk-secret", ["main-loop"], endpoint="http://remote-host/v1")

    assert store._row("k1") is None
    # loopback 明文仍然允许（本机 provider）
    assert store.create("k2", "sk-secret", ["main-loop"], endpoint="http://127.0.0.1:8080/v1") == 1
    assert store.create("k3", "sk-secret", ["main-loop"], endpoint="https://api.example.com/v1") == 1


def test_update_secret_failure_keeps_old_version_and_secret(db_conn, store):
    store.create("k1", "sk-old", ["main-loop"], endpoint="https://api.example.com/v1")
    old_secret = store.get_secret("k1")
    assert old_secret == "sk-old"

    # 换成「能读旧值、写不进去」的 keyring
    store._kr = _FailingKeyring(readable={("qio", "k1"): "sk-old"})
    with pytest.raises(RuntimeError, match="keyring unavailable"):
        store.update_secret("k1", "sk-new")

    assert store.get_metadata("k1")["version"] == 1, "写失败不能把 version 涨上去"
    assert store.get_secret("k1") == "sk-old"


def test_update_secret_db_failure_restores_the_old_secret(store, monkeypatch):
    """密钥已写、元数据提交失败 → 必须把旧密钥写回去，不能留下「新密钥 + 旧版本」。"""
    store.create("k1", "sk-old", ["main-loop"], endpoint="https://api.example.com/v1")

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "_audit", boom)
    with pytest.raises(RuntimeError, match="db exploded"):
        store.update_secret("k1", "sk-new")

    assert store.get_metadata("k1")["version"] == 1
    assert store.get_secret("k1") == "sk-old", "失败的更新必须回滚到旧密钥"


def test_create_metadata_failure_removes_the_written_secret(store, monkeypatch):
    """反向顺序也要干净：先写密钥、元数据失败 → 密钥不能留下。"""
    keyring = MemoryKeyring()
    store._kr = keyring

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "_audit", boom)
    with pytest.raises(RuntimeError, match="db exploded"):
        store.create("k1", "sk-secret", ["main-loop"], endpoint="https://api.example.com/v1")

    assert store._row("k1") is None
    assert keyring.get_password("qio", "k1") is None, "回滚必须把刚写进去的密钥删掉"
