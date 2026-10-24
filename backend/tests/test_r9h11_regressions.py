"""R9-H11 独立验收（D 角色：真实工具/台账/引擎/审批/临时文件；模型 fake）。

基线 r9h10/main（80b06308，未修复）上先红；main 集成后同一文件必须绿。
本文件只写测试，不改生产代码；判定只使用公开入口（工具 run / 引擎 authorize /
recheck_approved / 台账 / 审批恢复）与冻结契约里的状态码字符串，不依赖 Lead 内部实现。

覆盖三类真实流程问题：
1. 历史无绑定 program_name（state=structured 但保存时解析失败）仍可经绝对路径执行
   或经文件读取绕过；
2. 原路径被替换后，旧对象的硬链接失去文件操作（read/write/delete）保护；
3. 编号复用/生命周期变化（同 identity 不同 birth）时新对象被当成旧程序放行。

真实/模拟分界（如实标注）：
* 真实：临时程序文件（复制真实 python 可执行文件）、真实 PATH、真实 SQLite 台账、
  真实审批服务、真实硬链接（平台支持时；不支持则 skip 并如实说明）、真实文件替换/删除/移动；
* 模拟：_override_identity 注入「同一 identity、不同/缺失生命周期」的文件事实
  （本机无法稳定复现操作系统 file_id 复用，见 test_p3_real_delete_recreate 的实测记录），
  注入点只替换 agent.services.file_identity.identity_of 这一身份提供器边界，
  文件本身仍真实存在；相关用例 docstring 均以「模拟」开头注明。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.requirements import RequirementLedger
from agent.services import file_identity
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsReadTool, FsWriteTool

WINDOWS = os.name == "nt"

#: 冻结契约 §2 的结构化决策码（与 agent/security/effects.py 的历史常量同值）。
DECISION_UNBOUND = "program_name_requires_object_binding"
DECISION_INVALID = "program_binding_invalidated"
DECISION_UNKNOWN = "object_state_unconfirmed"

#: 程序启动/退出阶段才会出现的错误词；「启动前拒绝」不得使用这些说法。
START_STAGE_WORDS = ("退出码", "returncode", "启动失败", "stderr", "Traceback")

#: 明确指向「对象未绑定/绑定失效/无法确认」的原因词（契约 §2 状态语义）。
OBJECT_WORDS = (
    "绑定",
    "对象",
    "无法确认",
    "未确认",
    "生命周期",
    "身份",
)


# ---------------------------------------------------------------------------
# 公共夹具与真实文件/PATH 帮助
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    """真实装配（SQLite + 审批 + 引擎 + 工具注入），模型不参与。"""
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _prepend_path(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))


def _copy_program(directory: Path, name: str = "runner") -> Path:
    """真实可执行程序副本：Windows 复制 cmd.exe / POSIX 复制 sh（系统库可解析）。

    不能复制 venv 里的 python.exe：它依赖旁边的 pyvenv.cfg，副本单独运行时
    会以「No pyvenv.cfg file / 退出码 106」失败，无法作为真实程序验证放行路径。
    """
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS and not name.lower().endswith(".exe"):
        name = name + ".exe"
    source = (
        (os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe")
        if WINDOWS
        else (shutil.which("sh") or "/bin/sh")
    )
    target = directory / name
    shutil.copy2(source, target)
    if not WINDOWS:
        target.chmod(0o755)
    return target


def _text_program(
    directory: Path,
    name: str = "runner",
    *,
    body: str = "fake program body (original)\n",
) -> Path:
    """真实存在、可被 PATH 解析的「程序文件」；内容为文本，便于验证读取/写入保护。

    读取限制用例不执行它（执行用例才需要真实可执行文件），因此文本内容足够；
    Windows 的 shutil.which 也会按 PATHEXT 找到 *.exe，POSIX 依赖可执行位。
    """
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS and not name.lower().endswith(".exe"):
        name = name + ".exe"
    target = directory / name
    target.write_text(body, encoding="utf-8")
    if not WINDOWS:
        target.chmod(0o755)
    return target


def _launch_args(root: Path, name: str = "marker.txt") -> tuple[list[str], Path]:
    """真实可执行程序的启动参数：命令成功执行会写出 root/name（marker）。"""
    root.mkdir(parents=True, exist_ok=True)
    marker = root / name
    if WINDOWS:
        return ["/c", "echo ran> " + name], marker
    return ["-c", "echo ran > '" + str(marker) + "'"], marker


def _hardlink(source: Path, alias: Path) -> bool:
    """真实硬链接；平台不支持时如实返回 False（调用方 skip 并标注）。"""
    try:
        os.link(source, alias)
        return True
    except (OSError, NotImplementedError):
        return False


def _mode(ctx, mode: str = "unrestricted") -> None:
    ctx.settings_store.set("computer.permission_mode", mode)


def _new_requirement(ctx, tag: str):
    return ctx.requirements.record_user_message(
        text="R9-H11 独立验收要求：" + tag,
        message_id="m-" + tag,
        turn_id="t-" + tag,
    )


def _save_limits(
    ctx,
    requirement_id: str,
    operations,
    *,
    reason: str,
    name: str = "runner",
    without_resolution: bool = False,
    monkeypatch=None,
    no_such_path: Path | None = None,
):
    """保存 program_name 结构化限制。

    without_resolution=True：保存时 PATH 指向不包含该程序的空目录（真实环境变化），
    台账因此无法形成 program_binding——复现「保存时解析失败的历史无绑定记录」，
    不 monkeypatch 任何产品函数。
    """
    limits = [
        {
            "operations": list(operations),
            "objects": [{"kind": "program_name", "value": name}],
            "reason": reason,
        }
    ]
    if not without_resolution:
        return ctx.requirements.set_structured_limits(
            requirement_id,
            limits,
            source="test-r9h11",
            state=requirement_limits.STATE_STRUCTURED,
        )
    assert monkeypatch is not None and no_such_path is not None
    previous = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(no_such_path))
    try:
        return ctx.requirements.set_structured_limits(
            requirement_id,
            limits,
            source="test-r9h11-unbound",
            state=requirement_limits.STATE_STRUCTURED,
        )
    finally:
        monkeypatch.setenv("PATH", previous)


def _binding(ctx, requirement_id: str):
    binding = ctx.requirements.binding_for(requirement_id)
    assert binding is not None, "测试要求必须有版本绑定"
    return binding


def _first_object(ctx, requirement_id: str) -> dict:
    requirement = ctx.requirements.by_id(requirement_id)
    assert requirement is not None
    assert requirement.structured_limits, "结构化限制应已写入"
    objects = [
        obj
        for entry in requirement.structured_limits
        for obj in (entry.get("objects") or ())
        if isinstance(obj, dict)
    ]
    assert objects, "结构化限制应至少有一个对象"
    return objects[0]


class _ReadSpy:
    """正文/摘要读取次数 spy：只观察，不改变工具行为。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def install(self, monkeypatch) -> None:
        real_sha = security_gate.content_sha256

        def spy_read(path):
            # 只记录调用：基线可能真的允许读取二进制程序文件，真实读取会抛
            # UnicodeDecodeError 把「应拒绝」的断言掩盖成异常；这里返回占位文本，
            # 让基线的错误表现为「读取被允许」的明确断言失败。
            self.calls.append(("read", str(path)))
            return "[read-by-r9h11-test-spy]"

        def spy_sha(path):
            self.calls.append(("sha", str(path)))
            return real_sha(path)

        monkeypatch.setattr(fs_tools, "_read_file_text", spy_read)
        monkeypatch.setattr(security_gate, "content_sha256", spy_sha)


