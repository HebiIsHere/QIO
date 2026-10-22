"""R9-F W3：文件来源持久化（FileOriginStore，契约 §3.4）。

全部使用**真实** sqlite（迁移 55 的 file_origins）与真实临时文件；
不 mock 文件身份/数据库 —— 否则"重启后可查/身份优先/外部替换不继承"就没有证据。
"""

from __future__ import annotations

import os
import sys

import pytest

from agent.services import file_origins as fo
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "origins.db"
    connection = connect(db)
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
def store(conn, tmp_path):
    return fo.FileOriginStore(conn, tmp_path / "workspace")


def _atomic_replace(path, data: str) -> None:
    """同目录临时文件 + os.replace（原子替换会改变 OS 文件身份）。"""
    temp = path.with_name(path.name + ".new")
    temp.write_text(data, encoding="utf-8")
    os.replace(temp, path)


def test_created_modified_recycled_origin(store, tmp_path):
    """创建→修改→回收的完整生命周期：来源始终正确、身份每次替换后更新。"""
    path = tmp_path / "报告.md"
    path.write_text("v1", encoding="utf-8")
    created = store.record_created(
        path, origin=fo.ORIGIN_QIO_GENERATED, source="fs_write"
    )
    assert created is not None and created.origin == fo.ORIGIN_QIO_GENERATED
    first_identity = created.identity_key
    assert store.is_generated(path)

    # 原子替换（新身份）→ 来源保留、身份更新
    _atomic_replace(path, "v2")
    updated = store.update_after_replace(created, path)
    assert updated is not None
    assert updated.origin == fo.ORIGIN_QIO_GENERATED
    assert updated.identity_key != first_identity, "原子替换后必须记录新身份"
    assert store.is_generated(path)

    # 回收：保留来源事实，只标记 missing
    path.unlink()
    store.mark_missing(path)
    gone = store.lookup(path)
    assert gone is not None
    assert gone.origin == fo.ORIGIN_QIO_GENERATED
    assert gone.missing_at


def test_user_material_is_never_generated(store, tmp_path):
    """用户材料（上传/截图首次落盘）不得被记成 QIO 生成物。"""
    path = tmp_path / "用户材料.bin"
    path.write_bytes(b"user-bytes")
    record = store.record_existing_file(
        path, origin=fo.ORIGIN_USER_SAVED_MATERIAL, source="attachment.upload"
    )
    assert record is not None and record.origin == fo.ORIGIN_USER_SAVED_MATERIAL
    assert store.is_generated(path) is False

    # 即使后续被 QIO 编辑（原子替换），归属仍是用户材料
    _atomic_replace(path, "edited-by-qio")
    kept = store.update_after_replace(record, path)
    assert kept is not None and kept.origin == fo.ORIGIN_USER_SAVED_MATERIAL

    # 试图把它"创建"成 generated 也不得改写用户来源（保守检查）
    not_stolen = store.record_created(
        path, origin=fo.ORIGIN_QIO_GENERATED, source="bad-claim"
    )
    assert not_stolen is not None
    assert not_stolen.origin == fo.ORIGIN_USER_SAVED_MATERIAL


def test_existing_user_file_is_not_claimed_as_generated(store, tmp_path):
    """已存在且已被观测为用户文件的路径：record_created 不得冒充 generated。"""
    path = tmp_path / "已有文件.txt"
    path.write_text("用户自己的内容", encoding="utf-8")
    observed = store.record_existing_file(
        path, origin=fo.ORIGIN_USER_ORIGINAL, source="fs_write.observed"
    )
    assert observed is not None
    claimed = store.record_created(
        path, origin=fo.ORIGIN_QIO_GENERATED, source="fs_write"
    )
    assert claimed is not None and claimed.origin == fo.ORIGIN_USER_ORIGINAL


def test_external_replacement_does_not_inherit_generated(store, tmp_path):
    """QIO 生成文件被外部替换（新身份）→ 不得继承旧生成来源。"""
    path = tmp_path / "生成物.txt"
    path.write_text("qio", encoding="utf-8")
    generated = store.record_created(
        path, origin=fo.ORIGIN_QIO_GENERATED, source="fs_write"
    )
    assert generated is not None

    # 外部程序删掉并重建同名文件（新身份）。
    # POSIX 上 unlink 后立刻在同一路径重建，文件系统可能复用同一 inode（ext4 实测会），
    # 这会把"平台行为差异"误读成产品缺陷。先用一个打开的文件描述符钉住旧 inode，
    # 保证重建得到不同身份；Windows 不允许删除已打开的文件，所以只在 POSIX 这样做。
    hold = None
    if sys.platform != "win32":
        hold = os.open(str(path), os.O_RDONLY)
    path.unlink()
    path.write_text("someone-else", encoding="utf-8")
    if hold is not None:
        os.close(hold)

    assert store.lookup(path) is None, "外部替换不得继承旧记录"
    assert store.is_generated(path) is False
    # 旧记录仍是历史事实（可按身份查到旧文件），但新文件查不到它；
    # 写路径侧由 fs_write 的执行前身份重核对挡住「覆盖外部替换」。
    assert store._by_identity(generated) is not None


def test_restart_lookup_survives_new_connection(tmp_path):
    """重启（新连接 + 新 store）后同一 DB 仍可查来源。"""
    db = tmp_path / "restart.db"
    conn = connect(db)
    apply_migrations(conn)
    store = fo.FileOriginStore(conn, tmp_path / "workspace")
    path = tmp_path / "持久.txt"
    path.write_text("persisted", encoding="utf-8")
    store.record_created(path, origin=fo.ORIGIN_QIO_GENERATED, source="fs_write")
    conn.close()

    conn2 = connect(db)
    try:
        restarted = fo.FileOriginStore(conn2, tmp_path / "workspace")
        record = restarted.lookup(path)
        assert record is not None
        assert record.origin == fo.ORIGIN_QIO_GENERATED
        assert restarted.is_generated(path)
    finally:
        conn2.close()


def test_moved_file_still_found_by_identity(store, tmp_path):
    """同卷移动/改名：身份不变 → 来源仍可查（路径不是权威）。"""
    old_path = tmp_path / "原名.txt"
    old_path.write_text("move-me", encoding="utf-8")
    record = store.record_created(
        old_path, origin=fo.ORIGIN_QIO_GENERATED, source="fs_write"
    )
    new_path = tmp_path / "改名后.txt"
    os.replace(old_path, new_path)
    found = store.lookup(new_path)
    assert found is not None
    assert found.origin == fo.ORIGIN_QIO_GENERATED
    assert found.identity_key == record.identity_key
    # 更新路径后，旧路径不再命中"当前身份"，但仍保留历史记录
    moved = store.update_path(found, new_path)
    assert moved is not None and moved.path.endswith("改名后.txt")
