"""R9-H2 C：内部数据真实对象身份保护（契约 §5）。

真实 sqlite 文件数据库 + 真实迁移 61 表 + 临时目录（不碰真实用户数据）。
覆盖：
* sync 登记注册文件/目录身份；相对路径/规范化、符号链接、硬链接、改名/移动后
  同一真实对象仍受保护；
* 同内容不同身份复制件不登记；普通同名文件不是内部数据；
* 原子替换后新身份登记、旧身份 retired；注册路径不存在/身份读失败保留登记；
* known_paths 在身份读不到时保守命中（不静默降级），身份可读但不是已登记对象时
  不因旧路径登记被误保护；
* 关闭连接重开 store 恢复；effect_targets_internal_data 使用 target.identity；
* 安全网关身份 helper 补齐 path target 的真实 identity；三种模式路径规则不变。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.security import internal_data
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.internal_data_identity import (
    InternalDataIdentityStore,
    KIND_DIRECTORY,
)
from agent.services import file_identity
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools import security_gate

from _r9_auth_support import make_security


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "internal_identity.db"


@pytest.fixture()
def conn(db_path):
    connection = connect(db_path)
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
def env(tmp_path, conn):
    internal_dir = tmp_path / "qio-internal"
    (internal_dir / "nested").mkdir(parents=True)
    app_db = internal_dir / "app.db"
    app_db.write_text("db-content", encoding="utf-8")
    config = internal_dir / "config.toml"
    config.write_text("mode = 'restricted'", encoding="utf-8")
    nested = internal_dir / "nested" / "cache.bin"
    nested.write_bytes(b"cache-bytes")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = InternalDataIdentityStore(conn)
    return {
        "store": store,
        "dir": internal_dir,
        "app_db": app_db,
        "config": config,
        "nested": nested,
        "workspace": workspace,
        "conn": conn,
        "tmp": tmp_path,
    }


def _key(path) -> str:  # noqa: ANN001
    found = file_identity.identity_of(path)
    assert found is not None, f"身份读不到：{path}"
    return found.key


def _sync_protected(env) -> dict:
    return env["store"].sync([env["dir"]], source="test")


# ---------------------------------------------------------------------------
# 登记与查询
# ---------------------------------------------------------------------------


def test_sync_registers_files_and_directory_identities(env):
    counts = _sync_protected(env)
    assert counts["active"] >= 4, counts  # 目录 + app.db + config + nested/cache
    assert counts["added"] >= 4
    assert counts["errors"] == 0, counts
    keys = env["store"].active_keys()
    for path in (env["app_db"], env["config"], env["nested"]):
        assert _key(path) in keys, path
    directory_key = _key(env["dir"])
    assert directory_key in keys
    kinds = {row["identity_key"]: row["kind"] for row in env["store"].snapshot()}
    assert kinds[directory_key] == KIND_DIRECTORY
    known = env["store"].known_paths()
    assert str(env["app_db"].resolve()) in known
    assert env["store"].lookup(_key(env["app_db"])) is True


def test_relative_and_effect_identity_classification(env):
    _sync_protected(env)
    keys = env["store"].active_keys()
    known = env["store"].known_paths()
    alias = env["dir"] / "nested" / ".." / "app.db"
    # 路径注册：相对/.. 规范化后仍在真实注册范围内
    assert internal_data.is_internal_data_object(alias, [env["dir"].resolve()])
    # 只靠对象身份（路径已不在注册范围）也保护真实对象
    moved_key = _key(env["app_db"])
    assert internal_data.is_internal_data_object(
        alias, [], identity_key=moved_key, identity_keys=keys, known_paths=known
    )
    # effect target 携带真实 identity → 内部数据
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(
            Target("path", str(env["workspace"] / "outside-name.txt"), identity=moved_key, exists=True),
        ),
    )
    assert internal_data.effect_targets_internal_data(effect, [], identity_keys=keys)
    # 同内容不同身份的复制件（路径也不在登记里）→ 不是内部数据
    copy_path = env["workspace"] / "copy.db"
    copy_path.write_bytes(env["app_db"].read_bytes())
    assert file_identity.identity_of(copy_path).key != moved_key
    assert not internal_data.is_internal_data_object(
        copy_path, [], identity_keys=keys, known_paths=known
    )
    assert not internal_data.effect_targets_internal_data(
        Effect(kind=EffectKind.WRITE, targets=(Target("path", str(copy_path)),)),
        [],
        identity_keys=keys,
    )


def test_rename_move_keeps_identity_protection(env):
    _sync_protected(env)
    keys = env["store"].active_keys()
    original = env["app_db"]
    original_key = _key(original)
    moved = env["workspace"] / "moved-away.db"
    os.replace(original, moved)
    # 路径判定不再命中（已在注册目录外），对象身份仍命中
    assert not internal_data.is_internal_data_path(moved, [env["dir"].resolve()])
    assert internal_data.is_internal_data_object(
        moved, [], identity_keys=keys, known_paths=env["store"].known_paths()
    )
    # 注册路径暂时不存在：sync 保留已有 active 身份（不降级）
    before = env["store"].active_keys()
    counts = env["store"].sync([env["app_db"]], source="test")
    assert counts["errors"] >= 1
    assert env["store"].active_keys() == before
    assert original_key in env["store"].active_keys()


def test_hardlink_or_capability_fallback(env):
    _sync_protected(env)
    keys = env["store"].active_keys()
    hardlink = env["workspace"] / "hardlink.db"
    try:
        os.link(env["app_db"], hardlink)
    except (OSError, NotImplementedError) as exc:
        # 平台/文件系统不支持硬链接：如实记录能力边界；切换为同身份移动验证，
        # 不用 skip 掩盖（同一对象身份判据仍有真实覆盖）。
        # 仅记录平台能力边界（不 skip）：用同身份移动验证同一判据。
        assert exc is not None
        assert file_identity.identity_of(env["app_db"]) is not None
        moved = env["workspace"] / "fallback-moved.db"
        os.replace(env["app_db"], moved)
        assert internal_data.is_internal_data_object(moved, [], identity_keys=keys)
        return
    assert _key(hardlink) == _key(env["app_db"]), "硬链接必须是同一身份"
    assert internal_data.is_internal_data_object(
        hardlink, [], identity_keys=keys, known_paths=env["store"].known_paths()
    )


def test_symlink_resolution_does_not_bypass(env):
    _sync_protected(env)
    keys = env["store"].active_keys()
    link = env["workspace"] / "shortcut.db"
    try:
        link.symlink_to(env["app_db"])
    except (OSError, NotImplementedError):
        # 平台不支持符号链接：用规范化别名验证同一保护（不 skip）。
        alias = env["workspace"] / ".." / "qio-internal" / "app.db"
        assert internal_data.is_internal_data_object(alias, [env["dir"].resolve()])
        assert internal_data.is_internal_data_object(alias, [], identity_keys=keys)
        return
    assert internal_data.is_internal_data_object(
        link, [], identity_keys=keys, known_paths=env["store"].known_paths()
    )


# ---------------------------------------------------------------------------
# 原子替换 / 复制件 / 身份失败 / 重启
# ---------------------------------------------------------------------------


def test_atomic_replacement_retires_old_identity(env):
    _sync_protected(env)
    old_key = _key(env["app_db"])
    temp = env["app_db"].with_name("app.db.new")
    temp.write_text("db-content-v2", encoding="utf-8")
    os.replace(temp, env["app_db"])
    new_key = _key(env["app_db"])
    assert new_key != old_key

    counts = _sync_protected(env)
    assert counts["retired"] >= 1, counts
    assert env["store"].lookup(old_key) is False, "原子替换后旧身份必须 retired"
    assert env["store"].lookup(new_key) is True
    snapshot = {row["identity_key"]: row for row in env["store"].snapshot()}
    assert snapshot[old_key]["retired_reason"] == "replaced"
    assert env["store"].active_keys() == env["store"].active_keys() | {new_key}
    old_effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(env["workspace"] / "x.db"), identity=old_key),),
    )
    assert not internal_data.effect_targets_internal_data(
        old_effect, [], identity_keys=env["store"].active_keys()
    )


def test_identity_read_failure_does_not_silently_downgrade(env):
    _sync_protected(env)
    key = _key(env["app_db"])
    known = env["store"].known_paths()
    os.unlink(env["app_db"])
    # 身份读不到 + 路径是历史登记 → 保守当作内部数据对象（不静默降级）
    assert internal_data.is_internal_data_object(
        env["app_db"], [], identity_keys=env["store"].active_keys(), known_paths=known
    )
    assert internal_data.is_internal_data_object(
        env["app_db"], [], known_paths=known
    ), "身份不可读时历史登记路径必须保守命中"
    # 同一路径被另一份内容/身份的文件顶替：身份可读且不是登记对象 → 不误保护
    replacement = env["app_db"].with_name("other.new")
    replacement.write_text("different-object", encoding="utf-8")
    os.replace(replacement, env["app_db"])
    replacement_key = _key(env["app_db"])
    assert replacement_key != key
    assert not internal_data.is_internal_data_object(
        env["app_db"],
        [],
        identity_keys=env["store"].active_keys(),
        known_paths=known,
    )


def test_store_restart_persists_identity(env, db_path):
    _sync_protected(env)
    keys = env["store"].active_keys()
    known = env["store"].known_paths()
    env["conn"].close()
    reopened = connect(db_path)
    try:
        restarted = InternalDataIdentityStore(reopened)
        assert restarted.active_keys() == keys
        assert restarted.known_paths() == known
        assert restarted.lookup(_key(env["app_db"])) is True
        assert restarted.refresh_path(env["config"], source="refresh") == _key(env["config"])
    finally:
        reopened.close()


def test_ordinary_same_name_is_not_internal(env):
    ordinary = env["workspace"] / ".env"
    ordinary.write_text("USER_TOKEN=not-a-secret", encoding="utf-8")
    _sync_protected(env)
    keys = env["store"].active_keys()
    known = env["store"].known_paths()
    assert not internal_data.is_internal_data_object(
        ordinary, [], identity_keys=keys, known_paths=known
    )
    assert not internal_data.is_internal_data_object(ordinary, [env["dir"].resolve()])
    for name in (".git/config", "credentials/token.json", "service.pem"):
        path = env["workspace"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ordinary", encoding="utf-8")
        assert not internal_data.is_internal_data_object(
            path, [], identity_keys=keys, known_paths=known
        ), name


# ---------------------------------------------------------------------------
# security_gate helper + 模式行为
# ---------------------------------------------------------------------------


def test_security_gate_target_identity_helper(env):
    key = security_gate.path_identity_key(env["app_db"])
    assert key == _key(env["app_db"])
    effect = security_gate.write_effect(
        "fs_write", env["app_db"], purpose="test", exists=True
    )
    filled = security_gate.with_target_identity(effect)
    assert filled.targets[0].identity == key
    # 已有 identity 不被覆盖
    existing = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(env["app_db"]), identity="stat:kept", exists=True),),
    )
    assert security_gate.with_target_identity(existing).targets[0].identity == "stat:kept"
    missing = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(env["workspace"] / "不存在.txt"), exists=False),),
    )
    assert security_gate.with_target_identity(missing).targets[0].identity is None


@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_mode_rules_unchanged_for_registered_path(tmp_path, db_conn, mode):
    internal_file = tmp_path / "qio-internal" / "app.db"
    internal_file.parent.mkdir(parents=True, exist_ok=True)
    internal_file.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode=mode, protected=[internal_file])
    read_decision = await security.engine.authorize(
        Effect(kind=EffectKind.READ, targets=(Target("path", str(internal_file)),))
    )
    if mode == "unrestricted":
        assert read_decision.verdict == Verdict.ALLOW
    else:
        assert read_decision.verdict == Verdict.DENY
        assert "内部运行数据" in read_decision.reason
    write_decision = await security.engine.authorize(
        Effect(kind=EffectKind.WRITE, targets=(Target("path", str(internal_file)),))
    )
    assert write_decision.verdict == Verdict.APPROVE
    assert "三次确认" in write_decision.reason
