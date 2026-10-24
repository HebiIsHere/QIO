"""R9-H12 A：UNRELATED 证据契约与共同对象判定修订（契约 §2/§3/§6.1）。

基线 r9h11/main 503df11a（R9-H12 共同契约冻结 2026-10-25）。
本文件只测 agent.security.object_state（纯函数 + 冻结类型 + 白名单注册表），
不装配引擎；引擎接线与恢复由 Lead/B/C 各自覆盖。

覆盖：
* §2 UNRELATED 白名单注册表：完整性、必带事实、未注册分支拒绝；
* §3.1 P3 路径身份不可读矩阵（限制路径不存在 / 存在但身份不可读 / 候选不可读 /
  显式 program 路径共用同一 _path_core）；
* §3.2 P1 未绑定 program_name 的入口/别名绕过（保存候选矩阵、不同入口名、
  当前澄清解析到别处）；
* §3.2/§4.3 merge 优先级（MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED）；
* §6.2 可靠无关仍继续（操作范围外 / 候选不存在 / 前缀外 / 已绑定不同对象）。

真实临时文件 + 真实 PATH 只读查找；身份不可读用显式注入（模拟分支单独标注，
与真实文件分支分开）。不联网、不调用模型、不新增依赖。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent.security import object_state
from agent.security.effects import Effect, EffectKind, Target
from agent.security.object_state import (
    LimitAssessment,
    ObjectDecision,
    ObjectFact,
    ObjectState,
    ProgramResolution,
    decide_object,
)
from agent.services import file_identity


# ---------------------------------------------------------------------------
# 契约 §2 白名单（冻结）：basis -> (必带事实字段, 必须为真的相等性)
# ---------------------------------------------------------------------------

UNRELATED_BASIS_SPECS = {
    "operation_out_of_scope": (
        ("operation", "operation_scope"),
        (("operation_scope", False),),
    ),
    "object_kind_out_of_scope": (
        ("limit_kind", "candidate"),
        (),
    ),
    "candidate_not_exists": (
        ("exists",),
        (("exists", False),),
    ),
    "path_prefix_outside": (
        ("path_within",),
        (("path_within", False),),
    ),
    "service_outside": (
        ("service_scope",),
        (("service_scope", False),),
    ),
    "different_object_identity": (
        ("identity_key",),
        (("identity_key", False),),
    ),
    "path_different_object": (
        ("identity_key",),
        (("identity_key", False),),
    ),
    "path_different_lifecycle": (
        ("identity_key", "birth_key"),
        (("identity_key", True), ("birth_key", False)),
    ),
    "entry_resolves_to_other_object": (
        ("entry_name", "identity_key"),
        (("identity_key", False),),
    ),
}


# ---------------------------------------------------------------------------
# 小工具：真实临时程序 / 身份注入 / 解析器
# ---------------------------------------------------------------------------


def _make_program(directory: Path, name: str = "tool") -> Path:
    """在本机造一个可被 shutil.which 找到的真实程序文件（不执行、不联网）。"""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        target = directory / (name + ".exe")
        target.write_bytes(b"fake-program")
    else:
        target = directory / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
    return target


def _identity(path: Path):
    found = file_identity.identity_of(path)
    assert found is not None, "身份读不到：" + str(path)
    return found


def _binding(path: Path, *, birth: bool = True) -> dict:
    identity = _identity(path)
    return {
        "path": str(path.resolve()),
        "identity": identity.key,
        "birth_key": identity.birth_key if birth else None,
        "source": "path_lookup",
    }


def _program_item(name: str, binding: dict | None = None) -> dict:
    item: dict = {"kind": "program_name", "value": name}
    if binding is not None:
        item["program_binding"] = binding
    return item


def _unbound_item(
    name: str = "runner",
    *,
    candidates=None,
    failure: str = "lookup_failed",
    state: str = "unresolved",
) -> dict:
    """保存时未形成绑定（纯数据）的对象条目。"""
    item: dict = {
        "kind": "program_name",
        "value": name,
        "binding_state": state,
        "binding_failure": failure,
    }
    if candidates is not None:
        item["binding_candidates"] = list(candidates)
    return item


def _saved_candidate(path, *, identity=None, source: str = "path_lookup", state: str = "unresolved") -> dict:
    candidate = {"path": str(path), "source": source, "state": state}
    if identity is not None:
        candidate["identity"] = identity
    return candidate


def _path_item(path, kind: str = "path", operations=None) -> dict:
    item: dict = {"kind": kind, "value": str(path)}
    if operations is not None:
        item["operations"] = list(operations)
    return item


def _candidate(kind: str, value) -> dict:
    return {"kind": kind, "value": str(value)}


def _execute_effect(program: Path, *, requested: str) -> Effect:
    return Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(program)),),
        detail={"requested_program": requested},
    )


def _fake_identity(path: Path, *, key: str, birth: str | None):
    volume, _, file_id = key.partition(":")
    return file_identity.FileIdentity(
        volume=volume,
        file_id=file_id,
        path=str(path),
        size=0,
        mtime_ns=0,
        birth_key=birth,
        link_count=1,
    )


def _patch_identity_for(monkeypatch, mapping: dict) -> None:
    """按真实路径选择性注入身份事实（模拟分支，与真实文件分支分开标注）。"""
    real = file_identity.identity_of

    def fake(candidate):  # noqa: ANN001
        target = os.path.normcase(os.path.abspath(str(candidate)))
        for path, value in mapping.items():
            if os.path.normcase(os.path.abspath(str(path))) == target:
                return value
        return real(candidate)

    monkeypatch.setattr(file_identity, "identity_of", fake)


def _resolver(**resolution):
    def resolver(name):  # noqa: ANN001
        return ProgramResolution(name=name, **resolution)

    return resolver


def _other_birth(identity) -> str:
    return "win:creation:1" if sys.platform == "win32" else "posix:birth:1"


def _assert_unrelated(decision, basis: str) -> None:
    assert decision.state is ObjectState.UNRELATED, decision
    assert decision.basis == basis
    problems = object_state.validate_unrelated_decision(decision)
    assert problems == (), problems
    # 可靠无关：聚合结论不冲突，允许按模式继续。
    assert LimitAssessment(decisions=(decision,)).conflict is False


def _stub(state: ObjectState, basis: str) -> ObjectDecision:
    return ObjectDecision(state=state, basis=basis)


# ===========================================================================
# §2 UNRELATED 白名单注册表
# ===========================================================================


def test_unrelated_registry_matches_frozen_whitelist():
    assert set(object_state.UNRELATED_BASES) == set(UNRELATED_BASIS_SPECS)
    for basis, (facts, equalities) in UNRELATED_BASIS_SPECS.items():
        spec = object_state.UNRELATED_BASES[basis]
        assert spec.basis == basis
        assert spec.required_facts == facts, basis
        assert spec.required_equal == equalities, basis
        assert spec.condition.strip(), basis
        assert isinstance(spec.required_facts, tuple)


def test_unrelated_registry_is_a_mapping_and_excludes_old_name_difference_basis():
    from types import MappingProxyType

    assert isinstance(object_state.UNRELATED_BASES, MappingProxyType)
    # 旧“入口名不同 = 无关”的依据必须消失（契约 §3.2 删除提前返回）。
    assert "different_entry_name" not in object_state.UNRELATED_BASES
    assert "entry_resolves_elsewhere" not in object_state.UNRELATED_BASES


def test_unregistered_unrelated_basis_is_rejected_by_factory():
    fabricated = ObjectDecision(state=ObjectState.UNRELATED, basis="different_entry_name")
    assert object_state.validate_unrelated_decision(fabricated) == (
        "unregistered_basis:different_entry_name",
    )
    with pytest.raises(ValueError):
        object_state._decision(
            ObjectState.UNRELATED,
            "different_entry_name",
            operation="execute",
            candidate_kind="program_name",
            candidate_value="other",
            facts=(),
        )


@pytest.mark.parametrize("basis", sorted(UNRELATED_BASIS_SPECS))
def test_registered_basis_without_required_facts_is_rejected(basis):
    facts, _equalities = UNRELATED_BASIS_SPECS[basis]
    fabricated = ObjectDecision(state=ObjectState.UNRELATED, basis=basis)
    problems = object_state.validate_unrelated_decision(fabricated)
    assert problems == tuple("missing_fact:" + field for field in facts)
    with pytest.raises(ValueError):
        object_state._decision(
            ObjectState.UNRELATED,
            basis,
            operation="execute",
            candidate_kind="path",
            candidate_value="/tmp/example",
            facts=(),
        )


@pytest.mark.parametrize("basis", sorted(UNRELATED_BASIS_SPECS))
def test_registered_basis_with_unverified_fact_is_rejected(basis):
    facts, equalities = UNRELATED_BASIS_SPECS[basis]
    fabricated = ObjectDecision(
        state=ObjectState.UNRELATED,
        basis=basis,
        facts=tuple(ObjectFact(field=field, observed="x", equal=True) for field in facts),
    )
    expected = tuple(
        "fact_not_verified:" + field for field, equal in equalities if equal is not True
    )
    assert object_state.validate_unrelated_decision(fabricated) == expected


def test_validate_non_unrelated_decision_is_noop():
    assert (
        object_state.validate_unrelated_decision(
            ObjectDecision(state=ObjectState.MATCH, basis="path_same")
        )
        == ()
    )
    assert (
        object_state.validate_unrelated_decision(
            ObjectDecision(state=ObjectState.UNKNOWN, basis="path_identity_unverifiable")
        )
        == ()
    )


# ===========================================================================
# §2 白名单 basis 正例（每条 UNRELATED 都带足事实，且允许继续）
# ===========================================================================


def test_unrelated_operation_out_of_scope_evidence(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(
        _path_item(path, operations=["write"]),
        _candidate("path", path),
        operation="read",
    )
    _assert_unrelated(decision, "operation_out_of_scope")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["operation_scope"].equal is False


def test_unrelated_object_kind_out_of_scope_evidence(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(_path_item(path), _candidate("service", "api.example.com"))
    _assert_unrelated(decision, "object_kind_out_of_scope")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["limit_kind"].observed == "path"


def test_unrelated_candidate_not_exists_evidence(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"x")
    missing = tmp_path / "missing-candidate.bin"
    decision = decide_object(_path_item(original), _candidate("path", missing))
    _assert_unrelated(decision, "candidate_not_exists")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["exists"].equal is False


def test_unrelated_path_prefix_outside_evidence(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside" / "tool.bin"
    outside.parent.mkdir()
    outside.write_bytes(b"tool")
    decision = decide_object(_path_item(allowed, kind="path_prefix"), _candidate("path", outside))
    _assert_unrelated(decision, "path_prefix_outside")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["path_within"].equal is False


def test_unrelated_service_outside_evidence():
    decision = decide_object(
        {"kind": "service", "value": "api.example.com"},
        _candidate("service", "other.example.com"),
    )
    _assert_unrelated(decision, "service_outside")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["service_scope"].equal is False


def test_unrelated_different_object_identity_evidence(tmp_path):
    program = _make_program(tmp_path / "progs-a", "runner")
    other = _make_program(tmp_path / "progs-b", "other")
    decision = decide_object(_program_item("runner", _binding(program)), _candidate("program", other))
    _assert_unrelated(decision, "different_object_identity")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["identity_key"].equal is False


def test_unrelated_path_different_object_evidence(tmp_path):
    first = tmp_path / "first.bin"
    first.write_bytes(b"x")
    second = tmp_path / "second.bin"
    second.write_bytes(b"y")
    decision = decide_object(_path_item(first), _candidate("path", second))
    _assert_unrelated(decision, "path_different_object")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["identity_key"].equal is False


def test_unrelated_path_different_lifecycle_evidence(tmp_path, monkeypatch):
    """模拟分支：同一身份 + 不同生命周期 = 编号复用后的另一个对象。"""
    original = tmp_path / "original.bin"
    original.write_bytes(b"x")
    other = tmp_path / "other.bin"
    other.write_bytes(b"y")
    identity = _identity(original)
    _patch_identity_for(
        monkeypatch,
        {
            str(original): _fake_identity(original, key=identity.key, birth=identity.birth_key),
            str(other): _fake_identity(other, key=identity.key, birth=_other_birth(identity)),
        },
    )
    decision = decide_object(_path_item(original), _candidate("path", other))
    _assert_unrelated(decision, "path_different_lifecycle")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["identity_key"].equal is True
    assert facts["birth_key"].equal is False


def test_unrelated_entry_resolves_to_other_object_evidence(tmp_path):
    bound = _make_program(tmp_path / "bound", "runner")
    other = _make_program(tmp_path / "other", "other")
    other_identity = _identity(other)
    decision = decide_object(
        _program_item("runner", _binding(bound)),
        _candidate("program_name", "other"),
        resolver=_resolver(
            path=str(other.resolve()),
            identity_key=other_identity.key,
            birth_key=other_identity.birth_key,
        ),
    )
    _assert_unrelated(decision, "entry_resolves_to_other_object")
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["entry_name"].observed == "other"
    assert facts["identity_key"].equal is False


# ===========================================================================
# §3.1 P3：限制路径身份不可读 -> UNKNOWN（不再 UNRELATED）
# ===========================================================================


def test_path_limit_missing_is_unknown_target_unreadable(tmp_path):
    missing_limit = tmp_path / "missing-limit.bin"
    candidate = tmp_path / "exists.bin"
    candidate.write_bytes(b"y")
    decision = decide_object(_path_item(missing_limit), _candidate("path", candidate))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_limit_target_unreadable"
    assert decision.fail_closed is True
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["exists"].equal is False


def test_path_limit_identity_unreadable_is_unknown(tmp_path, monkeypatch):
    limit = tmp_path / "limit.bin"
    limit.write_bytes(b"x")
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"y")
    _patch_identity_for(monkeypatch, {str(limit): None})
    decision = decide_object(_path_item(limit), _candidate("path", candidate))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_limit_identity_unavailable"
    assert decision.fail_closed is True


def test_path_candidate_identity_unreadable_is_unknown_h12(tmp_path, monkeypatch):
    limit = tmp_path / "limit.bin"
    limit.write_bytes(b"x")
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"y")
    _patch_identity_for(monkeypatch, {str(candidate): None})
    decision = decide_object(_path_item(limit), _candidate("path", candidate))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_identity_unverifiable"


def test_program_explicit_path_limit_identity_unreadable_is_unknown(tmp_path, monkeypatch):
    """§3.1：显式 program 路径与 path 限制共用同一 _path_core。"""
    program = _make_program(tmp_path / "progs", "runner")
    candidate = _make_program(tmp_path / "other", "other")
    _patch_identity_for(monkeypatch, {str(program): None})
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program", candidate),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_limit_identity_unavailable"
    assert decision.fail_closed is True


def test_path_limit_entry_name_limit_identity_unreadable_is_unknown(tmp_path, monkeypatch):
    limit = tmp_path / "limit.bin"
    limit.write_bytes(b"x")
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"y")
    candidate_identity = _identity(candidate)
    _patch_identity_for(monkeypatch, {str(limit): None})
    decision = decide_object(
        _path_item(limit),
        _candidate("program_name", "runner"),
        resolver=_resolver(
            path=str(candidate.resolve()),
            identity_key=candidate_identity.key,
            birth_key=candidate_identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_limit_identity_unavailable"


def test_path_prefix_outside_still_reliable_when_limit_identity_unreadable(tmp_path, monkeypatch):
    """§2 path_prefix_outside 只需路径关系事实：身份不可读不改变前缀外结论。"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside" / "tool.bin"
    outside.parent.mkdir()
    outside.write_bytes(b"tool")
    _patch_identity_for(monkeypatch, {str(allowed): None})
    decision = decide_object(
        _path_item(allowed, kind="path_prefix"),
        _candidate("path", outside),
    )
    _assert_unrelated(decision, "path_prefix_outside")


