"""R9-H12 C：真实工具/引擎/台账/审批/临时文件的跨操作一致性验收（契约 §5）。

本文件只用 **真实** 引擎（AuthorizationEngine + security/object_state 五态判定）、
真实工具（fs_read/fs_write/fs_patch/fs_delete/run_program）、真实要求台账、真实审批
服务、真实临时文件与真实硬链接；模型一律 fake（本文件不调用任何模型）。

「真实 vs 模拟」边界（按契约要求如实标注）：
* 真实：文件对象、硬链接（os.link 后断言 samefile）、文件身份（volume+file_id+
  birth_key）、PATH 查找、可执行程序启动（临时目录内复制系统 cmd/sh 的副本，
  用 marker 文件证明「真的启动了」）、审批状态机与后台执行器、SQLite 台账。
* 模拟（仅在**文件身份提供器边界**注入，不伪造判定结论）：把
  agent.services.file_identity.identity_of 对**指定路径**返回 None，用来复现
  「对象存在但身份不可读」的系统边界。判定仍由真实 object_state 逻辑完成。
* 模拟（历史数据形态）：用 SQL 写入「state=structured 但保存时未形成对象绑定」
  的历史限制（与 H11 旧用例同一手法），复现既有库里的未绑定限制，不改判定逻辑。

依赖标注（基线会红、对应分支合入后转绿；不 skip、不放宽断言）。首次提交
5938b9a4 时只有 C 适配层：P1/P3/P2/审批/重核对真实链路用例为基线红；A 的
object_state（§3.1/§3.2）与 B 的恢复入口（§4）合入 r9h12/main 后全部转绿，
当前分支全绿（证据见交付报告与提交记录）。
* P1（未绑定 execute + 硬链接别名 + 原名称查找失败 → 别名实际不启动）与
  P3（path 限制 + 限制路径身份不可读 → 读硬链接被拒）的**结论**来自安全分支 A 的
  object_state §3.2 / §3.1（UNRELATED → UNBOUND/UNKNOWN）。C 侧只消费同一
  assessment，不复制判定。
* P2 的「服务层恢复尝试不得把当前 PATH 的 B 静默绑定」来自 B 的恢复入口
  recover_program_bindings（§4）；C 侧验收「恢复尝试后 A 仍受保护、B 未被绑定」。
* 旧期望修订：本文件不含旧断言；H11 里「入口名不同/当前解析到别处 = 无关」的旧
  守卫已在 A/D 的范围里改为 UNKNOWN/DENY，本文件按冻结契约写新期望。
* 已知边界（如实标注，不掩盖）：execute 入口在判定前会采集「程序内容摘要」作为
  启动事实（H5/H7 的 launch facts，审查/审批/执行前重核对共用同一份）。因此对
  execute 入口，本文件按契约 §5 验收「程序 0 次启动 + 不读取/打开任何正文
  （open/read_bytes/read_text 为 0）」；程序内容 sha256 属启动事实，不当作正文
  读取。若要把它也后置到 assessment 之后，需要重构 cmd_tools 的 launch facts
  采集时机（并保证 effect/审批指纹使用同一份事实），不在 C 适配层最小改动内。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import launch as launch_boundary
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target
from agent.security.operations import ExecutionContext as _ExecutionContext
from agent.security.recheck import OperationBinding, RecheckService
from agent.security.requirements import RequirementBinding
from agent.services import file_identity
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsReadTool, FsWriteTool

#: 冻结契约 §2/§3 的决策码（与 effects.py / object_state.decision_code_for 一致）。
UNBOUND_CODE = "program_name_requires_object_binding"
INVALID_CODE = "program_binding_invalidated"
UNKNOWN_CODE = "object_state_unconfirmed"

#: 真实可执行程序（启动用例的控制组/反证用）。没有它就无法证明「本可启动」。
_LAUNCH_SOURCE = (
    os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    if os.name == "nt"
    else (shutil.which("sh") or "/bin/sh")
)


# --------------------------------------------------------------------------
# 夹具与辅助（全部真实对象；身份注入处显式标注「模拟」）
# --------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _launch_program(directory: Path, name: str) -> Path:
    """真实可执行程序副本（同卷、可启动）：复制系统 shell 的副本。"""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name += ".exe"
    target = directory / name
    shutil.copy2(_LAUNCH_SOURCE, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _text_program(directory: Path, name: str, text: str = "#!/bin/fake program\n") -> Path:
    """普通文本文件（当程序名候选/被读对象用；内容可读，便于断言读取次数）。"""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name += ".exe"
    target = directory / name
    target.write_text(text, encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o755)  # PATH 查找要求可执行位（POSIX）
    return target


def _hardlink(source: Path, link: Path) -> Path:
    """真实硬链接（同卷）；断言确实是同一个对象。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, link)
    assert os.path.samefile(source, link), "测试前提：硬链接必须指向同一对象"
    return link


def _marker_command(marker: Path) -> list[str]:
    """让真实程序写 marker 的参数（POSIX/Windows 各自一套）。"""
    if os.name == "nt":
        return ["/c", "echo ran>%s" % marker]
    return ["-c", "printf ran > %s" % marker]


def _install_launch_counter(monkeypatch) -> list[dict]:
    """真实启动计数：包装受控启动器（真的会启动；只记录每次 launch 尝试）。"""
    calls: list[dict] = []
    real_launch = launch_boundary.ControlledLauncher.launch

    async def spy(self, *args, **kwargs):
        spec = args[0] if args else kwargs.get("spec")
        calls.append(
            {
                "program": str(getattr(spec, "program", "")),
                "argv": [str(item) for item in tuple(getattr(spec, "argv", ()) or ())],
            }
        )
        return await real_launch(self, *args, **kwargs)

    monkeypatch.setattr(launch_boundary.ControlledLauncher, "launch", spy)
    return calls


