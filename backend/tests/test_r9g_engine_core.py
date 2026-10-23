"""R9-G 引擎核心：结构化限制 / 要求绑定 / 批量逐项 / 统一执行前重核对。

这些用例只用真实 AuthorizationEngine + 最小需求台账替身；模型审查/命令分类等
由 W1/W2 分支的专门测试覆盖。目的是守住"关键词不再决定权限、绑定不能回退
最近一条、批量不聚合、重核对按最新状态"。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.recheck import OperationBinding, RecheckService, content_digest
from agent.security.review import operation_fingerprint
from agent.storage.settings import SettingsStore


class FakeRequirements:
    """最小需求台账：by_id 返回 active/version；current() 被调用即证明回退 bug。"""

    def __init__(self) -> None:
        self.items: dict[str, SimpleNamespace] = {}
        self.current_calls = 0

    def add(self, *, text="要求", version=1, status="active", limits=()):
        requirement_id = "req_%d" % (len(self.items) + 1)
        self.items[requirement_id] = SimpleNamespace(
            requirement_id=requirement_id,
            text=text,
            version=version,
            status=status,
            limits=tuple(limits),
            parent_id=None,
            corrections=(),
        )
        return self.items[requirement_id]

    def by_id(self, requirement_id):
        return self.items.get(str(requirement_id))

    def current(self):  # pragma: no cover - 引擎不得再使用"最近一条"
        self.current_calls += 1
        if not self.items:
            return None
        return list(self.items.values())[-1]


class FakeAuthorizations:
    def __init__(self) -> None:
        self.records = []

    def reuse(self, *, requirement_id, operation_kind, fingerprint):
        for record in self.records:
            if (
                record.requirement_id == requirement_id
                and record.operation_kind == operation_kind
                and record.fingerprint == fingerprint
                and record.revoked_at is None
            ):
                return record
        return None

    def records_for_requirement(self, requirement_id):
        return [r for r in self.records if r.requirement_id == requirement_id]

    def record(self, record):
        self.records.append(record)


def make_engine(tmp_path, conn, *, mode="restricted", requirements=None, authorizations=None):
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    return AuthorizationEngine(
        conn=conn,
        settings=store,
        workspace_root=lambda: tmp_path,
        requirements=requirements or FakeRequirements(),
        authorizations=authorizations or FakeAuthorizations(),
    )


def write_effect(path, **detail):
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path), identity="vol:file"),),
        purpose="修改文件",
        detail=dict(detail),
    )


# -- 结构化限制：不再扫描原话，只有模型理解出的结构化 limits 生效 ----------------


def test_structured_limit_denies_matching_write_in_unrestricted(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirement = requirements.add(
        limits=(
            {
                "operations": ["write"],
                "objects": [{"kind": "path_prefix", "value": str(tmp_path)}],
                "reason": "用户明确限制不要修改这个目录",
            },
        )
    )
    engine = make_engine(
        tmp_path, db_conn, mode="unrestricted", requirements=requirements
    )
    with requirement_context(requirement):
        write = asyncio.run(engine.authorize(write_effect(tmp_path / "a.txt")))
        # 不在受限前缀内的另一个目录（不存在也可判定，只看路径关系）。
        outside_path = tmp_path.parent / "r9g-outside" / "b.txt"
        outside = asyncio.run(engine.authorize(write_effect(outside_path)))
        assert write.verdict == Verdict.DENY
        assert "限制" in write.reason
        assert outside.verdict == Verdict.ALLOW
        assert requirements.current_calls == 0, "引擎不得回退到最近一条要求"


def test_legacy_text_limits_do_not_drive_decisions(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirement = requirements.add(limits=("不要修改 /any/path", "只分析"))
    engine = make_engine(
        tmp_path, db_conn, mode="unrestricted", requirements=requirements
    )
    with requirement_context(requirement):
        decision = asyncio.run(engine.authorize(write_effect(tmp_path / "a.txt")))
    assert decision.verdict == Verdict.ALLOW, "旧文本限制不解析、不用关键词误判"


def test_limit_matches_only_structured_objects(tmp_path, db_conn):
    requirements = FakeRequirements()
    requirement = requirements.add(
        limits=(
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(tmp_path / "locked.txt")}],
                "reason": "只锁定一个文件",
            },
        )
    )
    engine = make_engine(
        tmp_path, db_conn, mode="unrestricted", requirements=requirements
    )
    with requirement_context(requirement):
        locked = asyncio.run(engine.authorize(write_effect(tmp_path / "locked.txt")))
        other = asyncio.run(engine.authorize(write_effect(tmp_path / "other.txt")))
    assert locked.verdict == Verdict.DENY
    assert other.verdict == Verdict.ALLOW


# -- 要求绑定：只用绑定，缺失不回退 ----------------------------------------------


def test_restricted_write_without_requirement_binding_needs_confirmation(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirements.add(text="把报告改一下")
    engine = make_engine(tmp_path, db_conn, mode="restricted", requirements=requirements)
    decision = asyncio.run(engine.authorize(write_effect(tmp_path / "a.txt")))
    assert decision.verdict == Verdict.APPROVE
    assert decision.needs_review is True
    assert ("无法确认授权范围" in decision.reason) or ("没有可对照" in decision.reason)
    assert requirements.current_calls == 0


def test_withdrawn_requirement_binding_is_invalid(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirement = requirements.add(text="把报告改一下")
    requirement.status = "withdrawn"
    engine = make_engine(tmp_path, db_conn, mode="restricted", requirements=requirements)
    binding = SimpleNamespace(requirement_id=requirement.requirement_id, version=1)

    with requirement_context(binding):
        decision = asyncio.run(engine.authorize(write_effect(tmp_path / "a.txt")))
    assert decision.verdict == Verdict.APPROVE
    assert decision.needs_review is True


# -- 批量：逐项判定，不聚合 ------------------------------------------------------


def test_authorize_batch_returns_one_decision_per_item(db_conn, tmp_path):
    engine = make_engine(tmp_path, db_conn, mode="read_only")
    read = Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(tmp_path / "a.txt")),),
    )
    write = write_effect(tmp_path / "b.txt")
    decisions = asyncio.run(engine.authorize_batch([read, write]))
    assert [decision.verdict for decision in decisions] == [Verdict.ALLOW, Verdict.DENY]


# -- 执行前重核对 ---------------------------------------------------------------


def _binding_for(requirement, effect, *, fingerprint=None, content=()):
    engine_probe = SimpleNamespace(_review_operation=None, mode=lambda: SimpleNamespace(value="unrestricted"))
    actual = fingerprint
    return OperationBinding(
        requirement=SimpleNamespace(requirement_id=requirement.requirement_id, version=requirement.version),
        mode="unrestricted",
        operation_kind="write",
        fingerprint=actual or "",
        content_digests=tuple(content),
        conditions=(),
    )


def test_recheck_ok_with_active_requirement_and_same_fingerprint(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirement = requirements.add()
    engine = make_engine(
        tmp_path, db_conn, mode="unrestricted", requirements=requirements
    )
    effect = write_effect(tmp_path / "a.txt")
    operation = engine._review_operation(effect)
    expected = operation_fingerprint(operation, mode="unrestricted")
    service = RecheckService(engine=engine, requirements=requirements)
    binding = OperationBinding(
        requirement=SimpleNamespace(
            requirement_id=requirement.requirement_id, version=requirement.version
        ),
        mode="unrestricted",
        operation_kind="write",
        fingerprint=expected,
    )
    result = asyncio.run(service.recheck(binding, effect))
    assert result.ok is True, result.reason


def test_recheck_fails_when_requirement_version_changed(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirement = requirements.add()
    engine = make_engine(
        tmp_path, db_conn, mode="unrestricted", requirements=requirements
    )
    effect = write_effect(tmp_path / "a.txt")
    service = RecheckService(engine=engine, requirements=requirements)
    binding = OperationBinding(
        requirement=SimpleNamespace(
            requirement_id=requirement.requirement_id, version=requirement.version + 1
        ),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    result = asyncio.run(service.recheck(binding, effect))
    assert result.ok is False
    assert result.code == "requirement_invalid"


def test_recheck_fails_when_operation_content_changed(db_conn, tmp_path):
    requirements = FakeRequirements()
    requirement = requirements.add()
    engine = make_engine(
        tmp_path, db_conn, mode="unrestricted", requirements=requirements
    )
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    effect = write_effect(target, content_sha256="new")
    binding = OperationBinding(
        requirement=SimpleNamespace(
            requirement_id=requirement.requirement_id, version=requirement.version
        ),
        mode="unrestricted",
        operation_kind="write",
        fingerprint=operation_fingerprint(
            engine._review_operation(effect), mode="unrestricted"
        ),
        content_digests=((str(target), "stale-digest"),),
    )
    service = RecheckService(engine=engine, requirements=requirements)
    result = asyncio.run(service.recheck(binding, effect))
    assert result.ok is False
    assert result.code == "content_changed"


def test_recheck_without_binding_never_executes():
    class _Engine:
        def mode(self):  # pragma: no cover - 不应被调用
            raise AssertionError("no binding should fail before engine decision")

    service = RecheckService(engine=_Engine(), requirements=FakeRequirements())
    result = asyncio.run(service.recheck(None, write_effect("x")))
    assert result.ok is False
    assert result.code == "no_binding"