def _spy_launch(monkeypatch) -> list[tuple[str, ...]]:
    """子进程启动计数：真实启动仍会执行（基线红用例需要证明它真的启动了）。"""
    calls: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        calls.append(tuple(str(item) for item in args))
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return calls


async def _wait_state(
    ctx,
    approval_id: str,
    states=("completed", "failed", "invalidated"),
    timeout: float = 15.0,
):
    deadline = asyncio.get_running_loop().time() + timeout
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = ctx.approvals.store.get(approval_id)
        if row and str(row.get("state")) in states:
            return row
        await asyncio.sleep(0.03)
    return row


def _read_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path)),),
        purpose="R9-H11 验收读取",
    )


def _write_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path)),),
        purpose="R9-H11 验收写入",
    )


def _delete_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.DELETE,
        targets=(Target("path", str(path)),),
        purpose="R9-H11 验收删除",
    )


def _execute_effect(path, *, requested: str | None = None) -> Effect:
    return Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(path)),),
        purpose="R9-H11 验收执行",
        detail={"requested_program": str(requested if requested is not None else path)},
    )


async def _authorize(ctx, binding, effect) -> object:
    with requirement_context(binding):
        return await ctx.authorization.authorize(effect)


def _assert_denied(decision, *, code: str | None = None) -> None:
    assert decision.verdict == Verdict.DENY, (decision.verdict, decision.reason)
    if code is not None:
        assert getattr(decision, "decision_code", None) == code, (
            "decision_code 不符（冻结契约 §2/§4）",
            getattr(decision, "decision_code", None),
            decision.reason,
        )


def _assert_object_words(text: str) -> None:
    assert any(word in str(text or "") for word in OBJECT_WORDS), text


def _assert_not_start_stage(text: str) -> None:
    assert not any(word in str(text or "") for word in START_STAGE_WORDS), text