def _install_read_spies(monkeypatch) -> list[str]:
    """正文/摘要读取计数（真实读取仍发生；只记录）。"""
    sink: list[str] = []
    real_bytes = Path.read_bytes
    real_text = Path.read_text
    real_open_text = fs_tools._read_file_text
    real_sha = security_gate.content_sha256

    def spy_bytes(self, *args, **kwargs):
        sink.append("read_bytes:" + str(self))
        return real_bytes(self, *args, **kwargs)

    def spy_text(self, *args, **kwargs):
        sink.append("read_text:" + str(self))
        return real_text(self, *args, **kwargs)

    def spy_open_text(path):
        sink.append("open:" + str(path))
        return real_open_text(path)

    def spy_content_sha(path):
        sink.append("sha256:" + str(path))
        return real_sha(path)

    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(fs_tools, "_read_file_text", spy_open_text)
    monkeypatch.setattr(security_gate, "content_sha256", spy_content_sha)
    return sink


def _sandbox(root: Path):
    """旧接线用的 ComputerSandbox（真实路径解析/敏感路径判定）。"""
    from agent.services.computer import ComputerSandbox

    root.mkdir(parents=True, exist_ok=True)
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "unrestricted"
    )


def _fail_identity_for(monkeypatch, paths) -> set[str]:
    """【模拟】文件身份提供器边界：指定路径「存在但身份不可读」（返回 None）。

    只注入系统边界失败，不改变任何判定状态；五态判定仍由真实 object_state 计算。
    返回可变的注入集合：调用方 discard(path) 即恢复该路径身份可读（模拟「当时不可读、
    之后可读」的时间差）。
    """
    real = file_identity.identity_of
    wanted = {str(path) for path in paths}

    def wrapped(path):
        if str(path) in wanted:
            return None
        return real(path)

    monkeypatch.setattr(file_identity, "identity_of", wrapped)
    return wanted


def _write_raw_limits(db_conn, requirement_id: str, limits) -> None:
    """模拟历史数据：state=structured 但保存时未形成对象绑定（与 H11 同一手法）。"""
    db_conn.execute(
        "UPDATE user_requirements SET structured_limits = ?,"
        " structured_limits_state = 'structured' WHERE id = ?",
        (json.dumps(list(limits), ensure_ascii=False), str(requirement_id)),
    )
    db_conn.commit()


def _save_limits(ctx, *, text: str, operations, objects, tag: str):
    requirement = ctx.requirements.record_user_message(
        text=text, message_id="m-" + tag, turn_id="t-" + tag
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": [str(item) for item in operations],
                "objects": [dict(item) for item in objects],
                "reason": text,
            }
        ],
        source="test-r9h12-c:" + tag,
        state=requirement_limits.STATE_STRUCTURED,
    )
    return requirement, ctx.requirements.binding_for(requirement.requirement_id)


_STRUCTURED_CODES = (UNBOUND_CODE, INVALID_CODE, UNKNOWN_CODE)


def _decision_code(result) -> str | None:
    """结构化决策码：顶层 data.decision_code，或批量结果里逐项的 code。"""
    data = getattr(result, "data", None) or {}
    code = str(data.get("decision_code") or "")
    if code in _STRUCTURED_CODES:
        return code
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("decision_code") or item.get("code") or "")
        if candidate in _STRUCTURED_CODES:
            return candidate
    return code or None


def _states(result) -> list[str]:
    data = getattr(result, "data", None) or {}
    decisions = data.get("object_decisions") or []
    return [str(item.get("state")) for item in decisions if isinstance(item, dict)]


def _bases(result) -> list[str]:
    data = getattr(result, "data", None) or {}
    decisions = data.get("object_decisions") or []
    return [
        str(item.get("basis"))
        for item in decisions
        if isinstance(item, dict) and item.get("basis")
    ]


