"""工具执行的**权威事实**：过程事件可以丢，已经发生的终态不能永久丢。

`ToolExecutionState` 只保存「现在在跑什么」和「最近跑完的结果」——
它是 snapshot 的唯一来源，所以 EventBus 是否完整与它无关。

这里覆盖容器本身的语义（写入 / 投影 / retention）；「丢了 TOOL_END 之后
HTTP snapshot 还能不能恢复」由 `test_tool_recovery.py` 覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.core.tool_state import ToolExecutionState


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_running_tool_is_reported_as_running_and_terminal_state_replaces_it():
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "fs_read")

    running = state.snapshot(active_turn_id="turn_a")
    assert [r["status"] for r in running] == ["running"]
    assert running[0]["tool_call_id"] == "call_1"
    assert running[0]["tool_name"] == "fs_read"
    assert running[0]["turn_id"] == "turn_a"
    assert running[0]["started_at"]
    assert running[0]["ended_at"] is None

    state.finish("turn_a", "call_1", "success")

    done = state.snapshot(active_turn_id="turn_a")
    assert len(done) == 1, "同一次调用只能有一条记录（不能开始一条、结束又补一条）"
    assert done[0]["status"] == "success"
    assert done[0]["ended_at"]
    assert done[0]["error_summary"] is None


def test_failed_tool_keeps_a_short_error_summary():
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "run_cmd")
    state.finish("turn_a", "call_1", "failed", error="x" * 500)

    (record,) = state.snapshot(active_turn_id="turn_a")
    assert record["status"] == "failed"
    assert record["error_summary"]
    assert len(record["error_summary"]) <= 200, "只留一行为用户看的摘要，不存完整输出"


def test_cancelled_is_its_own_status_not_a_failure():
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "run_cmd")
    state.finish("turn_a", "call_1", "cancelled")

    (record,) = state.snapshot(active_turn_id="turn_a")
    assert record["status"] == "cancelled"


def test_finish_without_a_start_still_records_the_terminal_fact():
    """TOOL_START 也丢了：结束事实照样是权威事实，不能因为缺 start 就丢弃。"""
    state = ToolExecutionState()
    state.finish("turn_a", "call_9", "success", tool_name="web_search")

    (record,) = state.snapshot(active_turn_id="turn_a")
    assert record["tool_call_id"] == "call_9"
    assert record["tool_name"] == "web_search"
    assert record["status"] == "success"
    assert record["started_at"]


def test_terminal_records_survive_the_turn_that_owns_them():
    """一个已经结束的 Turn 的终态仍然要在保留期内可查（断线后才重连也能恢复）。"""
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "fs_read")
    state.finish("turn_a", "call_1", "success")

    # 下一轮已经在跑：上一轮的终态仍要出现在快照里
    records = state.snapshot(active_turn_id="turn_b")
    assert [(r["tool_call_id"], r["status"]) for r in records] == [("call_1", "success")]


def test_running_record_of_a_turn_that_is_gone_is_reported_unknown():
    """Turn 已经不在了、记录还停在 running：服务器不能替它保证还在跑。

    这时唯一的诚实答案就是 unknown —— 既不伪造 success/failed，
    也不让界面永远显示「运行中」。
    """
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "fs_read")

    assert state.snapshot(active_turn_id="turn_a")[0]["status"] == "running"
    assert state.snapshot(active_turn_id=None)[0]["status"] == "unknown"
    assert state.snapshot(active_turn_id="turn_b")[0]["status"] == "unknown"


def test_active_turn_terminal_state_is_never_pruned_early():
    """active Turn 里已经结束的工具不得被 TTL 提前清理 —— 否则恢复又变成 unknown。"""
    clock = _Clock()
    state = ToolExecutionState(ttl_seconds=1.0, clock=clock)
    state.start("turn_a", "call_1", "fs_read")
    state.finish("turn_a", "call_1", "success")

    clock.advance(3600)
    state.prune(active_turn_id="turn_a")

    assert [r["status"] for r in state.snapshot(active_turn_id="turn_a")] == ["success"]


def test_turn_that_ended_is_reclaimed_by_ttl_and_by_max_count():
    clock = _Clock()
    state = ToolExecutionState(ttl_seconds=60.0, max_records=2, clock=clock)
    state.finish("turn_a", "call_1", "success")
    state.finish("turn_a", "call_2", "failed")
    state.finish("turn_a", "call_3", "success")

    # 超过 max_records：最旧的一条先走
    state.prune(active_turn_id="turn_b")
    assert [r["tool_call_id"] for r in state.snapshot(active_turn_id="turn_b")] == [
        "call_2",
        "call_3",
    ]

    # 超过 TTL：整体回收，界面只会看到「结果未收到（服务器也没有记录）」
    clock.advance(120)
    state.prune(active_turn_id="turn_b")
    assert state.snapshot(active_turn_id="turn_b") == []


def test_record_that_is_being_reclaimed_is_not_used_as_a_live_tool():
    clock = _Clock()
    state = ToolExecutionState(ttl_seconds=10.0, clock=clock)
    state.start("turn_a", "call_1", "fs_read")
    clock.advance(60)

    # Turn 还在跑：它的记录不能被回收（重连的客户端还需要这份活动事实）
    state.prune(active_turn_id="turn_a")
    assert [r["status"] for r in state.snapshot(active_turn_id="turn_a")] == ["running"]

    # Turn 结束了：stale running 记录可以回收，投影本来也只是 unknown
    state.prune(active_turn_id=None)
    assert state.snapshot(active_turn_id=None) == []


def test_state_is_keyed_by_turn_and_call_id_so_turns_never_share_it():
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "fs_read")
    state.start("turn_b", "call_1", "fs_read")

    state.finish("turn_b", "call_1", "failed")

    by_turn = {r["turn_id"]: r["status"] for r in state.snapshot(active_turn_id="turn_a")}
    assert by_turn == {"turn_a": "running", "turn_b": "failed"}


def test_same_tool_name_twice_in_one_turn_is_two_independent_records():
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "fs_read")
    state.start("turn_a", "call_2", "fs_read")
    state.finish("turn_a", "call_2", "failed", error="boom")

    records = {r["tool_call_id"]: r for r in state.snapshot(active_turn_id="turn_a")}
    assert records["call_1"]["status"] == "running"
    assert records["call_2"]["status"] == "failed"


def test_snapshot_can_be_filtered_by_turn_for_the_loop_view():
    state = ToolExecutionState()
    state.start("turn_a", "call_1", "fs_read")
    state.start("turn_b", "call_2", "fs_read")

    assert [r["tool_call_id"] for r in state.snapshot(turn_id="turn_b")] == ["call_2"]
