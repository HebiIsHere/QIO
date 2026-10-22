"""R9 授权/审批测试的共享夹具（不参与 pytest 收集）。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from agent.api.bus import EventBus
from agent.security.context import instruction_context
from agent.security.engine import AuthorizationEngine
from agent.security.instructions import InstructionRegistry
from agent.security.operations import ExecResult, OperationRegistry
from agent.storage.settings import SettingsStore
from agent.tools.approval import ApprovalService


def make_security(tmp_path, conn, *, mode=None, protected=None, workspace=None):
    """引擎 + 审批服务 + 操作注册表的真实（SQLite 支撑）小装配。"""
    store = SettingsStore(conn)
    if mode:
        store.set("computer.permission_mode", str(mode))
    workspace_path = workspace or (tmp_path / "workspace")
    workspace_path = workspace_path if hasattr(workspace_path, "mkdir") else workspace_path
    try:
        workspace_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    bus = EventBus()
    approvals = ApprovalService(bus, conn=conn)
    operations = OperationRegistry()
    engine = AuthorizationEngine(
        conn=conn,
        approvals=approvals,
        settings=store,
        instance_id="inst_test",
        workspace_root=lambda: workspace_path,
        internal_data_paths=lambda: list(protected or []),
    )
    approvals.set_engine(engine)
    approvals.set_operations(operations)
    return SimpleNamespace(
        conn=conn,
        bus=bus,
        store=store,
        engine=engine,
        approvals=approvals,
        operations=operations,
        instructions=InstructionRegistry(),
        workspace=workspace_path,
    )


@asynccontextmanager
async def with_instruction(security, text, *, turn_id="turn_test"):
    instruction = security.instructions.begin(turn_id=turn_id, text=text)
    try:
        with instruction_context(instruction):
            yield instruction
    finally:
        security.instructions.end(instruction)


def record_events(bus, sink: list | None = None) -> list:
    """把 bus.publish 包一层记录下来（返回事件对象列表）。"""
    events = sink if sink is not None else []
    original = bus.publish

    async def spy(event):
        events.append(event)
        await original(event)

    bus.publish = spy  # type: ignore[assignment]
    return events


async def wait_for(condition, *, timeout: float = 5.0, interval: float = 0.02):
    """等待异步执行器推进到条件成立；超时返回 False（测试里断言用）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return bool(condition())


def ok_executor(summary="已执行", *, detail=None):
    async def handler(payload, ctx):
        return ExecResult(ok=True, summary=summary, detail=dict(detail or {"payload": payload}))

    return handler


def counting_executor(counter, *, summary="已执行"):
    async def handler(payload, ctx):
        counter.append(payload)
        return ExecResult(ok=True, summary=summary, detail={"count": len(counter)})

    return handler
