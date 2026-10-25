"""R9-F 端到端验收（W4）：真实应用装配下的授权链路（A/B/C/D + 三模式 + 内部数据）。

与 test_r9_acceptance_e2e.py 的分工：后者守住 R9 既有结论；本文件按 **R9-F 冻结契约**
（docs/r9f-contracts.md）覆盖修复轮的四条验收：

* A 程序与审批后执行：只读拒绝、受限→审批→真实执行、审批单绑定 program/argv；
* B 模型审查三态与否定/引用/对象解析：**W1 的 review.py/requirements.py 尚未合入本分支**，
  相关用例先按冻结接口写、标 xfail（strict=False，可运行、不隐藏），合入后应转为绿；
* C 工作区来源自主：引擎对 qio_generated 的自主写规则（现可验）+ 来源持久化后重启仍自主（W3 待合入）；
* D 项目必要执行不复审批：现行为（每次审批）与契约目标（有界审查记录）分别覆盖；
* 三模式判定矩阵；内部数据三次确认在所有模式都生效。

假件边界：模型调用一律 FakeAdapter（项目约束）；AuthorizationEngine / 注册表 / 审批服务 /
工具执行全部是真实装配。xfail 只用于"依赖分支未合入"，不用于放宽既有断言。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ToolCall
from agent.api.server import create_app
from agent.config import Settings
from agent.security.context import instruction_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.storage.settings import SettingsStore

#: 依赖分支未合入时的统一标注（合入后把 strict 打开即可当验收）。
PENDING_W1 = "R9-F W1（security/review.py + requirements.py）未合入本分支：待集成后转绿"
PENDING_W2 = "R9-F W2（ControlledLauncher/permit 重核对）未合入本分支：待集成后转绿"
PENDING_W3 = "R9-F W3（services/file_origins.py 持久化来源）未合入本分支：待集成后转绿"


@pytest.fixture()
def app_client(db_conn, settings: Settings, tmp_path):
    """真实应用；工作区根放到临时目录，避免碰用户真实工作区。"""
    from agent.credentials.store import MemoryKeyring

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SettingsStore(db_conn)
    store.set("computer.root_dir", str(workspace))
    store.set("computer.permission_mode", "restricted")  # 首次安装默认（契约 §1）
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        client.workspace = workspace
        yield client


def _binding_for_client(client: TestClient):
    """R9-G：给工具/引擎一个真实存在的用户要求绑定（生产由 turn 要求解析建立）。"""
    binding = getattr(client, "_r9g_binding", None)
    if binding is not None:
        return binding
    from agent.security.context import current_requirement

    binding = current_requirement.get()
    if binding is not None:
        return binding
    ctx = client.app.state.ctx
    requirement = ctx.requirements.record_user_message(
        text="按我的要求执行这次操作", message_id="msg_r9f", turn_id="turn_r9f"
    )
    binder = getattr(ctx.requirements, "binding_for", None)
    binding = binder(requirement.requirement_id) if callable(binder) else None
    if binding is None:
        from types import SimpleNamespace

        binding = SimpleNamespace(
            requirement_id=requirement.requirement_id,
            version=int(getattr(requirement, "version", 1) or 1),
        )
    client._r9g_binding = binding
    return binding


def _exec(client: TestClient, name: str, arguments: dict):
    async def _do():
        from agent.security.context import requirement_context

        with requirement_context(_binding_for_client(client)):
            return await client.app.state.ctx.registry.execute(
                ToolCall(id="call_r9f", name=name, arguments=arguments)
            )

    return client.portal.call(_do)


def _pending(client: TestClient) -> list[dict]:
    resp = client.get("/api/approvals/pending")
    assert resp.status_code == 200
    return resp.json()["approvals"]


def _set_mode(client: TestClient, mode: str) -> None:
    resp = client.put("/api/security/mode", json={"mode": mode, "user_confirmed": True})
    assert resp.status_code == 200, resp.text


def _wait_state(client: TestClient, approval_id: str, state: str, *, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = client.app.state.ctx.conn.execute(
            "SELECT state, result, error FROM pending_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is not None and row["state"] == state:
            return dict(row)
        time.sleep(0.05)
    raise AssertionError(f"审批 {approval_id} 未在 {timeout}s 内进入 {state}")


def _decide(client: TestClient, approval_id: str, decision: str):
    return client.post(f"/api/approvals/{approval_id}/decide", json={"decision": decision})


def _authorize(client: TestClient, effect: Effect):
    async def _do():
        from agent.security.context import requirement_context

        with requirement_context(_binding_for_client(client)):
            return await client.app.state.ctx.authorization.authorize(effect)

    return client.portal.call(_do)


class _FakeReviewer:
    """可编程授权审查器替身：只替代模型调用，三态/校验仍走真实 review 模块。"""

    def __init__(self, decision=None):
        from agent.security.review import ReviewDecision

        self.decision = decision or ReviewDecision.ALLOW
        self.calls = []

    async def review(self, request):
        from agent.security.review import ReviewDecision, ReviewOutcome

        self.calls.append(request)
        allow = self.decision == ReviewDecision.ALLOW
        op = request.operation
        return ReviewOutcome(
            decision=self.decision,
            reason="依据用户要求",
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(op.kind,) if allow else (),
            objects=op.objects if allow else (),
            limits=("仅限本次操作",) if allow else (),
            purpose=op.summary or "用户要求",
        )


def _install_reviewer(client: TestClient, *, text: str, decision=None):
    """登记一条真实用户要求并注入审查替身（模型调用是唯一允许的 fake）。"""
    ctx = client.app.state.ctx
    requirement = ctx.requirements.record_user_message(
        text=text, message_id="msg_fake", turn_id="turn_fake", topic_id=None
    )
    binder = getattr(ctx.requirements, "binding_for", None)
    binding = binder(requirement.requirement_id) if callable(binder) else None
    if binding is None:
        from types import SimpleNamespace

        binding = SimpleNamespace(
            requirement_id=requirement.requirement_id,
            version=int(getattr(requirement, "version", 1) or 1),
        )
    client._r9g_binding = binding
    reviewer = _FakeReviewer(decision)
    ctx.authorization.reviewer = reviewer
    return ctx, requirement, reviewer


def _internal_target(client: TestClient) -> str:
    """内部运行数据目标：data_dir/tmp 下的文件（在受保护清单内，且不碰真实 DB）。"""
    target = Path(client.app.state.ctx.settings.data_dir) / "tmp" / "internal-note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def _write_effect(path, **detail) -> Effect:
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target(kind="path", value=str(path)),),
        purpose="写用户文件",
        detail=dict(detail),
    )


def _execute_effect(program: str, cwd=None) -> Effect:
    targets = [Target(kind="program", value=program)]
    if cwd is not None:
        targets.append(Target(kind="path", value=str(cwd)))
    return Effect(kind=EffectKind.EXECUTE, targets=tuple(targets), purpose="运行项目命令")


# ---------------------------------------------------------------------------
# A. 程序与审批后执行
# ---------------------------------------------------------------------------


def test_read_only_denies_program_execution_without_pending(app_client):
    """只读模式：程序执行固定规则拒绝，不产生待审批、不启动进程。"""
    client = app_client
    _set_mode(client, "read_only")
    result = _exec(client, "run_program", {"program": sys.executable, "args": ["-c", "print('NOPE')"]})
    assert result.ok is False, result
    assert "只读" in (result.error or ""), result.error
    assert _pending(client) == [], "只读应是拒绝，不是待审批"


def test_restricted_program_approval_then_real_execution(app_client):
    """受限模式：程序执行 → 待审批（不阻塞、不执行）→ 批准 → 后台执行器真实执行。"""
    client = app_client
    marker = "R9F_PROGRAM_OK"
    result = _exec(client, "run_program", {"program": sys.executable, "args": ["-c", f"print('{marker}')"]})
    assert result.ok is True, result.error
    pending = _pending(client)
    assert len(pending) == 1, pending
    item = pending[0]
    assert item["state"] == "waiting"
    approval_id = item["approval_id"]

    resp = _decide(client, approval_id, "approve")
    assert resp.status_code == 200, resp.text
    row = _wait_state(client, approval_id, "completed")
    assert marker in json.dumps(row.get("result"), ensure_ascii=False), row


def test_approval_payload_binds_program_and_argv(app_client):
    """审批单必须绑定具体 program 与 argv（不是"允许执行命令"这种笼统许可）。"""
    client = app_client
    result = _exec(client, "run_program", {"program": sys.executable, "args": ["-c", "print('BOUND')"]})
    assert result.ok is True, result.error
    item = _pending(client)[0]
    blob = json.dumps(item, ensure_ascii=False)
    assert sys.executable in blob or "program" in blob, item
    assert "BOUND" in blob or "argv" in blob, item
    _decide(client, item["approval_id"], "cancel")


def test_script_change_does_not_reuse_old_approval(app_client, tmp_path):
    """A 修复点：审批后执行前必须重核对**脚本内容/程序身份**（契约 §3.3 permit.spec_digest）。

    装置用真实文件副作用：批准时脚本写 original.marker；批准之后、执行之前把脚本
    内容换成写 tampered.marker。现在没有执行前的 spec_digest 重核对 → 篡改后的脚本
    照跑（这正是 W2 要收紧的点）。修复后应拒绝执行/标 invalidated，tampered 不得出现。
    """
    client = app_client
    script = tmp_path / "job.py"
    original_marker = tmp_path / "original.marker"
    tampered_marker = tmp_path / "tampered.marker"
    script.write_text(f"open(r'{original_marker}', 'w').write('orig')", encoding="utf-8")

    result = _exec(client, "run_program", {"program": sys.executable, "args": [str(script)]})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]

    # 批准之后、执行之前：脚本内容被替换
    script.write_text(f"open(r'{tampered_marker}', 'w').write('tampered')", encoding="utf-8")
    _decide(client, approval_id, "approve")

    # 执行器重算 spec_digest 发现脚本内容已变 → 许可失效（终态 failed/invalidated，
    # 不是 completed）；两种终态名都接受，但绝不允许 completed。
    deadline = time.time() + 15.0
    row = None
    while time.time() < deadline:
        row = client.app.state.ctx.conn.execute(
            "SELECT state, result, error FROM pending_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is not None and row["state"] in {"failed", "invalidated", "completed"}:
            break
        time.sleep(0.05)
    assert row is not None and row["state"] in {"failed", "invalidated"}, (
        "脚本内容在批准后发生变化，旧许可必须失效", dict(row) if row else None
    )
    assert not tampered_marker.exists(), (
        "脚本内容在批准后发生变化，旧许可不得沿用（篡改后的脚本不得被执行）"
    )
    assert not original_marker.exists(), "旧许可失效后原脚本也不得执行"


# ---------------------------------------------------------------------------
# 三模式判定矩阵
# ---------------------------------------------------------------------------


def test_three_modes_verdict_matrix(app_client):
    """真实引擎：写/执行/读在三种模式下的判定（只读拒绝、受限审批、无限制放行）。"""
    client = app_client
    target = client.workspace / "matrix.txt"
    expected = {
        "read_only": (Verdict.DENY, Verdict.DENY, Verdict.ALLOW),
        "restricted": (Verdict.APPROVE, Verdict.APPROVE, Verdict.ALLOW),
        "unrestricted": (Verdict.ALLOW, Verdict.ALLOW, Verdict.ALLOW),
    }
    for mode, (write_v, exec_v, read_v) in expected.items():
        _set_mode(client, mode)
        write = _authorize(client, _write_effect(target))
        execute = _authorize(client, _execute_effect(sys.executable))
        read = _authorize(
            client, Effect(kind=EffectKind.READ, targets=(Target(kind="path", value=str(target)),))
        )
        assert write.verdict == write_v, (mode, write)
        assert execute.verdict == exec_v, (mode, execute)
        assert read.verdict == read_v, (mode, read)


def test_qio_generated_workspace_write_is_autonomous_in_restricted(app_client):
    """C 的引擎侧规则：QIO 生成的工作文件在受限模式下可自主修改/删除（不进审批）。"""
    client = app_client
    _set_mode(client, "restricted")
    generated = client.workspace / "qio-report.md"
    write = _authorize(client, _write_effect(generated, origin="qio_generated"))
    delete = _authorize(
        client,
        Effect(
            kind=EffectKind.DELETE,
            targets=(Target(kind="path", value=str(generated)),),
            purpose="清理 QIO 生成的报告",
            detail={"origin": "qio_generated"},
        ),
    )
    assert write.verdict == Verdict.ALLOW, write
    assert delete.verdict == Verdict.ALLOW, delete


def test_workspace_created_file_stays_autonomous_after_restart(app_client):
    """C：真实用户要求下创建的工作区报告 → 后续修改自主；重启（新 store 实例）后仍自主。

    首次创建走模型审查 ALLOW 并形成有界授权记录；创建后由受控服务登记
    qio_generated，之后修改命中固定规则（不再调用模型、不再审批）。
    """
    from agent.services import file_origins
    from agent.services.file_origins import FileOriginStore

    client = app_client
    _set_mode(client, "restricted")
    ctx, _requirement, reviewer = _install_reviewer(
        client, text="帮我整理一份报告并保存到工作区"
    )
    report = client.workspace / "整理报告.md"

    created = _exec(client, "fs_write", {"path": str(report), "content": "# 报告"})
    assert created.ok is True, created.error
    assert report.exists(), "审查 ALLOW 后创建应真实执行"
    assert len(reviewer.calls) == 1
    assert file_origins.origin_for_path(report) == file_origins.ORIGIN_QIO_GENERATED

    # 再改一次：QIO 生成文件命中固定规则，不再调用模型、不再审批
    again = _exec(client, "fs_write", {"path": str(report), "content": "# 报告 v2"})
    assert again.ok is True, again.error
    assert report.read_text(encoding="utf-8") == "# 报告 v2"
    assert len(reviewer.calls) == 1, "已生成文件的修改不应再调用审查"
    assert _pending(client) == [], "工作区自主创建的产物不该再要求审批"

    # 模拟重启：新 store 实例读同一 DB 仍能查到来源；引擎按来源自主放行
    fresh = FileOriginStore(ctx.conn, ctx.computer.root())
    record = fresh.lookup(report)
    assert record is not None and record.origin == file_origins.ORIGIN_QIO_GENERATED
    file_origins.set_default_store(fresh)
    resumed = _authorize(client, _write_effect(report))
    assert resumed.verdict == Verdict.ALLOW, "重启后同一生成文件仍应自主"
    assert len(reviewer.calls) == 1


# ---------------------------------------------------------------------------
# 内部运行数据：三次确认（所有模式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
def test_internal_data_requires_three_confirmations_in_every_mode(app_client, mode):
    """契约 §1/§2：内部运行数据直接改写，所有模式都要后端 CAS 的连续三次确认。"""
    client = app_client
    _set_mode(client, mode)
    internal = _internal_target(client)
    result = _exec(client, "fs_write", {"path": internal, "content": "corrupt"})
    assert result.ok is True, result.error
    pending = _pending(client)
    assert len(pending) == 1, pending
    item = pending[0]
    assert int(item.get("requires_confirmations") or 1) == 3, item
    assert int(item.get("confirmed_stage") or 0) == 0, item
    # 只确认 1 次：不执行
    resp = client.post(f"/api/approvals/{item['approval_id']}/confirm", json={"stage": 1})
    assert resp.status_code == 200, resp.text
    time.sleep(0.1)
    row = client.app.state.ctx.conn.execute(
        "SELECT state FROM pending_approvals WHERE approval_id = ?", (item["approval_id"],)
    ).fetchone()
    assert row["state"] != "completed", "只确认一次不得执行"
    _decide(client, item["approval_id"], "cancel")


def test_internal_data_three_confirmations_then_executes(app_client):
    """三次确认都到位后，内部数据写入按批准执行（受限模式；真实执行器）。"""
    client = app_client
    _set_mode(client, "restricted")
    internal = _internal_target(client)
    result = _exec(client, "fs_write", {"path": internal, "content": "corrupt"})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    for stage in (1, 2, 3):
        resp = client.post(f"/api/approvals/{approval_id}/confirm", json={"stage": stage})
        assert resp.status_code == 200, resp.text
    row = _wait_state(client, approval_id, "completed")
    assert row is not None


# ---------------------------------------------------------------------------
# D. 项目必要执行：现状与契约目标
# ---------------------------------------------------------------------------


def test_project_command_currently_pends_every_time(app_client):
    """现状记录（R9-F 修复前）：受限模式下项目命令每次都要审批。

    这不是目标行为，而是 D 问题的证据；目标行为见下面标 xfail 的用例。
    """
    client = app_client
    _set_mode(client, "restricted")
    project = client.workspace / "project"
    project.mkdir()
    ctx = client.app.state.ctx
    instruction = ctx.instructions.begin(
        turn_id="turn_proj_current", text=f"在 {project} 里运行测试并修复问题"
    )
    try:
        with instruction_context(instruction):
            result = _exec(
                client,
                "run_program",
                {"program": sys.executable, "args": ["-c", "print('PROJ')"], "cwd": str(project)},
            )
        assert result.ok is True, result.error
        assert len(_pending(client)) == 1, "现状：需要审批（D 待修复）"
    finally:
        ctx.instructions.end(instruction)
        for item in _pending(client):
            _decide(client, item["approval_id"], "cancel")


def test_project_command_with_user_requirement_is_not_reasked(app_client):
    """D：真实用户要求（"在 <项目> 里运行测试并修复"）下的真实测试命令不反复审批。

    模型审查产生有界 AuthorizationRecord（绑定 program/argv/cwd）；同一命令复用记录，
    不重复调用模型/审批；换了 argv 或程序必须重新判断（不能靠目录命中放行任意程序）。
    """
    client = app_client
    _set_mode(client, "restricted")
    project = client.workspace / "project"
    project.mkdir()
    _ctx, _requirement, reviewer = _install_reviewer(
        client, text=f"在 {project} 里运行测试并修复问题"
    )
    args = ["-c", "print('PROJ')"]
    first = _exec(
        client,
        "run_program",
        {"program": sys.executable, "args": args, "cwd": str(project)},
    )
    assert first.ok is True, first.error
    assert _pending(client) == [], (
        "真实用户要求范围内的测试命令不该每次都要审批（D 目标）", _pending(client)
    )
    assert len(reviewer.calls) == 1

    # 同一程序同一参数再跑：复用有界授权记录，不再调用模型、不再审批
    second = _exec(
        client,
        "run_program",
        {"program": sys.executable, "args": args, "cwd": str(project)},
    )
    assert second.ok is True, second.error
    assert _pending(client) == []
    assert len(reviewer.calls) == 1, "同一 fingerprint 不应重复审查"

    # 换成另一条真实测试命令：必须重新判断（目录命中不等于放行任意程序）
    third = _exec(
        client,
        "run_program",
        {"program": sys.executable, "args": ["-c", "print('OTHER')"], "cwd": str(project)},
    )
    assert third.ok is True, third.error
    assert len(reviewer.calls) == 2, "新程序/新参数必须重新审查"


# ---------------------------------------------------------------------------
# B. 模型审查（W1 冻结接口；未合入 → xfail，可运行）
# ---------------------------------------------------------------------------


def test_negated_instruction_does_not_authorize_write(app_client):
    """B：否定句不得被关键词当成许可（"不要修改 X" 不能授权写 X）。"""
    client = app_client
    target = client.workspace / "negation.txt"
    target.write_text("原始", encoding="utf-8")
    ctx = client.app.state.ctx
    instruction = ctx.instructions.begin(turn_id="turn_neg", text=f"不要修改 {target}，保持原样")
    try:
        with instruction_context(instruction):
            result = _exec(client, "fs_write", {"path": str(target), "content": "被改了"})
        assert target.read_text(encoding="utf-8") == "原始", "否定句不得授权写入"
        assert result.ok is False or _pending(client), "否定句下写入必须拒绝或转审批"
    finally:
        ctx.instructions.end(instruction)
        for item in _pending(client):
            _decide(client, item["approval_id"], "cancel")


def test_reviewer_tri_state_and_backend_validation(app_client):
    """B：审查三态（ALLOW/CONFIRM/DENY）+ 后端校验（对象/操作不得越界）。"""
    from agent.security.review import (  # noqa: PLC0415
        ReviewDecision,
        ReviewObject,
        ReviewOperation,
        ReviewOutcome,
        ReviewRequest,
        validate_outcome,
    )

    client = app_client
    ctx = client.app.state.ctx
    requirement = ctx.requirements.record_user_message(
        text="整理一下工作区里的报告", message_id="m1", turn_id="t1", topic_id=None
    )
    request = ReviewRequest(
        requirement=requirement,
        corrections=(),
        limits=(),
        mode="restricted",
        operation=ReviewOperation(
            kind="write",
            objects=(ReviewObject(kind="path", value=str(client.workspace / "a.md")),),
            summary="写报告",
        ),
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
    )
    for decision, expected in (
        (ReviewDecision.ALLOW, Verdict.ALLOW),
        (ReviewDecision.CONFIRM, Verdict.APPROVE),
        (ReviewDecision.DENY, Verdict.DENY),
    ):
        allow = decision == ReviewDecision.ALLOW
        outcome = ReviewOutcome(
            decision=decision,
            reason="依据用户要求",
            requirement_id=requirement.requirement_id,
            allowed_operations=("write",) if allow else (),
            objects=request.operation.objects if allow else (),
            # 契约：ALLOW 必须给出 limits/purpose（不是可选装饰）。
            limits=("仅限本次操作",) if allow else (),
            purpose="写报告",
        )
        validated = validate_outcome(outcome, request)
        assert validated.decision == decision
        assert expected is not None
    # 越界对象必须被校验拒绝
    rogue = ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="越界",
        requirement_id=requirement.requirement_id,
        allowed_operations=("write",),
        objects=(ReviewObject(kind="path", value=str(client.workspace / "other.md")),),
        limits=(),
        purpose="写别的文件",
    )
    with pytest.raises(Exception):
        validate_outcome(rogue, request)


def test_review_unavailable_is_not_silent_allow(app_client):
    """B：模型不可用/超时/输出非法 → 不默认允许（APPROVE，不自动切模式）。"""
    from agent.security.review import (  # noqa: PLC0415
        ModelAuthorizationReviewer,
        ReviewUnavailable,
    )

    client = app_client

    class _BoomAdapter:
        async def complete(self, messages, tools=None, **kwargs):
            raise RuntimeError("model down")

    from agent.security.review import ReviewObject, ReviewOperation, ReviewRequest

    reviewer = ModelAuthorizationReviewer(adapter_factory=lambda: _BoomAdapter())
    ctx = client.app.state.ctx
    requirement = ctx.requirements.record_user_message(
        text="把报告发到 example.com", message_id="m2", turn_id="t2", topic_id=None
    )
    request = ReviewRequest(
        requirement=requirement,
        corrections=(),
        limits=(),
        mode="restricted",
        operation=ReviewOperation(
            kind="egress",
            objects=(ReviewObject(kind="service", value="https://example.com"),),
            network_hosts=("example.com",),
            summary="把报告发到 example.com",
        ),
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
    )
    with pytest.raises(ReviewUnavailable):
        asyncio.run(reviewer.review(request))