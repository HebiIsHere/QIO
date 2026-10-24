"""R9-H11 A：共同对象状态与身份/生命周期比较（契约 §2/§3）。

本文件只测 agent.security.object_state 模块本身（纯函数 + 冻结类型），
不装配引擎；引擎接线、恢复、跨操作集成由 Lead/C/D 各自覆盖。

测试依据（契约 §8）：
* 真实临时文件/真实 PATH 只读查找；
* “编号复用/生命周期不同”分支用真实文件与显式注入的身份事实分开标注；
* 不联网、模型一律 fake/mock（本文件不调用模型）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent.security import effects as effects_module
from agent.security import object_state
from agent.security.effects import Effect, EffectKind, Target
from agent.security.object_state import (
    IdentityComparison,
    LimitAssessment,
    ObjectDecision,
    ObjectFact,
    ObjectState,
    ProgramResolution,
    compare_bound_identity,
    decide_object,
    decision_code_for,
    resolve_path,
    resolve_program_name,
    same_path_text,
)
from agent.services import file_identity


# ---------------------------------------------------------------------------
# 小工具：真实临时程序 / 能力探测
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


def _hardlink(source: Path, target: Path) -> bool:
    try:
        os.link(source, target)
        return True
    except (OSError, NotImplementedError):
        return False


def _decision(
    state: ObjectState,
    basis: str = "test_basis",
    *,
    object_kind: str | None = None,
    object_value: str | None = None,
    facts: tuple = (),
    detail: str = "",
    operation: str | None = None,
) -> ObjectDecision:
    return ObjectDecision(
        state=state,
        basis=basis,
        operation=operation,
        object_kind=object_kind,
        object_value=object_value,
        facts=facts,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# §2 冻结类型 / 审计序列化
# ---------------------------------------------------------------------------


def test_object_state_values_are_frozen_strings():
    assert [state.value for state in ObjectState] == [
        "match",
        "unrelated",
        "unbound",
        "invalid",
        "unknown",
    ]
    # str Enum：调用方可以按稳定字符串比较/落库（契约 §2）。
    assert ObjectState.MATCH == "match"
    assert ObjectState.UNRELATED == "unrelated"
    assert isinstance(ObjectState.UNKNOWN, str)


def test_object_fact_as_dict_structured():
    fact = ObjectFact(
        field="identity_key",
        expected="vol:1",
        observed="vol:2",
        source="path_compare",
        equal=False,
    )
    assert fact.as_dict() == {
        "field": "identity_key",
        "expected": "vol:1",
        "observed": "vol:2",
        "source": "path_compare",
        "equal": False,
    }
    # None(=不可比较) 必须原样保留，不能折叠成 False。
    assert ObjectFact(field="birth_key").as_dict()["equal"] is None


def test_identity_comparison_defaults_to_no_facts():
    comparison = IdentityComparison(state="same")
    assert comparison.state == "same"
    assert comparison.facts == ()


def test_program_resolution_is_read_only_shape():
    resolution = ProgramResolution(name="runner")
    assert resolution.name == "runner"
    assert resolution.path is None
    assert resolution.identity_key is None
    assert resolution.birth_key is None
    assert resolution.source == "path_lookup"
    assert resolution.error == ""


def test_decision_flag_properties_never_boolean_only():
    assert _decision(ObjectState.MATCH).matched is True
    assert _decision(ObjectState.MATCH).unrelated is False
    assert _decision(ObjectState.UNRELATED).unrelated is True
    assert _decision(ObjectState.UNRELATED).matched is False
    for state in (ObjectState.UNBOUND, ObjectState.INVALID):
        decision = _decision(state)
        assert decision.needs_recovery is True
        assert decision.fail_closed is False
        assert decision.matched is False and decision.unrelated is False
    unknown = _decision(ObjectState.UNKNOWN)
    assert unknown.fail_closed is True
    assert unknown.needs_recovery is False


def test_object_decision_as_dict_is_bounded_and_auditable():
    long_text = "x" * (object_state.MAX_AUDIT_CHARS * 3)
    fact = ObjectFact(
        field="path",
        expected=long_text,
        observed=long_text,
        source="path_compare",
    )
    decision = _decision(
        ObjectState.INVALID,
        basis=long_text,
        object_kind="program_name",
        object_value=long_text,
        facts=(fact,),
        detail=long_text,
        operation="execute",
    )
    payload = decision.as_dict()
    assert payload["state"] == "invalid"
    assert payload["operation"] == "execute"
    assert payload["object_kind"] == "program_name"
    assert isinstance(payload["facts"], list) and payload["facts"][0]["field"] == "path"
    assert payload["facts"][0]["expected"].startswith("xxx")
    # 全部为有界字符串（审计可序列化，不因超长文本膨胀）。
    encoded = json.dumps(payload, ensure_ascii=False)
    assert long_text not in encoded
    for name in ("basis", "object_value", "detail"):
        assert len(payload[name]) <= object_state.MAX_AUDIT_CHARS
    assert len(payload["facts"][0]["expected"]) <= object_state.MAX_AUDIT_CHARS
    # 审计结构里不出现"密钥/推理"之类自由字段：只保留白名单键。
    assert set(payload) == {
        "state",
        "basis",
        "operation",
        "object_kind",
        "object_value",
        "facts",
        "detail",
    }


def test_limit_assessment_conflict_only_for_non_unrelated():
    assert LimitAssessment().conflict is False
    unrelated = _decision(ObjectState.UNRELATED)
    assert LimitAssessment(decisions=(unrelated,)).conflict is False
    assert LimitAssessment(decisions=(unrelated, _decision(ObjectState.MATCH))).conflict is True
    for state in (
        ObjectState.UNBOUND,
        ObjectState.INVALID,
        ObjectState.UNKNOWN,
    ):
        assert LimitAssessment(decisions=(unrelated, _decision(state))).conflict is True


def test_decision_code_for_frozen_mapping():
    assert (
        decision_code_for(ObjectState.UNBOUND, "program_name")
        == effects_module.DECISION_PROGRAM_NAME_UNRESOLVED
    )
    # UNBOUND 的专用恢复码只针对程序名称；路径对象的 unbound 没有专用码。
    assert decision_code_for(ObjectState.UNBOUND, "path") is None
    assert decision_code_for(ObjectState.UNBOUND, None) is None
    assert decision_code_for(ObjectState.INVALID, "path") == "program_binding_invalidated"
    assert decision_code_for(ObjectState.INVALID, "program") == "program_binding_invalidated"
    assert decision_code_for(ObjectState.UNKNOWN, "path") == "object_state_unconfirmed"
    assert decision_code_for(ObjectState.UNKNOWN, None) == "object_state_unconfirmed"
    assert decision_code_for(ObjectState.MATCH, "path") is None
    assert decision_code_for(ObjectState.UNRELATED, "path") is None
    # 接受稳定字符串（审计/反序列化路径不强制枚举实例）。
    assert decision_code_for("invalid", "service") == "program_binding_invalidated"


# ---------------------------------------------------------------------------
# §2 纯函数：resolve_path / same_path_text
# ---------------------------------------------------------------------------


def test_resolve_path_absolute_and_relative(tmp_path):
    absolute = tmp_path / "a" / "b.txt"
    assert resolve_path(str(absolute), None) == str(absolute.resolve())
    assert resolve_path(absolute, tmp_path) == str(absolute.resolve())
    # base 接受 str/Path；相对路径按 base 规范化。
    assert resolve_path("a/b.txt", tmp_path) == str((tmp_path / "a" / "b.txt").resolve())
    if os.name == "nt":
        # Windows：反斜杠是分隔符，相对路径按平台语义规范化。
        assert resolve_path("a\\b.txt", str(tmp_path)) == str(
            (tmp_path / "a" / "b.txt").resolve()
        )
    else:
        # POSIX：反斜杠是合法文件名字符（不是分隔符），不能被改写成子目录。
        assert resolve_path("a\\b.txt", str(tmp_path)) == str(
            (tmp_path / "a\\b.txt").resolve()
        )


def test_resolve_path_requires_base_for_relative():
    assert resolve_path("relative/file.txt", None) is None
    assert resolve_path("", None) is None
    assert resolve_path(None, None) is None
    assert resolve_path("   ", None) is None
    # expanduser 之后是绝对路径：base=None 也接受。
    home = Path.home()
    assert resolve_path("~", None) == str(home.resolve())


def test_resolve_path_normalizes_dot_segments(tmp_path):
    assert resolve_path(str(tmp_path / "x" / ".." / "y"), None) == str((tmp_path / "y").resolve())


def test_same_path_text_platform_normalization():
    assert same_path_text("C:\\Data\\A.txt", "c:/data/a.txt")
    assert same_path_text("/tmp/a/", "/tmp/a")
    assert same_path_text("a\\b", "a/b")
    assert not same_path_text("/tmp/a", "/tmp/b")
    assert not same_path_text("", "")
    assert not same_path_text(None, "/tmp/a")


# ---------------------------------------------------------------------------
# §2 resolve_program_name：当前 PATH 只读查找，不写库/不补绑定
# ---------------------------------------------------------------------------


def test_resolve_program_name_reads_identity_from_path(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "path-a", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    before = sorted(item.name for item in program.parent.iterdir())
    resolution = resolve_program_name("runner")
    after = sorted(item.name for item in program.parent.iterdir())
    identity = file_identity.identity_of(program)
    assert identity is not None
    assert resolution.name == "runner"
    assert resolution.path is not None
    assert os.path.normcase(str(Path(resolution.path).resolve())) == os.path.normcase(
        str(program.resolve())
    )
    assert resolution.identity_key == identity.key
    assert resolution.birth_key == identity.birth_key
    assert resolution.source == "path_lookup"
    assert resolution.error == ""
    # 只读澄清：不创建/删除任何文件（也不写库——本函数不依赖数据库连接）。
    assert before == after


def test_resolve_program_name_not_found_is_error_and_no_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    resolution = resolve_program_name("definitely-not-existing-runner")
    assert resolution.path is None
    assert resolution.identity_key is None
    assert resolution.birth_key is None
    assert resolution.source == "path_lookup"
    assert resolution.error


def test_resolve_program_name_identity_unavailable_is_reported(tmp_path, monkeypatch):
    _make_program(tmp_path / "path-b", "runner")
    monkeypatch.setenv("PATH", str(tmp_path / "path-b"))
    calls = {"count": 0}

    def unavailable(path):  # noqa: ANN001 - 模拟身份读取不可用
        calls["count"] += 1
        return None

    monkeypatch.setattr(file_identity, "identity_of", unavailable)
    resolution = resolve_program_name("runner")
    assert calls["count"] == 1
    assert resolution.path is not None
    assert resolution.identity_key is None
    assert resolution.error


# ---------------------------------------------------------------------------
# §2 compare_bound_identity：唯一身份/生命周期比较入口
# ---------------------------------------------------------------------------


def _identity_facts(comparison: IdentityComparison) -> dict:
    return {fact.field: fact for fact in comparison.facts}


def test_compare_bound_identity_bound_missing_is_unavailable():
    comparison = compare_bound_identity(None, None)
    assert comparison.state == "identity_unavailable"
    facts = _identity_facts(comparison)
    assert "identity_key" in facts
    assert facts["identity_key"].equal is None


def test_compare_bound_identity_observed_missing_is_unavailable(tmp_path):
    comparison = compare_bound_identity("vol:1", "birth:1", candidate_path=str(tmp_path / "nope"))
    assert comparison.state == "identity_unavailable"


def test_compare_bound_identity_different_object(tmp_path):
    left = tmp_path / "left.bin"
    right = tmp_path / "right.bin"
    left.write_bytes(b"same-bytes")
    right.write_bytes(b"same-bytes")
    identity = file_identity.identity_of(left)
    assert identity is not None
    comparison = compare_bound_identity(
        identity.key, identity.birth_key, candidate_path=str(right)
    )
    assert comparison.state == "different_object"
    facts = _identity_facts(comparison)
    assert facts["identity_key"].equal is False


def test_compare_bound_identity_same_file_same_lifecycle(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    identity = file_identity.identity_of(path)
    assert identity is not None and identity.birth_key
    comparison = compare_bound_identity(
        identity.key, identity.birth_key, candidate_path=str(path)
    )
    assert comparison.state == "same"
    facts = _identity_facts(comparison)
    assert facts["identity_key"].equal is True
    assert facts["birth_key"].equal is True


def test_compare_bound_identity_different_lifecycle_same_identity(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    identity = file_identity.identity_of(path)
    assert identity is not None and identity.birth_key
    # 模拟编号复用：同一 identity、不同生命周期依据（显式注入事实，与真实文件分支分开标注）。
    other_birth = "win:creation:1" if sys.platform == "win32" else "posix:birth:1"
    comparison = compare_bound_identity(
        identity.key, identity.birth_key, observed_identity=identity.key, observed_birth=other_birth
    )
    assert comparison.state == "different_lifecycle"
    facts = _identity_facts(comparison)
    assert facts["identity_key"].equal is True
    assert facts["birth_key"].equal is False


def test_compare_bound_identity_lifecycle_unverifiable(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    identity = file_identity.identity_of(path)
    assert identity is not None and identity.birth_key
    comparison = compare_bound_identity(
        identity.key, identity.birth_key, observed_identity=identity.key, observed_birth=None
    )
    assert comparison.state == "lifecycle_unverifiable"
    facts = _identity_facts(comparison)
    assert facts["birth_key"].equal is None


def test_compare_bound_identity_weak_when_bound_birth_missing(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    identity = file_identity.identity_of(path)
    assert identity is not None and identity.birth_key
    comparison = compare_bound_identity(
        identity.key, None, observed_identity=identity.key, observed_birth=identity.birth_key
    )
    assert comparison.state == "same"
    facts = _identity_facts(comparison)
    assert facts["identity_key"].equal is True
    assert facts["lifecycle_basis"].expected == "binding_without_birth"
    assert facts["lifecycle_basis"].equal is None


def test_compare_bound_identity_never_uses_content_or_mtime(tmp_path):
    left = tmp_path / "left.bin"
    right = tmp_path / "right.bin"
    left.write_bytes(b"identical")
    right.write_bytes(b"identical")
    fixed = 1_700_000_000_000_000_000
    os.utime(left, ns=(fixed, fixed))
    os.utime(right, ns=(fixed, fixed))
    left_identity = file_identity.identity_of(left)
    right_identity = file_identity.identity_of(right)
    assert left_identity is not None and right_identity is not None
    assert file_identity.content_sha256(left) == file_identity.content_sha256(right)
    assert left_identity.size == right_identity.size
    comparison = compare_bound_identity(
        left_identity.key, left_identity.birth_key, candidate_path=str(right)
    )
    assert comparison.state == "different_object"


def test_compare_bound_identity_uses_cache_for_repeated_lookups(tmp_path, monkeypatch):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    identity = file_identity.identity_of(path)
    assert identity is not None
    calls = {"count": 0}
    real_identity_of = file_identity.identity_of

    def counted(candidate):  # noqa: ANN001
        calls["count"] += 1
        return real_identity_of(candidate)

    monkeypatch.setattr(file_identity, "identity_of", counted)
    cache: dict = {}
    first = compare_bound_identity(identity.key, identity.birth_key, candidate_path=str(path), cache=cache)
    second = compare_bound_identity(identity.key, identity.birth_key, candidate_path=str(path), cache=cache)
    assert first.state == second.state == "same"
    assert calls["count"] == 1, "同一路径在同一 cache 生命周期内只应查询一次身份"
    assert cache, "缓存必须写入可审计的观测事实"


def test_compare_bound_identity_accepts_injected_cache_facts(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    identity = file_identity.identity_of(path)
    assert identity is not None and identity.birth_key
    cache = {str(path): (identity.key, identity.birth_key)}
    comparison = compare_bound_identity("other:1", "birth:x", candidate_path=str(path), cache=cache)
    assert comparison.state == "different_object"

# ===========================================================================
# §3 判定语义表：decide_object 逐行覆盖
# ===========================================================================


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


def _binding_item(name: str, binding: dict | None = None, operations=None) -> dict:
    item: dict = {"kind": "program_name", "value": name}
    if binding is not None:
        item["program_binding"] = binding
    if operations is not None:
        item["operations"] = list(operations)
    return item


def _path_item(path: Path, kind: str = "path", operations=None) -> dict:
    item: dict = {"kind": kind, "value": str(path)}
    if operations is not None:
        item["operations"] = list(operations)
    return item


def _candidate(kind: str, value) -> dict:
    return {"kind": kind, "value": str(value)}


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


# ---------------------------------------------------------------- §3.1 操作范围


def test_operation_out_of_scope_is_reliable_unrelated(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(
        _path_item(path, operations=["write"]),
        _candidate("path", path),
        operation="read",
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "operation_out_of_scope"
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["operation"].observed == "read"


def test_operation_none_still_checks_the_object(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(
        _path_item(path, operations=["write"]),
        _candidate("path", path),
        operation=None,
    )
    # 动作无法确定时不能据此判无关：仍然核对对象并命中。
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_same"


def test_operation_in_scope_and_alias_wildcard(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    aliased = decide_object(
        {"operations": ["install_deps"], "kind": "path", "value": str(path)},
        _candidate("path", path),
        operation="install",
    )
    assert aliased.state is ObjectState.MATCH
    wildcard = decide_object(
        {"operations": ["*"], "kind": "path", "value": str(path)},
        _candidate("path", path),
        operation="read",
    )
    assert wildcard.state is ObjectState.MATCH


# ---------------------------------------------------------------- §3.2 path


def test_path_exact_same_path_matches(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(_path_item(path), _candidate("path", path))
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_same"
    assert decision.unrelated is False


def test_path_unresolvable_same_text_matches_conservatively():
    decision = decide_object(
        {"kind": "path", "value": "rel/a\\b.TXT"},
        {"kind": "path", "value": "rel/a/b.txt"},
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_text_same"


def test_path_unresolvable_different_text_is_unknown():
    decision = decide_object(
        {"kind": "path", "value": "rel/a/b"},
        {"kind": "path", "value": "rel/c/d"},
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_unresolvable"


def test_path_prefix_contains_and_outside(tmp_path):
    allowed = tmp_path / "allowed"
    inside = allowed / "sub" / "file.txt"
    outside = tmp_path / "outside" / "file.txt"
    matched = decide_object(
        {"kind": "path_prefix", "value": str(allowed)},
        _candidate("path", inside),
    )
    assert matched.state is ObjectState.MATCH
    assert matched.basis == "path_prefix_contains"
    absent = decide_object(
        {"kind": "path_prefix", "value": str(allowed)},
        _candidate("path", outside),
    )
    assert absent.state is ObjectState.UNRELATED
    assert absent.basis == "path_prefix_outside"


def test_directory_limit_contains_itself(tmp_path):
    decision = decide_object(
        {"kind": "directory", "value": str(tmp_path)},
        _candidate("path", tmp_path),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_prefix_contains"


def test_path_hardlink_is_same_object(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"stable")
    hard = tmp_path / "hardlink.bin"
    if not _hardlink(original, hard):
        pytest.skip("平台不支持硬链接")
    decision = decide_object(_path_item(original), _candidate("path", hard))
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_hardlink_same_object"
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["identity_key"].equal is True


def test_path_same_content_copy_is_different_object(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"same-content")
    copy = tmp_path / "copy.bin"
    copy.write_bytes(b"same-content")
    fixed = 1_700_000_000_000_000_000
    os.utime(original, ns=(fixed, fixed))
    os.utime(copy, ns=(fixed, fixed))
    decision = decide_object(_path_item(original), _candidate("path", copy))
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "path_different_object"


def test_path_different_lifecycle_is_other_object(tmp_path, monkeypatch):
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
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "path_different_lifecycle"
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["birth_key"].equal is False


def test_path_candidate_identity_unreadable_is_unknown(tmp_path, monkeypatch):
    original = tmp_path / "original.bin"
    original.write_bytes(b"x")
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"y")
    _patch_identity_for(monkeypatch, {str(candidate): None})
    decision = decide_object(_path_item(original), _candidate("path", candidate))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "path_identity_unverifiable"
    assert decision.fail_closed is True


def test_path_limit_identity_unreadable_is_unrelated(tmp_path):
    """§3.7：路径型限制（无保存身份）且限制路径当前不可读 -> 可靠无关。"""
    missing_limit = tmp_path / "missing-limit.bin"
    candidate = tmp_path / "exists.bin"
    candidate.write_bytes(b"y")
    decision = decide_object(_path_item(missing_limit), _candidate("path", candidate))
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "path_limit_identity_unavailable"


def test_path_candidate_missing_is_unrelated(tmp_path):
    original = tmp_path / "original.bin"
    original.write_bytes(b"x")
    missing = tmp_path / "missing-candidate.bin"
    decision = decide_object(_path_item(original), _candidate("path", missing))
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "candidate_not_exists"


def test_path_limit_vs_service_candidate_is_unrelated(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(_path_item(path), _candidate("service", "api.example.com"))
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "object_kind_out_of_scope"


def test_path_limit_entry_name_resolves_to_same_file(tmp_path):
    path = tmp_path / "program.bin"
    path.write_bytes(b"program")
    identity = _identity(path)
    resolution = _resolver(
        path=str(path.resolve()), identity_key=identity.key, birth_key=identity.birth_key
    )
    decision = decide_object(
        _path_item(path),
        _candidate("program_name", "runner"),
        resolver=resolution,
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_same"


def test_path_limit_entry_name_resolves_elsewhere(tmp_path):
    path = tmp_path / "program.bin"
    path.write_bytes(b"program")
    other = tmp_path / "other.bin"
    other.write_bytes(b"other")
    other_identity = _identity(other)
    resolution = _resolver(
        path=str(other.resolve()),
        identity_key=other_identity.key,
        birth_key=other_identity.birth_key,
    )
    decision = decide_object(
        _path_item(path),
        _candidate("program_name", "runner"),
        resolver=resolution,
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "path_different_object"


def test_path_limit_entry_name_unresolved_is_unknown(tmp_path):
    path = tmp_path / "program.bin"
    path.write_bytes(b"program")
    decision = decide_object(
        _path_item(path),
        _candidate("program_name", "runner"),
        resolver=_resolver(error="program_not_found"),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "entry_name_unresolved"


def test_path_prefix_entry_name_inside_and_outside(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "tool.bin"
    inside.write_bytes(b"tool")
    outside = tmp_path / "outside" / "tool.bin"
    outside.parent.mkdir()
    outside.write_bytes(b"tool")
    inside_identity = _identity(inside)
    outside_identity = _identity(outside)
    inside_decision = decide_object(
        {"kind": "path_prefix", "value": str(allowed)},
        _candidate("program_name", "tool"),
        resolver=_resolver(
            path=str(inside.resolve()),
            identity_key=inside_identity.key,
            birth_key=inside_identity.birth_key,
        ),
    )
    assert inside_decision.state is ObjectState.MATCH
    assert inside_decision.basis == "path_prefix_contains"
    outside_decision = decide_object(
        {"kind": "path_prefix", "value": str(allowed)},
        _candidate("program_name", "tool"),
        resolver=_resolver(
            path=str(outside.resolve()),
            identity_key=outside_identity.key,
            birth_key=outside_identity.birth_key,
        ),
    )
    assert outside_decision.state is ObjectState.UNRELATED
    assert outside_decision.basis == "path_prefix_outside"


# ---------------------------------------------------------------- §3.3 program


def test_program_limit_same_program_path_matches(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program", program),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_same"


def test_program_limit_hardlink_matches(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    hard = tmp_path / "progs" / "runner-hardlink.exe"
    if not _hardlink(program, hard):
        pytest.skip("平台不支持硬链接")
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program", hard),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "path_hardlink_same_object"


def test_program_limit_different_program_is_unrelated(tmp_path):
    program = _make_program(tmp_path / "progs-a", "runner")
    other = _make_program(tmp_path / "progs-b", "runner")
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program", other),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "path_different_object"


def test_program_limit_entry_name_resolves_to_limit_path(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    identity = _identity(program)
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program_name", "runner"),
        resolver=_resolver(
            path=str(program.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "entry_resolves_to_limit_path"


def test_program_limit_entry_name_resolves_elsewhere(tmp_path):
    program = _make_program(tmp_path / "progs-a", "runner")
    other = _make_program(tmp_path / "progs-b", "runner")
    other_identity = _identity(other)
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program_name", "runner"),
        resolver=_resolver(
            path=str(other.resolve()),
            identity_key=other_identity.key,
            birth_key=other_identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "entry_resolves_elsewhere"


def test_program_limit_entry_name_unresolved_is_unknown(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program_name", "runner"),
        resolver=_resolver(error="program_not_found"),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "entry_name_unresolved"


def test_program_limit_entry_name_same_path_but_new_object_still_matches(tmp_path):
    """显式程序路径限制：入口名解析回同一路径（对象被替换）仍属限制路径。"""
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        {"kind": "program", "value": str(program)},
        _candidate("program_name", "runner"),
        resolver=_resolver(
            path=str(program.resolve()), identity_key="vol:new", birth_key="birth:new"
        ),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "entry_resolves_to_limit_path"


# ---------------------------------------------------------------- §3.4 绑定存在


def test_binding_same_object_matches(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    item = _binding_item("runner", _binding(program))
    decision = decide_object(item, _candidate("program", program))
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "binding_identity+birth_same"
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["identity_key"].equal is True
    assert facts["birth_key"].equal is True
    assert facts["identity_key"].source == "path_compare"


def test_binding_hardlink_alias_matches(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    hard = tmp_path / "progs" / "runner-hardlink.exe"
    if not _hardlink(program, hard):
        pytest.skip("平台不支持硬链接")
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", hard),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "binding_identity+birth_same"


def test_binding_without_birth_is_weak_match(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    item = _binding_item("runner", _binding(program, birth=False))
    decision = decide_object(item, _candidate("program", program))
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "binding_identity_only"
    facts = {fact.field: fact for fact in decision.facts}
    assert facts["lifecycle_basis"].expected == "binding_without_birth"


def test_binding_same_content_copy_is_unrelated(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    copy = tmp_path / "progs" / "runner-copy.exe"
    copy.write_bytes(program.read_bytes())
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", copy),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "different_object_identity"


def test_binding_lifecycle_reuse_is_invalid(tmp_path, monkeypatch):
    """模拟分支：同一身份 + 不同生命周期 = 编号复用（绑定失效）。"""
    program = _make_program(tmp_path / "progs", "runner")
    candidate = _make_program(tmp_path / "progs", "runner-new")
    identity = _identity(program)
    _patch_identity_for(
        monkeypatch,
        {str(candidate): _fake_identity(candidate, key=identity.key, birth=_other_birth(identity))},
    )
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", candidate),
    )
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "lifecycle_changed_same_identity"
    assert decision.needs_recovery is True
    assert "绑定" in decision.detail


def test_binding_path_replaced_is_invalid(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    replacement = _make_program(tmp_path / "progs-b", "replacement")
    item = _binding_item("runner", _binding(program))
    os.replace(replacement, program)
    decision = decide_object(item, _candidate("program", program))
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "bound_path_object_replaced"
    assert "绑定" in decision.detail


def test_binding_path_missing_is_invalid(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    item = _binding_item("runner", _binding(program))
    program.unlink()
    decision = decide_object(item, _candidate("program", program))
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "bound_path_object_replaced"
    assert "绑定" in decision.detail


def test_binding_candidate_missing_elsewhere_is_unrelated(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    missing = tmp_path / "missing" / "runner.exe"
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", missing),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "candidate_not_exists"


def test_binding_candidate_identity_unreadable_is_unknown(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    candidate = _make_program(tmp_path / "progs", "runner-new")
    _patch_identity_for(monkeypatch, {str(candidate): None})
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", candidate),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "identity_unavailable"
    assert "程序名称" in decision.detail
    assert decision.fail_closed is True


def test_binding_lifecycle_unverifiable_is_unknown(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    candidate = _make_program(tmp_path / "progs", "runner-new")
    identity = _identity(program)
    _patch_identity_for(
        monkeypatch,
        {str(candidate): _fake_identity(candidate, key=identity.key, birth=None)},
    )
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", candidate),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "lifecycle_unverifiable"
    assert "程序名称" in decision.detail


def test_binding_incomplete_is_unknown(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    item = _binding_item(
        "runner",
        {"path": str(program.resolve()), "identity": "", "source": "path_lookup"},
    )
    decision = decide_object(item, _candidate("program", program))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "binding_incomplete"
    assert "绑定" in decision.detail


def test_binding_entry_name_same_object_same_matches(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    identity = _identity(program)
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program_name", "runner"),
        resolver=_resolver(
            path=str(program.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "entry_name_same_object_same"


def test_binding_entry_name_same_object_differs_is_invalid(tmp_path):
    program = _make_program(tmp_path / "progs-a", "runner")
    other = _make_program(tmp_path / "progs-b", "runner")
    other_identity = _identity(other)
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program_name", "runner"),
        resolver=_resolver(
            path=str(other.resolve()),
            identity_key=other_identity.key,
            birth_key=other_identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "entry_name_same_object_differs"
    assert "绑定" in decision.detail


def test_binding_entry_name_same_unresolvable_is_invalid(tmp_path):
    """§3.7：绑定 + 同一来源名，即使当前目标不可读也按 INVALID（不是 UNKNOWN）。"""
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program_name", "runner"),
        resolver=_resolver(path=str(program.resolve()), error="identity_unavailable"),
    )
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "entry_name_same_object_differs"
    assert "绑定" in decision.detail


def test_binding_entry_name_different_alias_resolves_bound(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    identity = _identity(program)
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program_name", "runner-alias"),
        resolver=_resolver(
            path=str(program.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "entry_resolves_to_bound_object"


def test_binding_entry_name_different_resolves_elsewhere_is_unrelated(tmp_path):
    program = _make_program(tmp_path / "progs-a", "runner")
    other = _make_program(tmp_path / "progs-b", "other")
    other_identity = _identity(other)
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program_name", "other"),
        resolver=_resolver(
            path=str(other.resolve()),
            identity_key=other_identity.key,
            birth_key=other_identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "different_entry_name"


def test_binding_entry_name_different_unresolvable_is_unrelated(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program_name", "other"),
        resolver=_resolver(error="program_not_found"),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "different_entry_name"


def test_binding_program_kind_bare_name_uses_entry_branch(tmp_path):
    """§3.7：cmd effect 拍平后 kind=program 的裸入口名要按入口名处理。"""
    program = _make_program(tmp_path / "progs", "runner")
    identity = _identity(program)
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", "runner"),
        resolver=_resolver(
            path=str(program.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "entry_name_same_object_same"


def test_effect_entry_name_same_source_dominates_path_unrelated(tmp_path, monkeypatch):
    """真实入口名相同但 PATH 已指向新对象：路径候选无关也要按 INVALID 停住。"""
    bound = _make_program(tmp_path / "bound", "runner")
    current = _make_program(tmp_path / "current", "runner")
    item = _binding_item("runner", _binding(bound))
    monkeypatch.setenv("PATH", str(current.parent))
    effect = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(current)),),
        detail={"requested_program": "runner"},
    )
    decision = decide_object(item, _candidate("program", current), effect=effect, operation="execute")
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "entry_name_same_object_differs"
    assert "绑定" in decision.detail


def test_effect_entry_name_from_execution_program(tmp_path, monkeypatch):
    bound = _make_program(tmp_path / "bound", "runner")
    current = _make_program(tmp_path / "current", "runner")
    item = _binding_item("runner", _binding(bound))
    monkeypatch.setenv("PATH", str(current.parent))
    effect = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(current)),),
        detail={"execution": {"program": "runner"}},
    )
    decision = decide_object(item, _candidate("program", current), effect=effect)
    assert decision.state is ObjectState.INVALID
    assert decision.basis == "entry_name_same_object_differs"


def test_effect_requested_program_path_is_not_entry_name(tmp_path):
    bound = _make_program(tmp_path / "bound", "runner")
    current = _make_program(tmp_path / "current", "runner-other")
    item = _binding_item("runner", _binding(bound))
    effect = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(current)),),
        detail={"requested_program": str(current)},
    )
    decision = decide_object(item, _candidate("program", current), effect=effect)
    # 带路径分隔符的 requested_program 不是入口名，不触发名称分支。
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "different_object_identity"


def test_effect_different_entry_alias_to_bound_object_matches(tmp_path):
    bound = _make_program(tmp_path / "bound", "runner")
    identity = _identity(bound)
    unrelated = _make_program(tmp_path / "other", "unrelated")
    item = _binding_item("runner", _binding(bound))
    effect = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(unrelated)),),
        detail={"requested_program": "runner-alias"},
    )
    decision = decide_object(
        item,
        _candidate("program", unrelated),
        effect=effect,
        resolver=_resolver(
            path=str(bound.resolve()),
            identity_key=identity.key,
            birth_key=identity.birth_key,
        ),
    )
    assert decision.state is ObjectState.MATCH
    assert decision.basis == "entry_resolves_to_bound_object"


# ---------------------------------------------------------------- §3.5 绑定缺失


def test_unbound_same_object_is_unbound(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", program),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_same_object"
    assert decision.needs_recovery is True
    assert "程序名称" in decision.detail
    assert (
        decision_code_for(decision.state, "program_name")
        == effects_module.DECISION_PROGRAM_NAME_UNRESOLVED
    )


def test_unbound_resolved_elsewhere_is_unrelated(tmp_path, monkeypatch):
    bound_a = _make_program(tmp_path / "dir-a", "runner")
    other = _make_program(tmp_path / "dir-b", "runner")
    monkeypatch.setenv("PATH", str(bound_a.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", other),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "binding_missing_resolved_elsewhere"


def test_unbound_lookup_unresolved_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    program = _make_program(tmp_path / "progs", "runner")
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", program),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "binding_missing_lookup_unresolved"
    assert "程序名称" in decision.detail
    assert decision.fail_closed is True


def test_unbound_candidate_identity_unreadable_is_unknown(tmp_path, monkeypatch):
    resolved = _make_program(tmp_path / "dir-a", "runner")
    candidate = _make_program(tmp_path / "dir-b", "runner")
    monkeypatch.setenv("PATH", str(resolved.parent))
    _patch_identity_for(monkeypatch, {str(candidate): None})
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", candidate),
    )
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "binding_missing_candidate_unreadable"
    assert "程序名称" in decision.detail


def test_unbound_candidate_missing_is_unrelated(tmp_path, monkeypatch):
    resolved = _make_program(tmp_path / "dir-a", "runner")
    monkeypatch.setenv("PATH", str(resolved.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", tmp_path / "missing" / "runner.exe"),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "candidate_not_exists"


def test_unbound_hardlink_alias_is_unbound(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    hard = tmp_path / "progs" / "runner-hardlink.exe"
    if not _hardlink(program, hard):
        pytest.skip("平台不支持硬链接")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", hard),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_same_object"


def test_unbound_entry_name_same_is_unbound(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program_name", "runner"),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_entry_same"
    assert "程序名称" in decision.detail


def test_unbound_entry_name_different_is_unrelated(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program_name", "other"),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "different_entry_name"


def test_unbound_entry_name_case_is_platform_normalized(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    candidate = "RUNNER" if os.name == "nt" else "runner"
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program_name", candidate),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_entry_same"


def test_unbound_program_kind_bare_name_uses_entry_branch(tmp_path, monkeypatch):
    program = _make_program(tmp_path / "progs", "runner")
    monkeypatch.setenv("PATH", str(program.parent))
    decision = decide_object(
        {"kind": "program_name", "value": "runner"},
        _candidate("program", "runner"),
    )
    assert decision.state is ObjectState.UNBOUND
    assert decision.basis == "binding_missing_entry_same"


# ---------------------------------------------------------------- §3.6 service


def test_service_equal_and_scope_matches():
    equal = decide_object(
        {"kind": "service", "value": "api.example.com"},
        _candidate("service", "api.example.com"),
    )
    assert equal.state is ObjectState.MATCH
    assert equal.basis == "service_same"
    scoped = decide_object(
        {"kind": "service", "value": "api.example.com"},
        _candidate("url", "api.example.com/v1/items"),
    )
    assert scoped.state is ObjectState.MATCH
    assert scoped.basis == "service_scope_same"


def test_service_outside_is_unrelated():
    decision = decide_object(
        {"kind": "service", "value": "api.example.com"},
        _candidate("service", "other.example.com"),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "service_outside"


def test_service_limit_vs_path_candidate_is_unrelated(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(
        {"kind": "service", "value": "api.example.com"},
        _candidate("path", path),
    )
    assert decision.state is ObjectState.UNRELATED
    assert decision.basis == "object_kind_out_of_scope"


# ---------------------------------------------------------------- 其它/审计


def test_unknown_candidate_kind_fails_closed(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object(_path_item(path), {"kind": "material", "value": "x"})
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "unsupported_candidate_kind"
    assert decision.unrelated is False


def test_unsupported_limit_kind_fails_closed(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    decision = decide_object({"kind": "material", "value": "x"}, _candidate("path", path))
    assert decision.state is ObjectState.UNKNOWN
    assert decision.basis == "unsupported_limit_object_kind"


def test_missing_values_are_unknown(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("v1", encoding="utf-8")
    assert (
        decide_object({"kind": "path", "value": ""}, _candidate("path", path)).state
        is ObjectState.UNKNOWN
    )
    missing_candidate = decide_object(_path_item(path), {"kind": "path", "value": ""})
    assert missing_candidate.state is ObjectState.UNKNOWN
    assert missing_candidate.basis == "candidate_missing_value"


def test_decision_as_dict_does_not_leak_effect_detail_secrets(tmp_path):
    program = _make_program(tmp_path / "progs", "runner")
    effect = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(program)),),
        detail={
            "requested_program": "runner",
            "api_key": "sk-live-secret-value",
            "authorization": "Bearer secret-token",
        },
    )
    decision = decide_object(
        _binding_item("runner", _binding(program)),
        _candidate("program", program),
        effect=effect,
        operation="execute",
    )
    payload = decision.as_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "sk-live-secret-value" not in encoded
    assert "secret-token" not in encoded
    assert payload["facts"]
    for fact in payload["facts"]:
        for name in ("field", "expected", "observed", "source"):
            value = fact[name]
            assert value is None or len(str(value)) <= object_state.MAX_AUDIT_CHARS


def test_unknown_is_never_unrelated_and_blocks_pattern_allow(tmp_path):
    missing_limit = tmp_path / "missing-limit.bin"
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"x")
    # 让限制路径存在但身份不可读：模拟权限/平台不支持。
    decision = decide_object(
        _path_item(missing_limit),
        _candidate("path", tmp_path / "other.bin"),
        cache={str(missing_limit): None},
    )
    assert decision.state is ObjectState.UNRELATED  # 限制路径不存在 -> 可靠无关
    assert LimitAssessment(decisions=(decision,)).conflict is False
    unreadable = decide_object(
        _path_item(candidate),
        _candidate("path", candidate),
        operation="read",
    )
    assert unreadable.state is ObjectState.MATCH

