"""R9-H13 B：对象判定过严（B1/B2/B3）的真实流程验收（先红后绿）。

本文件只用真实引擎（AuthorizationEngine + security/object_state 五态判定）、
真实工具（fs_read / run_program）、真实要求台账（SQLite + set_structured_limits）、
真实临时文件 / 真实硬链接 / 真实 PATH 查找 / 真实程序启动；模型一律 fake（不调用模型）。

真实 / 模拟边界（按契约 §5 如实标注）：
* 真实：文件对象、硬链接（os.link 后 samefile 断言）、文件身份（volume+file_id+
  birth_key）、PATH 查找、可执行程序启动（临时目录内复制系统 shell 副本，marker
  证明真的启动）、SQLite 台账、引擎与工具判定；
* 构造（保存事实注入）：B1/B2 中 requirement_limits 的“保存时对象身份 + 保存候选
  identity”目前生产代码尚未写入（Lead 已确认由 A 的保存流程补，见交付报告“依赖/
  阻塞”）；本文件用 **构造的保存事实** 经真实引擎入口注入（monkeypatch
  AuthorizationEngine._requirement_for，只补保存字段，不改判定逻辑），用于验证
  object_state 消费侧的真实行为。真实保存层端到端作为依赖项在报告中列明。
* 无任何判定结果伪造：注入的字段只是 requirement_limits 即将保存的对象事实
  （identity/birth_key/exists/observed_at/source 与 binding_candidates.identity）。

基线 e68fb9b7 上的复现（红）：
* B1：未绑定 program_name 读取限制（局部程序不在 PATH；保存候选带 identity 时）
  普通 report.txt 在 read_only/restricted/unrestricted 三种模式都被拒绝；
* B2：保存“禁止读取 A”后 B 可读；A 被删除后 B 也被拒绝；
* B3：禁止绑定到 A，PATH 改为指向独立程序 B；B 的绝对路径允许执行，
  同名入口 runner 却被判 INVALID。
"""

from __future__ import annotations

import dataclasses
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
from agent.security.engine import AuthorizationEngine
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.engine import AuthorizationEngine
from agent.security.object_state import ObjectState
from agent.services import file_identity
from agent.tools import security_gate
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsReadTool, FsWriteTool

#: 真实可执行程序来源（控制组证明“本可启动”）。
_LAUNCH_SOURCE = (
    os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    if os.name == "nt"
    else (shutil.which("sh") or "/bin/sh")
)

_MODES = ("read_only", "restricted", "unrestricted")

#: 冻结决策码（契约 §2/§5；与 object_state.decision_code_for 一致）。
UNBOUND_CODE = "program_name_requires_object_binding"
INVALID_CODE = "program_binding_invalidated"
UNKNOWN_CODE = "object_state_unconfirmed"


# --------------------------------------------------------------------------
# 真实装配（与 H11/H12 同一套：真实 app ctx + SQLite 台账 + 真实引擎）
# --------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _workspace(ctx) -> Path:
    return Path(ctx.computer.root())


def _save_limits(ctx, *, text: str, operations, objects, tag: str):
    """真实保存流程：record_user_message + set_structured_limits（触发绑定创建）。"""
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
        source="test-r9h13-b:" + tag,
        state=requirement_limits.STATE_STRUCTURED,
    )
    saved = ctx.requirements.structured_limits_for(requirement.requirement_id)
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    return requirement, binding, tuple(saved or ())


