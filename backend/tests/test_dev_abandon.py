"""放弃未完成的工具开发任务：终态、接口、执行与审批边界。

契约见仓库根的 `_ABANDON-CONTRACT.md`（Lead 冻结）。这里逐条锁住四件事：

1. 放弃是**不可逆终态**：所有写操作对它无效，且不能把 abandoned 擦回 False；
2. 「正在执行」与「正在等审批」分开：前者拒绝放弃且零状态改动，后者允许放弃
   并先作废未决审批；
3. 注册前的守卫真的挡得住：不落盘、不进注册表、不发「已注册」事件；
4. state.json 向后兼容：旧格式（schema 2）必须仍能读回测试证据 / 授权 / 提交摘要。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.tools.approval import ApprovalResult, ApprovalService
from agent.tools.dev_auth import ensure_test_authorization
from agent.tools.dev_tools import (
    ABANDONED_ERROR,
    DevListFilesTool,
    DevListTasksTool,
    DevReadFileTool,
    DevRunTestsTool,
    DevSubmitTool,
    DevWriteFileTool,
)
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.registry import ToolRegistry
from agent.tools.sandbox import SandboxExecutor, SandboxResult
from agent.tools.spec import ToolDefinition


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _RecordingSandbox:
    """记录「生成代码到底有没有被执行」的沙箱替身。"""

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
    """只记录请求并返回预设结论的审批服务替身。"""

    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs) -> ApprovalResult:
        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision)


class _FakeToolStore:
    """只记录落盘的持久层替身：用来证明「守卫拒绝时一个字都没写」。"""

    def __init__(self) -> None:
        self.saved: list[str] = []
        self.removed: list[str] = []

    def load(self, name: str):
        return None

    def save(self, definition) -> None:
        self.saved.append(definition.name)

    def remove(self, name: str) -> None:
        self.removed.append(name)


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="add_numbers",
        description="求和",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
    )


def _workspace_with_tool(tmp_path, *, request: str = "工具"):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create(request)
    definition = _definition()
    ws.write_definition(task.id, definition)
    return ws, task, definition


def _snapshot(ws: DevWorkspace, task_id: str) -> dict:
    """任务的全部可观察状态：被拒绝的操作必须让它**一模一样**。"""
    state_file = ws.task(task_id).dir / "state.json"
    return {
        "state_file": state_file.read_text(encoding="utf-8") if state_file.exists() else None,
        "status": ws.status(task_id),
        "authorizations": ws.authorizations(),
    }


# ---------------------------------------------------------------------------
# 1. 状态层：终态、幂等、零改动
# ---------------------------------------------------------------------------


def test_abandon_enters_the_terminal_state(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("做一个求和工具")
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1")

    result = ws.abandon(task.id)

    assert result["ok"] is True
    assert result["status"] == "abandoned"
    assert result["revoked"] is False  # 没有授权可收回
    state = ws.status(task.id)
    assert state["abandoned"] is True
    assert state["abandoned_at"]
    assert state["phase"] == "abandoned"
    # 放弃不删东西：工作区文件还在
    assert "tool.py" in state["files"]


def test_repeated_abandon_is_idempotent_and_changes_nothing(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    first = ws.abandon(task.id)
    assert first["ok"] is True
    before = _snapshot(ws, task.id)

    readiness = ws.abandon_readiness(task.id)
    assert readiness["status"] == "already_abandoned"
    assert readiness["allowed"] is True

    second = ws.abandon(task.id)

    assert second["ok"] is True
    assert second["status"] == "already_abandoned"
    assert second["revoked"] is False
    assert _snapshot(ws, task.id) == before


def test_unknown_task_is_not_found(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    readiness = ws.abandon_readiness("ws_ffffffffffff")
    assert readiness["status"] == "not_found"
    assert readiness["allowed"] is False
    assert "ws_ffffffffffff" in readiness["message"]

    result = ws.abandon("ws_ffffffffffff")
    assert result["ok"] is False
    assert result["status"] == "not_found"


def test_submitted_task_cannot_be_abandoned_and_state_is_untouched(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.mark_submitted(task.id)
    before = _snapshot(ws, task.id)

    readiness = ws.abandon_readiness(task.id)
    assert readiness["status"] == "submitted"
    assert readiness["allowed"] is False
    assert "撤销工具" in readiness["message"]

    result = ws.abandon(task.id)

    assert result["ok"] is False
    assert result["status"] == "submitted"
    assert _snapshot(ws, task.id) == before


def test_running_task_is_refused_with_zero_state_change(tmp_path):
    ws, task, _ = _workspace_with_tool(tmp_path)
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    ws.begin_run(task.id, "tests")
    ws.set_run_stage(task.id, "executing")
    before = _snapshot(ws, task.id)

    readiness = ws.abandon_readiness(task.id)

    assert readiness["status"] == "running"
    assert readiness["allowed"] is False
    assert readiness["can_stop"] is False
    assert readiness["run"]["stage"] == "executing"
    # 诚实表述：说清「没有只停止这一个任务的能力」，不说「已停止」。
    assert "没有只停止这一个任务的能力" in readiness["message"]
    assert "已放弃" not in readiness["message"]
    assert "已停止" not in readiness["message"]

    result = ws.abandon(task.id)

    assert result["ok"] is False
    assert result["status"] == "running"
    # 零状态改动：不标放弃、不收回授权、不动 state.json
    assert _snapshot(ws, task.id) == before
    assert ws.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is True
    ws.end_run(task.id, "tests")


def test_can_stop_is_derived_from_a_registered_stopper(tmp_path):
    """can_stop 必须是**算出来的真实能力**，不是写死的 False。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")

    ws.begin_run(task.id, "tests")
    assert ws.active_run(task.id)["can_stop"] is False
    ws.set_run_stage(task.id, "executing")
    assert ws.abandon_readiness(task.id)["can_stop"] is False
    ws.end_run(task.id, "tests")

    # 有人真的登记了「只停止这一次执行」的句柄 → 能力立刻变 True
    stopped = {"n": 0}
    ws.begin_run(task.id, "tests", stopper=lambda: stopped.__setitem__("n", 1))
    run = ws.active_run(task.id)
    assert run["can_stop"] is True
    ws.set_run_stage(task.id, "executing")
    readiness = ws.abandon_readiness(task.id)
    assert readiness["can_stop"] is True
    assert "没有只停止这一个任务的能力" not in readiness["message"]
    # 登记表不把内部句柄漏给调用方
    assert "stopper" not in run


