"""R9-H 执行前重核对：从持久化台账重新加载结构化限制（Lead 接线验收）。

真实 RequirementLedger（sqlite 文件）+ 真实 RecheckService/AuthorizationEngine；
验证重新构造 AuthContext 后限制仍生效，legacy 不会被当作“明确无限制”。
"""

from __future__ import annotations

import asyncio

from agent.security.effects import Effect, EffectKind, Target
from agent.security.recheck import OperationBinding, RecheckService
from agent.security.review import operation_fingerprint

from _r9_auth_support import make_security


def _write_effect(path):
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path), identity="vol:file"),),
        purpose="写文件",
    )


def _binding_for(security, requirement, effect):
    mode = security.engine.mode().value
    return OperationBinding(
        requirement=security.requirements.binding_for(requirement.requirement_id),
        mode=mode,
        operation_kind="write",
        fingerprint=operation_fingerprint(
            security.engine._review_operation(effect), mode=mode
        ),
    )


def test_recheck_reloads_persisted_structured_limit(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="只整理这个目录，不要修改 locked.txt", message_id="m1", turn_id="t1"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(tmp_path / "locked.txt")}],
                "reason": "用户明确不要修改这个文件",
            }
        ],
        source="test",
        state="structured",
    )
    requirement = security.requirements.by_id(requirement.requirement_id)
    effect = _write_effect(tmp_path / "locked.txt")
    service = RecheckService(engine=security.engine, requirements=security.requirements)
    result = asyncio.run(service.recheck(_binding_for(security, requirement, effect), effect))
    assert result.ok is False
    assert result.code == "denied"
    assert "不要修改" in result.reason or "限制" in result.reason


def test_recheck_allows_operation_outside_persisted_limit(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="只整理这个目录，不要修改 locked.txt", message_id="m1", turn_id="t1"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(tmp_path / "locked.txt")}],
                "reason": "用户明确不要修改这个文件",
            }
        ],
        source="test",
        state="structured",
    )
    requirement = security.requirements.by_id(requirement.requirement_id)
    effect = _write_effect(tmp_path / "other.txt")
    service = RecheckService(engine=security.engine, requirements=security.requirements)
    result = asyncio.run(service.recheck(_binding_for(security, requirement, effect), effect))
    assert result.ok is True, result.reason


def test_recheck_does_not_treat_legacy_limits_as_known_empty(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="帮我整理一下工作区", message_id="m1", turn_id="t1"
    )
    # 模拟历史行：结构化限制未知（legacy），不能当成“明确无限制”。
    security.requirements.set_structured_limits(
        requirement.requirement_id, None, source="legacy", state="legacy"
    )
    requirement = security.requirements.by_id(requirement.requirement_id)
    effect = _write_effect(tmp_path / "a.txt")
    service = RecheckService(engine=security.engine, requirements=security.requirements)
    result = asyncio.run(service.recheck(_binding_for(security, requirement, effect), effect))
    assert result.ok is False
    assert result.code == "needs_confirmation"