def _override_identity(
    monkeypatch,
    target: Path,
    *,
    identity_key: str | None = None,
    birth_key=...,
    unavailable: bool = False,
) -> None:
    """模拟身份提供器边界：只把 target 路径的文件事实换成注入值。

    * identity_key：同一编号（模拟操作系统 file_id 复用）；
    * birth_key=...：保留真实生命周期；None：生命周期不可得；
      字符串：注入另一个生命周期（通常是强依据且与保存值不同）；
    * unavailable=True：身份不可读（identity_of 返回 None）。

    文件本体仍真实存在；这是本机无法稳定复现编号复用时的显式「模拟」分支。
    """
    key = os.path.normcase(os.path.realpath(str(target)))
    real_identity_of = file_identity.identity_of

    def same(candidate) -> bool:
        try:
            return os.path.normcase(os.path.realpath(str(candidate))) == key
        except (OSError, ValueError):
            return False

    def fake_identity_of(path, *args, **kwargs):
        if not same(path):
            return real_identity_of(path, *args, **kwargs)
        if unavailable:
            return None
        real = real_identity_of(path, *args, **kwargs)
        if real is None:
            return None
        volume, file_id = real.volume, real.file_id
        if identity_key:
            piece = str(identity_key).split(":", 1)
            if len(piece) == 2:
                volume, file_id = piece[0], piece[1]
        return file_identity.FileIdentity(
            volume=str(volume),
            file_id=str(file_id),
            path=str(path),
            size=real.size,
            mtime_ns=real.mtime_ns,
            birth_key=real.birth_key if birth_key is ... else birth_key,
            link_count=real.link_count,
        )

    monkeypatch.setattr(file_identity, "identity_of", fake_identity_of)


def _fake_birth_other() -> str:
    return "win:creation:1111111111111111" if WINDOWS else "posix:birth:1111111111111111"


def _fake_recycle(monkeypatch, trash: Path) -> list[str]:
    """真实回收站替身：只在测试临时目录内移动，记录被移动的源路径。"""
    from agent.services import recycle_bin

    trash.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []

    def mover(path):
        source = Path(str(path))
        destination = trash / (source.name + "-%d" % len(moved))
        os.replace(source, destination)
        moved.append(str(source))
        return {
            "ok": True,
            "mechanism": "test_move",
            "trashed_path": str(destination),
            "reason": "",
        }

    monkeypatch.setattr(recycle_bin, "send_to_recycle_bin", mover, raising=False)
    return moved


# ===========================================================================
# 问题一：历史无绑定 program_name（保存时解析失败）仍可经绝对路径/读取绕过
# ===========================================================================


async def test_p1_unbound_name_entry_denied_before_launch(ctx, tmp_path, monkeypatch):
    """名称入口拒绝：保存 execute+program_name=runner 但保存时无绑定。"""
    program_dir = tmp_path / "p1-name-bin"
    runner = _copy_program(program_dir)
    _prepend_path(monkeypatch, program_dir)
    _mode(ctx)
    requirement = _new_requirement(ctx, "p1-name")
    _save_limits(
        ctx,
        requirement.requirement_id,
        ["execute"],
        reason="禁止运行 runner",
        without_resolution=True,
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "p1-name-no-bin",
    )
    obj = _first_object(ctx, requirement.requirement_id)
    assert "program_binding" not in obj, "保存时解析失败的历史记录不应带对象绑定"
    binding = _binding(ctx, requirement.requirement_id)

    args, marker = _launch_args(tmp_path / "p1-name-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program="runner", args=args, cwd=str(marker.parent), timeout=30
        )

    assert result.ok is False, result
    assert launches == [], "对象未绑定的名称限制不得真正启动程序"
    assert not marker.exists()
    assert not ctx.approvals.pending(), "对象未绑定不是普通批准可以解除的"
    _assert_object_words(result.error or "")

    decision = await _authorize(
        ctx, binding, _execute_effect(runner, requested="runner")
    )
    _assert_denied(decision, code=DECISION_UNBOUND)