# ===========================================================================
# §3.2 P1：未绑定 program_name + 保存候选（纯数据）矩阵
# ===========================================================================


def test_unbound_saved_candidate_path_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    saved = tmp_path / "saved" / "runner.exe"
    saved.parent.mkdir()
    saved.write_bytes(b"program")
    item = _unbound_item(
        candidates=[_saved_candidate(saved.resolve())],
    )
    decision = decide_object(item, _candidate("program", saved))
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_saved_candidate"
    assert decision.needs_recovery is True
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["saved_candidate_path"].observed == str(saved.resolve())
    assert facts["saved_candidate_path"].source == "path_lookup"
    assert facts["binding_state"].observed == "unresolved"
    assert facts["binding_failure"].observed == "lookup_failed"


def test_unbound_saved_candidate_identity_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    candidate = tmp_path / "bin" / "runner.exe"
    candidate.parent.mkdir()
    candidate.write_bytes(b"program")
    identity = _identity(candidate)
    gone_alias = tmp_path / "gone" / "runner-alias.exe"
    item = _unbound_item(
        candidates=[
            _saved_candidate(gone_alias, identity=identity.key),
        ]
    )
    decision = decide_object(item, _candidate("program", candidate))
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_saved_candidate_identity"
    assert decision.needs_recovery is True


