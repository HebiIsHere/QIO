"""R9-H11 B：程序限制绑定恢复 / 历史兼容 / 持久化生命周期。

覆盖 _R9H11-COMMON.md §1/§5 的 B 部分：
* normalize/serialize/deserialize 是纯数据操作：不查找程序、不采集身份、不补建绑定；
  历史 structured-but-unbound 记录读取后保持原样；
* attach_program_bindings 仍是**新保存**的可信绑定创建入口：记录
  requirement_id/version/status/path/identity/birth_key/source；
  “保存时解析失败”作为对象条目上的显式事实（binding_state/binding_failure/候选）；
* structured + program_name 无 program_binding 的历史记录进入专用恢复：
  只采纳**保存时记录的候选**（后端重新核验），不生成普通授权、不调用普通审查；
  R9-H12 修订：无保存候选时不得用当前 PATH 查找结果直接绑定（旧期望已改）。
* 恢复写回前核对 requirement id/version/status/chain_digest（条件写入）；
  恢复失败/等待期间要求被修改或撤回时丢弃结果，原始 JSON 保持不变；
* close/reopen 后绑定仍在；未恢复记录不会被普通读取/新建轮/审批隐式绑定。

模型一律 fake：本文件不联网、不使用真实模型。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.security import requirement_limits
from agent.security.context import (
    current_requirement,
    requirement_limits as context_requirement_limits,
)
from agent.security.requirements import (
    RequirementBinding,
    RequirementLedger,
    RequirementVersionMismatch,
)
from agent.security.resolution import RequirementRelation, ResolutionOutcome
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

_PROGRAM_LIMIT = {
    "operations": ["execute"],
    "objects": [{"kind": "program_name", "value": "runner"}],
    "reason": "禁止运行 runner",
}


# --------------------------------------------------------------------------
# 夹具 / 工具
# --------------------------------------------------------------------------


def _runner(directory: Path, name: str = "runner") -> Path:
    """真实临时程序（复制当前解释器），用于真实 PATH 查找与文件身份。"""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name = name + ".exe"
    target = directory / name
    shutil.copy2(sys.executable, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _prepend_path(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))


def _isolated_path(monkeypatch, directory: Path) -> None:
    """把 PATH 收窄到空目录：确定性地让程序名称查不到（不联网、不依赖宿主）。"""
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", str(directory))


def _raw_limits(conn, requirement_id: str):
    row = conn.execute(
        "SELECT structured_limits FROM user_requirements WHERE id = ?",
        (requirement_id,),
    ).fetchone()
    return None if row is None else row[0]


def _raw_row(conn, requirement_id: str):
    row = conn.execute(
        "SELECT structured_limits, structured_limits_state, structured_limits_source,"
        " status, version FROM user_requirements WHERE id = ?",
        (requirement_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _seed_structured_unbound(conn, ledger, *, limits=(_PROGRAM_LIMIT,), text="禁止运行 runner"):
    """模拟 R9-H10 之前保存的历史行：state=structured 但对象没有 program_binding。"""
    requirement = ledger.record_user_message(
        text=text, message_id="m-seed", turn_id="t-seed"
    )
    conn.execute(
        "UPDATE user_requirements SET structured_limits = ?,"
        " structured_limits_state = 'structured', structured_limits_source = ?"
        " WHERE id = ?",
        (
            json.dumps([dict(item) for item in limits], ensure_ascii=False, sort_keys=True),
            "r9h8_saved_limit",
            requirement.requirement_id,
        ),
    )
    conn.commit()
    stored = ledger.by_id(requirement.requirement_id)
    assert stored is not None
    return stored


def _saved_candidate_limit(path: Path) -> dict:
    """带保存候选的未解决限制（R9-H12：恢复只认保存候选，不认当前 PATH）。"""
    return {
        "operations": ["execute"],
        "objects": [
            {
                "kind": "program_name",
                "value": "runner",
                "binding_state": requirement_limits.BINDING_STATE_UNRESOLVED,
                "binding_failure": requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE,
                "binding_candidates": [
                    {
                        "path": os.path.realpath(str(path)),
                        "source": "path_lookup",
                        "state": requirement_limits.BINDING_STATE_UNRESOLVED,
                    }
                ],
            }
        ],
        "reason": "禁止运行 runner",
    }


def _seed_saved_candidate_unbound(conn, ledger, candidate_path: Path):
    return _seed_structured_unbound(
        conn, ledger, limits=(_saved_candidate_limit(candidate_path),)
    )


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _probe_spy(monkeypatch) -> dict:
    """统计后端候选事实解析次数（attach_program_bindings 的唯一查找入口）。"""
    real = requirement_limits._probe_program_binding
    seen = {"count": 0, "names": []}

    def spy(name):
        seen["count"] += 1
        seen["names"].append(str(name))
        return real(name)

    monkeypatch.setattr(requirement_limits, "_probe_program_binding", spy)
    return seen


class _RecordingReviewer:
    """普通授权审查替身：专用恢复一旦调用它就直接失败。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def review(self, request):
        self.calls.append(request)
        raise AssertionError("专用绑定恢复不得调用普通授权审查")


