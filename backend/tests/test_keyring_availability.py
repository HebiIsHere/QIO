"""没有可用 OS 凭据后端时也要能启动（CI 的 headless Ubuntu 就是这种情况）。

真实缺陷：`CredentialStore.__init__` 会立刻调用 `_default_keyring()`，而它在
`keyring.get_keyring()` 返回 `FailKeyring` 时直接抛 RuntimeError。于是「构造一个
CredentialStore」= 必须有可用的系统凭据后端。

headless Linux（GitHub Actions 的 ubuntu runner、容器、没有 SecretService 的机器）
恰恰没有 —— 结果是应用工厂和几百个测试在**构造期**就炸掉，CI 的 backend 任务
从第一次运行起就一直全红，而 Windows 上永远看不到这个问题。

契约：
* 构造 store 不得依赖 OS 后端；
* 读路径（拿密钥）如实回答「没有可用密钥」→ None；
* 写路径（存 / 轮换 / 删除密钥）仍然**大声失败**，绝不静默假成功；
* 显式注入后端（测试用的 MemoryKeyring）之后，绝不再去探测 OS 后端。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from keyring.backends.fail import Keyring as FailKeyring

import agent.credentials.store as store_module
from agent.credentials.store import CredentialStore, MemoryKeyring


@pytest.fixture()
def no_os_backend(monkeypatch):
    """模拟 headless Linux：系统里没有任何可用的凭据后端。"""
    calls: list[int] = []

    def fake_get_keyring() -> FailKeyring:
        calls.append(1)
        return FailKeyring()

    monkeypatch.setattr(store_module.keyring, "get_keyring", fake_get_keyring)
    return calls


def _active_metadata_row(conn: sqlite3.Connection, key_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO credentials (id, version, tags, budget_used, status, "
        "created_at, updated_at, enabled) VALUES (?, 1, ?, 0, 'active', ?, ?, 1)",
        (key_id, '["main-loop"]', now, now),
    )
    conn.commit()


def test_constructing_a_store_does_not_require_an_os_keyring(db_conn, no_os_backend):
    """构造期不得探测系统凭据后端 —— 否则 headless 环境连应用都起不来。"""
    store = CredentialStore(db_conn)
    assert store is not None


def test_reading_without_a_backend_reports_no_secret(db_conn, no_os_backend):
    """读路径：没有可用后端 = 这台机器上没有可用密钥，如实返回 None。"""
    store = CredentialStore(db_conn)
    _active_metadata_row(db_conn, "k1")
    assert store.get_secret("k1") is None


def test_writing_without_a_backend_still_fails_loudly(db_conn, no_os_backend):
    """写路径必须大声失败：绝不静默降级到 no-op 后端。"""
    store = CredentialStore(db_conn)
    with pytest.raises(RuntimeError, match="no usable OS credential backend"):
        store.create("k1", "sk-test-secret", ["main-loop"])


def test_injected_backend_never_probes_the_os_keyring(db_conn, no_os_backend):
    """显式注入后端之后，绝不再去解析系统后端（现有测试的用法必须继续有效）。"""
    store = CredentialStore(db_conn)
    store._kr = MemoryKeyring()

    store.create("k1", "sk-test-secret", ["main-loop"])
    assert store.get_secret("k1") == "sk-test-secret"
    assert no_os_backend == [], "注入后端之后不应该再去探测操作系统凭据后端"
