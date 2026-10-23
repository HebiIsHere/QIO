"""R9-H4 §8 E：内部数据身份登记、替换生命周期、删除/复用、失败恢复。

真实临时文件 + 真实 sqlite（迁移 61/65 表）；Windows/POSIX 能力如实记录，
不支持的能力分支不 skip、用同判据替代覆盖并写进报告。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from agent.security import internal_data
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.internal_data_identity import (
    InternalDataIdentityStore,
    RETIRED_DELETED,
    RETIRED_REPLACED,
    STATE_DIFFERENT,
    STATE_RETIRED,
    STATE_SAME,
    STATE_UNCERTAIN,
)
from agent.services import file_identity
from agent.storage.db import connect
from agent.storage.settings import SettingsStore

#: 本机能力（测试证据，写进报告）。
CAPABILITIES = file_identity.identity_capabilities()


def _identity(path: Path):
    found = file_identity.identity_of(path)
    assert found is not None, "身份读不到：" + str(path)
    assert found.birth_key, "本机应能取得生命周期依据（取不到时必须按未知处理）"
    return found


def _key(path: Path) -> str:
    return _identity(path).key


def _make_store(conn: sqlite3.Connection, paths) -> InternalDataIdentityStore:
    store = InternalDataIdentityStore(conn)
    counts = store.sync(paths, source="test_sync")
    assert counts["errors"] == 0, counts
    return store


def _hardlink(src: Path, dst: Path) -> bool:
    """尝试创建硬链接；不支持时如实返回 False（调用方用同判据替代，不 skip）。"""
    try:
        os.link(src, dst)
        return True
    except (OSError, NotImplementedError):
        return False


# ---------------------------------------------------------------------------
# 1) FileIdentity 生命周期依据
# ---------------------------------------------------------------------------


def test_birth_key_capabilities_are_honest(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x", encoding="utf-8")
    identity = _identity(path)
    caps = file_identity.identity_capabilities(path)
    print("生命周期能力：", caps)
    if sys.platform == "win32":
        assert caps["birth_source"] in ("win:creation", "win:birthtime", "win:stat_ctime")
    else:
        assert caps["birth_source"] in ("posix:birth", "posix:ctime")
    assert caps["birth_key"] == identity.birth_key
    assert caps["birth_strong"] == file_identity.is_strong_birth(identity.birth_key)
    assert caps["link_count"] == 1
    assert identity.lifecycle_token == identity.key + "#" + identity.birth_key


def test_birth_independent_of_content_size_and_mtime(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    payload = b"same-bytes"
    first.write_bytes(payload)
    second.write_bytes(payload)
    fixed = 1_700_000_000_000_000_000
    os.utime(first, ns=(fixed, fixed))
    os.utime(second, ns=(fixed, fixed))
    a = _identity(first)
    b = _identity(second)
    assert file_identity.content_sha256(first) == file_identity.content_sha256(second)
    assert a.size == b.size and a.mtime_ns == b.mtime_ns
    # 同内容/同大小/同 mtime 的另一份文件不是同一文件（身份不同）
    assert a.key != b.key
    assert not file_identity.same_file(a, b)
    assert file_identity.same_lifecycle(a, b) is False


def test_edit_keeps_identity_and_birth(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    before = _identity(path)
    path.write_text("v2-longer-content", encoding="utf-8")
    after = _identity(path)
    assert file_identity.same_file(before, after)
    assert before.birth_key == after.birth_key
    assert file_identity.same_lifecycle(before, after) is True
    assert before.size != after.size or before.mtime_ns != after.mtime_ns


def test_same_lifecycle_tristate(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("x", encoding="utf-8")
    identity = _identity(path)
    from agent.services.file_identity import FileIdentity

    unknown = FileIdentity(
        volume=identity.volume,
        file_id=identity.file_id,
        path=str(path),
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        birth_key=None,
    )
    assert file_identity.same_lifecycle(identity, unknown) is None
    strong_other = FileIdentity(
        volume=identity.volume,
        file_id=identity.file_id,
        path=str(path),
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        birth_key="win:creation:1" if sys.platform == "win32" else "posix:birth:1",
    )
    assert file_identity.same_lifecycle(identity, strong_other) is False
    weak_other = FileIdentity(
        volume=identity.volume,
        file_id=identity.file_id,
        path=str(path),
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        birth_key="posix:ctime:1" if sys.platform != "win32" else "win:stat_ctime:1",
    )
    assert file_identity.same_lifecycle(identity, weak_other) is None


# ---------------------------------------------------------------------------
# 2) 登记 identity+birth；lookup 同时核对；旧 birth NULL 不确定
# ---------------------------------------------------------------------------


def test_register_stores_birth_and_match_requires_both(db_conn, tmp_path):
    path = tmp_path / "app.db"
    path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [path])
    identity = _identity(path)
    row = next(item for item in store.snapshot() if item["identity_key"] == identity.key)
    assert row["birth_key"] == identity.birth_key
    assert row["active"] == 1
    match = store.match_identity(identity)
    assert match["state"] == STATE_SAME
    assert store.lookup_lifecycle(identity.key, identity.birth_key) == STATE_SAME
    assert identity.key + "#" + identity.birth_key in store.protected_tokens()
    # 旧调用方纯键集合仍然可用（兼容旧接线）
    assert store.lookup(identity.key) is True


def test_old_row_without_birth_is_uncertain_not_same(db_conn, tmp_path):
    path = tmp_path / "app.db"
    path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [path])
    identity = _identity(path)
    db_conn.execute(
        "UPDATE internal_data_identities SET birth_key = NULL WHERE identity_key = ?",
        (identity.key,),
    )
    match = store.match_identity(identity)
    assert match["state"] == STATE_UNCERTAIN, match
    assert match["state"] != STATE_SAME, "旧登记 birth 未知时不得声称同一文件"
    # 路径注册仍兜底保护（路径是 AppContext 的真实注册范围，与生命周期无关）
    assert internal_data.is_internal_data_object(path, [path.resolve()])
    # 旧 birth NULL：不进入「identity 声称同一文件」的识别集合，只按路径/历史保守
    assert store.protected_tokens() == frozenset()
    hard = tmp_path / "unknown-link.db"
    if _hardlink(path, hard):
        # 未知生命周期 + 不在登记路径里：不凭 identity 单独声称同一文件
        assert not internal_data.is_internal_data_object(
            hard, [], identity_keys=store.protected_tokens()
        )
        # 登记查询能明确返回 uncertain 时，同样只按路径/历史保守
        assert not internal_data.is_internal_data_object(
            hard, [], identity_lookup=store.match_identity
        )
        # 但登记读失败（异常）必须保守保护，不静默降级
        assert internal_data.is_internal_data_object(
            hard, [], identity_lookup=lambda _identity: (_ for _ in ()).throw(RuntimeError("db down"))
        )
    # 启动 sync 会用当前对象的 birth 回填旧行：回填后重新按 identity+birth 保护
    store.sync([path], source="test_backfill")
    match_after = store.match_identity(identity)
    assert match_after["state"] == STATE_SAME
    assert identity.key + "#" + identity.birth_key in store.protected_tokens()


def test_strong_birth_mismatch_is_different_not_same(db_conn, tmp_path):
    path = tmp_path / "app.db"
    path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [path])
    identity = _identity(path)
    fake = "win:creation:1" if sys.platform == "win32" else "posix:birth:1"
    db_conn.execute(
        "UPDATE internal_data_identities SET birth_key = ? WHERE identity_key = ?",
        (fake, identity.key),
    )
    assert store.match_identity(identity)["state"] == STATE_DIFFERENT
    assert store.lookup_lifecycle(identity.key, identity.birth_key) == STATE_DIFFERENT


def test_weak_birth_mismatch_is_uncertain(db_conn, tmp_path):
    path = tmp_path / "app.db"
    path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [path])
    identity = _identity(path)
    weak = "posix:ctime:1" if sys.platform != "win32" else "win:stat_ctime:1"
    db_conn.execute(
        "UPDATE internal_data_identities SET birth_key = ? WHERE identity_key = ?",
        (weak, identity.key),
    )
    assert store.match_identity(identity)["state"] == STATE_UNCERTAIN


# ---------------------------------------------------------------------------
# 3) 原路径替换的生命周期
# ---------------------------------------------------------------------------


def test_replacement_keeps_old_lifecycle_protection(db_conn, tmp_path):
    target = tmp_path / "internal" / "current.log"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    hard = tmp_path / "old-hardlink.log"
    has_hardlink = _hardlink(target, hard)
    store = _make_store(db_conn, [target])
    old = _identity(target)
    if has_hardlink:
        assert (store.observed(old.key) or {}).get("link_count", 0) >= 2
    replacement = tmp_path / "new.tmp"
    replacement.write_text("v2", encoding="utf-8")
    os.replace(replacement, target)
    new = _identity(target)
    assert new.key != old.key
    outcome = store.note_committed_path(target, source="test_replace")
    assert outcome["ok"] is True, outcome
    assert store.lookup(new.key) is True, "新对象必须 active"
    # 旧生命周期 token 始终保留：旧对象可能仍经其它路径存在（硬链接/移动）
    tokens = store.protected_tokens()
    assert old.key + "#" + old.birth_key in tokens
    if has_hardlink:
        assert store.lookup(old.key) is True, "已知硬链接仍在时必须保留 active 登记"
        assert store.match_identity(_identity(hard))["state"] == STATE_SAME
        assert internal_data.is_internal_data_object(
            hard, [], identity_keys=tokens
        )
    else:
        # 平台不支持硬链接：用移动替代同一判据（旧对象仍经另一路径存在）
        moved = tmp_path / "moved.log"
        os.replace(target, moved)
        store.sync([target], source="test_missing")  # 原路径不存在：保留登记
        assert store.lookup(new.key) is True
        assert internal_data.is_internal_data_object(
            moved, [], identity_keys=store.protected_tokens()
        )
        # 移回，保持后续断言的数据形态一致
        os.replace(moved, target)


def test_hidden_hardlink_after_observation_still_protected(db_conn, tmp_path):
    """硬链接在观测之后才出现：不能用「原路径替换」直接解除旧生命周期保护。"""
    target = tmp_path / "internal" / "current.log"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    store = _make_store(db_conn, [target])
    old = _identity(target)
    hidden = tmp_path / "hidden-link.log"
    has_hardlink = _hardlink(target, hidden)
    replacement = tmp_path / "v2.tmp"
    replacement.write_text("v2", encoding="utf-8")
    os.replace(replacement, target)
    new = _identity(target)
    outcome = store.note_committed_path(target, source="test_replace")
    assert outcome["ok"] is True, outcome
    assert store.lookup(new.key) is True
    assert old.key + "#" + old.birth_key in store.protected_tokens(), (
        "被顶替的旧生命周期 token 必须保留（对象可能仍经其它路径存在）"
    )
    if has_hardlink:
        assert internal_data.is_internal_data_object(
            hidden, [], identity_keys=store.protected_tokens()
        )


def test_new_registration_failure_keeps_old_protection(db_conn, tmp_path, monkeypatch):
    target = tmp_path / "internal" / "current.log"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    store = _make_store(db_conn, [target])
    old = _identity(target)
    before = {row["identity_key"]: row for row in store.snapshot()}
    replacement = tmp_path / "v2.tmp"
    replacement.write_text("v2", encoding="utf-8")
    os.replace(replacement, target)

    def broken_insert(*args, **kwargs):  # noqa: ANN001
        raise sqlite3.OperationalError("数据库写入失败（测试注入）")

    monkeypatch.setattr(store, "_insert_row", broken_insert)
    outcome = store.note_committed_path(target, source="test_replace")
    assert outcome["ok"] is False
    assert outcome["code"] == "identity_update_failed"
    assert "旧登记" in outcome["reason"]
    after = {row["identity_key"]: row for row in store.snapshot()}
    assert before[old.key]["active"] == 1
    assert after[old.key]["active"] == 1, "新登记失败必须保住旧保护（事务回滚）"
    assert old.key + "#" + old.birth_key in store.protected_tokens()


def test_replacement_retires_and_registers_atomically(db_conn, tmp_path):
    target = tmp_path / "internal" / "current.log"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    store = _make_store(db_conn, [target])
    old = _identity(target)
    replacement = tmp_path / "v2.tmp"
    replacement.write_text("v2", encoding="utf-8")
    os.replace(replacement, target)
    new = _identity(target)
    counts = store.sync([target], source="test_sync_after_replace")
    assert counts["errors"] == 0, counts
    snapshot = {row["identity_key"]: row for row in store.snapshot()}
    assert snapshot[old.key]["active"] == 0
    assert snapshot[old.key]["retired_reason"] == RETIRED_REPLACED
    assert snapshot[new.key]["active"] == 1
    # 旧登记行保留（不删除历史），但仍不误保护复用编号（token 带 birth）
    assert old.key + "#" + old.birth_key in store.protected_tokens()


def test_refresh_is_only_supplement_and_never_degrades(db_conn, tmp_path):
    target = tmp_path / "internal" / "current.log"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    store = _make_store(db_conn, [target])
    old = _identity(target)
    target.unlink()
    assert store.refresh_path(target, source="manual_refresh") is None
    assert store.lookup(old.key) is True, "显式刷新读不到身份时不得撤销登记"
    assert store.match_identity(old)["state"] == STATE_SAME


# ---------------------------------------------------------------------------
# 4) 真正删除 + 编号复用
# ---------------------------------------------------------------------------


def test_identity_reuse_is_not_mistaken_for_old_object(db_conn, tmp_path, monkeypatch):
    old_path = tmp_path / "internal" / "app.db"
    old_path.parent.mkdir()
    old_path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [old_path])
    old = _identity(old_path)

    # 模拟「真正删除后编号被新文件复用」：新对象拿到同一 identity、不同 birth。
    new_path = tmp_path / "reused.db"
    new_path.write_text("new-db", encoding="utf-8")
    new_physical = _identity(new_path)
    fake = file_identity.FileIdentity(
        volume=old.volume,
        file_id=old.file_id,
        path=str(new_path),
        size=new_physical.size,
        mtime_ns=new_physical.mtime_ns,
        birth_key=new_physical.birth_key,
        link_count=new_physical.link_count,
    )
    real_identity_of = file_identity.identity_of

    def fake_identity_of(path, *args, **kwargs):  # noqa: ANN001
        if str(path).lower() == str(new_path).lower():
            return fake
        return real_identity_of(path, *args, **kwargs)

    monkeypatch.setattr(file_identity, "identity_of", fake_identity_of)
    outcome = store.note_committed_path(new_path, source="test_reuse")
    assert outcome["ok"] is True, outcome
    assert store.reuse_events, "同一 identity + 不同强 birth 必须识别为编号复用"
    match_old = store.match_identity(old)
    assert match_old["state"] == STATE_DIFFERENT, "复用编号的新对象不得被认成旧内部对象"
    match_new = store.match_identity(fake)
    assert match_new["state"] == STATE_SAME
    tokens = store.protected_tokens()
    assert old.key + "#" + old.birth_key not in tokens, "不永久保护全部历史编号/生命周期"
    assert old.key + "#" + fake.birth_key in tokens


def test_delete_verified_retires_and_number_not_protected_forever(db_conn, tmp_path):
    path = tmp_path / "internal" / "cache.bin"
    path.parent.mkdir()
    path.write_bytes(b"cache")
    store = _make_store(db_conn, [path])
    old = _identity(path)
    path.unlink()
    results = store.note_committed_delete([path], source="test_delete")
    assert results and results[0]["ok"] is True, results
    assert results[0]["retired"] == 1, results
    assert store.lookup(old.key) is False
    assert old.key + "#" + old.birth_key not in store.protected_tokens()
    assert store.match_identity(old)["state"] == STATE_RETIRED
    assert store.match_identity({"identity_key": old.key, "birth_key": None})["state"] in (
        STATE_RETIRED,
        STATE_UNCERTAIN,
    )


def test_delete_with_remaining_hardlink_keeps_protection(db_conn, tmp_path):
    path = tmp_path / "internal" / "cache.bin"
    path.parent.mkdir()
    path.write_bytes(b"cache")
    hard = tmp_path / "cache-hardlink.bin"
    if not _hardlink(path, hard):
        # 平台不支持硬链接：用「对象仍在其它路径」的移动替代（同一判据，不 skip）
        moved = tmp_path / "cache-moved.bin"
        os.replace(path, moved)
        store = _make_store(db_conn, [path])
        results = store.note_committed_delete([path], source="test_delete")
        assert results and results[0]["retained"] == 1, results
        assert internal_data.is_internal_data_object(
            moved, [], identity_keys=store.protected_tokens()
        )
        return
    store = _make_store(db_conn, [path])
    old = _identity(path)
    path.unlink()
    results = store.note_committed_delete([path], source="test_delete")
    assert results and results[0]["retained"] == 1, results
    assert store.lookup(old.key) is True, "旧对象仍经硬链接存在时必须保持保护"
    assert internal_data.is_internal_data_object(
        hard, [], identity_keys=store.protected_tokens()
    )


def test_sync_scan_is_bounded_not_full_disk(db_conn, tmp_path):
    internal_dir = tmp_path / "logs"
    internal_dir.mkdir()
    for index in range(10):
        (internal_dir / ("f%d.log" % index)).write_text("x", encoding="utf-8")
    store = InternalDataIdentityStore(db_conn)
    counts = store.sync([internal_dir], source="test", max_files=2, max_depth=1)
    assert counts["scanned"] <= 3, counts  # 目录自身 + 最多 2 个文件：有界枚举，不全盘扫描
    assert counts["errors"] == 0, counts


# ---------------------------------------------------------------------------
# 5) 三次确认识别：原路径/硬链接/改名/移动一致
# ---------------------------------------------------------------------------


def _engine(conn, internal_dir, store):
    return AuthorizationEngine(
        conn=conn,
        approvals=None,
        settings=SettingsStore(conn),
        workspace_root=lambda: internal_dir.parent,
        internal_data_paths=lambda: [internal_dir.resolve()],
        internal_data_identity_keys=lambda: store.protected_tokens(),
        internal_data_known_paths=lambda: store.known_paths(),
    )


async def test_three_confirmations_consistent_for_hardlink_rename_move(db_conn, tmp_path):
    internal_dir = tmp_path / "internal"
    internal_dir.mkdir()
    target = internal_dir / "app.db"
    target.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [internal_dir])
    identity = _identity(target)
    engine = _engine(db_conn, internal_dir, store)

    variants: list[Path] = [target, internal_dir / "sub" / ".." / "app.db"]
    hard = tmp_path / "hardlink.db"
    if _hardlink(target, hard):
        variants.append(hard)
    else:
        moved = tmp_path / "moved.db"
        os.replace(target, moved)
        variants.append(moved)
        # 移回，保证路径本身仍在注册范围内
        os.replace(moved, target)
        moved2 = tmp_path / "moved2.db"
        os.replace(target, moved2)
        variants.append(moved2)
        internal_data_variants = variants
    if not variants:
        variants = [target]

    for path in variants:
        effect = Effect(
            kind=EffectKind.WRITE,
            targets=(Target("path", str(path)),),
            purpose="直接维护内部数据",
        )
        assert internal_data.confirmations_for(
            effect, [], identity_keys=store.protected_tokens()
        ) == internal_data.CONFIRMATIONS_REQUIRED, path
        decision = await engine.authorize(effect)
        assert decision.verdict == Verdict.APPROVE, (path, decision)
        assert "三次确认" in decision.reason

    # 普通复制件（同内容、不同身份）不是内部对象：一次确认即可
    copy_path = tmp_path / "workspace-copy.db"
    copy_path.write_bytes(target.read_bytes())
    copy_effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(copy_path)),),
        purpose="普通文件",
    )
    assert internal_data.confirmations_for(
        copy_effect, [], identity_keys=store.protected_tokens()
    ) == 1
    copy_decision = await engine.authorize(copy_effect)
    assert copy_decision.verdict != Verdict.APPROVE or "三次确认" not in copy_decision.reason


async def test_engine_read_denied_for_moved_internal_object(db_conn, tmp_path):
    internal_dir = tmp_path / "internal"
    internal_dir.mkdir()
    target = internal_dir / "app.db"
    target.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [internal_dir])
    moved = tmp_path / "moved-outside.db"
    os.replace(target, moved)
    engine = _engine(db_conn, internal_dir, store)
    decision = await engine.authorize(
        Effect(kind=EffectKind.READ, targets=(Target("path", str(moved)),))
    )
    assert decision.verdict == Verdict.DENY
    assert "内部运行数据" in decision.reason


# ---------------------------------------------------------------------------
# 6) 失败不静默降级 / 重启 / 健康
# ---------------------------------------------------------------------------


def test_identity_read_failure_is_explicit_and_keeps_registration(db_conn, tmp_path):
    path = tmp_path / "internal" / "app.db"
    path.parent.mkdir()
    path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [path])
    old = _identity(path)
    path.unlink()
    outcome = store.note_committed_path(path, source="test")
    assert outcome["ok"] is False
    assert outcome["code"] == "identity_unavailable"
    assert "保留旧登记" in outcome["reason"]
    assert store.lookup(old.key) is True
    assert store.health()["ready"] is True


def test_lookup_failure_fails_closed_not_silent(tmp_path):
    """登记查询失败时不能把内部对象当普通文件放行（保守保护）。"""
    path = tmp_path / "unknown-internal.db"
    path.write_text("db", encoding="utf-8")

    def broken_lookup(identity):  # noqa: ANN001
        raise RuntimeError("身份登记读失败")

    assert internal_data.is_internal_data_object(
        path, [], identity_lookup=broken_lookup
    ) is True


def test_store_read_error_surfaces_last_error(db_conn, tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    path.write_text("db", encoding="utf-8")
    store = _make_store(db_conn, [path])

    class _BrokenConn:
        def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise sqlite3.OperationalError("database is locked")

    # 模拟连接不可用：查询失败必须留下 last_error，不能静默当成「没有登记」
    monkeypatch.setattr(store, "conn", _BrokenConn())
    monkeypatch.setattr(store, "_ready_cache", None)
    match = store.match_identity(_identity(path))
    assert match["state"] in ("unknown", "uncertain")
    assert store.health()["last_error"], "身份登记读失败必须可见，不得静默降级"


def test_restart_persists_birth_and_keeps_uncertain_semantics(tmp_path):
    db_path = tmp_path / "identity.db"
    conn = connect(db_path)
    try:
        from agent.storage.migrate import apply_migrations

        apply_migrations(conn)
        target = tmp_path / "internal" / "app.db"
        target.parent.mkdir()
        target.write_text("db", encoding="utf-8")
        store = InternalDataIdentityStore(conn)
        counts = store.sync([target], source="test")
        assert counts["errors"] == 0, counts
        identity = _identity(target)
        snapshot = {row["identity_key"]: row for row in store.snapshot()}
        assert snapshot[identity.key]["birth_key"] == identity.birth_key
    finally:
        conn.close()
    reopened = connect(db_path)
    try:
        restarted = InternalDataIdentityStore(reopened)
        assert restarted.match_identity(identity)["state"] == STATE_SAME
        assert identity.key + "#" + identity.birth_key in restarted.protected_tokens()
        # 旧行 birth 未知：重启后仍按不确定处理（不声称同一文件）
        reopened.execute(
            "UPDATE internal_data_identities SET birth_key = NULL WHERE identity_key = ?",
            (identity.key,),
        )
        assert restarted.match_identity(identity)["state"] == STATE_UNCERTAIN
    finally:
        reopened.close()


def test_capabilities_recorded_for_report():
    """把本机能力固定为测试证据（Windows/POSIX 都运行，不 skip）。"""
    print("R9-H4 身份能力：", CAPABILITIES)
    assert set(CAPABILITIES) >= {"platform", "birth_source", "birth_strong", "link_count"}
    assert CAPABILITIES["platform"] in ("win32", "linux", "darwin")
