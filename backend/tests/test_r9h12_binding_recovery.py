"""R9-H12 B：保存候选优先的绑定恢复 / 纯数据保留 / P2 真实服务流程。

契约 _R9H12-COMMON.md §4：
* 新要求保存时绑定（attach_program_bindings）与恢复历史未解决绑定
  （recover_program_bindings）是两个入口；
* 恢复只处理**无绑定**的 program_name；已有有效绑定原样保留，不随 PATH 转移；
* 保存候选优先：candidate.path 现在存在且身份可读 -> 采纳该路径/identity/birth，
  source=saved_candidate_verified；保存候选不可验证/多冲突 -> 不写绑定、不改 JSON；
* 无保存候选 -> **不得**用当前 PATH 查找结果直接绑定，只把当前候选事实作为
  审计/返回事实，保持未解决；
* 采纳对象必须后端核验（路径存在 + 文件身份可读 + path/identity 一致）；
  写回前重新核验候选事实仍有效；调用方/模型自报不能替代核验；
* normalize/serialize/deserialize/merge/remove 完整保留 binding_state /
  binding_failure / binding_candidates（纯数据）。

模型一律 fake；不联网。真实：SQLite、PATH、临时程序、文件身份。
模拟：P2 流程里「身份提供器读取失败」只在 file_identity.identity_of 边界注入
（测试内明确标注 simulated），恢复核验走真实身份提供器。
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
from agent.security import requirement_limits
from agent.security.requirements import RequirementBinding, RequirementLedger
from agent.services import file_identity
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


def _executable(directory: Path, name: str = "runner") -> Path:
    """真实临时程序（复制当前解释器），用于真实 PATH 查找与文件身份。"""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name = name + ".exe"
    target = directory / name
    shutil.copy2(sys.executable, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _plain_file(directory: Path, name: str = "tool") -> Path:
    """真实临时文件（只要求文件身份，不要求可执行）。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("real file: " + name, encoding="utf-8")
    return target


def _prepend_path(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))


def _set_path_only(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", str(directory))


def _raw_row(conn, requirement_id: str):
    row = conn.execute(
        "SELECT structured_limits, structured_limits_state, structured_limits_source,"
        " status, version FROM user_requirements WHERE id = ?",
        (requirement_id,),
    ).fetchone()
    return None if row is None else dict(row)


def _raw_limits(conn, requirement_id: str):
    row = conn.execute(
        "SELECT structured_limits FROM user_requirements WHERE id = ?",
        (requirement_id,),
    ).fetchone()
    return None if row is None else row[0]


def _candidate(path: Path, *, source: str = "path_lookup", identity=None) -> dict:
    item = {
        "path": os.path.realpath(str(path)),
        "source": source,
        "state": requirement_limits.BINDING_STATE_UNRESOLVED,
    }
    if identity is not None:
        item["identity"] = identity
    return item


def _unbound_limit(*, candidates=(), failure=None) -> dict:
    obj = {"kind": "program_name", "value": "runner"}
    obj["binding_state"] = requirement_limits.BINDING_STATE_UNRESOLVED
    obj["binding_failure"] = (
        failure or requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    )
    if candidates:
        obj["binding_candidates"] = [dict(item) for item in candidates]
    return {
        "operations": ["execute"],
        "objects": [obj],
        "reason": "禁止运行 runner",
    }


def _seed_unbound(
    conn,
    ledger,
    *,
    limits=(_PROGRAM_LIMIT,),
    text="禁止运行 runner",
    source="r9h8_saved_limit",
):
    """模拟历史/保存时未形成绑定的行（structured + program_name 无 binding）。"""
    requirement = ledger.record_user_message(
        text=text, message_id="m-seed", turn_id="t-seed"
    )
    conn.execute(
        "UPDATE user_requirements SET structured_limits = ?,"
        " structured_limits_state = 'structured', structured_limits_source = ?"
        " WHERE id = ?",
        (
            json.dumps(
                [dict(item) for item in limits], ensure_ascii=False, sort_keys=True
            ),
            source,
            requirement.requirement_id,
        ),
    )
    conn.commit()
    stored = ledger.by_id(requirement.requirement_id)
    assert stored is not None
    return stored


def _probe_spy(monkeypatch) -> dict:
    """统计后端 PATH 候选查找次数（恢复不得借用当前 PATH 绑定）。"""
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


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


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
        turn_id="turn-r9h12-binding-recovery",
    )
    return recovered, recovery_ctx