def test_end_run_only_clears_the_same_kind(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.begin_run(task.id, "submit")
    ws.end_run(task.id, "tests")  # 迟到的 tests 结束不能抹掉正在跑的 submit
    assert ws.active_run(task.id)["kind"] == "submit"
    ws.end_run(task.id, "submit")
    assert ws.active_run(task.id) is None


def test_waiting_approval_allows_abandon(tmp_path):
    """等审批 ≠ 在执行：这时放弃是允许的（未决审批由接口层作废）。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.begin_run(task.id, "tests")
    ws.set_run_stage(task.id, "waiting_approval")

    readiness = ws.abandon_readiness(task.id)

    assert readiness["status"] == "ok"
    assert readiness["allowed"] is True
    assert readiness["run"]["stage"] == "waiting_approval"
    assert "作废" in readiness["message"]
    assert ws.abandon(task.id)["ok"] is True
    assert ws.status(task.id)["abandoned"] is True


def test_terminal_state_cannot_be_resurrected_by_late_writes(tmp_path):
    """迟到的 set_phase / record_test / mark_submitted / 授权都不能复活任务。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.abandon(task.id)
    before = _snapshot(ws, task.id)

    # 全部写操作：一个都不许生效
    ws.set_phase(task.id, "testing_passed")
    ws.record_test(task.id, True, "迟到的测试结果")
    ws.mark_submitted(task.id)
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", lifetime="long_term"
    )
    assert ws.consume_test_authorization(task.id) is False
    assert ws.archive(task.id) is None
    with pytest.raises(ValueError):
        ws.write_file(task.id, "late.py", "print('late')")

    assert _snapshot(ws, task.id) == before
    state = ws.status(task.id)
    assert state["abandoned"] is True
    assert state["phase"] == "abandoned"
    assert state["submitted"] is False
    assert state["test_runs"] == 1  # 放弃前那一次，迟到的没有被记上
    assert state["test_authorized"] is False
    assert "late.py" not in state["files"]
    # 没有长期授权被偷偷建立
    assert ws.test_authorized(
        "ws_ffffffffffff", policy_fingerprint="p1", executor="subprocess"
    ) is False
    assert not (ws.root_dir / "archive" / task.id).exists()