def _augment_saved_requirement(monkeypatch, requirement_id: str, mutator):
    """注入“保存时对象事实”（构造分支）：只把 mutator 返回的字段补进对象条目。

    生产上这些字段应由 requirement_limits/requirements 保存流程写入（A 的改动）；
    本函数不改任何判定逻辑，只补保存数据，判定仍由真实 object_state 完成。
    同时包住引擎的两条取要求入口（工具预核对 current_requirement 走
    _requirement_for；authorize 走 _requirement_binding_state），保证真实链路
    看到同一份保存事实。
    """
    real_for = AuthorizationEngine._requirement_for
    real_state = AuthorizationEngine._requirement_binding_state

    def augment(requirement):  # noqa: ANN001
        if requirement is None or str(
            getattr(requirement, "requirement_id", "")
        ) != str(requirement_id):
            return requirement
        limits = []
        for entry in requirement.structured_limits or ():
            entry = {**entry}
            entry["objects"] = [
                mutator(dict(obj)) for obj in (entry.get("objects") or ())
            ]
            limits.append(entry)
        return dataclasses.replace(requirement, structured_limits=tuple(limits))

    def patched_for(self, context):  # noqa: ANN001
        return augment(real_for(self, context))

    def patched_state(self, context, effect=None):  # noqa: ANN001
        requirement, state, problem = real_state(self, context, effect)
        return augment(requirement), state, problem

    monkeypatch.setattr(AuthorizationEngine, "_requirement_for", patched_for)
    monkeypatch.setattr(
        AuthorizationEngine, "_requirement_binding_state", patched_state
    )
    return patched_for


def _identity(path: Path):
    found = file_identity.identity_of(path)
    assert found is not None, "测试前提：对象身份必须可读：" + str(path)
    return found