async def _recover(ctx, requirement) -> tuple:
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    recovery_ctx = SimpleNamespace(notices=[])
    recovered = await ctx._ensure_legacy_structured_limits(
        recovery_ctx,
        binding,
        message_id=None,
        turn_id="turn-binding-recovery",
    )
    return recovered, recovery_ctx


class _FakeResolver:
    """要求解析替身：只返回给定 outcome，不联网。"""

    model = "fake-r9h11-model"
    prompt_version = "fake-r9h11-prompt-v1"

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def resolve(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.outcome


def _new_outcome(text: str = "新的独立要求。") -> ResolutionOutcome:
    return ResolutionOutcome(
        relation=RequirementRelation.NEW,
        target_requirement_id=None,
        corrections=(text,),
        limits=(),
        reason="新要求",
        model="fake-r9h11-model",
        prompt_version="fake-r9h11-prompt-v1",
    )


# --------------------------------------------------------------------------
# 1) 数据纯化 + 显式绑定事实（保存入口）
# --------------------------------------------------------------------------


def test_attach_records_full_binding_and_requirement_facts(
    db_conn, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    ledger = RequirementLedger(db_conn)
    requirement = ledger.record_user_message(
        text="禁止运行 runner", message_id="m-attach", turn_id="t-attach"
    )
    ledger.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:attach"
    )

    stored = ledger.by_id(requirement.requirement_id)
    assert stored is not None
    obj = stored.structured_limits[0]["objects"][0]
    assert "program_binding" in obj, "保存入口必须固化程序对象绑定"
    binding = obj["program_binding"]
    assert os.path.normcase(os.path.realpath(binding["path"])) == os.path.normcase(
        os.path.realpath(str(runner))
    )
    assert binding["identity"]
    assert "birth_key" in binding
    assert binding["source"] == "path_lookup"
    assert binding["requirement_id"] == requirement.requirement_id
    assert binding["requirement_version"] == int(stored.version)
    assert binding["requirement_status"] == "active"
    assert "binding_state" not in obj and "binding_failure" not in obj


def test_save_time_lookup_failure_is_explicit_and_reads_stay_pure(
    db_conn, tmp_path, monkeypatch
):
    _isolated_path(monkeypatch, tmp_path / "empty-bin")
    ledger = RequirementLedger(db_conn)
    requirement = ledger.record_user_message(
        text="禁止运行 runner", message_id="m-fail", turn_id="t-fail"
    )
    ledger.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:save-failure"
    )

    stored = ledger.by_id(requirement.requirement_id)
    assert stored is not None
    obj = stored.structured_limits[0]["objects"][0]
    assert "program_binding" not in obj, "查找失败不得伪造绑定"
    assert obj["binding_state"] == requirement_limits.BINDING_STATE_UNRESOLVED
    assert obj["binding_failure"] == requirement_limits.BINDING_FAILURE_LOOKUP_FAILED
    assert requirement_limits.program_binding_state(obj) == "unresolved"

    # 纯数据往返保留“保存时失败”的事实，且读取不重新查找程序。
    serialized = requirement_limits.serialize_limits(stored.structured_limits)
    parsed = requirement_limits.deserialize_limits(serialized)
    assert parsed == stored.structured_limits
    seen = _probe_spy(monkeypatch)
    RequirementLedger(db_conn).by_id(requirement.requirement_id)
    RequirementLedger(db_conn).structured_limits_for(requirement.requirement_id)
    requirement_limits.known_limits_from_requirement(stored)
    assert seen["count"] == 0, "读取/反序列化不得查找程序或补建绑定"
    assert _raw_limits(db_conn, requirement.requirement_id) == _raw_limits(
        db_conn, requirement.requirement_id
    )

    # 环境恢复后重新保存（保留事实的纯数据）才允许绑定。
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    ledger.set_structured_limits(
        requirement.requirement_id,
        stored.structured_limits,
        source="test:save-retry",
    )
    rebound = ledger.by_id(requirement.requirement_id)
    assert rebound is not None
    obj = rebound.structured_limits[0]["objects"][0]
    assert obj["program_binding"]["identity"]
    assert "binding_state" not in obj and "binding_failure" not in obj


