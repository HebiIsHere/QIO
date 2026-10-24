"""R9-H12 独立验收（D：真实工具/引擎/台账/审批/临时文件；模型 fake）。

基线 503df11a（未修复）上先红；集成 r9h12/main 后同一文件必须绿。
本文件只写测试，不改生产代码；判定只使用公开入口（工具 run / 引擎 authorize /
assess_limit_conflict / 台账）与冻结契约里的状态码字符串，不依赖内部实现细节。

覆盖 _R9H12-COMMON.md §2/§3/§6：
* P1：保存时查找失败（无绑定、无保存候选）的 program_name=execute 限制，
  经真实硬链接暴露的**另一个入口名**仍能启动同一程序对象；原名称查找失败时
  绝对路径已被拒绝，但别入口名放行 —— 旧代码把「入口名不同」当可靠无关。
* P2：保存时候选 A 但身份读取失败（身份提供器边界注入，见下），之后 PATH 改到 B：
  未恢复时读取/执行 A 被当作「当前解析到别处」放行；调用真实专用恢复入口
  （ctx._ensure_legacy_structured_limits，当前候选解析器用 fake 指向 B）后，
  旧实现把限制绑定到当前 PATH 的 B，A 之后反而可执行 —— 契约要求只采纳保存候选。
* P3：显式程序路径 read 限制 + 真实硬链接；限制路径身份不可读（注入，标模拟）时
  读硬链接必须失败关闭，旧实现按「限制路径身份不可读」判无关并返回正文。
* 规则反例：不同入口名 / 名称查找失败 / 当前解析到别处 / 限制路径身份不可读 /
  无完整绑定，单独都不能变成 ALLOW；可靠无关（候选不存在、操作范围外、
  已绑定不同对象、前缀外、已绑定入口解析到别的对象）必须继续。

真实/模拟分界（如实标注）：
* 真实：临时程序文件（复制真实 cmd.exe / sh）、真实 PATH 变化、真实 SQLite 台账、
  真实引擎/工具装配、真实审批服务、真实硬链接（平台支持时；不支持则 skip 并说明）、
  真实程序启动与 marker 文件；
* 模拟：身份提供器边界注入 agent.services.file_identity.identity_of
  （P2 保存时 A 身份不可读；P3 限制路径身份不可读），文件本体真实存在；
  P2 恢复期的「当前候选解析器」用 fake 函数替换 requirement_limits.
  _probe_program_binding 返回 B 的绑定事实（同时真实 PATH 也确实指向 B）；
  相关用例 docstring 以「模拟」开头注明。模型全程不参与（无 fake 模型调用）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.requirements import RequirementBinding
from agent.services import file_identity
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsReadTool

WINDOWS = os.name == "nt"

#: 冻结契约 §2 的结构化决策码（与 agent/security/effects.py 的常量同值）。
DECISION_UNBOUND = "program_name_requires_object_binding"
DECISION_UNKNOWN = "object_state_unconfirmed"

#: 程序启动/退出阶段才会出现的错误词；「启动前拒绝」不得使用这些说法。
START_STAGE_WORDS = ("退出码", "returncode", "启动失败", "stderr", "Traceback")

#: 明确指向「对象未绑定/绑定失效/无法确认」的原因词。
OBJECT_WORDS = ("绑定", "对象", "无法确认", "未确认", "生命周期", "身份")


# ---------------------------------------------------------------------------
# 公共夹具与真实文件/PATH 帮助（与 H11 独立验收同一套真实边界）
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    """真实装配（SQLite + 审批 + 引擎 + 工具注入），模型不参与。"""
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _prepend_path(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))


def _copy_program(directory: Path, name: str) -> Path:
    """真实可执行程序副本（Windows 复制 cmd.exe / POSIX 复制 sh）。"""
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


def _text_program(directory: Path, name: str, body: str = "program body\n") -> Path:
    """真实存在、可被 PATH 解析的「程序文件」（内容为文本，便于读取验证）。"""
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
        text="R9-H12 独立验收要求：" + tag,
        message_id="m-" + tag,
        turn_id="t-" + tag,
    )


_PROGRAM_LIMIT = {
    "operations": ["execute"],
    "objects": [{"kind": "program_name", "value": "runner"}],
    "reason": "禁止运行 runner",
}


def _save_limits(ctx, requirement_id: str, limits, *, source: str = "test-r9h12"):
    return ctx.requirements.set_structured_limits(
        requirement_id,
        limits,
        source=source,
        state=requirement_limits.STATE_STRUCTURED,
    )


def _save_unbound_program_limit(
    ctx,
    requirement_id: str,
    operations,
    name: str,
    *,
    monkeypatch,
    no_such_path: Path,
):
    """保存 program_name 限制，但保存时 PATH 指向空目录 -> 查找失败、无绑定。"""
    limits = [
        {
            "operations": list(operations),
            "objects": [{"kind": "program_name", "value": name}],
            "reason": "禁止 " + name,
        }
    ]
    previous = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(no_such_path))
    try:
        return _save_limits(ctx, requirement_id, limits, source="test-r9h12-unbound")
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
    """正文读取次数 spy：只观察，不改变工具行为。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def install(self, monkeypatch) -> None:
        real_sha = security_gate.content_sha256

        def spy_read(path):
            self.calls.append(("read", str(path)))
            return "[read-by-r9h12-test-spy]"

        def spy_sha(path):
            self.calls.append(("sha", str(path)))
            return real_sha(path)

        monkeypatch.setattr(fs_tools, "_read_file_text", spy_read)
        monkeypatch.setattr(security_gate, "content_sha256", spy_sha)