def test_unbound_saved_candidates_no_match_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    saved = tmp_path / "saved" / "runner.exe"
    saved.parent.mkdir()
    saved.write_bytes(b"saved")
    candidate = tmp_path / "bin" / "runner.exe"
    candidate.parent.mkdir()
    candidate.write_bytes(b"candidate")
    item = _unbound_item(candidates=[_saved_candidate(saved.resolve())])
    decision = decide_object(item, _candidate("program", candidate))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "binding_missing_candidate_not_in_saved"
    assert decision.fail_closed is True


def test_unbound_saved_candidates_candidate_missing_is_unrelated(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    saved = tmp_path / "saved" / "runner.exe"
    saved.parent.mkdir()
    saved.write_bytes(b"saved")
    item = _unbound_item(candidates=[_saved_candidate(saved.resolve())])
    decision = decide_object(item, _candidate("program", tmp_path / "missing" / "runner.exe"))
    _assert_unrelated(decision, "candidate_not_exists")


def test_unbound_no_candidates_resolved_elsewhere_is_unknown(tmp_path, monkeypatch):
    resolved = _make_program(tmp_path / "dir-a", "runner")
    other = _make_program(tmp_path / "dir-b", "runner")
    monkeypatch.setenv("PATH", str(resolved.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", other),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "binding_missing_resolved_elsewhere"
    assert decision.fail_closed is True


def test_unbound_no_candidates_lookup_failed_entry_same_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", program),
        effect=_execute_effect(program, requested="runner"),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_entry_same"


def test_unbound_no_candidates_lookup_failed_entry_differs_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", program),
        effect=_execute_effect(program, requested="other"),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "binding_missing_lookup_unresolved"
    assert decision.fail_closed is True


def test_unbound_entry_name_differs_is_unknown(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program_name", "other"),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "unbound_entry_name_differs"
    assert decision.fail_closed is True


def test_unbound_entry_name_differs_saved_identity_match_is_unbound(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    identity = _identity(program)
    item = _unbound_item(candidates=[_saved_candidate(program.resolve(), identity=identity.key)])
    decision = decide_object(
        item,
        _candidate("program_name", "other"),
        resolver=_resolver(
            path=str(program.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_saved_candidate_identity"


def test_unbound_entry_name_differs_saved_candidate_without_identity_is_unknown(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    identity = _identity(program)
    item = _unbound_item(candidates=[_saved_candidate(program.resolve())])
    decision = decide_object(
        item,
        _candidate("program_name", "other"),
        resolver=_resolver(
            path=str(program.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "unbound_entry_name_differs"


def test_unbound_entry_name_same_is_unbound_h12(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        _unbound_item(),
        _candidate("program_name", "runner"),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_entry_same"


def test_bound_entry_name_differs_unverifiable_is_unknown(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    other = tmp_path / "other" / "runner"
    decision = decide_object(
        _program_item("runner", _binding(program)),
        _candidate("program_name", "other"),
        resolver=_resolver(path=str(other), error="identity_unavailable"),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "entry_name_differs_unverifiable"
    assert decision.fail_closed is True


# ===========================================================================
# §3.2/§4.3 merge 优先级：MATCH > INVALID/UNBOUND > UNKNOWN > UNRELATED
# ===========================================================================


def test_merge_priority_match_beats_invalid_and_unbound():
    assert (
        object_state._merge_decisions(
            _stub(ObjectState.MATCH, "match"),
            _stub(ObjectState.INVALID, "invalid"),
        ).state
        is ObjectState.MATCH
    )
    assert (
        object_state._merge_decisions(
            _stub(ObjectState.UNBOUND, "unbound"),
            _stub(ObjectState.MATCH, "match"),
        ).state
        is ObjectState.MATCH
    )


def test_merge_priority_invalid_unbound_beat_unknown():
    assert (
        object_state._merge_decisions(
            _stub(ObjectState.INVALID, "invalid"),
            _stub(ObjectState.UNKNOWN, "unknown"),
        ).state
        is ObjectState.INVALID
    )
    assert (
        object_state._merge_decisions(
            _stub(ObjectState.UNBOUND, "unbound"),
            _stub(ObjectState.UNKNOWN, "unknown"),
        ).state
        is ObjectState.UNBOUND
    )


def test_merge_priority_unknown_beats_unrelated():
    assert (
        object_state._merge_decisions(
            _stub(ObjectState.UNKNOWN, "unknown"),
            _stub(ObjectState.UNRELATED, "unrelated"),
        ).state
        is ObjectState.UNKNOWN
    )
    assert (
        object_state._merge_decisions(
            _stub(ObjectState.UNRELATED, "unrelated"),
            _stub(ObjectState.UNKNOWN, "unknown"),
        ).state
        is ObjectState.UNKNOWN
    )


def test_merge_unknown_beats_unrelated_in_decide_object(tmp_path, monkeypatch):
    """候选不存在（UNRELATED）但真实入口名不同且无法澄清（UNKNOWN）→ 失败关闭。"""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    missing_program = tmp_path / "missing" / "runner.exe"
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", missing_program),
        effect=_execute_effect(missing_program, requested="other"),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "unbound_entry_name_differs"
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["exists"].equal is False


def test_merge_invalid_dominates_path_unrelated(tmp_path, monkeypatch):
    """绑定存在 + 同一入口名解析到新对象：路径候选无关也要按 INVALID 停住。"""
    bound = _make_program(tmp_path / "bound", "runner")
    current = _make_program(tmp_path / "current", "runner")
    unrelated = _make_program(tmp_path / "elsewhere", "unrelated")
    monkeypatch.setenv("PATH", str(current.parent))
    decision = decide_object(
        _program_item("runner", _binding(bound)),
        _candidate("program", unrelated),
        effect=_execute_effect(unrelated, requested="runner"),
    )
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "entry_name_same_object_differs"


# ===========================================================================
# §6.2 可靠无关“仍继续”汇总（聚合结论不冲突）
# ===========================================================================


def test_reliable_unrelated_bases_do_not_conflict(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    missing = tmp_path / "missing.bin"
    decisions = [
        decide_object(
            _path_item(path, operations=["write"]),
            _candidate("path", path),
            operation="read",
        ),
        decide_object(_path_item(path), _candidate("path", missing)),
        decide_object(
            _path_item(path, kind="directory"),
            _candidate("path", tmp_path / "outside.bin"),
        ),
        decide_object(
            {"kind": "service", "value": "api.example.com"},
            _candidate("service", "other.example.com"),
        ),
    ]
    bound = _make_program(tmp_path / "bound", "runner")
    other = _make_program(tmp_path / "other", "other")
    decisions.append(decide_object(_program_item("runner", _binding(bound)), _candidate("program", other)))
    for decision in decisions:
        assert decision.state is ObjectState.UNRELATED, decision
        assert object_state.validate_unrelated_decision(decision) == ()
    assert LimitAssessment(decisions=tuple(decisions)).conflict is False