def test_binding_state_helpers_are_pure_and_explicit():
    unbound = requirement_limits.normalize_limits([_PROGRAM_LIMIT])
    assert requirement_limits.needs_binding_recovery(unbound) is True
    assert requirement_limits.binding_recovery_complete(unbound) is False
    facts = requirement_limits.binding_facts(unbound)
    assert facts["program_name"] == 1
    assert facts["bound"] == 0
    assert facts["unresolved"] == 0
    assert facts["missing"] == 1

    failed = {
        "operations": ["execute"],
        "objects": [
            {
                "kind": "program_name",
                "value": "runner",
                "binding_state": requirement_limits.BINDING_STATE_UNRESOLVED,
                "binding_failure": requirement_limits.BINDING_FAILURE_LOOKUP_FAILED,
                "binding_candidates": [
                    {"path": "/fake/bin/runner", "source": "path_lookup"}
                ],
            }
        ],
        "reason": "保存时查不到",
    }
    stored_failed = requirement_limits.normalize_limits([failed])
    assert requirement_limits.needs_binding_recovery(stored_failed) is True
    obj = stored_failed[0]["objects"][0]
    assert requirement_limits.program_binding_state(obj) == "unresolved"
    assert requirement_limits.binding_facts(stored_failed)["unresolved"] == 1

    bound = requirement_limits.normalize_limits(
        [
            {
                "operations": ["execute"],
                "objects": [
                    {
                        "kind": "program_name",
                        "value": "runner",
                        "program_binding": {
                            "path": "/fake/bin/runner",
                            "identity": "vol:fake",
                            "source": "path_lookup",
                        },
                    }
                ],
                "reason": "已绑定",
            }
        ]
    )
    assert requirement_limits.needs_binding_recovery(bound) is False
    assert requirement_limits.binding_recovery_complete(bound) is True
    assert requirement_limits.binding_facts(bound)["bound"] == 1

    source = requirement_limits.build_binding_recovery_source(
        requirement_text="禁止运行 runner",
        chain_digest="a" * 64,
        version=3,
    )
    assert source.startswith("binding_recovery:path_lookup"), source
    assert "chain=" in source and "version=3" in source and "text=" in source


# --------------------------------------------------------------------------
# 2) 专用恢复：成功 / 失败 / 竞态 / 重启
# --------------------------------------------------------------------------


async def test_structured_unbound_recovery_binds_from_backend_facts(
    ctx, tmp_path, monkeypatch
):
    """R9-H12 旧期望修订（保留原输入）：无保存候选时不得按当前 PATH 绑定。

    原场景（R9-H11）：structured + runner 无 program_binding、也没有
    binding_candidates；PATH 里有 runner。旧期望=专用恢复从当前 PATH 查找成功绑定。
    新契约（_R9H12-COMMON.md §4）：无保存候选时不得用当前 PATH 结果直接绑定，
    只把当前候选当审计事实，保持未解决、零写入。
    """
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    review = _RecordingReviewer()
    ctx.authorization.reviewer = review
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    requirement = _seed_structured_unbound(ctx.conn, ctx.requirements)
    row_before = _raw_row(ctx.conn, requirement.requirement_id)
    seen = _probe_spy(monkeypatch)

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "无保存候选：不得用当前 PATH 结果创建历史绑定"
    assert seen["count"] >= 1, "当前 PATH 只作审计候选事实采集"
    row_after = _raw_row(ctx.conn, requirement.requirement_id)
    assert row_after == row_before, "未采纳必须零写入（限制/候选/来源保留）"
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    assert stored.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert stored.structured_limits_source == "r9h8_saved_limit"
    obj = stored.structured_limits[0]["objects"][0]
    assert "program_binding" not in obj
    assert any("历史用户限制" in item for item in recovery_ctx.notices)
    # 专用恢复不是普通授权：不生成授权记录、不进入普通审查、不弹审批。
    assert ctx.authorizations.records_for_requirement(requirement.requirement_id) == []
    assert review.calls == []
    assert ctx.approvals.pending() == []
    assert ctx.settings_store.get("computer.permission_mode") == "unrestricted"