async def test_p1_unbound_absolute_path_denied_before_launch(ctx, tmp_path, monkeypatch):
    """该程序绝对路径运行必须停止（不依赖入口名相同）。"""
    program_dir = tmp_path / "p1-abs-bin"
    runner = _copy_program(program_dir)
    _prepend_path(monkeypatch, program_dir)
    _mode(ctx)
    requirement = _new_requirement(ctx, "p1-abs")
    _save_limits(
        ctx,
        requirement.requirement_id,
        ["execute"],
        reason="禁止运行 runner",
        without_resolution=True,
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "p1-abs-no-bin",
    )
    binding = _binding(ctx, requirement.requirement_id)

    args, marker = _launch_args(tmp_path / "p1-abs-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )

    assert result.ok is False, result
    assert launches == [], "历史无绑定记录不能经绝对路径执行同一程序对象"
    assert not marker.exists()
    assert not ctx.approvals.pending()
    _assert_object_words(result.error or "")
    _assert_not_start_stage(result.error or "")

    decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(decision, code=DECISION_UNBOUND)


async def test_p1_unbound_read_and_summary_denied_before_any_read(
    ctx, tmp_path, monkeypatch
):
    """read+program_name=runner 无绑定：读取该程序正文/摘要均被拒且读取次数 0。"""
    program_dir = tmp_path / "p1-read-bin"
    runner = _text_program(program_dir)
    _prepend_path(monkeypatch, program_dir)
    _mode(ctx)
    requirement = _new_requirement(ctx, "p1-read")
    _save_limits(
        ctx,
        requirement.requirement_id,
        ["read"],
        reason="禁止读取 runner",
        without_resolution=True,
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "p1-read-no-bin",
    )
    binding = _binding(ctx, requirement.requirement_id)
    assert "program_binding" not in _first_object(ctx, requirement.requirement_id)

    spy = _ReadSpy()
    spy.install(monkeypatch)
    original = runner.read_text(encoding="utf-8")
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await read_tool.run(path=str(runner))
    assert read_result.ok is False, read_result
    assert spy.calls == [], "读取限制必须在读取正文之前生效（禁止先读后拒）"

    write_tool = FsWriteTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        write_result = await write_tool.run(path=str(runner), content="overwritten")
    assert write_result.ok is False, write_result
    assert spy.calls == [], "覆盖前摘要也属于读取，必须在读取次数为 0 时停止"
    assert runner.read_text(encoding="utf-8") == original

    decision = await _authorize(ctx, binding, _read_effect(runner))
    _assert_denied(decision, code=DECISION_UNBOUND)


def test_p1_unbound_json_unchanged_after_env_change_and_reopen(tmp_path):
    """历史原始 JSON 在 PATH 环境变化/关库重开后保持不变（不补建绑定）。"""
    db_path = tmp_path / "p1-json.db"
    dir_a = tmp_path / "p1-json-a"
    dir_b = tmp_path / "p1-json-b"
    _text_program(dir_a, body="runner A\n")
    _text_program(dir_b, body="runner B\n")
    old_path = os.environ.get("PATH", "")
    requirement_id = ""
    raw_before = ""
    conn = connect(db_path)
    try:
        apply_migrations(conn)
        ledger = RequirementLedger(conn)
        requirement = ledger.record_user_message(
            text="禁止运行 runner", message_id="m-p1-json", turn_id="t-p1-json"
        )
        requirement_id = requirement.requirement_id
        os.environ["PATH"] = str(tmp_path / "p1-json-no-bin")
        try:
            ledger.set_structured_limits(
                requirement_id,
                [
                    {
                        "operations": ["execute"],
                        "objects": [{"kind": "program_name", "value": "runner"}],
                        "reason": "历史无绑定",
                    }
                ],
                source="test-r9h11-unbound",
                state=requirement_limits.STATE_STRUCTURED,
            )
        finally:
            os.environ["PATH"] = str(dir_a) + os.pathsep + old_path
        raw_before = conn.execute(
            "SELECT structured_limits FROM user_requirements WHERE id = ?",
            (requirement_id,),
        ).fetchone()[0]
        assert raw_before and "program_binding" not in raw_before
    finally:
        conn.close()

    # 环境变化：runner 现在解析到另一个目录；关库重开后历史 JSON 不得被改写/补建。
    os.environ["PATH"] = str(dir_b) + os.pathsep + old_path
    reopened = connect(db_path)
    try:
        reloaded = RequirementLedger(reopened).by_id(requirement_id)
        assert reloaded is not None
        raw_after = reopened.execute(
            "SELECT structured_limits FROM user_requirements WHERE id = ?",
            (requirement_id,),
        ).fetchone()[0]
        assert raw_after == raw_before, "环境变化 + 关库重开不得改写历史原始 JSON"
        assert reloaded.structured_limits_state == requirement_limits.STATE_STRUCTURED
        obj = reloaded.structured_limits[0]["objects"][0]
        assert "program_binding" not in obj
        parsed = requirement_limits.deserialize_limits(raw_after)
        assert parsed is not None
        assert "program_binding" not in parsed[0]["objects"][0]
    finally:
        os.environ["PATH"] = old_path
        reopened.close()


async def test_p1_unbound_approval_recovery_does_not_launch(
    ctx, tmp_path, monkeypatch
):
    """适用审批恢复路径同样不执行：先拿到待审批，再保存无绑定限制后批准。"""
    program_dir = tmp_path / "p1-appr-bin"
    runner = _copy_program(program_dir)
    _prepend_path(monkeypatch, program_dir)
    _mode(ctx, "restricted")
    requirement = _new_requirement(ctx, "p1-appr")
    binding = _binding(ctx, requirement.requirement_id)
    args, marker = _launch_args(tmp_path / "p1-appr-root")

    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        pending = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )
    assert pending.ok is True and pending.code == "pending_approval", pending
    approval_id = str((pending.data or {}).get("approval_id") or "")
    assert approval_id

    # 批准之前保存历史无绑定限制（同一要求）：恢复/执行前核对也必须看到它。
    _save_limits(
        ctx,
        requirement.requirement_id,
        ["execute"],
        reason="禁止运行 runner",
        without_resolution=True,
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "p1-appr-no-bin",
    )
    obj = _first_object(ctx, requirement.requirement_id)
    assert "program_binding" not in obj

    launches = _spy_launch(monkeypatch)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None
    assert str(row.get("state")) in ("failed", "invalidated"), row
    assert launches == [], "审批恢复路径不得启动对象未绑定的程序"
    assert not marker.exists()
    assert not ctx.approvals.pending()