def _launch_program(directory: Path, name: str) -> Path:
    """真实可执行程序副本（同卷可启动）。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (name + ".exe" if os.name == "nt" else name)
    shutil.copy2(_LAUNCH_SOURCE, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _text_file(directory: Path, name: str, text: str = "普通正文\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(text, encoding="utf-8")
    return target


def _read(ctx, path: Path):
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    return tool.run(path=str(path))


def _run(ctx, program: str, *, cwd: Path, marker: Path):
    tool = RunProgramTool(
        computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization
    )
    if os.name == "nt":
        args = ["/c", "echo ran>%s" % marker]
    else:
        args = ["-c", "printf ran > %s" % marker]
    return tool.run(program=program, args=args, cwd=str(cwd), timeout=30)


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


def _decision_code(result) -> str | None:
    data = getattr(result, "data", None) or {}
    code = str(data.get("decision_code") or "")
    return code or None


def _states(result) -> list[str]:
    data = getattr(result, "data", None) or {}
    return [
        str(item.get("state"))
        for item in (data.get("object_decisions") or [])
        if isinstance(item, dict)
    ]


def _bases(result) -> list[str]:
    data = getattr(result, "data", None) or {}
    return [
        str(item.get("basis"))
        for item in (data.get("object_decisions") or [])
        if isinstance(item, dict)
    ]


def _read_allowed(result) -> bool:
    return bool(getattr(result, "ok", False))


def _assert_denied(result) -> None:
    assert getattr(result, "ok", False) is False, result
    assert getattr(result, "category", "") == "permission", result


# ==========================================================================
# B1：未绑定的局部程序读取限制不得拒绝普通文件读取
# ==========================================================================


def _b1_setup(ctx, tmp_path, monkeypatch):
    """局部程序（workspace/tools 下、不在 PATH）+ 普通 report.txt。"""
    local_program = _launch_program(_workspace(ctx) / "tools", "localtool")
    report = _text_file(_workspace(ctx), "report.txt", "报告正文\n")
    # PATH 不含局部程序目录：真实保存时 which 找不到这个局部程序。
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    return local_program, report


def _b1_saved_candidate_without_identity(local_program: Path) -> dict:
    """构造保存候选事实（无 identity：历史数据/保存时身份不可读的形态）。"""
    return {
        "path": str(local_program.resolve()),
        "source": "path_lookup",
        "state": "unresolved",
    }


def _b1_augment_candidates(monkeypatch, requirement_id: str, candidate: dict):
    def mutator(obj: dict) -> dict:
        if str(obj.get("kind")) == "program_name":
            obj["binding_candidates"] = [dict(candidate)]
        return obj

    return _augment_saved_requirement(monkeypatch, requirement_id, mutator)


def _b1_assessment(ctx, binding, requirement, report: Path):
    """真实引擎入口的结构化判定（读取 effect，真实对象状态判定）。"""
    effect = security_gate.read_effect("fs_read", report, purpose="读取文件内容")
    with requirement_context(binding):
        return ctx.authorization.assess_limit_conflict(
            effect, ctx.requirements.by_id(requirement.requirement_id)
        )


async def test_b1_unbound_local_program_limit_allows_plain_file_in_all_modes(
    ctx, tmp_path, monkeypatch
):
    """B1 核心（红）：未绑定局部程序读取限制下，普通 report.txt 三种模式可读。

    H13 范围规则：限制对象 kind=program_name + 无有效绑定 + 无任何保存候选 +
    本次 operation=read + 候选是具体文件路径 → 该限制是“程序”概念，本次是普通
    文件读取，按当前模式继续。依据来自保存事实（未绑定/无候选）与对象事实
    （文件路径）；当前环境对限制名称的解析只作候选事实写入 facts，不单独用来
    证明无关（解析到同一对象时仍是 UNBOUND）。
    """
    local_program, report = _b1_setup(ctx, tmp_path, monkeypatch)
    requirement, binding, saved = _save_limits(
        ctx,
        text="禁止读取局部程序 localtool",
        operations=["read"],
        objects=[{"kind": "program_name", "value": "localtool"}],
        tag="b1-plain",
    )
    saved_item = [obj for obj in saved[0]["objects"] if obj.get("kind") == "program_name"][0]
    assert saved_item.get("binding_state") == "unresolved", saved
    assert not saved_item.get("program_binding"), saved
    assert not saved_item.get("binding_candidates"), saved

    assessment = _b1_assessment(ctx, binding, requirement, report)
    assert assessment.conflict is False, assessment.as_dict()
    unrelated = [
        decision
        for decision in assessment.decisions
        if decision.state is ObjectState.UNRELATED
    ]
    assert unrelated, assessment.as_dict()
    decision = unrelated[0]
    assert decision.basis == "program_concept_file_read_out_of_scope", decision.as_dict()
    from agent.security import object_state as object_state_module

    assert object_state_module.validate_unrelated_decision(decision) == ()
    facts = {fact.field: fact for fact in decision.facts}
    for field in (
        "limit_kind",
        "binding_state",
        "has_saved_candidates",
        "read_path_scope",
        "operation",
        "candidate",
        "candidate_path",
        "resolution",
    ):
        assert field in facts, (field, decision.as_dict())
    assert facts["limit_kind"].observed == "program_name"
    assert facts["has_saved_candidates"].equal is False
    assert facts["read_path_scope"].equal is True
    assert facts["operation"].observed == "read"
    assert facts["resolution"].observed == "unresolved"

    for mode in _MODES:
        ctx.settings_store.set("computer.permission_mode", mode)
        with requirement_context(binding):
            result = await _read(ctx, report)
        assert _read_allowed(result), (mode, result)
        assert result.content == "报告正文\n", (mode, result)
        assert _decision_code(result) is None, (mode, result)
    assert local_program.exists()


async def test_b1_unbound_local_program_currently_resolved_stays_unbound(
    ctx, tmp_path, monkeypatch
):
    """B1 保护/澄清：当前环境解析到本次文件本体时仍是 UNBOUND（需澄清绑定）。

    不得因为“是读取”就放行程序对象本身；当前解析到同一对象是对象事实，
    结论必须是绑定缺失（专用恢复/澄清），不是 UNRELATED。
    """
    local_program, _report = _b1_setup(ctx, tmp_path, monkeypatch)
    requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止读取局部程序 localtool",
        operations=["read"],
        objects=[{"kind": "program_name", "value": "localtool"}],
        tag="b1-resolved",
    )
    # 当前 PATH 能解析到这个局部程序本体的真实名字（保存时并未形成绑定）。
    monkeypatch.setenv(
        "PATH",
        str(local_program.parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    with requirement_context(binding):
        result = await _read(ctx, local_program)
    _assert_denied(result)
    assert _decision_code(result) == UNBOUND_CODE, result
    assert "unbound" in _states(result), _states(result)


async def test_b1_unbound_program_limit_keeps_non_read_operations_fail_closed(
    ctx, tmp_path, monkeypatch
):
    """B1 残留边界：范围规则只适用于 read；写/执行等仍失败关闭。

    同一条限制同时声明 read+write，路径候选的普通读取按新范围规则继续，
    但对同一路径的写操作仍是无对象事实可证明无关的 UNKNOWN（失败关闭）。
    """
    local_program, report = _b1_setup(ctx, tmp_path, monkeypatch)
    _requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止读取和改写局部程序 localtool",
        operations=["read", "write"],
        objects=[{"kind": "program_name", "value": "localtool"}],
        tag="b1-nonread",
    )
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    write_tool = FsWriteTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        read_result = await _read(ctx, report)
        write_result = await write_tool.run(path=str(report), content="改写正文")
    assert _read_allowed(read_result), read_result
    assert write_result.ok is False, write_result
    assert _decision_code(write_result) == UNKNOWN_CODE, write_result
    assert "unknown" in _states(write_result), _states(write_result)
    assert report.read_text(encoding="utf-8") == "报告正文\n"
    assert local_program.exists()


async def test_b1_saved_candidates_still_fail_closed_for_plain_reads(
    ctx, tmp_path, monkeypatch
):
    """B1 反例：有保存候选时仍按候选判定（UNKNOWN 失败关闭），范围规则不适用。"""
    local_program, report = _b1_setup(ctx, tmp_path, monkeypatch)
    requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止读取局部程序 localtool",
        operations=["read"],
        objects=[{"kind": "program_name", "value": "localtool"}],
        tag="b1-candidates",
    )
    _b1_augment_candidates(
        monkeypatch,
        requirement.requirement_id,
        _b1_saved_candidate_without_identity(local_program),
    )
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    with requirement_context(binding):
        result = await _read(ctx, report)
    _assert_denied(result)
    assert _decision_code(result) == UNKNOWN_CODE, result
    assert "unknown" in _states(result), _states(result)
    assert "binding_missing_candidate_not_in_saved" in _bases(result), _bases(result)


# ==========================================================================
# B2：限制对象删除后不得持续拒绝无关对象；旧对象硬链接仍保护
# ==========================================================================


def _b2_saved_path_facts(path: Path) -> dict:
    """构造保存时路径对象事实（A 的保存流程落库字段：identity/birth/exists/...）。"""
    identity = _identity(path)
    return {
        "identity": identity.key,
        "birth_key": identity.birth_key,
        "exists": True,
        "observed_at": "2026-10-25T00:00:00+08:00",
        "source": "requirement_save",
    }


def _b2_augment(monkeypatch, requirement_id: str, limit_value: str, facts: dict):
    def mutator(obj: dict) -> dict:
        if str(obj.get("kind")) == "path" and str(obj.get("value")) == str(limit_value):
            obj.update(dict(facts))
        return obj

    return _augment_saved_requirement(monkeypatch, requirement_id, mutator)


async def test_b2_deleted_limit_target_does_not_block_unrelated_file(
    ctx, tmp_path, monkeypatch
):
    """B2 核心（红）：禁止读取 A；B 原本可读；A 删除后 B 仍可读（按模式正常处理）。"""
    workspace = _workspace(ctx)
    a = _text_file(workspace, "secret-a.txt", "A 正文\n")
    b = _text_file(workspace, "report-b.txt", "B 正文\n")
    facts = _b2_saved_path_facts(a)
    requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止读取 A",
        operations=["read"],
        objects=[{"kind": "path", "value": str(a)}],
        tag="b2-deleted",
    )
    _b2_augment(monkeypatch, requirement.requirement_id, str(a), facts)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    with requirement_context(binding):
        before = await _read(ctx, b)
    assert _read_allowed(before) and before.content == "B 正文\n", before
    a.unlink()
    assert not a.exists()
    effect = security_gate.read_effect("fs_read", b, purpose="读取文件内容")
    with requirement_context(binding):
        requirement_obj = ctx.authorization._requirement_for(
            ctx.authorization._context(None)
        )
        assessment = ctx.authorization.assess_limit_conflict(effect, requirement_obj)
    assert assessment.conflict is False, assessment.as_dict()
    unrelated = [
        decision
        for decision in assessment.decisions
        if decision.state is ObjectState.UNRELATED
    ]
    assert unrelated and all(
        decision.basis == "path_different_object" for decision in unrelated
    ), [decision.as_dict() for decision in assessment.decisions]
    with requirement_context(binding):
        after = await _read(ctx, b)
    assert _read_allowed(after), after
    assert after.content == "B 正文\n", after


async def test_b2_deleted_target_old_hardlink_alias_still_blocked(
    ctx, tmp_path, monkeypatch
):
    """B2 相邻保护（红）：A 路径删除后，旧对象的真实硬链接别名仍按保存身份命中。"""
    workspace = _workspace(ctx)
    a = _text_file(workspace, "secret-a2.txt", "A2 正文\n")
    alias = workspace / "alias-a2.txt"
    os.link(a, alias)
    assert os.path.samefile(a, alias), "测试前提：必须是真实硬链接（同一对象）"
    facts = _b2_saved_path_facts(a)
    requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止读取 A2",
        operations=["read"],
        objects=[{"kind": "path", "value": str(a)}],
        tag="b2-alias",
    )
    _b2_augment(monkeypatch, requirement.requirement_id, str(a), facts)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    a.unlink()
    with requirement_context(binding):
        result = await _read(ctx, alias)
    _assert_denied(result)
    assert "path_hardlink_same_object" in _bases(result), _bases(result)
    assert "match" in _states(result), _states(result)
    assert alias.read_text(encoding="utf-8") == "A2 正文\n"


async def test_b2_historical_path_limit_without_saved_identity_fails_closed(
    ctx, tmp_path, monkeypatch
):
    """B2 反例（历史数据）：没有保存身份时，A 删除后仍失败关闭（不得猜无关）。

    R9-H13 B2 修订：新保存流程现在会记录保存时对象事实；H13 之前的历史行没有这些
    字段。这里保留真实保存流程，再用测试侧注入把对象条目还原成历史形态（不改
    判定逻辑；原始保存事实仍在 DB 中），断言同一失败关闭语义。
    """
    workspace = _workspace(ctx)
    a = _text_file(workspace, "secret-a3.txt", "A3 正文\n")
    b = _text_file(workspace, "report-b3.txt", "B3 正文\n")
    requirement, binding, saved = _save_limits(
        ctx,
        text="禁止读取 A3",
        operations=["read"],
        objects=[{"kind": "path", "value": str(a)}],
        tag="b2-historical",
    )
    # 新保存确实记录了保存时对象事实（B2 生产行为）。
    assert "identity" in saved[0]["objects"][0], saved

    def _strip_saved_facts(obj):  # noqa: ANN001
        return {
            key: value
            for key, value in obj.items()
            if key not in requirement_limits.SAVED_OBJECT_FACT_KEYS
        }

    _augment_saved_requirement(
        monkeypatch, requirement.requirement_id, _strip_saved_facts
    )
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    a.unlink()
    with requirement_context(binding):
        result = await _read(ctx, b)
    _assert_denied(result)
    assert _decision_code(result) == UNKNOWN_CODE, result
    assert "unknown" in _states(result), _states(result)


# ==========================================================================
# B3：同名入口解析到独立对象时按对象事实判断
# ==========================================================================


def _b3_programs(tmp_path: Path):
    """两个不同的真实程序：dirA/runner 与 dirB/runner（独立对象）。"""
    runner_a = _launch_program(tmp_path / "b3-a", "runner")
    runner_b = _launch_program(tmp_path / "b3-b", "runner")
    assert not os.path.samefile(runner_a, runner_b), "测试前提：必须是两个独立对象"
    return runner_a, runner_b


def _b3_path(monkeypatch, directory: Path) -> None:
    """当前 PATH 优先解析到指定目录（真实 shutil.which 查找）。"""
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))


async def test_b3_same_name_entry_resolving_to_independent_program_continues(
    ctx, tmp_path, monkeypatch
):
    """B3 核心（红）：禁止绑定到 A，PATH 指向独立程序 B；同名入口 runner 应继续。

    B 绝对路径执行一直允许（相邻正常放行）；同名入口解析到 B（identity 可读且
    与绑定不同、入口路径≠绑定路径）→ 可靠不同对象，按当前模式继续并真实启动。
    """
    runner_a, runner_b = _b3_programs(tmp_path)
    _b3_path(monkeypatch, runner_a.parent)
    requirement, binding, saved = _save_limits(
        ctx,
        text="禁止运行 runner",
        operations=["execute"],
        objects=[{"kind": "program_name", "value": "runner"}],
        tag="b3-entry",
    )
    bound = saved[0]["objects"][0].get("program_binding")
    assert bound and str(bound.get("path")) == str(runner_a.resolve()), saved
    # PATH 改为只含 B 的目录：同名入口 runner 现在解析到独立程序 B。
    _b3_path(monkeypatch, runner_b.parent)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    marker = _workspace(ctx) / "b3-entry-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    with requirement_context(binding):
        by_path = await _run(ctx, str(runner_b), cwd=_workspace(ctx), marker=marker)
    assert by_path.ok is True, by_path
    assert marker.exists(), "B 绝对路径必须真实启动（相邻正常放行）"
    marker.unlink()
    with requirement_context(binding):
        by_name = await _run(ctx, "runner", cwd=_workspace(ctx), marker=marker)
    assert by_name.ok is True, by_name
    assert marker.exists(), "同名入口解析到独立对象 B 必须按对象事实继续并真实启动"
    assert launches, "必须发生真实程序启动"


async def test_b3_same_name_entry_resolving_to_bound_object_still_matches(
    ctx, tmp_path, monkeypatch
):
    """B3 相邻保护：同名入口仍解析到绑定对象 A → MATCH 拒绝、0 启动。"""
    runner_a, _runner_b = _b3_programs(tmp_path)
    _b3_path(monkeypatch, runner_a.parent)
    _requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止运行 runner",
        operations=["execute"],
        objects=[{"kind": "program_name", "value": "runner"}],
        tag="b3-match",
    )
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    marker = _workspace(ctx) / "b3-match-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    with requirement_context(binding):
        result = await _run(ctx, "runner", cwd=_workspace(ctx), marker=marker)
    _assert_denied(result)
    assert "match" in _states(result), _states(result)
    assert launches == [], "命中绑定对象的入口不得启动"
    assert not marker.exists(), "命中绑定对象的入口实际启动了程序"


async def test_b3_same_name_entry_on_bound_path_replaced_object_still_invalid(
    ctx, tmp_path, monkeypatch
):
    """B3 失败关闭：同名入口仍解析到绑定**路径**但对象身份已变 → INVALID、0 启动。"""
    runner_a, _runner_b = _b3_programs(tmp_path)
    _b3_path(monkeypatch, runner_a.parent)
    _requirement, binding, _saved = _save_limits(
        ctx,
        text="禁止运行 runner",
        operations=["execute"],
        objects=[{"kind": "program_name", "value": "runner"}],
        tag="b3-invalid",
    )
    # 原绑定路径对象被替换：同一路径写入另一个真实程序（身份不同）。
    runner_a.unlink()
    shutil.copy2(_LAUNCH_SOURCE, runner_a)
    if os.name != "nt":
        runner_a.chmod(0o755)
    assert not os.path.samefile(runner_a, _LAUNCH_SOURCE)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    marker = _workspace(ctx) / "b3-invalid-marker.txt"
    launches = _install_launch_counter(monkeypatch)
    with requirement_context(binding):
        result = await _run(ctx, "runner", cwd=_workspace(ctx), marker=marker)
    _assert_denied(result)
    assert _decision_code(result) == INVALID_CODE, result
    assert launches == [], "绑定失效的同名入口不得启动"
    assert not marker.exists()