async def test_saved_candidate_recovery_adopts_saved_not_current_path(
    ctx, tmp_path, monkeypatch
):
    """R9-H12 新增相邻案例：保存候选优先，且不采纳当前 PATH 指向的 B。"""
    saved = _runner(tmp_path / "bin-saved")
    current = _runner(tmp_path / "bin-current")
    _prepend_path(monkeypatch, current.parent)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    review = _RecordingReviewer()
    ctx.authorization.reviewer = review
    requirement = _seed_saved_candidate_unbound(ctx.conn, ctx.requirements, saved)
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is not None, "保存候选可核验时必须恢复成功"
    binding = recovered[0]["objects"][0]["program_binding"]
    assert os.path.normcase(os.path.realpath(binding["path"])) == os.path.normcase(
        os.path.realpath(str(saved))
    ), "必须采纳保存候选，而不是当前 PATH 指向的对象"
    assert os.path.normcase(os.path.realpath(binding["path"])) != os.path.normcase(
        os.path.realpath(str(current))
    )
    assert binding["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
    assert binding["requirement_id"] == requirement.requirement_id
    assert binding["requirement_version"] == int(requirement.version)
    assert binding["requirement_status"] == "active"
    stored = ctx.requirements.by_id(requirement.requirement_id)
    source = str(stored.structured_limits_source or "")
    assert source.startswith("binding_recovery:"), source
    assert requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED in source
    assert "origin=r9h8_saved_limit" in source
    assert _raw_limits(ctx.conn, requirement.requirement_id) != raw_before
    assert review.calls == []
    assert ctx.authorizations.records_for_requirement(requirement.requirement_id) == []
    assert not any("恢复结果已过期" in item for item in recovery_ctx.notices)


async def test_recovery_binding_survives_close_reopen(tmp_path, monkeypatch):
    """R9-H12 输入修订：用带保存候选的行；PATH 指向另一个同名程序，恢复仍采纳候选。"""
    db_path = tmp_path / "binding-recovery.db"
    conn = connect(db_path)
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ctx = app.state.ctx
    runner = _runner(tmp_path / "bin-saved")
    current = _runner(tmp_path / "bin-current")
    _prepend_path(monkeypatch, current.parent)
    requirement = _seed_saved_candidate_unbound(conn, ctx.requirements, runner)
    recovered, _ = await _recover(ctx, requirement)
    assert recovered is not None
    bound = recovered[0]["objects"][0]["program_binding"]
    assert os.path.normcase(os.path.realpath(bound["path"])) == os.path.normcase(
        os.path.realpath(str(runner))
    ), "重开后仍必须是保存候选对象"
    assert bound["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
    assert bound["path"] != os.path.realpath(str(current))
    raw_after = _raw_limits(conn, requirement.requirement_id)
    conn.close()

    reopened = connect(db_path)
    try:
        ledger = RequirementLedger(reopened)
        reloaded = ledger.by_id(requirement.requirement_id)
        assert reloaded is not None
        assert reloaded.structured_limits_state == requirement_limits.STATE_STRUCTURED
        obj = reloaded.structured_limits[0]["objects"][0]
        assert obj["program_binding"]["identity"] == bound["identity"]
        assert obj["program_binding"]["path"] == bound["path"]
        assert obj["program_binding"]["requirement_id"] == requirement.requirement_id
        assert _raw_limits(reopened, requirement.requirement_id) == raw_after
    finally:
        reopened.close()


async def test_recovery_failure_keeps_original_json_state_and_source(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "binding-failure.db"
    conn = connect(db_path)
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ctx = app.state.ctx
    _isolated_path(monkeypatch, tmp_path / "empty-bin")
    requirement = _seed_structured_unbound(conn, ctx.requirements)
    row_before = _raw_row(conn, requirement.requirement_id)
    limits_before = requirement.structured_limits

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "无法从后端事实绑定对象时恢复失败"
    row_after = _raw_row(conn, requirement.requirement_id)
    assert row_after == row_before, "恢复失败不得改写原始 JSON/状态/来源"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)
    conn.close()

    reopened = connect(db_path)
    try:
        ledger = RequirementLedger(reopened)
        stored = ledger.by_id(requirement.requirement_id)
        assert stored is not None
        assert stored.structured_limits == limits_before, "已知限制必须保留"
        assert stored.structured_limits_state == requirement_limits.STATE_STRUCTURED
        assert stored.structured_limits_source == "r9h8_saved_limit"
        assert _raw_row(reopened, requirement.requirement_id) == row_before
    finally:
        reopened.close()


async def test_recovery_discards_stale_result_after_withdraw(
    ctx, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    requirement = _seed_saved_candidate_unbound(ctx.conn, ctx.requirements, runner)
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)
    real_verify = requirement_limits._verify_saved_binding_candidate

    def verify_then_withdraw(candidate):
        if ctx.requirements.by_id(requirement.requirement_id).status == "active":
            ctx.requirements.withdraw(requirement.requirement_id)
        return real_verify(candidate)

    monkeypatch.setattr(
        requirement_limits, "_verify_saved_binding_candidate", verify_then_withdraw
    )

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "恢复等待期间要求被撤回：结果必须丢弃"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before, (
        "丢弃的结果不得写回原始 JSON"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    assert stored.status == "withdrawn"
    assert stored.structured_limits_state == "structured"
    assert stored.structured_limits_source == "r9h8_saved_limit"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)


async def test_recovery_discards_result_when_version_changes(
    ctx, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    requirement = _seed_saved_candidate_unbound(ctx.conn, ctx.requirements, runner)
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)
    real_verify = requirement_limits._verify_saved_binding_candidate

    def verify_then_bump_version(candidate):
        ctx.conn.execute(
            "UPDATE user_requirements SET version = version + 1 WHERE id = ?",
            (requirement.requirement_id,),
        )
        ctx.conn.commit()
        return real_verify(candidate)

    monkeypatch.setattr(
        requirement_limits, "_verify_saved_binding_candidate", verify_then_bump_version
    )

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "恢复等待期间要求版本变化：结果必须丢弃"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    assert stored.version == int(requirement.version) + 1
    assert stored.structured_limits_state == "structured"
    assert stored.structured_limits_source == "r9h8_saved_limit"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)


async def test_recovery_write_back_rejects_requirement_changed_after_snapshot(
    ctx, tmp_path, monkeypatch
):
    """快照核对之后、写回事务之前要求被 amend：条件写入拒绝，旧行保持原样。"""
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    requirement = _seed_saved_candidate_unbound(ctx.conn, ctx.requirements, runner)
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)
    real_apply = ctx.requirements.apply_binding_recovery

    def apply_after_amend(requirement_id, limits, **kwargs):
        ctx.requirements.add_correction(requirement_id, "在写回前再改一次。")
        return real_apply(requirement_id, limits, **kwargs)

    monkeypatch.setattr(
        ctx.requirements, "apply_binding_recovery", apply_after_amend
    )

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "条件写入必须拒绝已变化要求的旧恢复结果"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    assert stored.status == "superseded"
    assert stored.structured_limits_state == "structured"
    assert stored.structured_limits_source == "r9h8_saved_limit"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)