async def test_p1_unbound_does_not_block_other_operations_entries_or_objects(
    ctx, tmp_path, monkeypatch
):
    """有可靠依据的无关对象不阻塞：操作范围外/不同入口名/不同对象。"""
    program_dir = tmp_path / "p1-unrelated-bin"
    runner_text = _text_program(program_dir)
    other_program = _copy_program(program_dir, "other")
    other_dir = tmp_path / "p1-unrelated-other"
    same_name_elsewhere = _copy_program(other_dir, "runner")
    _prepend_path(monkeypatch, program_dir)
    _mode(ctx)
    requirement = _new_requirement(ctx, "p1-unrelated")
    _save_limits(
        ctx,
        requirement.requirement_id,
        ["execute"],
        reason="禁止运行 runner",
        without_resolution=True,
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "p1-unrelated-no-bin",
    )
    binding = _binding(ctx, requirement.requirement_id)

    # 1) 操作范围外：同一程序文件的读取不受 execute 限制阻塞。
    unrelated_text = tmp_path / "p1-unrelated-note.txt"
    unrelated_text.write_text("普通文件内容\n", encoding="utf-8")
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        note_read = await read_tool.run(path=str(unrelated_text))
        program_read = await read_tool.run(path=str(runner_text))
    assert note_read.ok is True, note_read
    assert program_read.ok is True, program_read

    # 2) 不同入口名：执行另一个程序（不同名称）不被阻塞，且真正执行。
    root = tmp_path / "p1-unrelated-root"
    args, marker = _launch_args(root, "other-marker.txt")
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        other_result = await tool.run(
            program="other", args=args, cwd=str(marker.parent), timeout=30
        )
    assert other_result.ok is True, other_result
    assert marker.exists(), "不同入口名的无关程序应允许执行"

    # 3) 不同对象（另一目录同名程序，绝对路径）：不继承同名限制。
    args2, marker2 = _launch_args(root, "same-name-marker.txt")
    with requirement_context(binding):
        same_name_result = await tool.run(
            program=str(same_name_elsewhere),
            args=args2,
            cwd=str(marker2.parent),
            timeout=30,
        )
    assert same_name_result.ok is True, same_name_result
    assert marker2.exists(), "另一目录的同名程序不是同一对象，应允许执行"

    # 引擎层同样按可靠依据放行不同入口名/不同对象。
    decision = await _authorize(
        ctx, binding, _execute_effect(other_program, requested="other")
    )
    assert decision.verdict == Verdict.ALLOW, decision.reason

# ===========================================================================
# 问题二：原路径被替换后旧对象硬链接的文件操作保护
# ===========================================================================


def _bound_program_limit(
    ctx,
    tmp_path,
    monkeypatch,
    tag: str,
    operations,
    *,
    executable: bool = True,
    reason: str = "禁止该程序",
    require_birth: bool = False,
):
    """真实保存 program_name 限制并形成完整绑定（path + identity + birth）。"""
    directory = tmp_path / ("bound-" + tag + "-bin")
    runner = _copy_program(directory) if executable else _text_program(directory)
    _prepend_path(monkeypatch, directory)
    _mode(ctx)
    requirement = _new_requirement(ctx, tag)
    _save_limits(ctx, requirement.requirement_id, operations, reason=reason)
    binding = _binding(ctx, requirement.requirement_id)
    obj = _first_object(ctx, requirement.requirement_id)
    bound = obj.get("program_binding")
    assert isinstance(bound, dict), "有可靠依据保存时必须形成 program_binding：" + repr(obj)
    assert bound.get("path") and bound.get("identity"), bound
    assert os.path.normcase(str(Path(bound["path"]).resolve())) == os.path.normcase(
        str(runner.resolve())
    ), "绑定路径应是保存时解析到的真实程序"
    if require_birth and not bound.get("birth_key"):
        pytest.skip("本机取不到生命周期依据：编号复用/生命周期用例无法真实验证")
    return runner, requirement, binding, bound


def _replace_with_new_text(runner: Path, body: str = "replacement body\n") -> Path:
    replacement = runner.parent / (runner.name + ".replacement")
    replacement.write_text(body, encoding="utf-8")
    if not WINDOWS:
        replacement.chmod(0o755)
    os.replace(replacement, runner)
    return replacement


