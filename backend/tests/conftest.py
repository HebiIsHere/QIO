from __future__ import annotations

import pytest
from pathlib import Path

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