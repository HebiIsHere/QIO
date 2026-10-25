"""R9-H13 A：要求继承、绑定恢复与限制去重（A1-A3）真实流程验收（契约 §3.A/§5）。

真实流程（全部真实对象/台账/引擎/工具，模型 fake）：
* 真实 SQLite 要求台账（RequirementLedger + 真实迁移）、真实用户要求解析落实入口
  ``apply_resolution``、真实专用绑定恢复服务入口
  ``AppContext._ensure_legacy_structured_limits``/``_mark_limits_unresolved``；
* 真实 PATH 查找、真实临时可执行程序（系统 shell 副本）、真实启动事实（marker 文件
  证明程序真的运行了；被拒绝时 0 次启动）；
* 真实硬链接（``os.link`` 后断言 ``samefile``）、真实文件身份提供器
  （volume+file_id+birth_key）；
* 真实 AuthorizationEngine + 工具适配层（RunProgramTool / FsReadTool / FsWriteTool）。

模拟（显式标注，不伪造判定结论）：
* 模型：``ResolutionOutcome`` 由测试构造（等价于 fake 模型输出），不联网；
* 文件身份提供器边界：``file_identity.identity_of`` 对指定路径返回 None，复现
  “对象存在但保存时身份不可读”的系统边界；判定仍由真实 object_state 完成。

本文件先于实现落盘：A1/A2/A3 用例在基线 e68fb9b7 上红，修复后转绿；红/绿输出见
``_r9h13-evidence/a/``。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import launch as launch_boundary
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.requirements import RequirementBinding
from agent.security.resolution import RequirementRelation, ResolutionOutcome
from agent.services import file_identity
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsReadTool

#: 冻结契约 §2/§3 的决策码（与 effects.py / object_state.decision_code_for 一致）。
UNBOUND_CODE = "program_name_requires_object_binding"
INVALID_CODE = "program_binding_invalidated"
UNKNOWN_CODE = "object_state_unconfirmed"

#: 真实可执行程序（启动用例的控制组/反证用）。
_LAUNCH_SOURCE = (
    os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    if os.name == "nt"
    else (shutil.which("sh") or "/bin/sh")
)

_PROGRAM_LIMIT = {
    "operations": ["execute"],
    "objects": [{"kind": "program_name", "value": "runner"}],
    "reason": "禁止运行 runner",
}


# --------------------------------------------------------------------------
# 夹具与辅助（真实对象；身份注入处显式标注「模拟」）
# --------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _launch_program(directory: Path, name: str) -> Path:
    """真实可执行程序副本（复制系统 shell，可真实启动）。"""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name += ".exe"
    target = directory / name
    shutil.copy2(_LAUNCH_SOURCE, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _hardlink(source: Path, link: Path) -> Path:
    """真实硬链接（同卷）；断言确实是同一个对象。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, link)
    assert os.path.samefile(source, link), "测试前提：硬链接必须指向同一对象"
    return link


def _set_path(monkeypatch, *directories: Path) -> None:
    base = os.environ.get("PATH", "")
    parts = [str(item) for item in directories if str(item)]
    monkeypatch.setenv("PATH", os.pathsep.join(parts + [base]))


def _marker_command(marker: Path) -> list[str]:
    if os.name == "nt":
        return ["/c", "echo ran>%s" % marker]
    return ["-c", "printf ran > %s" % marker]


def _install_launch_counter(monkeypatch) -> list[dict]:
    """真实启动计数：包装受控启动器（真的会启动；只记录每次 launch 尝试）。"""
    calls: list[dict] = []
    real_launch = launch_boundary.ControlledLauncher.launch

    async def spy(self, *args, **kwargs):
        spec = args[0] if args else kwargs.get("spec")
        calls.append({"program": str(getattr(spec, "program", ""))})
        return await real_launch(self, *args, **kwargs)

    monkeypatch.setattr(launch_boundary.ControlledLauncher, "launch", spy)
    return calls


