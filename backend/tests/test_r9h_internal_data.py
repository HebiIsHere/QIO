"""R9-H C：名称判定清理 + 内部运行数据只按真实注册路径识别（契约 §5）。

覆盖：
* internal_data 模块不再有敏感名/后缀词表与 is_sensitive_credential_path（源码级检查）；
* 普通 .key/.pem/.crt、credentials/.config 同名目录不因名字自动成为内部数据/敏感；
* 真实注册的内部数据（目录/文件）仍按既定规则：读取拒绝、写入三次确认（全模式）；
* 相对路径/..、符号链接（能创建时）、改名不绕过注册范围；移出注册路径范围后仍按
  真实对象身份受保护（R9-H2 对象身份登记）；
* 固定查询不是绕过内部数据保护的入口（固定查询规则只作用于已验证命令，不参与文件读取）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.security import internal_data
from agent.security.effects import Effect, EffectKind, Target, Verdict

from _r9_auth_support import make_security


def _read_effect(path) -> Effect:  # noqa: ANN001
    return Effect(kind=EffectKind.READ, targets=(Target("path", str(path)),))


def _write_effect(path) -> Effect:  # noqa: ANN001
    return Effect(kind=EffectKind.WRITE, targets=(Target("path", str(path)),))


def _ordinary_names(tmp_path: Path) -> list[Path]:
    files = [
        tmp_path / "manual.key",
        tmp_path / "service.pem",
        tmp_path / "bundle.crt",
        tmp_path / "credentials" / "token.json",
        tmp_path / ".config" / "settings.toml",
        tmp_path / ".git" / "config",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ordinary user content", encoding="utf-8")
    return files


def test_module_has_no_sensitive_name_tables() -> None:
    """R9-H §5：不得保留 SENSITIVE_* 词表 / is_sensitive_credential_path。"""
    for removed in (
        "SENSITIVE_DIR_NAMES",
        "SENSITIVE_FILE_NAMES",
        "SENSITIVE_SUFFIXES",
        "is_sensitive_credential_path",
    ):
        assert not hasattr(internal_data, removed), removed
    source = Path(internal_data.__file__).read_text(encoding="utf-8")
    for marker in (
        "SENSITIVE_DIR_NAMES",
        "SENSITIVE_FILE_NAMES",
        "SENSITIVE_SUFFIXES",
        "is_sensitive_credential_path",
        "id_rsa",
        "id_ed25519",
        ".pgpass",
    ):
        assert marker not in source, marker


def test_ordinary_sensitive_looking_names_are_not_internal(tmp_path) -> None:
    files = _ordinary_names(tmp_path)
    protected = internal_data.protected_list([tmp_path / "real-internal"])  # 另一个路径
    for path in files:
        assert not internal_data.is_internal_data_path(path, protected), path
        effect = _write_effect(path)
        assert not internal_data.requires_three_confirmations(effect, protected), path
        assert internal_data.confirmations_for(effect, protected) == 1


async def test_registered_internal_data_read_denied_and_write_three_confirmations(tmp_path, db_conn) -> None:
    internal_dir = tmp_path / "qio-internal"
    internal_dir.mkdir()
    db_file = internal_dir / "app.db"
    db_file.write_text("db", encoding="utf-8")
    protected = [internal_dir]
    for mode in ("read_only", "restricted", "unrestricted"):
        security = make_security(tmp_path, db_conn, mode=mode, protected=protected)
        read_decision = await security.engine.authorize(_read_effect(db_file))
        if mode == "unrestricted":
            assert read_decision.verdict == Verdict.ALLOW, (mode, read_decision)
        else:
            assert read_decision.verdict == Verdict.DENY, (mode, read_decision)
            assert "内部运行数据" in read_decision.reason
        write_decision = await security.engine.authorize(_write_effect(db_file))
        assert write_decision.verdict == Verdict.APPROVE, (mode, write_decision)
        assert "三次确认" in write_decision.reason


def test_relative_path_and_rename_do_not_bypass_registration(tmp_path) -> None:
    internal_dir = tmp_path / "qio-internal"
    internal_dir.mkdir()
    protected_file = internal_dir / "app.db"
    protected_file.write_text("db", encoding="utf-8")
    protected = internal_data.protected_list([internal_dir])
    # 相对路径 / .. 规范化
    alias = internal_dir / "sub" / ".." / "app.db"
    assert internal_data.is_internal_data_path(alias, protected)
    assert internal_data.is_internal_data_path(str(alias), protected)
    # 改名后仍在注册目录内 → 仍受保护
    renamed = internal_dir / "renamed.db"
    os.replace(protected_file, renamed)
    assert internal_data.is_internal_data_path(renamed, protected)
    # R9-H2 §5：移出注册**路径**范围后，路径判定不再命中；同一真实对象按身份登记
    # 仍受保护（不能把"移出注册范围"当作自动普通文件）。
    outside = tmp_path / "outside.db"
    os.replace(renamed, outside)
    assert not internal_data.is_internal_data_path(outside, protected)
    from agent.services import file_identity

    object_identity = file_identity.identity_of(outside)
    assert object_identity is not None
    assert internal_data.is_internal_data_object(
        outside, [], identity_key=object_identity.key, identity_keys={object_identity.key}
    )


def test_symlink_or_normalized_alias_cannot_escape_protection(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    internal_dir = tmp_path / "qio-internal"
    internal_dir.mkdir()
    protected_file = internal_dir / "app.db"
    protected_file.write_text("db", encoding="utf-8")
    protected = internal_data.protected_list([internal_dir])
    link = workspace / "shortcut.db"
    try:
        link.symlink_to(protected_file)
    except (OSError, NotImplementedError):
        # 平台不允许创建符号链接：用等价的路径别名（..）验证规范化，不跳过用例。
        alias = workspace / ".." / "qio-internal" / "app.db"
        assert internal_data.is_internal_data_path(alias, protected)
        return
    assert internal_data.is_internal_data_path(link, protected)