def _spy_launch(monkeypatch) -> list[tuple[str, ...]]:
    """子进程启动计数：基线红用例需要证明它真的启动了。"""
    calls: list[tuple[str, ...]] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        calls.append(tuple(str(item) for item in args))
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    return calls


class _ExplodingReviewer:
    """普通授权审查替身：专用恢复一旦调用它就直接失败。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def review(self, request):
        self.calls.append(request)
        raise AssertionError("专用绑定恢复不得调用普通授权审查")


def _read_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path)),),
        purpose="R9-H12 验收读取",
    )


def _execute_effect(path, *, requested: str | None = None) -> Effect:
    return Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(path)),),
        purpose="R9-H12 验收执行",
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


def _assert_not_start_stage(text: str) -> None:
    assert not any(word in str(text or "") for word in START_STAGE_WORDS), text


def _assert_object_words(text: str) -> None:
    assert any(word in str(text or "") for word in OBJECT_WORDS), text


def _same_path(left: object, right: object) -> bool:
    try:
        return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(
            os.path.realpath(str(right))
        )
    except (OSError, ValueError):
        return False


def _override_identity(
    monkeypatch,
    target: Path,
    *,
    unavailable: bool = False,
    controlled=None,
) -> None:
    """模拟身份提供器边界：只把 target 路径的文件事实换成注入值。

    * unavailable=True：该路径身份不可读（identity_of 返回 None）；
    * controlled：{"fail": bool}，可在测试中途切换（模拟保存时读不到、之后可读）。

    文件本体仍真实存在；其余路径返回真实身份。相关用例以「模拟」注明。
    """
    key = os.path.normcase(os.path.realpath(str(target)))
    real_identity_of = file_identity.identity_of

    def same(candidate) -> bool:
        try:
            return os.path.normcase(os.path.realpath(str(candidate))) == key
        except (OSError, ValueError):
            return False

    def fake_identity_of(path, *args, **kwargs):
        if same(path) and (unavailable or (controlled is not None and controlled.get("fail"))):
            return None
        return real_identity_of(path, *args, **kwargs)

    monkeypatch.setattr(file_identity, "identity_of", fake_identity_of)


def _fake_current_candidate_resolver(monkeypatch, program_b: Path) -> list[str]:
    """模拟恢复期的「当前环境候选解析器」：当前 PATH 把名称解析到 B。

    只替换 requirement_limits._probe_program_binding（后端候选事实读取边界），
    返回 B 的完整绑定事实；文件系统/身份仍真实。用来证明恢复不得把「当前环境
    候选」当成保存候选采纳。若实现已不再使用该探测入口，raising=False 保证
    不会因此报错（真实 PATH 仍指向 B）。
    """
    identity = file_identity.identity_of(program_b)
    assert identity is not None, "B 的程序身份应可读"
    candidate = {
        "path": str(program_b),
        "identity": str(identity.key),
        "birth_key": identity.birth_key,
        "source": "path_lookup",
    }
    calls: list[str] = []

    def fake_probe(name):
        calls.append(str(name))
        return {"binding": dict(candidate), "failure": "", "candidates": ()}

    monkeypatch.setattr(
        requirement_limits, "_probe_program_binding", fake_probe, raising=False
    )
    return calls


async def _recover(ctx, requirement_id: str) -> tuple:
    """调用真实专用恢复入口（服务层轮次入口），返回 (recovered, ctx)。"""
    requirement = ctx.requirements.by_id(requirement_id)
    assert requirement is not None
    binding = RequirementBinding(
        requirement_id=requirement_id,
        version=int(requirement.version),
    )
    recovery_ctx = SimpleNamespace(notices=[])
    recovered = await ctx._ensure_legacy_structured_limits(
        recovery_ctx,
        binding,
        message_id=None,
        turn_id="turn-r9h12-binding-recovery",
    )
    return recovered, recovery_ctx


def _raw_limits(ctx, requirement_id: str) -> str:
    row = ctx.conn.execute(
        "SELECT structured_limits FROM user_requirements WHERE id = ?",
        (requirement_id,),
    ).fetchone()
    return "" if row is None else str(row[0] or "")


# ===========================================================================
# P1：未绑定 program_name 的「另一个入口名」（真实硬链接）绕过
# ===========================================================================


async def test_p1_unbound_hardlink_alternate_entry_launches(ctx, tmp_path, monkeypatch):
    """P1：保存无绑定 program_name=execute，真实硬链接 alternate 仍能启动同一程序。

    真实边界：程序是 cmd.exe/sh 的真实副本；alternate 是同一文件的真实硬链接；
    原名称在 PATH 上查找失败（保存时失败、验证时仍失败）。
    """
    program_dir = tmp_path / "p1-bin"
    other_dir = tmp_path / "p1-other-bin"
    program = _copy_program(program_dir, "d12p1runner")
    other_dir.mkdir(parents=True, exist_ok=True)
    alias = other_dir / ("d12p1alt.exe" if WINDOWS else "d12p1alt")
    if not _hardlink(program, alias):
        pytest.skip("平台不支持真实硬链接：另一入口名绕过无法真实验证（如实标注）")

    _mode(ctx)
    requirement = _new_requirement(ctx, "p1")
    _save_unbound_program_limit(
        ctx,
        requirement.requirement_id,
        ["execute"],
        "d12p1runner",
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "p1-empty",
    )
    obj = _first_object(ctx, requirement.requirement_id)
    assert "program_binding" not in obj, "保存时查找失败不得伪造绑定"
    assert obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED, obj
    assert not obj.get("binding_candidates"), (
        "保存时原名称查找失败：不应有保存候选（事实如此，不是猜测）",
        obj,
    )
    # 验证期 PATH 只有 alternate 所在目录：原名称 d12p1runner 查找失败。
    monkeypatch.setenv("PATH", str(other_dir) + os.pathsep + os.environ.get("PATH", ""))
    binding = _binding(ctx, requirement.requirement_id)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )

    # 1) 原名称查找失败时绝对路径已被拒绝（基线已修复的一半，必须保持）。
    root = tmp_path / "p1-root"
    args, marker = _launch_args(root, "abs-marker.txt")
    launches = _spy_launch(monkeypatch)
    with requirement_context(binding):
        abs_result = await tool.run(
            program=str(program), args=args, cwd=str(marker.parent), timeout=30
        )
    assert abs_result.ok is False, abs_result
    assert launches == [], "无绑定限制不得经绝对路径启动程序"
    assert not marker.exists()
    abs_decision = await _authorize(ctx, binding, _execute_effect(program))
    _assert_denied(abs_decision, code=DECISION_UNKNOWN)

    # 2) 真实硬链接暴露的另一入口名：基线会把它当「无关」启动（红）。
    args2, marker2 = _launch_args(root, "alias-marker.txt")
    alt_decision = None
    with requirement_context(binding):
        alt_result = await tool.run(
            program="d12p1alt", args=args2, cwd=str(marker2.parent), timeout=30
        )
    assert alt_result.ok is False, (
        "另一个入口名解析到保存时本应限制的同一程序对象，必须在启动前拒绝",
        alt_result,
    )
    assert marker2.exists() is False, "禁止被另一个入口名经真实硬链接绕过（marker 不应出现）"
    assert not ctx.approvals.pending(), "对象未绑定不是普通批准可以解除的"
    _assert_not_start_stage(alt_result.error or "")
    _assert_object_words(alt_result.error or "")

    alt_decision = await _authorize(
        ctx, binding, _execute_effect(alias, requested="d12p1alt")
    )
    _assert_denied(alt_decision, code=DECISION_UNKNOWN)


# ===========================================================================
# P2：保存候选 A（身份读取失败）不得被当前 PATH 的 B 覆盖
# ===========================================================================


async def test_p2_saved_candidate_not_rebound_to_current_path(ctx, tmp_path, monkeypatch):
    """模拟（身份提供器边界）：保存候选 A 身份不可读，PATH 改到 B 后恢复不得绑定 B。

    真实边界：A/B 都是 cmd.exe 的真实副本；台账/恢复入口/引擎/工具真实；
    「保存时 A 身份不可读」由身份提供器边界注入（本机无法稳定制造文件身份读取
    失败），文件本体真实存在；恢复期「当前候选解析器」fake 指向 B（真实 PATH
    也确实只解析到 B）。
    """
    name = "d12p2runner"
    bin_a = tmp_path / "p2-bin-a"
    bin_b = tmp_path / "p2-bin-b"
    program_a = _copy_program(bin_a, name)
    program_b = _copy_program(bin_b, name)
    assert not _same_path(program_a, program_b)
    identity_a = file_identity.identity_of(program_a)
    identity_b = file_identity.identity_of(program_b)
    assert identity_a is not None and identity_b is not None
    assert identity_a.key != identity_b.key, "测试需要 A/B 是不同文件对象"

    _mode(ctx)
    requirement = _new_requirement(ctx, "p2")
    controlled = {"fail": True}
    _override_identity(monkeypatch, program_a, controlled=controlled)
    previous = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(bin_a) + os.pathsep + previous)
    try:
        _save_limits(
            ctx,
            requirement.requirement_id,
            [
                {
                    "operations": ["read", "execute"],
                    "objects": [{"kind": "program_name", "value": name}],
                    "reason": "禁止读取/运行 " + name,
                }
            ],
            source="test-r9h12-candidate",
        )
    finally:
        controlled["fail"] = False
        monkeypatch.setenv("PATH", str(bin_b) + os.pathsep + previous)

    obj = _first_object(ctx, requirement.requirement_id)
    assert "program_binding" not in obj, obj
    assert obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED, obj
    assert obj.get("binding_failure") == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE, obj
    candidates = obj.get("binding_candidates") or []
    assert any(_same_path(item.get("path"), program_a) for item in candidates), (
        "保存时候选 A 的路径必须作为纯数据保存在限制条目上",
        obj,
    )

    binding = _binding(ctx, requirement.requirement_id)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )

    # 1) 未恢复：读取候选 A 必须失败关闭；旧实现按「当前解析到别处」放行（红）。
    spy = _ReadSpy()
    spy.install(monkeypatch)
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_a = await read_tool.run(path=str(program_a))
    assert read_a.ok is False, (
        "保存候选 A 仍是本限制的相关对象（未恢复），不得读取",
        read_a,
    )
    assert spy.calls == [], "读取限制必须在读取正文/摘要之前生效"

    # 2) 未恢复：执行候选 A 同样必须失败关闭（旧实现按 B 认证后放行 A，红）。
    root = tmp_path / "p2-root"
    args, marker = _launch_args(root, "alias-a-marker.txt")
    launches = _spy_launch(monkeypatch)
    with requirement_context(binding):
        exec_a = await tool.run(
            program=str(program_a), args=args, cwd=str(marker.parent), timeout=30
        )
    assert exec_a.ok is False, exec_a
    assert launches == [], "未恢复的保存候选对象不得被执行"
    assert not marker.exists()
    exec_a_decision = await _authorize(ctx, binding, _execute_effect(program_a))
    _assert_denied(exec_a_decision, code=DECISION_UNBOUND)

    # 3) 真实专用恢复入口 + fake 当前候选解析器（指向 B）：只允许采纳保存候选 A。
    reviewer = _ExplodingReviewer()
    ctx.authorization.reviewer = reviewer
    mode_before = ctx.settings_store.get("computer.permission_mode")
    calls = _fake_current_candidate_resolver(monkeypatch, program_b)
    recovered, recovery_ctx = await _recover(ctx, requirement.requirement_id)
    assert calls, "恢复期应读取后端的当前候选事实（fake resolver 被调用）"
    assert reviewer.calls == [], "专用绑定恢复不得调用普通授权审查"
    assert ctx.settings_store.get("computer.permission_mode") == mode_before, (
        "专用绑定恢复不得改变权限模式"
    )

    assert recovered is not None, (
        "保存候选 A 现在可验证（路径存在 + 身份可读），专用恢复必须采纳它",
        recovery_ctx.notices,
    )
    recovered_obj = [
        item
        for entry in recovered
        for item in (entry.get("objects") or ())
        if isinstance(item, dict) and str(item.get("kind")) == "program_name"
    ][0]
    bound = recovered_obj.get("program_binding")
    assert isinstance(bound, dict), ("恢复后必须写回 program_binding", recovered_obj)
    assert not _same_path(bound.get("path"), program_b), (
        "恢复不得把限制绑定到当前 PATH 的 B（保存时从未采纳 B）",
        bound,
    )
    assert _same_path(bound.get("path"), program_a), (
        "恢复必须采纳保存候选 A 的路径",
        bound,
    )
    assert str(bound.get("identity")) == str(identity_a.key), bound
    assert str(bound.get("source") or "").startswith("saved_candidate"), (
        "恢复来源必须标记为保存候选核验，不能是当前 PATH 查找",
        bound.get("source"),
    )
    raw_paths = _raw_binding_paths(ctx, requirement.requirement_id)
    assert raw_paths, "写回的原始 JSON 必须包含恢复后的 program_binding"
    assert not any(_same_path(path, program_b) for path in raw_paths), (
        "写回的原始 JSON 不得把限制对象指向 B",
        raw_paths,
    )
    assert any(_same_path(path, program_a) for path in raw_paths), raw_paths

    # 4) 恢复后：A 是绑定对象本身，执行/读取都必须停止（旧实现 A 变「无关」可执行，红）。
    binding_after = _binding(ctx, requirement.requirement_id)
    args2, marker2 = _launch_args(root, "after-recovery-marker.txt")
    launches_after = _spy_launch(monkeypatch)
    with requirement_context(binding_after):
        exec_a_after = await tool.run(
            program=str(program_a), args=args2, cwd=str(marker2.parent), timeout=30
        )
    assert exec_a_after.ok is False, exec_a_after
    assert launches_after == [], "恢复后候选 A 是限制对象本身，不得执行"
    assert not marker2.exists()
    stub_read = _ReadSpy()
    stub_read.install(monkeypatch)
    with requirement_context(binding_after):
        read_a_after = await read_tool.run(path=str(program_a))
    assert read_a_after.ok is False, read_a_after
    assert stub_read.calls == []


def _raw_binding_paths(ctx, requirement_id: str) -> list[str]:
    """从台账原始 JSON 读取已写回的 program_binding 路径（纯数据核对）。"""
    import json

    raw = _raw_limits(ctx, requirement_id)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    paths: list[str] = []
    for entry in data if isinstance(data, list) else ():
        if not isinstance(entry, dict):
            continue
        for item in entry.get("objects") or ():
            if not isinstance(item, dict):
                continue
            binding = item.get("program_binding")
            if isinstance(binding, dict) and binding.get("path"):
                paths.append(str(binding["path"]))
    return paths


# ===========================================================================
# P3：限制路径身份不可读时，硬链接读取必须失败关闭
# ===========================================================================


async def test_p3_limit_path_identity_unreadable_alias_read_denied(
    ctx, tmp_path, monkeypatch
):
    """模拟（身份提供器边界）：显式程序路径身份不可读时，读其真实硬链接必须拒绝。

    真实边界：限制路径与硬链接都是真实文件；文本内容真实；读取拒绝发生在读取
    正文之前。身份不可读只注入限制路径这一条事实（本机无法稳定制造），文件仍存在。
    """
    program_dir = tmp_path / "p3-bin"
    body = "d12-p3 original program body\n"
    program = _text_program(program_dir, "d12p3runner", body)
    alias = tmp_path / "d12p3-alias"
    if not _hardlink(program, alias):
        pytest.skip("平台不支持真实硬链接：限制路径身份不可读的别名读取无法真实验证")
    assert alias.exists() and program.exists()

    _mode(ctx)
    requirement = _new_requirement(ctx, "p3")
    _save_limits(
        ctx,
        requirement.requirement_id,
        [
            {
                "operations": ["read"],
                "objects": [{"kind": "program", "value": str(program)}],
                "reason": "禁止读取该程序",
            }
        ],
        source="test-r9h12-p3",
    )
    binding = _binding(ctx, requirement.requirement_id)
    _override_identity(monkeypatch, program, unavailable=True)
    assert file_identity.identity_of(alias) is not None, (
        "候选（硬链接）身份应可读：只有限制路径身份不可读才走目标分支"
    )

    spy = _ReadSpy()
    spy.install(monkeypatch)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        alias_result = await tool.run(path=str(alias))
    assert alias_result.ok is False, (
        "限制路径身份不可读时，硬链接正文不得返回（旧实现判无关并读取，红）",
        alias_result,
    )
    assert spy.calls == [], "拒绝必须发生在读取正文/摘要之前"
    assert alias.read_text(encoding="utf-8") == body

    requirement_now = ctx.requirements.by_id(requirement.requirement_id)
    assessment = ctx.authorization.assess_limit_conflict(
        _read_effect(alias), requirement_now
    )
    assert assessment.conflict is True, [
        (decision.state, decision.basis) for decision in assessment.decisions
    ]
    assert [(decision.state.value, decision.basis) for decision in assessment.decisions] == [
        ("unknown", "path_limit_identity_unavailable")
    ], [(decision.state, decision.basis) for decision in assessment.decisions]
    decision = await _authorize(ctx, binding, _read_effect(alias))
    _assert_denied(decision, code=DECISION_UNKNOWN)


# ===========================================================================
# 规则反例：单独的事实不能变成 ALLOW
# ===========================================================================


async def test_rule_different_entry_name_and_lookup_elsewhere_not_allow(
    ctx, tmp_path, monkeypatch
):
    """不同入口名 / 当前解析到别处 / 名称查找失败，单独都不能变成 ALLOW。"""
    name = "d12rule-runner"
    bin_b = tmp_path / "rule-bin-b"
    program_b = _copy_program(bin_b, name)
    other = _copy_program(bin_b, "d12rule-other")
    bin_a = tmp_path / "rule-bin-a"
    absolute_candidate = _copy_program(bin_a, "d12rule-absolute")
    _mode(ctx)
    requirement = _new_requirement(ctx, "rule")
    _save_unbound_program_limit(
        ctx,
        requirement.requirement_id,
        ["execute"],
        name,
        monkeypatch=monkeypatch,
        no_such_path=tmp_path / "rule-empty",
    )
    binding = _binding(ctx, requirement.requirement_id)
    previous = os.environ.get("PATH", "")

    # 1) 不同入口名（解析到另一个真实程序对象）+ PATH 当前把限制名解析到 B：
    #    旧实现按「入口名不同」判 UNRELATED -> ALLOW（红）；契约要求 UNKNOWN。
    monkeypatch.setenv("PATH", str(bin_b) + os.pathsep + previous)
    decision = await _authorize(
        ctx, binding, _execute_effect(other, requested="d12rule-other")
    )
    _assert_denied(decision, code=DECISION_UNKNOWN)

    # 2) 当前解析到别处：本次候选是另一真实程序（绝对路径，无入口名）。
    #    旧实现按「当前解析到别的对象」判 UNRELATED -> ALLOW（红）；契约要求 UNKNOWN。
    decision = await _authorize(ctx, binding, _execute_effect(absolute_candidate))
    _assert_denied(decision, code=DECISION_UNKNOWN)

    # 3) 原名称查找失败 + 候选存在：旧实现已失败关闭（UNKNOWN），必须保持。
    monkeypatch.setenv("PATH", str(tmp_path / "rule-empty"))
    decision = await _authorize(ctx, binding, _execute_effect(absolute_candidate))
    _assert_denied(decision, code=DECISION_UNKNOWN)


async def test_rule_incomplete_binding_not_allow(ctx, tmp_path, monkeypatch):
    """无完整绑定（program_binding 缺 identity）单独也不能变成 ALLOW。"""
    program = _copy_program(tmp_path / "incomplete-bin", "d12incomplete")
    requirement = SimpleNamespace(
        structured_limits=(
            {
                "operations": ["execute"],
                "objects": [
                    {
                        "kind": "program_name",
                        "value": "d12incomplete",
                        "program_binding": {"path": str(program)},
                    }
                ],
                "reason": "不完整绑定",
            },
        )
    )
    assessment = ctx.authorization.assess_limit_conflict(
        _execute_effect(program), requirement
    )
    assert assessment.conflict is True, "不完整绑定不得按无关放行"
    states = [(decision.state.value, decision.basis) for decision in assessment.decisions]
    assert states and all(state == "unknown" for state, _ in states), states
    assert any(basis == "binding_incomplete" for _, basis in states), states


async def test_rule_reliable_unrelated_still_continues(ctx, tmp_path, monkeypatch):
    """可靠无关必须继续：操作范围外、候选不存在、已绑定不同对象、前缀外。"""
    name = "d12continue-runner"
    bin_dir = tmp_path / "continue-bin"
    text_program = _text_program(bin_dir, name, "continue body\n")
    other_program = _copy_program(bin_dir, "d12continue-other")
    outside_file = tmp_path / "continue-outside.txt"
    outside_file.write_text("outside body\n", encoding="utf-8")
    _mode(ctx)
    _prepend_path(monkeypatch, bin_dir)

    # 1) 操作范围外：只限制 execute，读取同一程序文件仍应允许。
    requirement = _new_requirement(ctx, "continue-scope")
    _save_limits(
        ctx,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": name}],
                "reason": "禁止运行 " + name,
            }
        ],
        source="test-r9h12-continue",
    )
    binding = _binding(ctx, requirement.requirement_id)
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await read_tool.run(path=str(text_program))
    assert read_result.ok is True, read_result
    assert "continue body" in (read_result.content or "")

    # 2) 候选不存在：不存在的目标不可能是被禁止对象。
    decision = await _authorize(
        ctx, binding, _execute_effect(tmp_path / "definitely-not-exists-9f2a")
    )
    assert decision.verdict == Verdict.ALLOW, decision.reason

    # 3) 已绑定不同对象：完整绑定 + 另一个真实程序 -> 按身份比较放行（真实执行）。
    bound_requirement = _new_requirement(ctx, "continue-bound")
    _save_limits(
        ctx,
        bound_requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": name}],
                "reason": "禁止运行 " + name,
            }
        ],
        source="test-r9h12-continue",
    )
    bound_binding = _binding(ctx, bound_requirement.requirement_id)
    bound_obj = _first_object(ctx, bound_requirement.requirement_id)
    assert isinstance(bound_obj.get("program_binding"), dict), bound_obj
    root = tmp_path / "continue-root"
    args, marker = _launch_args(root, "other-marker.txt")
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(bound_binding):
        other_result = await tool.run(
            program="d12continue-other", args=args, cwd=str(marker.parent), timeout=30
        )
    assert other_result.ok is True, other_result
    assert marker.exists(), "已绑定不同对象（另一程序文件）应允许真实执行"
    decision = await _authorize(
        ctx, bound_binding, _execute_effect(other_program, requested="d12continue-other")
    )
    assert decision.verdict == Verdict.ALLOW, decision.reason

    # 4) 前缀外：目录型限制对前缀外的对象是可靠无关。
    prefix_dir = tmp_path / "continue-prefix"
    prefix_dir.mkdir()
    prefix_requirement = _new_requirement(ctx, "continue-prefix")
    _save_limits(
        ctx,
        prefix_requirement.requirement_id,
        [
            {
                "operations": ["read"],
                "objects": [{"kind": "path_prefix", "value": str(prefix_dir)}],
                "reason": "禁止读取该目录",
            }
        ],
        source="test-r9h12-continue",
    )
    prefix_binding = _binding(ctx, prefix_requirement.requirement_id)
    with requirement_context(prefix_binding):
        outside_read = await read_tool.run(path=str(outside_file))
    assert outside_read.ok is True, outside_read
    assert "outside body" in (outside_read.content or "")
    decision = await _authorize(ctx, prefix_binding, _read_effect(outside_file))
    assert decision.verdict == Verdict.ALLOW, decision.reason


async def test_rule_bound_entry_resolves_to_other_object_allowed(
    ctx, tmp_path, monkeypatch
):
    """已绑定 program_name + 不同入口名解析到可核验的另一个对象 -> 可靠无关。"""
    name = "d12bound-runner"
    bin_dir = tmp_path / "bound-entry-bin"
    _copy_program(bin_dir, name)
    other_program = _copy_program(bin_dir, "d12bound-other")
    _mode(ctx)
    _prepend_path(monkeypatch, bin_dir)
    requirement = _new_requirement(ctx, "bound-entry")
    _save_limits(
        ctx,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": name}],
                "reason": "禁止运行 " + name,
            }
        ],
        source="test-r9h12-bound-entry",
    )
    binding = _binding(ctx, requirement.requirement_id)
    bound_obj = _first_object(ctx, requirement.requirement_id)
    assert isinstance(bound_obj.get("program_binding"), dict), bound_obj

    root = tmp_path / "bound-entry-root"
    args, marker = _launch_args(root, "bound-other-marker.txt")
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program="d12bound-other", args=args, cwd=str(marker.parent), timeout=30
        )
    assert result.ok is True, result
    assert marker.exists(), "已绑定对象与本次入口解析对象身份不同：可靠无关，应允许"
    decision = await _authorize(
        ctx, binding, _execute_effect(other_program, requested="d12bound-other")
    )
    assert decision.verdict == Verdict.ALLOW, decision.reason