def _fail_identity_for(monkeypatch, paths) -> set[str]:
    """【模拟】文件身份提供器边界：指定路径「存在但身份不可读」。

    只注入系统边界失败，不改变任何判定状态；返回可变集合，discard 即恢复可读
    （模拟“保存时不可读、之后可读”的时间差）。
    """
    real = file_identity.identity_of
    wanted = {str(path) for path in paths}

    def wrapped(path):
        if str(path) in wanted:
            return None
        return real(path)

    monkeypatch.setattr(file_identity, "identity_of", wrapped)
    return wanted


def _real(path: Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _raw_limits(conn, requirement_id: str):
    row = conn.execute(
        "SELECT structured_limits FROM user_requirements WHERE id = ?",
        (str(requirement_id),),
    ).fetchone()
    return None if row is None else row[0]


def _decision_code(result) -> str | None:
    data = getattr(result, "data", None) or {}
    code = str(data.get("decision_code") or "")
    return code or None


def _states(result) -> list[str]:
    data = getattr(result, "data", None) or {}
    decisions = data.get("object_decisions") or []
    return [str(item.get("state")) for item in decisions if isinstance(item, dict)]


def _amend_outcome(
    requirement,
    *,
    message: str,
    limits=(),
    removed_limits=(),
    reason: str = "用户补充",
) -> ResolutionOutcome:
    """【模拟】模型输出：只给出语义关系/新增/解除，对象事实由后端核验。"""
    return ResolutionOutcome(
        relation=RequirementRelation.AMEND,
        target_requirement_id=requirement.requirement_id,
        target_version=int(requirement.version),
        corrections=(message,),
        limits=tuple(dict(item) for item in limits),
        removed_limits=tuple(dict(item) for item in removed_limits),
        reason=reason,
        model="fake-r9h13-a",
        prompt_version="fake-r9h13-a-v1",
    )


async def _run_program(ctx, program: Path, marker: Path, *, binding) -> object:
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    with requirement_context(binding):
        return await tool.run(
            program=str(program),
            args=_marker_command(marker),
            cwd=str(ctx.computer.root()),
            timeout=30,
        )


# ==========================================================================
# A1 普通补充不得重新绑定旧禁止
# ==========================================================================


async def test_a1_plain_amend_keeps_inherited_unresolved_limit(
    ctx, tmp_path, monkeypatch
):
    """A1：保存 runner 禁止（身份不可读 → 候选 A）→ PATH 改为 B → 只补充标题。

    契约 §3.A1：amend 必须**原样继承**旧绑定/未解决状态/失败原因/候选及来源，
    不得重新查找并把继承限制绑定到 B；A 仍必须受保护（拒绝且 0 次启动）。
    基线（e68fb9b7）：add_correction 对继承集合重跑 attach_program_bindings，
    A 被换成 UNRELATED 后实际启动成功（本用例红）。
    """
    a_dir = tmp_path / "a1-bin"
    a = _launch_program(a_dir, "runner")
    b_dir = tmp_path / "a1-other-bin"
    b = _launch_program(b_dir, "runner")  # 另一个真实同名对象（PATH 后来优先找到）

    # 保存阶段：PATH 优先找到 A，但 A 的身份读取失败（模拟身份提供器边界）。
    wanted = _fail_identity_for(monkeypatch, [a])
    _set_path(monkeypatch, a_dir)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-a1", turn_id="t-a1"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:a1:save"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    saved_obj = stored.structured_limits[0]["objects"][0]
    assert "program_binding" not in saved_obj, saved_obj
    assert saved_obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED
    assert (
        saved_obj.get("binding_failure")
        == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    )
    saved_candidates = [str(item.get("path")) for item in saved_obj["binding_candidates"]]
    assert any(
        os.path.normcase(os.path.realpath(item)) == _real(a) for item in saved_candidates
    ), saved_candidates
    parent_source = str(stored.structured_limits_source or "")

    # 时间差：A 的身份恢复可读；PATH 改为优先找到 B（另一个真实对象）。
    wanted.discard(str(a))
    _set_path(monkeypatch, b_dir)

    # 真实 amend 流程：用户只补充“把报告标题改成 X”（没有新增/解除限制）。
    applied = ctx.requirements.apply_resolution(
        _amend_outcome(stored, message="把报告标题改成 X"),
        message="把报告标题改成 X",
        message_id="m-a1-amend",
        turn_id="t-a1",
    )
    child = ctx.requirements.by_id(applied.requirement.requirement_id)
    assert child is not None and int(child.version) == int(stored.version) + 1
    child_obj = child.structured_limits[0]["objects"][0]
    assert "program_binding" not in child_obj, (
        "普通补充不得把继承的未解决限制重新绑定到 PATH 当前对象："
        + json.dumps(child_obj, ensure_ascii=False, default=str)
    )
    assert child_obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED
    assert (
        child_obj.get("binding_failure")
        == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    )
    assert [str(item.get("path")) for item in child_obj["binding_candidates"]] == (
        saved_candidates
    ), "候选事实必须原样继承"
    assert all(
        str(item.get("source")) == "path_lookup"
        for item in child_obj["binding_candidates"]
    ), "候选来源必须原样继承"
    assert parent_source and parent_source in str(child.structured_limits_source or ""), (
        "原 structured_limits_source 必须仍可追溯（来源原样继承）："
        + str(child.structured_limits_source)
    )

    # 真实执行 A：必须被拒，且程序 0 次启动 / marker 不存在。
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    binding = ctx.requirements.binding_for(child.requirement_id)
    assert binding is not None
    marker = Path(ctx.computer.root()) / "a1-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    result = await _run_program(ctx, a, marker, binding=binding)
    assert result.ok is False, result
    assert _decision_code(result) in (UNBOUND_CODE, UNKNOWN_CODE), result
    assert launches == [], "被拒绝的执行不得尝试启动任何程序"
    assert not marker.exists(), "普通补充后 A 实际执行成功（基线缺陷）"

    # 相邻正常放行：execute 限制不影响普通报告读取（operation_out_of_scope）。
    report = Path(ctx.computer.root()) / "a1-report.txt"
    report.write_text("标题：旧", encoding="utf-8")
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await read_tool.run(path=str(report))
    assert read_result.ok is True, read_result
    assert read_result.content == "标题：旧"


async def test_a1_amend_binds_only_newly_added_limit(ctx, tmp_path, monkeypatch):
    """A1：普通补充里**本次新增**的限制仍要建绑定；继承条目不得重绑。

    父限制（runner，候选 A）继承保持未解决；同一次 amend 新增“禁止运行 blocker”
    必须按真实后端事实绑定到 D。执行 A 与 D 都必须被拒（0 启动），读取正常放行。
    """
    a_dir = tmp_path / "a1b-bin"
    a = _launch_program(a_dir, "runner")
    b_dir = tmp_path / "a1b-other-bin"
    b = _launch_program(b_dir, "runner")
    d_dir = tmp_path / "a1b-blocker-bin"
    d = _launch_program(d_dir, "blocker")

    wanted = _fail_identity_for(monkeypatch, [a])
    _set_path(monkeypatch, a_dir)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-a1b", turn_id="t-a1b"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:a1b:save"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    wanted.discard(str(a))
    _set_path(monkeypatch, b_dir, d_dir)

    applied = ctx.requirements.apply_resolution(
        _amend_outcome(
            stored,
            message="另外禁止运行 blocker",
            limits=[
                {
                    "operations": ["execute"],
                    "objects": [{"kind": "program_name", "value": "blocker"}],
                    "reason": "禁止运行 blocker",
                }
            ],
        ),
        message="另外禁止运行 blocker",
        message_id="m-a1b-amend",
        turn_id="t-a1b",
    )
    child = ctx.requirements.by_id(applied.requirement.requirement_id)
    assert child is not None
    by_value = {
        str(item["objects"][0].get("value")): item["objects"][0]
        for item in child.structured_limits
        if item.get("objects")
    }
    runner_obj = by_value["runner"]
    assert "program_binding" not in runner_obj, runner_obj
    assert runner_obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED
    blocker_obj = by_value["blocker"]
    binding_blocker = blocker_obj.get("program_binding") or {}
    assert _real(Path(binding_blocker.get("path") or "/")) == _real(d), blocker_obj
    assert binding_blocker.get("requirement_id") == child.requirement_id
    assert binding_blocker.get("source") == "path_lookup"

    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    binding = ctx.requirements.binding_for(child.requirement_id)
    assert binding is not None
    launches = _install_launch_counter(monkeypatch)
    marker_a = Path(ctx.computer.root()) / "a1b-marker-a.txt"
    marker_d = Path(ctx.computer.root()) / "a1b-marker-d.txt"
    result_a = await _run_program(ctx, a, marker_a, binding=binding)
    result_d = await _run_program(ctx, d, marker_d, binding=binding)
    for label, result, marker in (
        ("inherited-runner", result_a, marker_a),
        ("new-blocker", result_d, marker_d),
    ):
        assert result.ok is False, (label, result)
        assert not marker.exists(), label + " 实际执行成功"
    assert launches == [], "被拒绝的执行不得尝试启动任何程序"

    report = Path(ctx.computer.root()) / "a1b-report.txt"
    report.write_text("正常读取", encoding="utf-8")
    read_tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await read_tool.run(path=str(report))
    assert read_result.ok is True, read_result


async def test_a1_indirect_rewrites_do_not_rebind_inherited_limits(
    ctx, tmp_path, monkeypatch
):
    """A1：间接入口不得重探测继承限制（set_structured_limits / app 回写）。"""
    a_dir = tmp_path / "a1c-bin"
    a = _launch_program(a_dir, "runner")
    b_dir = tmp_path / "a1c-other-bin"
    b = _launch_program(b_dir, "runner")

    wanted = _fail_identity_for(monkeypatch, [a])
    _set_path(monkeypatch, a_dir)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-a1c", turn_id="t-a1c"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:a1c:save"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    wanted.discard(str(a))
    _set_path(monkeypatch, b_dir)
    applied = ctx.requirements.apply_resolution(
        _amend_outcome(stored, message="把报告标题改成 X"),
        message="把报告标题改成 X",
        message_id="m-a1c-amend",
        turn_id="t-a1c",
    )
    child = ctx.requirements.by_id(applied.requirement.requirement_id)
    assert child is not None
    raw_before = _raw_limits(ctx.conn, child.requirement_id)

    # (a) 低层写入入口重写同一继承事实：不得重新查找并绑定 B。
    ctx.requirements.set_structured_limits(
        child.requirement_id,
        child.structured_limits,
        source="test:a1c:indirect-rewrite",
    )
    after_rewrite = ctx.requirements.by_id(child.requirement_id)
    assert after_rewrite is not None
    rewrite_obj = after_rewrite.structured_limits[0]["objects"][0]
    assert "program_binding" not in rewrite_obj, (
        "set_structured_limits 不得对继承的未解决事实重新查找/绑定："
        + json.dumps(rewrite_obj, ensure_ascii=False, default=str)
    )
    assert rewrite_obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED
    assert rewrite_obj.get("binding_candidates")
    assert _raw_limits(ctx.conn, child.requirement_id) == raw_before

    # (b) 服务层恢复失败回写入口：只把 known 标 unresolved，不得补绑定。
    await ctx._mark_limits_unresolved(
        SimpleNamespace(notices=[]), after_rewrite, code="test_recovery_failed"
    )
    after_mark = ctx.requirements.by_id(child.requirement_id)
    assert after_mark is not None
    mark_obj = after_mark.structured_limits[0]["objects"][0]
    assert "program_binding" not in mark_obj, (
        "app 回写 known limits 不得补建绑定："
        + json.dumps(mark_obj, ensure_ascii=False, default=str)
    )
    assert after_mark.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert _raw_limits(ctx.conn, child.requirement_id) == raw_before

    # (c) A 仍受保护：拒绝且 0 次启动。
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    binding = ctx.requirements.binding_for(child.requirement_id)
    assert binding is not None
    marker = Path(ctx.computer.root()) / "a1c-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    result = await _run_program(ctx, a, marker, binding=binding)
    assert result.ok is False, result
    assert _decision_code(result) in (UNBOUND_CODE, UNKNOWN_CODE), result
    assert launches == [] and not marker.exists()


# ==========================================================================
# A2 缺失历史身份事实的候选不得宣称已验证
# ==========================================================================


async def test_a2_missing_historical_identity_not_claimed_verified(
    ctx, tmp_path, monkeypatch
):
    """A2：保存候选只有 A 的路径（保存时身份不可读）；旧对象保留硬链接、A 被替换。

    契约 §3.A2：恢复不得仅凭“A 当前存在且身份可读”声称已验证并绑定 A 的新对象——
    那会把旧对象硬链接从拒绝变成允许。历史依据不足时必须保持未解决、零写入，保留
    known limits/候选/来源；随后由真实用户澄清（明确对象路径）确定限制含义，
    后端按真实对象事实核验（旧对象仍禁止、不同对象按当前模式放行）。
    基线（e68fb9b7）：_verify_saved_binding_candidate 对无 identity 事实的候选
    verified=True → 绑定 A 新对象 → 旧硬链接实际启动成功（本用例红）。
    """
    a_dir = tmp_path / "a2-bin"
    a = _launch_program(a_dir, "runner")
    old_dir = tmp_path / "a2-old"
    old_object = _hardlink(
        a, old_dir / ("runner-old.exe" if os.name == "nt" else "runner-old")
    )

    # 保存阶段：PATH 优先找到 A，但 A 的身份读取失败（模拟身份提供器边界）。
    wanted = _fail_identity_for(monkeypatch, [a])
    _set_path(monkeypatch, a_dir)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-a2", turn_id="t-a2"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:a2:save"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    saved_obj = stored.structured_limits[0]["objects"][0]
    assert "program_binding" not in saved_obj, saved_obj
    assert saved_obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED
    assert (
        saved_obj.get("binding_failure")
        == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    )
    saved_candidates = [str(item.get("path")) for item in saved_obj["binding_candidates"]]
    assert any(
        os.path.normcase(os.path.realpath(item)) == _real(a) for item in saved_candidates
    ), saved_candidates
    assert all(
        not item.get("identity") for item in saved_obj["binding_candidates"]
    ), "前提：保存时身份不可读，候选没有保存时 identity 事实"
    raw_before = _raw_limits(ctx.conn, stored.requirement_id)
    source_before = str(stored.structured_limits_source or "")
    state_before = str(stored.structured_limits_state)

    # 旧对象保留在硬链接里；A 路径被另一个真实文件（不同对象）替换。
    a.unlink()
    _launch_program(a_dir, "runner")
    wanted.discard(str(a))  # 时间差：现在身份可读，但历史对象已无法证明
    assert not os.path.samefile(old_object, a), "前提：A 已被另一个对象替换"
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    binding = ctx.requirements.binding_for(stored.requirement_id)
    assert binding is not None
    launches = _install_launch_counter(monkeypatch)

    # 恢复前控制组：旧对象硬链接执行被拒（失败关闭）。
    marker_before = Path(ctx.computer.root()) / "a2-before-marker.txt"
    before_result = await _run_program(ctx, old_object, marker_before, binding=binding)
    assert before_result.ok is False, before_result
    assert _decision_code(before_result) in (UNBOUND_CODE, UNKNOWN_CODE), before_result
    assert launches == [] and not marker_before.exists()

    # 真实专用恢复服务入口：不得采纳“当前 A 新对象”。
    recovery_ctx = SimpleNamespace(notices=[])
    recovered = await ctx._ensure_legacy_structured_limits(
        recovery_ctx,
        binding,
        message_id="m-a2-recover",
        turn_id="t-a2-recover",
    )
    assert recovered is None, "历史身份事实不足的候选不得被采纳为本轮恢复结果"
    after = ctx.requirements.by_id(stored.requirement_id)
    assert after is not None
    after_obj = after.structured_limits[0]["objects"][0]
    assert not (after_obj.get("program_binding") or {}), (
        "缺失保存时 identity 事实的候选不得宣称已验证并绑定 A 的新对象："
        + json.dumps(after_obj, ensure_ascii=False, default=str)
    )
    assert after_obj.get("binding_state") == requirement_limits.BINDING_STATE_UNRESOLVED
    assert [str(item.get("path")) for item in after_obj["binding_candidates"]] == (
        saved_candidates
    )
    assert str(after.structured_limits_state) == state_before
    assert str(after.structured_limits_source or "") == source_before
    assert _raw_limits(ctx.conn, stored.requirement_id) == raw_before, "恢复失败零写入"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)

    # 旧对象硬链接保护不得丢失；A 新对象同样无法证明同一对象 → 两者都被拒且 0 启动。
    launches.clear()
    marker_old_after = Path(ctx.computer.root()) / "a2-old-after-marker.txt"
    marker_new_after = Path(ctx.computer.root()) / "a2-new-after-marker.txt"
    old_after = await _run_program(
        ctx, old_object, marker_old_after, binding=binding
    )
    new_after = await _run_program(ctx, a, marker_new_after, binding=binding)
    for label, result, marker in (
        ("old-object-hardlink", old_after, marker_old_after),
        ("replacement-at-candidate-path", new_after, marker_new_after),
    ):
        assert result.ok is False, (label, result)
        assert _decision_code(result) in (UNBOUND_CODE, UNKNOWN_CODE), (label, result)
        assert not marker.exists(), label + " 实际执行成功（旧对象保护被解除）"
    assert launches == [], "被拒绝的执行不得尝试启动任何程序"

    # 用户澄清：真实 amend 明确限制所指对象（旧对象路径），并解除无法证明的裸名称。
    clarified_value = _real(old_object)
    applied = ctx.requirements.apply_resolution(
        _amend_outcome(
            after,
            message="我指的是旧程序对象，禁止运行它",
            limits=[
                {
                    "operations": ["execute"],
                    "objects": [{"kind": "program", "value": clarified_value}],
                    "reason": "禁止运行用户明确指出的旧程序对象",
                }
            ],
            removed_limits=[
                {
                    "operations": ["execute"],
                    "objects": [{"kind": "program_name", "value": "runner"}],
                    "reason": "澄清对象后解除无法证明的裸名称限制",
                }
            ],
            reason="用户澄清限制所指对象",
        ),
        message="我指的是旧程序对象，禁止运行它",
        message_id="m-a2-clarify",
        turn_id="t-a2",
    )
    child = ctx.requirements.by_id(applied.requirement.requirement_id)
    assert child is not None
    child_binding = ctx.requirements.binding_for(child.requirement_id)
    assert child_binding is not None
    assert not requirement_limits.needs_binding_recovery(child.structured_limits), (
        "澄清后限制应表达为明确对象，不再需要程序名称绑定恢复："
        + json.dumps(child.structured_limits, ensure_ascii=False, default=str)
    )
    # 后端按真实对象事实核验：旧对象被拒绝（0 启动），不同对象按当前模式真实运行。
    launches.clear()
    marker_old_clar = Path(ctx.computer.root()) / "a2-clar-old-marker.txt"
    marker_new_clar = Path(ctx.computer.root()) / "a2-clar-new-marker.txt"
    old_clar = await _run_program(
        ctx, old_object, marker_old_clar, binding=child_binding
    )
    assert old_clar.ok is False, old_clar
    assert not marker_old_clar.exists()
    new_clar = await _run_program(ctx, a, marker_new_clar, binding=child_binding)
    assert new_clar.ok is True, new_clar
    assert marker_new_clar.exists(), "澄清为具体旧对象后，不同对象应按当前模式正常执行"
    assert len(launches) == 1, launches


