"""看图用途：显式指定不可用时**不许**静默回落主对话模型（设置页改版 §8）。

背景：`api/interactive_store.py::_adapter_provider` 的 images 分支以前把
`build_vision_adapter() → None` 一律当成「没有专用看图凭据」，然后回落主对话 adapter。
但 None 有两种含义：

* 没有显式指定 / 没有 vision 专用凭据 → 回落主对话是**基线行为**（图片能否交付由适配器
  的 supports_images 决定）；
* 用户**显式指定**了看图模型、但那把 Key 现在不可用 → 回落主对话就是把图片发给用户没选过
  的模型 = 静默换 Key。这一条必须抛错，让理解模块如实失败（修好后可重试）。

这些用例全部 fake provider（probe_adapter 打桩），不联网。
"""

from __future__ import annotations

import time

import pytest

from agent.adapters.base import AdapterMode
from agent.adapters.probe import ProbeResult
from agent.api.bus import EventBus
from agent.api.interactive_store import _adapter_provider
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive.understanding import _adapter_for_stage
from agent.services.app import AppContext
from agent.services.usage_target import REASON_EXPLICIT_UNAVAILABLE, UsageUnavailable

_MODEL_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS credential_models (
    id                 TEXT PRIMARY KEY,
    credential_id      TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    display_name       TEXT,
    source             TEXT NOT NULL DEFAULT 'discovered',
    capabilities       TEXT NOT NULL DEFAULT '[]',
    verify_state       TEXT NOT NULL DEFAULT 'unverified',
    verified_at        TEXT,
    verify_error       TEXT,
    verify_fingerprint TEXT,
    context_window     INTEGER,
    discovered_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (credential_id, model_id)
)
"""

_MODEL_USAGES_DDL = """
CREATE TABLE IF NOT EXISTS model_usages (
    id            TEXT PRIMARY KEY,
    usage_key     TEXT NOT NULL UNIQUE,
    credential_id TEXT,
    model_id      TEXT,
    mode          TEXT NOT NULL DEFAULT 'inherit',
    updated_at    TEXT NOT NULL
)
"""


class _App:
    def __init__(self, ctx) -> None:
        self.state = type("State", (), {"ctx": ctx})()


class _Request:
    def __init__(self, ctx) -> None:
        self.app = _App(ctx)


@pytest.fixture()
def ctx(tmp_path):
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    yield app_ctx
    conn.close()


@pytest.fixture()
def probe_counter(monkeypatch):
    calls: list[tuple] = []

    async def fake_probe(client, model, endpoint=None, cache=None):
        calls.append((model, endpoint))
        return ProbeResult(AdapterMode.NATIVE, "ok", time.time())

    monkeypatch.setattr("agent.services.app.probe_adapter", fake_probe)
    return calls


def _grant(ctx, key_id: str, *, tags=None, model: str = "gpt-x") -> None:
    ctx.credentials.create(
        key_id,
        "sk-test-secret",
        list(tags or ["main-loop"]),
        "https://api.example.com/v1",
        model,
        verify_state="verified",
    )


def _assign_vision(ctx, credential_id: str, model_id: str) -> None:
    ctx.conn.execute(_MODEL_CATALOG_DDL)
    ctx.conn.execute(_MODEL_USAGES_DDL)
    now = "2026-10-28T00:00:00+00:00"
    ctx.conn.execute(
        "INSERT INTO credential_models (id, credential_id, model_id, source, capabilities,"
        " verify_state, discovered_at, updated_at)"
        " VALUES (?, ?, ?, 'manual', '[]', 'verified', ?, ?)"
        " ON CONFLICT(credential_id, model_id) DO NOTHING",
        (f"cm_{credential_id}_{model_id}", credential_id, model_id, now, now),
    )
    ctx.conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES ('mu_vision', 'vision', ?, ?, 'explicit', ?)"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode='explicit'",
        (credential_id, model_id, now),
    )
    ctx.conn.commit()


async def test_explicit_vision_unavailable_raises_instead_of_falling_back(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "pinned-vision", model="gpt-pinned-vision")
    ctx.credentials.set_enabled("pinned-vision", False)
    _assign_vision(ctx, "pinned-vision", "gpt-pinned-vision")

    provider = _adapter_provider(_Request(ctx))
    with pytest.raises(UsageUnavailable) as excinfo:
        await provider(images=True)

    error = excinfo.value
    assert error.usage_key == "vision"
    assert error.reason_code == REASON_EXPLICIT_UNAVAILABLE
    assert "pinned-vision" in error.message
    # 主对话那把 Key 不能因为这里失败而被顶上来（调用方拿到的是错误，不是 adapter）
    assert "main-key" not in error.message


async def test_understanding_gets_readable_reason_not_main_adapter(ctx, probe_counter):
    """理解模块的取用路径：拿到 None + 可读原因（可重试），而不是主对话 adapter。"""
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "pinned-vision", model="gpt-pinned-vision")
    ctx.credentials.set_enabled("pinned-vision", False)
    _assign_vision(ctx, "pinned-vision", "gpt-pinned-vision")

    errors: list[str] = []
    adapter = await _adapter_for_stage(
        _adapter_provider(_Request(ctx)), images_hint=True, errors=errors
    )

    assert adapter is None, "显式看图模型不可用时不能把主对话 adapter 交出去"
    assert errors, "必须留下可读失败原因"
    assert "pinned-vision" in errors[0]
    assert "sk-test-secret" not in errors[0]  # 原因里不能出现密钥


async def test_no_explicit_vision_keeps_baseline_fallback(ctx, probe_counter):
    """压根没有显式指定 → 保持基线：回落到主对话 adapter（不是错误）。"""
    _grant(ctx, "main-key", model="gpt-main")
    provider = _adapter_provider(_Request(ctx))
    adapter = await provider(images=True)
    assert adapter is not None
    assert adapter.key_id == "main-key"


async def test_vision_tagged_credential_still_used(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "vision-key", tags=["vision"], model="gpt-vision")
    provider = _adapter_provider(_Request(ctx))
    adapter = await provider(images=True)
    assert adapter is not None
    assert (adapter.key_id, adapter.model) == ("vision-key", "gpt-vision")


async def test_clearing_assignment_restores_baseline_fallback(ctx, probe_counter):
    """用户把显式分配清掉之后：不能再拿旧原因报错，回到基线回落。"""
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "pinned-vision", model="gpt-pinned-vision")
    ctx.credentials.set_enabled("pinned-vision", False)
    _assign_vision(ctx, "pinned-vision", "gpt-pinned-vision")

    provider = _adapter_provider(_Request(ctx))
    with pytest.raises(UsageUnavailable):
        await provider(images=True)

    ctx.conn.execute("DELETE FROM model_usages WHERE usage_key = 'vision'")
    ctx.conn.commit()

    adapter = await provider(images=True)
    assert adapter is not None
    assert adapter.key_id == "main-key"
