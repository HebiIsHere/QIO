"""B2 独立验收：不完整流结束语义贯穿 TURN_END 与台账（F06 · 冻结契约 C2）。

反例（基线应为红）：SSE 没有合法结束标记就 EOF（native 无 finish_reason /
anthropic 无 message_stop / 仅 usage / 空流 / 未结束的工具调用）时，整轮被落成
status=completed —— 前端只能显示「正常完成」。

本文件把结束语义钉在终态 `incomplete` 上（这是本轮冻结契约新增的取值）：

1. 不完整 → TURN_END.status == "incomplete"、reason_code == "incomplete_stream"、
   stopped_by == "system"、人话 reason、actions == ["retry"]；已确认正文不丢；
2. 合法长度截断（length_limit）仍是 completed —— 只用 reason_code 区分，不虚构「未完成」；
3. 正常完整流仍是 completed + none + 无操作；
4. 取消优先于 incomplete（status=cancelled，不让「未完成」覆盖用户意图）；
5. `incomplete` 是台账终态：turn_journal 接受它，record_facts 后刷新仍能恢复
   「未完成 + 原因 + retry」。

链路：本地真实 HTTP/SSE 假厂商 → 真 NativeAdapter / AnthropicAdapter → AgentLoop →
TurnResult → TurnManager → TURN_END。模型调用一律用假厂商，不联网、不读真实密钥。

运行：cd backend; uv run --frozen pytest tests/test_acc_b2_incomplete_status.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.core.turn import INCOMPLETE_REASON, TurnManager
from agent.storage import turn_journal as journal_module
from agent.storage.turn_journal import TurnJournal
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

from _acc_b_provider import AccSseProvider

DECL = "[[QIO:ANSWER]]"


# ---- 装置 -------------------------------------------------------------------


@pytest.fixture()
def provider():
    server = AccSseProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


class _EchoTool(Tool):
    name = "echo"
    description = "echo（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    is_concurrency_safe = True

    async def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(ok=True, content="echo:%s" % kwargs.get("text", ""))


def _registry() -> tuple[ToolRegistry, _EchoTool]:
    registry = ToolRegistry()
    tool = _EchoTool()
    registry.register(tool)
    return registry, tool


@contextlib.asynccontextmanager
async def _loop(server: AccSseProvider, steps: list[dict], *, kind: str = "native"):
    registry, _tool = _registry()
    server.set(steps)
    if kind == "anthropic":
        from agent.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(
            api_key="sk-acc-b2-fake-0001",
            model="acc-b2-anthropic",
            endpoint="http://127.0.0.1:%d/v1" % server.server_port,
        )
        try:
            bus = EventBus()
            yield AgentLoop(adapter, registry, bus, turn_id="turn_accb2"), bus
        finally:
            await adapter.close()
        return
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-acc-b2-fake-0001",
        timeout=15.0,
    )
    try:
        bus = EventBus()
        yield AgentLoop(NativeAdapter(client=client, model="acc-b2-native"), registry, bus, turn_id="turn_accb2"), bus
    finally:
        await client.close()


async def _drive(result, journal: TurnJournal) -> tuple[str, dict]:
    """把真实 loop 的 TurnResult 喂给 TurnManager，返回 (turn_id, TURN_END data)。

    emitter 里按服务层同一口径把结束事实落台账（app.py::_record_turn_facts），
    所以这里同时验证「事件」与「刷新后仍可恢复的落库事实」。
    """
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))
        if name == "TURN_END":
            journal.record_facts(
                str(data.get("turn_id")),
                reason_code=data.get("reason_code"),
                reason=data.get("reason"),
                stopped_by=data.get("stopped_by"),
                actions=list(data.get("actions") or []),
            )

    async def runner(ctx) -> None:  # noqa: ANN001
        ctx.final_content = result.final_content
        ctx.result = {"ok": True, "turn": result.__dict__}

    manager = TurnManager(runner=runner, emitter=emitter)
    manager.set_journal(journal)
    ctx = manager.submit("回答我")
    await manager.wait(ctx.turn_id, timeout=10)
    await manager.shutdown()
    ends = [data for name, data in events if name == "TURN_END"]
    assert len(ends) == 1, ("一个 accepted turn 恰好一条 TURN_END", ends)
    return ctx.turn_id, ends[0]


# ---- 1. 五种「没有合法结束标记」的流都归到 incomplete ---------------------------


INCOMPLETE_CASES: dict[str, tuple[str, list[dict], str | None]] = {
    "native_abort": (
        "native",
        [{"chunks": [DECL + "\n", "前两句。", "第二句。"], "abort": True}],
        "前两句。第二句。",
    ),
    "anthropic_without_message_stop": (
        "anthropic",
        [{"chunks": [DECL + "\n", "半句回答"], "abort": True}],
        "半句回答",
    ),
    "usage_only": ("native", [{"usage_only": True}], None),
    "empty_stream": ("native", [{"abort": True}], None),
    "unfinished_tool_call": (
        "native",
        [
            {
                "tool_chunks": [
                    {"id": "call_b2", "name": "echo", "args_fragments": ['{"text": "hi"}']}
                ],
                "abort": True,
            }
        ],
        None,
    ),
}


@pytest.mark.parametrize("case", sorted(INCOMPLETE_CASES))
async def test_incomplete_eof_is_terminal_incomplete(case, provider, db_conn):
    kind, steps, expected_text = INCOMPLETE_CASES[case]
    async with _loop(provider, steps, kind=kind) as (loop, _bus):
        result = await loop.run("回答我")

    assert result.stop_reason_code == "incomplete_stream", (case, result.stop_reason_code)
    if expected_text is not None:
        # 已确认正文保留（不丢字、不补未确认后缀）
        assert result.final_content == expected_text, result.final_content

    journal = TurnJournal(db_conn)
    turn_id, end = await _drive(result, journal)

    assert end["status"] == "incomplete", end
    assert end["reason_code"] == "incomplete_stream", end
    assert end["stopped_by"] == "system", end
    assert end["reason"] == INCOMPLETE_REASON, end
    assert end["actions"] == ["retry"], end
    if expected_text is not None:
        assert end["final_content"] == expected_text, end["final_content"]

    # 刷新 / 换设备后仍能恢复「未完成 + 原因 + retry」
    facts = journal.facts([turn_id])[turn_id]
    assert facts["status"] == "incomplete", facts
    assert facts["reason_code"] == "incomplete_stream", facts
    assert facts["stopped_by"] == "system", facts
    assert facts["actions"] == ["retry"], facts
    print(
        "[诊断] B2 %s：status=%s reason_code=%s actions=%s 正文保留=%s"
        % (case, end["status"], end["reason_code"], end["actions"], end["final_content"])
    )


# ---- 2. 合法终止仍是 completed，只用 reason_code 区分 ---------------------------


async def test_length_limit_stays_completed(provider, db_conn):
    async with _loop(
        provider, [{"chunks": [DECL + "\n", "被长度截断的正文"], "finish": "length"}]
    ) as (loop, _bus):
        result = await loop.run("回答我")

    assert result.stop_reason_code == "length_limit", result.stop_reason_code
    journal = TurnJournal(db_conn)
    turn_id, end = await _drive(result, journal)

    assert end["status"] == "completed", ("厂商合法截断不是「未完成」", end)
    assert end["reason_code"] == "length_limit", end
    assert end["actions"] == ["retry"], end
    facts = journal.facts([turn_id])[turn_id]
    assert facts["status"] == "completed", facts
    assert facts["reason_code"] == "length_limit", facts


async def test_complete_stream_stays_completed_with_no_actions(provider, db_conn):
    async with _loop(provider, [{"chunks": [DECL + "\n", "完整回答。"]}]) as (loop, _bus):
        result = await loop.run("回答我")

    assert result.stop_reason_code == "none", result.stop_reason_code
    journal = TurnJournal(db_conn)
    turn_id, end = await _drive(result, journal)

    assert end["status"] == "completed", end
    assert end["reason_code"] == "none", end
    assert end["actions"] == [], end
    assert end["final_content"] == "完整回答。", end["final_content"]
    facts = journal.facts([turn_id])[turn_id]
    assert facts["status"] == "completed" and facts["actions"] == [], facts


# ---- 3. 取消优先于 incomplete --------------------------------------------------


async def test_user_cancel_wins_over_incomplete(db_conn):
    events: list[tuple[str, dict]] = []
    release = asyncio.Event()

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:  # noqa: ANN001
        await release.wait()
        # 取消发生之后 runner 仍然可能返回「流不完整」的结果：不得因此改写成 incomplete。
        ctx.final_content = "已经收到的半截正文"
        ctx.result = {
            "ok": True,
            "turn": {
                "stop_reason_code": "incomplete_stream",
                "stop_reason": INCOMPLETE_REASON,
                "stopped_by": "system",
            },
        }

    manager = TurnManager(runner=runner, emitter=emitter)
    manager.set_journal(TurnJournal(db_conn))
    ctx = manager.submit("回答我")
    while manager.active is None:
        await asyncio.sleep(0.005)
    assert manager.cancel_active() is True
    release.set()
    await manager.wait(ctx.turn_id, timeout=5)
    await manager.shutdown()

    ends = [data for name, data in events if name == "TURN_END"]
    assert len(ends) == 1
    end = ends[0]
    assert end["status"] == "cancelled", end
    assert end["reason_code"] == "user_stopped", end
    assert end["stopped_by"] == "user", end
    assert end["final_content"] == "已经收到的半截正文", end["final_content"]


# ---- 4. 台账层：incomplete 是终态 ----------------------------------------------


def test_journal_accepts_incomplete_as_terminal(db_conn):
    assert "incomplete" in journal_module.TERMINAL_STATUSES, (
        "台账必须把 incomplete 当终态，否则 terminal() 会把它写成 failed",
        journal_module.TERMINAL_STATUSES,
    )
    journal = TurnJournal(db_conn)
    journal.accepted(turn_id="turn_b2_journal", message="hi")
    journal.terminal("turn_b2_journal", "incomplete", reason="incomplete_stream")
    row = db_conn.execute(
        "SELECT status, reason FROM turn_journal WHERE turn_id = 'turn_b2_journal'"
    ).fetchone()
    assert row["status"] == "incomplete", dict(row)
    assert row["reason"] == "incomplete_stream", dict(row)


def test_prune_terminal_covers_every_terminal_status(db_conn):
    """终态集合扩到 5 个之后，清理语句必须同步。

    回归：prune_terminal 以前把占位符写死成 4 个，加入 incomplete 后会变成
    「5 个占位符、6 个绑定参数」——清理静默失败（只记 warning），终态行永不清理。
    """
    from datetime import datetime, timedelta, timezone

    journal = TurnJournal(db_conn)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for status in journal_module.TERMINAL_STATUSES:
        turn_id = "turn_b2_prune_%s" % status
        journal.accepted(turn_id=turn_id, message="hi")
        journal.terminal(turn_id, status)
        db_conn.execute(
            "UPDATE turn_journal SET ended_at = ? WHERE turn_id = ?", (old, turn_id)
        )
    assert journal.prune_terminal(days=7) == len(journal_module.TERMINAL_STATUSES), (
        "过期终态行必须真的被清掉（写死占位符会让清理整条失败）"
    )
