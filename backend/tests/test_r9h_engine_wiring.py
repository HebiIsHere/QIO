"""R9-H 引擎接线守卫：真实 effect.detail → ReviewOperation → 指纹（Lead 负责）。

只验证引擎组装路径；工具是否填写这些事实由 B 的真实工具测试覆盖。
"""

from __future__ import annotations

import asyncio

from agent.security.effects import Effect, EffectKind, Target
from agent.security.review import operation_fingerprint

from _r9_auth_support import make_security


def _write_effect(path, **detail):
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path), identity="vol:file"),),
        purpose="写文件",
        detail=dict(detail),
    )


def test_engine_review_operation_carries_content_binding_fields(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    effect = _write_effect(
        tmp_path / "a.txt",
        content_digest="sha256:new-content",
        precondition_digest="sha256:before-content",
        spec_digest="sha256:launch-spec",
        script_sha256="sha256:script",
        delete_mode="recycle_bin",
        conditions=[{"kind": "test-passed", "satisfied": True, "evidence": "pytest"}],
    )
    operation = security.engine._review_operation(effect)
    assert operation.content_digest == "sha256:new-content"
    assert operation.precondition_digest == "sha256:before-content"
    assert operation.spec_digest == "sha256:launch-spec"
    assert operation.script_sha256 == "sha256:script"
    assert operation.delete_mode == "recycle_bin"
    assert operation.conditions and operation.conditions[0]["kind"] == "test-passed"


def test_same_identity_different_content_has_different_fingerprint(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = tmp_path / "a.txt"
    first = _write_effect(target, content_digest="sha256:A")
    second = _write_effect(target, content_digest="sha256:B")
    op_first = security.engine._review_operation(first)
    op_second = security.engine._review_operation(second)
    assert operation_fingerprint(op_first, mode="restricted") != operation_fingerprint(
        op_second, mode="restricted"
    )


def test_engine_accepts_legacy_tool_keys_content_sha256_and_base_content(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    effect = _write_effect(
        tmp_path / "a.txt",
        content_sha256="sha256:new",
        base_content_sha256="sha256:base",
    )
    operation = security.engine._review_operation(effect)
    assert operation.content_digest == "sha256:new"
    assert operation.precondition_digest == "sha256:base"


def test_delete_default_recycle_bin_and_permanent_flag(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = Target("path", str(tmp_path / "a.txt"))
    recycle = Effect(kind=EffectKind.DELETE, targets=(target,), purpose="删除")
    permanent = Effect(
        kind=EffectKind.DELETE,
        targets=(target,),
        purpose="永久删除",
        detail={"permanent": True},
    )
    assert security.engine._review_operation(recycle).delete_mode == "recycle_bin"
    assert security.engine._review_operation(permanent).delete_mode == "permanent"