async def _wait_state(ctx, approval_id: str, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = ctx.approvals.store.get(approval_id)
        if row and str(row.get("state")) in ("completed", "failed", "invalidated"):
            return row
        await asyncio.sleep(0.03)
    return row


async def _approval_request(ctx, tool, **kwargs) -> str:
    """真实审批通道：提交待审批操作（工具返回 pending，不阻塞本轮）。"""
    result = await tool.run(**kwargs)
    data = dict(getattr(result, "data", None) or {})
    assert data.get("pending_approval") is True, result
    approval_id = str(data.get("approval_id") or "")
    assert approval_id, result
    return approval_id


def _spy_limit_conflict_result(monkeypatch) -> list[dict]:
    """记录每次消费 assessment 的结论（适配层入口 + 引擎内部同一判定）。

    * 适配层：security_gate.limit_conflict_result（工具读/写/删/执行前）；
    * 引擎内部：AuthorizationEngine.assess_limit_conflict（批量删除经
      authorize_item → engine.authorize 的同一判定；结论必须一致）。
    """
    from agent.security.engine import AuthorizationEngine

    calls: list[dict] = []
    real = security_gate.limit_conflict_result
    real_assess = AuthorizationEngine.assess_limit_conflict

    def _kind(effect) -> str:
        return str(
            getattr(
                getattr(effect, "kind", None), "value", getattr(effect, "kind", "")
            )
        )

    def spy(gateway, effect, **kwargs):
        result = real(gateway, effect, **kwargs)
        data = dict(getattr(result, "data", None) or {}) if result is not None else {}
        calls.append(
            {
                "source": "adapter",
                "effect_kind": _kind(effect),
                "denied": result is not None,
                "code": str(getattr(result, "code", "") or "") if result is not None else "",
                "decision_code": str(data.get("decision_code") or ""),
                "states": [
                    str(item.get("state"))
                    for item in (data.get("object_decisions") or [])
                    if isinstance(item, dict)
                ],
            }
        )
        return result

    def spy_assess(self, effect, requirement=None, *, extra_limits=()):
        raw = real_assess(self, effect, requirement, extra_limits=extra_limits)
        calls.append(
            {
                "source": "engine",
                "effect_kind": _kind(effect),
                "denied": bool(getattr(raw, "conflict", False)),
                "code": "",
                "decision_code": str(getattr(raw, "decision_code", "") or ""),
                "states": [
                    str(getattr(item.state, "value", item.state))
                    for item in (getattr(raw, "decisions", ()) or ())
                ],
            }
        )
        return raw

    monkeypatch.setattr(security_gate, "limit_conflict_result", spy)
    monkeypatch.setattr(
        AuthorizationEngine, "assess_limit_conflict", spy_assess
    )
    return calls


# --------------------------------------------------------------------------
# 适配层消费 assessment 的边界（真实工具；冻结接口异常/空返回 → 失败关闭）
# --------------------------------------------------------------------------


class _NoneAssessmentGateway:
    """冻结接口返回 None（网关缺陷/半接线）：不得被当成「没有限制」。"""

    def mode(self):
        return "unrestricted"

    async def authorize(self, effect, *, ctx=None):  # pragma: no cover - 不应到达
        raise AssertionError("assessment 返回 None 时必须失败关闭，不得进入普通授权")

    def assess_limit_conflict(self, effect, requirement=None, *, extra_limits=()):
        return None


async def test_none_assessment_fails_closed_zero_reads(tmp_path, monkeypatch):
    """assess_limit_conflict 返回 None → 失败关闭（0 读取），不是默认放行。"""
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "note.txt"
    target.write_text("正文", encoding="utf-8")
    reads = _install_read_spies(monkeypatch)
    tool = FsReadTool(computer=_sandbox(root), security=_NoneAssessmentGateway())
    result = await tool.run(path=str(target))
    assert result.ok is False, result
    assert _decision_code(result) == UNKNOWN_CODE, result
    assert reads == []
    # 写/删同样不得在被破坏的 assessment 下继续（同一结论）。
    write_result = await FsWriteTool(
        computer=_sandbox(root), security=_NoneAssessmentGateway()
    ).run(path=str(target), content="改写")
    assert write_result.ok is False and _decision_code(write_result) == UNKNOWN_CODE
    delete_result = await FsDeleteTool(
        computer=_sandbox(root), security=_NoneAssessmentGateway()
    ).run(paths=[str(target)])
    assert delete_result.ok is False and _decision_code(delete_result) == UNKNOWN_CODE
    assert target.read_text(encoding="utf-8") == "正文"


async def test_patch_denied_before_reading_body_when_identity_unreadable(
    ctx, tmp_path, monkeypatch
):
    """补丁前置内容：限制路径身份不可读（模拟）→ 补丁在读正文前被拒（0 读取）。"""
    source = _text_program(tmp_path / "patch-src", "prog", "old-value\n")
    alias = _hardlink(
        source,
        tmp_path / "patch-alias" / ("alias.exe" if os.name == "nt" else "alias"),
    )
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement, binding = _save_limits(
        ctx,
        text="禁止读取这个程序文件",
        operations=["read"],
        objects=[{"kind": "path", "value": str(source)}],
        tag="patch-read",
    )
    _fail_identity_for(monkeypatch, [source])
    reads = _install_read_spies(monkeypatch)
    with requirement_context(binding):
        result = await FsPatchTool(
            computer=ctx.computer, security=ctx.authorization
        ).run(path=str(alias), old="old-value", new="new-value")
    assert result.ok is False, result
    assert _decision_code(result) == UNKNOWN_CODE, result
    assert reads == [], "补丁不得先读正文再拒绝"
    assert source.read_text(encoding="utf-8") == "old-value\n"


# --------------------------------------------------------------------------
# P1：未绑定 execute 限制 + 真实硬链接别名 + 原名称查找失败
#     → 别名实际不启动（marker + 真实启动计数都为 0；不只看错误文字）
# --------------------------------------------------------------------------


def _p1_objects(tmp_path: Path):
    """真实可执行 runner + 它的真实硬链接别名 alternate（不同目录/不同名称）。"""
    program_dir = tmp_path / "p1-bin"
    runner = _launch_program(program_dir, "runner")
    alt_dir = tmp_path / "p1-alt"
    alternate = _hardlink(runner, alt_dir / ("alternate.exe" if os.name == "nt" else "alternate"))
    return program_dir, runner, alt_dir, alternate


def _p1_path(monkeypatch, alt_dir: Path) -> None:
    """PATH 只含别名目录：原名称 runner 当前查找失败（真实 PATH 查找）。"""
    monkeypatch.setenv("PATH", str(alt_dir) + os.pathsep + os.environ.get("PATH", ""))


async def test_p1_control_alias_launches_without_the_limit(ctx, tmp_path, monkeypatch):
    """控制组（反证）：没有该限制时同一别名确实能启动并写 marker。

    这条用例证明后面「没启动」不是因为程序坏了或工具链不工作。
    """
    program_dir, runner, alt_dir, alternate = _p1_objects(tmp_path)
    _p1_path(monkeypatch, alt_dir)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="运行 alternate 生成一个结果文件", message_id="m-p1-ctl", turn_id="t-p1-ctl"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    marker = Path(ctx.computer.root()) / "p1-control-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program="alternate",
            args=_marker_command(marker),
            cwd=str(ctx.computer.root()),
            timeout=30,
        )
    assert result.ok is True, result
    assert marker.exists(), "控制组必须真的启动程序并写 marker"
    assert len(launches) == 1, launches


