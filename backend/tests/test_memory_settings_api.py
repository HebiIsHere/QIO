"""单段长度（fragment.max_tokens）的设置接口（阶段 4 / 阶段 5 文案的数据来源）。

界面要把「根据讨论进展分段，长度用于控制单段规模」说清楚，前提是接口先把这个值
暴露出来；同时不能因为新字段把旧字段的语义改掉（轮数仍然可单独设置）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    conn = connect(tmp_path / "mem.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx: AppContext = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_memory_settings_expose_both_turns_and_tokens(client: TestClient):
    body = client.get("/api/settings/memory").json()

    assert body["fragment_max_turns"] == 10  # 默认轮数不变
    assert body["fragment_max_tokens"] == 4096  # 新增：单段长度目标


def test_update_tokens_alone_keeps_turns(client: TestClient):
    ctx: AppContext = client.app.state.ctx
    client.put("/api/settings/memory", json={"fragment_max_turns": 12})

    resp = client.put("/api/settings/memory", json={"fragment_max_tokens": 12000})

    assert resp.status_code == 200
    assert resp.json()["fragment_max_tokens"] == 12000
    assert resp.json()["fragment_max_turns"] == 12, "只改长度不该动轮数"
    assert ctx.fragments.max_tokens == 4096, "运行时值在下一轮才刷新（不偷偷改）"


def test_tokens_out_of_range_is_rejected(client: TestClient):
    for bad in (999, 999_999):
        resp = client.put("/api/settings/memory", json={"fragment_max_tokens": bad})
        assert resp.status_code == 400, bad


def test_turns_random_value_still_validated(client: TestClient):
    resp = client.put("/api/settings/memory", json={"fragment_max_turns": 99})
    assert resp.status_code == 400