# --------------------------------------------------------------------------
# 1) normalize/serialize/deserialize/merge/remove 完整保留绑定事实（纯数据）
# --------------------------------------------------------------------------


def test_normalize_roundtrip_keeps_binding_facts_and_candidate_identity(monkeypatch):
    seen = _probe_spy(monkeypatch)
    candidate = {
        "path": "/opt/bin/runner",
        "source": "path_lookup",
        "state": requirement_limits.BINDING_STATE_UNRESOLVED,
        "identity": "vol:abc",
        "birth_key": "posix:birth:123",
    }
    limit = _unbound_limit(candidates=[candidate])

    normalized = requirement_limits.normalize_limits([limit])
    obj = normalized[0]["objects"][0]
    assert obj["binding_state"] == requirement_limits.BINDING_STATE_UNRESOLVED
    assert obj["binding_failure"] == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    saved = obj["binding_candidates"][0]
    assert saved["path"] == candidate["path"]
    assert saved["source"] == "path_lookup"
    assert saved["state"] == requirement_limits.BINDING_STATE_UNRESOLVED
    assert saved["identity"] == "vol:abc"
    assert saved["birth_key"] == "posix:birth:123"

    serialized = requirement_limits.serialize_limits(normalized)
    parsed = requirement_limits.deserialize_limits(serialized)
    assert parsed == normalized, "往返必须完整保留 binding_state/failure/candidates"
    assert seen["count"] == 0, "纯数据操作不得查找程序/采集身份"


def test_merge_and_remove_preserve_binding_facts():
    candidate = _candidate_to_dict("/opt/bin/runner")
    parent = (_unbound_limit(candidates=[candidate]),)
    added = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": "/tmp/report.txt"}],
            "reason": "禁止改报告",
        },
    )
    merged = requirement_limits.merge_limits(parent, added=added)
    obj = merged[0]["objects"][0]
    assert obj["binding_state"] == requirement_limits.BINDING_STATE_UNRESOLVED
    assert obj["binding_failure"] == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    assert obj["binding_candidates"][0]["path"] == candidate["path"]

    # 同一 entry 里删除另一个对象：剩余 program_name 的绑定事实必须保留。
    mixed = {
        "operations": ["execute"],
        "objects": [
            dict(_unbound_limit(candidates=[candidate])["objects"][0]),
            {"kind": "path", "value": "/tmp/other.txt"},
        ],
        "reason": "混合限制",
    }
    removed = (
        {
            "operations": ["execute"],
            "objects": [{"kind": "path", "value": "/tmp/other.txt"}],
            "reason": "解除该文件",
        },
    )
    reduced = requirement_limits.remove_limits_precise([mixed], removed)
    remaining = reduced[0]["objects"]
    assert [item["kind"] for item in remaining] == ["program_name"]
    assert remaining[0]["binding_state"] == requirement_limits.BINDING_STATE_UNRESOLVED
    assert remaining[0]["binding_candidates"][0]["path"] == candidate["path"]


def _candidate_to_dict(path: str) -> dict:
    return {
        "path": path,
        "source": "path_lookup",
        "state": requirement_limits.BINDING_STATE_UNRESOLVED,
    }


# --------------------------------------------------------------------------
# 2) recover_program_bindings：保存候选优先，不随 PATH 转移
# --------------------------------------------------------------------------