async def test_p1_unbound_execute_hardlink_alias_not_launched(ctx, db_conn, tmp_path, monkeypatch):
    """P1 核心：未绑定 execute 限制，别名入口必须被拒且**零启动**（基线红）。"""
    program_dir, runner, alt_dir, alternate = _p1_objects(tmp_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-p1", turn_id="t-p1"
    )
    # 历史数据：明确禁止 execute runner，但保存时没有形成对象绑定。
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止运行 runner",
            }
        ],
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    # 原名称查找失败：PATH 不含 runner 目录（只含别名目录）。
    _p1_path(monkeypatch, alt_dir)
    marker = Path(ctx.computer.root()) / "p1-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    args = _marker_command(marker)
    results = {}
    with requirement_context(binding):
        results["by_name"] = await tool.run(
            program="alternate", args=args, cwd=str(ctx.computer.root()), timeout=30
        )
        results["absolute_alias"] = await tool.run(
            program=str(alternate), args=args, cwd=str(ctx.computer.root()), timeout=30
        )
        results["absolute_original"] = await tool.run(
            program=str(runner), args=args, cwd=str(ctx.computer.root()), timeout=30
        )
    for label, result in results.items():
        assert result.ok is False, (label, result)
        # 事实不足（原名称查不到、别名无法排除）→ UNKNOWN，失败关闭。
        assert _decision_code(result) == UNKNOWN_CODE, (label, result)
        assert _states(result) and set(_states(result)) == {"unknown"}, (label, _states(result))
    assert launches == [], "被拒绝的执行不得尝试启动任何程序"
    assert not marker.exists(), "别名入口实际启动了程序（marker 出现）"


async def test_p1_unbound_execute_limit_is_not_loosened_by_normal_allow(
    ctx, db_conn, tmp_path, monkeypatch
):
    """P1 补充：unrestricted 模式（普通放行）不能解除未绑定限制。"""
    program_dir, runner, alt_dir, alternate = _p1_objects(tmp_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-p1-mode", turn_id="t-p1-mode"
    )
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止运行 runner",
            }
        ],
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    _p1_path(monkeypatch, alt_dir)
    marker = Path(ctx.computer.root()) / "p1-mode-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program="alternate",
            args=_marker_command(marker),
            cwd=str(ctx.computer.root()),
            timeout=30,
        )
    assert result.ok is False, result
    assert _decision_code(result) == UNKNOWN_CODE, result
    assert launches == [] and not marker.exists(), "普通放行不得解除未绑定限制"


async def test_p1_reliable_unrelated_cases_continue(ctx, db_conn, tmp_path, monkeypatch):
    """P1 可靠无关仍继续：操作范围外（读取）与候选不存在（不存在的程序路径）。"""
    program_dir, runner, alt_dir, alternate = _p1_objects(tmp_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-p1-unrel", turn_id="t-p1-unrel"
    )
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止运行 runner",
            }
        ],
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    _p1_path(monkeypatch, alt_dir)
    note = Path(ctx.computer.root()) / "p1-note.txt"
    note.write_text("无关正文", encoding="utf-8")
    launches = _install_launch_counter(monkeypatch)
    # (a) 操作范围外：execute 限制不得阻止无关文件读取（operation_out_of_scope）。
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await read_tool.run(path=str(note))
    assert read_result.ok is True, read_result
    assert read_result.content == "无关正文"
    # (b) 候选不存在：程序路径真实不存在 → candidate_not_exists（可靠无关），
    #     后续按普通启动失败如实汇报，不是限制拒绝。
    missing = tmp_path / "p1-missing-program.exe"
    assert not missing.exists()
    run_tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        # 不给 cwd：影响对象只有「真实不存在的程序路径」这一个候选。
        missing_result = await run_tool.run(
            program=str(missing),
            args=_marker_command(Path(ctx.computer.root()) / "p1-missing-marker.txt"),
            timeout=30,
        )
    assert missing_result.ok is False, missing_result
    assert _decision_code(missing_result) is None, missing_result
    assert missing_result.category == "startup", missing_result


async def test_p1_tool_denial_does_not_trigger_binding_recovery(
    ctx, db_conn, tmp_path, monkeypatch
):
    """工具不自动触发恢复：被拒后历史限制保持原样（不补绑定、不改版本）。"""
    program_dir, runner, alt_dir, alternate = _p1_objects(tmp_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-p1-norec", turn_id="t-p1-norec"
    )
    raw_limits = [
        {
            "operations": ["execute"],
            "objects": [{"kind": "program_name", "value": "runner"}],
            "reason": "禁止运行 runner",
        }
    ]
    _write_raw_limits(db_conn, requirement.requirement_id, raw_limits)
    before = ctx.requirements.by_id(requirement.requirement_id)
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    _p1_path(monkeypatch, alt_dir)
    marker = Path(ctx.computer.root()) / "p1-norec-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        result = await tool.run(
            program="alternate",
            args=_marker_command(marker),
            cwd=str(ctx.computer.root()),
            timeout=30,
        )
    assert result.ok is False, result
    assert launches == [] and not marker.exists()
    after = ctx.requirements.by_id(requirement.requirement_id)
    assert json.loads(json.dumps(before.structured_limits, default=str)) == json.loads(
        json.dumps(after.structured_limits, default=str)
    ), "工具/适配层不得在拒绝路径上写入绑定或改写限制"
    assert int(after.version) == int(before.version)
    stored_object = after.structured_limits[0]["objects"][0]
    assert not stored_object.get("program_binding"), "工具不得自动为历史限制补建绑定"