async def test_p2_read_limit_hardlink_protected_after_original_path_replaced(
    ctx, tmp_path, monkeypatch
):
    """完整绑定 + 真实硬链接：原路径被替换后旧硬链接读取必须拒绝且 0 次正文读取。"""
    runner, _requirement, binding, _bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p2-read", ["read"], executable=False
    )
    alias = tmp_path / "p2-read-alias"
    if not _hardlink(runner, alias):
        pytest.skip("平台不支持真实硬链接：旧对象别名保护无法真实验证（如实标注）")
    _replace_with_new_text(runner)

    spy = _ReadSpy()
    spy.install(monkeypatch)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        alias_result = await tool.run(path=str(alias))
    assert alias_result.ok is False, alias_result
    assert spy.calls == [], "原路径被替换后，旧硬链接读取必须在读取正文/摘要前停止"
    assert alias.exists() and alias.stat().st_size > 0

    # 原路径新对象按绑定失效拒绝（不是程序退出码/启动失败一类原因）。
    replaced = await _authorize(ctx, binding, _read_effect(runner))
    _assert_denied(replaced, code=DECISION_INVALID)
    _assert_not_start_stage(replaced.reason)

    # 同内容复制件（不同对象）与无关文件不被阻塞。
    copy = tmp_path / "p2-read-copy"
    copy.write_text(runner.read_text(encoding="utf-8"), encoding="utf-8")
    unrelated = tmp_path / "p2-read-unrelated"
    unrelated.write_text("无关文件\n", encoding="utf-8")
    copy_decision = await _authorize(ctx, binding, _read_effect(copy))
    unrelated_decision = await _authorize(ctx, binding, _read_effect(unrelated))
    assert copy_decision.verdict == Verdict.ALLOW, copy_decision.reason
    assert unrelated_decision.verdict == Verdict.ALLOW, unrelated_decision.reason


async def test_p2_write_limit_hardlink_protected_after_original_path_replaced(
    ctx, tmp_path, monkeypatch
):
    """完整绑定 + 真实硬链接：原路径被替换后旧硬链接写入必须拒绝且原内容不变。"""
    runner, _requirement, binding, _bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p2-write", ["write"], executable=False
    )
    alias = tmp_path / "p2-write-alias"
    if not _hardlink(runner, alias):
        pytest.skip("平台不支持真实硬链接：旧对象别名保护无法真实验证（如实标注）")
    original_alias = alias.read_text(encoding="utf-8")
    _replace_with_new_text(runner, "replacement body for write\n")

    tool = FsWriteTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        alias_result = await tool.run(path=str(alias), content="alias-overwritten")
    assert alias_result.ok is False, alias_result
    assert alias.read_text(encoding="utf-8") == original_alias

    with requirement_context(binding):
        replaced_result = await tool.run(path=str(runner), content="path-overwritten")
    assert replaced_result.ok is False, replaced_result
    assert "replacement body for write" in runner.read_text(encoding="utf-8")

    replaced = await _authorize(ctx, binding, _write_effect(runner))
    _assert_denied(replaced, code=DECISION_INVALID)
    _assert_not_start_stage(replaced.reason)

    copy = tmp_path / "p2-write-copy"
    copy.write_text("copy body\n", encoding="utf-8")
    unrelated = tmp_path / "p2-write-unrelated"
    unrelated.write_text("unrelated\n", encoding="utf-8")
    copy_decision = await _authorize(ctx, binding, _write_effect(copy))
    unrelated_decision = await _authorize(ctx, binding, _write_effect(unrelated))
    assert copy_decision.verdict == Verdict.ALLOW, copy_decision.reason
    assert unrelated_decision.verdict == Verdict.ALLOW, unrelated_decision.reason


async def test_p2_delete_limit_hardlink_protected_after_original_path_replaced(
    ctx, tmp_path, monkeypatch
):
    """完整绑定 + 真实硬链接：原路径被替换后旧硬链接删除必须拒绝且文件保留。"""
    runner, _requirement, binding, _bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p2-delete", ["delete"], executable=False
    )
    alias = tmp_path / "p2-delete-alias"
    if not _hardlink(runner, alias):
        pytest.skip("平台不支持真实硬链接：旧对象别名保护无法真实验证（如实标注）")
    _replace_with_new_text(runner, "replacement body for delete\n")
    moved = _fake_recycle(monkeypatch, tmp_path / "p2-delete-trash")

    tool = FsDeleteTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        alias_result = await tool.run(paths=[str(alias)])
    assert alias_result.ok is False, alias_result
    assert alias.exists(), "旧硬链接是限制绑定的同一对象，删除必须被拒绝"
    assert str(alias.resolve()) not in [str(Path(item).resolve()) for item in moved]

    with requirement_context(binding):
        replaced_result = await tool.run(paths=[str(runner)])
    assert replaced_result.ok is False, replaced_result
    assert runner.exists(), "原路径新对象按绑定失效拒绝，不得删除"

    replaced = await _authorize(ctx, binding, _delete_effect(runner))
    _assert_denied(replaced, code=DECISION_INVALID)
    _assert_not_start_stage(replaced.reason)

    # 无关文件删除仍应允许（可靠无关依据）。
    unrelated = tmp_path / "p2-delete-unrelated"
    unrelated.write_text("unrelated\n", encoding="utf-8")
    with requirement_context(binding):
        unrelated_result = await tool.run(paths=[str(unrelated)])
    assert unrelated_result.ok is True, unrelated_result
    assert not unrelated.exists()


