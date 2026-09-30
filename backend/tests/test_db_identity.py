"""数据库身份自检：判断「这次打开的，是不是原来那个数据库」。

设计（见 docs/status 里的数据目录一节）：

* 数据库自身有身份证：settings 表里的 `db.instance_id`，首次创建时生成。它跟着
  数据走（拷贝、搬家都不变），是「是不是同一个库」的权威答案；
* 文件身份（卷 + 文件号 + 大小）记在数据库之外（注册表 / 测试用的 JSON 文件），
  用来发现「同一个库被换成了另一份文件」；
* 启动时比对两者，得到 first_run / ok / replaced_same_database / different_database。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from agent.storage.db import connect
from agent.storage.db_identity import (
    DB_INSTANCE_KEY,
    FileBaselineStore,
    accept_current,
    check_integrity,
)
from agent.storage.migrate import apply_migrations


def _fresh_db(path: Path) -> sqlite3.Connection:
    conn = connect(path)
    apply_migrations(conn)
    return conn


def _store(tmp_path: Path) -> FileBaselineStore:
    return FileBaselineStore(tmp_path / "baseline.json")


def test_first_run_records_baseline_and_second_check_is_ok(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    conn = _fresh_db(db_path)
    store = _store(tmp_path)

    first = check_integrity(conn, db_path, store=store)
    assert first.status == "first_run"
    assert first.instance_id
    assert store.read()["last"]["instance_id"] == first.instance_id

    again = check_integrity(conn, db_path, store=store)
    assert again.status == "ok"
    assert again.instance_id == first.instance_id


def test_different_database_is_reported_and_baseline_is_kept(tmp_path: Path) -> None:
    original = tmp_path / "app.db"
    conn = _fresh_db(original)
    store = _store(tmp_path)
    baseline = check_integrity(conn, original, store=store)
    original_id = baseline.instance_id
    conn.close()

    # 模拟「被掉包」：同一个路径，换成一个全新的空库。
    replacement = tmp_path / "replacement.db"
    other = _fresh_db(replacement)
    other.close()
    shutil.copy2(replacement, original)
    swapped = connect(original)

    report = check_integrity(swapped, original, store=store)
    assert report.status == "different_database"
    assert report.expected_instance_id == original_id
    assert report.instance_id != original_id
    assert "不是" in report.message
    # 基线不能被悄悄覆盖，否则下次就发现不了了。
    assert store.read()["last"]["instance_id"] == original_id
    assert report.alert is True
    swapped.close()


def test_copy_of_same_database_is_hint_and_rebaselined(tmp_path: Path) -> None:
    original = tmp_path / "app.db"
    conn = _fresh_db(original)
    store = _store(tmp_path)
    baseline = check_integrity(conn, original, store=store)
    conn.close()

    copied = tmp_path / "moved" / "app.db"
    copied.parent.mkdir()
    shutil.copy2(original, copied)
    conn = connect(copied)

    report = check_integrity(conn, copied, store=store)
    assert report.status == "replaced_same_database"
    assert report.instance_id == baseline.instance_id
    assert report.alert is False  # 同一份数据的拷贝：只提示，不当作事故
    assert store.read()["last"]["db_path"] == str(copied)
    assert check_integrity(conn, copied, store=store).status == "ok"
    conn.close()


def test_accept_current_rebaselines_after_alert(tmp_path: Path) -> None:
    first = tmp_path / "app.db"
    conn = _fresh_db(first)
    store = _store(tmp_path)
    check_integrity(conn, first, store=store)
    first_id = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (DB_INSTANCE_KEY,)
    ).fetchone()["value"]
    conn.close()

    second = tmp_path / "app.db"
    second.unlink()
    conn = _fresh_db(second)
    assert check_integrity(conn, second, store=store).status == "different_database"

    accepted = accept_current(conn, second, store=store)
    assert accepted.status == "ok"
    assert accepted.instance_id != first_id
    assert store.read()["last"]["instance_id"] == accepted.instance_id
    assert check_integrity(conn, second, store=store).status == "ok"
    conn.close()


def test_isolated_dev_instance_does_not_warn_or_clobber(tmp_path: Path) -> None:
    """开发实例（另一个目录、全新空库）不应该报警，也不该动正式库的基线。"""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_db = real_dir / "app.db"
    conn = _fresh_db(real_db)
    store = _store(tmp_path)
    baseline = check_integrity(conn, real_db, store=store)
    conn.close()

    dev_dir = tmp_path / "dev-scratch"
    dev_dir.mkdir()
    dev_db = dev_dir / "app.db"
    dev = _fresh_db(dev_db)

    report = check_integrity(dev, dev_db, store=store)
    assert report.status == "first_run"
    assert report.alert is False
    # 正式库那条记录必须原封不动（否则正式库下次启动就会误报/漏报）
    kept = store.read()["entries"][os.path.normcase(os.path.abspath(real_db))]
    assert kept["instance_id"] == baseline.instance_id
    dev.close()


def test_instance_endpoint_reports_database_integrity(
    tmp_path: Path, monkeypatch, db_conn, settings
) -> None:
    """/api/instance 要带上自检结果：前端启动时就能据此弹告警。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    monkeypatch.setenv("QIO_DB_BASELINE", str(tmp_path / "baseline.json"))
    monkeypatch.delenv("QIO_DISABLE_DB_CHECK", raising=False)

    app = create_app(settings, db_conn)
    with TestClient(app, base_url="http://127.0.0.1:5199") as client:
        first = client.get("/api/instance").json()
        assert first["db"]["status"] in ("first_run", "ok")
        assert first["db"]["alert"] is False
        assert first["db"]["db_path"].endswith("test.db")

        # 把基线改成一个别的身份证：模拟"这次打开的不是原来那个库"
        baseline_path = tmp_path / "baseline.json"
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        for entry in payload["entries"].values():
            entry["instance_id"] = "deadbeef" * 4
        payload["last"]["instance_id"] = "deadbeef" * 4
        baseline_path.write_text(json.dumps(payload), encoding="utf-8")

        second = client.get("/api/instance").json()
        assert second["db"]["status"] == "different_database"
        assert second["db"]["alert"] is True

        ok = client.post("/api/db-integrity/accept")
        assert ok.status_code == 200
        third = client.get("/api/instance").json()
        assert third["db"]["status"] == "ok"
        assert third["db"]["alert"] is False