# --------------------------------------------------------------------------
# P2：未绑定 + 保存候选 A（保存时身份不可读）+ PATH 改到不同对象 B
#     → 读 A 被拒（0 读取）；服务层恢复尝试后 A 仍受保护、B 不被静默绑定
# --------------------------------------------------------------------------


def _p2_objects(tmp_path: Path):
    """两个真实同名程序（不同对象）：A 是保存候选，B 是之后 PATH 解析到的对象。"""
    a_dir = tmp_path / "p2-a"
    a = _text_program(a_dir, "runner", "# program A（保存候选）\n")
    b_dir = tmp_path / "p2-b"
    b = _text_program(b_dir, "runner", "# program B（后来 PATH 解析到的对象）\n")
    return a_dir, a, b_dir, b


def _p2_saved_unbound(ctx, *, a_dir: Path, tag: str):
    """保存时身份不可读 → binding_state=unresolved + binding_candidates 记 A。"""
    base_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(a_dir) + os.pathsep + base_path
    try:
        requirement = ctx.requirements.record_user_message(
            text="禁止读取 runner", message_id="m-" + tag, turn_id="t-" + tag
        )
        ctx.requirements.set_structured_limits(
            requirement.requirement_id,
            [
                {
                    "operations": ["read"],
                    "objects": [{"kind": "program_name", "value": "runner"}],
                    "reason": "禁止读取 runner",
                }
            ],
            source="test-r9h12-c:" + tag,
            state=requirement_limits.STATE_STRUCTURED,
        )
    finally:
        os.environ["PATH"] = base_path
    stored = ctx.requirements.by_id(requirement.requirement_id)
    return requirement, ctx.requirements.binding_for(requirement.requirement_id), stored


async def test_p2_unbound_saved_candidate_read_denied_zero_reads(
    ctx, tmp_path, monkeypatch
):
    """P2：保存候选 A 当时身份不可读、之后可读 + PATH 改到 B → 读 A 必须被拒。

    基线（A 的 §3.2 未合入）：候选 A 与当前解析对象 B 身份不同 → UNRELATED → 放行。
    """
    a_dir, a, b_dir, b = _p2_objects(tmp_path)
    base_path = os.environ.get("PATH", "")
    wanted = _fail_identity_for(monkeypatch, [a])
    requirement, binding, stored = _p2_saved_unbound(ctx, a_dir=a_dir, tag="p2")
    object_state = stored.structured_limits[0]["objects"][0]
    assert not object_state.get("program_binding"), object_state
    assert str(object_state.get("binding_state")) == "unresolved", object_state
    candidates = object_state.get("binding_candidates") or []
    assert any(str(item.get("path")) == str(a) for item in candidates), candidates
    # 时间差：保存之后 A 的身份恢复可读；PATH 改为解析到 B（不同的真实对象）。
    wanted.discard(str(a))
    monkeypatch.setenv("PATH", str(b_dir) + os.pathsep + base_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    reads = _install_read_spies(monkeypatch)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(a))
    assert result.ok is False, result
    assert _decision_code(result) == UNBOUND_CODE, result
    assert "unbound" in _states(result), _states(result)
    assert reads == [], "被拒前不得读取候选 A 的正文"


