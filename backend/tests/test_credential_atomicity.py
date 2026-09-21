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


class _FlakyKeyring:
    """前 N 次写入成功，之后开始失败（用来模拟「回滚本身也失败」）。"""

    def __init__(self, *, fail_after: int, initial: dict[tuple[str, str], str] | None = None) -> None:
        self._data = dict(initial or {})
        self._fail_after = fail_after
        self.writes = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.writes += 1
        if self.writes > self._fail_after:
            raise RuntimeError("keyring went down mid-rollback")
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)


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


# ---------------------------------------------------------------------------
# secret + endpoint 的**组合**更新：一次 API 调用 = 一次完整操作
# ---------------------------------------------------------------------------


def test_reconfigure_updates_secret_and_endpoint_together(store):
    store.create(
        "k1", "sk-old", ["main-loop"], endpoint="https://old.example.com/v1", default_model="m1"
    )

    meta = store.reconfigure(
        "k1",
        secret="sk-new",
        endpoint="https://new.example.com/v1",
        confirm_reconfigure=True,
    )

    assert meta["endpoint"] == "https://new.example.com/v1"
    assert meta["version"] == 2, "换了 endpoint / secret 就要推进版本"
    assert store.get_secret("k1") == "sk-new"


def test_reconfigure_is_all_or_nothing_when_metadata_write_fails(store, monkeypatch):
    """secret 已经写进去了，元数据提交失败 → 两边都必须回到旧状态。"""
    store.create("k1", "sk-old", ["main-loop"], endpoint="https://old.example.com/v1")

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "_audit", boom)
    with pytest.raises(RuntimeError, match="db exploded"):
        store.reconfigure(
            "k1",
            secret="sk-new",
            endpoint="https://new.example.com/v1",
            confirm_reconfigure=True,
        )
    monkeypatch.undo()

    meta = store.get_metadata("k1")
    assert meta["endpoint"] == "https://old.example.com/v1", "endpoint 必须回滚"
    assert meta["version"] == 1, "version 必须回滚"
    assert store.get_secret("k1") == "sk-old", "secret 必须回滚"


def test_reconfigure_logs_loudly_when_the_rollback_itself_fails(store, monkeypatch, caplog):
    """回滚失败是严重事件：必须记录、必须抛出、绝不能声称操作成功。"""
    store.create("k1", "sk-old", ["main-loop"], endpoint="https://old.example.com/v1")
    # 第一次写入（新 secret）成功，第二次（回滚旧 secret）失败
    store._kr = _FlakyKeyring(
        fail_after=1, initial={("qio", "k1"): "sk-old"}
    )

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "_audit", boom)
    with caplog.at_level("CRITICAL"):
        with pytest.raises(Exception) as excinfo:
            store.reconfigure(
                "k1",
                secret="sk-new",
                endpoint="https://new.example.com/v1",
                confirm_reconfigure=True,
            )

    message = str(excinfo.value)
    assert "rollback" in message.lower() or "回滚" in message, message
    assert "db exploded" in message, "原始失败原因也不能丢"
    assert any(record.levelname == "CRITICAL" for record in caplog.records), (
        "回滚失败必须留下 CRITICAL 级别的诊断信息"
    )


def test_reconfigure_endpoint_change_requires_secret_and_confirmation(store):
    store.create("k1", "sk-old", ["main-loop"], endpoint="https://old.example.com/v1")

    with pytest.raises(ValueError):
        store.reconfigure("k1", endpoint="https://new.example.com/v1")
    with pytest.raises(ValueError):
        store.reconfigure("k1", secret="sk-new", endpoint="https://new.example.com/v1")

    meta = store.get_metadata("k1")
    assert meta["endpoint"] == "https://old.example.com/v1"
    assert meta["version"] == 1
    assert store.get_secret("k1") == "sk-old"


def test_reconfigure_plain_metadata_edit_needs_no_secret(store):
    store.create("k1", "sk-old", ["main-loop"], endpoint="https://old.example.com/v1")

    meta = store.reconfigure("k1", note="只是一句备注")

    assert meta["note"] == "只是一句备注"
    assert meta["version"] == 1, "只改备注不该推进版本"
    assert store.get_secret("k1") == "sk-old"