def test_recover_adopts_saved_candidate_not_current_path(tmp_path):
    a = _plain_file(tmp_path / "bin-a", "runner")
    b = _plain_file(tmp_path / "bin-b", "runner")
    limits = (_unbound_limit(candidates=[_candidate(a)]),)
    now = [
        {
            "name": "runner",
            "path": os.path.realpath(str(b)),
            "identity": "caller:self-reported",
            "source": "path_lookup",
        }
    ]

    outcome = requirement_limits.recover_program_bindings(
        limits,
        requirement_id="req_saved",
        version=3,
        status="active",
        now_candidates=now,
    )

    assert outcome.complete is True
    assert outcome.changed is True
    assert outcome.reason == ""
    binding = outcome.limits[0]["objects"][0]["program_binding"]
    assert os.path.normcase(os.path.realpath(binding["path"])) == os.path.normcase(
        os.path.realpath(str(a))
    )
    assert binding["identity"] == file_identity.identity_of(a).key
    assert binding["birth_key"] == file_identity.identity_of(a).birth_key
    assert binding["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
    assert binding["requirement_id"] == "req_saved"
    assert binding["requirement_version"] == 3
    assert binding["requirement_status"] == "active"
    obj = outcome.limits[0]["objects"][0]
    assert "binding_state" not in obj
    assert "binding_failure" not in obj
    assert "binding_candidates" not in obj, "采纳后不再保留未解决候选事实"
    adopted = outcome.adopted[0]
    assert adopted["path"] == binding["path"]
    assert adopted["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
    assert adopted["candidate_source"] == "path_lookup"
    assert requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED in outcome.sources
    # 调用方自报的当前 PATH 候选只作审计事实，未参与绑定。
    assert outcome.current_candidates
    assert all(fact.get("verified") is False for fact in outcome.current_candidates)
    assert outcome.current_candidates[0]["path"] == os.path.realpath(str(b))


def test_recover_without_saved_candidates_never_binds_current_path(tmp_path, monkeypatch):
    b = _plain_file(tmp_path / "bin-b", "runner")
    limits = requirement_limits.normalize_limits([_PROGRAM_LIMIT])
    seen = _probe_spy(monkeypatch)
    now = [
        {
            "name": "runner",
            "path": os.path.realpath(str(b)),
            "identity": file_identity.identity_of(b).key,
            "birth_key": file_identity.identity_of(b).birth_key,
            "source": "path_lookup",
        }
    ]

    outcome = requirement_limits.recover_program_bindings(
        limits,
        requirement_id="req_none",
        version=1,
        status="active",
        now_candidates=now,
    )

    assert outcome.complete is False
    assert outcome.changed is False
    assert outcome.limits == limits, "无保存候选：不得用当前 PATH 结果直接绑定"
    assert "program_binding" not in outcome.limits[0]["objects"][0]
    assert outcome.unresolved[0]["reason"] == "no_saved_candidates"
    assert outcome.current_candidates[0]["path"] == os.path.realpath(str(b))
    assert seen["count"] == 0, "recover_program_bindings 自身不得重新查找 PATH"


def test_recover_unverifiable_saved_candidate_writes_nothing(tmp_path):
    missing = tmp_path / "gone" / "runner"
    limits = (_unbound_limit(candidates=[_candidate(missing)]),)

    outcome = requirement_limits.recover_program_bindings(
        limits, requirement_id="req_gone", version=2, status="active"
    )

    assert outcome.complete is False
    assert outcome.changed is False
    assert outcome.limits == requirement_limits.normalize_limits(limits)
    assert outcome.unresolved[0]["reason"] == "saved_candidate_unverifiable"
    assert outcome.unresolved[0]["failure"] == "candidate_path_missing"
    assert outcome.unresolved[0]["saved_candidates"] == [os.path.realpath(str(missing))]


def test_recover_identity_unreadable_candidate_stays_unresolved(tmp_path, monkeypatch):
    a = _plain_file(tmp_path / "bin-a", "runner")
    limits = (_unbound_limit(candidates=[_candidate(a)]),)
    monkeypatch.setattr(file_identity, "identity_of", lambda path: None)  # simulated

    outcome = requirement_limits.recover_program_bindings(
        limits, requirement_id="req_unreadable", version=1, status="active"
    )

    assert outcome.complete is False
    assert outcome.changed is False
    assert "program_binding" not in outcome.limits[0]["objects"][0]
    assert outcome.unresolved[0]["failure"] == "candidate_identity_unavailable"


def test_recover_conflicting_saved_candidates_does_not_choose(tmp_path):
    a = _plain_file(tmp_path / "bin-a", "runner")
    b = _plain_file(tmp_path / "bin-b", "runner")
    limits = (
        _unbound_limit(candidates=[_candidate(a), _candidate(b)]),
    )

    outcome = requirement_limits.recover_program_bindings(
        limits, requirement_id="req_conflict", version=1, status="active"
    )

    assert outcome.complete is False
    assert outcome.changed is False
    assert "program_binding" not in outcome.limits[0]["objects"][0]
    assert outcome.unresolved[0]["reason"] == "saved_candidate_conflict"
    assert len(outcome.unresolved[0]["saved_candidates"]) == 2


def test_recover_saved_candidate_identity_mismatch_not_adopted(tmp_path):
    a = _plain_file(tmp_path / "bin-a", "runner")
    stale = _candidate(a, identity="999:deadbeefdeadbeef")
    limits = (_unbound_limit(candidates=[stale]),)

    outcome = requirement_limits.recover_program_bindings(
        limits, requirement_id="req_mismatch", version=1, status="active"
    )

    assert outcome.complete is False
    assert outcome.changed is False
    assert "program_binding" not in outcome.limits[0]["objects"][0]
    assert outcome.unresolved[0]["failure"] == "candidate_identity_mismatch"


def test_recover_uses_recorded_candidate_identity_and_birth(tmp_path):
    """保存候选若已带 identity/birth，采纳时必须与当前观测一致且原样写入绑定。"""
    a = _plain_file(tmp_path / "bin-a", "runner")
    observed = file_identity.identity_of(a)
    assert observed is not None
    candidate = _candidate(a, identity=observed.key)
    candidate["birth_key"] = observed.birth_key
    limits = (_unbound_limit(candidates=[candidate]),)

    outcome = requirement_limits.recover_program_bindings(
        limits, requirement_id="req_recorded", version=2, status="active"
    )

    assert outcome.complete is True
    binding = outcome.limits[0]["objects"][0]["program_binding"]
    assert binding["identity"] == observed.key
    assert binding["birth_key"] == observed.birth_key

    # 记录的生命周期事实与当前不同 -> 不采纳（同一 identity 也不是同一生命周期对象）。
    stale = _candidate(a, identity=observed.key)
    stale["birth_key"] = "fake:creation:1"
    stale_outcome = requirement_limits.recover_program_bindings(
        (_unbound_limit(candidates=[stale]),),
        requirement_id="req_stale_birth",
        version=2,
        status="active",
    )
    assert stale_outcome.complete is False
    assert stale_outcome.unresolved[0]["failure"] == "candidate_lifecycle_mismatch"
    assert "program_binding" not in stale_outcome.limits[0]["objects"][0]


def test_current_binding_candidates_are_audit_only(tmp_path, monkeypatch):
    a = _executable(tmp_path / "bin-a")
    _set_path_only(monkeypatch, a.parent)
    limits = requirement_limits.normalize_limits([_PROGRAM_LIMIT])

    facts = requirement_limits.current_binding_candidates(limits)

    assert facts and facts[0]["name"] == "runner"
    assert os.path.normcase(os.path.realpath(facts[0]["path"])) == os.path.normcase(
        os.path.realpath(str(a))
    )
    assert facts[0]["verified"] is False and facts[0]["audit_only"] is True
    assert "program_binding" not in limits[0]["objects"][0], "审计事实不得写绑定"


def test_recover_already_bound_object_untouched_by_any_candidate(tmp_path, monkeypatch):
    a = _plain_file(tmp_path / "bin-a", "runner")
    b = _plain_file(tmp_path / "bin-b", "runner")
    identity = file_identity.identity_of(a)
    assert identity is not None
    bound = {
        "operations": ["execute"],
        "objects": [
            {
                "kind": "program_name",
                "value": "runner",
                "program_binding": {
                    "path": os.path.realpath(str(a)),
                    "identity": identity.key,
                    "birth_key": identity.birth_key,
                    "source": "path_lookup",
                    "requirement_id": "req_old",
                    "requirement_version": 1,
                    "requirement_status": "active",
                },
            }
        ],
        "reason": "已绑定",
    }
    seen = _probe_spy(monkeypatch)

    outcome = requirement_limits.recover_program_bindings(
        [bound],
        requirement_id="req_new",
        version=9,
        status="active",
        now_candidates=[
            {"name": "runner", "path": os.path.realpath(str(b)), "identity": "x:y"}
        ],
    )

    assert outcome.complete is True
    assert outcome.changed is False
    assert outcome.adopted == ()
    assert outcome.limits[0]["objects"][0]["program_binding"] == bound["objects"][0]["program_binding"]
    assert seen["count"] == 0


def test_verify_recovered_bindings_detects_path_replacement(tmp_path):
    a = _plain_file(tmp_path / "bin-a", "runner")
    limits = (_unbound_limit(candidates=[_candidate(a)]),)
    outcome = requirement_limits.recover_program_bindings(
        limits, requirement_id="req_reverify", version=1, status="active"
    )
    assert outcome.complete is True
    assert requirement_limits.verify_recovered_bindings(outcome.limits) == ()

    # 原路径对象被替换（删除后重建同名文件）：身份变化，写回前重新核验必须失败。
    a.unlink()
    _plain_file(tmp_path / "bin-a", "runner")
    invalid = requirement_limits.verify_recovered_bindings(outcome.limits)

    assert invalid, "原路径对象被替换后重新核验必须失败"
    # 平台差异：tmpfs/部分文件系统会复用 file_id —— 删除重建后 identity 相同、
    # 生命周期(birth)不同，给出 binding_lifecycle_mismatch；另一些平台/时机给出
    # binding_identity_mismatch。两者都是“原对象已被替换”的结构化失败依据，
    # 断言仍要求重新核验失败且失败原因属于这两类（不放宽为任意原因）。
    assert invalid[0]["failure"] in (
        "binding_identity_mismatch",
        "binding_lifecycle_mismatch",
    ), invalid[0]


# --------------------------------------------------------------------------
# 3) P2 真实服务流程：身份读取失败时保存候选 -> PATH 改到 B -> 恢复不得绑定 B
#    真实：SQLite 文件、PATH、临时程序；模拟：仅 identity_of 边界返回 None
# --------------------------------------------------------------------------


def _save_with_identity_failure(monkeypatch, ledger, *, identity_of_real):
    """P2 保存阶段：真实 PATH 找到 A，但身份提供器读取失败（模拟）。

    返回 (requirement, a_path)。保存后数据库里应只有候选 A、没有绑定。
    """
    monkeypatch.setattr(
        file_identity, "identity_of", lambda path: None  # simulated identity failure
    )
    requirement = ledger.record_user_message(
        text="禁止运行 runner", message_id="m-p2", turn_id="t-p2"
    )
    ledger.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="r9h8_saved_limit"
    )
    stored = ledger.by_id(requirement.requirement_id)
    monkeypatch.setattr(file_identity, "identity_of", identity_of_real)
    assert stored is not None
    return stored


async def test_p2_saved_candidate_a_not_path_b(ctx, tmp_path, monkeypatch):
    a = _executable(tmp_path / "bin-a")
    b = _executable(tmp_path / "bin-b")
    identity_of_real = file_identity.identity_of
    _set_path_only(monkeypatch, a.parent)

    stored = _save_with_identity_failure(monkeypatch, ctx.requirements, identity_of_real=identity_of_real)
    obj = stored.structured_limits[0]["objects"][0]
    assert "program_binding" not in obj
    assert obj["binding_state"] == requirement_limits.BINDING_STATE_UNRESOLVED
    assert obj["binding_failure"] == requirement_limits.BINDING_FAILURE_IDENTITY_UNAVAILABLE
    saved_candidate = obj["binding_candidates"][0]["path"]
    assert os.path.normcase(saved_candidate) == os.path.normcase(os.path.realpath(str(a)))
    raw_after_save = _raw_limits(ctx.conn, stored.requirement_id)

    # PATH 改到 B；身份提供器恢复真实（A 现在可核验）。恢复必须采纳保存候选 A。
    _set_path_only(monkeypatch, b.parent)
    seen = _probe_spy(monkeypatch)
    review = _RecordingReviewer()
    ctx.authorization.reviewer = review
    ctx.settings_store.set("computer.permission_mode", "unrestricted")

    recovered, recovery_ctx = await _recover(ctx, stored)

    assert recovered is not None, "保存候选 A 可核验时必须恢复成功"
    binding = recovered[0]["objects"][0]["program_binding"]
    assert os.path.normcase(os.path.realpath(binding["path"])) == os.path.normcase(
        os.path.realpath(str(a))
    ), "恢复不得绑定 PATH 当前指向的 B"
    assert os.path.normcase(os.path.realpath(binding["path"])) != os.path.normcase(
        os.path.realpath(str(b))
    )
    assert binding["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
    assert binding["identity"] == identity_of_real(a).key
    assert seen["count"] == 0, "保存候选可核验时恢复不得查找当前 PATH"
    stored_after = ctx.requirements.by_id(stored.requirement_id)
    source = str(stored_after.structured_limits_source or "")
    assert source.startswith("binding_recovery:"), source
    assert requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED in source
    assert "path_lookup" in source, "source 必须记录候选来源"
    assert "origin=r9h8_saved_limit" in source, "source 必须保留原 structured_limits_source"
    assert "version=" in source and "text=" in source and "chain=" in source
    assert _raw_limits(ctx.conn, stored.requirement_id) != raw_after_save
    # 专用恢复不是普通授权：不生成授权记录、不进入普通审查、不弹审批、不改模式。
    assert ctx.authorizations.records_for_requirement(stored.requirement_id) == []
    assert review.calls == []
    assert ctx.approvals.pending() == []
    assert ctx.settings_store.get("computer.permission_mode") == "unrestricted"


async def test_p2_unverifiable_candidate_zero_write_and_lifecycle(tmp_path, monkeypatch):
    """模拟身份读取持续失败：恢复零写入；close/reopen 后一致；随后可核验才绑定 A。"""
    db_path = tmp_path / "p2.db"
    conn = connect(db_path)
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ctx = app.state.ctx
    a = _executable(tmp_path / "bin-a")
    b = _executable(tmp_path / "bin-b")
    identity_of_real = file_identity.identity_of
    _set_path_only(monkeypatch, a.parent)

    stored = _save_with_identity_failure(
        monkeypatch, ctx.requirements, identity_of_real=identity_of_real
    )
    candidates_after_save = stored.structured_limits[0]["objects"][0]["binding_candidates"]
    row_before = _raw_row(conn, stored.requirement_id)

    # PATH 改到 B；身份提供器仍模拟失败（A 当前不可核验）。
    _set_path_only(monkeypatch, b.parent)
    monkeypatch.setattr(
        file_identity, "identity_of", lambda path: None  # simulated identity failure
    )
    recovered, recovery_ctx = await _recover(ctx, stored)

    assert recovered is None, "保存候选不可核验时恢复必须失败关闭"
    row_after = _raw_row(conn, stored.requirement_id)
    assert row_after == row_before, "失败必须保留原限制/known/候选/来源，零写入"
    assert any("历史用户限制" in item for item in recovery_ctx.notices)
    reloaded = ctx.requirements.by_id(stored.requirement_id)
    obj = reloaded.structured_limits[0]["objects"][0]
    assert "program_binding" not in obj
    assert obj["binding_candidates"] == candidates_after_save
    assert reloaded.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert reloaded.structured_limits_source == "r9h8_saved_limit"
    conn.close()

    # close/reopen：未解决事实与候选保持不变。
    reopened = connect(db_path)
    try:
        ledger = RequirementLedger(reopened)
        stored_reopened = ledger.by_id(stored.requirement_id)
        assert stored_reopened is not None
        assert _raw_row(reopened, stored.requirement_id) == row_before
        assert stored_reopened.structured_limits[0]["objects"][0]["binding_candidates"]

        # 身份提供器恢复真实（A 可核验）；PATH 仍指向 B。现在才允许绑定 A。
        monkeypatch.setattr(file_identity, "identity_of", identity_of_real)
        app2 = create_app(Settings(data_dir=tmp_path / "data"), reopened)
        ctx2 = app2.state.ctx
        recovered2, _ = await _recover(ctx2, stored_reopened)
        assert recovered2 is not None
        binding = recovered2[0]["objects"][0]["program_binding"]
        assert os.path.normcase(os.path.realpath(binding["path"])) == os.path.normcase(
            os.path.realpath(str(a))
        )
        assert binding["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
        stored_bound = ctx2.requirements.by_id(stored.requirement_id)
        assert _raw_limits(reopened, stored.requirement_id) != row_before
    finally:
        reopened.close()


async def test_p2_existing_binding_not_transferred_by_path(ctx, tmp_path, monkeypatch):
    a = _executable(tmp_path / "bin-a")
    b = _executable(tmp_path / "bin-b")
    _set_path_only(monkeypatch, a.parent)
    requirement = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-bound", turn_id="t-bound"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, [_PROGRAM_LIMIT], source="test:bound"
    )
    stored = ctx.requirements.by_id(requirement.requirement_id)
    binding_before = stored.structured_limits[0]["objects"][0]["program_binding"]
    assert os.path.normcase(os.path.realpath(binding_before["path"])) == os.path.normcase(
        os.path.realpath(str(a))
    )
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)

    _set_path_only(monkeypatch, b.parent)
    seen = _probe_spy(monkeypatch)
    recovered, _ = await _recover(ctx, stored)

    assert recovered is None, "已有有效绑定：无需恢复，也不得改写"
    assert seen["count"] == 0, "已有绑定不得随 PATH 重新解析"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before
    stored_after = ctx.requirements.by_id(requirement.requirement_id)
    binding_after = stored_after.structured_limits[0]["objects"][0]["program_binding"]
    assert binding_after == binding_before


async def test_recovery_reverifies_adopted_candidate_before_write(
    ctx, tmp_path, monkeypatch
):
    """写回前重新核验：采纳后候选路径对象被替换 -> 不写回、失败关闭。"""
    a = _executable(tmp_path / "bin-a")
    _set_path_only(monkeypatch, a.parent)
    requirement = _seed_unbound(
        ctx.conn,
        ctx.requirements,
        limits=(_unbound_limit(candidates=[_candidate(a)]),),
    )
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)
    real_recover = requirement_limits.recover_program_bindings

    def recover_then_replace(*args, **kwargs):
        outcome = real_recover(*args, **kwargs)
        a.unlink()
        _executable(tmp_path / "bin-a")  # 同路径换成另一份真实程序（身份变化）
        return outcome

    monkeypatch.setattr(
        requirement_limits, "recover_program_bindings", recover_then_replace
    )

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "写回前复核失败必须失败关闭"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before, (
        "复查失败不得写回"
    )
    assert any("历史用户限制" in item for item in recovery_ctx.notices)