async def test_p2_service_recovery_does_not_bind_current_path_target(
    ctx, db_conn, tmp_path, monkeypatch
):
    """P2 恢复：服务层轮次入口尝试恢复后，A 仍受保护、B 不被静默绑定。"""
    a_dir, a, b_dir, b = _p2_objects(tmp_path)
    base_path = os.environ.get("PATH", "")
    wanted = _fail_identity_for(monkeypatch, [a])
    requirement, binding, stored = _p2_saved_unbound(ctx, a_dir=a_dir, tag="p2-rec")
    # 保存候选 A 现在身份可读；当前 PATH 已改到 B（另一个真实对象）。
    wanted.discard(str(a))
    monkeypatch.setenv("PATH", str(b_dir) + os.pathsep + base_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    assert requirement_limits.needs_binding_recovery(stored.structured_limits)
    recovered = await ctx._ensure_legacy_structured_limits(
        ctx, binding, message_id="m-p2-rec-recover", turn_id="turn-p2-rec"
    )
    after = ctx.requirements.by_id(requirement.requirement_id)
    object_state = after.structured_limits[0]["objects"][0]
    bound = object_state.get("program_binding") or {}
    bound_path = str(bound.get("path") or "")
    assert bound_path != str(b), (
        "恢复不得用当前 PATH 查找结果把历史限制静默绑定到 B："
        + json.dumps(object_state, ensure_ascii=False, default=str)
    )
    # 恢复若真正采纳对象，只能是后端核验过的保存候选 A（saved_candidate_verified）。
    if bound_path:
        assert bound_path == str(a), object_state
        assert "saved_candidate" in str(bound.get("source") or ""), bound
        assert str(object_state.get("binding_state") or "") != "unresolved", object_state
    # 无论是否采纳：原限制不得被清空（保留 reason/operations），A 仍受保护。
    kept = after.structured_limits[0]
    assert str(kept.get("reason") or "").strip(), kept
    assert list(kept.get("operations") or []) == ["read"], kept
    reads = _install_read_spies(monkeypatch)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(ctx.requirements.binding_for(requirement.requirement_id)):
        protected = await tool.run(path=str(a))
    assert protected.ok is False, protected
    assert reads == [], "恢复尝试后 A 仍必须受保护（零读取）"
    # 恢复结果不是普通授权：不产生长期授权记录、不改权限模式。
    assert ctx.settings_store.get("computer.permission_mode") == "unrestricted"
    if recovered is not None:
        # 本轮重新取到的限制必须是纯数据（不改变契约语义）。
        assert all(isinstance(item, dict) for item in recovered)


async def test_p2_recovery_keeps_unverified_saved_candidate_unbound(
    ctx, tmp_path, monkeypatch
):
    """P2 变体：保存候选 A 身份始终不可读 → 恢复不得写绑定（尤其不得写 B）。"""
    a_dir, a, b_dir, b = _p2_objects(tmp_path)
    base_path = os.environ.get("PATH", "")
    _fail_identity_for(monkeypatch, [a])  # 保持不可读
    requirement, binding, stored = _p2_saved_unbound(ctx, a_dir=a_dir, tag="p2-keep")
    monkeypatch.setenv("PATH", str(b_dir) + os.pathsep + base_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    before = ctx.requirements.by_id(requirement.requirement_id)
    await ctx._ensure_legacy_structured_limits(
        ctx, binding, message_id="m-p2-keep-recover", turn_id="turn-p2-keep"
    )
    after = ctx.requirements.by_id(requirement.requirement_id)
    object_state = after.structured_limits[0]["objects"][0]
    assert not object_state.get("program_binding"), (
        "保存候选都无法核验时不得写绑定（尤其不得写当前 PATH 的 B）："
        + json.dumps(object_state, ensure_ascii=False, default=str)
    )
    assert json.loads(json.dumps(before.structured_limits, default=str)) == json.loads(
        json.dumps(after.structured_limits, default=str)
    ), "恢复失败不得改写原限制/候选"
    reads = _install_read_spies(monkeypatch)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(a))
    assert result.ok is False, result
    # 候选路径就是保存候选 A：这是「需恢复的绑定缺失」UNBOUND（不是无关、也不是可放行）。
    assert _decision_code(result) == UNBOUND_CODE, result
    assert "unbound" in _states(result), _states(result)
    assert reads == [], "不可读候选的读取必须失败关闭且零读取"


# --------------------------------------------------------------------------
# P3：显式程序路径 read 限制 + 真实硬链接 + 限制路径身份不可读
#     → 读硬链接被拒（0 读取）；正常不同对象/复制件继续
# --------------------------------------------------------------------------


def _p3_objects(tmp_path: Path):
    src = tmp_path / "p3-src"
    prog = _text_program(src, "prog", "原始程序正文\n")
    link_dir = tmp_path / "p3-link"
    link = _hardlink(prog, link_dir / ("alias.exe" if os.name == "nt" else "alias"))
    return prog, link


async def test_p3_path_limit_identity_unreadable_blocks_hardlink_read(
    ctx, tmp_path, monkeypatch
):
    """P3 核心：限制路径身份不可读（模拟身份提供器边界）→ 读硬链接必须被拒。

    基线（A 的 §3.1 未合入）：target_observed is None → UNRELATED → 放行并读取。
    """
    prog, link = _p3_objects(tmp_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement, binding = _save_limits(
        ctx,
        text="禁止读取这个程序文件",
        operations=["read"],
        objects=[{"kind": "path", "value": str(prog)}],
        tag="p3",
    )
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    # 正常（两侧身份都可读）：硬链接是同一对象 → MATCH 拒绝，零读取。
    reads = _install_read_spies(monkeypatch)
    with requirement_context(binding):
        normal = await tool.run(path=str(link))
    assert normal.ok is False, normal
    assert "match" in _states(normal), _states(normal)
    assert reads == [], "MATCH 判定必须在读取正文前拒绝"
    # 【模拟】限制路径身份不可读（对象存在，系统边界读取失败）。
    _fail_identity_for(monkeypatch, [prog])
    reads.clear()
    with requirement_context(binding):
        broken = await tool.run(path=str(link))
    assert broken.ok is False, broken
    assert _decision_code(broken) == UNKNOWN_CODE, broken
    assert "unknown" in _states(broken), _states(broken)
    assert reads == [], "限制路径身份不可读时不得读取硬链接正文"


async def test_p3_different_object_and_copy_continue(ctx, tmp_path, monkeypatch):
    """P3 可靠无关仍继续：身份都可读时的不同对象/复制件不阻塞。"""
    prog, link = _p3_objects(tmp_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement, binding = _save_limits(
        ctx,
        text="禁止读取这个程序文件",
        operations=["read"],
        objects=[{"kind": "path", "value": str(prog)}],
        tag="p3-rel",
    )
    copy_dir = tmp_path / "p3-copy"
    copy = copy_dir / ("copy.exe" if os.name == "nt" else "copy")
    copy_dir.mkdir(parents=True, exist_ok=True)
    copy.write_text("原始程序正文\n", encoding="utf-8")  # 内容相同、对象不同
    other = tmp_path / "p3-other.txt"
    other.write_text("无关正文", encoding="utf-8")
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        original = await tool.run(path=str(prog))
        copied = await tool.run(path=str(copy))
        unrelated = await tool.run(path=str(other))
        hardlink = await tool.run(path=str(link))
    assert original.ok is False and "match" in _states(original), original
    # 不同身份的对象（复制件）是可靠无关：按模式继续读取。
    assert copied.ok is True, copied
    assert copied.content == "原始程序正文\n"
    assert unrelated.ok is True and unrelated.content == "无关正文", unrelated
    # 硬链接仍是同一对象：拒绝（不是按名称相似，而是真实文件身份）。
    assert hardlink.ok is False and "match" in _states(hardlink), hardlink


# --------------------------------------------------------------------------
# 审批恢复：UNBOUND/UNKNOWN 普通批准不能解除；执行器/重核对消费同一 assessment
# --------------------------------------------------------------------------


def _spy_authorize_started(monkeypatch) -> list[dict]:
    """真实审批执行器的重核对入口计数（结论/结构化决策码都记下来）。"""
    calls: list[dict] = []
    real = _ExecutionContext.authorize_started

    async def spy(self, effect=None, **kwargs):
        allowed, reason = await real(self, effect, **kwargs)
        calls.append(
            {
                "allowed": bool(allowed),
                "limit_decision_code": str(
                    getattr(self, "limit_decision_code", "") or ""
                ),
                "basis_kind": str(
                    (getattr(self, "authorization_basis", {}) or {}).get("kind") or ""
                ),
                "reason": str(reason or ""),
            }
        )
        return allowed, reason

    monkeypatch.setattr(_ExecutionContext, "authorize_started", spy)
    return calls


async def test_approval_unbound_execute_not_launched_after_approve(
    ctx, db_conn, tmp_path, monkeypatch
):
    """UNBOUND 的程序限制：普通批准后仍不得启动程序（零启动）。"""
    program_dir = tmp_path / "appr-bin"
    runner = _launch_program(program_dir, "runner")
    alt_dir = tmp_path / "appr-alt"
    alternate = _hardlink(
        runner, alt_dir / ("alternate.exe" if os.name == "nt" else "alternate")
    )
    base_path = os.environ.get("PATH", "")
    # 入口名 alternate 可查找；原名称 runner 当前查找失败（PATH 不含 runner 目录）。
    monkeypatch.setenv("PATH", str(alt_dir) + os.pathsep + base_path)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="运行 alternate 生成一个结果文件",
        message_id="m-appr-exec",
        turn_id="t-appr-exec",
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    marker = Path(ctx.computer.root()) / "appr-exec-marker.txt"
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    args = _marker_command(marker)
    with requirement_context(binding):
        approval_id = await _approval_request(
            ctx,
            tool,
            program="alternate",
            args=args,
            cwd=str(ctx.computer.root()),
            timeout=30,
        )
    # 批准期间要求被重新理解为「禁止运行 runner」，但历史数据没有对象绑定。
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止运行 runner",
            }
        ],
    )
    launches = _install_launch_counter(monkeypatch)
    captured = _spy_authorize_started(monkeypatch)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert launches == [], "批准后仍不得尝试启动被拒绝的程序"
    assert not marker.exists(), "批准不得解除未绑定限制（程序实际启动了）"
    denied = [item for item in captured if item["allowed"] is False]
    assert denied, captured
    assert any(
        item["limit_decision_code"] == UNKNOWN_CODE
        and item["basis_kind"] == "limit_assessment_denied"
        for item in denied
    ), captured
    # 审批恢复不自动触发绑定恢复：历史限制保持原样。
    after = ctx.requirements.by_id(requirement.requirement_id)
    object_state = after.structured_limits[0]["objects"][0]
    assert not object_state.get("program_binding"), object_state


