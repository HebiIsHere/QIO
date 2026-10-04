"""独立验证：放弃未完成开发任务 —— 并发、持久化与终态不可逆。

这份文件由**验证方**维护，与实现方自己的用例互不覆盖（实现方：tests/test_dev_tasks_api.py、
tests/test_dev_tools.py 等）。断言只依据冻结契约 `_ABANDON-CONTRACT.md`（§2/§3/§4/§5），
不看实现内部结构，所以它能在「还没实现」时真的变红。

覆盖的验收点：
1. 前置判定五态（not_found / already_abandoned / submitted / running / 等审批→ok）；
2. 放弃成功后：终态字段、phase、授权收回、文件保留、列表仍列出并带 abandoned；
3. **终态不可逆**：8 个写操作对已放弃任务一律无效，且不能把 abandoned 擦回 False；
4. 重启（重新构造 DevWorkspace）后状态不复活；
5. 向后兼容：改动之前写下的 state.json（没有 abandoned 键）必须仍能读回证据与授权；
6. 并发：
   - 放弃 vs 未决审批（审批被作废、等待方收到 cancelled、迟到的 respond 必然 False）；
   - 审批先通过、放弃后到（任务不会因为「审批已通过」而偷偷继续）；
   - 执行中放弃被拒（不做任何状态改动），执行结束后迟到的测试结果不能写进已放弃任务；
7. 不误伤：放弃 A 时 B 的执行登记、授权、state.json 一个都不动，且不调用 turn 取消；
8. 接口层：404、被拒时零改动、`task` 行与列表行同形状、调用顺序（先作废审批再标放弃）；
9. 守卫：已放弃任务不会再跑测试、不会再注册工具（lifecycle 都不被调用）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.tools.approval import ApprovalService
from agent.tools.dev_auth import ensure_test_authorization
from agent.tools.dev_tools import (
    DevListFilesTool,
    DevListTasksTool,
    DevReadFileTool,
    DevRunTestsTool,
    DevSubmitTool,
    DevWriteFileTool,
)
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.sandbox import SandboxExecutor, SandboxResult
from agent.tools.spec import ToolDefinition

# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------

_STATE_FILE = "state.json"


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    """开发机上的 QIO_DATA_DIR 会让 Settings 落到真实数据目录：用例只认自己的临时目录。"""
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _ws(tmp_path: Path) -> DevWorkspace:
    return DevWorkspace(tmp_path / "dev-workspaces")


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="add_numbers",
        description="求和",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
    )


def _read_state(task) -> dict:
    return json.loads((task.dir / _STATE_FILE).read_text(encoding="utf-8"))


def _state_bytes(task) -> bytes:
    return (task.dir / _STATE_FILE).read_bytes()


class _RecordingSandbox:
    """沙箱替身：只记录「生成代码到底有没有被执行」。"""

    executor = "subprocess"
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.executions = 0

    async def effective_executor(self) -> str:
        return "subprocess"

    async def execute(
        self,
        code,
        arguments,
        extra_env=None,
        policy=None,
        files=None,
        entry=None,
        interpreter=None,
    ) -> SandboxResult:
        self.executions += 1
        return SandboxResult(ok=True, value={"ok": True}, stdout="", stderr="")


class _FakeApprovals:
    """只记录请求并给一个预设结论的审批服务。"""

    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision, None, None)


# ---------------------------------------------------------------------------
# 1. 前置判定：五种状态
# ---------------------------------------------------------------------------


def test_readiness_says_not_found_for_an_unknown_task(tmp_path):
    ws = _ws(tmp_path)
    ready = ws.abandon_readiness("ws_ffffffffffff")
    assert ready["status"] == "not_found"
    assert ready["allowed"] is False
    assert "ws_ffffffffffff" in ready["message"]
    assert ready["run"] is None


def test_readiness_allows_a_task_that_is_still_being_developed(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("做一个求和工具")
    ready = ws.abandon_readiness(task.id)
    assert ready["status"] == "ok"
    assert ready["allowed"] is True
    assert ready["message"]


def test_readiness_of_an_abandoned_task_is_idempotent(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")
    assert ws.abandon(task.id)["ok"] is True

    ready = ws.abandon_readiness(task.id)
    assert ready["status"] == "already_abandoned"
    assert ready["allowed"] is True, "重复点击/重试必须算成功，不能变成失败"


def test_a_submitted_task_can_never_be_abandoned(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("已经做完的工具")
    ws.mark_submitted(task.id)

    ready = ws.abandon_readiness(task.id)
    assert ready["status"] == "submitted"
    assert ready["allowed"] is False
    # 语义必须一致：说清「放弃只处理没做完的」，并指向撤销工具那条路
    assert ("做完" in ready["message"]) or ("已提交" in ready["message"])
    assert "撤销" in ready["message"]

    result = ws.abandon(task.id)
    assert result["ok"] is False
    assert result["status"] == "submitted"
    assert ws.status(task.id)["abandoned"] is False, "被拒绝时不能偷偷标成已放弃"


def test_a_running_task_is_refused_and_waiting_for_approval_is_allowed(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("正在跑测试的工具")
    ws.begin_run(task.id, "tests")
    ws.set_run_stage(task.id, "executing")

    ready = ws.abandon_readiness(task.id)
    assert ready["status"] == "running"
    assert ready["allowed"] is False
    assert ready["can_stop"] is False
    assert ready["run"] is not None and ready["run"]["kind"] == "tests"
    # 必须如实说「没有只停这一个任务的能力」，而不是「已停止/已放弃」
    assert "正在执行" in ready["message"]
    assert "先停止" in ready["message"]
    assert "没有" in ready["message"]
    assert "已放弃" not in ready["message"]

    refused = ws.abandon(task.id)
    assert refused["ok"] is False and refused["status"] == "running"
    assert ws.status(task.id)["abandoned"] is False

    # 等审批 ≠ 在执行：这时放弃是允许的（由接口层把未决审批一并作废）
    ws.set_run_stage(task.id, "waiting_approval")
    waiting = ws.abandon_readiness(task.id)
    assert waiting["status"] == "ok"
    assert waiting["allowed"] is True
    ws.end_run(task.id, "tests")


def test_active_run_registration_is_honest_about_can_stop(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")
    assert ws.active_run(task.id) is None

    ws.begin_run(task.id, "submit")
    ws.set_run_stage(task.id, "waiting_approval")
    run = ws.active_run(task.id)
    assert run is not None
    assert run["kind"] == "submit"
    assert run["stage"] == "waiting_approval"
    assert run["started_at"]
    assert run["can_stop"] is False, "当前架构没有单个工具调用的取消句柄，不能不诚实地说能停"

    ws.end_run(task.id, "submit")
    assert ws.active_run(task.id) is None
    # 进程内事实：重启后本来就没有东西在跑
    assert DevWorkspace(ws.root_dir).active_run(task.id) is None


# ---------------------------------------------------------------------------
# 2. 放弃成功：终态、保留物、列表事实
# ---------------------------------------------------------------------------


def test_abandon_marks_a_terminal_state_and_keeps_every_file(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("做一个抓网页的工具")
    ws.write_file(task.id, "tool.py", "print('hi')\n")
    before = ws.status(task.id)

    result = ws.abandon(task.id)
    assert result["ok"] is True
    assert result["status"] == "abandoned"

    status = ws.status(task.id)
    assert status["abandoned"] is True
    assert status["abandoned_at"]
    assert status["phase"] == "abandoned"
    # 不删目录、不删文件、不清证据、不改内容摘要
    assert (task.dir / "tool.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert task.dir.exists()
    assert status["content_digest"] == before["content_digest"]
    # 其余既有键一个都没丢
    for key in ("id", "request", "created_at", "submitted", "test_runs", "test_evidence_current"):
        assert key in status, f"status() 里既有键 {key} 不能消失"
    # 事实快照（给主循环记账用）也必须带上终态
    fact = ws.fact_for(task.id, "dev_run_tests")["dev_task"]
    assert fact["abandoned"] is True
    assert fact["abandoned_at"]
    # 落盘：state.json 里两个新键都在
    state = _read_state(task)
    assert state["abandoned"] is True and state["abandoned_at"]


def test_abandon_revokes_task_and_long_term_authorizations(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("会长期授权的工具")
    other = ws.create("另一个任务")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", lifetime="long_term"
    )
    assert ws.test_authorized(
        other.id, policy_fingerprint="p1", executor="subprocess"
    ) is True, "长期授权本来覆盖所有任务"

    result = ws.abandon(task.id)

    assert result["revoked"] is True
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert ws.test_authorized(
        other.id, policy_fingerprint="p1", executor="subprocess"
    ) is False, "该任务建立的长期授权也要一并收回"
    assert ws.authorizations() == []


def test_abandon_reports_when_there_was_nothing_to_revoke(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("从没授权过的工具")
    result = ws.abandon(task.id)
    assert result["ok"] is True
    assert result["revoked"] is False, "没有授权就说没有，不能虚报「已收回」"


# ---------------------------------------------------------------------------
# 3. 终态不可逆：所有写操作一律无效
# ---------------------------------------------------------------------------


def test_no_write_operation_can_revive_an_abandoned_task(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("被放弃后不许复活")
    ws.write_file(task.id, "tool.py", "print(1)\n")
    ws.abandon(task.id)
    frozen_state = _state_bytes(task)
    frozen_files = ws.list_files(task.id)

    # 契约 §2.2 点名的 8 个方法，逐个试。写操作允许「静默无效」或「明确拒绝」
    # （抛异常也是明确拒绝），但两种都不许改状态、不许落盘、不许复活任务。
    ws.set_phase(task.id, "testing_passed")
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.mark_submitted(task.id)
    try:
        ws.write_file(task.id, "later.py", "# 迟到的写入\n")
    except ValueError:
        pass  # 明确拒绝也算「无效操作」
    archived = ws.archive(task.id)
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    consumed = ws.consume_test_authorization(task.id)

    status = ws.status(task.id)
    assert status["abandoned"] is True, "abandoned 不许被任何写操作擦回 False"
    assert status["phase"] == "abandoned"
    assert status["submitted"] is False, "已放弃任务不能被标成已提交"
    assert status["last_test_passed"] is None, "迟到的测试结果不能写进已放弃任务"
    assert status["test_runs"] == 0
    assert archived is None, "archive 是提交后的快照，对已放弃任务必须直接返回 None"
    assert consumed is False
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert ws.list_files(task.id) == frozen_files, "迟到的写入不能在已放弃任务里落盘"
    assert (task.dir / "later.py").exists() is False
    assert _state_bytes(task) == frozen_state, "已放弃任务的 state.json 不许被改写"


def test_a_late_test_result_cannot_write_evidence_into_an_abandoned_task(tmp_path):
    """执行中放弃被拒 → 执行结束 → 放弃成功 → 这次执行的结果是「迟到的」，不能复活任务。"""
    ws = _ws(tmp_path)
    task = ws.create("测试跑完就放弃")
    ws.begin_run(task.id, "tests")
    ws.set_run_stage(task.id, "executing")

    # 执行中：放弃被拒，且什么都不改
    before = _state_bytes(task)
    assert ws.abandon(task.id)["ok"] is False
    assert _state_bytes(task) == before

    # 测试跑完（真实路径：DevRunTestsTool 记录结果、end_run）
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.end_run(task.id, "tests")

    # 现在放弃：允许
    assert ws.abandon(task.id)["ok"] is True

    # 迟到的第二份结果（例如复测回调晚到）不许写进去
    ws.record_test(task.id, False, "0/1 tests passed")
    status = ws.status(task.id)
    assert status["abandoned"] is True
    assert status["phase"] == "abandoned"
    assert status["last_test_summary"] == "1/1 tests passed", "放弃之后的结果一律不落账"
    assert status["submitted"] is False


# ---------------------------------------------------------------------------
# 4. 持久化与向后兼容
# ---------------------------------------------------------------------------


def test_abandon_survives_a_restart(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("重启后不许复活")
    ws.abandon(task.id)
    abandoned_at = ws.status(task.id)["abandoned_at"]

    reborn = DevWorkspace(ws.root_dir)

    status = reborn.status(task.id)
    assert status["abandoned"] is True
    assert status["abandoned_at"] == abandoned_at
    assert status["phase"] == "abandoned"
    assert reborn.abandon_readiness(task.id)["status"] == "already_abandoned"
    # 重启后仍然不可逆
    reborn.record_test(task.id, True, "复活我")
    assert reborn.status(task.id)["last_test_passed"] is None


def test_state_json_written_before_this_change_still_reads_back(tmp_path):
    """契约 §2.1 的硬要求：改动之前写下的 state.json 必须仍能读回，
    测试证据 / 授权记录 / 提交摘要一个都不能丢（可以升 schema，但必须接受旧格式）。"""
    ws = _ws(tmp_path)
    task = ws.create("旧格式状态")
    ws.write_file(task.id, "tool.py", "print(1)\n")
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")

    current = _read_state(task)
    legacy_keys = {
        "schema",
        "source",
        "id",
        "request",
        "created_at",
        "phase",
        "submitted",
        "test_runs",
        "last_test_passed",
        "last_test_summary",
        "last_test_at",
        "last_test_digest",
        "evidence_state",
        "test_authorization",
        "submitted_digest",
        "submitted_at",
    }
    legacy = {key: value for key, value in current.items() if key in legacy_keys}
    assert "abandoned" not in legacy and "abandoned_at" not in legacy
    (task.dir / _STATE_FILE).write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    reborn = DevWorkspace(ws.root_dir)
    status = reborn.status(task.id)
    assert status["last_test_passed"] is True, "旧格式里的测试结论不能丢"
    assert status["test_evidence_current"] is True, "证据摘要仍对得上当前内容就不该被当成未知"
    assert status["last_test_summary"] == "1/1 tests passed"
    assert reborn.test_authorized(
        task.id, policy_fingerprint="p1", executor="subprocess"
    ) is True, "旧格式里的授权记录不能丢"
    assert status["submitted"] is False
    assert status["abandoned"] is False, "旧格式任务默认「没被放弃」，不是「未知」"


def test_an_abandoned_task_still_appears_in_the_authoritative_list(tmp_path):
    """列表是事实清单：已放弃的任务仍然列出（带 abandoned），由界面做「未完成」过滤。"""
    ws = _ws(tmp_path)
    kept = ws.create("还在做的")
    gone = ws.create("要放弃的")
    ws.abandon(gone.id)

    ids = [task.id for task in ws.list_tasks()]
    assert kept.id in ids and gone.id in ids
    assert ws.status(gone.id)["abandoned"] is True


# ---------------------------------------------------------------------------
# 5. 并发：放弃 vs 审批
# ---------------------------------------------------------------------------


async def test_abandon_invalidates_a_pending_approval_of_that_task(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("等审批时放弃")
    other = ws.create("别的任务")
    approvals = ApprovalService(EventBus(), timeout_seconds=5.0)

    waiting = asyncio.create_task(
        approvals.request("tool_execution", {"workspace": task.id, "tool": "dev_run_tests"})
    )
    untouched = asyncio.create_task(
        approvals.request("tool_execution", {"workspace": other.id, "tool": "dev_run_tests"})
    )
    await asyncio.sleep(0.05)
    mine = [row for row in approvals.pending() if row["payload"]["workspace"] == task.id]
    assert len(mine) == 1
    approval_id = mine[0]["approval_id"]

    # 接口层的顺序：先作废未决审批，再标放弃
    invalidated = approvals.invalidate_for_task(task.id)
    assert invalidated == 1
    assert ws.abandon(task.id)["ok"] is True

    # 等待方拿到的是明确的 cancelled，不是异常、也不是静默丢弃
    result = await waiting
    assert result.decision == "cancelled"

    # 作废过的审批再应答必然 False（单次使用语义不变）
    assert await approvals.respond(approval_id, "approved") is False
    # 别的任务的审批一条都没动
    assert len(approvals.pending()) == 1
    assert ws.abandon_readiness(other.id)["status"] == "ok"
    untouched.cancel()
    with pytest.raises(asyncio.CancelledError):
        await untouched


async def test_invalidate_for_task_publishes_a_cancelled_result_event(tmp_path):
    """界面上的确认卡靠这条事件自己消失：必须发 APPROVAL_RESULT(cancelled)。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5.0)
    chunks: list[str] = []

    async def consume():
        async for chunk in bus.stream():
            chunks.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(approvals.request("tool_execution", {"workspace": task.id}))
    await asyncio.sleep(0.05)

    assert approvals.invalidate_for_task(task.id) == 1
    await asyncio.sleep(0.05)
    events = [
        json.loads(line[6:])
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    results = [e for e in events if e["type"] == "APPROVAL_RESULT"]
    assert results, "作废审批必须发一条 APPROVAL_RESULT 事件"
    payload = results[-1]["data"]
    assert payload["decision"] == "cancelled"
    assert payload.get("reason") == "task_abandoned"
    await waiter
    consumer.cancel()


async def test_an_approval_that_arrived_first_cannot_keep_the_task_alive(tmp_path):
    """顺序反过来：审批先被用户通过 → 放弃后到。

    「审批通过」只是这一次执行的放行，不是任务的续命符：放弃之后，
    授权记录与测试结果都不许再写进这个任务。"""
    ws = _ws(tmp_path)
    task = ws.create("审批先通过")

    # 审批通过后，工具本来会做这两件事：记授权、跑测试、记结果
    assert ws.abandon(task.id)["ok"] is True
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    ws.record_test(task.id, True, "1/1 tests passed")

    status = ws.status(task.id)
    assert status["abandoned"] is True
    assert status["last_test_passed"] is None
    assert status["test_authorized"] is False
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert ws.authorizations() == []


# ---------------------------------------------------------------------------
# 6. 守卫：已放弃任务不再执行、不再注册
# ---------------------------------------------------------------------------


async def test_dev_run_tests_refuses_an_abandoned_task_without_executing(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_definition(task.id, _definition())
    sandbox = _RecordingSandbox()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=_FakeApprovals())
    ws.abandon(task.id)

    result = await tool.run(workspace=task.id)

    assert result.ok is False
    assert result.category == "abandoned"
    assert "放弃" in result.error
    assert sandbox.executions == 0, "已放弃的任务绝不能真的执行生成代码"


async def test_dev_submit_refuses_an_abandoned_task_without_registering(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_definition(task.id, _definition())
    built = {"n": 0}

    async def lifecycle_builder():
        built["n"] += 1
        raise AssertionError("已放弃的任务不该走到注册流程")

    tool = DevSubmitTool(ws, lifecycle_builder=lifecycle_builder, approvals=_FakeApprovals())
    ws.abandon(task.id)

    result = await tool.run(workspace=task.id, explanation="求和工具")

    assert result.ok is False
    assert result.category == "abandoned"
    assert built["n"] == 0, "守卫必须在 lifecycle 之前拦住，不能发出注册事件、不能落盘"


async def test_file_tools_refuse_an_abandoned_task(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_file(task.id, "tool.py", "print(1)\n")
    ws.abandon(task.id)

    write = await DevWriteFileTool(ws).run(workspace=task.id, name="late.py", content="x")
    read = await DevReadFileTool(ws).run(workspace=task.id, name="tool.py")
    listing = await DevListFilesTool(ws).run(workspace=task.id)

    assert write.ok is False and "放弃" in write.error
    assert read.ok is False, "读也拒绝，避免界面上出现「还能继续做」的假象"
    assert listing.ok is False
    assert (task.dir / "late.py").exists() is False


async def test_dev_list_tasks_marks_abandoned_tasks_as_abandoned(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("被放弃的任务")
    ws.abandon(task.id)

    result = await DevListTasksTool(ws).run()

    assert result.ok is True
    assert task.id in result.content
    assert "已放弃" in result.content, "模型不能以为它还能继续做"


# ---------------------------------------------------------------------------
# 7. 接口层（HTTP）
# ---------------------------------------------------------------------------


def test_abandon_endpoint_returns_the_same_row_shape_as_the_list(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("做一个求和工具")
    ctx.dev_workspaces.write_file(task.id, "tool.py", "print(1)\n")

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is True
    assert body["status"] == "abandoned"
    assert body["message"]
    assert body["can_stop"] is False
    assert isinstance(body["invalidated_approvals"], int)
    row = body["task"]
    assert row["id"] == task.id
    assert row["abandoned"] is True and row["abandoned_at"]

    listed = client.get("/api/dev/tasks").json()["tasks"]
    assert [r["id"] for r in listed] == [task.id]
    assert listed[0]["abandoned"] is True
    assert set(listed[0]) == set(row), "接口返回的 task 行与列表行必须同形状"
    assert set(listed[0]) >= {
        "id",
        "request",
        "phase",
        "submitted",
        "test_passed",
        "test_evidence_current",
        "updated_at",
        "authorized",
    }


def test_abandon_endpoint_is_idempotent(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")

    first = client.post(f"/api/dev/tasks/{task.id}/abandon").json()
    second = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert first["ok"] is True and first["status"] == "abandoned"
    assert second["ok"] is True and second["status"] == "already_abandoned"


def test_abandon_endpoint_404s_for_an_unknown_task(client):
    resp = client.post("/api/dev/tasks/ws_ffffffffffff/abandon")
    assert resp.status_code == 404


def test_abandon_endpoint_refuses_a_running_task_without_changing_anything(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("正在跑测试")
    ctx.dev_workspaces.write_file(task.id, "tool.py", "print(1)\n")
    ctx.dev_workspaces.begin_run(task.id, "tests")
    ctx.dev_workspaces.set_run_stage(task.id, "executing")
    before = _state_bytes(task)

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is False
    assert body["status"] == "running"
    assert body["revoked"] is False
    assert body["invalidated_approvals"] == 0
    assert body["can_stop"] is False
    assert body["task"]["abandoned"] is False
    assert _state_bytes(task) == before, "被拒绝时不许有任何状态改动"
    ctx.dev_workspaces.end_run(task.id, "tests")


def test_abandon_endpoint_persists_the_terminal_state_before_invalidating(client, monkeypatch):
    """顺序硬要求（契约 v2 第 C 章）：**先可靠落盘放弃终态，成功之后才作废审批**。

    v1 的顺序（先作废、再标放弃）已被 v2 反转：作废不可逆 —— 先作废、随后保存失败，
    会留下「确认已作废、任务却还在」的部分完成状态；反过来最坏只是「任务仍在、
    卡片还在、重试收敛」。这里用调用顺序断言，而不是只看最终状态。
    """
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    calls: list[str] = []

    real_invalidate = ctx.approvals.invalidate_for_task
    real_abandon = ctx.dev_workspaces.abandon

    def spy_invalidate(task_id, **kwargs):
        calls.append("invalidate")
        return real_invalidate(task_id, **kwargs)

    def spy_abandon(task_id):
        calls.append("abandon")
        return real_abandon(task_id)

    monkeypatch.setattr(ctx.approvals, "invalidate_for_task", spy_invalidate)
    monkeypatch.setattr(ctx.dev_workspaces, "abandon", spy_abandon)

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is True
    assert body["persisted"] is True
    assert calls == ["abandon", "invalidate"], f"顺序不对（契约 v2 要求先落盘再作废）：{calls}"


def test_abandon_endpoint_persist_failure_invalidates_nothing_and_retry_converges(client, monkeypatch):
    """契约 v2 C4/D1：保存失败这一支**一个审批都不许作废**（作废不可逆），且重试能收敛。

    v1 没有这条语义（它会先作废、再在保存失败时留下部分完成状态）。
    """
    import json as _json
    from pathlib import Path as _Path

    import agent.tools.dev_workspace as _dev_workspace

    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("验证：保存失败不许作废审批")
    calls: list[str] = []
    real_invalidate = ctx.approvals.invalidate_for_task

    def spy_invalidate(task_id, **kwargs):
        calls.append("invalidate")
        return real_invalidate(task_id, **kwargs)

    monkeypatch.setattr(ctx.approvals, "invalidate_for_task", spy_invalidate)

    # 只让「任务状态」这一步写失败：长期授权那一步（如果有）仍然能正常落盘
    real_atomic = getattr(_dev_workspace, "_atomic_write_json", None)
    if real_atomic is None:
        pytest.fail("严格持久化还没落地：缺少 _atomic_write_json（契约 v2 A2）")

    def failing_atomic(path, payload):
        if _Path(path).name.startswith("state.json"):
            raise OSError(28, "No space left on device (injected)")
        return real_atomic(path, payload)

    monkeypatch.setattr(_dev_workspace, "_atomic_write_json", failing_atomic)

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is False, f"保存失败却报成功：{body}"
    assert body["status"] == "persist_failed", f"status={body['status']}"
    assert body["persisted"] is False, f"persisted={body['persisted']}"
    assert body["invalidated_approvals"] == 0, "失败这一支作废了审批（作废不可逆）"
    assert calls == [], f"保存失败时不该调用 invalidate_for_task，实际调用：{calls}"
    assert body["task"]["abandoned"] is False, "返回的任务行被标成已放弃"
    # 内存与磁盘一致：都还是「没放弃」
    assert ctx.dev_workspaces.status(task.id)["abandoned"] is False
    disk = _json.loads((task.dir / "state.json").read_text(encoding="utf-8"))
    assert disk.get("abandoned") is False, "磁盘上留下了已放弃标记"
    listed = client.get("/api/dev/tasks").json()["tasks"]
    row = [item for item in listed if item["id"] == task.id]
    assert row and row[0]["abandoned"] is False, "列表里任务被错误地标成已放弃"

    # 解除注入后重试：这时才成功、也这时才作废审批
    monkeypatch.undo()
    monkeypatch.setattr(ctx.approvals, "invalidate_for_task", spy_invalidate)
    retry = client.post(f"/api/dev/tasks/{task.id}/abandon").json()
    assert retry["ok"] is True and retry["persisted"] is True, f"重试没有成功：{retry}"
    assert calls == ["invalidate"], f"成功路径应当作废一次审批，实际：{calls}"


def test_abandon_endpoint_never_cancels_any_turn(client, monkeypatch):
    """不得误停其它任务：放弃开发与 turn 取消是两条路。"""
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    other = ctx.dev_workspaces.create("别的任务")
    ctx.dev_workspaces.begin_run(other.id, "tests")
    ctx.dev_workspaces.set_run_stage(other.id, "executing")
    touched: list[str] = []

    def spy_cancel_active():
        touched.append("cancel_active")
        return True

    def spy_cancel(turn_id):
        touched.append("cancel")
        return True

    monkeypatch.setattr(ctx.turns, "cancel_active", spy_cancel_active, raising=False)
    monkeypatch.setattr(ctx.turns, "cancel", spy_cancel, raising=False)

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is True
    assert touched == [], "放弃一个任务不得取消任何 turn"
    # 别的任务还在跑：执行登记、状态一个都没动
    assert ctx.dev_workspaces.active_run(other.id)["stage"] == "executing"
    assert ctx.dev_workspaces.status(other.id)["abandoned"] is False
    ctx.dev_workspaces.end_run(other.id, "tests")


def test_abandoning_one_task_leaves_every_other_task_untouched(client):
    ctx = client.app.state.ctx
    doomed = ctx.dev_workspaces.create("要放弃的")
    survivor = ctx.dev_workspaces.create("不该被牵连的")
    ctx.dev_workspaces.write_file(survivor.id, "tool.py", "print(1)\n")
    ctx.dev_workspaces.record_test(survivor.id, True, "1/1 tests passed")
    ctx.dev_workspaces.grant_test_authorization(
        survivor.id, policy_fingerprint="p1", executor="subprocess"
    )
    ctx.dev_workspaces.grant_test_authorization(
        doomed.id, policy_fingerprint="p1", executor="subprocess"
    )
    before_state = _state_bytes(survivor)
    before_status = ctx.dev_workspaces.status(survivor.id)

    body = client.post(f"/api/dev/tasks/{doomed.id}/abandon").json()

    assert body["ok"] is True
    assert body["revoked"] is True
    after = ctx.dev_workspaces.status(survivor.id)
    assert after["abandoned"] is False
    assert after["phase"] == before_status["phase"]
    assert after["last_test_passed"] is True
    assert after["test_evidence_current"] is True
    assert after["test_authorized"] is True, "别的任务的授权不许被顺手收掉"
    assert _state_bytes(survivor) == before_state

    rows = {row["id"]: row for row in client.get("/api/dev/tasks").json()["tasks"]}
    assert rows[doomed.id]["abandoned"] is True
    assert rows[survivor.id]["abandoned"] is False
    assert rows[survivor.id]["authorized"] is True


def test_a_late_approval_response_cannot_be_accepted_after_the_abandon(client):
    """旧审批 / 迟到的结果：接口必须回 404（没有可应答的审批），且不改任务状态。"""
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    client.post(f"/api/dev/tasks/{task.id}/abandon")

    resp = client.post(
        "/api/approvals/appr_deadbeefcafe/respond",
        json={"decision": "approved"},
    )

    assert resp.status_code == 404
    assert ctx.dev_workspaces.status(task.id)["abandoned"] is True


# ---------------------------------------------------------------------------
# 8. 契约里点名的三种审批载荷形状与注册守卫
# ---------------------------------------------------------------------------


async def test_invalidate_for_task_matches_all_three_documented_payload_shapes(tmp_path):
    """契约 §4：`payload["workspace"]` / `payload["code_boundary"]["task_id"]` /
    `payload["task_id"]` 三种指向都要认，别人的审批一条都不许碰。"""
    task_id = "ws_0123456789ab"
    other_id = "ws_ffffffffffff"
    approvals = ApprovalService(EventBus(), timeout_seconds=5.0)

    waiters = [
        asyncio.create_task(approvals.request("tool_execution", {"workspace": task_id})),
        asyncio.create_task(
            approvals.request("tool_create", {"code_boundary": {"task_id": task_id}})
        ),
        asyncio.create_task(approvals.request("computer", {"task_id": task_id})),
    ]
    unrelated = asyncio.create_task(
        approvals.request("tool_execution", {"workspace": other_id})
    )
    await asyncio.sleep(0.05)

    invalidated = approvals.invalidate_for_task(task_id)

    assert invalidated == 3
    results = await asyncio.gather(*waiters)
    assert [item.decision for item in results] == ["cancelled", "cancelled", "cancelled"]
    assert len(approvals.pending()) == 1, "别的任务的审批不该被作废"
    assert approvals.pending()[0]["payload"]["workspace"] == other_id
    unrelated.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unrelated


async def test_ensure_test_authorization_blocks_an_abandoned_task_without_asking(tmp_path):
    """契约 §5：已放弃任务 → 不发起审批、不执行，直接 blocked（category=permission）。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_definition(task.id, _definition())
    approvals = _FakeApprovals()
    ws.abandon(task.id)

    result = await ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=_definition(),
    )

    assert result is not None
    assert result.ok is False
    assert result.category == "permission"
    assert approvals.requests == [], "已放弃的任务不该再弹审批"


def test_the_cancelled_refusal_is_not_worded_as_a_user_rejection():
    """契约 §5：cancelled 分支要说清「没有执行 + 任务已被放弃/本轮已停止」，
    不能写成用户拒绝。"""
    from agent.tools import dev_auth

    text = dev_auth._refusal_text("cancelled")
    assert "放弃" in text
    assert "停止" in text
    assert "不是你的拒绝" in text, f"必须明确说不是用户拒绝：{text}"
    assert text != dev_auth._refusal_text("rejected")
    assert "超时" not in text


async def test_the_create_approval_payload_carries_the_workspace_id(tmp_path):
    """契约 §4：创建审批（tool_create）的载荷必须追加 `"workspace": group_id`，
    否则放弃任务时作废不了这条审批。"""
    from agent.tools.lifecycle import ToolLifecycle
    from agent.tools.registry import ToolRegistry

    class _Adapter:
        mode = "native"

    class _Recorder:
        def __init__(self) -> None:
            self.payloads: list[tuple[str, dict]] = []

        async def request(self, kind, payload, **kwargs):
            from agent.tools.approval import ApprovalResult

            self.payloads.append((kind, payload))
            return ApprovalResult("appr_test", "approved", None, None)

    approvals = _Recorder()
    lifecycle = ToolLifecycle(
        adapter=_Adapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=ToolRegistry(),
    )
    task_id = "ws_0123456789ab"
    try:
        await lifecycle.submit_definition(
            _definition(), "求和工具", skip_tests=True, group_id=task_id
        )
    except Exception:  # noqa: BLE001 - 注册环节可能缺依赖，这里只关心审批载荷
        pass

    creates = [payload for kind, payload in approvals.payloads if kind == "tool_create"]
    assert creates, "没有发起创建审批"
    assert creates[0].get("workspace") == task_id

    # 空 group_id 不写这个键（保持既有载荷形状）
    approvals.payloads.clear()
    try:
        await lifecycle.submit_definition(_definition(), "求和工具", skip_tests=True)
    except Exception:  # noqa: BLE001 - 同上
        pass
    creates = [payload for kind, payload in approvals.payloads if kind == "tool_create"]
    assert creates and "workspace" not in creates[0]
