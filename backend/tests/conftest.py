from __future__ import annotations

import os

import pytest
from pathlib import Path

# 测试环境显式选择「开发豁免」：绝大多数测试关心的是业务行为，不是认证。
# 认证本身由 tests/test_api_auth.py 专门覆盖（那里显式传入会话令牌，
# 并且断言无令牌 401 / 恶意 origin 403）。
os.environ.setdefault("QIO_DEV_INSECURE", "1")

from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def db_conn(tmp_path: Path):
    conn = connect(tmp_path / "test.db")
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")
