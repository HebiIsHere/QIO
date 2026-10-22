"""旧附件数据迁移（契约 §9）：盘点 → 审计 → 幂等/断点续跑 → 登记与清理分离。

覆盖：
* 有有效原路径：改为源引用、保留差异事实（legacy_paths），不静默替换历史材料；
* 无原路径但 QIO 保存文件仍在：登记为当前源文件（user_saved_material），不删不复制；
* 全部失效：missing + 重新指定位置入口，不伪造恢复；
* 幂等重跑、审计断点续跑；
* 清理清单与登记分开：旧副本只登记 candidate，第一次迁移不搬/不删任何文件；
* 保留附件 id、消息/卡片/预览引用；
* HTTP 维护接口（GET 状态 / POST 重跑）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.attachments import COPY_MAX_BYTES, AttachmentService
from agent.storage import attachment_migration as migration
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def service(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "migration_api.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _legacy_copy(service: AttachmentService, tmp_path: Path, *, name: str = "旧文件.txt", content: bytes = b"old-content"):
    source = tmp_path / name
    source.write_bytes(content)
    att = service.prepare(str(source), message_id="msg_1")
    att = service.run_prepare(att.id)
    assert att.stored_path and Path(att.stored_path).is_file()
    return att, source, Path(att.stored_path)


def _legacy_reference(service: AttachmentService, tmp_path: Path, *, name: str = "大文件.bin"):
    source = tmp_path / name
    with open(source, "wb") as handle:
        handle.truncate(COPY_MAX_BYTES + 1)  # 稀疏文件：不占真实磁盘
    att = service.prepare(str(source))
    att = service.run_prepare(att.id)
    assert att.kind == "reference" and att.stored_path is None
    return att, source


def test_inventory_counts_files_and_refs(service: AttachmentService, tmp_path: Path):
    att1, source1, stored1 = _legacy_copy(service, tmp_path)
    att2, source2 = _legacy_reference(service, tmp_path, name="另一个大文件.bin")
    # 全部失效的行：副本与路径都不在了
    att3, source3, stored3 = _legacy_copy(service, tmp_path, name="会全丢.txt")
    source3.unlink()
    stored3.unlink()

    inv = migration.inventory(service.conn, tmp_path / "data")
    assert inv["schema_ready"] is True
    assert inv["total"] == 3
    assert inv["legacy_rows"] == 3
    assert inv["source_present"] == 2
    assert inv["stored_present"] == 1
    assert inv["both_missing"] == 1
    assert inv["message_refs"] == 2
    assert inv["managed_copy_files"] == 1
    assert inv["data_dir"] == str(tmp_path / "data")


def test_migrate_source_ref_keeps_difference_facts(service: AttachmentService, tmp_path: Path):
    att, source, stored = _legacy_copy(service, tmp_path, content="历史副本内容".encode("utf-8"))
    # 用户后来编辑了原文件：迁移不能静默把历史材料替换掉，必须保留差异事实
    source.write_bytes("用户后来改的内容".encode("utf-8"))

    report = migration.migrate(service.conn, tmp_path / "data")
    assert report["schema_ready"] is True
    assert report["migrated_source_ref"] == 1, report

    refreshed = service.get(att.id, check=False)
    assert refreshed.id == att.id, "必须保留既有附件 id 与引用关系"
    assert refreshed.source_kind == "desktop_path"
    assert refreshed.origin == "user_original"
    assert refreshed.stored_path is None
    assert refreshed.source_path == str(source)
    assert refreshed.file_id
    assert refreshed.reference_state == "available"
    assert refreshed.content_sha256
    facts = json.loads(refreshed.legacy_paths)
    entry = [item for item in facts if item.get("event") == "migrated_source_ref"][-1]
    assert entry["legacy_stored_path"] == str(stored)
    assert entry["content_differs_from_saved_copy"] is True

    # 清理与登记分开：旧副本只登记候选，第一次迁移不搬、不删
    candidates = migration.cleanup_candidates(service.conn)
    assert len(candidates) == 1
    assert candidates[0]["attachment_id"] == att.id
    assert candidates[0]["path"] == str(stored)
    assert candidates[0]["state"] == "candidate"
    assert stored.is_file(), "首次迁移不得删除/搬动任何现存文件"
    assert source.read_bytes() == "用户后来改的内容".encode("utf-8")


def test_migrate_saved_material_when_source_missing(service: AttachmentService, tmp_path: Path):
    att, source, stored = _legacy_copy(service, tmp_path, content="只剩副本".encode("utf-8"))
    source.unlink()  # 原件不在了，QIO 保存的文件还在

    report = migration.migrate(service.conn, tmp_path / "data")
    assert report["migrated_saved_material"] == 1, report

    refreshed = service.get(att.id, check=False)
    assert refreshed.stored_path is None
    assert refreshed.source_path == str(stored)
    assert refreshed.source_kind == "legacy_copy"
    assert refreshed.origin == "user_saved_material"
    assert refreshed.reference_state == "available"
    assert Path(refreshed.source_path).is_file()
    facts = json.loads(refreshed.legacy_paths)
    assert [item for item in facts if item.get("event") == "migrated_saved_material"]
    # 被登记为源文件后不能再进清理清单
    assert migration.cleanup_candidates(service.conn) == []
    # 读取入口按当前源文件工作（不再依赖旧副本字段）
    resolved = service.resolve_source(att.id)
    assert resolved.path == stored and resolved.state == "available"


def test_migrate_all_missing_keeps_relocate_entry(service: AttachmentService, tmp_path: Path):
    att, source, stored = _legacy_copy(service, tmp_path, name="全丢.txt")
    source.unlink()
    stored.unlink()

    report = migration.migrate(service.conn, tmp_path / "data")
    assert report["migrated_missing"] == 1, report
    refreshed = service.get(att.id, check=False)
    assert refreshed.state == "missing"
    assert refreshed.source_kind == "missing"
    assert refreshed.reference_state == "missing"
    assert refreshed.stored_path is None
    assert refreshed.error and "重新指定位置" in refreshed.error
    payload = service.payload(refreshed)
    assert payload["actions"] == ["relocate"]
    assert payload["missing_reason"]

    # 不伪造恢复；用户指定新位置后可用（确认身份后才采纳）
    restored = tmp_path / "找回来的.txt"
    restored.write_text("恢复内容", encoding="utf-8")
    fixed = service.relocate_reference(att.id, str(restored), confirmed_identity=True)
    assert fixed.state == "ready"
    assert service.resolve_source(att.id).path == restored


def test_migration_is_idempotent_and_resumes_from_audit(service: AttachmentService, tmp_path: Path):
    att, source, stored = _legacy_copy(service, tmp_path)
    first = migration.migrate(service.conn, tmp_path / "data")
    assert first["migrated_source_ref"] == 1

    second = migration.migrate(service.conn, tmp_path / "data")
    assert second["migrated_source_ref"] == 0
    assert second["already_source_ref"] == 1, second
    assert second["scanned"] == 1
    assert len(migration.cleanup_candidates(service.conn)) == 1, "重复运行不得产生重复清理候选"

    # 断点续跑：审计已标记完成的行即使 source_kind 还是 NULL 也不再被重复处理
    att2, source2, stored2 = _legacy_copy(service, tmp_path, name="第二份.txt")
    migration.audit(service.conn, att2.id, migration.STEP_SOURCE_REF, "done", {"outcome": "source_ref"})
    resumed = migration.migrate(service.conn, tmp_path / "data")
    assert resumed["skipped_resumed"] == 1, resumed
    untouched = service.get(att2.id, check=False)
    assert untouched.source_kind is None


def test_migration_keeps_card_and_preview_references(service: AttachmentService, tmp_path: Path):
    att, source, stored = _legacy_copy(service, tmp_path, name="卡片文件.txt")
    stamp = "2026-10-22T00:00:00+00:00"
    conn = service.conn
    conn.execute(
        "INSERT INTO boards (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        ("board_1", "测试板面", stamp, stamp),
    )
    state = {
        "cards": [
            {
                "id": "card_1",
                "kind": "file",
                "meta": {"file": {"attachmentId": att.id, "name": "卡片文件.txt"}},
            }
        ]
    }
    conn.execute(
        "INSERT INTO board_states (board_id, seq, state, reason, updated_at) VALUES (?,?,?,?,?)",
        ("board_1", 1, json.dumps(state), "op", stamp),
    )
    conn.execute(
        "INSERT INTO board_preview_state"
        " (board_id, card_id, attachment_id, version, position, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("board_1", "card_1", att.id, "v1", "{}", stamp),
    )
    conn.commit()

    inv = migration.inventory(conn, tmp_path / "data")
    assert inv["card_refs"] == 1
    assert inv["preview_refs"] == 1

    migration.migrate(conn, tmp_path / "data")

    card_state = json.loads(
        conn.execute("SELECT state FROM board_states WHERE board_id='board_1'").fetchone()["state"]
    )
    assert card_state["cards"][0]["meta"]["file"]["attachmentId"] == att.id
    preview = conn.execute(
        "SELECT attachment_id FROM board_preview_state WHERE card_id='card_1'"
    ).fetchone()
    assert preview["attachment_id"] == att.id
    assert service.get(att.id, check=False) is not None


def test_maintenance_api_runs_and_reports(client: TestClient, tmp_path: Path):
    ctx = client.app.state.ctx
    source = tmp_path / "接口迁移.txt"
    source.write_bytes("接口迁移内容".encode("utf-8"))
    att = ctx.attachments.prepare(str(source))
    att = ctx.attachments.run_prepare(att.id)

    status = client.get("/api/maintenance/attachments/migration")
    assert status.status_code == 200
    assert status.json()["inventory"]["total"] == 1
    assert status.json()["pending"] == 1

    run = client.post("/api/maintenance/attachments/migration", json={"apply": True})
    assert run.status_code == 200, run.text
    report = run.json()["report"]
    assert report["migrated_source_ref"] == 1, report
    assert run.json()["cleanup_candidates"]
    refreshed = ctx.attachments.get(att.id, check=False)
    assert refreshed.source_kind == "desktop_path"
    assert refreshed.stored_path is None

    again = client.post("/api/maintenance/attachments/migration", json={"apply": True})
    assert again.json()["report"]["migrated_source_ref"] == 0
    assert again.json()["report"]["already_source_ref"] == 1

    dry = client.post("/api/maintenance/attachments/migration", json={"apply": False})
    assert dry.json()["report"]["apply"] is False