# ==========================================================================
# 引擎 E-4 依赖：AuthorizationLedger 当前有效 allow 记录只读查询
# ==========================================================================


def test_authorization_ledger_active_allow_records_are_filtered_and_readonly(
    db_conn,
):
    """只读查询：decision=allow、未撤销、未过期、当前版本且要求 active；有界 32。"""
    from agent.security.requirements import (
        STATUS_ACTIVE,
        AuthorizationLedger,
        AuthorizationRecord,
        RequirementLedger,
    )

    ledger = RequirementLedger(db_conn)
    requirement = ledger.record_user_message(
        text="生成报告", message_id="m-e4", turn_id="t-e4"
    )
    rid = requirement.requirement_id
    version = int(requirement.version)
    records = AuthorizationLedger(db_conn)

    def make(
        record_id: str,
        *,
        decision: str = "allow",
        created_at: str = "2026-01-01T00:00:00+00:00",
        expires_at: str | None = None,
        revoked_at: str | None = None,
        requirement_version: int | None = None,
        requirement_id: str | None = None,
    ) -> AuthorizationRecord:
        return AuthorizationRecord(
            record_id=record_id,
            requirement_id=rid if requirement_id is None else requirement_id,
            operation_kind="write",
            fingerprint="fp-" + record_id,
            decision=decision,
            reason="test",
            allowed_operations=("write",),
            objects=(),
            limits=(),
            source="test",
            mode="restricted",
            created_at=created_at,
            expires_at=expires_at,
            revoked_at=revoked_at,
            review_id=None,
            requirement_version=(
                version if requirement_version is None else requirement_version
            ),
        )

    records.record(make("rec-allow-1"))
    records.record(make("rec-deny", decision="deny"))
    records.record(make("rec-revoked", revoked_at="2026-01-02T00:00:00+00:00"))
    records.record(make("rec-expired", expires_at="2020-01-01T00:00:00+00:00"))
    records.record(make("rec-old-version", requirement_version=version + 1))
    records.record(
        make(
            "rec-allow-2",
            created_at="2026-02-01T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
        )
    )
    total_before = int(
        db_conn.execute("SELECT COUNT(*) FROM authorization_records").fetchone()[0]
    )

    found = records.active_allow_records_for_requirement(rid)

    assert isinstance(found, tuple)
    assert [item.record_id for item in found] == ["rec-allow-1", "rec-allow-2"]
    # 旧接口语义保持不变（不是 allow-only / 不是当前版本-only）。
    assert len(records.records_for_requirement(rid)) == 5
    # 只读：不写、不撤销、不删。
    total_after = int(
        db_conn.execute("SELECT COUNT(*) FROM authorization_records").fetchone()[0]
    )
    assert total_after == total_before
    assert all(item.revoked_at is None for item in found)

    # 要求被修正（旧版本 superseded）后，旧记录的查询结果为空。
    child = ledger.add_correction(rid, "把标题改成 X")
    assert child is not None
    assert records.active_allow_records_for_requirement(rid) == ()

    # 有界：当前版本的 allow 记录最多返回 32 条。
    for index in range(40):
        records.record(
            make(
                "rec-bulk-%02d" % index,
                created_at="2026-03-01T00:00:%02d+00:00" % index,
                requirement_id=child.requirement_id,
                requirement_version=int(child.version),
            )
        )
    bounded = records.active_allow_records_for_requirement(child.requirement_id)
    assert len(bounded) == 32, len(bounded)
    assert records.active_allow_records_for_requirement(rid) == ()
    assert STATUS_ACTIVE == "active"
