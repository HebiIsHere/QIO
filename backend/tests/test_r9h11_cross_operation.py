"""R9-H11 C：文件/程序适配入口与审批恢复共用共同对象判定（跨操作一致性）。

模型一律 fake；引擎/台账/审批/临时文件/真实程序副本都是真实对象。
除「接口契约替身」（fake gateway 暴露冻结形状的 assess_limit_conflict）外，本文件
不复制判定逻辑：替身只返回固定结论，用来断言 C 侧接线确实消费了同一个 assessment，
并保持旧 fake gateway 的兼容回退。

末尾「真实引擎」用例在 engine.assess_limit_conflict（Lead task-1）尚未合入当前分支时
skip，合入后自动运行真实判定链路（skip 原因写清楚，不隐藏）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import AuthDecision, Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.operations import ExecutionContext, PendingOperation
from agent.security.recheck import OperationBinding, RecheckService
from agent.security.requirements import RequirementBinding
from agent.services.computer import ComputerSandbox
from agent.tools import fs_tools, security_gate
from agent.tools.approved_operations import _authorize_content_read
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsReadTool, FsWriteTool

from _r9_auth_support import make_security

#: 冻结契约 §2/§3 的决策码（与 effects.py / object_state.decision_code_for 一致）。
UNBOUND_CODE = "program_name_requires_object_binding"
INVALID_CODE = "program_binding_invalidated"
UNKNOWN_CODE = "object_state_unconfirmed"

_ENGINE_HAS_ASSESSMENT = hasattr(AuthorizationEngine, "assess_limit_conflict")
_ENGINE_REASON = (
    "engine.assess_limit_conflict 尚未合入当前分支（Lead task-1）；"
    "真实链路用例在集成分支运行，本次 skip，不隐藏"
)
engine_only = pytest.mark.skipif(not _ENGINE_HAS_ASSESSMENT, reason=_ENGINE_REASON)


# --------------------------------------------------------------------------
# 冻结接口的测试替身（只回固定结论，不复制判定逻辑）
# --------------------------------------------------------------------------


class _EnumValue:
    def __init__(self, value: str) -> None:
        self.value = value


class _StubDecision:
    """ObjectDecision 形状替身：state/basis/object_kind/as_dict。"""

    def __init__(
        self,
        state: str,
        *,
        object_kind: str = "program_name",
        object_value: str = "runner",
        basis: str = "stub_basis",
        operation: str | None = "read",
    ) -> None:
        self.state = _EnumValue(state)
        self.basis = basis
        self.operation = operation
        self.object_kind = object_kind
        self.object_value = object_value
        self.detail = "测试替身"

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "basis": self.basis,
            "operation": self.operation,
            "object_kind": self.object_kind,
            "object_value": self.object_value,
            "detail": self.detail,
        }


class _StubAssessment:
    """LimitAssessment 形状替身（decisions/reason/decision_code/conflict）。"""

    def __init__(self, decisions=(), *, reason="", decision_code=None) -> None:
        self.decisions = tuple(decisions)
        self.reason = reason
        self.decision_code = decision_code

    @property
    def conflict(self) -> bool:
        states = {item.state.value for item in self.decisions}
        return bool(states & {"match", "unbound", "invalid", "unknown"})


class _FakeGateway:
    """最小 fake gateway：authorize 只回预设 verdict；assessment 回固定替身。"""

    def __init__(
        self,
        *,
        mode: str = "unrestricted",
        verdict: Verdict = Verdict.ALLOW,
        reason: str = "fake verdict",
        decision_code: str | None = None,
        object_decisions: tuple = (),
        assessment=None,
        assessment_error: Exception | None = None,
    ) -> None:
        self._mode = mode
        self.verdict = verdict
        self.reason = reason
        self.decision_code_value = decision_code
        self.object_decisions_value = tuple(object_decisions)
        self.assessment = assessment
        self.assessment_error = assessment_error
        self.effects: list = []
        self.assessments: list = []

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        return AuthDecision(
            verdict=self.verdict,
            reason=self.reason,
            decision_code=self.decision_code_value,
            object_decisions=self.object_decisions_value,
        )

    def assess_limit_conflict(self, effect, requirement=None, *, extra_limits=()):
        self.assessments.append((effect, requirement, tuple(extra_limits)))
        if self.assessment_error is not None:
            raise self.assessment_error
        if callable(self.assessment):
            return self.assessment(effect)
        if self.assessment is not None:
            return self.assessment
        return _StubAssessment()


class _LegacyGateway:
    """旧 fake gateway：只有 _limit_conflict（布尔/字符串语义），没有 assessment。"""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self.effects: list = []

    def mode(self):
        return "unrestricted"

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        return AuthDecision(verdict=Verdict.ALLOW, reason="legacy allow")

    def _limit_conflict(self, effect, requirement=None, *, extra_limits=()):
        return self.reason


class _BareGateway:
    """最旧 fake gateway：既无 assessment 也无 _limit_conflict（保持兼容放行）。"""

    def mode(self):
        return "unrestricted"

    async def authorize(self, effect, *, ctx=None):
        return AuthDecision(verdict=Verdict.ALLOW, reason="legacy allow")


# --------------------------------------------------------------------------
# 夹具/辅助
# --------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _sandbox(root: Path) -> ComputerSandbox:
    root.mkdir(parents=True, exist_ok=True)
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "unrestricted"
    )


def _read_effect_for(path: Path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path)),),
        purpose="读取文件内容",
        detail={
            "action": "read",
            "limit_impacts": [
                {"operation": "read", "objects": [{"kind": "path", "value": str(path)}]}
            ],
            "limit_impacts_complete": True,
        },
    )


def _install_read_spies(monkeypatch, sink: list[str]) -> None:
    """正文/摘要读取 spy：Path.read_bytes/read_text + open 读取 + content_sha256。"""
    real_bytes = Path.read_bytes
    real_text = Path.read_text
    real_open_text = fs_tools._read_file_text
    real_sha = security_gate.content_sha256

    def spy_bytes(self, *args, **kwargs):
        sink.append("read_bytes:" + str(self))
        return real_bytes(self, *args, **kwargs)

    def spy_text(self, *args, **kwargs):
        sink.append("read_text:" + str(self))
        return real_text(self, *args, **kwargs)

    def spy_open_text(path):
        sink.append("open:" + str(path))
        return real_open_text(path)

    def spy_content_sha(path):
        sink.append("sha256:" + str(path))
        return real_sha(path)

    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(fs_tools, "_read_file_text", spy_open_text)
    monkeypatch.setattr(security_gate, "content_sha256", spy_content_sha)


def _pending_write_operation(security, requirement, effect) -> PendingOperation:
    return PendingOperation(
        kind="fs.write",
        label="写入文件",
        payload={"path": str(next(iter(effect.path_targets())).value)},
        effect=security_gate.effect_to_dict(effect),
        fingerprint={"kind": "fs.write"},
        purpose="写入文件",
        mode_at_request="restricted",
        requirement_id=requirement.requirement_id,
        requirement_version=int(requirement.version),
    )


def _exec_ctx(security, operation) -> ExecutionContext:
    return ExecutionContext(
        approval_id="appr-r9h11-c",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=security.engine,
    )


async def _wait_state(
    ctx, approval_id: str, states=("completed", "failed", "invalidated"), timeout=8
):
    deadline = asyncio.get_running_loop().time() + timeout
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = ctx.approvals.store.get(approval_id)
        if row and str(row.get("state")) in states:
            return row
        await asyncio.sleep(0.03)
    return row


def _runner(directory: Path, name: str = "runner") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt" and not name.lower().endswith(".exe"):
        name = name + ".exe"
    target = directory / name
    shutil.copy2(sys.executable, target)
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _write_raw_limits(conn, requirement_id: str, limits) -> None:
    """模拟历史数据：state=structured 但保存时未形成对象绑定。"""
    conn.execute(
        "UPDATE user_requirements SET structured_limits = ?,"
        " structured_limits_state = 'structured' WHERE id = ?",
        (json.dumps(list(limits), ensure_ascii=False), str(requirement_id)),
    )
    conn.commit()


# --------------------------------------------------------------------------
# 1) 共同 assessment 的消费视图（冻结接口形状 + 决策码兜底 + 旧网关兼容）
# --------------------------------------------------------------------------


def test_assessment_view_consumes_frozen_shape():
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("unbound")],
            reason="程序名称尚未可靠绑定到明确对象",
            decision_code=UNBOUND_CODE,
        )
    )
    view = security_gate.assess_limits(gateway, _read_effect_for(Path("x")), None)
    assert view.available is True
    assert view.conflict is True and view.denied is True
    assert view.blocking is True
    assert view.decision_code == UNBOUND_CODE
    assert view.decisions and view.decisions[0]["state"] == "unbound"
    assert view.as_dict()["decision_code"] == UNBOUND_CODE
    assert gateway.assessments and gateway.assessments[0][2] == ()


def test_assessment_view_derives_codes_when_engine_omits_them():
    effect = _read_effect_for(Path("x"))
    invalid = security_gate.assess_limits(
        _FakeGateway(assessment=_StubAssessment([_StubDecision("invalid")])), effect, None
    )
    assert invalid.conflict is True and invalid.decision_code == INVALID_CODE
    unknown = security_gate.assess_limits(
        _FakeGateway(assessment=_StubAssessment([_StubDecision("unknown")])), effect, None
    )
    assert unknown.conflict is True and unknown.decision_code == UNKNOWN_CODE
    unrelated = security_gate.assess_limits(
        _FakeGateway(
            assessment=_StubAssessment(
                [_StubDecision("unrelated", basis="operation_out_of_scope")]
            )
        ),
        effect,
        None,
    )
    assert unrelated.conflict is False and unrelated.decision_code is None


def test_assessment_view_keeps_legacy_gateway_fallback():
    effect = _read_effect_for(Path("x"))
    legacy = security_gate.assess_limits(
        _LegacyGateway("用户限制禁止读取这个文件"), effect, None
    )
    assert legacy.available is False
    assert legacy.conflict is True
    assert "用户限制" in legacy.reason
    clean = security_gate.assess_limits(_LegacyGateway(None), effect, None)
    assert clean.conflict is False
    bare = security_gate.assess_limits(_BareGateway(), effect, None)
    assert bare.available is False and bare.conflict is False


# --------------------------------------------------------------------------
# 2) 读取边界：真正读取前消费 assessment；拒绝时读取次数为 0
# --------------------------------------------------------------------------


async def test_fs_read_assessment_denial_reads_nothing(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    target = root / "secret.txt"
    root.mkdir()
    target.write_text("原始正文", encoding="utf-8")
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("unbound")],
            reason="程序名称尚未可靠绑定到明确对象；需先恢复",
            decision_code=UNBOUND_CODE,
        )
    )
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    tool = FsReadTool(computer=_sandbox(root), security=gateway)
    result = await tool.run(path=str(target))
    assert result.ok is False, result
    assert reads == [], "拒绝前不得读取正文"
    assert result.code == UNBOUND_CODE
    assert result.data and result.data["decision_code"] == UNBOUND_CODE
    assert gateway.effects == [], "判定命中时不得进入普通授权/审批（普通批准不能解除）"
    assert target.read_text(encoding="utf-8") == "原始正文"


async def test_fs_read_assessment_failure_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    target = root / "secret.txt"
    root.mkdir()
    target.write_text("body", encoding="utf-8")
    gateway = _FakeGateway(assessment_error=RuntimeError("assessment boom"))
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    result = await FsReadTool(computer=_sandbox(root), security=gateway).run(
        path=str(target)
    )
    assert result.ok is False, result
    assert reads == [], "判定失败不得先读后拒"
    assert result.data and result.data["decision_code"] == UNKNOWN_CODE


async def test_fs_read_unrelated_assessment_allows_read(tmp_path):
    root = tmp_path / "workspace"
    target = root / "note.txt"
    root.mkdir()
    target.write_text("body", encoding="utf-8")
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("unrelated", basis="operation_out_of_scope")]
        )
    )
    result = await FsReadTool(computer=_sandbox(root), security=gateway).run(
        path=str(target)
    )
    assert result.ok is True, result
    assert result.content == "body"
    assert gateway.effects, "无关对象仍走普通授权路径"


async def test_fs_write_assessment_denial_before_content_hash(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    target = root / "write.txt"
    root.mkdir()
    target.write_text("original", encoding="utf-8")
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("unknown", object_kind="path")],
            reason="事实不足，无法确认对象",
            decision_code=UNKNOWN_CODE,
        )
    )
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    result = await FsWriteTool(computer=_sandbox(root), security=gateway).run(
        path=str(target), content="new-content"
    )
    assert result.ok is False, result
    assert reads == [], "写入前不得读取原内容/生成摘要"
    assert result.code == UNKNOWN_CODE
    assert target.read_text(encoding="utf-8") == "original"


async def test_fs_patch_assessment_denial_before_body_read(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    target = root / "patch.txt"
    root.mkdir()
    target.write_text("old-value", encoding="utf-8")
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("invalid")],
            reason="绑定失效：原路径对象已被替换",
            decision_code=INVALID_CODE,
        )
    )
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    result = await FsPatchTool(computer=_sandbox(root), security=gateway).run(
        path=str(target), old="old-value", new="new-value"
    )
    assert result.ok is False, result
    assert reads == [], "补丁不得先读正文再拒绝"
    assert result.code == INVALID_CODE
    assert target.read_text(encoding="utf-8") == "old-value"


async def test_fs_delete_assessment_denial_before_content_digest(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    target = root / "delete.txt"
    root.mkdir()
    target.write_text("secret", encoding="utf-8")
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("unbound")],
            reason="读取限制未通过：对象无法确认",
            decision_code=UNBOUND_CODE,
        )
    )
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    result = await FsDeleteTool(computer=_sandbox(root), security=gateway).run(
        paths=[str(target)]
    )
    assert result.ok is False, result
    assert reads == [], "删除前不得读取被限制的内容/摘要"
    assert target.exists(), "被拒绝的删除必须保留文件"


# --------------------------------------------------------------------------
# 3) 程序执行：先共同 assessment 后启动；拒绝携带 decision_code 且不启动
# --------------------------------------------------------------------------


async def test_run_program_assessment_denial_blocks_execution(ctx, tmp_path):
    root = Path(ctx.computer.root())
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "exec-marker.txt"
    script = root / "exec.py"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('ran')\n" % marker,
        encoding="utf-8",
    )
    gateway = _FakeGateway(
        assessment=_StubAssessment(
            [_StubDecision("unbound")],
            reason="名称限制未绑定对象：需先恢复",
            decision_code=UNBOUND_CODE,
        )
    )
    tool = RunProgramTool(computer=ctx.computer, security=gateway)
    result = await tool.run(program=sys.executable, args=[str(script)], timeout=30)
    assert result.ok is False, result
    assert result.code == UNBOUND_CODE
    assert result.data and result.data["decision_code"] == UNBOUND_CODE
    assert not marker.exists(), "判定命中时不得启动程序"
    assert gateway.effects == [], "判定命中时不得进入授权/审批"


async def test_run_program_authorization_denial_carries_decision_code(ctx):
    gateway = _FakeGateway(
        verdict=Verdict.DENY,
        reason="这条用户限制禁止执行该程序",
        decision_code=UNBOUND_CODE,
        object_decisions=({"state": "unbound", "object_kind": "program_name"},),
    )
    tool = RunProgramTool(computer=ctx.computer, security=gateway)
    result = await tool.run(program=sys.executable, args=["-c", "print(1)"], timeout=30)
    assert result.ok is False, result
    assert result.code == UNBOUND_CODE
    assert result.data["decision_code"] == UNBOUND_CODE
    assert result.data["object_decisions"][0]["state"] == "unbound"


# --------------------------------------------------------------------------
# 4) 审批恢复：operations.authorize_started / approved_operations / recheck.py
# --------------------------------------------------------------------------


def _read_only_denial_gateway(*, decision_code=UNBOUND_CODE):
    """给真实引擎装上冻结接口替身：只拒绝 READ effect（模拟对象判定 UNBOUND）。"""

    def fake_assess(effect, requirement=None, *, extra_limits=()):
        kind = str(getattr(getattr(effect, "kind", None), "value", effect.kind))
        if kind == "read":
            return _StubAssessment(
                [_StubDecision("unbound")],
                reason="程序名称尚未可靠绑定到明确对象",
                decision_code=decision_code,
            )
        return _StubAssessment()

    return fake_assess


async def test_authorize_started_consumes_assessment_before_recheck(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = security.workspace / "recovery.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    requirement = security.requirements.record_user_message(
        text="写入文件", message_id="m-recovery", turn_id="t-recovery"
    )
    effect = security_gate.write_effect(
        "fs.write", target, purpose="写入文件", content_sha256="abc"
    )
    operation = _pending_write_operation(security, requirement, effect)
    calls = {"recheck": 0}
    real_recheck = security.engine.recheck_approved

    def spy_recheck(*args, **kwargs):
        calls["recheck"] += 1
        return real_recheck(*args, **kwargs)

    security.engine.recheck_approved = spy_recheck
    security.engine.assess_limit_conflict = _read_only_denial_gateway()
    read_effect = _read_effect_for(target)
    try:
        exec_ctx = _exec_ctx(security, operation)
        allowed, reason = await exec_ctx.authorize_started(read_effect)
    finally:
        security.engine.recheck_approved = real_recheck
        del security.engine.assess_limit_conflict
    assert allowed is False, reason
    assert calls["recheck"] == 0, "共同 assessment 必须先于 recheck_approved 的放行分支"
    assert exec_ctx.authorization_basis.get("decision_code") == UNBOUND_CODE
    assert exec_ctx.authorization_basis.get("kind") == "limit_assessment_denied"
    assert "绑定" in reason or "限制" in reason or "未执行" in reason


async def test_approved_content_read_failure_carries_decision_code(
    db_conn, tmp_path, monkeypatch
):
    security = make_security(tmp_path, db_conn, mode="restricted")
    target = security.workspace / "recovery-read.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    requirement = security.requirements.record_user_message(
        text="写入文件", message_id="m-recovery-read", turn_id="t-recovery-read"
    )
    effect = security_gate.write_effect(
        "fs.write", target, purpose="写入文件", content_sha256="abc"
    )
    operation = _pending_write_operation(security, requirement, effect)
    security.engine.assess_limit_conflict = _read_only_denial_gateway(
        decision_code=INVALID_CODE
    )
    reads: list[str] = []
    real_bytes = Path.read_bytes
    real_text = Path.read_text

    def spy_bytes(self, *args, **kwargs):
        reads.append("read_bytes:" + str(self))
        return real_bytes(self, *args, **kwargs)

    def spy_text(self, *args, **kwargs):
        reads.append("read_text:" + str(self))
        return real_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", spy_bytes)
    monkeypatch.setattr(Path, "read_text", spy_text)
    try:
        exec_ctx = _exec_ctx(security, operation)
        result = await _authorize_content_read(exec_ctx, str(target), purpose="读取原内容")
    finally:
        del security.engine.assess_limit_conflict
    assert result is not None and result.ok is False, result
    assert result.detail.get("decision_code") == INVALID_CODE
    assert reads == [], "审批恢复的读取核对不得先读正文"


async def test_recheck_service_denies_on_assessment_before_content_digest(
    db_conn, tmp_path, monkeypatch
):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    target = security.workspace / "recheck.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    requirement = security.requirements.record_user_message(
        text="写入文件", message_id="m-recheck", turn_id="t-recheck"
    )
    binding = OperationBinding(
        requirement=RequirementBinding(
            requirement_id=requirement.requirement_id, version=int(requirement.version)
        ),
        mode="unrestricted",
        operation_kind="fs.write",
        content_digests=((str(target), "deadbeef"),),
    )
    security.engine.assess_limit_conflict = (
        lambda effect, requirement=None, *, extra_limits=(): _StubAssessment(
            [_StubDecision("unknown", object_kind="path")],
            reason="事实不足，无法确认对象",
            decision_code=UNKNOWN_CODE,
        )
    )
    reads: list[str] = []
    import agent.security.recheck as recheck_module

    monkeypatch.setattr(
        recheck_module,
        "content_digest",
        lambda path: reads.append(str(path)) or "deadbeef",
    )
    service = RecheckService(engine=security.engine, requirements=security.requirements)
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(target)),),
        purpose="写入文件",
        detail={"action": "write"},
    )
    try:
        result = await service.recheck(binding, effect)
    finally:
        del security.engine.assess_limit_conflict
    assert result.ok is False, result
    assert result.code == "denied"
    assert result.decision_code == UNKNOWN_CODE
    assert reads == [], "共同判定拒绝时不得读取内容摘要"
    assert str(getattr(result.decision, "decision_code", "")) == UNKNOWN_CODE


async def test_denied_result_carries_structured_decisions():
    decision = AuthDecision(
        verdict=Verdict.DENY,
        reason="禁止执行",
        decision_code=UNBOUND_CODE,
        object_decisions=(
            {"state": "unbound", "object_kind": "program_name", "basis": "binding_missing"},
        ),
    )
    result = security_gate.denied_result(decision, "默认拒绝")
    assert result.ok is False
    assert result.code == UNBOUND_CODE
    assert result.data and result.data["decision_code"] == UNBOUND_CODE
    assert result.data["object_decisions"][0]["state"] == "unbound"


# --------------------------------------------------------------------------
# 5) 真实引擎链路（Lead 合入 assess_limit_conflict 后自动运行）
# --------------------------------------------------------------------------


@engine_only
async def test_engine_native_unbound_program_name_denies_read_zero_reads(
    ctx, db_conn, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    monkeypatch.setenv(
        "PATH", str(runner.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    requirement = ctx.requirements.record_user_message(
        text="禁止读取 runner", message_id="m-native-read", turn_id="t-native-read"
    )
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["read"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止读取 runner",
            }
        ],
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    tool = FsReadTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(runner))
    assert result.ok is False, result
    assert reads == [], "UNBOUND 对象判定必须在正文读取前拒绝"
    assert result.data and result.data.get("decision_code") == UNBOUND_CODE, result


@engine_only
async def test_engine_native_operation_scope_filtering(ctx, db_conn, tmp_path, monkeypatch):
    runner = _runner(tmp_path / "bin")
    monkeypatch.setenv(
        "PATH", str(runner.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    execute_effect = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(runner)),),
        purpose="运行",
        detail={"requested_program": "runner", "execution": {"program": "runner"}},
    )
    # write-only 程序限制不得扩大为 execute 限制。
    write_only = ctx.requirements.record_user_message(
        text="禁止修改 runner", message_id="m-native-w", turn_id="t-native-w"
    )
    ctx.requirements.set_structured_limits(
        write_only.requirement_id,
        [
            {
                "operations": ["write"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止修改",
            }
        ],
        source="test-native-write",
        state=requirement_limits.STATE_STRUCTURED,
    )
    write_assessment = ctx.authorization.assess_limit_conflict(
        execute_effect, ctx.requirements.by_id(write_only.requirement_id)
    )
    assert write_assessment.conflict is False, "write-only 程序限制不得阻止 execute"

    # execute 限制不阻止读取无关文件。
    other = tmp_path / "note.txt"
    other.write_text("x", encoding="utf-8")
    execute_only = ctx.requirements.record_user_message(
        text="禁止运行 runner", message_id="m-native-x", turn_id="t-native-x"
    )
    ctx.requirements.set_structured_limits(
        execute_only.requirement_id,
        [
            {
                "operations": ["execute"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止运行",
            }
        ],
        source="test-native-exec",
        state=requirement_limits.STATE_STRUCTURED,
    )
    read_assessment = ctx.authorization.assess_limit_conflict(
        _read_effect_for(other), ctx.requirements.by_id(execute_only.requirement_id)
    )
    assert read_assessment.conflict is False, "execute 限制不得阻止无关读取正文"

    # 候选对象路径无法规范化：事实不足 → 拒绝（不得把未确定对象当无关放行）。
    path_limit = ctx.requirements.record_user_message(
        text="禁止修改该文件", message_id="m-native-m", turn_id="t-native-m"
    )
    ctx.requirements.set_structured_limits(
        path_limit.requirement_id,
        [
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(runner)}],
                "reason": "禁止修改",
            }
        ],
        source="test-native-unresolvable",
        state=requirement_limits.STATE_STRUCTURED,
    )
    unresolved_effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", "bad\x00path"),),
        purpose="写入",
        detail={"action": "write"},
    )
    unresolved_assessment = ctx.authorization.assess_limit_conflict(
        unresolved_effect, ctx.requirements.by_id(path_limit.requirement_id)
    )
    assert unresolved_assessment.conflict is True, "未确定对象不得当无关放行"
    assert unresolved_assessment.decision_code == UNKNOWN_CODE


@engine_only
async def test_engine_native_approval_recovery_refuses_unbound_read(
    ctx, db_conn, tmp_path, monkeypatch
):
    runner = _runner(tmp_path / "bin")
    monkeypatch.setenv(
        "PATH", str(runner.parent) + os.pathsep + os.environ.get("PATH", "")
    )
    ctx.settings_store.set("computer.permission_mode", "restricted")
    requirement = ctx.requirements.record_user_message(
        text="写入该程序文件", message_id="m-native-rec", turn_id="t-native-rec"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(path=str(runner), content="overwritten")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    # 批准期间要求被重新理解为：“禁止读取 runner”，但历史数据没有对象绑定。
    _write_raw_limits(
        db_conn,
        requirement.requirement_id,
        [
            {
                "operations": ["read"],
                "objects": [{"kind": "program_name", "value": "runner"}],
                "reason": "禁止读取 runner",
            }
        ],
    )
    original = runner.read_bytes()
    reads: list[str] = []
    _install_read_spies(monkeypatch, reads)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert reads == [], "审批恢复必须在读取正文/摘要前拒绝"
    assert runner.read_bytes() == original, "被拒绝的审批不得改写文件"