async def test_p2_execute_limit_hardlink_and_invalidated_path_stopped_before_launch(
    ctx, tmp_path, monkeypatch
):
    """完整绑定：旧硬链接执行拒绝；原路径新对象按绑定失效拒绝（均不启动）。"""
    runner, _requirement, binding, _bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p2-exec", ["execute"], executable=True
    )
    alias = tmp_path / "p2-exec-alias"
    if not _hardlink(runner, alias):
        pytest.skip("平台不支持真实硬链接：旧对象别名保护无法真实验证（如实标注）")
    replacement = _copy_program(tmp_path / "p2-exec-replacement", "replacement")
    os.replace(replacement, runner)
    args, marker = _launch_args(tmp_path / "p2-exec-root")

    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        alias_result = await tool.run(
            program=str(alias), args=args, cwd=str(marker.parent), timeout=30
        )
    assert alias_result.ok is False, alias_result
    assert launches == [], "旧硬链接是绑定对象本身，执行必须在启动前停止"
    assert not marker.exists()

    with requirement_context(binding):
        replaced_result = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )
    assert replaced_result.ok is False, replaced_result
    assert launches == [], "原路径新对象按绑定失效拒绝，执行必须在启动前停止"
    assert not marker.exists()
    _assert_not_start_stage(replaced_result.error or "")

    alias_decision = await _authorize(ctx, binding, _execute_effect(alias))
    _assert_denied(alias_decision)
    replaced_decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(replaced_decision, code=DECISION_INVALID)

    copy = tmp_path / "p2-exec-copy.exe"
    shutil.copy2(runner, copy)
    unrelated = _copy_program(tmp_path / "p2-exec-unrelated", "unrelated")
    copy_decision = await _authorize(ctx, binding, _execute_effect(copy))
    unrelated_decision = await _authorize(ctx, binding, _execute_effect(unrelated))
    assert copy_decision.verdict == Verdict.ALLOW, copy_decision.reason
    assert unrelated_decision.verdict == Verdict.ALLOW, unrelated_decision.reason


@pytest.mark.parametrize("scenario", ["kept", "deleted", "moved"])
async def test_p2_read_alias_protected_when_original_path_kept_deleted_or_moved(
    ctx, tmp_path, monkeypatch, scenario
):
    """原路径保持/被删除/被移动时，旧别名仍按保存的绑定身份受保护。"""
    runner, _requirement, binding, _bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p2-keep-" + scenario, ["read"], executable=False
    )
    alias = tmp_path / ("p2-keep-alias-" + scenario)
    if not _hardlink(runner, alias):
        pytest.skip("平台不支持真实硬链接：旧对象别名保护无法真实验证（如实标注）")
    if scenario == "deleted":
        runner.unlink()
    elif scenario == "moved":
        os.replace(runner, tmp_path / ("p2-keep-moved-" + scenario))

    spy = _ReadSpy()
    spy.install(monkeypatch)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(alias))
    assert result.ok is False, (scenario, result)
    assert spy.calls == [], (scenario, "旧别名读取必须在正文读取前停止")

    decision = await _authorize(ctx, binding, _read_effect(alias))
    _assert_denied(decision)


# ===========================================================================
# 问题三：编号复用/生命周期变化时新对象被放行
# ===========================================================================


