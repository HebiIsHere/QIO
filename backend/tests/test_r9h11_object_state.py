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
from agent.security.object_state import (
    IdentityComparison,
    LimitAssessment,
    ObjectDecision,
    ObjectFact,
    ObjectState,
    ProgramResolution,
    compare_bound_identity,
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
    assert resolve_path("a\\b.txt", str(tmp_path)) == str((tmp_path / "a" / "b.txt").resolve())


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
