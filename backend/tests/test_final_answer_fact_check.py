"""最终结论的事实校正：主循环在收尾时补后端事实，且不重写模型的话。"""

from __future__ import annotations

from typing import Any

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.api.server import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _ScriptAdapter:
    """按脚本先调工具，最后给一句结论。"""

    mode = AdapterMode.NATIVE.value
    model = "fake"

    def __init__(self, script: list[tuple[str, dict]], answer: str) -> None:
        self.script = list(script)
        self.answer = answer
        self.seen: list[list[Any]] = []

    async def complete(self, messages, tools, **kwargs):
        self.seen.append(list(messages))
        if self.script:
            name, args = self.script.pop(0)
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id=f"c{len(self.seen)}", name=name, arguments=args)],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content=self.answer))


class _FailingDevTool(Tool):
    """真事故形状：测试失败，但模型随后说「全部通过」。"""

    name = "dev_run_tests"
    description = "测试（总是失败）"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            ok=False,
            error="测试失败 0/1 tests passed",
            category="assertion",
            facts={
                "dev_task": {
                    "id": "ws_ab12cd34ef56",
                    "tool_name": "dev_run_tests",
                    "phase": "testing_failed",
                    "version": "a" * 64,
                    "submitted": False,
                    "requires_tests": True,
                    "test": {"state": "current", "passed": False, "summary": "0/1 tests passed"},
                }
            },
        )


class _OkTool(Tool):
    name = "dev_list_files"
    description = "列文件"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=True, content="- tool.json")


class _CancelledTool(Tool):
    name = "run_shell"
    description = "被取消"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=False, error="工具已被取消")


class _DeclaringTool(Tool):
    """一次核对通过的结论声明。"""

    name = "declare_completion"
    description = "声明完成"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            ok=True,
            content="已核对：测试通过 与后端记录一致。",
            facts={
                "declaration": {
                    "accepted": True,
                    "task_id": "ws_ab12cd34ef56",
                    "version": "a" * 64,
                    "claims": ["test_passed"],
                    "basis": "版本 aaaaaaaaaaaa；测试 0/1 tests passed",
                    "missing": [],
                }
            },
        )


def _loop(registry: ToolRegistry, adapter: _ScriptAdapter) -> AgentLoop:
    return AgentLoop(adapter, registry, EventBus())


async def test_success_claim_after_failed_tool_is_corrected():
    registry = ToolRegistry()
    registry.register(_FailingDevTool())
    adapter = _ScriptAdapter([("dev_run_tests", {})], "测试全部通过，工具已就绪。")
    result = await _loop(registry, adapter).run("做一个工具")

    # 审计 F11：final_content 是**纯正文**，系统核对注释走独立字段 final_annotation
    # （旧实现把注释拼进 final_content，前端因「全文不等」会再补一条重复回答）。
    assert result.final_content == "测试全部通过，工具已就绪。"
    annotation = result.final_annotation or ""
    assert "系统核对" in annotation                     # 后端事实说明仍在，只是不再拼进正文
    assert "dev_run_tests" in annotation
    assert "ws_ab12cd34ef56" in annotation


async def test_retry_success_keeps_the_answer_untouched():
    """同一工具后来成功了，就不算未解决的失败。"""
    registry = ToolRegistry()
    registry.register(_OkTool())
    adapter = _ScriptAdapter([("dev_list_files", {})], "已经看过了工作区。")
    result = await _loop(registry, adapter).run("看看工作区")
    assert result.final_content == "已经看过了工作区。"


async def test_cancelled_tool_is_not_annotated():
    registry = ToolRegistry()
    registry.register(_CancelledTool())
    adapter = _ScriptAdapter([("run_shell", {})], "这条命令被取消了。")
    result = await _loop(registry, adapter).run("跑个命令")
    assert result.final_content == "这条命令被取消了。"


async def test_accepted_declaration_suppresses_the_note():
    registry = ToolRegistry()
    registry.register(_FailingDevTool())
    registry.register(_DeclaringTool())
    adapter = _ScriptAdapter(
        [("dev_run_tests", {}), ("declare_completion", {})],
        "已按后端记录说明：这一版测试没通过。",
    )
    result = await _loop(registry, adapter).run("做一个工具")
    assert result.final_content == "已按后端记录说明：这一版测试没通过。"


async def test_accepted_declaration_is_exposed_on_the_turn_result():
    """核对结论要能从这一轮的结果里取走：它会被落库并随 TURN_END 发出去。"""
    registry = ToolRegistry()
    registry.register(_FailingDevTool())
    registry.register(_DeclaringTool())
    adapter = _ScriptAdapter(
        [("dev_run_tests", {}), ("declare_completion", {})], "已经说明完了。"
    )
    result = await _loop(registry, adapter).run("做一个工具")
    assert result.verification is not None
    assert result.verification["accepted"] is True
    assert "版本 aaaaaaaaaaaa" in result.verification["basis"]
    assert result.verification["claims"] == ["test_passed"]


async def test_no_verification_without_a_declaration():
    registry = ToolRegistry()
    registry.register(_OkTool())
    adapter = _ScriptAdapter([("dev_list_files", {})], "看过了。")
    result = await _loop(registry, adapter).run("看看工作区")
    assert result.verification is None


async def test_turn_end_carries_the_verification():
    from agent.core.turn import TurnManager

    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:
        ctx.final_content = "好了"
        ctx.final_verification = {"accepted": True, "basis": "版本 a1b2；测试 1/1 通过"}

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("做一个工具")
    await manager.wait(ctx.turn_id, timeout=5)
    await manager.shutdown()

    ends = [data for name, data in events if name == "TURN_END"]
    assert ends, "没有发出 TURN_END"
    assert ends[-1]["verification"]["accepted"] is True
    assert ends[-1]["final_content"] == "好了"


class _DeclaringStub(Tool):
    """装配级的假核对工具：真实实现是 tools/declare_completion.py。"""

    name = "declare_stub"
    description = "核对结论"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            ok=True,
            content="已核对",
            facts={
                "declaration": {
                    "accepted": True,
                    "basis": "版本 a1b2c3d4e5f6；测试 1/1 通过",
                    "claims": ["test_passed"],
                }
            },
        )


async def test_verification_is_persisted_with_the_answer(tmp_path, monkeypatch):
    """核对结论要跟着那条 assistant 消息一起落库（前端靠 raw 渲染标记）。"""
    import json

    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.registry.register(_DeclaringStub())
    topic = ctx.topics.nodes.create_topic("核对标记").id

    class _Adapter:
        mode = AdapterMode.NATIVE.value
        model = "fake"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[ToolCall(id="c1", name="declare_stub", arguments={})],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content="可以用了。"))

    async def fake_build():
        return _Adapter()

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    result = await ctx.run_turn("做一个工具", topic_id=topic)
    assert result["ok"] is True

    row = ctx.conn.execute(
        "SELECT content, raw FROM messages WHERE role = 'assistant' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["content"] == "可以用了。"
    raw = json.loads(row["raw"])
    assert raw["verified"]["accepted"] is True
    assert "版本 a1b2c3d4e5f6" in raw["verified"]["basis"]
    assert raw["verified"]["claims"] == ["test_passed"]