async def test_service_recovery_uses_new_entry_not_current_path_binding(
    ctx, tmp_path, monkeypatch
):
    """服务入口必须调用 recover_program_bindings；恢复不按当前 PATH 查找绑定。"""
    saved = _executable(tmp_path / "bin-saved")
    current = _executable(tmp_path / "bin-current")
    _set_path_only(monkeypatch, current.parent)
    requirement = _seed_unbound(
        ctx.conn,
        ctx.requirements,
        limits=(_unbound_limit(candidates=[_candidate(saved)]),),
    )
    real_recover = requirement_limits.recover_program_bindings
    calls: list[dict] = []

    def counting_recover(saved_limits, **kwargs):
        calls.append({"limits": saved_limits, **kwargs})
        return real_recover(saved_limits, **kwargs)

    monkeypatch.setattr(requirement_limits, "recover_program_bindings", counting_recover)
    seen_probe = _probe_spy(monkeypatch)

    recovered, _ = await _recover(ctx, requirement)

    assert len(calls) == 1, "服务层必须使用新的恢复入口"
    assert recovered is not None
    binding = recovered[0]["objects"][0]["program_binding"]
    assert binding["source"] == requirement_limits.BINDING_SOURCE_SAVED_CANDIDATE_VERIFIED
    assert os.path.normcase(os.path.realpath(binding["path"])) == os.path.normcase(
        os.path.realpath(str(saved))
    )
    assert seen_probe["count"] == 0, "有保存候选时恢复不得查找当前 PATH"


async def test_recovery_write_failure_is_not_used_this_round(ctx, tmp_path, monkeypatch):
    a = _executable(tmp_path / "bin-a")
    _set_path_only(monkeypatch, a.parent)
    requirement = _seed_unbound(
        ctx.conn,
        ctx.requirements,
        limits=(_unbound_limit(candidates=[_candidate(a)]),),
    )
    raw_before = _raw_limits(ctx.conn, requirement.requirement_id)

    def reject_write(requirement_id, limits, **kwargs):
        raise RuntimeError("条件写入失败（模拟竞态）")

    monkeypatch.setattr(ctx.requirements, "apply_binding_recovery", reject_write)

    recovered, recovery_ctx = await _recover(ctx, requirement)

    assert recovered is None, "写回失败时本轮不得使用恢复结果"
    assert _raw_limits(ctx.conn, requirement.requirement_id) == raw_before
    assert any("历史用户限制" in item for item in recovery_ctx.notices)