async def test_recovery_discards_result_when_chain_digest_changes(
    ctx, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    parent = ctx.requirements.record_user_message(
        text="整理项目报告。", message_id="m-race-parent", turn_id="t-race"
    )
    child = ctx.requirements.add_correction(parent.requirement_id, "补充：禁止运行 runner。")
    conn = ctx.requirements.conn
    conn.execute(
        "UPDATE user_requirements SET structured_limits = ?,"
        " structured_limits_state = 'structured', structured_limits_source = ?"
        " WHERE id = ?",
        (
            json.dumps(
                [_saved_candidate_limit(runner)], ensure_ascii=False, sort_keys=True
            ),
            "r9h8_saved_limit",
            child.requirement_id,
        ),
    )
    conn.commit()
    target = ctx.requirements.by_id(child.requirement_id)
    assert target is not None
    raw_before = _raw_limits(conn, child.requirement_id)
    real_verify = requirement_limits._verify_saved_binding_candidate

    def verify_then_change_ancestor(candidate):
        conn.execute(
            "UPDATE user_requirements SET text = ? WHERE id = ?",
            ("整理项目报告，并检查附件。", parent.requirement_id),
        )
        conn.commit()
        return real_verify(candidate)

    monkeypatch.setattr(
        requirement_limits, "_verify_saved_binding_candidate", verify_then_change_ancestor
    )

    recovered, recovery_ctx = await _recover(ctx, target)

    assert recovered is None, "恢复等待期间链变化（祖先原话变化）：结果必须丢弃"
    assert _raw_limits(conn, child.requirement_id) == raw_before
    stored = ctx.requirements.by_id(child.requirement_id)
    assert stored is not None
    assert stored.structured_limits_source == "r9h8_saved_limit"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)


