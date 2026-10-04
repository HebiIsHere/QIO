"""凭据授权审批与开发任务的关联：放弃任务时能按任务作废它。

契约见仓库根 `_ABANDON-CONTRACT.md` 的「冻结契约 v2」B 章（B1–B5）。这里逐条锁住：

* **B1**：`credential_grant` 审批载荷带 `"workspace": group_id`，与 `tool_create`
  完全同一种写法，直接复用 `ApprovalService._refers_to` 既有的识别规则；
* **B2**：放弃一个任务能作废它的三类审批（`tool_execution` / `tool_create` /
  `credential_grant`），等待方都收到**明确**的 `cancelled`（不抛异常、不静默丢弃）；
* **B3**：作废的文案说「任务已放弃，这个确认已作废」，不写成「你没有同意」；
* **B5**：别的任务、与开发任务无关的独立凭据审批、已注册工具都不受影响。

这里的审批层是**真实**的 `ApprovalService`（带落库连接）与真实 `ToolLifecycle`；
只有凭据库 / 工具持久层 / 沙箱是替身 —— 审批的载荷形状与结局不允许被替身抹平。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from agent.api.bus import EventBus
from agent.tools.approval import ApprovalService
from agent.tools.dev_auth import _refusal_text
from agent.tools.dev_tools import ABANDONED_ERROR
from agent.tools.lifecycle import ToolLifecycle
from agent.tools.registry import ToolRegistry
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition

TASK = "ws_0123456789ab"
OTHER_TASK = "ws_ffffffffffff"
CRED = "weather-key"


# ---------------------------------------------------------------------------
# 替身：只替换「不测就不能跑」的那些外部件（沙箱 / 凭据库 / 工具持久层）
# ---------------------------------------------------------------------------


class _Adapter:
    mode = "native"


class _RecordingSandbox:
    """记录有没有真的跑生成代码；本文件全程 skip_tests，跑过就是 bug。"""

    executor = "subprocess"
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.executions = 0

    async def effective_executor(self) -> str:
        return "subprocess"


class _Creds:
    """凭据库替身：只回答「这把钥匙可用」。"""

    def __init__(self) -> None:
        self.scopes: list[tuple[str, str]] = []

    def get_metadata(self, ref: str):
        return {"status": "active", "id": ref} if ref == CRED else None

    def grant_tool_scope(self, ref: str, tool: str) -> None:
        self.scopes.append((ref, tool))


class _ToolStore:
    """工具持久层替身：用来证明「作废之后一个字都没落盘」。"""

    def __init__(self) -> None:
        self.saved: list[str] = []

    def load(self, name: str):
        return None

    def save(self, definition) -> None:
        self.saved.append(definition.name)

    def remove(self, name: str) -> None:
        if name in self.saved:
            self.saved.remove(name)


class _BusDriver:
    """消费真实事件流：自动批准 `tool_create`，其余审批留在等待状态。

    「自动批准工具创建」是为了把流程推进到凭据那一段；凭据审批**绝不**自动
    批准 —— 它就是要被放弃作废的那一条。
    """

    AUTO_APPROVE = ("tool_create",)

    def __init__(self, bus: EventBus, approvals: ApprovalService) -> None:
        self.bus = bus
        self.approvals = approvals
        self.events: list[dict] = []
        self.create_payloads: list[dict] = []
        self._task: asyncio.Task | None = None

    async def _consume(self) -> None:
        async for chunk in self.bus.stream():
            for line in chunk.splitlines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                self.events.append(event)
                if event["type"] != "APPROVAL_REQUIRED":
                    continue
                approval = event["data"]["approval"]
                if approval["kind"] in self.AUTO_APPROVE:
                    self.create_payloads.append(approval["payload"])
                    await self.approvals.respond(approval["approval_id"], "approved")

    def start(self) -> None:
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def of_type(self, event_type: str) -> list[dict]:
        return [event for event in self.events if event["type"] == event_type]

    async def wait_for_pending(
        self, kind: str, *, workspace: str | None = None, count: int = 1
    ) -> list[dict]:
        for _ in range(500):
            await asyncio.sleep(0.01)
            found = [
                item
                for item in self.approvals.pending()
                if item["kind"] == kind
                and (workspace is None or item["payload"].get("workspace") == workspace)
            ]
            if len(found) >= count:
                return found
        raise AssertionError(
            f"没有等到 {kind} 审批（workspace={workspace}）：{self.approvals.pending()}"
        )


def _definition(name: str = "weather_fetch") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="查询天气",
        code="def run(**kwargs):\n    return {'ok': True}",
        credential_ref=CRED,
    )


def _lifecycle(
    approvals: ApprovalService,
    *,
    registry: ToolRegistry,
    store: _ToolStore,
    sandbox: _RecordingSandbox | None = None,
    bus: EventBus | None = None,
) -> ToolLifecycle:
    return ToolLifecycle(
        adapter=_Adapter(),
        approvals=approvals,
        sandbox=sandbox or _RecordingSandbox(),
        registry=registry,
        credentials=_Creds(),
        tool_store=store,
        # 进度事件（TOOL_CREATE_STATUS）也走真实总线：作废后的文案要靠它证明
        bus=bus,
    )


# ---------------------------------------------------------------------------
# B1 + B2 + B3：凭据审批带任务标识；放弃按任务作废它；文案不写成用户拒绝
# ---------------------------------------------------------------------------


async def test_credential_grant_payload_carries_the_task_and_is_invalidated(
    db_conn: sqlite3.Connection,
):
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5.0, conn=db_conn)
    registry = ToolRegistry()
    store = _ToolStore()
    sandbox = _RecordingSandbox()
    lifecycle = _lifecycle(
        approvals, registry=registry, store=store, sandbox=sandbox, bus=bus
    )
    abandoned = {"value": False}
    # 与生产一致：DevSubmitTool 会把「这个任务是不是已放弃」注入成注册前守卫
    lifecycle.abandon_guard = lambda: ABANDONED_ERROR if abandoned["value"] else None
    driver = _BusDriver(bus, approvals)
    driver.start()
    try:
        running = asyncio.create_task(
            lifecycle.submit_definition(
                _definition(), "查询天气", skip_tests=True, group_id=TASK
            )
        )
        [grant] = await driver.wait_for_pending("credential_grant", workspace=TASK)

        # B1：载荷指向这个任务，写法与 tool_create 一模一样（同一个 key、同一个值）
        assert grant["payload"]["workspace"] == TASK
        assert grant["payload"]["key_id"] == CRED
        assert grant["payload"]["tool_name"] == "weather_fetch"
        assert driver.create_payloads, "创建审批应当已经被自动批准"
        assert driver.create_payloads[0]["workspace"] == TASK, (
            "credential_grant 要与 tool_create 用同一种写法"
        )

        # 用户放弃任务：接口层的顺序是「先放弃（严格落盘），再作废未决审批」
        abandoned["value"] = True
        assert approvals.invalidate_for_task(TASK) == 1

        outcome = await asyncio.wait_for(running, timeout=5)
    finally:
        await driver.stop()

    # B2：等待方拿到的是明确的 cancelled（不抛异常、不静默丢弃）→ 走 cancelled 分支
    assert outcome.ok is False
    assert outcome.step == "credential"
    # B3：作废文案说「任务已放弃，这个确认已作废」，不是「你没有同意」
    assert "已放弃" in outcome.detail
    assert "作废" in outcome.detail
    assert "没有同意" not in outcome.detail
    failed = [
        event
        for event in driver.of_type("TOOL_CREATE_STATUS")
        if event["data"]["phase"] == "failed"
    ]
    assert failed, "作废之后必须有明确的失败事件"
    assert failed[-1]["data"]["label"] == "已放弃"
    assert "任务已放弃" in failed[-1]["data"]["detail"]
    assert "作废" in failed[-1]["data"]["detail"]
    assert "你拒绝" not in json.dumps(failed[-1]["data"], ensure_ascii=False)
    assert "没有同意" not in failed[-1]["data"]["detail"]

    # 落库的结局是 cancelled（不是 pending、也不是 rejected）
    row = db_conn.execute(
        "SELECT status FROM pending_approvals WHERE approval_id = ?",
        (grant["approval_id"],),
    ).fetchone()
    assert row["status"] == "cancelled"

    # 单次使用：作废过的审批再允许一次必然无效
    assert await approvals.respond(grant["approval_id"], "approved") is False

    # 作废之后不注册、不落盘、也不执行任何生成代码
    assert registry.get("weather_fetch") is None
    assert store.saved == []
    assert sandbox.executions == 0


async def test_invalidate_cancels_all_three_approval_kinds_of_one_task(
    db_conn: sqlite3.Connection,
):
    """B2：测试执行 / 工具创建 / 凭据授权三类审批都要被作废，等待方都是 cancelled。"""
    service = ApprovalService(EventBus(), timeout_seconds=5.0, conn=db_conn)
    waiters = {
        "tool_execution": asyncio.create_task(
            service.request(
                "tool_execution",
                {"workspace": TASK, "code_boundary": {"task_id": TASK}},
            )
        ),
        "tool_create": asyncio.create_task(
            service.request("tool_create", {"name": "weather_fetch", "workspace": TASK})
        ),
        "credential_grant": asyncio.create_task(
            service.request(
                "credential_grant",
                {
                    "key_id": CRED,
                    "tool_name": "weather_fetch",
                    "capabilities": ["读取凭据"],
                    "workspace": TASK,
                },
            )
        ),
    }
    other = asyncio.create_task(
        service.request("tool_execution", {"workspace": OTHER_TASK})
    )
    await asyncio.sleep(0.05)
    ids = {
        item["kind"]: item["approval_id"]
        for item in service.pending()
        if item["payload"].get("workspace") == TASK
    }
    assert set(ids) == set(waiters)

    assert service.invalidate_for_task(TASK) == 3

    results = {
        kind: (await asyncio.wait_for(task, timeout=2)).decision
        for kind, task in waiters.items()
    }
    assert results == {
        "tool_execution": "cancelled",
        "tool_create": "cancelled",
        "credential_grant": "cancelled",
    }
    for approval_id in ids.values():
        row = db_conn.execute(
            "SELECT status FROM pending_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        assert row["status"] == "cancelled"
        # 单次使用语义不变
        assert await service.respond(approval_id, "approved") is False

    # 别的任务的审批一条都不许碰
    remaining = service.pending()
    assert len(remaining) == 1
    assert remaining[0]["payload"]["workspace"] == OTHER_TASK
    assert await service.respond(remaining[0]["approval_id"], "approved") is True
    assert (await asyncio.wait_for(other, timeout=2)).decision == "approved"


async def test_a_standalone_credential_approval_without_a_task_is_untouched():
    """B5：与开发任务无关的独立凭据审批（载荷里没有任务标识）不被作废。"""
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    standalone = asyncio.create_task(
        service.request(
            "credential_grant",
            {
                "key_id": CRED,
                "tool_name": "some_other_tool",
                "capabilities": ["读取凭据"],
            },
        )
    )
    await asyncio.sleep(0.05)

    assert service.invalidate_for_task(TASK) == 0
    assert service.invalidate_for_task(OTHER_TASK) == 0
    pending = service.pending()
    assert len(pending) == 1 and pending[0]["kind"] == "credential_grant"
    assert await service.respond(pending[0]["approval_id"], "approved") is True
    assert (await asyncio.wait_for(standalone, timeout=2)).decision == "approved"


async def test_invalidating_one_task_leaves_another_tasks_credential_grant_alone(
    db_conn: sqlite3.Connection,
):
    """B5：端到端 —— 放弃 A 只作废 A 的凭据确认；B 照常确认、照常注册。"""
    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5.0, conn=db_conn)
    registry_a, store_a = ToolRegistry(), _ToolStore()
    registry_b, store_b = ToolRegistry(), _ToolStore()
    lifecycle_a = _lifecycle(approvals, registry=registry_a, store=store_a, bus=bus)
    lifecycle_b = _lifecycle(approvals, registry=registry_b, store=store_b, bus=bus)
    abandoned = {"value": False}
    lifecycle_a.abandon_guard = lambda: ABANDONED_ERROR if abandoned["value"] else None
    driver = _BusDriver(bus, approvals)
    driver.start()
    try:
        run_a = asyncio.create_task(
            lifecycle_a.submit_definition(
                _definition("weather_a"), "查询天气 A", skip_tests=True, group_id=TASK
            )
        )
        run_b = asyncio.create_task(
            lifecycle_b.submit_definition(
                _definition("weather_b"), "查询天气 B", skip_tests=True, group_id=OTHER_TASK
            )
        )
        grants = await driver.wait_for_pending("credential_grant", count=2)
        grant_a = next(g for g in grants if g["payload"]["workspace"] == TASK)
        grant_b = next(g for g in grants if g["payload"]["workspace"] == OTHER_TASK)

        abandoned["value"] = True
        assert approvals.invalidate_for_task(TASK) == 1

        outcome_a = await asyncio.wait_for(run_a, timeout=5)

        # B 的那张卡还在等人：批准它，B 正常走完注册
        assert await approvals.respond(grant_b["approval_id"], "approved") is True
        outcome_b = await asyncio.wait_for(run_b, timeout=5)
    finally:
        await driver.stop()

    assert outcome_a.ok is False
    assert outcome_a.step == "credential"
    assert "已放弃" in outcome_a.detail
    assert registry_a.get("weather_a") is None
    assert store_a.saved == []

    assert outcome_b.ok is True
    assert outcome_b.step == "registered"
    assert registry_b.get("weather_b") is not None
    assert store_b.saved == ["weather_b"]

    # A 的那条凭据审批落库为 cancelled，B 的是 approved
    row_a = db_conn.execute(
        "SELECT status FROM pending_approvals WHERE approval_id = ?", (grant_a["approval_id"],)
    ).fetchone()
    row_b = db_conn.execute(
        "SELECT status FROM pending_approvals WHERE approval_id = ?", (grant_b["approval_id"],)
    ).fetchone()
    assert row_a["status"] == "cancelled"
    assert row_b["status"] == "approved"


def test_the_cancelled_refusal_says_the_confirmation_was_invalidated():
    """B3：`dev_auth._refusal_text("cancelled")` 说的是「任务已放弃 / 确认已作废」，
    不是用户拒绝，也不能和超时混成一句。"""
    text = _refusal_text("cancelled")
    assert "任务已放弃" in text
    assert "作废" in text
    assert "你拒绝了" not in text
    assert "不是你的拒绝" in text
    assert text != _refusal_text("rejected")
    assert text != _refusal_text("timeout")
    assert "超时" not in text
