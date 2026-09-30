"""无进展暂停：同样的调用拿到同样的结果时，别再把预算烧完。

回归的缺口：一轮里反复做同一件事（读同一个文件、查同一个状态）既不会失败、
也不会被「失败次数」护栏拦下，只能等迭代预算耗尽 —— 到了那一步，预算与 token
已经花光了，而用户看到的只是一句「预算用完」。
"""

from __future__ import annotations

from test_loop import FakeChoice, FakeCompletion, FakeMessage, ScriptedClient, _make_loop, _tc


def _identical_calls(count: int = 50) -> list[FakeCompletion]:
    return [
        FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x"}')]))])
        for i in range(count)
    ]


async def test_identical_calls_stop_the_turn_early():
    client = ScriptedClient(_identical_calls())
    loop, _ = _make_loop(client, max_iterations=20)

    result = await loop.run("loop")

    assert result.phase.value == "stopped"
    # 不是靠预算停的：预算还剩很多
    assert result.iterations_used < 20
    assert "没有新的进展" in (result.final_content or "")


async def test_no_progress_emits_a_warning_event():
    """暂停必须让界面看得到（WARNING 事件，带机器可读的 code）。"""
    client = ScriptedClient(_identical_calls())
    loop, bus = _make_loop(client, max_iterations=20)
    collected: list[str] = []

    async def consume():
        async for chunk in bus.stream():
            collected.append(chunk)

    import asyncio

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await loop.run("loop")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "event: WARNING" in joined
    assert "no_progress" in joined
    assert "没有新的进展" in joined


async def test_a_different_result_keeps_the_turn_going():
    """每一轮拿到的东西都不同 = 确实在往前走，不该被暂停。"""
    client = ScriptedClient(
        [
            FakeCompletion(
                [FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "%d"}' % i)]))]
            )
            for i in range(6)
        ]
    )
    loop, _ = _make_loop(client, max_iterations=20)

    result = await loop.run("loop")

    assert result.final_content == "final answer"
    assert result.tool_calls_made == 6


class _ApprovingService:
    """用户每次都选「继续」的审批服务。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", "approved")


async def test_user_can_choose_to_keep_going():
    """有审批通道时，「无进展」是**暂停问一下**，不是直接判死。"""
    client = ScriptedClient(_identical_calls(count=12))
    approvals = _ApprovingService()
    loop, _ = _make_loop(client, max_iterations=50, approvals=approvals)

    result = await loop.run("loop")

    assert result.phase.value == "done"
    assert result.final_content == "final answer"
    # 问过，而且问的是「无进展」这条原因
    assert approvals.requests
    assert approvals.requests[0][1].get("reason") == "no_progress"


async def test_user_can_choose_to_stop():
    class _RejectingService:
        async def request(self, kind: str, payload: dict, **kwargs):
            from agent.tools.approval import ApprovalResult

            return ApprovalResult("appr_test", "rejected")

    client = ScriptedClient(_identical_calls(count=12))
    loop, _ = _make_loop(client, max_iterations=50, approvals=_RejectingService())

    result = await loop.run("loop")

    assert result.phase.value == "stopped"
    assert "按你的选择停下来了" in (result.final_content or "")