async def test_fully_bound_structured_requirement_is_not_rewritten(
    ctx, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-bound", turn_id="t-bound"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:bound"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    assert stored.structured_limits[0]["objects"][0]["program_binding"]
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)
    seen = _probe_spy(monkeypatch)

    recovered, _ = await _recover(ctx, stored)

    assert recovered is None, "已完整绑定：不需要专用恢复，也不得改写"
    assert seen["count"] == 0, "已保存绑定不能被环境变化重新解析"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before


async def test_real_turn_amend_of_unbound_history_binds_child_limit(
    ctx, tmp_path, monkeypatch
):
    """真实主流程：历史 structured-but-unbound 被 amend 时进入专用绑定，不是无关。"""
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    review = _RecordingReviewer()
    ctx.authorization.reviewer = review
    parent = _seed_structured_unbound(ctx.conn, ctx.requirements)
    parent_raw = _raw_limits(ctx.conn, parent.requirement_id)
    ctx.requirement_resolver = _FakeResolver(
        ResolutionOutcome(
            relation=RequirementRelation.AMEND,
            target_requirement_id=parent.requirement_id,
            target_version=int(parent.version),
            corrections=("仍然禁止运行 runner。",),
            limits=(),
            reason="用户补充同一限制",
            model="fake-r9h11-model",
            prompt_version="fake-r9h11-prompt-v1",
        )
    )
    observed = {"executed": False, "binding": None, "limits": ()}

    async def stub_execute(turn_ctx):
        observed["executed"] = True
        observed["binding"] = current_requirement.get()
        observed["limits"] = tuple(context_requirement_limits.get() or ())

    ctx.turn_orchestrator.execute = stub_execute
    topic_id = ctx.current_topic()
    turn_ctx = TurnContext(
        turn_id="turn-r9h11-amend",
        message="仍然禁止运行 runner。",
        initial_topic=topic_id,
        current_topic=topic_id,
    )
    turn_ctx.cancelled = False
    await ctx._execute_turn(turn_ctx)

    assert observed["executed"] is True
    binding = observed["binding"]
    assert binding is not None and binding.requirement_id != parent.requirement_id
    child = ctx.requirements.by_id(binding.requirement_id)
    assert child is not None
    assert int(child.version) == int(parent.version) + 1
    child_obj = child.structured_limits[0]["objects"][0]
    assert child_obj["program_binding"]["requirement_id"] == child.requirement_id
    assert child_obj["program_binding"]["identity"]
    # 历史行原始 JSON 不变：保存/恢复路径不得在读取时隐式补建绑定。
    assert _raw_limits(ctx.conn, parent.requirement_id) == parent_raw
    # 本轮生效限制携带已固化绑定（执行完整保留绑定字段）。
    ctx_limits = observed["limits"]
    assert ctx_limits, observed["limits"]
    assert ctx_limits[0]["objects"][0].get("program_binding")
    # unrestricted 模式也不因专用恢复进入普通审查。
    assert review.calls == []


# --------------------------------------------------------------------------
# 3) 未恢复记录不会被普通读取/新建轮/审批隐式绑定
# --------------------------------------------------------------------------