async def test_p3_real_delete_recreate_new_object_denied_before_launch(
    ctx, tmp_path, monkeypatch
):
    """真实删除并重建：原路径新对象执行必须在启动前停止。

    本机实测：删除后在原路径重建，操作系统 file_id（identity）已变化（未复用），
    因此这里属于「原路径对象被替换」的真实验证；「同编号不同生命周期」在下一用例
    用身份提供器边界显式模拟（用例名标注 模拟）。
    """
    runner, _requirement, binding, bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p3-real", ["execute"], require_birth=True
    )
    before = file_identity.identity_of(runner)
    assert before is not None and before.birth_key
    runner.unlink()
    _copy_program(runner.parent)
    after = file_identity.identity_of(runner)
    assert after is not None
    print(
        "R9-H11 真实删除重建身份事实：",
        {
            "before": [before.key, before.birth_key],
            "after": [after.key, after.birth_key],
            "same_identity": after.key == before.key,
        },
    )
    if after.key == before.key and after.birth_key == before.birth_key:
        pytest.skip("真实删除重建后 identity+birth 完全相同，无法区分旧/新对象")

    args, marker = _launch_args(tmp_path / "p3-real-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )
    assert result.ok is False, result
    assert launches == [], "原路径新对象必须在程序启动前被拒绝"
    assert not marker.exists()
    _assert_not_start_stage(result.error or "")

    decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(decision, code=DECISION_INVALID)


async def test_p3_number_reuse_same_identity_different_lifecycle_denied_before_start(
    ctx, tmp_path, monkeypatch
):
    """模拟：同一编号、不同生命周期（编号复用）的新对象执行/读取都必须停止。

    真实 file_id 复用本机无法稳定复现（见上一用例实测）；这里只替换身份提供器边界
    agent.services.file_identity.identity_of 的返回事实，文件对象本身仍真实存在。
    """
    runner, _requirement, binding, bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p3-reuse", ["execute", "read"], require_birth=True
    )
    runner.unlink()
    _copy_program(runner.parent)
    _override_identity(
        monkeypatch,
        runner,
        identity_key=bound["identity"],
        birth_key=_fake_birth_other(),
    )

    spy = _ReadSpy()
    spy.install(monkeypatch)
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await read_tool.run(path=str(runner))
    assert read_result.ok is False, read_result
    assert spy.calls == [], "同一编号但不同生命周期：读取必须在读取正文/摘要前停止"

    args, marker = _launch_args(tmp_path / "p3-reuse-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )
    assert result.ok is False, result
    assert launches == [], "编号复用的新对象不是旧程序，必须在启动前停止"
    assert not marker.exists()
    _assert_not_start_stage(result.error or "")

    read_decision = await _authorize(ctx, binding, _read_effect(runner))
    _assert_denied(read_decision, code=DECISION_INVALID)
    exec_decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(exec_decision, code=DECISION_INVALID)


async def test_p3_old_hardlink_still_protected_after_number_reuse(
    ctx, tmp_path, monkeypatch
):
    """模拟：编号复用后，旧对象仍经真实硬链接引用时必须继续受保护。"""
    runner, _requirement, binding, bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p3-reuse-link", ["execute", "read"], require_birth=True
    )
    alias = tmp_path / "p3-reuse-alias"
    if not _hardlink(runner, alias):
        pytest.skip("平台不支持真实硬链接：旧对象仍被引用无法真实验证（如实标注）")
    runner.unlink()
    _copy_program(runner.parent)
    _override_identity(
        monkeypatch,
        runner,
        identity_key=bound["identity"],
        birth_key=_fake_birth_other(),
    )

    spy = _ReadSpy()
    spy.install(monkeypatch)
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        alias_read = await read_tool.run(path=str(alias))
    assert alias_read.ok is False, alias_read
    assert spy.calls == [], "旧硬链接是保存时绑定的同一对象，读取必须在读取前停止"

    args, marker = _launch_args(tmp_path / "p3-reuse-link-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        alias_run = await tool.run(
            program=str(alias), args=args, cwd=str(marker.parent), timeout=30
        )
    assert alias_run.ok is False, alias_run
    assert launches == [], "旧硬链接仍是绑定对象本身，执行必须在启动前停止"
    assert not marker.exists()

    alias_read_decision = await _authorize(ctx, binding, _read_effect(alias))
    _assert_denied(alias_read_decision)
    alias_exec_decision = await _authorize(ctx, binding, _execute_effect(alias))
    _assert_denied(alias_exec_decision)
    replaced_decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(replaced_decision, code=DECISION_INVALID)


async def test_p3_lifecycle_unverifiable_denied_before_start(
    ctx, tmp_path, monkeypatch
):
    """模拟：保存有生命周期、当前同编号但生命周期取不到 -> 事实不足，不得放行。"""
    runner, _requirement, binding, bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p3-unknown", ["execute"], require_birth=True
    )
    runner.unlink()
    _copy_program(runner.parent)
    _override_identity(
        monkeypatch, runner, identity_key=bound["identity"], birth_key=None
    )

    args, marker = _launch_args(tmp_path / "p3-unknown-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )
    assert result.ok is False, result
    assert launches == [], "生命周期无法确认时不能按无关程序放行"
    assert not marker.exists()
    _assert_not_start_stage(result.error or "")

    decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(decision, code=DECISION_UNKNOWN)


async def test_p3_identity_unavailable_denied_before_start(ctx, tmp_path, monkeypatch):
    """模拟：候选对象身份不可读 -> 事实不足，不得当作无关程序放行。"""
    runner, _requirement, binding, _bound = _bound_program_limit(
        ctx, tmp_path, monkeypatch, "p3-identity", ["execute"], require_birth=True
    )
    runner.unlink()
    _copy_program(runner.parent)
    _override_identity(monkeypatch, runner, unavailable=True)

    args, marker = _launch_args(tmp_path / "p3-identity-root")
    launches = _spy_launch(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program=str(runner), args=args, cwd=str(marker.parent), timeout=30
        )
    assert result.ok is False, result
    assert launches == [], "身份不可读时不能按无关程序放行"
    assert not marker.exists()
    _assert_not_start_stage(result.error or "")

    decision = await _authorize(ctx, binding, _execute_effect(runner))
    _assert_denied(decision, code=DECISION_UNKNOWN)