async def test_approval_unknown_read_limit_reads_nothing_after_approve(
    ctx, tmp_path, monkeypatch
):
    """UNKNOWN 的读取限制：批准后执行器仍不得读取正文/摘要，也不得写入。"""
    target = _text_program(tmp_path / "appr-write", "prog", "original-content\n")
    write_target = _hardlink(
        target,
        tmp_path / "appr-write-alias" / ("alias.exe" if os.name == "nt" else "alias"),
    )
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="把这个文件改成 new-content",
        message_id="m-appr-read",
        turn_id="t-appr-read",
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        # 审批对象是限制路径的**真实硬链接**（候选路径文本不同，不能按路径文本直接 MATCH）。
        approval_id = await _approval_request(
            ctx, tool, path=str(write_target), content="new-content\n"
        )
    # 审批期间用户加入「禁止读取该文件」限制。
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": ["read"],
                "objects": [{"kind": "path", "value": str(target)}],
                "reason": "禁止读取该文件",
            }
        ],
        source="test-r9h12-c:appr-read",
        state=requirement_limits.STATE_STRUCTURED,
    )
    # 【模拟】限制路径身份不可读（对象存在，系统边界读取失败）→ 事实不足 UNKNOWN。
    _fail_identity_for(monkeypatch, [target])
    reads = _install_read_spies(monkeypatch)
    captured = _spy_authorize_started(monkeypatch)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert reads == [], "UNKNOWN 读取限制必须在读取正文/摘要前拒绝"
    assert target.read_text(encoding="utf-8") == "original-content\n"
    assert any(
        item["limit_decision_code"] == UNKNOWN_CODE
        and item["basis_kind"] == "limit_assessment_denied"
        for item in captured
    ), captured


async def test_recheck_consumes_same_assessment_for_execute(
    ctx, db_conn, tmp_path, monkeypatch
):
    """recheck 消费同一个 assessment：UNBOUND execute 限制 → denied + 零摘要读取。"""
    import agent.security.recheck as recheck_module

    program_dir = tmp_path / "recheck-bin"
    runner = _launch_program(program_dir, "runner")
    alt_dir = tmp_path / "recheck-alt"
    alternate = _hardlink(
        runner, alt_dir / ("alternate.exe" if os.name == "nt" else "alternate")
    )
    base_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", str(alt_dir) + os.pathsep + base_path)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-recheck", turn_id="t-recheck"
    )
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止运行 runner",
            }
        ],
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    # 用工具真实的 effect 构造函数拿到与执行时同一份事实（不重写判定）。
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    spec = launch_boundary.LaunchSpec(
        kind="program",
        program="alternate",
        argv=(),
        cwd=str(ctx.computer.root()),
        timeout=30,
    )
    execution = tool._execution_facts(spec, shell=False)
    effect = tool._execute_effect(
        tool="run_program",
        program="alternate",
        args=[],
        cwd=str(ctx.computer.root()),
        shell=False,
        execution=execution,
        delete_intent=None,
        purpose="运行程序（重核对消费同一 assessment）",
    )
    operation_binding = OperationBinding(
        requirement=RequirementBinding(
            requirement_id=requirement.requirement_id, version=int(requirement.version)
        ),
        mode="unrestricted",
        operation_kind="proc.run_program",
    )
    digests: list[str] = []
    monkeypatch.setattr(
        recheck_module,
        "content_digest",
        lambda path: digests.append(str(path)) or "deadbeef",
    )
    service = RecheckService(
        engine=ctx.authorization, requirements=ctx.requirements
    )
    result = await service.recheck(operation_binding, effect)
    assert result.ok is False, result
    assert result.code == "denied", result
    assert result.decision_code == UNKNOWN_CODE, result
    assert digests == [], "共同判定拒绝时不得读取内容摘要"
    decision = getattr(result, "decision", None)
    verdict = str(getattr(getattr(decision, "verdict", None), "value", "") or "")
    assert verdict == "deny", result