async def test_unbound_history_not_bound_by_plain_reads_or_new_turn(
    ctx, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)  # 名称当前可解析：普通读取仍不得绑定
    requirement = _seed_structured_unbound(ctx.conn, ctx.requirements)
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)
    seen = _probe_spy(monkeypatch)

    reloaded = ctx.requirements.by_id(requirement.requirement_id)
    assert reloaded is not None
    assert "program_binding" not in reloaded.structured_limits[0]["objects"][0]
    assert ctx.requirements.structured_limits_for(requirement.requirement_id) is not None
    requirement_limits.limits_from_requirement(reloaded)
    requirement_limits.known_limits_from_requirement(reloaded)
    ctx._applied_limits(SimpleNamespace(requirement=reloaded))
    assert seen["count"] == 0, "普通读取不得查找程序或隐式补建绑定"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before

    # 真实主流程的新要求轮（不是 amend）也不得动历史行。
    ctx.requirement_resolver = _FakeResolver(_new_outcome())
    turn_ctx = SimpleNamespace(
        notices=[],
        turn_id="turn-new",
        user_message_id=None,
        message_id=None,
        initial_topic=ctx.current_topic(),
    )
    instruction = SimpleNamespace(text="新的独立要求：整理周报。")
    binding, status, limits, error = await ctx._prepare_requirement_binding(
        turn_ctx, instruction
    )
    assert error is None and status == "ok" and binding is not None
    assert seen["count"] == 0
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before
    assert (
        ctx.requirements.by_id(requirement.requirement_id).structured_limits_state
        == "structured"
    )


# --------------------------------------------------------------------------
# 4) 写回条件 + 服务层上下文不丢绑定
# --------------------------------------------------------------------------


def test_ledger_apply_binding_recovery_requires_complete_bindings_and_condition(
    db_conn, tmp_path, monkeypatch
):
    ledger = RequirementLedger(db_conn)
    _isolated_path(monkeypatch, tmp_path / "empty-bin")
    requirement = ledger.record_user_message(
        text="禁止运行 runner", message_id="m-write", turn_id="t-write"
    )
    ledger.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:save-failure"
    )
    stored = ledger.by_id(requirement.requirement_id)
    raw_before = _raw_limits(db_conn, requirement.requirement_id)

    with pytest.raises(ValueError):
        ledger.apply_binding_recovery(
            requirement.requirement_id,
            None,
            source="binding_recovery:path_lookup",
            expected_version=int(stored.version),
        )
    with pytest.raises(ValueError):
        ledger.apply_binding_recovery(
            requirement.requirement_id,
            stored.structured_limits,
            source="binding_recovery:path_lookup",
            expected_version=int(stored.version),
        )
    assert _raw_limits(db_conn, requirement.requirement_id) == raw_before, (
        "未完整绑定不得写回"
    )

    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    rebound = requirement_limits.attach_program_bindings(
        stored.structured_limits,
        requirement_id=requirement.requirement_id,
        version=int(stored.version),
        status="active",
    )
    with pytest.raises(RequirementVersionMismatch):
        ledger.apply_binding_recovery(
            requirement.requirement_id,
            rebound,
            source="binding_recovery:path_lookup",
            expected_version=int(stored.version) + 1,
            expected_status="active",
        )
    assert _raw_limits(db_conn, requirement.requirement_id) == raw_before

    written = ledger.apply_binding_recovery(
        requirement.requirement_id,
        rebound,
        source="binding_recovery:path_lookup;version=%d" % int(stored.version),
        expected_version=int(stored.version),
        expected_status="active",
    )
    assert written.structured_limits_state == requirement_limits.STATE_STRUCTURED
    obj = written.structured_limits[0]["objects"][0]
    assert obj["program_binding"]["requirement_id"] == requirement.requirement_id
    assert written.structured_limits_source.startswith("binding_recovery:")


def test_applied_limits_keeps_saved_program_binding(ctx, tmp_path, monkeypatch):
    runner = _runner(tmp_path / "bin")
    _prepend_path(monkeypatch, runner.parent)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-applied", turn_id="t-applied"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:applied"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    assert stored is not None
    pre_attach = (dict(_PROGRAM_LIMIT),)  # AppliedResolution 里的未绑定快照
    applied = SimpleNamespace(
        requirement=stored, structured_limits=pre_attach, limits=()
    )

    limits = ctx._applied_limits(applied)

    obj = limits[0]["objects"][0]
    assert obj.get("program_binding"), "本轮生效限制必须保留保存时固化的绑定"
    assert obj["program_binding"]["identity"] == (
        stored.structured_limits[0]["objects"][0]["program_binding"]["identity"]
    )
