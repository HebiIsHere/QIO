"""R9-H3 C：正常内部文件替换后身份自动更新（契约 §4）。

真实链路（不是只调 store.refresh_path）：
* 真实 AppContext（真实 sqlite 文件库 + 真实迁移 61 表 + 临时数据目录）；
* 真实可信写入入口：
  1) 审批通过后的真实执行器 ctx.operations.execute("fs.write", ...) —— 实际原子替换；
  2) SettingsService.apply（真实设置保存，提交成功后通知身份更新）；
* 断言不手动刷新、不重启时新对象硬链接立即按内部数据受保护（引擎判定），
  旧身份 retired、新身份 active；失败明确返回/记录；普通复制件/同名不误伤。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.security import internal_data
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services import file_identity
from agent.services.app import AppContext
from agent.services.settings_service import SettingsService, SettingsValidationError
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools import security_gate


@pytest.fixture()
def app_env(tmp_path):
    data = tmp_path / "qio-data"
    for name in ("logs", "archive", "workspace"):
        (data / name).mkdir(parents=True, exist_ok=True)
    log = data / "logs" / "current.log"
    log.write_text("version-1", encoding="utf-8")
    conn = connect(data / "app.db")
    apply_migrations(conn)
    settings = Settings(data_dir=data)
    return SimpleNamespace(
        tmp=tmp_path,
        data=data,
        log=log,
        logs_dir=data / "logs",
        conn=conn,
        settings=settings,
    )


@pytest.fixture()
async def ctx(app_env):
    context = AppContext(app_env.settings, app_env.conn, EventBus())
    try:
        yield context
    finally:
        await context.aclose()
        app_env.conn.close()


def _key(path) -> str:  # noqa: ANN001
    found = file_identity.identity_of(path)
    assert found is not None, f"身份读不到：{path}"
    return found.key


def _exec_ctx(ctx, *, kind: str, payload: dict, fingerprint: dict) -> ExecutionContext:
    operation = PendingOperation(
        kind=kind,
        label="测试内部文件维护",
        payload=payload,
        fingerprint=fingerprint,
        confirmations_required=3,
        mode_at_request="restricted",
    )
    return ExecutionContext(
        approval_id="approval-r9h3-c",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
    )


async def _approved_write(ctx, path: Path, content: str) -> tuple[object, str]:
    """真实可信写入入口：审批通过后的 fs.write 执行器（内部数据三次确认已完成）。"""
    old_key = _key(path)
    payload = {
        "path": str(path),
        "content": content,
        "content_sha256": security_gate.text_sha256(content),
    }
    fingerprint = {
        "internal_data_root": str(ctx.settings.data_dir / "logs"),
        "identity": security_gate.file_identity(path),
    }
    result = await ctx.operations.execute(
        "fs.write",
        payload,
        _exec_ctx(ctx, kind="fs.write", payload=payload, fingerprint=fingerprint),
    )
    return result, old_key


# ---------------------------------------------------------------------------
# 1) 真实审批执行器写入：原子替换后不刷新/不重启，新对象身份立即生效
# ---------------------------------------------------------------------------


async def test_approved_replace_updates_identity_without_manual_refresh(ctx, app_env):
    store = ctx.internal_data_identity
    assert store is not None
    old_key = _key(app_env.log)
    assert store.lookup(old_key) is True, "启动 sync 应已登记原内部文件"
    # 旧对象仍被硬链接引用：先建引用，用于核对旧身份生命周期（保留历史行）。
    old_link = app_env.tmp / "old-link.log"
    os.link(app_env.log, old_link)

    result, previous = await _approved_write(ctx, app_env.log, "version-2")
    assert result.ok is True, result
    assert not result.detail.get("internal_identity_error"), result.detail
    new_key = _key(app_env.log)
    assert new_key != old_key

    assert store.lookup(new_key) is True, "提交成功后必须已登记新对象身份"
    assert store.lookup(old_key) is False, "原子替换后旧身份必须 retired"
    snapshot = {row["identity_key"]: row for row in store.snapshot()}
    assert snapshot[old_key]["active"] == 0
    assert snapshot[old_key]["retired_reason"] == "replaced"
    assert snapshot[old_key]["path"] == str(app_env.log.resolve())
    # 旧对象仍被引用：历史行保留（不删除登记）。
    assert old_link.exists()

    # 不手动刷新、不重启：新对象硬链接立即按内部数据对象受保护。
    new_link = app_env.tmp / "new-link.log"
    os.link(app_env.log, new_link)
    assert internal_data.is_internal_data_object(
        new_link,
        [],
        identity_keys=ctx._internal_identity_keys(),
        known_paths=ctx._internal_identity_known_paths(),
    )
    read_decision = await ctx.authorization.authorize(
        Effect(kind=EffectKind.READ, targets=(Target("path", str(new_link)),))
    )
    assert read_decision.verdict == Verdict.DENY
    assert "内部运行数据" in read_decision.reason
    write_decision = await ctx.authorization.authorize(
        Effect(kind=EffectKind.WRITE, targets=(Target("path", str(new_link)),))
    )
    assert write_decision.verdict == Verdict.APPROVE
    assert "三次确认" in write_decision.reason


async def test_approved_create_registers_new_internal_file(ctx, app_env):
    store = ctx.internal_data_identity
    new_path = app_env.logs_dir / "created.jsonl"
    assert not new_path.exists()
    payload = {
        "path": str(new_path),
        "content": "{\"ok\": true}\n",
        "content_sha256": security_gate.text_sha256("{\"ok\": true}\n"),
    }
    fingerprint = {"internal_data_root": str(app_env.logs_dir)}
    result = await ctx.operations.execute(
        "fs.write",
        payload,
        _exec_ctx(ctx, kind="fs.write", payload=payload, fingerprint=fingerprint),
    )
    assert result.ok is True, result
    new_key = _key(new_path)
    assert store.lookup(new_key) is True, "新建内部文件提交成功后身份必须已登记"
    link = app_env.tmp / "created-link.jsonl"
    os.link(new_path, link)
    assert internal_data.is_internal_data_object(
        link, [], identity_keys=ctx._internal_identity_keys()
    )
    # 原路径/相对/规范化判定一致。
    assert internal_data.is_internal_data_object(new_path, [app_env.logs_dir.resolve()])
    (app_env.logs_dir / "sub").mkdir(exist_ok=True)
    alias = app_env.logs_dir / "sub" / ".." / "created.jsonl"
    assert internal_data.is_internal_data_object(
        alias, [], identity_keys=ctx._internal_identity_keys()
    )


async def test_renamed_or_moved_after_auto_update_still_protected(ctx, app_env):
    store = ctx.internal_data_identity
    result, previous = await _approved_write(ctx, app_env.log, "version-3")
    assert result.ok is True
    new_key = _key(app_env.log)
    moved = app_env.tmp / "moved-outside.log"
    os.replace(app_env.log, moved)
    # 不刷新：路径离开注册范围，但对象身份仍是登记的新身份。
    assert not internal_data.is_internal_data_path(moved, [app_env.logs_dir.resolve()])
    assert store.lookup(new_key) is True
    assert internal_data.is_internal_data_object(
        moved, [], identity_keys=ctx._internal_identity_keys()
    )
    decision = await ctx.authorization.authorize(
        Effect(kind=EffectKind.READ, targets=(Target("path", str(moved)),))
    )
    assert decision.verdict == Verdict.DENY


async def test_symlink_or_platform_alias_resolves_to_updated_identity(ctx, app_env):
    result, _before = await _approved_write(ctx, app_env.log, "version-4")
    assert result.ok is True
    keys = ctx._internal_identity_keys()
    link = app_env.tmp / "symlink.log"
    try:
        link.symlink_to(app_env.log)
    except (OSError, NotImplementedError):
        # Windows 本机无创建符号链接特权（WinError 1314）：用硬链接/规范化别名
        # 验证同一判据，不 skip；能力边界写入报告。
        hard = app_env.tmp / "hardlink.log"
        os.link(app_env.log, hard)
        assert internal_data.is_internal_data_object(hard, [], identity_keys=keys)
        alias = app_env.logs_dir / ".." / "logs" / "current.log"
        assert internal_data.is_internal_data_object(alias, [], identity_keys=keys)
        return
    assert internal_data.is_internal_data_object(link, [], identity_keys=keys)


# ---------------------------------------------------------------------------
# 2) 真实设置保存（可信写入入口）提交成功后通知身份更新
# ---------------------------------------------------------------------------


async def test_settings_save_trusted_entry_notifies_identity(ctx, app_env, monkeypatch):
    from agent.storage.db_identity import connection_db_path

    db_file = Path(connection_db_path(app_env.conn) or "")
    assert db_file.exists()
    store = ctx.internal_data_identity
    assert store.lookup(_key(db_file)) is True, "启动 sync 应已登记数据库文件身份"

    calls: list[dict] = []
    original = store.note_committed_paths

    def spy(paths, **kwargs):  # noqa: ANN001
        calls.append({"paths": [str(p) for p in paths], "source": kwargs.get("source")})
        return original(paths, **kwargs)

    monkeypatch.setattr(store, "note_committed_paths", spy)
    current = int(ctx.settings_store.get("memory.fragment_max_turns", "8") or 8)
    patch_value = current + 1 if current < 64 else current - 1
    result = SettingsService(ctx).apply({"memory": {"fragment_max_turns": patch_value}})
    assert result.changed, result
    assert calls, "设置提交成功后必须通知内部数据身份更新（真实可信写入入口）"
    assert calls[-1]["source"] == "settings_service.apply"
    assert str(db_file.resolve()) in calls[-1]["paths"]

    # 校验失败（未提交）不得触发身份更新。
    before = len(calls)
    with pytest.raises(SettingsValidationError):
        SettingsService(ctx).apply({"memory": {"fragment_max_turns": 0}})
    assert len(calls) == before

    # DB 正常原地写：身份不变，登记仍 active（幂等，不误退休）。
    assert store.lookup(_key(db_file)) is True


async def test_identity_update_failure_is_explicit_not_silent(ctx, app_env, monkeypatch):
    store = ctx.internal_data_identity
    old_key = _key(app_env.log)

    def broken(path, **kwargs):  # noqa: ANN001
        return {
            "ok": False,
            "code": "identity_update_failed",
            "reason": "测试注入：身份登记写入失败",
            "path": str(path),
        }

    monkeypatch.setattr(store, "note_committed_path", broken)
    result, _previous = await _approved_write(ctx, app_env.log, "version-5")
    # 真实文件写入已成功：不能把它报成失败；但身份更新失败必须明确返回/记录。
    assert result.ok is True
    assert result.detail.get("internal_identity_error"), result.detail
    assert ctx.internal_data_identity_error
    assert "身份登记写入失败" in ctx.internal_data_identity_error
    # 失败保留事实：旧登记仍在（路径判定兜底），不静默降级、不删除历史。
    assert store.snapshot(), "身份更新失败不得清空已有登记"
    assert any(row["identity_key"] == old_key for row in store.snapshot())


def test_store_reports_identity_unavailable_and_keeps_registration(ctx, app_env):
    store = ctx.internal_data_identity
    old_key = _key(app_env.log)
    app_env.log.unlink()
    outcome = store.note_committed_path(app_env.log, source="test")
    assert outcome["ok"] is False
    assert outcome["code"] == "identity_unavailable"
    assert "保留旧登记" in outcome["reason"]
    assert store.lookup(old_key) is True, "身份读不到时不得删除/降级已有登记"


# ---------------------------------------------------------------------------
# 3) 普通复制件/同名材料不误伤
# ---------------------------------------------------------------------------


async def test_copy_and_same_name_are_not_mistaken_for_internal(ctx, app_env):
    result, _previous = await _approved_write(ctx, app_env.log, "version-6")
    assert result.ok is True
    keys = ctx._internal_identity_keys()
    known = ctx._internal_identity_known_paths()
    copy_path = app_env.data / "workspace" / "current.log"
    copy_path.write_bytes(app_env.log.read_bytes())
    assert internal_data.is_internal_data_object(
        copy_path, [], identity_keys=keys, known_paths=known
    ) is False, "同内容不同身份的普通复制件不能因为内容相同被保护"
    ordinary = app_env.data / "workspace" / ".env"
    ordinary.write_text("USER=1", encoding="utf-8")
    assert internal_data.is_internal_data_object(ordinary, [], identity_keys=keys) is False
    # 受保护对象本身仍按路径/身份命中。
    assert internal_data.is_internal_data_object(app_env.log, [app_env.logs_dir.resolve()])
    assert internal_data.is_internal_data_object(
        app_env.log, [], identity_keys=ctx._internal_identity_keys()
    )


# ---------------------------------------------------------------------------
# 4) 边界：只跟踪真实 fs.write/fs.patch 的明确目标；工具不能自报身份
# ---------------------------------------------------------------------------


def test_trusted_commit_paths_are_bounded_and_kind_scoped(ctx, app_env):
    ctx_any = ctx
    # 普通工具执行（tool.invoke）没有可核对的"变更对象"：不更新、不猜测。
    assert ctx_any._trusted_commit_paths("tool.invoke", {"path": str(app_env.log)}) == []
    assert ctx_any._trusted_commit_paths("proc.run_program", {"args": []}) == []
    # 只取真实 fs 写入的明确目标；批量删除有界（最多 50），不扫描。
    assert ctx_any._trusted_commit_paths(
        "fs.write", {"path": str(app_env.log)}
    ) == [str(app_env.log)]
    many = [{"path": str(app_env.logs_dir / f"f{i}.log")} for i in range(80)]
    picked = ctx_any._trusted_commit_paths("fs.delete", {"paths": many})
    assert len(picked) == 50
    # 身份只能由真实文件读取：note_committed_path 不接受外部传入的 identity_key 自报。
    import inspect

    parameters = inspect.signature(
        ctx.internal_data_identity.note_committed_path
    ).parameters
    assert "identity_key" not in parameters
    assert "identity" not in parameters


def test_ordinary_tool_path_does_not_register_identity(ctx, app_env):
    """普通工具对普通文件（非内部注册范围）不会登记身份。"""
    store = ctx.internal_data_identity
    workspace_file = app_env.data / "workspace" / "notes.txt"
    workspace_file.write_text("ordinary", encoding="utf-8")
    workspace_key = _key(workspace_file)
    # 即使伪造一个可信提交路径，也只按真实注册范围/历史登记筛选。
    before = set(store.active_keys())
    ctx.note_trusted_internal_commit([workspace_file], source="test")
    assert workspace_key not in store.active_keys()
    assert set(store.active_keys()) == before