def test_abandon_revokes_task_and_long_term_authorizations(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    ws.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess", lifetime="long_term"
    )

    result = ws.abandon(task.id)

    assert result["ok"] is True
    assert result["revoked"] is True
    assert ws.status(task.id)["test_authorized"] is False
    # 长期授权是跨任务生效的：放弃时必须一并收回，否则别的任务还在用它
    assert ws.test_authorized(
        "ws_ffffffffffff", policy_fingerprint="p1", executor="subprocess"
    ) is False
    assert ws.authorizations() == []


def test_abandon_does_not_touch_another_task(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    target = ws.create("要放弃的任务")
    other = ws.create("还在做的任务")
    ws.grant_test_authorization(other.id, policy_fingerprint="p2", executor="subprocess")
    ws.record_test(other.id, True, "1/1 tests passed")
    other_before = _snapshot(ws, other.id)

    ws.abandon(target.id)

    assert ws.status(other.id)["abandoned"] is False
    assert ws.status(other.id)["phase"] == "testing_passed"
    assert ws.test_authorized(other.id, policy_fingerprint="p2", executor="subprocess") is True
    assert _snapshot(ws, other.id) == other_before
    # 别的任务仍然可以继续开发
    ws.set_phase(other.id, "building")
    assert ws.status(other.id)["phase"] == "building"


def test_abandon_survives_restart_with_evidence_and_files(tmp_path):
    root = tmp_path / "ws"
    ws, task, definition = _workspace_with_tool(tmp_path, request="做一个求和工具")
    ws.record_test(task.id, True, "1/1 tests passed")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    ws.abandon(task.id)

    reborn = DevWorkspace(root)

    restored = reborn.task(task.id)
    assert restored is not None
    assert restored.abandoned is True
    assert restored.abandoned_at == ws.task(task.id).abandoned_at
    assert restored.phase == "abandoned"
    # 授权已收回（重启后不能复活）
    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert reborn.authorizations() == []
    # 工作区文件与测试证据都还在
    assert "tool.json" in reborn.list_files(task.id)
    state = reborn.status(task.id)
    assert state["last_test_passed"] is True
    assert state["last_test_summary"] == "1/1 tests passed"
    assert state["abandoned"] is True
    assert reborn.abandon_readiness(task.id)["status"] == "already_abandoned"


def test_state_json_written_by_this_change_carries_the_terminal_state(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.abandon(task.id)
    raw = json.loads((task.dir / "state.json").read_text(encoding="utf-8"))
    assert raw["abandoned"] is True
    assert raw["abandoned_at"]
    assert raw["phase"] == "abandoned"


# ---------------------------------------------------------------------------
# 2. state.json 向后兼容（硬要求）
# ---------------------------------------------------------------------------


def test_handwritten_old_schema_state_is_read_back_without_losing_anything(tmp_path):
    """手工造一份**本改动之前**格式的 state.json（schema 2，没有 abandoned 键），
    断言测试证据、授权记录、提交摘要一个都不丢。"""
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    task_id = "ws_0123456789ab"
    task_dir = root / task_id
    task_dir.mkdir()
    (task_dir / "request.md").write_text("# 开发需求\n\n查文献\n", encoding="utf-8")
    (task_dir / "tool.py").write_text("def run(**kwargs):\n    return 1\n", encoding="utf-8")
    # 摘要按生产同一算法算：先让 DevWorkspace 认一下磁盘内容
    digest = DevWorkspace(root).content_digest(task_id)
    assert digest

    authorization = {
        "policy_fingerprint": "p1",
        "executor": "subprocess",
        "at": "2026-09-01T00:00:00+00:00",
        "lifetime": "task",
        "content_digest": digest,
        "filesystem": [],
        "network": False,
        "network_allow": [],
        "credentials": [],
        "owner_task_id": task_id,
    }
    (task_dir / "state.json").write_text(
        json.dumps(
            {
                "schema": 2,  # 旧号
                "source": "qio.dev_workspace",
                "id": task_id,
                "request": "查文献",
                "created_at": "2026-09-01T00:00:00+00:00",
                "phase": "submitted",
                "submitted": True,
                "test_runs": 3,
                "last_test_passed": True,
                "last_test_summary": "2/2 tests passed",
                "last_test_at": "2026-09-01T01:00:00+00:00",
                "last_test_digest": digest,
                "evidence_state": "current",
                "test_authorization": authorization,
                "submitted_digest": digest,
                "submitted_at": "2026-09-01T02:00:00+00:00",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    reborn = DevWorkspace(root)
    restored = reborn.task(task_id)

    assert restored is not None
    # 测试证据
    assert restored.test_runs == 3
    assert restored.last_test_passed is True
    assert restored.last_test_summary == "2/2 tests passed"
    assert restored.last_test_at == "2026-09-01T01:00:00+00:00"
    assert restored.last_test_digest == digest
    assert restored.evidence_state == "current"
    assert reborn.status(task_id)["test_evidence_current"] is True
    # 授权记录
    assert restored.test_authorization is not None
    assert restored.test_authorization["policy_fingerprint"] == "p1"
    assert restored.test_authorization["lifetime"] == "task"
    assert restored.test_authorization["content_digest"] == digest
    assert reborn.authorizations()[0]["task_id"] == task_id
    # 提交摘要
    assert restored.submitted is True
    assert restored.submitted_digest == digest
    assert restored.submitted_at == "2026-09-01T02:00:00+00:00"
    # 旧文件里没有 abandoned → 就是「没有被放弃」
    assert restored.abandoned is False
    assert restored.abandoned_at is None
    assert reborn.status(task_id)["abandoned"] is False


def test_old_schema_task_can_still_be_abandoned(tmp_path):
    """旧格式读回来的任务仍然可以被放弃（不能因为升 schema 就变成不可操作）。"""
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    state_file = task.dir / "state.json"
    old = json.loads(state_file.read_text(encoding="utf-8"))
    old["schema"] = 2
    old.pop("abandoned")
    old.pop("abandoned_at")
    state_file.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    reborn = DevWorkspace(root)
    assert reborn.abandon_readiness(task.id)["status"] == "ok"
    assert reborn.abandon(task.id)["ok"] is True
    assert DevWorkspace(root).task(task.id).abandoned is True


def test_abandoned_state_on_disk_is_never_read_back_as_active(tmp_path):
    """磁盘上写着 abandoned 的任务，读回时授权一律不认（终态的不变量）。"""
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    ws.grant_test_authorization(task.id, policy_fingerprint="p1", executor="subprocess")
    state_file = task.dir / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["abandoned"] = True
    state["abandoned_at"] = "2026-09-02T00:00:00+00:00"
    state["phase"] = "created"  # 故意写一个与终态矛盾的值
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    reborn = DevWorkspace(root)
    restored = reborn.task(task.id)

    assert restored.abandoned is True
    assert restored.phase == "abandoned"
    assert restored.test_authorization is None
    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False


# ---------------------------------------------------------------------------
# 3. 审批层：按任务作废
# ---------------------------------------------------------------------------


async def test_invalidate_for_task_cancels_the_waiter_without_raising():
    bus = EventBus()
    service = ApprovalService(bus, timeout_seconds=5.0)
    service.set_context(turn_id="turn_1")
    collected: list[str] = []

    async def consume():
        async for chunk in bus.stream():
            collected.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)

    pending = asyncio.create_task(
        service.request("tool_execution", {"workspace": "ws_1", "tool": "dev_run_tests"})
    )
    await asyncio.sleep(0.05)
    approval_id = next(iter(service._requests))

    assert service.invalidate_for_task("ws_1") == 1

    result = await asyncio.wait_for(pending, timeout=2)  # 不抛异常
    assert result.decision == "cancelled"
    assert result.approval_id == approval_id
    # 单次使用语义不变：作废过的审批再应答必然失败
    assert await service.respond(approval_id, "approved") is False
    assert service.pending() == []
    await asyncio.sleep(0.05)
    events = [
        json.loads(line[6:])
        for chunk in collected
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    results = [e for e in events if e["type"] == "APPROVAL_RESULT"]
    assert results, "界面要靠这条事件让确认卡自己消失"
    assert results[-1]["data"]["decision"] == "cancelled"
    assert results[-1]["data"]["reason"] == "task_abandoned"
    assert results[-1]["data"]["turn_id"] == "turn_1"
    consumer.cancel()


async def test_invalidate_matches_every_payload_shape():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    for payload in (
        {"workspace": "ws_a"},
        {"code_boundary": {"task_id": "ws_a"}},
        {"task_id": "ws_a"},
    ):
        task = asyncio.create_task(service.request("x", dict(payload)))
        await asyncio.sleep(0.02)
        assert service.invalidate_for_task("ws_a") == 1
        assert (await asyncio.wait_for(task, timeout=2)).decision == "cancelled"


async def test_invalidate_does_not_touch_other_tasks():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    mine = asyncio.create_task(service.request("x", {"workspace": "ws_a"}))
    other = asyncio.create_task(service.request("x", {"workspace": "ws_b"}))
    await asyncio.sleep(0.05)

    assert service.invalidate_for_task("ws_a") == 1

    other_id = [r["approval_id"] for r in service.pending()][0]
    assert await service.respond(other_id, "approved") is True
    assert (await asyncio.wait_for(other, timeout=2)).decision == "approved"
    assert (await asyncio.wait_for(mine, timeout=2)).decision == "cancelled"


async def test_invalidate_returns_zero_when_nothing_matches():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    assert service.invalidate_for_task("ws_nope") == 0


async def test_invalidate_settles_the_pending_record(db_conn: sqlite3.Connection):
    service = ApprovalService(EventBus(), timeout_seconds=5.0, conn=db_conn)
    pending = asyncio.create_task(service.request("x", {"workspace": "ws_a"}))
    await asyncio.sleep(0.05)

    assert service.invalidate_for_task("ws_a", reason="task_abandoned") == 1
    await asyncio.wait_for(pending, timeout=2)

    row = db_conn.execute(
        "SELECT status FROM pending_approvals ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4. 工具层守卫
# ---------------------------------------------------------------------------


async def test_run_tests_refuses_an_abandoned_task_without_executing(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    sandbox = _RecordingSandbox()
    approvals = _FakeApprovals()
    ws.abandon(task.id)

    result = await DevRunTestsTool(ws, sandbox=sandbox, approvals=approvals).run(
        workspace=task.id
    )

    assert result.ok is False
    assert result.category == "abandoned"
    assert result.error == ABANDONED_ERROR
    assert sandbox.executions == 0
    assert approvals.requests == []
    assert ws.status(task.id)["last_test_passed"] is None


async def test_submit_refuses_an_abandoned_task_without_building_lifecycle(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    built = {"n": 0}

    async def builder():
        built["n"] += 1
        raise AssertionError("已放弃的任务不该走到提交审批")

    ws.abandon(task.id)
    result = await DevSubmitTool(ws, lifecycle_builder=builder).run(
        workspace=task.id, explanation="求和工具"
    )

    assert result.ok is False
    assert result.category == "abandoned"
    assert built["n"] == 0


async def test_file_tools_refuse_an_abandoned_task(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    ws.write_file(task.id, "notes.txt", "还在写")
    ws.abandon(task.id)

    write = await DevWriteFileTool(ws).run(
        workspace=task.id, name="late.txt", content="迟到的写入"
    )
    read = await DevReadFileTool(ws).run(workspace=task.id, name="notes.txt")
    listing = await DevListFilesTool(ws).run(workspace=task.id)

    for result in (write, read, listing):
        assert result.ok is False
        assert result.category == "abandoned"
        assert "已经被放弃" in result.error
    assert "late.txt" not in ws.list_files(task.id)


async def test_list_tasks_marks_abandoned_as_terminal(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    abandoned = ws.create("没做完的任务")
    ws.abandon(abandoned.id)

    result = await DevListTasksTool(ws).run()

    assert result.ok is True
    assert "已放弃" in result.content
    assert "要重做请新建开发任务" in result.content
    # 不能把它列成「可以做」的任务（不再出现阶段/测试结论那套字段）
    assert "阶段 created" not in result.content


async def test_abandoned_task_blocks_execution_authorization_without_asking(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    approvals = _FakeApprovals()
    ws.abandon(task.id)

    blocked = await ensure_test_authorization(
        workspaces=ws,
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=definition,
    )

    assert blocked is not None
    assert blocked.ok is False
    assert blocked.category == "permission"
    assert "已经被放弃" in blocked.error
    assert approvals.requests == [], "已放弃的任务不该再打扰用户"


async def test_cancelled_authorization_is_not_described_as_a_user_refusal(tmp_path):
    """作废不是用户拒绝：说成「你拒绝了」会误导用户。"""
    from agent.tools.dev_auth import _refusal_text

    text = _refusal_text("cancelled")
    assert not text.startswith("你拒绝了")
    assert "不是你的拒绝" in text
    assert "已经被放弃" in text or "本轮已停止" in text


async def test_abandon_while_waiting_for_authorization_never_executes(tmp_path):
    """等审批时放弃：审批作废、等待方不抛异常、不执行生成代码、不注册。"""
    ws, task, definition = _workspace_with_tool(tmp_path)
    approvals = ApprovalService(EventBus(), timeout_seconds=5.0)
    sandbox = _RecordingSandbox()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=approvals)

    running = asyncio.create_task(tool.run(workspace=task.id))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if approvals.pending():
            break
    assert approvals.pending(), "这次执行应该正在等用户确认"
    assert ws.active_run(task.id)["stage"] == "waiting_approval"

    # 接口层的顺序：先作废未决审批，再标放弃
    invalidated = approvals.invalidate_for_task(task.id)
    abandoned = ws.abandon(task.id)

    result = await asyncio.wait_for(running, timeout=5)

    assert invalidated == 1
    assert abandoned["ok"] is True
    assert result.ok is False
    assert result.category == "permission"
    assert sandbox.executions == 0, "放弃之后绝不能再执行生成代码"
    assert ws.active_run(task.id) is None, "执行结束后不能留下僵尸登记"
    assert ws.status(task.id)["abandoned"] is True


# ---------------------------------------------------------------------------
# 5. 注册前守卫（lifecycle）
# ---------------------------------------------------------------------------


def _lifecycle(*, approvals, registry=None, store=None, sandbox=None):
    from agent.tools.lifecycle import ToolLifecycle

    class _Adapter:
        mode = "native"

    return ToolLifecycle(
        adapter=_Adapter(),
        approvals=approvals,
        sandbox=sandbox or SandboxExecutor(executor="subprocess"),
        registry=registry or ToolRegistry(),
        tool_store=store,
    )


async def test_registration_guard_refuses_without_saving_or_registering(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    registry = ToolRegistry()
    store = _FakeToolStore()
    lifecycle = _lifecycle(approvals=_FakeApprovals(), registry=registry, store=store)
    lifecycle.abandon_guard = lambda: ABANDONED_ERROR

    outcome = await lifecycle.submit_definition(
        definition,
        "求和工具",
        skip_tests=True,
        group_id=task.id,
    )

    assert outcome.ok is False
    assert outcome.step == "abandoned"
    assert outcome.detail == ABANDONED_ERROR
    assert store.saved == [], "守卫拒绝时不能落盘"
    assert registry.get(definition.name) is None, "守卫拒绝时不能进注册表"


async def test_registration_guard_lets_a_normal_task_through(tmp_path):
    ws, task, definition = _workspace_with_tool(tmp_path)
    registry = ToolRegistry()
    store = _FakeToolStore()
    lifecycle = _lifecycle(approvals=_FakeApprovals(), registry=registry, store=store)
    lifecycle.abandon_guard = lambda: None

    outcome = await lifecycle.submit_definition(
        definition,
        "求和工具",
        skip_tests=True,
        group_id=task.id,
    )

    assert outcome.ok is True
    assert store.saved == [definition.name]
    assert registry.get(definition.name) is not None


async def test_abandon_while_waiting_for_the_create_approval_does_not_register(tmp_path):
    """端到端：用户等在「是否创建这个工具」的确认上时点了放弃 → 工具不注册。

    走真实的 DevSubmitTool → ToolLifecycle → ApprovalService 链路，只有复测用的
    沙箱换成替身（本机起不了子进程）。
    """
    ws, task, definition = _workspace_with_tool(tmp_path)
    # 先让「跑生成代码」的执行授权就绪，否则提交会停在测试授权那一步
    admitted = await ensure_test_authorization(
        workspaces=ws,
        approvals=_FakeApprovals(),
        sandbox=SandboxExecutor(executor="subprocess"),
        task_id=task.id,
        definition=definition,
    )
    assert admitted is None

    approvals = ApprovalService(EventBus(), timeout_seconds=5.0)
    registry = ToolRegistry()
    store = _FakeToolStore()
    sandbox = _RecordingSandbox()
    lifecycle = _lifecycle(
        approvals=approvals, registry=registry, store=store, sandbox=sandbox
    )

    async def builder():
        return lifecycle

    tool = DevSubmitTool(
        ws, lifecycle_builder=builder, sandbox=sandbox, approvals=approvals
    )
    submitting = asyncio.create_task(tool.run(workspace=task.id, explanation="求和工具"))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if approvals.pending():
            break
    assert approvals.pending(), "提交应该正在等创建确认"
    # 注册审批的载荷必须带上 workspace，否则按任务作废找不到它
    assert approvals.pending()[0]["payload"]["workspace"] == task.id

    assert approvals.invalidate_for_task(task.id) == 1
    assert ws.abandon(task.id)["ok"] is True

    result = await asyncio.wait_for(submitting, timeout=5)

    assert result.ok is False
    assert "abandoned" in (result.error or "")
    assert store.saved == [], "已放弃的任务不能落盘注册"
    assert registry.get(definition.name) is None, "已放弃的任务不能进注册表"
    assert ws.status(task.id)["abandoned"] is True


# ---------------------------------------------------------------------------
# 6. 接口层
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    """本机可能设了 QIO_DATA_DIR：这些用例必须只看自己那份临时目录。"""
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _row(client, task_id: str) -> dict:
    rows = client.get("/api/dev/tasks").json()["tasks"]
    return [row for row in rows if row["id"] == task_id][0]


def test_dev_tasks_rows_expose_the_abandoned_fields(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("没做完的任务")

    row = _row(client, task.id)
    assert row["abandoned"] is False
    assert row["abandoned_at"] is None
    # 既有字段一个都不许改名或删除
    assert {
        "id",
        "request",
        "phase",
        "submitted",
        "test_passed",
        "test_evidence_current",
        "updated_at",
        "authorized",
    } <= set(row)

    ctx.dev_workspaces.abandon(task.id)

    row = _row(client, task.id)  # 已放弃的任务仍在列表里（事实清单）
    assert row["abandoned"] is True
    assert row["abandoned_at"]
    assert row["phase"] == "abandoned"
    assert row["authorized"] is False


def test_abandon_endpoint_returns_the_documented_shape(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("没做完的任务")

    resp = client.post(f"/api/dev/tasks/{task.id}/abandon")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "abandoned"
    assert body["revoked"] is False
    assert body["invalidated_approvals"] == 0
    assert body["can_stop"] is False
    assert body["message"]
    # task 字段的形状与列表行完全一致
    assert set(body["task"]) == set(_row(client, task.id))
    assert body["task"]["abandoned"] is True
    assert body["task"]["phase"] == "abandoned"


def test_abandon_endpoint_is_idempotent(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    first = client.post(f"/api/dev/tasks/{task.id}/abandon").json()
    before = _row(client, task.id)

    second = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["status"] == "already_abandoned"
    assert second["revoked"] is False
    assert _row(client, task.id) == before


def test_abandon_endpoint_404_for_an_unknown_task(client):
    resp = client.post("/api/dev/tasks/ws_ffffffffffff/abandon")
    assert resp.status_code == 404
    assert "ws_ffffffffffff" in resp.json()["detail"]


def test_abandon_endpoint_refuses_a_running_task_without_touching_anything(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    ctx.dev_workspaces.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess"
    )
    ctx.dev_workspaces.begin_run(task.id, "tests")
    ctx.dev_workspaces.set_run_stage(task.id, "executing")
    before = _row(client, task.id)
    state_file = ctx.dev_workspaces.task(task.id).dir / "state.json"
    before_state = state_file.read_text(encoding="utf-8")
    cancelled = {"n": 0}
    original_cancel = ctx.turns.cancel_active

    def _record_cancel():
        cancelled["n"] += 1
        return original_cancel()

    ctx.turns.cancel_active = _record_cancel

    resp = client.post(f"/api/dev/tasks/{task.id}/abandon")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "running"
    assert body["can_stop"] is False
    assert body["revoked"] is False
    assert body["invalidated_approvals"] == 0
    assert "没有只停止这一个任务的能力" in body["message"]
    # 零状态改动：不标放弃、不收回授权、不作废审批、不停任何东西
    assert _row(client, task.id) == before
    assert state_file.read_text(encoding="utf-8") == before_state
    assert ctx.dev_workspaces.test_authorized(
        task.id, policy_fingerprint="p1", executor="subprocess"
    ) is True
    assert cancelled["n"] == 0, "放弃接口绝不能调用 turn 取消（会误停别的任务）"
    ctx.dev_workspaces.end_run(task.id, "tests")


def test_abandon_endpoint_refuses_a_submitted_task(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    ctx.dev_workspaces.mark_submitted(task.id)
    before = _row(client, task.id)

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is False
    assert body["status"] == "submitted"
    assert "撤销工具" in body["message"]
    assert _row(client, task.id) == before


def test_abandon_endpoint_invalidates_pending_approvals_first(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")

    async def arm():
        ctx.approvals.set_context(turn_id="turn_1")
        pending = asyncio.create_task(
            ctx.approvals.request("tool_execution", {"workspace": task.id})
        )
        await asyncio.sleep(0.05)
        return pending

    pending = client.portal.call(arm)
    assert len(ctx.approvals.pending()) == 1

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is True
    assert body["invalidated_approvals"] == 1
    assert ctx.approvals.pending() == []
    # 等待方收到 cancelled（不是异常），并据此不执行
    assert client.portal.call(lambda: pending).decision == "cancelled"
    assert _row(client, task.id)["abandoned"] is True


def test_abandon_endpoint_revokes_authorizations(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    ctx.dev_workspaces.grant_test_authorization(
        task.id, policy_fingerprint="p1", executor="subprocess"
    )
    assert client.get("/api/dev/authorizations").json()["authorizations"]

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is True
    assert body["revoked"] is True
    assert client.get("/api/dev/authorizations").json()["authorizations"] == []
    assert _row(client, task.id)["authorized"] is False


def test_abandon_endpoint_does_not_touch_another_task(client):
    ctx = client.app.state.ctx
    target = ctx.dev_workspaces.create("要放弃的")
    other = ctx.dev_workspaces.create("还在做的")
    ctx.dev_workspaces.grant_test_authorization(
        other.id, policy_fingerprint="p2", executor="subprocess"
    )
    before = _row(client, other.id)

    client.post(f"/api/dev/tasks/{target.id}/abandon")

    assert _row(client, other.id) == before
    assert _row(client, other.id)["abandoned"] is False
    assert _row(client, other.id)["authorized"] is True


def test_dev_tasks_list_is_empty_on_a_fresh_install(client):
    assert client.get("/api/dev/tasks").json() == {"tasks": []}