async def test_all_entry_kinds_reach_the_same_match_conclusion(
    ctx, tmp_path, monkeypatch
):
    """同一对象 MATCH 限制：read/write/delete/execute 四个入口消费同一 assessment 结论。"""
    target = _text_program(tmp_path / "all-entries", "prog", "原始正文\n")
    ctx.settings_store.set("computer.permission_mode", "unrestricted")

    # 1) 读/写/执行同一条限制：读先于写（写入口的读取探测也要 0 读取）。
    requirement = ctx.requirements.record_user_message(
        text="禁止读取、修改或运行这个程序文件",
        message_id="m-all",
        turn_id="t-all",
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": ["read", "write", "execute"],
                "objects": [{"kind": "path", "value": str(target)}],
                "reason": "禁止对这个对象读/写/执行",
            }
        ],
        source="test-r9h12-c:all",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    reads = _install_read_spies(monkeypatch)
    calls = _spy_limit_conflict_result(monkeypatch)
    launches = _install_launch_counter(monkeypatch)
    results = {}
    with requirement_context(binding):
        results["read"] = await FsReadTool(
            computer=ctx.computer, security=ctx.authorization
        ).run(path=str(target))
        results["write"] = await FsWriteTool(
            computer=ctx.computer, security=ctx.authorization
        ).run(path=str(target), content="被改写\n")
    for label, result in results.items():
        assert result.ok is False, (label, result)
        assert "match" in _states(result), (label, _states(result))
    assert reads == [], "被拒的读取/写入都在读取正文/摘要前拒绝"
    # execute 入口：既有 H5/H7 设计会在判定前采集「程序内容摘要」作为启动事实
    # （审查/审批/执行前重核对共用）；这里按契约 §5 的「程序 0 次启动」验收，
    # 并额外要求它没有读取/打开任何文件正文（只有程序摘要 sha256 允许）。
    body_reads = [
        item for item in reads if item.startswith(("open:", "read_bytes:", "read_text:"))
    ]
    with requirement_context(binding):
        results["execute"] = await RunProgramTool(
            computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
        ).run(program=str(target), args=[], cwd=str(ctx.computer.root()), timeout=10)
    assert results["execute"].ok is False, results["execute"]
    assert "match" in _states(results["execute"]), _states(results["execute"])
    assert launches == [], "MATCH 判定命中时不得启动任何程序"
    assert body_reads == [], "被拒入口不得读取任何文件正文"
    assert target.exists()
    read_denied = [
        item for item in calls if item["effect_kind"] == "read" and item["denied"]
    ]
    execute_denied = [
        item for item in calls if item["effect_kind"] == "execute" and item["denied"]
    ]
    assert read_denied and execute_denied, calls
    assert any("match" in item["states"] for item in read_denied), read_denied
    assert any("match" in item["states"] for item in execute_denied), execute_denied

    # 2) 只写限制：写入口在读取探测（操作范围外）之后仍必须按写判定 MATCH 拒绝。
    write_requirement, write_binding = _save_limits(
        ctx,
        text="禁止修改这个程序文件",
        operations=["write"],
        objects=[{"kind": "path", "value": str(target)}],
        tag="all-write",
    )
    write_calls = _spy_limit_conflict_result(monkeypatch)
    with requirement_context(write_binding):
        write_result = await FsWriteTool(
            computer=ctx.computer, security=ctx.authorization
        ).run(path=str(target), content="被改写\n")
    assert write_result.ok is False, write_result
    write_denied = [
        item
        for item in write_calls
        if item["effect_kind"] == "write" and item["denied"]
    ]
    assert write_denied and any("match" in item["states"] for item in write_denied)
    assert target.read_text(encoding="utf-8") == "原始正文\n"

    # 3) 只删限制：删除入口（读取探测属操作范围外的可靠无关）→ 删除判定 MATCH。
    del_requirement, del_binding = _save_limits(
        ctx,
        text="禁止删除这个程序文件",
        operations=["delete"],
        objects=[{"kind": "path", "value": str(target)}],
        tag="all-delete",
    )
    del_calls = _spy_limit_conflict_result(monkeypatch)
    with requirement_context(del_binding):
        delete_result = await FsDeleteTool(
            computer=ctx.computer, security=ctx.authorization
        ).run(paths=[str(target)])
    assert delete_result.ok is False, delete_result
    # 删除走批量逐项 authorize：判定由引擎内部同一 assess_limit_conflict 完成。
    delete_denied = [
        item for item in del_calls if item["effect_kind"] == "delete" and item["denied"]
    ]
    assert delete_denied, del_calls
    assert any("match" in item["states"] for item in delete_denied), delete_denied
    assert target.exists(), "被拒绝的删除不得删除文件"
    assert target.read_text(encoding="utf-8") == "原始正文\n"
